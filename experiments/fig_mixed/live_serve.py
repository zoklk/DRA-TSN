#!/usr/bin/env python3
"""fig_mixed live watcher — the live half of the ADR-0039 contract.

One server for the whole shadow: N kind-cluster pollers (one per arm, each
pinned to its own kubeconfig), the declared physical-PLC state machine, and
one HTTP endpoint the Isaac extension (isaac-sim/exts/dratsn.live) pulls.

    python3 live_serve.py \
        --cluster dratsn=$HOME/.kube/kind-dratsn.kubeconfig \
        --cluster vanilla=$HOME/.kube/kind-vanilla.kubeconfig \
        --state-dir results/live

Cluster observation is the k8s API (kubectl JSON polls, 0.25 s — same rules
as watch_fault.py, ported as an incremental state machine). Field names in
each arm snapshot follow the feed v2 schema (documented in this file and in
SSOT). Fault injection/restore and the PLC fail/repair ride HTTP because
they act outside the k8s API (docker stop/start; a declared track).

  GET  /state              {v:3, ts_ms, lam_ms, seq, arms:{<arm>:...}, plc:...}
  GET  /healthz
  POST /inject  {cluster, node?, force?}    docker stop + state-env handoff
  POST /restore {cluster, node?}            docker start + MAC pin-back
  POST /plc/fail                            declared repair countdown starts
  POST /plc/repair                          immediate repair

The staged run.sh driver stays the baked/demo common path: inject-fault's
state-env file is the same handoff, so run.sh record keeps working.
"""
import argparse
import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import watch_fault as wf

# declared display ledger, mirror of run.sh bw_of / ACCS /
# MAX_LATENCY_NS (and of kind/deploy.sh ACC) — values are declared, not
# measured (feed v2.1 note)
# mirror of run.sh bw_of — the fig_regime tiers this used to carry matched
# nothing in fig_mixed, so the scene's bandwidth ledger read 1 Mbps everywhere
BW_MBPS = {"vplc-line": 5, "ai-inference": 150,
           "ai-training": 60, "logging": 10}
ACC_NS = [400000, 100000, 250000, 400000, 400000, 400000]
MAX_LATENCY_NS = 1_000_000
NET_CAPACITY_MBPS = 1000

TSN_LABEL = "tsn.dra.net/capable"
CP_LABEL = "node-role.kubernetes.io/control-plane"


def container_of(node):
    """kind hardcodes container names (<cluster>-worker, -worker2, ...) while
    K8s node names carry full numbering (worker1..worker6 via
    nodeRegistration, kind/kind-config.yaml): worker1 maps back to the
    unnumbered container."""
    return node[:-1] if node.endswith("worker1") else node


def read_state_env(path):
    """run.sh inject-fault handoff file: NODE0=<node>, T0=<ms>"""
    if not path or not os.path.exists(path):
        return None, None, None
    node0 = t0 = None
    with open(path) as f:
        for line in f:
            k, _, v = line.strip().partition("=")
            if k == "NODE0":
                node0 = v
            elif k == "T0":
                t0 = int(v)
    return node0, t0, os.path.getmtime(path)


class ClusterPoller(threading.Thread):
    """One arm = one kind cluster = one kubeconfig. The kubeconfig is pinned
    per poller (never the shared current-context — deploy.sh mutates that)."""

    def __init__(self, arm, kubeconfig, state_path, ns, interval,
                 domains=None, timeline=None):
        super().__init__(daemon=True)
        self.arm = arm
        # app -> stream domain it needs, e.g. {"vplc-line": "cell-A"}. The live
        # verdict: landing on a TSN-labelled node is not enough, the node must
        # still publish a TX stream in THIS workload's domain. Computed here
        # rather than after the fact because the scene paints correct/incorrect
        # as it happens — a post-hoc CSV is too late for that.
        self.domains = domains or {}
        self.timeline = timeline
        self._tl_last = None
        self.ns = ns
        self.state = state_path
        self.interval = interval
        self.env = dict(os.environ,
                        KUBECONFIG=os.path.expanduser(kubeconfig))
        self.lock = threading.Lock()
        self.snapshot = None
        self.seq = 0
        self.stop_evt = threading.Event()
        self._reset_fault()
        self._state_mtime = None
        # node -> healthy-baseline eth0 MAC (see /restore). Persisted next to
        # the state env so a server restart while a node is down does not
        # lose the baseline (that gap once cost a node its TX streams).
        self.mac_file = os.path.join(
            os.path.dirname(state_path) or ".", f"macs-{arm}.json")
        try:
            with open(self.mac_file) as f:
                self.macs = json.load(f)
        except Exception:
            self.macs = {}

    # -- kubectl against this poller's cluster -------------------------------

    def _kget(self, *args):
        out = subprocess.run([wf.KUBECTL, "get", *args, "-o", "json"],
                             capture_output=True, text=True, env=self.env)
        if out.returncode != 0:
            return None
        return json.loads(out.stdout)

    def _stream_domains(self):
        """node -> the TX stream domains it still publishes (one kubectl call).

        This is the piece of the scene's path premise that must be MEASURED:
        the HA layout puts a workload's stream on k of the M TSN nodes
        (ADR-0039 §2), so a TSN node without that domain has a NIC and nothing
        else. Same source as `_tx_domains`, batched for the whole snapshot.
        """
        d = self._kget("tsnstreams")
        if not d:
            return {}
        out = {}
        for x in d.get("items", []):
            spec = x["spec"]
            if spec.get("direction") != "TX":
                continue
            node, dom = spec.get("nodeName"), spec.get("domainId")
            if node and dom:
                out.setdefault(node, set()).add(dom)
        return {k: sorted(v) for k, v in out.items()}

    def _node_ready(self, name):
        n = self._kget("node", name)
        if n is None:
            return False
        return any(c["type"] == "Ready" and c["status"] == "True"
                   for c in n["status"].get("conditions", []))

    def _pods_snapshot(self):
        got = self._kget("pods", "-n", self.ns, "-l", "exp=fig-mixed")
        return got["items"] if got else []

    # -- fault episode state --------------------------------------------------

    def _reset_fault(self):
        self.pending_clear = False
        self.t0 = None
        self.node0 = None
        self.initial = {}        # uid -> {name, app, tier, node, mcpu}
        self.fault_vplcs = {}    # app -> pod0 name (mixed-group pods on node0)
        self.detect_ms = None
        self.stages = {}         # app -> {evict, sched, ready, node1}
        self.victims = {}        # uid -> {...}
        self.victim_apps = {}
        self.gone = set()
        self.events = []
        self.wedge_healed = False

    def _event(self, t_ms, kind, msg):
        self.events.append({"t_ms": t_ms, "kind": kind, "msg": msg})
        self.events = self.events[-50:]
        print(f"[{self.arm} {t_ms/1000.0:8.1f}s] {kind}: {msg}", flush=True)

    # -- one poll ------------------------------------------------------------

    def poll_once(self):
        if self.pending_clear:    # /restore of the fault node ends the episode
            self._reset_fault()
        nodes_j = self._kget("nodes")
        pods = self._pods_snapshot()
        if nodes_j is None:
            return None

        # fault handoff: a (re)written state env is a new injection. A stale
        # file must not re-arm the machine: arm only when the injection is
        # fresh (<90 s) or the node is actually NotReady (restart mid-fault).
        node0, t0, mtime = read_state_env(self.state)
        if t0 is not None and mtime != self._state_mtime:
            self._state_mtime = mtime
            fresh = wf.now_ms() - t0 < 90_000
            if fresh or not self._node_ready(node0):
                self._reset_fault()
                self._state_mtime = mtime
                self.node0, self.t0 = node0, t0
                self._freeze_initial(pods)
                if not self.fault_vplcs:
                    # no vPLC on the killed node: there is no failover story
                    # to stage-decompose, so no episode is armed — the dead
                    # node still greys out (display keys off per-node ready)
                    # and further injects are not blocked
                    self._event(0, "fault",
                                f"docker stop {node0} — no vPLC there, "
                                "background churn only (no episode)")
                    ev = self.events
                    self._reset_fault()
                    self.events = ev
                else:
                    self._event(0, "fault", f"docker stop {node0} (t0={t0})")
            else:
                print(f"[{self.arm}] ignoring stale state env "
                      f"(t0 {t0}, {node0} Ready)", flush=True)

        workers = [n for n in nodes_j["items"]
                   if CP_LABEL not in n["metadata"].get("labels", {})]

        # healthy-baseline MAC capture, once per Ready node. docker stop/start
        # regenerates a kind node's eth0 MAC, which breaks the CNC stream ×
        # TSNNic join and GCs the node's TX TSNStreams (ADR-0037); /restore
        # puts the recorded MAC back so the join re-forms on its own. After
        # unmanaged restarts run `run.sh repair <node>` once.
        for n in workers:
            name = n["metadata"]["name"]
            if name not in self.macs and any(
                    c["type"] == "Ready" and c["status"] == "True"
                    for c in n["status"].get("conditions", [])):
                try:
                    out = subprocess.run(
                        ["docker", "exec", container_of(name), "cat",
                         "/sys/class/net/eth0/address"],
                        capture_output=True, text=True, timeout=10)
                    if out.returncode == 0 and out.stdout.strip():
                        self.macs[name] = out.stdout.strip()
                        os.makedirs(os.path.dirname(self.mac_file) or ".",
                                    exist_ok=True)
                        with open(self.mac_file, "w") as f:
                            json.dump(self.macs, f)
                except Exception:
                    pass

        tsn_names = sorted(n["metadata"]["name"] for n in workers
                           if n["metadata"].get("labels", {}).get(TSN_LABEL) == "true")
        gen_names = sorted(n["metadata"]["name"] for n in workers
                           if n["metadata"]["name"] not in tsn_names)
        ready = {n["metadata"]["name"]:
                 any(c["type"] == "Ready" and c["status"] == "True"
                     for c in n["status"].get("conditions", []))
                 for n in workers}
        alloc = wf.node_alloc_mcpu({"items": workers})

        t = wf.now_ms()
        if self.t0 is not None:
            self._classify(pods, t)
            self._heal_prepare_wedge(t)

        # per-node display ledger from live pods (tier label = PriorityClass)
        by_node = {}
        tracked = None
        peer = None
        for p in pods:
            meta, spec, status = p["metadata"], p["spec"], p["status"]
            if status.get("phase") in ("Succeeded", "Failed"):
                continue
            node = spec.get("nodeName")
            labels = meta.get("labels", {})
            tier = labels.get("tier", "?")
            if node:
                slot = by_node.setdefault(node, {}).setdefault(
                    tier, {"count": 0, "mcpu": 0})
                slot["count"] += 1
                slot["mcpu"] += wf.pod_request_mcpu(p)
            if labels.get("mixed-group") == "guaranteed" and \
                    not meta.get("deletionTimestamp"):
                info = {"node": node, "mcpu": wf.pod_request_mcpu(p),
                        "phase": status.get("phase"),
                        "name": meta["name"], "uid": meta["uid"]}
                if tier.startswith("vplc"):
                    info["app"] = labels.get("app", "vplc-line")
                    tracked = self._merge_group(tracked, info)
                else:
                    info["tier"] = tier
                    peer = self._merge_group(peer, info)

        domains = self._stream_domains()
        node_list = []
        for i, name in enumerate(tsn_names):
            node_list.append({
                "name": name, "tsn": True, "ready": ready.get(name, False),
                "alloc_mcpu": alloc.get(name, 0),
                "pods": by_node.get(name, {}),
                "mbps": self._mbps(by_node.get(name, {})),
                # measured: which streams this node actually publishes
                "domains": domains.get(name, []),
                "n": {"max_latency_ns": MAX_LATENCY_NS,
                      "accumulated_ns": ACC_NS[i % len(ACC_NS)]}})
        for name in gen_names:
            node_list.append({
                "name": name, "tsn": False, "ready": ready.get(name, False),
                "alloc_mcpu": alloc.get(name, 0),
                "pods": by_node.get(name, {}),
                "mbps": self._mbps(by_node.get(name, {}))})

        util = {}
        for pool, members in (("tsn", tsn_names), ("general", gen_names)):
            live = [n for n in members if ready.get(n)]
            req = sum(v["mcpu"] for n in live
                      for v in by_node.get(n, {}).values())
            al = sum(alloc.get(n, 0) for n in live)
            util[pool] = round(100.0 * req / al, 1) if al else 0.0

        snap = self._assemble(node_list, util, tracked, peer, pods, t)
        self._append_timeline(snap, t)
        return snap

    def _tx_domains(self, node):
        """Domains for which `node` still publishes a TX TSNStream."""
        d = self._kget("tsnstreams")
        if not d:
            return set()
        # field is domainId, not domainID — the CRD follows the JSON tag
        return {x["spec"].get("domainId") for x in d.get("items", [])
                if x["spec"].get("nodeName") == node
                and x["spec"].get("direction") == "TX"}

    def _append_timeline(self, snap, t):
        """One line per state transition. This file is the ONLY input to the
        production-waveform figure, and it is written by the same process that
        paints the scene, so figure and animation cannot disagree."""
        if not (self.timeline and snap):
            return
        # v3 arm snapshot exposes the tracked workload as "tracked"; "cells" is
        # the baked feed's spelling and does not exist here
        tr = snap.get("tracked")
        if not tr:
            return
        # A missing file means a fresh run: re-seed from the current state rather
        # than waiting for the next transition. Without this, deleting the file
        # while the server is up silently drops every row until something moves,
        # and the baseline the waveform starts from is lost.
        if not os.path.exists(self.timeline):
            self._tl_last = None
        key = (tr.get("state"), tr.get("resched_node") or tr.get("node"),
               tr.get("stream_ok"))
        if key == self._tl_last:
            return
        self._tl_last = key
        # Prefer the stage machine's server-derived time over the poll time, so
        # the waveform's edges land where the cluster says they did.
        st = self.stages.get(tr.get("app"), {}) if self.t0 else {}
        off = {"scheduled": st.get("sched"), "running_again": st.get("ready")}.get(key[0])
        t_ev = (self.t0 + off) if off is not None else t
        new = not os.path.exists(self.timeline)
        with open(self.timeline, "a") as f:
            if new:
                f.write("t_ms,arm,workload,state,node,stream_ok\n")
            f.write(f"{t_ev},{self.arm},{tr.get('app','')},{key[0]},{key[1]},"
                    f"{'' if key[2] is None else key[2]}\n")

    def _stream_ok(self, app, node):
        """Does `node` still publish a TX stream in the domain `app` needs?

        None when the workload has no declared domain or is not placed. A pod
        can be Running on a TSN-labelled node with no path at all — that silent
        failure is what the label-based arm produces, and this is the only way
        to see it (kind runs -dataplane=false, so there is no tagged frame or
        bpf map to inspect).
        """
        dom = self.domains.get(app)
        if not dom or not node:
            return None
        return 1 if dom in self._tx_domains(node) else 0

    def _merge_group(self, cur, info):
        """pod0 keeps priority while a replacement is racing it: prefer the
        pod that is Running; among equals keep the newer sighting"""
        if cur is None:
            return info
        if info["phase"] == "Running" and cur["phase"] != "Running":
            return info
        if cur["phase"] == "Running" and info["phase"] != "Running":
            return cur
        return info

    def _mbps(self, pods):
        return sum(BW_MBPS.get(t, 1) * v["count"] for t, v in pods.items())

    def _heal_prepare_wedge(self, t):
        """A long-lived kind kubelet wedges DRA claim prepare: the rescheduled
        vPLC binds (sched fires) but sticks in ContainerCreating with no error
        (ADR-0037 후기). run.sh setup probes for this before a run; during a
        live episode the same remedy — restart that node's kubelet — is
        applied automatically once when ready lags sched by >35 s."""
        if self.wedge_healed:
            return
        vplc_app = next((x for x in self.fault_vplcs if x.startswith("vplc")),
                        None)
        st = self.stages.get(vplc_app or "", {})
        if "sched" not in st or "ready" in st:
            return
        if (t - self.t0) - st["sched"] < 35_000:
            return
        node = st.get("node1")
        if not node:
            return
        self.wedge_healed = True
        try:
            subprocess.run(["docker", "exec", container_of(node), "systemctl",
                            "restart", "kubelet"],
                           capture_output=True, text=True, timeout=30)
            self._event(t - self.t0, "wedge-heal",
                        f"claim prepare stuck >35s on {node} — kubelet restarted")
        except Exception as e:
            self._event(t - self.t0, "wedge-heal-failed", str(e))

    # -- fault stage machine (incremental port of watch_fault.cmd_watch) -----

    def _freeze_initial(self, pods):
        for p in pods:
            uid = p["metadata"]["uid"]
            labels = p["metadata"].get("labels", {})
            self.initial[uid] = {
                "name": p["metadata"]["name"],
                "app": labels.get("app", "?"),
                "tier": labels.get("tier", "?"),
                "node": p["spec"].get("nodeName", ""),
                "mcpu": wf.pod_request_mcpu(p)}
            if labels.get("mixed-group") == "guaranteed" and \
                    p["spec"].get("nodeName") == self.node0:
                self.fault_vplcs[labels["app"]] = p["metadata"]["name"]
        self.stages = {app: {} for app in self.fault_vplcs}

    def _classify_deletion(self, uid, t):
        """watch_fault.classify_deletion :179-193"""
        info = self.initial[uid]
        app = info["app"]
        rel = t - self.t0
        if app in self.fault_vplcs and info["name"] == self.fault_vplcs[app]:
            if "evict" not in self.stages[app]:
                self.stages[app]["evict"] = rel
                self._event(rel, "evict", f"{info['name']} evicted from {self.node0}")
        elif info["node"] != self.node0 and uid not in self.victims:
            self.victims[uid] = {"name": info["name"], "app": app,
                                 "tier": info["tier"], "node": info["node"],
                                 "mcpu": info["mcpu"], "evict_ms": rel}
            self.victim_apps.setdefault(app, uid)
            self._event(rel, "preempt",
                        f"{info['name']} ({info['tier']}) preempted on {info['node']}")

    def _classify(self, pods, t):
        """watch_fault.cmd_watch loop body :196-233"""
        rel = t - self.t0
        if self.detect_ms is None and not self._node_ready(self.node0):
            # Stage times come from the API objects, not from when this poll
            # happened to see them. The poll cannot keep up once a namespace
            # holds ~60 pods, and the lag inflated sched->ready to 4-6 s where
            # the pod's own conditions said 1-2 s. Same fix as watch_fault.
            n = self._kget("node", self.node0)
            self.detect_ms = wf.server_or(
                rel, wf.cond_ms(n, "Ready") if n else None, self.t0)
            self._event(self.detect_ms, "detect", f"{self.node0} NotReady")

        present = set()
        for p in pods:
            uid = p["metadata"]["uid"]
            present.add(uid)
            meta, spec, status = p["metadata"], p["spec"], p["status"]
            app = meta.get("labels", {}).get("app", "?")
            deleted = bool(meta.get("deletionTimestamp"))
            if uid in self.initial:
                if deleted:
                    self._classify_deletion(uid, t)
                continue
            if app in self.fault_vplcs and not deleted:
                s = self.stages[app]
                if spec.get("nodeName") and "sched" not in s:
                    s["sched"] = wf.server_or(rel, wf.cond_ms(p, "PodScheduled"), self.t0)
                    s["node1"] = spec["nodeName"]
                    self._event(s["sched"], "sched", f"{app} -> {spec['nodeName']}")
                # Trigger on Ready, not on phase == Running: Running precedes
                # Ready, and reading the Ready condition while it is still False
                # yields an earlier transition's timestamp.
                if wf.cond_true(p, "Ready") and "ready" not in s:
                    s.setdefault("sched", wf.server_or(rel, wf.cond_ms(p, "PodScheduled"), self.t0))
                    s.setdefault("node1", spec.get("nodeName", "?"))
                    s["ready"] = wf.server_or(rel, wf.cond_ms(p, "Ready"), self.t0)
                    self._event(s["ready"], "ready", f"{app} Ready on {s['node1']}")
            elif app in self.victim_apps and not deleted:
                v = self.victims[self.victim_apps[app]]
                if status.get("phase") == "Running" and "resched_ms" not in v:
                    v["resched_ms"] = rel
                    v["node1"] = spec.get("nodeName", "?")
                    self._event(rel, "victim-resched",
                                f"{v['name']} replacement on {v['node1']}")
        for uid in set(self.initial) - present - self.gone:
            self._classify_deletion(uid, t)
            self.gone.add(uid)

    # -- snapshot assembly ---------------------------------------------------

    def _assemble(self, node_list, util, tracked, peer, pods, t):
        # ADR-0039 §4 keeps ai-inference off the fault node, so exactly one
        # guaranteed workload is ever displaced and fault_vplcs holds one entry.
        # The peer plumbing below is inert here and kept only so the snapshot
        # shape stays identical to the one the Isaac extension already consumes.
        vplc_app = next((x for x in self.fault_vplcs if x.startswith("vplc")),
                        "vplc-line")
        st = self.stages.get(vplc_app, {})
        stages = None
        if self.t0 is not None:
            stages = {"detect_ms": self.detect_ms,
                      "evict_ms": st.get("evict"),
                      "sched_ms": st.get("sched"),
                      "ready_ms": st.get("ready")}

        tr = None
        if tracked:
            state = "running"
            resched = None
            if self.t0 is not None:
                pod0 = self.fault_vplcs.get(tracked.get("app", "vplc-line"))
                if "ready" in st:
                    state, resched = "running_again", st.get("node1")
                elif "sched" in st:
                    state, resched = "scheduled", st.get("node1")
                elif tracked["name"] == pod0 or tracked["node"] == self.node0:
                    state = "down"
            if tracked["phase"] == "Pending" and state == "running":
                state = "down"      # e.g. a scaled/recreated vPLC with no node
            tr = {"app": tracked.get("app", "vplc-line"),
                  "node": tracked["node"], "resched_node": resched,
                  "on_tsn": any(n["name"] == (resched or tracked["node"])
                                and n["tsn"] for n in node_list),
                  "tagged": self.arm == "dratsn", "state": state,
                  "stream_ok": self._stream_ok(tracked.get("app"),
                                               resched or tracked["node"]),
                  "mcpu": tracked["mcpu"]}
        elif self.t0 is not None and self.fault_vplcs:
            tr = {"app": vplc_app, "node": self.node0, "resched_node": None,
                  "on_tsn": True, "tagged": self.arm == "dratsn",
                  "state": "down", "stream_ok": None, "mcpu": 2000}

        pr = None
        if peer:
            papp = peer.get("name", "").rsplit("-", 2)[0] or "peer"
            pst = self.stages.get(next((x for x in self.fault_vplcs
                                        if x != vplc_app), ""), {})
            state = "running"
            resched = None
            if self.t0 is not None and peer["node"] == self.node0:
                state = "down"
            if "ready" in pst:
                state, resched = "running_again", pst.get("node1")
            elif "sched" in pst:
                state, resched = "scheduled", pst.get("node1")
            pr = {"vplc": papp, "tier": peer.get("tier", "?"),
                  "mcpu": peer["mcpu"], "node": peer["node"],
                  "resched_node": resched, "state": state}

        evs = []
        for v in self.victims.values():
            evs.append({"pod": v["name"], "priority_class": v["tier"],
                        "node": v["node"], "mcpu": v["mcpu"],
                        "evict_ms": v["evict_ms"],
                        "ai_resched_ms": v.get("resched_ms"),
                        "resched_node": v.get("node1")})

        if not pods:
            phase = "idle"
        elif self.t0 is None:
            phase = "filled"
        elif stages and stages["ready_ms"] is not None:
            phase = "recovered"
        else:
            phase = "fault"

        return {
            "v": 2, "ts_ms": t, "arm": self.arm, "phase": phase,
            "t0_ms": self.t0,
            "cluster": {"nodes": node_list, "util_pct": util,
                        "fault_node": self.node0,
                        # which stream domain the tracked line rides: the scene
                        # lights a node's path only if it publishes this one
                        "tracked_domain": self.domains.get(vplc_app),
                        "net": {"capacity_mbps": NET_CAPACITY_MBPS}},
            "tracked": tr, "peer": pr, "stages": stages,
            "evictions": evs, "events": self.events,
        }

    # -- thread --------------------------------------------------------------

    def run(self):
        while not self.stop_evt.is_set():
            t_start = time.monotonic()
            try:
                snap = self.poll_once()
            except Exception as e:      # kubectl hiccup: keep last snapshot
                print(f"[{self.arm}] poll error: {e}", flush=True)
                snap = None
            if snap is not None:
                with self.lock:
                    self.seq += 1
                    snap["seq"] = self.seq
                    self.snapshot = snap
            delay = self.interval - (time.monotonic() - t_start)
            if delay > 0:
                self.stop_evt.wait(delay)

    def latest(self):
        with self.lock:
            return self.snapshot, self.seq


class PlcState:
    """The physical-PLC track: declared, no hardware (ADR-0031). fail starts
    the declared repair countdown (compressed scale — document alongside the
    thesis's declared repair time); repair ends it immediately."""

    def __init__(self, repair_s):
        self.repair_s = repair_s
        self.lock = threading.Lock()
        self.state = "running"
        self.since_ms = wf.now_ms()
        self.version = 0
        self.events = []

    def _event(self, kind, msg):
        self.events.append({"t_ms": wf.now_ms(), "kind": kind, "msg": msg})
        self.events = self.events[-20:]
        print(f"[plc] {kind}: {msg}", flush=True)

    def _auto(self):
        if self.state == "failed" and \
                wf.now_ms() - self.since_ms >= self.repair_s * 1000:
            self.state = "running"
            self.since_ms = wf.now_ms()
            self.version += 1
            self._event("repaired", f"declared repair time ({self.repair_s}s) elapsed")

    def fail(self):
        with self.lock:
            self._auto()
            if self.state == "failed":
                return 409, {"error": "PLC already failed"}
            self.state = "failed"
            self.since_ms = wf.now_ms()
            self.version += 1
            self._event("failed", f"PLC failed — repair in {self.repair_s}s")
            return 200, {"ok": True, "state": self.state,
                         "repair_s": self.repair_s}

    def repair(self):
        with self.lock:
            self._auto()
            if self.state == "running":
                return 409, {"error": "PLC is running"}
            self.state = "running"
            self.since_ms = wf.now_ms()
            self.version += 1
            self._event("repaired", "manual repair")
            return 200, {"ok": True, "state": self.state}

    def snapshot(self):
        with self.lock:
            self._auto()
            eta = None
            if self.state == "failed":
                eta = max(0, self.repair_s * 1000
                          - (wf.now_ms() - self.since_ms))
            return {"state": self.state, "since_ms": self.since_ms,
                    "eta_ms": eta, "repair_s": self.repair_s,
                    "version": self.version, "events": self.events}


# --- control actions --------------------------------------------------------

def inject_fault(poller, node, force):
    """Interactive fault injection: docker-stop a worker and arm the stage
    machine through the same state-env handoff run.sh inject-fault uses —
    baked record and live demo stay one code path (ADR-0037)."""
    snap, _ = poller.latest()
    if snap is None or not snap["cluster"]["nodes"]:
        return 503, {"error": "no cluster snapshot yet"}
    nodes = {n["name"]: n for n in snap["cluster"]["nodes"]}
    tracked = snap.get("tracked") or {}
    if not node:
        node = tracked.get("node")
        if not node:
            return 400, {"error": "no vPLC found and no node given"}
    if node not in nodes:
        return 400, {"error": f"'{node}' is not a worker of cluster "
                              f"'{poller.arm}'", "workers": sorted(nodes)}
    if not nodes[node]["ready"]:
        return 409, {"error": f"{node} is already down"}
    if snap.get("phase") == "fault" and not force:
        return 409, {"error": "a fault is already in flight (force=true to override)"}

    notes = []
    if tracked.get("node") != node:
        notes.append(f"no vPLC on {node} — expect filler churn only, "
                     "no failover story")
    ready_tsn = [n for n in nodes.values() if n["tsn"] and n["ready"]]
    if nodes[node]["tsn"]:
        left = len(ready_tsn) - 1        # TSN nodes surviving this kill
        if left == 0:
            notes.append("no ready TSN node will remain — the vPLC WILL stay "
                         "Pending until a restore (honest failure mode)")
        elif left == 1:
            notes.append("one ready TSN node will remain — recovery relies "
                         "on preemption there")

    t0 = wf.now_ms()
    os.makedirs(os.path.dirname(poller.state) or ".", exist_ok=True)
    with open(poller.state, "w") as f:
        f.write(f"NODE0={node}\nT0={t0}\n")
    try:
        subprocess.run(["docker", "stop", container_of(node)], check=True,
                       capture_output=True, text=True, timeout=60)
    except Exception as e:
        return 500, {"error": f"docker stop failed: {e}"}
    return 200, {"ok": True, "cluster": poller.arm, "node": node,
                 "t0_ms": t0, "notes": notes}


def restore_node(poller, node):
    if node == "all":
        # revive every downed worker in one call. docker state, not API
        # readiness, is the truth here: a node killed seconds ago still shows
        # Ready for the whole detection window and would be skipped
        snap, _ = poller.latest()
        if snap is None:
            return 503, {"error": "no cluster snapshot yet"}
        dead = []
        for n in snap["cluster"]["nodes"]:
            c = container_of(n["name"])
            r = subprocess.run(["docker", "inspect", "-f",
                                "{{.State.Running}}", c],
                               capture_output=True, text=True, timeout=10)
            if r.returncode == 0 and r.stdout.strip() != "true":
                dead.append(n["name"])
        if not dead:
            return 200, {"ok": True, "cluster": poller.arm,
                         "notes": ["no downed nodes"]}
        results = [restore_node(poller, n)[1] for n in dead]
        return 200, {"ok": True, "cluster": poller.arm, "restored": dead,
                     "results": results}
    if not node:
        node, _t0, _m = read_state_env(poller.state)
    if not node:
        return 400, {"error": "no node given and no state-env NODE0"}
    try:
        subprocess.run(["docker", "start", container_of(node)], check=True,
                       capture_output=True, text=True, timeout=60)
    except Exception as e:
        return 500, {"error": f"docker start failed: {e}"}
    # restoring the fault node ends the episode: disarm the stage machine so
    # the next inject is not blocked by a fault that can never reach "ready"
    if node == poller.node0:
        poller.pending_clear = True
    notes = ["node restarting; Ready flips in the snapshot when kubelet is back"]
    mac = poller.macs.get(node)
    if mac:
        # pin the eth0 MAC back to the healthy baseline: the controller's
        # CNC × TSNNic join re-forms by itself and the TX TSNStreams come
        # back without touching the CNC (kind MAC-regeneration artifact)
        err = None
        for _ in range(10):
            r = subprocess.run(["docker", "exec", container_of(node), "ip", "link", "set",
                                "dev", "eth0", "address", mac],
                               capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                err = None
                break
            err = r.stderr.strip()
            time.sleep(1)
        notes.append(f"eth0 MAC pinned back to {mac}" if err is None
                     else f"MAC pin FAILED ({err}) — run: run.sh repair {node}")
    else:
        notes.append(f"no MAC baseline for {node} — if TX devices stay gone, "
                     f"run: run.sh repair {node}")
    return 200, {"ok": True, "cluster": poller.arm, "node": node,
                 "notes": notes}


# --- HTTP -------------------------------------------------------------------

def make_handler(pollers, plc, lam_ms):
    def combined():
        arms = {}
        seq = 0
        for arm, p in pollers.items():
            snap, s = p.latest()
            if snap:
                arms[arm] = snap
            seq += s
        pl = plc.snapshot()
        return {"v": 3, "ts_ms": wf.now_ms(), "lam_ms": lam_ms,
                "seq": seq + pl["version"], "arms": arms, "plc": pl}

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, code, obj):
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/state":
                self._reply(200, combined())
            elif self.path == "/healthz":
                self._reply(200, {"ok": True,
                                  "seq": combined()["seq"],
                                  "arms": sorted(pollers)})
            else:
                self.send_error(404)

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                self._reply(400, {"error": "bad JSON"})
                return
            if self.path == "/plc/fail":
                code, obj = plc.fail()
            elif self.path == "/plc/repair":
                code, obj = plc.repair()
            elif self.path in ("/inject", "/restore"):
                cluster = body.get("cluster")
                if cluster not in pollers:
                    self._reply(400, {"error": "cluster required",
                                      "clusters": sorted(pollers)})
                    return
                p = pollers[cluster]
                if self.path == "/inject":
                    code, obj = inject_fault(p, body.get("node"),
                                             bool(body.get("force")))
                else:
                    code, obj = restore_node(p, body.get("node"))
            else:
                self.send_error(404)
                return
            print(f"[control] {self.path} -> {code} {obj}", flush=True)
            self._reply(code, obj)

        def log_message(self, *args):    # quiet: one line per poll is enough
            pass
    return Handler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cluster", action="append", required=True,
                    metavar="ARM=KUBECONFIG",
                    help="arm and its kubeconfig, repeatable "
                         "(dratsn=~/.kube/kind-dratsn.kubeconfig)")
    ap.add_argument("--ns", default=os.environ.get("NS", "dratsn-test"))
    ap.add_argument("--state-dir", default="results/live",
                    help="per-arm state-<arm>.env handoff files live here "
                         "(run.sh inject-fault writes state.env — pass that "
                         "run's dir to share the handoff)")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--interval", type=float, default=0.25)
    ap.add_argument("--lam-ms", type=float, default=0.7,
                    help="declared BE delay median (gauge + live defect draws, "
                         "ADR-0036)")
    ap.add_argument("--domains", default="vplc-line=cell-A",
                    help="app=domain pairs, comma separated — which stream "
                         "domain each tracked workload needs. Drives the live "
                         "correct/incorrect verdict the scene paints.")
    ap.add_argument("--timeline", default="",
                    help="append state transitions to this CSV. It is the only "
                         "input to the production-waveform figure, so the "
                         "figure and the scene share one source.")
    ap.add_argument("--plc-repair-s", type=float, default=90.0,
                    help="declared physical-PLC repair time, compressed scale")
    a = ap.parse_args()

    pollers = {}
    for spec in a.cluster:
        arm, _, kubeconfig = spec.partition("=")
        if not arm or not kubeconfig:
            raise SystemExit(f"bad --cluster '{spec}' (want arm=/path/to/kubeconfig)")
        state = os.path.join(a.state_dir, f"state-{arm}.env")
        domains = dict(kv.split("=", 1) for kv in a.domains.split(",") if kv)
        tl = os.path.join(a.state_dir, f"timeline-{arm}.csv") if a.timeline == "auto" \
            else (a.timeline or None)
        pollers[arm] = ClusterPoller(arm, kubeconfig, state, a.ns, a.interval,
                                     domains=domains, timeline=tl)
    plc = PlcState(a.plc_repair_s)

    for p in pollers.values():
        p.start()
    srv = ThreadingHTTPServer((a.bind, a.port),
                              make_handler(pollers, plc, a.lam_ms))
    print(f"live_serve: arms={sorted(pollers)} plc-repair={a.plc_repair_s}s "
          f"on http://{a.bind}:{a.port}/state", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for p in pollers.values():
            p.stop_evt.set()


if __name__ == "__main__":
    main()
