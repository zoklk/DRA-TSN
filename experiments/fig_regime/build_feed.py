#!/usr/bin/env python3
"""regime.csv -> scenario_feed.json for the Isaac Sim twin (feed v2).

Successor of fig_twin/build_feed.py (kept untouched as the 12w/cordon record,
ADR-0037): the measured source is now the tight-regime failover collection in
this directory. Only the placement side is measured; everything else in the
feed is either a track constant or a declared parameter:

  measured   which node each track's vPLC runs on before and after the node
             death, the staged recovery times (regime.csv), and the eviction
             domino (evictions.csv) — all collected on kind
  constant   tagging. vanilla has no DRATSN so its frames are never tagged;
             dratsn always tags (fig_tagging: 200/200 on the wire)
  declared   the physical PLC track: fixed node, replacement time from the
             literature lower bound. Wire-delay distributions are declared in
             sim_model, not here (ADR-0036)

The spare arm is collected for f16 (utilisation x availability) but is NOT a
twin track: the scene contrasts control planes (physical / vanilla K8s /
DRATSN), not cluster sizing policies, and the stage has exactly three cells
(ADR-0033).

Feed v2 event schema — shared verbatim by the baked path (this file) and the
planned live watcher (ADR-0037: one schema, two sources). v1 consumers keep
working: sim_model.load_feed reads only the v1 fields and ignores the rest.

  {
    "scenario": "fault",
    "compression": 60,
    "source": {...provenance...},
    "cluster": {                       # v2: topology for the overlay layer
      "nodes": [{
        "name": str, "tsn": bool,
        "n": {                         # v2.1, TSN nodes only: the slack story.
          "max_latency_ns": int,       # 802.1Qcc budget the CNC reserves (1 ms)
          "accumulated_ns": int        # declared per-node path cost (deploy ACC)
        }
      }],
      "util_pct": {"tsn": int, "general": int},
      "fault_node": str,
      "net": {                         # v2.1: per-node request ledger, per arm
        "capacity_mbps": int,          # (arms are separate collections, so the
        "by_arm": {                    #  post-fault sums differ per arm)
          "dratsn|vanilla": {"<node>": {
            "pre_mbps": int, "post_mbps": int,
            "pre_pods":  {"<tier>": {"count": int, "mcpu": int}},  # overlay's
            "post_pods": {"<tier>": {"count": int, "mcpu": int}}   # C-block mix
          }}
        }
      },
      "net_model": {...BW_MBPS + capacity + provenance...}   # v2.1
    },
    "cells": [{
      "track": "physical|vanilla|dratsn",
      "start":  {"node": str, "on_tsn": bool, "tagged": bool},
      "disruptions": [{
        "t_ms": float, "down_ms": float,      # v1: sim_model verdict input
        "resume": {"node": str, "on_tsn": bool, "tagged": bool},
        "label": str,
        "stages": {                           # v2: cumulative ms from node death
          "detect_ms": float, "evict_ms": float,
          "sched_ms": float, "ready_ms": float
        },
        "evictions": [{                       # v2: the preemption domino
          "pod": str, "priority_class": str, "node": str,
          "ai_resched_ms": float,
          "resched_node": str                 # v2.1: where the replacement landed
        }],
        "peer": {                             # v2.1/2.2: the fault node's
          "vplc": str,                        # co-resident (fixture v2: the
          "start": {...placement...},         # camfeed gateway) — the overlay's
          "down_ms": float,                   # grey OtherLoad actor
          "resume": {...placement...},
          "tier": str, "mcpu": int            # v2.2: display size/colour hints
        }
      }]
    }]
  }

The v2.1 ledger values are declared, not measured: `accumulated_ns` mirrors the
per-node CNC path cost pushed at deploy time (kind/deploy.sh ACC — the same
constant-mirror pattern run.sh uses for SFXS/ACCS), and `net.used_*` multiplies
the *measured pod placement* of the representative trial (raw/pods-*.json) by
the declared per-tier bandwidth table BW_MBPS below. No traffic is measured —
the ledger is the scheduler's-eye request view, the same axis util_pct uses for
CPU. pre reconstructs each pod's home: fillers embed it in their app name
(ai-<node>, hist-<node>), vPLCs take node0 from regime.csv, everything else
stays put; only phase==Running pods count (Failed rows are preemption victims).

Usage: build_feed.py results/<RUN> [--fault-cycle 40] [--repair-ms 3600000]
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from pathlib import Path

CYCLE_REAL_MS = 60_000.0  # one index cycle in real time (sim_model.CYCLE_REAL_MS)

# Declared ledgers (constant mirrors of the deploy-time config, not measurements).
# ACC_NS mirrors kind/deploy.sh ACC / run.sh ACCS, indexed by position in the
# topology's TSN node list; MAX_LATENCY_NS is the Qcc budget every stream gets.
MAX_LATENCY_NS = 1_000_000
ACC_NS = [400_000, 100_000, 250_000, 400_000, 400_000, 400_000]
# Per-tier egress bandwidth (Mbps): what one pod of that tier is declared to
# push. Order-of-magnitude choices — a 500 Hz control loop is a few Mbps, an
# inference feed is a video-ish stream, a historian is bulk transfer.
BW_MBPS = {"vplc-emergency": 5, "vplc-line": 5, "ot-monitoring": 150,
           "it-ai": 150, "it-historian": 60}
NET_CAPACITY_MBPS = 1000


def load(path: Path) -> dict[str, list[dict]]:
    rows: dict[str, list[dict]] = {}
    with path.open() as fh:
        for row in csv.DictReader(fh):
            rows.setdefault(row["arm"], []).append(row)
    return rows


def representative(trials: list[dict], prefer_transition: bool = False) -> dict:
    """The typical row: the arm's usual placement outcome, at median latency.

    Same selection as fig_twin (modal on_tsn1 group, then closest to the median
    resched_ms inside it) — one replayed run must be typical in the dimension
    that matters, and a pure latency median could pick a minority placement.
    Every (trial, vPLC) row is a sample; the two vPLCs of a trial share the
    detect stage but land independently.
    """
    # fixture v2 rows include the camfeed gateway; representatives are vPLCs
    usable = [t for t in trials
              if t["pending"] == "0" and t["vplc"].startswith("vplc")]
    if not usable:
        raise SystemExit("no row produced a reschedule; cannot build a feed")
    group = None
    if prefer_transition:
        lost = [t for t in usable if t["on_tsn0"] == "1" and t["on_tsn1"] != "1"]
        gained = [t for t in usable if t["on_tsn0"] != "1" and t["on_tsn1"] == "1"]
        group = lost or gained or None
    if group is None:
        on = [t for t in usable if t["on_tsn1"] == "1"]
        off = [t for t in usable if t["on_tsn1"] != "1"]
        group = on if len(on) >= len(off) else off
    med = statistics.median(float(t["resched_ms"]) for t in group)
    return min(group, key=lambda t: abs(float(t["resched_ms"]) - med))


def placement(node: str, on_tsn: bool, tagged: bool) -> dict:
    return {"node": node, "on_tsn": bool(on_tsn), "tagged": bool(tagged)}


def stages(row: dict) -> dict:
    return {k: float(row[c]) for k, c in (
        ("detect_ms", "detect_ms"), ("evict_ms", "evict_ms"),
        ("sched_ms", "sched_ms"), ("ready_ms", "ready_ms"))}


def evictions_for(ev_rows: list[dict], trial: str) -> list[dict]:
    return [{
        "pod": r["pod"], "priority_class": r["priority_class"], "node": r["node"],
        "ai_resched_ms": float(r["ai_resched_ms"]),
    } for r in ev_rows if r["trial"] == trial]


def peer_for(arm_rows: list[dict], chosen: dict) -> dict | None:
    """The fault node's co-resident in the chosen trial (fixture v2: the
    camera-feed gateway; fixture v1: the second vPLC)."""
    for r in arm_rows:
        if r["trial"] == chosen["trial"] and r["vplc"] != chosen["vplc"]:
            return r
    return None


def peer_meta(run_dir: Path, arm: str, trial: str, app: str) -> dict:
    """tier + cpu request of the peer's Running pod, from the trial dump."""
    path = run_dir / "raw" / f"pods-{arm}-{trial}.json"
    if not path.exists():
        return {}
    for p in json.loads(path.read_text())["items"]:
        labels = p["metadata"].get("labels", {})
        if labels.get("app") == app and p["status"].get("phase") == "Running":
            cpu = str(p["spec"]["containers"][0]["resources"]
                      .get("requests", {}).get("cpu", "0"))
            mcpu = int(cpu[:-1]) if cpu.endswith("m") else int(float(cpu) * 1000)
            return {"tier": labels.get("tier", ""), "mcpu": mcpu}
    return {}


def net_ledger(run_dir: Path, arm: str, trial: str, fault_node: str,
               vplc_home: dict[str, str]) -> dict[str, dict]:
    """Per-node declared-bandwidth sums before/after the fault.

    Placement is measured (the trial's raw pod dump), the per-pod bandwidth is
    the declared BW_MBPS ledger — the same requests-not-usage axis util_pct
    uses for CPU. post counts Running pods off the dead node; pre maps every
    Running pod back to its home: fillers embed it in the app name
    (ai-<node>[-trim], hist-<node>[-trim]), vPLCs take regime.csv's node0,
    Failed rows (preemption victims, superseded vPLC instances) never count.
    """
    path = run_dir / "raw" / f"pods-{arm}-{trial}.json"
    if not path.exists():
        return {}
    pre: dict[str, int] = {}
    post: dict[str, int] = {}
    pre_pods: dict[str, dict[str, int]] = {}
    post_pods: dict[str, dict[str, int]] = {}

    def mcpu_of(p) -> int:
        total = 0
        for ct in p["spec"].get("containers", []):
            cpu = str(ct.get("resources", {}).get("requests", {}).get("cpu", "0"))
            total += int(cpu[:-1]) if cpu.endswith("m") else int(float(cpu) * 1000)
        return total

    def bump(mbps, pods, node, tier, bw, mcpu):
        mbps[node] = mbps.get(node, 0) + bw
        t = pods.setdefault(node, {}).setdefault(tier, {"count": 0, "mcpu": 0})
        t["count"] += 1
        t["mcpu"] += mcpu

    for p in json.loads(path.read_text())["items"]:
        if p["status"].get("phase") != "Running":
            continue
        labels = p["metadata"].get("labels", {})
        tier = labels.get("tier", "")
        bw = BW_MBPS.get(tier)
        node = p["spec"].get("nodeName")
        if bw is None or not node:
            continue
        app = labels.get("app", "")
        if labels.get("vplc-group") == "regime":
            home = vplc_home.get(app, node)
        else:
            base = app[: -len("-trim")] if app.endswith("-trim") else app
            home = base.split("-", 1)[1] if "-" in base else node
        mcpu = mcpu_of(p)
        bump(pre, pre_pods, home, tier, bw, mcpu)
        if node != fault_node:
            bump(post, post_pods, node, tier, bw, mcpu)
    return {n: {"pre_mbps": pre.get(n, 0), "post_mbps": post.get(n, 0),
                "pre_pods": pre_pods.get(n, {}), "post_pods": post_pods.get(n, {})}
            for n in sorted(set(pre) | set(post))}


def enrich_evictions(run_dir: Path, arm: str, trial: str, evs: list[dict]) -> None:
    """Attach where each victim's replacement re-landed (`resched_node`).

    evictions.csv records the eviction; the landing is read from the same trial
    dump — the Running pod of the victim's Deployment that is no longer on the
    eviction node.
    """
    path = run_dir / "raw" / f"pods-{arm}-{trial}.json"
    if not path.exists():
        return
    items = json.loads(path.read_text())["items"]
    for ev in evs:
        app = ev["pod"].rsplit("-", 2)[0]
        for p in items:
            if (p["metadata"].get("labels", {}).get("app") == app
                    and p["status"].get("phase") == "Running"
                    and p["spec"].get("nodeName")
                    and p["spec"]["nodeName"] != ev["node"]):
                ev["resched_node"] = p["spec"]["nodeName"]
                break


def parse_topology(path: Path) -> dict:
    kv = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                kv[k] = v.strip().strip('"')
    tsn = kv.get("EXP_TSN_NODES", "").split()
    gen = kv.get("EXP_GEN_NODES", "").split()
    return {
        "nodes": [{"name": n, "tsn": True} for n in tsn]
               + [{"name": n, "tsn": False} for n in gen],
        "util_pct": {"tsn": int(kv.get("EXP_UTIL_TSN", 0)),
                     "general": int(kv.get("EXP_UTIL_GEN", 0))},
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("--fault-cycle", type=int, default=40,
                    help="production cycle the fault lands on")
    ap.add_argument("--repair-ms", type=float, default=3_600_000.0,
                    help="physical PLC replacement time, declared not measured")
    ap.add_argument("--prefer-transition", action="store_true",
                    help="pick a row whose fault changes the path class, so the "
                         "marker moves on screen (demo reel; not representative)")
    args = ap.parse_args()

    run_dir = Path(args.run_dir)
    rows = load(run_dir / "regime.csv")
    for arm in ("vanilla", "dratsn"):
        if arm not in rows:
            raise SystemExit(f"regime.csv has no '{arm}' rows")
    ev_path = run_dir / "evictions.csv"
    ev = load(ev_path) if ev_path.exists() else {}

    fault_ms = args.fault_cycle * CYCLE_REAL_MS
    van = representative(rows["vanilla"], args.prefer_transition)
    dra = representative(rows["dratsn"], args.prefer_transition)

    cluster = parse_topology(run_dir / "topology.env")
    # the fault targets the node the vPLCs ran on; both arms stage it the same way
    cluster["fault_node"] = dra["node0"]

    # the slack story: declared per-node CNC path cost, by TSN list position
    tsn_idx = 0
    for nd in cluster["nodes"]:
        if nd["tsn"]:
            nd["n"] = {"max_latency_ns": MAX_LATENCY_NS,
                       "accumulated_ns": ACC_NS[tsn_idx]}
            tsn_idx += 1

    # the bandwidth ledger: declared per-tier rates x each arm's measured placement
    cluster["net"] = {
        "capacity_mbps": NET_CAPACITY_MBPS,
        "by_arm": {
            row["arm"]: net_ledger(
                run_dir, row["arm"], row["trial"], cluster["fault_node"],
                {r["vplc"]: r["node0"]
                 for r in rows[row["arm"]] if r["trial"] == row["trial"]})
            for row in (dra, van)
        },
    }
    cluster["net_model"] = {
        "bw_mbps": BW_MBPS, "capacity_mbps": NET_CAPACITY_MBPS,
        "node_alloc_mcpu": 12000,
        "note": "declared per-tier bandwidth x measured pod placement "
                "(raw/pods-*.json); a request ledger like util_pct, no traffic "
                "was measured or generated",
    }

    def cell(track: str, row: dict, tagged: bool, label: str) -> dict:
        peer = peer_for(rows[row["arm"]], row)
        d = {
            "t_ms": fault_ms,
            "down_ms": float(row["resched_ms"]),
            "resume": placement(row["node1"], row["on_tsn1"] == "1", tagged),
            "label": label,
            "stages": stages(row),
            "evictions": evictions_for(ev.get(row["arm"], []), row["trial"]),
        }
        enrich_evictions(run_dir, row["arm"], row["trial"], d["evictions"])
        if peer is not None:
            d["peer"] = {
                "vplc": peer["vplc"],
                "start": placement(peer["node0"], peer["on_tsn0"] == "1", tagged),
                "down_ms": float(peer["resched_ms"]),
                "resume": placement(peer["node1"], peer["on_tsn1"] == "1", tagged),
                **peer_meta(run_dir, row["arm"], row["trial"], peer["vplc"]),
            }
        return {
            "track": track,
            "start": placement(row["node0"], row["on_tsn0"] == "1", tagged),
            "disruptions": [d],
        }

    feed = {
        "scenario": "fault",
        "compression": 60,
        "source": {
            "measured": str((run_dir / "regime.csv").resolve()),
            "rows": {arm: len(v) for arm, v in rows.items()},
            "note": "placements, stages and evictions measured on kind (fig_regime); "
                    "physical track and repair time are declared; spare arm is "
                    "f16-only, not a twin track",
            "selection": "prefer-transition" if args.prefer_transition else "modal-outcome",
        },
        "cluster": cluster,
        "cells": [
            {
                "track": "physical",
                "start": placement("fixed-plc", True, True),
                "disruptions": [{
                    "t_ms": fault_ms,
                    "down_ms": args.repair_ms,
                    "resume": placement("fixed-plc", True, True),
                    "label": "PLC 교체 (문헌 기반 하한, 실측 아님)",
                }],
            },
            cell("vanilla", van, False, f"재배치 (trial {van['trial']}, {van['vplc']})"),
            cell("dratsn", dra, True, f"재배치+선점 (trial {dra['trial']}, {dra['vplc']})"),
        ],
    }

    out = run_dir / "scenario_feed.json"
    out.write_text(json.dumps(feed, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {out}")
    for c in feed["cells"]:
        d = c["disruptions"][0]
        n_ev = len(d.get("evictions", []))
        print(f"  {c['track']:9s} {c['start']['node']:16s} -> {d['resume']['node']:16s} "
              f"down {d['down_ms']:>9.0f} ms  on_tsn {c['start']['on_tsn']}->{d['resume']['on_tsn']}"
              f"  evictions {n_ev}")


if __name__ == "__main__":
    main()
