#!/usr/bin/env python3
"""Local smoke test for the live-mode mapping (no omni; pxr optional).

Run from a venv with `pip install usd-core`:

    STAGE=/path/to/factory_three_cells_v11.usda python3 test_overlay_map.py

Three parts (plan §검증 1):
  (a) constant parity — ast-parse build_factory_scene.py and assert the
      mirrored constants in overlay_map are identical (drift guard)
  (b) golden ops — a synthesized snapshot must produce ops whose paths all
      match the fixed naming contract (ADR-0033 §6), conserve blade counts,
      and land the tracked actor on the resume node
  (c) session masking — open the real v11 stage, apply the ops to the
      session layer, and assert the resolved value at timecode 0 equals the
      live value (session defaults must outrank baked time samples)
Part (c) is skipped unless STAGE points at the v11 root usda.
"""
import ast
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "exts", "dratsn.live", "dratsn", "live"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # sim_model
import floor_map as fm            # noqa: E402
import overlay_map as om          # noqa: E402

BUILDER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "build_factory_scene.py")

PARITY_NAMES = [
    "OVERLAY_SWITCH_X", "NODE_W", "NODE_D", "NODE_H", "TOWER_Y", "TOWER_Z0",
    "LEVEL_H", "RIB_LEN", "UNIT_MCPU", "UNIT_ROWS", "UNIT_COLS", "UNIT_W",
    "UNIT_D", "UNIT_H", "UNIT_PITCH", "UNIT_X_OFF", "SEG_LEN", "SEG_GAP",
    "VPLC_MCPU_DEFAULT", "BE_MAX_MS", "SHELF_DZ", "OVL_SW_Z", "RIBBON_W",
    "RIBBON_T", "LANE_W", "LANE_GAP", "NODE_ALLOC_MCPU", "ARRIVE_GLOW",
    "GLOW_FRAMES", "DEAD_WIRE", "RACK_X",
    # v12 (ADR-0040)
    "SCENE_VER", "GPU_NODE_SUFFIX",
]

PATH_RE = re.compile(
    r"^/World/(ClusterOverlay/(DRATSN|VanillaK8s)/"
    r"(Nodes/[A-Za-z0-9_]+(/Platform|/Cores/Core_\d|/Gpu/Box|/Ribbon/"
    r"(Trunk|LaneProtected|LaneBE|BudgetLimit|Deadline|Stream_\d+|PathLit))"
    r"|Actors/(Tracked|OtherLoad|Victim_\d+)|DelayGauge|Binding)"
    r"|Infrastructure/(DRATSN|VanillaK8s)/Rack_[AB]/LED_\d\d"
    r"|Looks/Ovl(DRATSN|VanillaK8s)_[A-Za-z0-9]+/Shader)$")

FAILED = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    print(f"[{tag}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


# --- (a) constant parity ----------------------------------------------------

def builder_constants():
    tree = ast.parse(open(BUILDER).read())
    consts = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        try:
            value = ast.literal_eval(node.value)
        except (ValueError, SyntaxError):
            continue
        for tgt in node.targets:
            if isinstance(tgt, ast.Name):
                consts[tgt.id] = value
            elif isinstance(tgt, ast.Tuple) and isinstance(value, tuple):
                for elt, v in zip(tgt.elts, value):
                    if isinstance(elt, ast.Name):
                        consts[elt.id] = v
    return consts


FLOOR_PARITY = ["STATION_PITCH", "STATIONS", "WORK_SLOT", "INSPECT_SLOT",
                "BELT_Y_IN", "BELT_Y_OUT", "BAR_COUNT", "BRIGHT",
                "DIM", "DEAD_WIRE", "CABLE_W_IDLE", "CABLE_W_LIVE",
                "GLOW_FRAMES", "CELL_OF_TRACK", "RACK_X"]


def test_parity():
    consts = builder_constants()
    for name in PARITY_NAMES:
        check(f"parity:{name}", name in consts, "not found in builder")
        if name in consts:
            check(f"parity:{name}=", consts[name] == getattr(om, name),
                  f"builder={consts[name]!r} mirror={getattr(om, name)!r}")
    for name in FLOOR_PARITY:
        check(f"parity:floor:{name}", name in consts, "not found in builder")
        if name in consts:
            check(f"parity:floor:{name}=", consts[name] == getattr(fm, name),
                  f"builder={consts[name]!r} mirror={getattr(fm, name)!r}")
    import sim_model
    check("parity:SIGMA", om.SIGMA == sim_model.SIGMA)
    check("parity:FPS", om.FPS == sim_model.FPS)
    check("parity:floor:BAR_SPAN", fm.BAR_SPAN == fm.BAR_COUNT * fm.STATION_PITCH)
    check("parity:floor:POOL", fm.POOL == len(fm.STATIONS))  # builder: len(STATIONS)


# --- (b) golden ops ---------------------------------------------------------

def snap_base():
    def node(name, tsn, ready=True, pods=None):
        n = {"name": name, "tsn": tsn, "ready": ready,
             "alloc_mcpu": 12000, "pods": pods or {}, "mbps": 0}
        if tsn:
            n["n"] = {"max_latency_ns": 1000000, "accumulated_ns": 400000}
        return n
    tsn_pods = {"vplc-line": {"count": 1, "mcpu": 2000},
                "ot-monitoring": {"count": 1, "mcpu": 500},
                "it-ai": {"count": 3, "mcpu": 6000},
                "it-historian": {"count": 2, "mcpu": 2000}}
    gen_pods = {"it-historian": {"count": 5, "mcpu": 9000}}
    return {
        "v": 2, "seq": 1, "arm": "dratsn", "phase": "filled", "t0_ms": None,
        "lam_ms": 0.5,
        "cluster": {
            "nodes": [node("dratsn-worker", True),
                      node("dratsn-worker2", True, pods=dict(tsn_pods)),
                      node("dratsn-worker3", True),
                      node("dratsn-worker4", False, pods=dict(gen_pods)),
                      node("dratsn-worker5", False),
                      node("dratsn-worker6", False)],
            "util_pct": {"tsn": 90.0, "general": 90.0},
            "fault_node": None, "net": {"capacity_mbps": 1000}},
        "tracked": {"app": "vplc-line", "node": "dratsn-worker2",
                    "resched_node": None, "on_tsn": True, "tagged": True,
                    "state": "running", "mcpu": 2000},
        "peer": {"vplc": "camfeed", "tier": "ot-monitoring", "mcpu": 500,
                 "node": "dratsn-worker2", "resched_node": None,
                 "state": "running"},
        "stages": None, "evictions": [], "events": [],
    }


def test_golden():
    anim = om.AnimState()
    snap = snap_base()
    ops, moves, pulses = om.compute_ops(snap, anim, first=True)
    check("golden:ops-nonempty", len(ops) > 50, f"got {len(ops)}")
    bad = [op for op in ops if not PATH_RE.match(op[1])]
    check("golden:path-contract", not bad, f"e.g. {bad[:3]}")

    # blade conservation on worker2: 10500 mcpu total, minus tracked 2000 and
    # peer 500 (they ride as actors) = 8000 -> round(8000/1500) = 5 blades
    w2 = om.node_prim("DRATSN", "dratsn-worker2")
    blades = {op[1]: op[2] for op in ops
              if op[0] == "bind" and op[1].startswith(f"{w2}/Cores/")}
    filled = sum(1 for m in blades.values() if not m.endswith("UnitEmpty"))
    check("golden:blade-conservation", filled == 5, f"filled={filled}")
    check("golden:blade-count", len(blades) == 8, f"{len(blades)}")

    # pre-fault: platform healthy, stream on the tracked node visible
    plat = [op for op in ops if op[0] == "bind" and op[1] == f"{w2}/Platform"]
    check("golden:platform-live", plat and plat[0][2] == "OvlDRATSN_PlatTSN",
          f"{plat}")
    stream = [op for op in ops if op[0] == "cube"
              and op[1] == f"{w2}/Ribbon/Stream_0"]
    check("golden:stream-visible", stream and stream[0][5] is True, f"{stream}")

    # fault snapshot: worker2 dead
    snap2 = snap_base()
    snap2["seq"] = 2
    snap2["cluster"]["fault_node"] = "dratsn-worker2"
    snap2["cluster"]["nodes"][1]["ready"] = False
    snap2["tracked"]["state"] = "down"
    snap2["peer"]["state"] = "down"
    snap2["stages"] = {"detect_ms": 44000.0, "evict_ms": None,
                       "sched_ms": None, "ready_ms": None}
    ops2, _m2, pulses2 = om.compute_ops(snap2, anim)
    plat2 = [op for op in ops2 if op[0] == "bind" and op[1] == f"{w2}/Platform"]
    check("golden:platform-dead", plat2 and plat2[0][2] == "OvlDRATSN_PlatDead",
          f"{plat2}")
    dead_diffuse = [op for op in ops2 if op[0] == "attr"
                    and op[1] == om.shader_path("DRATSN", "PlatDead")
                    and op[4] == om.DEAD_WIRE]
    check("golden:dead-desaturate", bool(dead_diffuse))
    check("golden:detect-pulse", len(pulses2) == 1
          and "PlatTSN" in pulses2[0][0], f"{pulses2}")
    tr_dead = [op for op in ops2 if op[0] == "attr"
               and op[1] == om.shader_path("DRATSN", "ActTracked")
               and op[4] == om.DEAD_WIRE]
    check("golden:tracked-stale", bool(tr_dead))

    # recovery snapshot: tracked lands on worker3, one AI victim to worker5
    snap3 = snap_base()
    snap3["seq"] = 3
    snap3["cluster"]["fault_node"] = "dratsn-worker2"
    snap3["cluster"]["nodes"][1]["ready"] = False
    snap3["tracked"].update({"state": "running_again",
                             "resched_node": "dratsn-worker3"})
    snap3["peer"].update({"state": "running_again",
                          "resched_node": "dratsn-worker6"})
    snap3["evictions"] = [{"pod": "ai-x", "priority_class": "it-ai",
                           "node": "dratsn-worker3", "mcpu": 2000,
                           "evict_ms": 46000.0, "ai_resched_ms": 51000.0,
                           "resched_node": "dratsn-worker5"}]
    snap3["stages"] = {"detect_ms": 44000.0, "evict_ms": 46000.0,
                       "sched_ms": 46600.0, "ready_ms": 47900.0}
    ops3, moves3, pulses3 = om.compute_ops(snap3, anim)
    tracked_moves = [m for m in moves3
                     if m[0].endswith("Actors/Tracked")]
    check("golden:tracked-move", len(tracked_moves) == 1, f"{moves3}")
    if tracked_moves:
        p1 = tracked_moves[0][2]
        # worker3 = TSN rack, level 2; the blade column sits UNIT_X_OFF left of
        # the cage centre since v12 put the GPU slots on its right
        check("golden:tracked-lands-tsn",
              abs(p1[0] - (om.RACK_X["DRATSN"][0] - om.UNIT_X_OFF)) < 1e-6,
              f"x={p1[0]}")
        base3 = om.TOWER_Z0 + 2 * om.LEVEL_H
        check("golden:tracked-level", base3 < p1[2] < base3 + om.NODE_H,
              f"z={p1[2]}")
    kinds = {p[0].rsplit("_", 1)[-1] for p in pulses3}
    check("golden:sched+arrive-pulses",
          any("LaneProt" in p[0] for p in pulses3)
          and any("ActTracked" in p[0] for p in pulses3), f"{kinds}")

    # gauge follows the resumed node
    check("golden:gauge-ctx", anim.gauge_ctx is not None
          and anim.gauge_ctx["pos"] == om.node_positions(
              "DRATSN", snap3["cluster"]["nodes"])["dratsn-worker3"])
    gauge_ops = anim.tick(1000.0)
    check("golden:gauge-tick", any("DelayGauge" in op[1] for op in gauge_ops),
          f"{gauge_ops[:2]}")

    # double fault: killing a second node moves fault_node, but the first
    # casualty must stay dead — dead display keys off per-node ready
    snap4 = snap_base()
    snap4["seq"] = 4
    snap4["cluster"]["fault_node"] = "dratsn-worker3"   # pointer moved to B
    snap4["cluster"]["nodes"][1]["ready"] = False       # A (worker2) still down
    snap4["cluster"]["nodes"][2]["ready"] = False       # B (worker3) down
    anim4 = om.AnimState()
    ops4, _m4, _p4 = om.compute_ops(snap4, anim4, first=True)
    w3 = om.node_prim("DRATSN", "dratsn-worker3")
    plat_a = [op for op in ops4 if op[0] == "bind" and op[1] == f"{w2}/Platform"]
    plat_b = [op for op in ops4 if op[0] == "bind" and op[1] == f"{w3}/Platform"]
    check("golden:double-fault-A-stays-dead",
          plat_a and plat_a[0][2] == "OvlDRATSN_PlatDead", f"{plat_a}")
    check("golden:double-fault-B-dead",
          plat_b and plat_b[0][2] == "OvlDRATSN_PlatDead", f"{plat_b}")
    leds = {op[1]: op[2] for op in ops4 if op[0] == "bind" and "/LED_" in op[1]}
    check("golden:led-red-on-down",
          leds.get("/World/Infrastructure/DRATSN/Rack_A/LED_01") == "StatusRed"
          and leds.get("/World/Infrastructure/DRATSN/Rack_A/LED_02") == "StatusRed"
          and leds.get("/World/Infrastructure/DRATSN/Rack_A/LED_00") == "RackLed_TSN_DRATSN"
          and leds.get("/World/Infrastructure/DRATSN/Rack_B/LED_00") == "RackLed_BE_DRATSN",
          f"{leds}")
    plat_scale = [op for op in ops4 if op[0] == "attr"
                  and op[1].endswith("/Platform") and op[2] == "xformOp:scale"]
    check("golden:platform-shrunk",
          plat_scale and abs(plat_scale[0][4][0] - (om.NODE_W - 0.06)) < 1e-9,
          f"{plat_scale[:1]}")

    # alias: a differently-named live cluster (vanilla-*) drives the baked
    # prims (dratsn_worker*) positionally — dual-cluster demo path
    snapv = snap_base()
    snapv["arm"] = "vanilla"
    for n in snapv["cluster"]["nodes"]:
        n["name"] = n["name"].replace("dratsn-", "vanilla-")
    snapv["tracked"]["node"] = "vanilla-worker2"
    snapv["peer"]["node"] = "vanilla-worker2"
    animv = om.AnimState()
    animv.alias = {n["name"]: n["name"].replace("vanilla-", "dratsn_").replace("-", "_")
                   for n in snapv["cluster"]["nodes"]}
    opsv, _mv, _pv = om.compute_ops(snapv, animv, first=True)
    node_paths = [op[1] for op in opsv if "/Nodes/" in op[1]]
    check("golden:alias-maps-to-baked",
          node_paths and all("/Nodes/dratsn_worker" in p for p in node_paths),
          f"e.g. {[p for p in node_paths if '/Nodes/dratsn_worker' not in p][:3]}")
    return ops, ops2, ops3


# --- (b2) floor golden ------------------------------------------------------

def drive(fs, f0, n_frames, running=True, on_tsn=True, tagged=True, step=0.8):
    """simulate Kit ticks on the timeline-frame clock; returns (ops, f_end)"""
    ops = []
    f = f0
    while f < f0 + n_frames:
        f += step
        ops += fs.tick(f % fm.LIVE_LOOP_FRAMES, running, on_tsn, tagged)
    return ops, f


def test_floor():
    # P0 (dratsn, tagged+TSN): products always Good, wires blue
    fs = fm.FloorState("dratsn", lam_ms=0.5, seed=1)
    ops, t = drive(fs, 0.0, 6 * fm.CYCLE_FRAMES)
    root = fm.cell_root("dratsn")
    check("floor:paths-in-cell",
          all(op[1].startswith(("/World/Cells", "/World/Looks",
                                "/World/Topology", "/World/Infrastructure"))
              for op in ops), "stray path")
    goods = [op for op in ops if op[0] == "vis"
             and op[1].endswith("/Good") and op[2] is True]
    bads = [op for op in ops if op[0] == "vis"
            and op[1].endswith("/Bad") and op[2] is True]
    check("floor:p0-all-good", len(goods) >= 3 and not bads,
          f"good={len(goods)} bad={len(bads)}")
    wire_blue = [op for op in ops if op[0] == "attr"
                 and "TSN_DRATSN" in op[1] and op[2] == "inputs:diffuseColor"]
    check("floor:p0-wire-blue", wire_blue
          and wire_blue[0][4] == fm.TSN_BASE, f"{wire_blue[:1]}")
    moves = [op for op in ops if op[0] == "attr"
             and op[2] == "xformOp:translate" and "/Products/" in op[1]]
    check("floor:items-move", len(moves) > 50, f"{len(moves)}")

    # freeze: down -> only the wire repaint, no pipeline ops afterwards
    ops_down, t = drive(fs, t, 2 * fm.CYCLE_FRAMES, running=False)
    non_wire = [op for op in ops_down
                if "/Looks/" not in op[1] and "/Topology/" not in op[1]
                and "PodAnimationAnchor" not in op[1]]
    check("floor:down-frozen", not non_wire, f"{non_wire[:3]}")
    wire_dead = [op for op in ops_down if op[0] == "attr"
                 and "TSN_DRATSN" in op[1] and op[2] == "inputs:diffuseColor"]
    check("floor:down-wire-dead", wire_dead
          and wire_dead[0][4] == fm.DEAD_WIRE)
    ops_up, t = drive(fs, t, 3 * fm.CYCLE_FRAMES)
    check("floor:resume", any("/Products/" in op[1] for op in ops_up))

    # P2 (vanilla, untagged): defects appear at a lam-consistent rate
    fs2 = fm.FloorState("vanilla", lam_ms=0.5, seed=7)
    ops2, _ = drive(fs2, 0.0, 60 * fm.CYCLE_FRAMES, on_tsn=False, tagged=False)
    bads2 = [op for op in ops2 if op[0] == "vis"
             and op[1].endswith("/Bad") and op[2] is True]
    goods2 = [op for op in ops2 if op[0] == "vis"
              and op[1].endswith("/Good") and op[2] is True]
    import sim_model
    exp = sim_model.defect_prob(sim_model.miss_rate(0.5),
                                sim_model.n_control_cycles(),
                                sim_model.RUN_THRESHOLD)
    rate = len(bads2) / max(len(bads2) + len(goods2), 1)
    check("floor:p2-defects", len(bads2) > 0 and abs(rate - exp) < 0.25,
          f"rate={rate:.2f} expected~{exp:.2f} (n={len(bads2)+len(goods2)})")
    wire2 = [op for op in ops2 if op[0] == "attr"
             and "BestEffort_VanillaK8s" in op[1]
             and op[2] == "inputs:diffuseColor"]
    check("floor:p2-be-wire", wire2 and wire2[0][4] == fm.BE_BASE)

    # physical: PLC fail freezes + cabinet goes red
    fs3 = fm.FloorState("physical", lam_ms=0.5, seed=3)
    ops3, t3 = drive(fs3, 0.0, 3 * fm.CYCLE_FRAMES)
    ops3f, _ = drive(fs3, t3, fm.CYCLE_FRAMES, running=False)
    plc_red = [op for op in ops3f if op[0] == "attr"
               and "Plc_PhysicalPLC" in op[1] and op[4] == fm.PLC_FAULT]
    check("floor:plc-red", bool(plc_red))

    # freeze targets cover the rhythm attrs (bars + 12 robot links)
    tgt = fm.freeze_targets("dratsn")
    check("floor:freeze-targets", len(tgt) == fm.BAR_COUNT + 12
          and all(a for _p, a in tgt), f"{len(tgt)}")
    return ops + ops3


# --- (c) session masking on the real stage ----------------------------------

def test_stage(all_ops):
    stage_path = os.environ.get("STAGE", "")
    if not stage_path or not os.path.exists(stage_path):
        print("[SKIP] stage test (set STAGE=/path/to/factory_three_cells_v11.usda)")
        return
    from pxr import Usd, UsdGeom, Gf  # noqa: F401
    import applier
    stage = Usd.Stage.Open(stage_path)
    check("stage:open", stage is not None)
    check("stage:overlay-present",
          bool(stage.GetPrimAtPath("/World/ClusterOverlay/DRATSN")))

    missing_total = []
    for ops in all_ops:
        missing_total += applier.apply(stage, ops)
    # playback-leak seal ops may target prims the baked trial never produced
    # (extra victims, arrival stream segments) — absent means nothing to seal
    seal_ok = re.compile(r"/(Actors/Victim_[1-9]\d*|Ribbon/Stream_[1-9]\d*)$")
    real_missing = [p for p in missing_total if not seal_ok.search(p)]
    check("stage:no-missing-prims", not real_missing,
          f"{sorted(set(real_missing))[:6]}")

    # the decisive assertion: session default beats baked time samples at t=0
    gauge = stage.GetPrimAtPath("/World/ClusterOverlay/DRATSN/DelayGauge")
    anim = om.AnimState()
    om.compute_ops_probe = None
    tr = stage.GetPrimAtPath("/World/ClusterOverlay/DRATSN/Actors/Tracked")
    if tr:
        applier.apply(stage, [("attr", str(tr.GetPath()),
                               "xformOp:translate", "double3",
                               (1.0, 2.0, 3.0))])
        val = tr.GetAttribute("xformOp:translate").Get(Usd.TimeCode(0))
        check("stage:session-masks-samples",
              val is not None and tuple(val) == (1.0, 2.0, 3.0), f"got {val}")
        # resolved GetNumTimeSamples reports 0 once the session default wins,
        # so ask the baked overlay layer directly for its (masked) samples
        overlay_layer = next(l for l in stage.GetLayerStack()
                             if "overlay" in l.identifier)
        baked = overlay_layer.ListTimeSamplesForPath(
            "/World/ClusterOverlay/DRATSN/Actors/Tracked.xformOp:translate")
        check("stage:baked-samples-still-there", len(baked) >= 2,
              f"samples={len(baked)} (expected baked animation underneath)")
    # binding curve points resolve to the session value at an arbitrary time
    binding = stage.GetPrimAtPath("/World/ClusterOverlay/DRATSN/Binding")
    if binding:
        pts = binding.GetAttribute("points").Get(Usd.TimeCode(500))
        check("stage:binding-resolved", pts is not None and len(pts) == 2)
    check("stage:gauge-exists", bool(gauge))
    # freeze/clear roundtrip on a rhythm attr: freeze pins the resolved value
    # against the timeline; clear returns ownership to the baked samples
    bar = stage.GetPrimAtPath(
        "/World/Cells/Cell_DRATSN/Conveyor/FlightBar_0")
    if bar:
        attr = bar.GetAttribute("xformOp:translate")
        baked_at_500 = attr.Get(Usd.TimeCode(500))
        applier.apply(stage, [("freeze", str(bar.GetPath()),
                               ["xformOp:translate"], 24.0)])
        frozen = attr.Get(Usd.TimeCode(500))
        pinned = attr.Get(Usd.TimeCode(24.0))
        check("stage:freeze-pins", frozen == pinned,
              f"frozen={frozen} pinned={pinned}")
        applier.apply(stage, [("clear", str(bar.GetPath()),
                               ["xformOp:translate"])])
        back = attr.Get(Usd.TimeCode(500))
        check("stage:clear-restores", back == baked_at_500,
              f"back={back} baked={baked_at_500}")
    # nothing was written to the layers on disk
    for layer in stage.GetLayerStack(includeSessionLayers=False):
        check(f"stage:clean:{os.path.basename(layer.identifier)}",
              not layer.dirty if hasattr(layer, 'dirty') else True)


def main():
    test_parity()
    all_ops = test_golden()
    floor_ops = test_floor()
    test_stage(list(all_ops) + [floor_ops])
    print()
    if FAILED:
        print(f"{len(FAILED)} FAILED: {FAILED}")
        sys.exit(1)
    print("all checks passed")


if __name__ == "__main__":
    main()
