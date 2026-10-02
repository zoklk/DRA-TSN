#!/usr/bin/env python3
"""fig_mixed helper — four subcommands sharing one kubectl JSON layer.

plan   per-node filler counts to reach a target utilisation (request arithmetic:
       the scheduler sees requests, not usage, so the regime is built from
       requests alone)
free   unrequested mcpu on one node — fill() uses it to prove the two surviving
       TSN nodes are interchangeable
util   append one pre/post utilisation snapshot per pool to util.csv
watch  the record stage: poll the API during a node fault and decompose the
       recovery into detect / evict / schedule / ready, ms from t0 (docker stop)

The loop polls to notice that something happened, but the time it REPORTS comes
from the API objects themselves (see server_or below). Observation time and
event time are not the same thing, and conflating them cost real numbers: with
~60 pods in the namespace the 0.25 s loop falls behind and reported 4-6 s for a
transition the pod's own conditions timestamped at 1-2 s.
"""
import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time

KUBECTL = os.environ.get("KUBECTL", "kubectl")


def now_ms() -> int:
    return int(time.time() * 1000)


# --- server-side timestamps --------------------------------------------------
# Stage times are read from the API objects, not from when this loop happened to
# notice. The 0.25 s poll cannot keep up once the namespace holds ~60 pods — each
# iteration is a `kubectl get pods` — and the observation lag inflated
# sched->ready to 4-6 s where the pod's own conditions said 1-2 s.
#
# Every stage except t0 has a server timestamp: the node Ready condition, the
# pod's deletionTimestamp, and the pod's PodScheduled / Ready conditions. t0 is
# a `docker stop`, which happens outside the cluster and has no server-side
# equivalent.
#
# Mixing a local t0 with server times is only sound because kind is single-host:
# the node containers share the host clock, so apiserver time IS host time. On a
# real multi-node cluster this would need clock-skew handling.
#
# Resolution is 1 s (metav1.Time serialises RFC3339 seconds), so these values
# carry +-1 s. That is coarse but an order of magnitude better than the poll lag
# it replaces, and every stage here is seconds to tens of seconds.

def ts_ms(rfc3339):
    if not rfc3339:
        return None
    try:
        return int(dt.datetime.fromisoformat(
            rfc3339.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def cond_true(obj, cond_type):
    for c in (obj.get("status", {}).get("conditions") or []):
        if c.get("type") == cond_type:
            return c.get("status") == "True"
    return False


def cond_ms(obj, cond_type):
    """lastTransitionTime of one status condition, in epoch ms."""
    for c in (obj.get("status", {}).get("conditions") or []):
        if c.get("type") == cond_type:
            return ts_ms(c.get("lastTransitionTime"))
    return None


def server_or(observed, server, t0):
    """server-side offset from t0 when available, else the observed one.

    Falls back rather than failing: a condition can be missing on a pod caught
    mid-transition, and a slightly late number beats a hole in the row.
    """
    return (server - t0) if server is not None else observed


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


# --- free --------------------------------------------------------------------

def cmd_free(a):
    """Unrequested mcpu on one node.

    fill() compares this between the two surviving TSN nodes. They must match:
    if one has more headroom, LeastAllocated prefers it and the vanilla arm's
    (k-1)/(M-1) stops being a fair coin.
    """
    nodes = kget("nodes")
    used = live_requests_by_node(kget("pods", "-A"))
    alloc = node_alloc_mcpu(nodes)
    print(max(0, alloc.get(a.node, 0) - used.get(a.node, 0)))


# --- stream ownership --------------------------------------------------------

def live_tx_domains(node):
    """Domains for which `node` still publishes a TX TSNStream.

    This is the judgment (ADR-0039 §4). It is a control-plane fact, which is the
    only kind available here: kind runs -dataplane=false, so no eBPF map or
    tagged frame exists to inspect. A pod can be Running on a TSN-labelled node
    and still have no path — that silent failure is exactly what the vanilla arm
    produces, and the only way to see it is to ask which streams survived.
    """
    try:
        d = kget("tsnstreams")
    except Exception:
        return set()
    # field is domainId, not domainID — the CRD follows the JSON tag, and getting
    # this wrong makes every stream_ok read 0 rather than erroring
    return {s["spec"].get("domainId") for s in d.get("items", [])
            if s["spec"].get("nodeName") == node
            and s["spec"].get("direction") == "TX"}


# --- watch -------------------------------------------------------------------

def pods_snapshot(ns):
    got = kget("pods", "-n", ns, "-l", "exp=fig-mixed")
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
        # the tracked group = the guaranteed workloads sitting on the dying node.
        # ADR-0039 §4 keeps ai-inference elsewhere, so in practice this is the
        # single vPLC — the dict shape survives from when two pods were staged.
        if labels.get("mixed-group") == "guaranteed" and \
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
        vict_done = all("resched_ms" in v or v.get("tier") not in ("ai-training",)
                        for v in victims.values())
        return vplc_done and vict_done

    def classify_deletion(uid, t, pod=None):
        """an initial pod got a deletionTimestamp or vanished outright —
        terminationGracePeriodSeconds: 0 removes the object between polls,
        so absence is the reliable signal, the timestamp a lucky catch."""
        info = initial[uid]
        app = info["app"]
        dts = ts_ms(pod["metadata"].get("deletionTimestamp")) if pod else None
        # dead-node vPLC being evicted by the taint manager
        if app in fault_vplcs and info["name"] == fault_vplcs[app]["pod0"]:
            stages[app].setdefault("evict", server_or(t - t0, dts, t0))
        # any other deletion on a surviving node = preemption victim
        elif info["node"] != a.node0 and uid not in victims:
            victims[uid] = {"name": info["name"], "app": app,
                            "tier": info["tier"], "node": info["node"],
                            "evict_ms": server_or(t - t0, dts, t0)}
            victim_apps.setdefault(app, uid)

    gone = set()
    while now_ms() < deadline:
        t = now_ms()
        if detect_ms is None and not node_ready(a.node0):
            n = kget("node", a.node0)
            detect_ms = server_or(t - t0, cond_ms(n, "Ready") if n else None, t0)

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
                    classify_deletion(uid, t, p)
                continue

            # new pod, born after t0
            if app in fault_vplcs and not deleted:
                s = stages[app]
                if spec.get("nodeName") and "sched" not in s:
                    s["sched"] = server_or(t - t0, cond_ms(p, "PodScheduled"), t0)
                    s["node1"] = spec["nodeName"]
                # Trigger on the Ready condition, not on phase == Running:
                # Running precedes Ready, and reading the Ready condition while
                # it is still False yields the timestamp of an earlier
                # transition. The column is called ready_ms, so it should mean
                # Ready.
                if cond_true(p, "Ready") and "ready" not in s:
                    s.setdefault("sched", server_or(t - t0, cond_ms(p, "PodScheduled"), t0))
                    s.setdefault("node1", spec.get("nodeName", "?"))
                    s["ready"] = server_or(t - t0, cond_ms(p, "Ready"), t0)
            elif app in victim_apps and not deleted:
                v = victims[victim_apps[app]]
                if cond_true(p, "Ready") and "resched_ms" not in v:
                    v["resched_ms"] = server_or(t - t0, cond_ms(p, "Ready"), t0)
                    v["node1"] = spec.get("nodeName", "?")

        for uid in set(initial) - present - gone:
            classify_deletion(uid, t)
            gone.add(uid)

        if all_done():
            break
        time.sleep(0.25)

    # --- emit ---------------------------------------------------------------
    det = detect_ms if detect_ms is not None else -1
    # app -> the stream domain it needs, e.g. "vplc-line=cell-A,ai-inference=cell-B"
    want_domain = dict(kv.split("=", 1) for kv in a.domains.split(",") if kv)
    domains_by_node = {}
    with open(os.path.join(a.out, "mixed.csv"), "a") as f:
        for app, s in sorted(stages.items()):
            pending = 0 if "ready" in s else 1
            node1 = s.get("node1", "PENDING")
            dom = want_domain.get(app, "")
            # The verdict. Landing on a TSN-labelled node is not enough — the
            # node must still publish a TX stream in THIS workload's domain.
            # on_tsn1 and stream_ok diverge exactly where the vanilla arm fails.
            if dom and node1 != "PENDING":
                if node1 not in domains_by_node:
                    domains_by_node[node1] = live_tx_domains(node1)
                stream_ok = 1 if dom in domains_by_node[node1] else 0
            else:
                stream_ok = -1
            f.write(",".join(str(x) for x in [
                a.arm, a.trial, app, a.node0,
                1 if a.node0 in tsn else 0,
                node1, 1 if node1 in tsn else 0,
                dom, stream_ok,
                det, s.get("evict", -1), s.get("sched", -1), s.get("ready", -1),
                s.get("ready", -1), pending]) + "\n")
    with open(os.path.join(a.out, "evictions.csv"), "a") as f:
        for v in victims.values():
            correct = 1 if v["tier"] in ("ai-training", "logging") else 0
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
        subprocess.run([KUBECTL, "get", "pods", "-n", a.ns, "-l", "exp=fig-mixed",
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

    p = sub.add_parser("free")
    p.add_argument("--node", required=True)
    p.set_defaults(fn=cmd_free)

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
    p.add_argument("--domains", default="",
                   help="app=domain pairs, comma separated — which stream domain "
                        "each tracked workload needs (vplc-line=cell-A,...)")
    p.set_defaults(fn=cmd_watch)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
