#!/usr/bin/env python3
"""fig_regime helper — three subcommands sharing one kubectl JSON layer.

plan   per-node filler counts to reach a target utilisation (request arithmetic,
       ADR-0037: the scheduler sees requests, not usage, so the regime is built
       from requests alone)
util   append one pre/post utilisation snapshot per pool to util.csv
watch  the record stage: poll the API during a node fault and decompose the
       recovery into detect / evict / schedule / ready, ms from t0 (docker stop).
       wait_for's 1 s poll is too coarse for the sub-second stages, hence this
       0.25 s loop with local monotonic-ish wall clock (same clock as t0).

All timestamps are local now_ms values; server-side condition timestamps and
events are dumped to raw/ for cross-checking, not used for the figure.
"""
import argparse
import json
import os
import subprocess
import sys
import time

KUBECTL = os.environ.get("KUBECTL", "kubectl")


def now_ms() -> int:
    return int(time.time() * 1000)


def kget(*args):
    out = subprocess.run(
        [KUBECTL, "get", *args, "-o", "json"],
        capture_output=True, text=True)
    if out.returncode != 0:
        return None
    return json.loads(out.stdout)


def cpu_mcpu(q: str) -> int:
    if not q:
        return 0
    if q.endswith("m"):
        return int(q[:-1])
    return int(float(q) * 1000)


def pod_request_mcpu(pod) -> int:
    total = 0
    for c in pod["spec"].get("containers", []):
        total += cpu_mcpu(c.get("resources", {}).get("requests", {}).get("cpu", ""))
    return total


def live_requests_by_node(pods):
    """sum of cpu requests of non-terminated, scheduled pods, per node"""
    by_node = {}
    for p in pods["items"]:
        if p["status"].get("phase") in ("Succeeded", "Failed"):
            continue
        node = p["spec"].get("nodeName")
        if not node:
            continue
        by_node[node] = by_node.get(node, 0) + pod_request_mcpu(p)
    return by_node


def node_alloc_mcpu(nodes):
    return {n["metadata"]["name"]: cpu_mcpu(n["status"]["allocatable"]["cpu"])
            for n in nodes["items"]}


def node_ready(name) -> bool:
    n = kget("node", name)
    if n is None:
        return False
    for c in n["status"].get("conditions", []):
        if c["type"] == "Ready":
            return c["status"] == "True"
    return False


# --- plan --------------------------------------------------------------------

def cmd_plan(a):
    """emit: <node> <unit-pod count> <trimmer mcpu>

    The trimmer pins the node's remaining headroom to exactly (100-util)% of
    allocatable. Without it the flooring of count leaves up to unit-1 mcpu of
    slack on top of the headroom, which in the gate run was enough to admit a
    2000m vPLC without preemption — the regime must not be that loose."""
    nodes = kget("nodes")
    pods = kget("pods", "-A")
    alloc = node_alloc_mcpu(nodes)
    used = live_requests_by_node(pods)
    want = [n for n in a.nodes.split() if n]
    for n in want:
        target = alloc[n] * a.util // 100 - a.reserve_mcpu
        free = max(0, target - used.get(n, 0))
        count = free // a.unit_mcpu
        trim = free % a.unit_mcpu
        if trim < 50:                 # below any meaningful request
            trim = 0
        print(f"{n} {count} {trim}")


# --- util --------------------------------------------------------------------

def cmd_util(a):
    nodes = kget("nodes")
    pods = kget("pods", "-A")
    alloc = node_alloc_mcpu(nodes)
    used = live_requests_by_node(pods)
    ready = {n["metadata"]["name"]:
             any(c["type"] == "Ready" and c["status"] == "True"
                 for c in n["status"].get("conditions", []))
             for n in nodes["items"]}
    tsn = set(a.tsn_nodes.split())
    workers = [n["metadata"]["name"] for n in nodes["items"]
               if "node-role.kubernetes.io/control-plane"
               not in n["metadata"].get("labels", {})]
    pools = {"tsn": [n for n in workers if n in tsn],
             "general": [n for n in workers if n not in tsn]}
    with open(a.csv, "a") as f:
        for pool, members in pools.items():
            # post-fault utilisation is a claim about surviving capacity
            members = [n for n in members if ready[n]]
            req = sum(used.get(n, 0) for n in members)
            al = sum(alloc[n] for n in members)
            pct = round(100.0 * req / al, 1) if al else 0.0
            f.write(f"{a.arm},{a.trial},{a.phase},{pool},{req},{al},{pct}\n")


# --- watch -------------------------------------------------------------------

def pods_snapshot(ns):
    got = kget("pods", "-n", ns, "-l", "exp=fig-regime")
    return got["items"] if got else []


def cmd_watch(a):
    t0 = a.t0_ms
    tsn = set(a.tsn_nodes.split())
    deadline = t0 + a.timeout * 1000

    # initial state: which vPLC pods sat on the dead node, and every live pod's
    # identity, so later deletionTimestamps can be classified.
    initial = {}          # uid -> {name, app, tier, node}
    fault_vplcs = {}      # app -> {pod0}
    for p in pods_snapshot(a.ns):
        uid = p["metadata"]["uid"]
        labels = p["metadata"].get("labels", {})
        initial[uid] = {
            "name": p["metadata"]["name"],
            "app": labels.get("app", "?"),
            "tier": labels.get("tier", "?"),
            "node": p["spec"].get("nodeName", ""),
        }
        # fixture v2: the tracked group = the vPLC and its camera-feed gateway
        # (vplc-group label), both staged through the same pipeline
        if labels.get("vplc-group") == "regime" and \
           p["spec"].get("nodeName") == a.node0:
            fault_vplcs[labels["app"]] = {"pod0": p["metadata"]["name"]}

    if not fault_vplcs:
        print(f"watch: no vPLC pods found on {a.node0}", file=sys.stderr)
        sys.exit(1)

    detect_ms = None
    stages = {app: {} for app in fault_vplcs}   # app -> detect/evict/sched/ready/node1
    victims = {}                                # uid -> {..., evict_ms, resched_ms}
    victim_apps = {}                            # app -> victim uid (for replacement tracking)

    def all_done():
        vplc_done = all("ready" in s or "pending" in s for s in stages.values())
        vict_done = all("resched_ms" in v or v.get("tier") not in ("it-ai",)
                        for v in victims.values())
        return vplc_done and vict_done

    def classify_deletion(uid, t):
        """an initial pod got a deletionTimestamp or vanished outright —
        terminationGracePeriodSeconds: 0 removes the object between polls,
        so absence is the reliable signal, the timestamp a lucky catch."""
        info = initial[uid]
        app = info["app"]
        # dead-node vPLC being evicted by the taint manager
        if app in fault_vplcs and info["name"] == fault_vplcs[app]["pod0"]:
            stages[app].setdefault("evict", t - t0)
        # any other deletion on a surviving node = preemption victim
        elif info["node"] != a.node0 and uid not in victims:
            victims[uid] = {"name": info["name"], "app": app,
                            "tier": info["tier"], "node": info["node"],
                            "evict_ms": t - t0}
            victim_apps.setdefault(app, uid)

    gone = set()
    while now_ms() < deadline:
        t = now_ms()
        if detect_ms is None and not node_ready(a.node0):
            detect_ms = t - t0

        present = set()
        for p in pods_snapshot(a.ns):
            uid = p["metadata"]["uid"]
            present.add(uid)
            meta, spec, status = p["metadata"], p["spec"], p["status"]
            labels = meta.get("labels", {})
            app = labels.get("app", "?")
            deleted = bool(meta.get("deletionTimestamp"))

            if uid in initial:
                if deleted:
                    classify_deletion(uid, t)
                continue

            # new pod, born after t0
            if app in fault_vplcs and not deleted:
                s = stages[app]
                if spec.get("nodeName") and "sched" not in s:
                    s["sched"] = t - t0
                    s["node1"] = spec["nodeName"]
                if status.get("phase") == "Running" and "ready" not in s:
                    s.setdefault("sched", t - t0)
                    s.setdefault("node1", spec.get("nodeName", "?"))
                    s["ready"] = t - t0
            elif app in victim_apps and not deleted:
                v = victims[victim_apps[app]]
                if status.get("phase") == "Running" and "resched_ms" not in v:
                    v["resched_ms"] = t - t0
                    v["node1"] = spec.get("nodeName", "?")

        for uid in set(initial) - present - gone:
            classify_deletion(uid, t)
            gone.add(uid)

        if all_done():
            break
        time.sleep(0.25)

    # --- emit ---------------------------------------------------------------
    det = detect_ms if detect_ms is not None else -1
    with open(os.path.join(a.out, "regime.csv"), "a") as f:
        for app, s in sorted(stages.items()):
            pending = 0 if "ready" in s else 1
            node1 = s.get("node1", "PENDING")
            f.write(",".join(str(x) for x in [
                a.arm, a.trial, app, a.node0,
                1 if a.node0 in tsn else 0,
                node1, 1 if node1 in tsn else 0,
                det, s.get("evict", -1), s.get("sched", -1), s.get("ready", -1),
                s.get("ready", -1), pending]) + "\n")
    with open(os.path.join(a.out, "evictions.csv"), "a") as f:
        for v in victims.values():
            correct = 1 if v["tier"] == "it-ai" else 0
            f.write(",".join(str(x) for x in [
                a.arm, a.trial, v["name"], v["tier"], v["node"],
                correct, v.get("resched_ms", -1)]) + "\n")

    # server-side record for cross-checking the local clocks
    raw = os.path.join(a.out, "raw")
    os.makedirs(raw, exist_ok=True)
    with open(os.path.join(raw, f"events-{a.arm}-{a.trial}.txt"), "w") as f:
        subprocess.run([KUBECTL, "get", "events", "-n", a.ns,
                        "--sort-by=.lastTimestamp"], stdout=f, stderr=f, text=True)
    with open(os.path.join(raw, f"pods-{a.arm}-{a.trial}.json"), "w") as f:
        subprocess.run([KUBECTL, "get", "pods", "-n", a.ns, "-l", "exp=fig-regime",
                        "-o", "json"], stdout=f, stderr=f, text=True)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan")
    p.add_argument("--nodes", required=True)
    p.add_argument("--util", type=int, required=True)
    p.add_argument("--unit-mcpu", type=int, required=True)
    p.add_argument("--reserve-mcpu", type=int, default=0)
    p.set_defaults(fn=cmd_plan)

    p = sub.add_parser("util")
    p.add_argument("--arm", required=True)
    p.add_argument("--trial", required=True)
    p.add_argument("--phase", required=True, choices=["pre", "post"])
    p.add_argument("--tsn-nodes", required=True)
    p.add_argument("--csv", required=True)
    p.set_defaults(fn=cmd_util)

    p = sub.add_parser("watch")
    p.add_argument("--arm", required=True)
    p.add_argument("--trial", required=True)
    p.add_argument("--node0", required=True)
    p.add_argument("--t0-ms", type=int, required=True)
    p.add_argument("--tsn-nodes", required=True)
    p.add_argument("--ns", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--timeout", type=int, default=240)
    p.set_defaults(fn=cmd_watch)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
