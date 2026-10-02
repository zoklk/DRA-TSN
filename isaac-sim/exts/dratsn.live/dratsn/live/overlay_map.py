"""Live snapshot -> overlay prim write-ops. Pure python: no pxr, no omni.

The same file is consumed by the Kit extension (dratsn.live) and by the local
usd-core smoke test (isaac-sim/test_overlay_map.py). Every constant and layout
function mirrors build_factory_scene.py (line refs in docstrings); the mirror
is deliberate — the builder boots SimulationApp at import time so it cannot be
imported outside Kit, and the finished v11 scene must not be touched
(ADR-0033 §6: live mode rewrites overlay prim attributes only). Drift is
caught by the ast-based constant-parity test.

Op tuples (consumed by applier.apply, all authored as session-layer defaults):
  ("attr",  prim_path, attr_name, type_name, value)
  ("bind",  prim_path, material_name)            # name under /World/Looks
  ("cube",  prim_path, translate, scale, material_name, visible|None)
                                                 # ensure-exists + set
  ("vis",   prim_path, visible_bool)
  ("curve", prim_path, [points])                 # BasisCurves points
"""
import math
import random

try:  # SIGMA is the one declared-model constant shared with the floor model
    import sim_model as _M          # stdlib-only by design (ADR-0036)
    SIGMA = _M.SIGMA
except Exception:                   # pragma: no cover - test env without path
    SIGMA = 1.0

# --- constants mirrored from build_factory_scene.py ------------------------
# module-level ones are parity-checked by test_overlay_map.py
OVERLAY_SWITCH_X = {"VanillaK8s": {"tsn": 1.05, "be": 2.55},
                    "DRATSN": {"tsn": -5.95, "be": -4.45}}
NODE_W, NODE_D, NODE_H = 1.30, 0.95, 1.05
TOWER_Y = 6.15
TOWER_Z0 = 3.55
LEVEL_H = 1.34
RIB_LEN = 0.95
UNIT_MCPU = 1500.0
UNIT_ROWS, UNIT_COLS = 8, 1
UNIT_W, UNIT_D, UNIT_H = 0.72, 0.62, 0.105
UNIT_PITCH = 0.115
UNIT_X_OFF = -0.18
SEG_LEN, SEG_GAP = 0.26, 0.06
VPLC_MCPU_DEFAULT = 2000
BE_MAX_MS = 1.6
SHELF_DZ = -0.02
OVL_SW_Z = 2.90
RIBBON_W, RIBBON_T = 1.04, 0.10
LANE_W, LANE_GAP = 0.36, 0.05
NODE_ALLOC_MCPU = 12000.0
ARRIVE_GLOW = 3.0
# v12 (ADR-0040): device axis + path premise
SCENE_VER = "v12"
GPU_NODE_SUFFIX = "2345"
GLOW_FRAMES = 14
DEAD_WIRE = (0.085, 0.095, 0.105)
RACK_X = {"VanillaK8s": (-1.75, -0.40), "DRATSN": (-8.75, -7.40)}
FPS = 24

# zone colours (builder :926-930, function-local there so not parity-checked)
TSN_BLUE = (0.02, 0.45, 1.0)
BE_ORANGE = (1.0, 0.28, 0.03)   # best-effort NETWORK only
AI_VIOLET = (0.58, 0.22, 0.86)  # AI workload: blades and the GPU it claims
C_LOG = (0.42, 0.44, 0.48)
GAUGE_WARM = (0.95, 0.90, 0.35)
GAUGE_WARM_EM = (0.45, 0.40, 0.10)
GAUGE_HOT = (0.95, 0.15, 0.10)
GAUGE_HOT_EM = (0.90, 0.08, 0.05)
CAP_RED = (0.90, 0.10, 0.08)

ZONE_OF_ARM = {"dratsn": "DRATSN", "vanilla": "VanillaK8s"}

# blade tiers in stack order (builder :1059-1060): (tier, live mat, dead mat)
# fig_regime PriorityClass names and fig_mixed workload names (ADR-0039 §1)
# both land on the same two colours
BLADE_TIERS = (("it-historian", "BlockLog", "BlockLog"),
               ("ot-monitoring", "BlockLog", "BlockLog"),
               ("logging", "BlockLog", "BlockLog"),
               ("vplc", "BlockLog", "BlockLog"),
               ("it-ai", "BlockAI", "BlockAIDead"),
               ("ai-", "BlockAI", "BlockAIDead"))

# dead-material desaturation set (builder :1267-1275): mat -> healthy colour
PATH_BLUE = TSN_BLUE                # builder m_path diffuse (v12): one hue for
                                    # the path, width carries "in use"

DEAD_MATS = {"PlatDead": TSN_BLUE, "BlockAIDead": AI_VIOLET,
             "LaneDead": BE_ORANGE, "LaneDeadProt": TSN_BLUE,
             "FillDeadProt": TSN_BLUE, "CapDead": CAP_RED,
             "PathDead": PATH_BLUE, "GpuDead": AI_VIOLET,
             "TrunkDead": BE_ORANGE}


def ai_pods(pods):
    """AI-tier pod count under either vocabulary (builder _ai_pods)."""
    return sum(v["count"] for t, v in pods.items()
               if t.startswith("it-ai") or t.startswith("ai-"))


def has_gpu(n):
    """Does this node own a GPU (builder _has_gpu, ADR-0039 §9 presence only)."""
    g = n.get("gpu")
    if isinstance(g, dict):
        return bool(g.get("count", 1))
    if g is not None:
        return bool(g)
    return n["name"][-1] in GPU_NODE_SUFFIX


def has_tsn_path(n, tracked_domain=None):
    """Does this node publish the tracked line's stream (builder _has_path).

    Measured: `domains` is what the node's TSNStreams actually carry, so a TSN
    node outside the HA set (ADR-0039 §2) shows a NIC and no path. A snapshot
    without the field predates it and is treated as "all TSN nodes have one".
    """
    if not n["tsn"]:
        return False
    doms = n.get("domains")
    if doms is None:
        return True
    return tracked_domain in doms if tracked_domain else bool(doms)

MOVE_S = 0.8            # live actor flight time (wall clock, replaces baked
GLOW_S = GLOW_FRAMES / FPS  # frame interpolation; §3 of the plan)
PULSE_S = 6 / FPS       # detect/sched emissive pulse length (builder :1256)
GAUGE_PERIOD_S = 4 / FPS    # builder resamples every 4 frames (:1295)


# --- paths -----------------------------------------------------------------

def zone_root(zone):
    return f"/World/ClusterOverlay/{zone}"


def node_prim(zone, name, alias=None):
    """Hyphen -> underscore, the one owner of the rule (builder :1030).

    alias maps a live node name to the baked prim leaf when the live cluster
    is named differently from the one the scene was baked from (dual-cluster
    demo: live vanilla-worker2 drives baked dratsn_worker2 in the VanillaK8s
    zone — identity is positional: rack = class, level = index)."""
    leaf = (alias or {}).get(name) or name.replace('-', '_')
    return f"{zone_root(zone)}/Nodes/{leaf}"


def mat_name(zone, name):
    return f"Ovl{zone}_{name}"


def shader_path(zone, name):
    return f"/World/Looks/{mat_name(zone, name)}/Shader"


# --- layout mirrors --------------------------------------------------------

def node_positions(zone, nodes):
    """name -> (x, y, level, base_z). Builder :978-982: rack = class,
    level = index within the class, in cluster.nodes order."""
    rack_tsn_x, rack_be_x = RACK_X[zone]
    pos = {}
    for is_tsn, rack_x in ((True, rack_tsn_x), (False, rack_be_x)):
        for i, n in enumerate([n for n in nodes if n["tsn"] == is_tsn]):
            pos[n["name"]] = (rack_x, TOWER_Y, i, TOWER_Z0 + i * LEVEL_H)
    return pos


def tier_mcpu(pods, prefix):
    """builder _tier_mcpu :908-909"""
    return sum(v["mcpu"] for t, v in pods.items() if t.startswith(prefix))


def blade_fill(pods, is_fault):
    """8 material names, largest-remainder fill (builder :1061-1070)."""
    exact = [max(tier_mcpu(pods, t), 0) / UNIT_MCPU for t, _l, _d in BLADE_TIERS]
    counts = [int(v) for v in exact]
    want = round(sum(exact))
    for _ in range(want - sum(counts)):
        i = max(range(len(exact)), key=lambda k: exact[k] - counts[k])
        counts[i] += 1
    state = []
    for (tier, mat_live, mat_dead), cnt in zip(BLADE_TIERS, counts):
        state += [mat_dead if is_fault else mat_live] * cnt
    state = state[: UNIT_ROWS * UNIT_COLS]
    return state + ["UnitEmpty"] * (UNIT_ROWS * UNIT_COLS - len(state))


def blade_pos(pos_entry, i):
    """socket i centre (builder :1075-1077)"""
    x, y, _level, base = pos_entry
    col, row = divmod(i, UNIT_ROWS)
    return (x + (col * 2 - 1) * UNIT_X_OFF, y, base + 0.10 + row * UNIT_PITCH)


def unit_block(pos_entry, slot, u):
    """actor block centre + height (builder :1199-1204)"""
    x, y, _level, base = pos_entry
    col, row = divmod(slot, UNIT_ROWS)
    h = (u - 1) * UNIT_PITCH + UNIT_H
    return ((x + (col * 2 - 1) * UNIT_X_OFF, y,
             base + 0.10 + (row + (u - 1) / 2) * UNIT_PITCH), h)


def take_slot(cursor, node, u):
    """slot cursor advance (builder :1192-1197); cursor is a mutable dict"""
    slot = cursor.get(node, 0)
    if slot % UNIT_ROWS + u > UNIT_ROWS:
        slot = (slot // UNIT_ROWS + 1) * UNIT_ROWS
    cursor[node] = slot + u
    return slot


def lane_anchor(pos_entry):
    """(x, y_front, z_shelf) of a node's ribbon (builder :1087-1088)"""
    x, y, _level, base = pos_entry
    return (x, y - NODE_D / 2 - 0.06, base + SHELF_DZ)


def stream_seg(pos_entry, seg_idx):
    """protected-lane stream segment centre + dims (builder :1106-1109)"""
    x, y_front, z_shelf = lane_anchor(pos_entry)
    x_prot = x - (LANE_W + LANE_GAP) / 2
    pos = (x_prot, y_front - 0.18 - seg_idx * (SEG_LEN + SEG_GAP),
           z_shelf + RIBBON_T / 2 + 0.015)
    return pos, (LANE_W * 0.78, SEG_LEN, 0.03)


def gauge_geom(pos_entry, delay, on_prot, on_tsn_node):
    """gauge translate/scale for one draw (builder :1304-1332)"""
    ax, ay_front, az_shelf = lane_anchor(pos_entry)
    if on_prot:
        x_off, w = -(LANE_W + LANE_GAP) / 2, LANE_W * 0.55
    else:
        x_off = (LANE_W + LANE_GAP) / 2 if on_tsn_node else 0.0
        w = (LANE_W if on_tsn_node else RIBBON_W * 0.84) * 0.55
    ln = max(delay, 0.03) * RIB_LEN
    t = (ax + x_off, ay_front - ln / 2, az_shelf + RIBBON_T / 2 + 0.045)
    return t, (w, ln, 0.05)


def marker_point(zone, on_tsn):
    """rack pod-marker end of the Binding curve (builder :1340)"""
    tsn_x, be_x = RACK_X[zone]
    return (tsn_x if on_tsn else be_x, 6.15, 2.85)


# --- desired state ---------------------------------------------------------

def _actor_stack_deductions(snap):
    """pods each actor represents, to subtract from the static blade stacks
    (mirror of builder :1033-1051, incremental): node -> [(tier, mcpu)]"""
    ded = {}

    def sub(node, tier, mcpu):
        if node:
            ded.setdefault(node, []).append((tier, mcpu))

    tr = snap.get("tracked") or {}
    if tr.get("node"):
        node = tr["resched_node"] if tr.get("state") in ("scheduled", "running_again") \
            else tr["node"]
        sub(node, "vplc-line", tr.get("mcpu", VPLC_MCPU_DEFAULT))
    peer = snap.get("peer") or {}
    if peer.get("node"):
        node = peer["resched_node"] if peer.get("state") in ("scheduled", "running_again") \
            else peer["node"]
        sub(node, peer.get("tier", "ot-monitoring"), peer.get("mcpu", 500))
    for ev in snap.get("evictions") or []:
        # the victim's replacement pod is drawn as the Victim actor at its
        # landing node; the vacated source needs no deduction — the pod is
        # already gone from the API there
        if ev.get("resched_node") and ev.get("ai_resched_ms") is not None:
            sub(ev["resched_node"], ev.get("priority_class", "it-ai"),
                ev.get("mcpu", 2000))
    return ded


def _deduct(pods, deductions):
    out = {t: dict(v) for t, v in pods.items()}
    for tier, mcpu in deductions:
        if tier in out:
            out[tier]["mcpu"] = max(0, out[tier]["mcpu"] - int(mcpu))
            out[tier]["count"] = max(0, out[tier]["count"] - 1)
    return out


def desired_actors(snap, blades_used, pos, anim):
    """actor placements with sticky slot assignment.

    Returns {actor_name: {node, u, diffuse, moving_from}}; slot bookkeeping
    lives in anim.slots / anim.freed so a placement holds until it changes
    (the baked equivalent is :1206-1217)."""
    acts = {}
    tr = snap.get("tracked") or {}
    if tr.get("node"):
        landed = tr.get("state") in ("scheduled", "running_again")
        node = tr["resched_node"] if landed and tr.get("resched_node") else tr["node"]
        u = max(1, round(tr.get("mcpu", VPLC_MCPU_DEFAULT) / UNIT_MCPU))
        acts["Tracked"] = {"node": node, "u": u,
                           "dead": tr.get("state") == "down", "base": TSN_BLUE}
    peer = snap.get("peer") or {}
    if peer.get("node"):
        landed = peer.get("state") in ("scheduled", "running_again")
        node = peer["resched_node"] if landed and peer.get("resched_node") else peer["node"]
        u = max(1, round(peer.get("mcpu", 500) / UNIT_MCPU))
        acts["OtherLoad"] = {"node": node, "u": u,
                             "dead": peer.get("state") == "down", "base": C_LOG}
    for i, ev in enumerate(snap.get("evictions") or []):
        if not ev.get("resched_node"):
            continue        # victim evicted but replacement not landed: skip
        u = max(1, round(ev.get("mcpu", 2000) / UNIT_MCPU))
        # a live cascade can evict more than it-ai (AI replacements pushing
        # historians off general nodes) — colour by tier, orange = AI only
        base = AI_VIOLET if ev.get("priority_class", "").startswith(
            ("it-ai", "ai-")) else C_LOG
        acts[f"Victim_{i}"] = {"node": ev["resched_node"], "u": u,
                               "dead": False, "base": base,
                               "src": ev.get("node")}

    # slot assignment: sticky per (actor, node); victims free their source
    # slots for the tracked vPLC to land in (builder :1208-1217)
    cursor = dict(blades_used)
    for name, a in sorted(acts.items()):
        key = (name, a["node"])
        if anim.slots.get(name, (None, None))[0] == a["node"]:
            a["slot"] = anim.slots[name][1]
            cursor[a["node"]] = max(cursor.get(a["node"], 0), a["slot"] + a["u"])
            continue
        if name == "Tracked" and anim.freed:
            pool = anim.freed.get(a["node"])
            if pool:
                a["slot"] = pool.pop(0)
                anim.slots[name] = (a["node"], a["slot"])
                continue
        a["slot"] = take_slot(cursor, a["node"], a["u"])
        if name.startswith("Victim") and a.get("src"):
            src_slot = anim.slots.get(name, (None, None))
            if src_slot[0] is None:   # first sighting: record the freed source
                anim.freed.setdefault(a["src"],
                                      []).append(blades_used.get(a["src"], 0))
        anim.slots[name] = (a["node"], a["slot"])
    return acts


def compute_ops(snap, anim, first=False):
    """Diff the snapshot against anim.applied; returns (ops, moves, pulses).

    moves  = [(prim_path, p0, p1, height)]   -> AnimState lerps translate
    pulses = [(shader_path, peak_rgb)]       -> AnimState emissive pulse
    """
    arm = snap.get("arm")
    zone = ZONE_OF_ARM.get(arm)
    if zone is None:
        return [], [], []
    root = zone_root(zone)
    nodes = snap["cluster"]["nodes"]
    # the stream domain the tracked line needs; a node must publish it to count
    # as having a path (live_serve _assemble)
    tracked_domain = snap["cluster"].get("tracked_domain")
    fault = snap["cluster"].get("fault_node")
    has_dra = zone == "DRATSN"
    pos = node_positions(zone, nodes)
    tracked = snap.get("tracked") or {}
    stages = snap.get("stages") or {}

    desired = {}      # op-key -> op

    def put(op):
        if op[0] == "attr":
            desired[(op[0], op[1], op[2])] = op
        else:
            desired[(op[0], op[1])] = op

    # -- nodes: platform / lanes / blades ------------------------------------
    ded = _actor_stack_deductions(snap)
    blades_used = {}
    level_of_class = {True: -1, False: -1}   # rack slot index per node class
    for n in nodes:
        name = n["name"]
        nroot = node_prim(zone, name, anim.alias)
        level_of_class[n["tsn"]] += 1
        level = level_of_class[n["tsn"]]
        # dead display keys off per-node readiness, not the single fault
        # pointer — two nodes can be down at once, and overwriting state.env
        # with a second injection must not resurrect the first casualty
        is_fault = not n.get("ready", True)
        # infrastructure rack LED: slot = tower level; the LED materials are
        # zone-shared greens, so a down node gets its own LED prim rebound to
        # the base StatusRed material (and back on recovery)
        led = (f"/World/Infrastructure/{zone}/Rack_{'A' if n['tsn'] else 'B'}"
               f"/LED_{level:02d}")
        put(("bind", led, "StatusRed" if is_fault else
             (f"RackLed_TSN_{zone}" if n["tsn"] else f"RackLed_BE_{zone}")))
        # baked platforms interpenetrate across the 1.35 m rack gap (width
        # 1.54) — override the scale until the replay set is rebaked with the
        # builder fix (NODE_W - 0.06)
        put(("attr", f"{nroot}/Platform", "xformOp:scale", "double3",
             (NODE_W - 0.06, NODE_D + 0.24, 0.10)))
        pods = _deduct(n.get("pods", {}), ded.get(name, []))
        put(("bind", f"{nroot}/Platform",
             mat_name(zone, "PlatDead" if is_fault else
                      ("PlatTSN" if n["tsn"] else "PlatGen"))))
        blades = blade_fill(pods, is_fault)
        blades_used[name] = sum(1 for b in blades if b != "UnitEmpty")
        for i, b in enumerate(blades):
            put(("bind", f"{nroot}/Cores/Core_{i}", mat_name(zone, b)))
        # GPU box (v12): presence is baked, colour says claimed — same rule
        # as a core socket (dark when free)
        if has_gpu(n):
            claimed = ai_pods(pods) > 0
            put(("bind", f"{nroot}/Gpu/Box",
                 mat_name(zone, ("GpuDead" if is_fault else "Gpu") if claimed
                          else "UnitEmpty")))
        # lanes + caps + trunk (builder :1090-1154). A TSN node's trunk states
        # the path premise (v12): configured / configured-but-dead / no path.
        if n["tsn"]:
            trunk_mat = "PathDead" if is_fault else (
                "Path" if has_tsn_path(n, tracked_domain) else "NoPath")
        else:
            trunk_mat = "TrunkDead" if is_fault else "TrunkBE"
        put(("bind", f"{nroot}/Ribbon/Trunk", mat_name(zone, trunk_mat)))
        put(("bind", f"{nroot}/Ribbon/LaneBE",
             mat_name(zone, "LaneDead" if is_fault else "LaneBE")))
        put(("bind", f"{nroot}/Ribbon/Deadline",
             mat_name(zone, "CapDead" if is_fault else "BudgetLimit")))
        if n["tsn"]:
            put(("bind", f"{nroot}/Ribbon/LaneProtected",
                 mat_name(zone, "LaneDeadProt" if is_fault else "LaneProt")))
            put(("bind", f"{nroot}/Ribbon/BudgetLimit",
                 mat_name(zone, "CapDead" if is_fault else "BudgetLimit")))

    # dead materials lose their hue while any node is down (builder :1267-1275)
    fault_active = any(not n.get("ready", True) for n in nodes)
    for m, healthy in DEAD_MATS.items():
        put(("attr", shader_path(zone, m), "inputs:diffuseColor", "color3f",
             DEAD_WIRE if fault_active else healthy))

    # -- protected-lane stream segments (DRATSN zone only, builder :1100-1126)
    if has_dra:
        stream_node = None
        if tracked.get("node"):
            landed = tracked.get("state") in ("scheduled", "running_again")
            cand = tracked["resched_node"] if landed and tracked.get("resched_node") \
                else tracked["node"]
            n_of = {n["name"]: n for n in nodes}
            down = tracked.get("state") == "down" or \
                not n_of.get(cand, {}).get("ready", True)
            if cand in pos and n_of.get(cand, {}).get("tsn") and not down:
                stream_node = cand
        tsn_level = -1
        for n in nodes:
            if not n["tsn"]:
                continue
            tsn_level += 1
            nroot = node_prim(zone, n["name"], anim.alias)
            spos, sdims = stream_seg(pos[n["name"]], 0)
            put(("cube", f"{nroot}/Ribbon/Stream_0", spos, sdims,
                 mat_name(zone, "FillProt"), n["name"] == stream_node))
            # the lit chain follows the stream (v12): node -> switch -> cell
            if has_tsn_path(n, tracked_domain):
                put(("vis", f"{nroot}/Ribbon/PathLit",
                     n["name"] == stream_node))

    # -- actors --------------------------------------------------------------
    moves = []
    acts = desired_actors(snap, blades_used, pos, anim)
    for name, a in acts.items():
        apath = f"{root}/Actors/{name}"
        p1, h = unit_block(pos[a["node"]], a["slot"], a["u"])
        scale = (UNIT_W * 1.04, UNIT_D * 1.04, h + 0.012)
        prev = anim.actor_pos.get(name)
        if prev is None or first:
            put(("cube", apath, p1, scale,
                 mat_name(zone, _actor_mat(name)), True))
        elif prev != p1:
            moves.append((apath, prev, p1, h))
            put(("attr", apath, "xformOp:scale", "double3", scale))
        anim.actor_pos[name] = p1
        put(("attr", shader_path(zone, _actor_mat(name)),
             "inputs:diffuseColor", "color3f",
             DEAD_WIRE if a["dead"] else a["base"]))

    # -- binding curve (builder :1340-1347) ----------------------------------
    if "Tracked" in acts:
        a = acts["Tracked"]
        tsn_of = {n["name"]: n["tsn"] for n in nodes}
        p_block, _h = unit_block(pos[a["node"]], a["slot"], a["u"])
        put(("curve", f"{root}/Binding",
             [p_block, marker_point(zone, tsn_of.get(a["node"], False))]))

    # -- stage pulses (builder :1250-1263), edge-triggered -------------------
    # (shader, peak, baseline, duration_s); baseline = the mat's authored
    # emissive, restored after the pulse
    zero = (0.0, 0.0, 0.0)
    pulses = []
    if stages.get("detect_ms") is not None and "detect" not in anim.fired:
        anim.fired.add("detect")
        pulses.append((shader_path(zone, "PlatTSN"),
                       tuple(c * 1.6 for c in TSN_BLUE), zero, PULSE_S))
        if not has_dra:
            pulses.append((shader_path(zone, "PlatGen"),
                           tuple(c * 1.6 for c in BE_ORANGE), zero, PULSE_S))
    if has_dra and stages.get("sched_ms") is not None and "sched" not in anim.fired:
        anim.fired.add("sched")
        pulses.append((shader_path(zone, "LaneProt"),
                       tuple(c * 1.2 for c in TSN_BLUE), zero, PULSE_S))
    if tracked.get("state") == "running_again" and "arrive" not in anim.fired:
        anim.fired.add("arrive")
        pulses.append((shader_path(zone, "ActTracked"),
                       tuple(c * ARRIVE_GLOW for c in TSN_BLUE),
                       tuple(c * 0.18 for c in TSN_BLUE), GLOW_S))

    # -- gauge context (consumed by AnimState.tick) --------------------------
    g_node = None
    if tracked.get("node") and tracked.get("state") != "down":
        landed = tracked.get("state") in ("scheduled", "running_again")
        cand = tracked["resched_node"] if landed and tracked.get("resched_node") \
            else tracked["node"]
        if cand in pos:
            g_node = cand
    tsn_names = {n["name"] for n in nodes if n["tsn"]}
    anim.gauge_ctx = None if g_node is None else {
        "zone": zone, "pos": pos[g_node], "has_dra": has_dra,
        "on_tsn_node": g_node in tsn_names,
        "lam": snap.get("lam_ms", 0.5)}
    if g_node is None:
        put(("vis", f"{root}/DelayGauge", False))

    # -- playback-leak seal (hybrid mode: the timeline runs) -----------------
    # the baked overlay layer carries story time samples on attrs live only
    # authors on events (emissive pulses, victim actors, arrival stream
    # segments). Author their baseline as defaults so a playing timeline
    # cannot resurrect the baked story underneath the live state.
    zero = (0.0, 0.0, 0.0)
    for m in ("PlatTSN", "PlatGen", "LaneProt", "ActTracked", "ActOther",
              "Gauge"):
        k = ("attr", shader_path(zone, m), "inputs:emissiveColor")
        if k not in desired:
            put(("attr", shader_path(zone, m), "inputs:emissiveColor",
                 "color3f", zero))
    for i in range(3):                    # baked victim actors not live yet
        name = f"Victim_{i}"
        if f"{root}/Actors/{name}" not in {f"{root}/Actors/{n}" for n in acts}:
            put(("vis", f"{root}/Actors/{name}", False))
    if has_dra:                           # baked arrival segments (Stream_1+)
        for n in nodes:
            if n["tsn"]:
                nroot = node_prim(zone, n["name"], anim.alias)
                for si in (1, 2):
                    put(("vis", f"{nroot}/Ribbon/Stream_{si}", False))

    # -- diff against what was already applied -------------------------------
    ops = []
    for key, op in desired.items():
        if first or anim.applied.get(key) != op:
            ops.append(op)
            anim.applied[key] = op
    return ops, moves, pulses


def _actor_mat(name):
    if name == "Tracked":
        return "ActTracked"
    if name == "OtherLoad":
        return "ActOther"
    return "ActVictim" + name.split("_")[1]


# --- wall-clock animation --------------------------------------------------

class AnimState:
    """Per-zone animation bookkeeping. tick() emits transient ops; all state
    changes flow through compute_ops so a poll drop loses nothing."""

    def __init__(self):
        self.applied = {}
        self.alias = {}          # live node name -> baked prim leaf (dual demo)
        self.slots = {}          # actor -> (node, slot)
        self.freed = {}          # node -> [slot]
        self.actor_pos = {}      # actor -> last target translate
        self.fired = set()       # one-shot stage pulses
        self.gauge_ctx = None
        self.gauge_rng = {}      # zone -> Random
        self._moves = []         # (path, p0, p1, t_start)
        self._pulses = []        # (shader, peak, t_start)
        self._gauge_next = 0.0

    def reset_fault(self):
        """new inject-fault (t0 changed): re-arm one-shot pulses & slots"""
        self.fired.clear()
        self.freed.clear()

    def add_moves(self, moves, now):
        for path, p0, p1, _h in moves:
            self._moves.append((path, p0, p1, now))

    def add_pulses(self, pulses, now):
        for shader, peak, base, dur in pulses:
            self._pulses.append((shader, peak, base, dur, now))

    def tick(self, now):
        ops = []
        keep = []
        for path, p0, p1, t_start in self._moves:
            f = min(1.0, (now - t_start) / MOVE_S)
            s = f * f * (3.0 - 2.0 * f)          # smoothstep
            p = tuple(a + (b - a) * s for a, b in zip(p0, p1))
            ops.append(("attr", path, "xformOp:translate", "double3", p))
            if f < 1.0:
                keep.append((path, p0, p1, t_start))
        self._moves = keep
        keep = []
        for shader, peak, base, dur, t_start in self._pulses:
            f = (now - t_start) / dur
            if f >= 1.0:
                ops.append(("attr", shader, "inputs:emissiveColor", "color3f",
                            base))
            else:
                level = tuple(b + (p - b) * (1.0 - f)
                              for p, b in zip(peak, base))
                ops.append(("attr", shader, "inputs:emissiveColor", "color3f",
                            level))
                keep.append((shader, peak, base, dur, t_start))
        self._pulses = keep
        ops.extend(self._gauge_tick(now))
        return ops

    def _gauge_tick(self, now):
        """resample the delay gauge (builder :1292-1334; live = wall clock,
        independent draws — a display, not a measurement)"""
        ctx = self.gauge_ctx
        if ctx is None or now < self._gauge_next:
            return []
        self._gauge_next = now + GAUGE_PERIOD_S
        zone = ctx["zone"]
        rng = self.gauge_rng.setdefault(
            zone, random.Random(97 if ctx["has_dra"] else 53))
        if ctx["has_dra"]:
            delay, hot = rng.uniform(0.08, 0.92), False
        else:
            delay = min(rng.lognormvariate(math.log(ctx["lam"]), SIGMA),
                        BE_MAX_MS - 0.05)
            hot = delay > 1.0
        t, s = gauge_geom(ctx["pos"], delay, ctx["has_dra"], ctx["on_tsn_node"])
        root = zone_root(zone)
        return [
            ("vis", f"{root}/DelayGauge", True),
            ("attr", f"{root}/DelayGauge", "xformOp:translate", "double3", t),
            ("attr", f"{root}/DelayGauge", "xformOp:scale", "double3", s),
            ("attr", shader_path(zone, "Gauge"), "inputs:diffuseColor",
             "color3f", GAUGE_HOT if hot else GAUGE_WARM),
            ("attr", shader_path(zone, "Gauge"), "inputs:emissiveColor",
             "color3f", GAUGE_HOT_EM if hot else GAUGE_WARM_EM),
        ]
