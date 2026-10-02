"""Live floor driver — hybrid: the timeline plays the rhythm (flight bars +
robot swings, baked once into base_live as a clean 2880-frame loop), while
this module drives everything story-bearing as session defaults: product
pipeline and verdicts, wire/pod enforcement colours, PLC cabinet.

Pure python (no pxr/omni), same op contract as overlay_map. The master clock
is the TIMELINE FRAME (passed into tick), so items land in step with the
baked bars. A cell whose controller is down stops producing (its pipeline
counter pauses; the extension freezes the cell's bars/robots with "freeze"
ops); recovery resumes at the next cycle boundary.

Verdicts use the same declared model as the gauge (ADR-0036): P0 assembles
unconditionally (sim_model.run_track :275-280 mirror), P1/P2 draw
sample_part(miss_rate(lam)). Constants mirror build_factory_scene.py
(ast-parity-checked by test_overlay_map.py).
"""
import random

try:
    import sim_model as M
except Exception:                    # pragma: no cover
    M = None

# --- mirrored constants (builder :56-118, :100-104) -------------------------
STATION_PITCH = 1.80
STATIONS = [2.75, 0.95, -0.85, -2.65, -4.45]
WORK_SLOT, INSPECT_SLOT = 2, 3
POOL = 5
BELT_Y_IN, BELT_Y_OUT = 2.925, -4.225
BAR_COUNT = 5
BAR_SPAN = BAR_COUNT * STATION_PITCH
BRIGHT, DIM = 0.85, 0.04
DEAD_WIRE = (0.085, 0.095, 0.105)
CABLE_W_IDLE, CABLE_W_LIVE = 0.055, 0.105
GLOW_FRAMES = 14

CELL_OF_TRACK = {
    "physical": ("Cell_PhysicalPLC", 7.0, "PhysicalPLC"),
    "vanilla": ("Cell_VanillaK8s", 0.0, "VanillaK8s"),
    "dratsn": ("Cell_DRATSN", -7.0, "DRATSN"),
}
RACK_X = {"VanillaK8s": (-1.75, -0.40), "DRATSN": (-8.75, -7.40)}
ARM_LINKS = ["shoulder_link", "upper_arm_link", "forearm_link",
             "wrist_1_link", "wrist_2_link", "wrist_3_link"]

TSN_BASE = (0.02, 0.45, 1.0)         # bake_paths :695
BE_BASE = (1.0, 0.28, 0.03)
PLC_FAULT = (0.95, 0.06, 0.04)       # bake_plc_fault :595
ALERT_RED = (0.95, 0.05, 0.03)       # bake_cell :587-589
ALERT_DIM = (0.30, 0.02, 0.01)

FPS = 24.0
CYCLE_FRAMES = 24
MOVE_FRAMES = 10
DECIDE_OFFSET = 17
LIVE_LOOP_FRAMES = 120 * CYCLE_FRAMES   # base_live horizon (bar phase 120%5==0)

# per-zone topology curve names (builder :1522-1544) for widths writes
CURVES = {
    "PhysicalPLC": {"tsn": ["PLC_ToTSN", "TSN_ToRobot_0", "TSN_ToRobot_1"],
                    "besteffort": ["PLC_ToBestEffort",
                                   "BestEffort_ToRobot_0", "BestEffort_ToRobot_1"]},
    "VanillaK8s": {"tsn": ["RackA_ToTSN", "TSN_ToRobot_0", "TSN_ToRobot_1"],
                   "besteffort": ["RackB_ToBestEffort",
                                  "BestEffort_ToRobot_0", "BestEffort_ToRobot_1"]},
    "DRATSN": {"tsn": ["RackA_ToTSN", "TSN_ToRobot_0", "TSN_ToRobot_1"],
               "besteffort": ["RackB_ToBestEffort",
                              "BestEffort_ToRobot_0", "BestEffort_ToRobot_1"]},
}


def cell_root(track):
    return f"/World/Cells/{CELL_OF_TRACK[track][0]}"


def shader(name):
    return f"/World/Looks/{name}/Shader"


def path_class(on_tsn, tagged):
    if M is not None:
        return M.path_class(on_tsn, tagged)
    return "P0" if (on_tsn and tagged) else ("P1" if on_tsn else "P2")


def freeze_targets(track):
    """(prim_path, [attrs]) the extension freezes while the cell is down —
    exactly the rhythm attrs the base_live timeline owns."""
    root = cell_root(track)
    out = []
    for i in range(BAR_COUNT):
        out.append((f"{root}/Conveyor/FlightBar_{i}",
                    ["xformOp:translate", "visibility"]))
    for r in (0, 1):
        for link in ARM_LINKS:
            out.append((f"{root}/Robots/UR10e_{r}/RobotAsset/{link}",
                        ["xformOp:translate", "xformOp:orient"]))
    return out


class FloorState:
    """One track's cell. tick(frame, ...) with the current timeline frame;
    the pipeline advances at cycle boundaries while running."""

    def __init__(self, track, lam_ms=0.5, seed=None):
        self.track = track
        self.zone = CELL_OF_TRACK[track][2]
        self.cx = CELL_OF_TRACK[track][1]
        self.lam = lam_ms
        self.rng = random.Random(seed if seed is not None
                                 else hash(track) & 0xFFFF)
        self.slot = [None] * POOL
        self.verdict = [None] * POOL
        self.next_item = 0
        self.last_cyc = None
        self.moves = []                # (path, y_from, y_to) this cycle
        self.pending_entry = None      # item ops to fire on arrival
        self.entered = True
        self.decided = True
        self.alert_seq = []            # [(frame_offset_after_arrival, rgb)]
        self.alert_f0 = None
        self.wire_key = None
        self.running_prev = None       # extension reads this for freeze/clear

    # -- per-cycle pipeline (bake_cell :511-583 mirror, items only) ----------

    def _advance_cycle(self):
        ops = []
        root = cell_root(self.track)
        for i, k in enumerate(self.slot):        # exit (:540-548)
            if k is None:
                continue
            if k + 1 >= POOL:
                self.slot[i] = None
                self.verdict[i] = None
                ops.append(("vis", f"{root}/Products/Item_{i}", False))
            else:
                self.slot[i] = k + 1

        entering = self.next_item                # entry (:550-563)
        self.next_item = (self.next_item + 1) % POOL
        if self.slot[entering] is not None:
            entering = self.slot.index(None) if None in self.slot else None
        if entering is not None:
            self.slot[entering] = 0
            cls = self._cls
            if cls == "P0" or M is None:
                assembled = True
            else:
                q = M.miss_rate(self.lam)
                assembled, _run = M.sample_part(
                    q, M.n_control_cycles(), M.RUN_THRESHOLD, self.rng)
            self.verdict[entering] = assembled
            item = f"{root}/Products/Item_{entering}"
            # visibility fires at ARRIVAL (baked f_arrive convention)
            self.pending_entry = [
                ("vis", item, True),
                ("vis", f"{item}/Parts", True),
                ("vis", f"{item}/Good", False),
                ("vis", f"{item}/Bad", False)]
        else:
            self.pending_entry = None
        self.entered = self.pending_entry is None
        self.decided = False

        self.moves = []
        for i, k in enumerate(self.slot):
            if k is None:
                continue
            y_to = STATIONS[k]
            y_from = STATIONS[k - 1] if k > 0 else BELT_Y_IN
            self.moves.append((f"{root}/Products/Item_{i}", y_from, y_to))

        at_gate = next((i for i, k in enumerate(self.slot)
                        if k == INSPECT_SLOT), None)
        if at_gate is not None and self.verdict[at_gate] is False:
            # gate flash frames after arrival (bake_cell :586-590)
            self.alert_f0 = None
            self.alert_seq = [(1, ALERT_RED), (7, ALERT_DIM),
                              (11, ALERT_RED),
                              (CYCLE_FRAMES - MOVE_FRAMES - 1, (0.0, 0.0, 0.0))]
        return ops

    def _decide(self):
        """work-station verdict swap at DECIDE_OFFSET (:572-578)"""
        ops = []
        root = cell_root(self.track)
        at_work = next((i for i, k in enumerate(self.slot)
                        if k == WORK_SLOT), None)
        if at_work is not None:
            item = f"{root}/Products/Item_{at_work}"
            good = self.verdict[at_work]
            ops.append(("vis", f"{item}/Parts", False))
            ops.append(("vis", f"{item}/{'Good' if good else 'Bad'}", True))
        return ops

    # -- wires / pod marker (bake_paths wire()/emit() :698-721 mirror) -------

    def _wires(self, live, cls, on_tsn):
        key = (live, cls, on_tsn)
        if key == self.wire_key:
            return []
        self.wire_key = key
        ops = []
        protected = cls == "P0"
        hue = TSN_BASE if protected else BE_BASE
        a_on, b_on = live and on_tsn, live and not on_tsn

        def wire(mat, curve_key, hue, on):
            base = hue if on else DEAD_WIRE
            level = BRIGHT if on else DIM
            ops.append(("attr", shader(mat), "inputs:diffuseColor",
                        "color3f", base))
            ops.append(("attr", shader(mat), "inputs:emissiveColor",
                        "color3f", tuple(c * level for c in base)))
            w = CABLE_W_LIVE if on else CABLE_W_IDLE
            for cname in CURVES[self.zone][curve_key]:
                ops.append(("widths", f"/World/Topology/{self.zone}/{cname}", w))

        wire(f"TSN_{self.zone}", "tsn", hue, a_on)
        wire(f"BestEffort_{self.zone}", "besteffort", BE_BASE, b_on)
        if self.zone in RACK_X:
            tsn_x, be_x = RACK_X[self.zone]
            ops.append(("attr",
                        f"/World/Infrastructure/{self.zone}/PodAnimationAnchor",
                        "xformOp:translate", "double3",
                        (tsn_x if on_tsn else be_x, 6.15, 2.85)))
            base = hue if live else DEAD_WIRE
            ops.append(("attr", shader(f"Pod_{self.zone}"),
                        "inputs:diffuseColor", "color3f", base))
            ops.append(("attr", shader(f"Pod_{self.zone}"),
                        "inputs:emissiveColor", "color3f",
                        tuple(c * (0.55 if live else DIM) for c in base)))
        if self.track == "physical":
            ops.append(("attr", shader(f"Plc_{self.zone}"),
                        "inputs:emissiveColor", "color3f",
                        (0.0, 0.0, 0.0) if live else PLC_FAULT))
        return ops

    # -- tick ----------------------------------------------------------------

    def tick(self, frame, running, on_tsn, tagged):
        """frame: current timeline frame (float). The extension freezes the
        rhythm attrs on running False->True edges; here only the item
        pipeline pauses."""
        ops = []
        self._cls = path_class(on_tsn, tagged)
        ops.extend(self._wires(running, self._cls, on_tsn))

        cyc = int(frame // CYCLE_FRAMES)
        ph = frame - cyc * CYCLE_FRAMES          # 0..24 within the cycle
        if not running:
            # pipeline pauses; keep the cycle cursor synced so recovery
            # resumes at the NEXT boundary, not with a burst of advances
            self.last_cyc = cyc
            return ops

        if self.last_cyc is None:
            self.last_cyc = cyc
        if cyc != self.last_cyc:                 # boundary (or loop wrap)
            self.last_cyc = cyc
            ops.extend(self._advance_cycle())

        f = min(ph / MOVE_FRAMES, 1.0)
        s = f * f * (3.0 - 2.0 * f)
        if self.moves:
            for path, y0, y1 in self.moves:
                y = y0 + (y1 - y0) * s
                ops.append(("attr", path, "xformOp:translate",
                            "double3", (self.cx, y, 0.0)))
            if f >= 1.0:
                self.moves = []
        if f >= 1.0:
            if not self.entered and self.pending_entry:
                ops.extend(self.pending_entry)
                self.pending_entry = None
                self.entered = True
            if self.alert_seq and self.alert_f0 is None:
                self.alert_f0 = cyc * CYCLE_FRAMES + MOVE_FRAMES

        if not self.decided and ph >= DECIDE_OFFSET:
            self.decided = True
            ops.extend(self._decide())

        if self.alert_f0 is not None and self.alert_seq:
            rel = frame - self.alert_f0
            while self.alert_seq and rel >= self.alert_seq[0][0]:
                _off, rgb = self.alert_seq.pop(0)
                ops.append(("attr", shader(f"Alert_{self.zone}"),
                            "inputs:emissiveColor", "color3f", rgb))
            if not self.alert_seq:
                self.alert_f0 = None
        return ops
