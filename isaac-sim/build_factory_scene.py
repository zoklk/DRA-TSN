#!/usr/bin/env python3
"""Build the three-cell DRATSN comparison scene as a portable USDA stage.

v10 turned the static v9 stage into a scenario replay. v11 adds the abstract
C/N/S cluster overlay above the two K8s zones and splits the output into three
composed layers: `<name>_base.usda` (the whole v10-shape stage, rebuilt from
the feed), `<name>_overlay.usda` (the cluster layer only) and `<name>.usda`
(the root, subLayers = [overlay, base]) — muting the overlay is the
before/after comparison, and the planned live mode rewrites overlay attributes
while the base stays baked.

What the scene shows and where it comes from:
  placement (measured)   which rack the vPLC runs on -> which cable path lights up
  verdict (modelled)     whether a workpiece is assembled -> product state swap
  downtime (feed)        PLC repair or Pod reschedule -> the line holds still
  cluster (feed v2.1)    node boxes with CPU-request stacks (C), two-lane ribbon
                         uplinks (N: slack budget + bandwidth ledger), an inert
                         storage slab (S) — request-side declarations, no traffic

v12 answers "a TSN NIC is not a path" on screen (ADR-0040): the device axis
becomes visible (a GPU slot column beside the blade stack, ADR-0039 topology),
the stream path is drawn as a chain that reaches the cell instead of stopping
at the switch, a TSN node that has no configured path to that cell is drawn
grey-trunked with no chain at all, the protected queue shows its empty slots
next to the occupied ones, and the opening seconds light the chains once — the
CNC allocation beat, so the premise is on screen before the fault is.

The verdict itself lives in sim_model.py, which has no Isaac dependency, so the
same decisions drive both this stage and the lambda sweep.

Usage (inside the Isaac Sim container):
  ./python.sh build_factory_scene.py out.usda [--feed feed.json] [--lambda 5.0]
"""

from __future__ import annotations

import argparse
import math
import random
from pathlib import Path

import sim_model as M

_ap = argparse.ArgumentParser()
_ap.add_argument("output")
_ap.add_argument("--feed", help="scenario feed json (default: built-in scenario A)")
_ap.add_argument("--lambda", dest="lam", type=float, default=0.5,
                 help="unprotected-path delay median, ms (0.5 sits in the "
                      "ADR-0036 transition band, so the vanilla track fails "
                      "stochastically rather than always)")
_ap.add_argument("--seed", type=int, default=0)
_ap.add_argument("--live-base", action="store_true",
                 help="bake the live-mode rhythm base instead of the story: "
                      "flight bars + robot swings looping every cycle, no "
                      "item/product/wire/packet samples. Emits "
                      "<stem>_base.usda plus a root that sublayers the "
                      "EXISTING v11 overlay — the replay files stay untouched "
                      "(ADR-0033 hybrid live mode)")
ARGS, _ = _ap.parse_known_args()

from isaacsim import SimulationApp

simulation_app = SimulationApp({"headless": True})

from pxr import Gf, Sdf, Usd, UsdGeom, UsdLux, UsdShade


UR10E_ASSET = (
    "/isaac-sim/exts/isaacsim.asset.transformer.rules/data/tests/ur10e/ur10e.usd"
)

# --- scene constants -------------------------------------------------------
# The fixed furniture already pins the station grid: the robots sit at y = -0.85
# and the inspection gate at y = -2.65, so the index pitch is 1.80 and the rest
# of the stations fall inside the belt span y in [-4.225, 2.925].
STATION_PITCH = 1.80
STATIONS = [2.75, 0.95, -0.85, -2.65, -4.45]  # entry, queue, work, inspect, exit
WORK_SLOT, INSPECT_SLOT = 2, 3
POOL = len(STATIONS)
# Parts fed in the last cycles still have to reach the work station, so the stage
# indexes a few extra cycles with nothing new entering the belt.
DRAIN_CYCLES = POOL - 1

BELT_Y_IN, BELT_Y_OUT = 2.925, -4.225
BELT_TOP_Z = 0.83
PARTS_Z, AGV_Z, AGV_SCALE = 0.84, 0.72, 0.68

# Flight bars ride the belt surface and mark the pitch. They replace v9's
# in-place spinning rollers, which read as wheels turning but going nowhere.
BAR_COUNT = 5
BAR_SPAN = BAR_COUNT * STATION_PITCH

FPS = M.FPS
MOVE_FRAMES, DWELL_FRAMES = M.MOVE_FRAMES, M.DWELL_FRAMES

# live-mode rhythm loop: 120 cycles so the seam is invisible — the bar
# pattern repeats every BAR_COUNT(5) indexes (120 % 5 == 0), the pod bob
# every 48 frames (2880 % 48 == 0), the robot swing every cycle. No drain
# (the rhythm bake carries no items).
LIVE_CYCLES = 120
CYCLE_FRAMES = M.CYCLE_FRAMES
N_CYCLES = M.N_CYCLES
END_FRAME = (N_CYCLES + DRAIN_CYCLES) * CYCLE_FRAMES

DECIDE_OFFSET = MOVE_FRAMES + DWELL_FRAMES // 2

# The UR10e rests fully extended along its own +X for 1.18 m, so the two robots
# have to face across the belt (yaw 0 and 180) rather than along it, or their
# hands never come near a workpiece. The reach is then a rigid swing about the
# shoulder, which keeps the base planted instead of tipping it through the floor.
ROBOT_YAWS = (0.0, 180.0)
SHOULDER_PIVOT = (0.0, 0.0, 0.1807)
ARM_LINKS = ["shoulder_link", "upper_arm_link", "forearm_link",
             "wrist_1_link", "wrist_2_link", "wrist_3_link"]
# Negative pitches the extended arm upward. The parked arm stays fully off the
# conveyor; assembly bends only the elbow-down chain (the base and upper arm
# hold the parked pitch), so the arm never lies across the belt.
ARM_REST_DEG = -52.0
ELBOW_REACH_DEG = 62.0

CELL_OF_TRACK = {
    "physical": ("Cell_PhysicalPLC", 7.0, "PhysicalPLC"),
    "vanilla": ("Cell_VanillaK8s", 0.0, "VanillaK8s"),
    "dratsn": ("Cell_DRATSN", -7.0, "DRATSN"),
}
RACK_X = {"VanillaK8s": (-1.75, -0.40), "DRATSN": (-8.75, -7.40)}

BRIGHT, DIM = 0.85, 0.04
GLOW_FRAMES = 14  # afterglow that makes a sub-cycle reschedule catchable by eye
# An idle or dead wire loses its hue, not just its glow. Dimming emissive alone
# leaves the cable its full blue/orange diffuse colour, so a stopped cell still
# reads as carrying traffic.
DEAD_WIRE = (0.085, 0.095, 0.105)
CABLE_W_IDLE, CABLE_W_LIVE = 0.055, 0.105
PACKETS_PER_PATH = 3
PACKET_GAP_FRAMES = 5  # hidden return leg, so a trip never overlaps the next
# 1 m is roughly 32 px at the overview camera, so a few centimetres of travel
# reads as a static prim. The marker has to move far enough to be seen moving.
POD_MARKER = 0.42
POD_BOB = 0.22


class Mats:
    """Material library that keeps the shader around so inputs can be animated."""

    def __init__(self, stage):
        self.stage = stage
        self._mat = {}
        self._shader = {}

    def add(self, name, color, *, metallic=0.0, roughness=0.45,
            emissive=(0.0, 0.0, 0.0), opacity=None):
        mat = UsdShade.Material.Define(self.stage, f"/World/Looks/{name}")
        shader = UsdShade.Shader.Define(self.stage, f"/World/Looks/{name}/Shader")
        shader.CreateIdAttr("UsdPreviewSurface")
        shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(*color))
        shader.CreateInput("metallic", Sdf.ValueTypeNames.Float).Set(metallic)
        shader.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(roughness)
        shader.CreateInput("emissiveColor", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(*emissive))
        if opacity is not None:
            shader.CreateInput("opacity", Sdf.ValueTypeNames.Float).Set(opacity)
        mat.CreateSurfaceOutput().ConnectToSource(shader.ConnectableAPI(), "surface")
        self._mat[name] = mat
        self._shader[name] = shader
        return mat

    def __getitem__(self, name):
        return self._mat[name]

    def emissive(self, name, value, frame):
        self._shader[name].GetInput("emissiveColor").Set(Gf.Vec3f(*value), Usd.TimeCode(frame))

    def diffuse(self, name, value, frame):
        self._shader[name].GetInput("diffuseColor").Set(Gf.Vec3f(*value), Usd.TimeCode(frame))


def bind(prim, mat) -> None:
    if mat:
        UsdShade.MaterialBindingAPI.Apply(prim).Bind(mat)


def cube(stage, path, pos, dims, mat=None):
    obj = UsdGeom.Cube.Define(stage, path)
    obj.CreateSizeAttr(1.0)
    obj.AddTranslateOp().Set(Gf.Vec3d(*pos))
    obj.AddScaleOp().Set(Gf.Vec3d(*dims))
    bind(obj.GetPrim(), mat)
    return obj.GetPrim()


def rotated_cube(stage, path, pos, dims, rotation, mat=None):
    obj = UsdGeom.Cube.Define(stage, path)
    obj.CreateSizeAttr(1.0)
    obj.AddTranslateOp().Set(Gf.Vec3d(*pos))
    obj.AddRotateXYZOp().Set(Gf.Vec3f(*rotation))
    obj.AddScaleOp().Set(Gf.Vec3d(*dims))
    bind(obj.GetPrim(), mat)
    return obj.GetPrim()


def cylinder(stage, path, pos, radius, height, mat=None, rotate=(0.0, 0.0, 0.0)):
    obj = UsdGeom.Cylinder.Define(stage, path)
    obj.CreateRadiusAttr(radius)
    obj.CreateHeightAttr(height)
    obj.CreateAxisAttr("Z")
    obj.AddTranslateOp().Set(Gf.Vec3d(*pos))
    if rotate != (0.0, 0.0, 0.0):
        obj.AddRotateXYZOp().Set(Gf.Vec3f(*rotate))
    bind(obj.GetPrim(), mat)
    return obj.GetPrim()


def cable(stage, path, points, mat, width=CABLE_W_IDLE):
    curve = UsdGeom.BasisCurves.Define(stage, path)
    curve.CreateTypeAttr("linear")
    curve.CreateWrapAttr("nonperiodic")
    curve.CreateCurveVertexCountsAttr([len(points)])
    curve.CreatePointsAttr([Gf.Vec3f(*p) for p in points])
    curve.CreateWidthsAttr([width] * len(points))
    bind(curve.GetPrim(), mat)
    return curve


def rack(stage, root, x, y, mats, led_mat=None):
    # three server slots: one per cluster node of the rack's class, mirroring
    # the three-level overlay tower stacked above it
    cube(stage, f"{root}/Frame", (x, y, 1.15), (1.05, 0.75, 2.3), mats["dark"])
    cube(stage, f"{root}/Front", (x, y - 0.39, 1.15), (0.91, 0.035, 2.15), mats["black"])
    for idx in range(3):
        z = 0.45 + idx * 0.62
        cube(stage, f"{root}/Server_{idx:02d}", (x, y - 0.43, z), (0.78, 0.06, 0.22), mats["server"])
        cube(stage, f"{root}/LED_{idx:02d}", (x + 0.30, y - 0.47, z), (0.055, 0.02, 0.05),
             led_mat if led_mat is not None else mats["green"])


def cabinet(stage, root, pos, dims, body_mat, face_mat):
    x, y, z = pos
    cube(stage, f"{root}/Body", pos, dims, body_mat)
    cube(stage, f"{root}/Face", (x, y - dims[1] / 2 - 0.025, z), (dims[0] * 0.86, 0.04, dims[2] * 0.84), face_mat)


def agv_model(stage, root, pos, mats, defective=False, scale=1.0):
    x, y, z = pos
    s = scale
    cube(stage, f"{root}/Chassis", (x, y, z + 0.26 * s), (0.92 * s, 0.66 * s, 0.18 * s), mats["steel"])
    if defective:
        rotated_cube(stage, f"{root}/TopCover", (x + 0.16 * s, y, z + 0.44 * s), (0.76 * s, 0.54 * s, 0.13 * s), (0.0, 0.0, 13.0), mats["white"])
    else:
        cube(stage, f"{root}/TopCover", (x, y, z + 0.44 * s), (0.76 * s, 0.54 * s, 0.13 * s), mats["white"])

    wheel_positions = [(-0.50, -0.23), (-0.50, 0.23), (0.50, -0.23), (0.50, 0.23)]
    if defective:
        wheel_positions.pop()
    for wheel_idx, (wheel_x, wheel_y) in enumerate(wheel_positions):
        cylinder(stage, f"{root}/Wheel_{wheel_idx}", (x + wheel_x * s, y + wheel_y * s, z + 0.23 * s), 0.15 * s, 0.14 * s, mats["black"], rotate=(0, 90, 0))

    if not defective:
        cylinder(stage, f"{root}/Lidar", (x, y, z + 0.62 * s), 0.12 * s, 0.18 * s, mats["cyan"])
        cube(stage, f"{root}/Status", (x, y - 0.34 * s, z + 0.44 * s), (0.22 * s, 0.035 * s, 0.07 * s), mats["green"])
    else:
        cube(stage, f"{root}/Status", (x, y - 0.34 * s, z + 0.44 * s), (0.22 * s, 0.035 * s, 0.07 * s), mats["red"])


def agv_parts_kit(stage, root, pos, mats):
    x, y, z = pos
    cube(stage, f"{root}/CarrierTray", (x, y, z + 0.04), (0.82, 0.56, 0.07), mats["dark"])
    cube(stage, f"{root}/Chassis", (x - 0.16, y, z + 0.15), (0.42, 0.32, 0.10), mats["steel"])
    cube(stage, f"{root}/TopCover", (x + 0.23, y + 0.13, z + 0.15), (0.28, 0.20, 0.07), mats["white"])
    cube(stage, f"{root}/Battery", (x + 0.20, y - 0.14, z + 0.16), (0.20, 0.14, 0.09), mats["green"])
    for wheel_idx, wheel_x in enumerate((-0.15, 0.05, 0.25)):
        cylinder(stage, f"{root}/Wheel_{wheel_idx}", (x + wheel_x, y + 0.20, z + 0.16), 0.065, 0.06, mats["black"])
    cylinder(stage, f"{root}/Lidar", (x + 0.03, y - 0.18, z + 0.17), 0.06, 0.10, mats["cyan"])


def build_agv_showcase(stage, mats):
    root = "/World/AGVShowcase"
    center_x = 12.45
    cube(stage, f"{root}/Floor", (center_x, -0.45, 0.015), (4.35, 8.20, 0.03), mats["infra"])
    cube(stage, f"{root}/BoundaryBar", (10.30, -0.45, 0.08), (0.16, 8.05, 0.12), mats["yellow"])
    for post_idx, post_y in enumerate((-4.45, 3.55)):
        cube(stage, f"{root}/Fence/Post_{post_idx}", (10.25, post_y, 0.85), (0.09, 0.09, 1.70), mats["yellow"])
    cube(stage, f"{root}/Fence/Rail", (10.25, -0.45, 1.15), (0.055, 8.00, 0.08), mats["yellow"])

    cube(stage, f"{root}/Parts/Platform", (center_x, -3.05, 0.12), (3.55, 1.55, 0.20), mats["dark"])
    cube(stage, f"{root}/Parts/Chassis", (11.45, -3.05, 0.34), (0.90, 0.62, 0.16), mats["steel"])
    for wheel_idx, wheel_x in enumerate((12.25, 12.72)):
        cylinder(stage, f"{root}/Parts/Wheel_{wheel_idx}", (wheel_x, -3.05, 0.34), 0.17, 0.14, mats["black"], rotate=(0, 90, 0))
    cube(stage, f"{root}/Parts/TopCover", (13.42, -3.05, 0.34), (0.72, 0.52, 0.10), mats["white"])
    cube(stage, f"{root}/Parts/Battery", (11.75, -2.55, 0.34), (0.42, 0.28, 0.18), mats["green"])
    cylinder(stage, f"{root}/Parts/Lidar", (13.05, -2.55, 0.34), 0.13, 0.20, mats["cyan"])

    cube(stage, f"{root}/Good/Platform", (center_x, -0.25, 0.12), (3.55, 1.75, 0.20), mats["dark"])
    agv_model(stage, f"{root}/Good/CompletedAGV", (center_x, -0.25, 0.18), mats, defective=False)

    cube(stage, f"{root}/Defective/Platform", (center_x, 2.45, 0.12), (3.55, 1.75, 0.20), mats["dark"])
    agv_model(stage, f"{root}/Defective/MissingWheelMisalignedCover", (center_x, 2.45, 0.18), mats, defective=True)


# --- robot -----------------------------------------------------------------


def robot(stage, root, pos, yaw):
    """Reference the UR10e and grab the arm links so the reach can be authored.

    The asset is a flat articulation: every link is a sibling under /ur10e with
    its own world-space pose and PhysicsRevoluteJoints between them, so there is
    no nested chain to drive with a single angle. Overriding the link transforms
    directly is what is left, and a rigid swing about the shoulder is enough to
    read as reaching without needing full forward kinematics.
    """
    holder = UsdGeom.Xform.Define(stage, root)
    holder.AddTranslateOp().Set(Gf.Vec3d(*pos))
    holder.AddRotateZOp().Set(yaw)
    asset = UsdGeom.Xform.Define(stage, f"{root}/RobotAsset")
    asset.GetPrim().GetReferences().AddReference(UR10E_ASSET)
    asset.GetPrim().SetDisplayName("UR10e assembly robot")

    # The asset is a live PhysX articulation; on interactive play the physics
    # engine would own the links and freeze out the baked swing. These robots
    # are kinematic set dressing, so disable every physics hook on the instance.
    for prim in Usd.PrimRange(asset.GetPrim()):
        tname = str(prim.GetTypeName())
        if "Joint" in tname:
            prim.CreateAttribute("physics:jointEnabled",
                                 Sdf.ValueTypeNames.Bool).Set(False)
        if prim.HasAttribute("physics:rigidBodyEnabled") or "Xform" in tname:
            prim.CreateAttribute("physics:rigidBodyEnabled",
                                 Sdf.ValueTypeNames.Bool).Set(False)
    asset.GetPrim().CreateAttribute("physxArticulation:articulationEnabled",
                                    Sdf.ValueTypeNames.Bool).Set(False)

    rest = []
    for link in ARM_LINKS:
        prim = stage.GetPrimAtPath(f"{root}/RobotAsset/{link}")
        if not prim or not prim.IsValid():
            continue
        t_attr = prim.GetAttribute("xformOp:translate")
        o_attr = prim.GetAttribute("xformOp:orient")
        if not t_attr or not o_attr:
            continue
        rest.append((t_attr, t_attr.Get(), o_attr, o_attr.Get()))
    return rest


def arm_pose(rest, shoulder_deg, frame, elbow_deg=0.0):
    """Two-pivot pose in the robot's frame.

    The whole chain swings rigidly about the shoulder (parked pitch); links
    from the forearm outward additionally rotate about the elbow, so only the
    outer joints dip while the base and upper arm stay put.
    """
    rot_s = Gf.Rotation(Gf.Vec3d(0.0, 1.0, 0.0), shoulder_deg)
    pivot_s = Gf.Vec3d(*SHOULDER_PIVOT)
    rot_e = Gf.Rotation(Gf.Vec3d(0.0, 1.0, 0.0), elbow_deg)
    pivot_e = None
    if len(rest) > 2:
        pivot_e = pivot_s + rot_s.TransformDir(Gf.Vec3d(*rest[2][1]) - pivot_s)
    for i, (t_attr, t_rest, o_attr, o_rest) in enumerate(rest):
        moved = pivot_s + rot_s.TransformDir(Gf.Vec3d(*t_rest) - pivot_s)
        quat = rot_s.GetQuat()
        if i >= 2 and elbow_deg and pivot_e is not None:
            moved = pivot_e + rot_e.TransformDir(moved - pivot_e)
            quat = rot_e.GetQuat() * quat
        t_attr.Set(type(t_rest)(moved[0], moved[1], moved[2]), Usd.TimeCode(frame))
        spun = quat * Gf.Quatd(o_rest.GetReal(), Gf.Vec3d(*o_rest.GetImaginary()))
        if isinstance(o_rest, Gf.Quatf):
            spun = Gf.Quatf(spun.GetReal(), Gf.Vec3f(*spun.GetImaginary()))
        elif isinstance(o_rest, Gf.Quath):
            spun = Gf.Quath(spun.GetReal(), Gf.Vec3h(*spun.GetImaginary()))
        o_attr.Set(spun, Usd.TimeCode(frame))


def bake_robot_motion(robots, f0, assembled):
    """Bend the elbow chain down to the workpiece and back.

    A broken trajectory never bends all the way and stutters on the way.
    """
    down = ELBOW_REACH_DEG if assembled else ELBOW_REACH_DEG * 0.45
    f_in = f0 + MOVE_FRAMES + 2
    f_hold = f0 + MOVE_FRAMES + DWELL_FRAMES // 2
    f_out = f0 + CYCLE_FRAMES - 1
    for rest in robots:
        arm_pose(rest, ARM_REST_DEG, f0 + MOVE_FRAMES, 0.0)
        arm_pose(rest, ARM_REST_DEG, f_in, down)
        if assembled:
            arm_pose(rest, ARM_REST_DEG, f_hold, down)
        else:
            arm_pose(rest, ARM_REST_DEG, f_hold - 2, ELBOW_REACH_DEG * 0.15)
            arm_pose(rest, ARM_REST_DEG, f_hold, down)
        arm_pose(rest, ARM_REST_DEG, f_out, 0.0)


# --- cell ------------------------------------------------------------------


def visibility(prim):
    return UsdGeom.Imageable(prim).CreateVisibilityAttr()


def build_cell(stage, name, center_x, controller, mats, cell_mats):
    root = f"/World/Cells/{name}"
    controller_mat = {
        "PhysicalPLC": mats["plc"],
        "VanillaK8s": mats["orange"],
        "DRATSN": mats["blue"],
    }[controller]

    cube(stage, f"{root}/Floor", (center_x, -0.65, 0.012), (5.8, 8.4, 0.024), mats["cell"])
    cube(stage, f"{root}/IdentityBar", (center_x, -4.72, 0.055), (5.5, 0.28, 0.08), controller_mat)

    # Reverse flow: AGVs enter from the infrastructure side and leave away from the racks.
    cube(stage, f"{root}/Conveyor/Belt", (center_x, -0.65, 0.77), (1.05, 7.15, 0.12), mats["belt"])
    for rail_idx, rail_x in enumerate((center_x - 0.58, center_x + 0.58)):
        cube(stage, f"{root}/Conveyor/Rail_{rail_idx}", (rail_x, -0.65, 0.91), (0.08, 7.30, 0.27), mats["steel"])
    for leg_idx, leg_y in enumerate((-3.65, -2.15, -0.65, 0.85, 2.35)):
        cube(stage, f"{root}/Conveyor/Leg_{leg_idx}_L", (center_x - 0.45, leg_y, 0.36), (0.10, 0.10, 0.72), mats["steel"])
        cube(stage, f"{root}/Conveyor/Leg_{leg_idx}_R", (center_x + 0.45, leg_y, 0.36), (0.10, 0.10, 0.72), mats["steel"])

    # Flight bars travel with the belt surface, so the motion has a direction.
    bars = []
    for bar_idx in range(BAR_COUNT):
        bar_root = f"{root}/Conveyor/FlightBar_{bar_idx}"
        bar = UsdGeom.Xform.Define(stage, bar_root)
        op = bar.AddTranslateOp()
        op.Set(Gf.Vec3d(center_x, BELT_Y_IN, 0.0), Usd.TimeCode(0))
        cube(stage, f"{bar_root}/Bar", (0.0, 0.0, BELT_TOP_Z + 0.03), (1.00, 0.13, 0.07), mats["white"])
        vis = visibility(bar.GetPrim())
        vis.Set(UsdGeom.Tokens.invisible, Usd.TimeCode(0))
        bars.append({"translate": op, "vis": vis})

    # Product pool. The belt holds at most one workpiece per station, so the same
    # prims are recycled instead of authoring one set per production cycle.
    items = []
    for item_idx in range(POOL):
        item_root = f"{root}/Products/Item_{item_idx}"
        item = UsdGeom.Xform.Define(stage, item_root)
        translate = item.AddTranslateOp()
        translate.Set(Gf.Vec3d(center_x, STATIONS[0], 0.0), Usd.TimeCode(0))
        vis = visibility(item.GetPrim())
        vis.Set(UsdGeom.Tokens.invisible, Usd.TimeCode(0))

        parts = UsdGeom.Xform.Define(stage, f"{item_root}/Parts")
        agv_parts_kit(stage, f"{item_root}/Parts", (0.0, 0.0, PARTS_Z), mats)
        good = UsdGeom.Xform.Define(stage, f"{item_root}/Good")
        agv_model(stage, f"{item_root}/Good", (0.0, 0.0, AGV_Z), mats, defective=False, scale=AGV_SCALE)
        bad = UsdGeom.Xform.Define(stage, f"{item_root}/Bad")
        agv_model(stage, f"{item_root}/Bad", (0.0, 0.0, AGV_Z), mats, defective=True, scale=AGV_SCALE)
        for sub in (good, bad):
            visibility(sub.GetPrim()).Set(UsdGeom.Tokens.invisible, Usd.TimeCode(0))
        visibility(parts.GetPrim()).Set(UsdGeom.Tokens.inherited, Usd.TimeCode(0))

        items.append({
            "translate": translate,
            "vis": vis,
            "parts": visibility(parts.GetPrim()),
            "good": visibility(good.GetPrim()),
            "bad": visibility(bad.GetPrim()),
        })

    # Two opposing robots reach across the belt into one shared assembly window.
    robots = []
    for robot_idx, offset_x in enumerate((-1.15, 1.15)):
        robot_x = center_x + offset_x
        cube(stage, f"{root}/Robots/Pedestal_{robot_idx}", (robot_x, -0.85, 0.30), (0.62, 0.62, 0.60), mats["steel"])
        robots.append(robot(stage, f"{root}/Robots/UR10e_{robot_idx}",
                            (robot_x, -0.85, 0.60), ROBOT_YAWS[robot_idx]))

    # Inspection gate. The alert panel is what carries the verdict at a distance;
    # the lens alone is a few centimetres across and invisible from the overview.
    camera_y = -2.65
    alert_mat = mats[cell_mats["alert"]]
    cube(stage, f"{root}/Inspection/PostL", (center_x - 0.82, camera_y, 1.065), (0.08, 0.08, 2.13), mats["yellow"])
    cube(stage, f"{root}/Inspection/PostR", (center_x + 0.82, camera_y, 1.065), (0.08, 0.08, 2.13), mats["yellow"])
    cube(stage, f"{root}/Inspection/Beam", (center_x, camera_y, 2.08), (1.72, 0.10, 0.10), alert_mat)
    cube(stage, f"{root}/Inspection/AlertPanel", (center_x, camera_y - 0.14, 2.52), (1.30, 0.07, 0.42), alert_mat)
    cube(stage, f"{root}/Inspection/CameraBody", (center_x, camera_y, 1.92), (0.26, 0.24, 0.20), mats["black"])
    cylinder(stage, f"{root}/Inspection/Lens", (center_x, camera_y - 0.02, 1.77), 0.075, 0.10, alert_mat)

    for post_idx, (post_x, post_y) in enumerate(
        ((center_x - 2.85, -4.55), (center_x + 2.85, -4.55), (center_x - 2.85, 3.30), (center_x + 2.85, 3.30))
    ):
        cube(stage, f"{root}/Fence/Post_{post_idx}", (post_x, post_y, 0.85), (0.09, 0.09, 1.70), mats["yellow"])
    cube(stage, f"{root}/Fence/LeftRail", (center_x - 2.85, -0.62, 1.15), (0.055, 7.86, 0.08), mats["yellow"])
    cube(stage, f"{root}/Fence/RightRail", (center_x + 2.85, -0.62, 1.15), (0.055, 7.86, 0.08), mats["yellow"])
    cube(stage, f"{root}/Fence/BackRail", (center_x, 3.30, 1.15), (5.70, 0.055, 0.08), mats["yellow"])

    if controller == "PhysicalPLC":
        cabinet(stage, f"{root}/PhysicalPLC", (center_x - 2.25, 2.45, 0.82), (0.72, 0.52, 1.64),
                mats["white"], mats[cell_mats["plc"]])
        for io_idx in range(4):
            cube(stage, f"{root}/PhysicalPLC/IO_{io_idx}", (center_x - 2.25, 2.155, 0.47 + io_idx * 0.23), (0.42, 0.04, 0.11), mats["black"])

    return {"items": items, "bars": bars, "robots": robots, "center_x": center_x}


def drained(verdicts):
    """Production cycles followed by the tail cycles that clear the belt."""
    tail = [
        M.CycleVerdict(N_CYCLES + i, (N_CYCLES + i) * CYCLE_FRAMES, 0.0, True, M.P0, True)
        for i in range(DRAIN_CYCLES)
    ]
    return list(verdicts) + tail


def bar_y(index_count, bar_idx):
    return BELT_Y_IN - ((bar_idx * STATION_PITCH + index_count * STATION_PITCH) % BAR_SPAN)


def bake_cell(cell, verdicts, mats, cell_mats):
    """Turn one track's per-cycle verdicts into time samples for its cell.

    Items and flight bars advance one station per running cycle and hold still
    while the line is down, so a stopped line reads as a frozen conveyor rather
    than a gap in the product stream.
    """
    center_x = cell["center_x"]
    items, bars = cell["items"], cell["bars"]
    slot = [None] * POOL
    verdict_of = [None] * POOL
    next_item = 0
    indexes = 0

    for rest in cell["robots"]:
        arm_pose(rest, ARM_REST_DEG, 0)

    for i, bar in enumerate(bars):
        y = bar_y(0, i)
        bar["translate"].Set(Gf.Vec3d(center_x, y, 0.0), Usd.TimeCode(0))
        bar["vis"].Set(UsdGeom.Tokens.inherited if y >= BELT_Y_OUT else UsdGeom.Tokens.invisible,
                       Usd.TimeCode(0))

    for v in drained(verdicts):
        f0 = v.frame_start
        f_arrive = f0 + MOVE_FRAMES
        f_end = f0 + CYCLE_FRAMES

        if not v.running:
            for i, k in enumerate(slot):
                if k is None:
                    continue
                y = STATIONS[k]
                items[i]["translate"].Set(Gf.Vec3d(center_x, y, 0.0), Usd.TimeCode(f_arrive))
                items[i]["translate"].Set(Gf.Vec3d(center_x, y, 0.0), Usd.TimeCode(f_end))
            for i, bar in enumerate(bars):
                y = bar_y(indexes, i)
                bar["translate"].Set(Gf.Vec3d(center_x, y, 0.0), Usd.TimeCode(f_arrive))
                bar["translate"].Set(Gf.Vec3d(center_x, y, 0.0), Usd.TimeCode(f_end))
            continue

        # flight bars ride one pitch; a bar wrapping round is hidden for the cycle
        for i, bar in enumerate(bars):
            y_from, y_to = bar_y(indexes, i), bar_y(indexes + 1, i)
            on_belt = y_to >= BELT_Y_OUT and y_from >= y_to
            bar["vis"].Set(UsdGeom.Tokens.inherited if on_belt else UsdGeom.Tokens.invisible,
                           Usd.TimeCode(f0))
            bar["translate"].Set(Gf.Vec3d(center_x, y_from, 0.0), Usd.TimeCode(f0))
            bar["translate"].Set(Gf.Vec3d(center_x, y_to, 0.0), Usd.TimeCode(f_arrive))
            bar["translate"].Set(Gf.Vec3d(center_x, y_to, 0.0), Usd.TimeCode(f_end))
        indexes += 1

        for i, k in enumerate(slot):
            if k is None:
                continue
            if k + 1 >= POOL:
                slot[i] = None
                verdict_of[i] = None
                items[i]["vis"].Set(UsdGeom.Tokens.invisible, Usd.TimeCode(f0))
            else:
                slot[i] = k + 1

        entering = None
        if v.cycle < N_CYCLES:
            entering = next_item
            next_item = (next_item + 1) % POOL
            if slot[entering] is not None:
                entering = slot.index(None) if None in slot else None
        if entering is not None:
            slot[entering] = 0
            verdict_of[entering] = v
            op = items[entering]
            op["vis"].Set(UsdGeom.Tokens.inherited, Usd.TimeCode(f_arrive))
            op["parts"].Set(UsdGeom.Tokens.inherited, Usd.TimeCode(f_arrive))
            op["good"].Set(UsdGeom.Tokens.invisible, Usd.TimeCode(f_arrive))
            op["bad"].Set(UsdGeom.Tokens.invisible, Usd.TimeCode(f_arrive))

        for i, k in enumerate(slot):
            if k is None:
                continue
            y = STATIONS[k]
            items[i]["translate"].Set(Gf.Vec3d(center_x, y, 0.0), Usd.TimeCode(f_arrive))
            items[i]["translate"].Set(Gf.Vec3d(center_x, y, 0.0), Usd.TimeCode(f_end))

        at_work = next((i for i, k in enumerate(slot) if k == WORK_SLOT), None)
        if at_work is not None:
            wv = verdict_of[at_work] or v
            f_decide = f0 + DECIDE_OFFSET
            op = items[at_work]
            op["parts"].Set(UsdGeom.Tokens.invisible, Usd.TimeCode(f_decide))
            op["good" if wv.assembled else "bad"].Set(UsdGeom.Tokens.inherited, Usd.TimeCode(f_decide))
            bake_robot_motion(cell["robots"], f0, wv.assembled)

        # The gate only fires on a reject. Flashing on every good part would turn
        # the alert into background noise.
        at_gate = next((i for i, k in enumerate(slot) if k == INSPECT_SLOT), None)
        gv = verdict_of[at_gate] if at_gate is not None else None
        if gv is not None and not gv.assembled:
            mats.emissive(cell_mats["alert"], (0.0, 0.0, 0.0), f_arrive - 1)
            mats.emissive(cell_mats["alert"], (0.95, 0.05, 0.03), f_arrive + 1)
            mats.emissive(cell_mats["alert"], (0.30, 0.02, 0.01), f_arrive + 7)
            mats.emissive(cell_mats["alert"], (0.95, 0.05, 0.03), f_arrive + 11)
            mats.emissive(cell_mats["alert"], (0.0, 0.0, 0.0), f_end - 1)


def bake_rhythm(cell):
    """Live-mode base: the story-free conveyor rhythm, looping cleanly.

    Only what the live extension does NOT drive gets samples here — flight
    bars (bake_cell :529-537 mirror, every cycle) and the robot swing
    (assembled-style every cycle). Items/products/wires/packets/PLC stay
    unanimated; the dratsn.live extension owns them as session defaults.
    """
    center_x = cell["center_x"]
    bars = cell["bars"]
    for rest in cell["robots"]:
        arm_pose(rest, ARM_REST_DEG, 0)
    for i, bar in enumerate(bars):
        y = bar_y(0, i)
        bar["translate"].Set(Gf.Vec3d(center_x, y, 0.0), Usd.TimeCode(0))
        bar["vis"].Set(UsdGeom.Tokens.inherited if y >= BELT_Y_OUT
                       else UsdGeom.Tokens.invisible, Usd.TimeCode(0))
    for c in range(LIVE_CYCLES):
        f0 = c * CYCLE_FRAMES
        f_arrive = f0 + MOVE_FRAMES
        f_end = f0 + CYCLE_FRAMES
        for i, bar in enumerate(bars):
            y_from, y_to = bar_y(c, i), bar_y(c + 1, i)
            on_belt = y_to >= BELT_Y_OUT and y_from >= y_to
            bar["vis"].Set(UsdGeom.Tokens.inherited if on_belt
                           else UsdGeom.Tokens.invisible, Usd.TimeCode(f0))
            bar["translate"].Set(Gf.Vec3d(center_x, y_from, 0.0), Usd.TimeCode(f0))
            bar["translate"].Set(Gf.Vec3d(center_x, y_to, 0.0), Usd.TimeCode(f_arrive))
            bar["translate"].Set(Gf.Vec3d(center_x, y_to, 0.0), Usd.TimeCode(f_end))
        bake_robot_motion(cell["robots"], f0, True)


def bake_plc_fault(mats, cell_mats, verdicts):
    """The PLC cabinet face goes red for the whole replacement window."""
    faulted = (0.95, 0.06, 0.04)
    name = cell_mats["plc"]
    prev = None
    for v in verdicts:
        if v.running != prev:
            mats.emissive(name, (0.0, 0.0, 0.0) if v.running else faulted, v.frame_start)
            prev = v.running


# --- network path ----------------------------------------------------------


def frame_of(t_ms):
    return int(round(t_ms / M.CYCLE_REAL_MS * CYCLE_FRAMES))


def resample(points, count):
    """Even arc-length samples along a polyline, for the travelling packets."""
    pts = [Gf.Vec3d(*p) for p in points]
    seg = [(pts[i + 1] - pts[i]).GetLength() for i in range(len(pts) - 1)]
    total = sum(seg) or 1.0
    out = []
    for n in range(count):
        want = total * n / (count - 1)
        acc = 0.0
        for i, length in enumerate(seg):
            if acc + length >= want or i == len(seg) - 1:
                f = (want - acc) / length if length else 0.0
                out.append(pts[i] + (pts[i + 1] - pts[i]) * min(max(f, 0.0), 1.0))
                break
            acc += length
    return out


def build_packets(stage, root, points, mat, count=PACKETS_PER_PATH):
    """Markers that run the wire in the direction traffic flows.

    A colour change alone does not say which way anything is moving, and the
    direction is the whole point of showing the path at all.
    """
    path_pts = resample(points, 6)
    packets = []
    for idx in range(count):
        xf = UsdGeom.Xform.Define(stage, f"{root}/Packet_{idx}")
        op = xf.AddTranslateOp()
        cube(stage, f"{root}/Packet_{idx}/Body", (0.0, 0.0, 0.0), (0.17, 0.17, 0.17), mat)
        packets.append({"op": op, "vis": visibility(xf.GetPrim()), "phase": idx / count})
    return {"points": path_pts, "packets": packets}


def bake_packets(rig, live_spans):
    """One trip along the wire per index cycle, staggered between packets.

    The trip has to end before the next one starts, or the two runs share a
    timecode and the packet interpolates straight from the far end back to the
    switch, sailing across open air. It reaches the end, goes invisible, snaps
    back while hidden, and reappears at the switch.
    """
    pts = rig["points"]
    steps = len(pts) - 1
    travel = CYCLE_FRAMES - PACKET_GAP_FRAMES
    for pk in rig["packets"]:
        op, vis, phase = pk["op"], pk["vis"], pk["phase"]
        op.Set(pts[0], Usd.TimeCode(0))
        vis.Set(UsdGeom.Tokens.invisible, Usd.TimeCode(0))
        for start, end in live_spans:
            cycle0 = start // CYCLE_FRAMES
            cycle1 = min(end // CYCLE_FRAMES + 1, N_CYCLES + DRAIN_CYCLES)
            for c in range(cycle0, cycle1):
                base = round(c * CYCLE_FRAMES + phase * CYCLE_FRAMES)
                if base < start or base + CYCLE_FRAMES > end:
                    continue
                vis.Set(UsdGeom.Tokens.inherited, Usd.TimeCode(base))
                for s in range(steps + 1):
                    op.Set(pts[s], Usd.TimeCode(base + round(s * travel / steps)))
                vis.Set(UsdGeom.Tokens.invisible, Usd.TimeCode(base + travel))
                op.Set(pts[0], Usd.TimeCode(base + CYCLE_FRAMES - 1))
            vis.Set(UsdGeom.Tokens.invisible, Usd.TimeCode(end))


def bake_paths(mats, cell_mats, zone, track, pod_op, curves, packet_rigs):
    """Light the uplink of the rack the vPLC actually runs on.

    Every rack has exactly one uplink, so which rack the Pod sits on and which
    wire carries its traffic are the same choice. Two separate signals ride on
    that one wire, and conflating them would paint over the finding:

      marker position  the placement — which node the Pod landed on
      wire colour      the enforcement — blue only under GCL protection (P0)

    A Pod on the TSN node without tagging (P1) therefore sits on the TSN rack
    with an orange wire running into the blue TSN switch: the path is there, the
    traffic is not getting it. Forcing the marker onto the best-effort rack
    instead would report the wrong node, and painting its wire blue would report
    protection it does not have.

    Driven off the feed's event times rather than the production cycles: a
    reschedule takes well under one cycle, so cycle granularity cannot see it.
    """
    a_name, b_name = cell_mats["tsn"], cell_mats["besteffort"]
    tsn_base, be_base = (0.02, 0.45, 1.0), (1.0, 0.28, 0.03)
    spans = {"tsn": [], "besteffort": []}

    def wire(name, key, hue, on, glow, frame):
        mats.diffuse(name, hue if on else DEAD_WIRE, frame)
        level = BRIGHT * (1.0 + glow) if on else DIM
        mats.emissive(name, tuple(c * level for c in (hue if on else DEAD_WIRE)), frame)
        width = CABLE_W_LIVE if on else CABLE_W_IDLE
        for curve in curves[key]:
            n = len(curve.GetPointsAttr().Get())
            curve.GetWidthsAttr().Set([width] * n, Usd.TimeCode(frame))

    def emit(frame, placement, live=True, glow=0.0):
        protected = placement.cls == M.P0
        on_a = placement.on_tsn          # the TSN-capable rack
        a_on, b_on = live and on_a, live and not on_a
        hue = tsn_base if protected else be_base
        wire(a_name, "tsn", hue, a_on, glow, frame)
        wire(b_name, "besteffort", be_base, b_on, glow, frame)
        if pod_op is not None and zone in RACK_X:
            tsn_x, be_x = RACK_X[zone]
            pod_op.Set(Gf.Vec3d(tsn_x if on_a else be_x, 6.15, 2.85), Usd.TimeCode(frame))
            mats.diffuse(cell_mats["pod"], hue if live else DEAD_WIRE, frame)
            mats.emissive(cell_mats["pod"],
                          tuple(c * (0.55 if live else DIM) for c in (hue if live else DEAD_WIRE)),
                          frame)
        return a_on, b_on

    def open_span(frame, tsn_on, be_on):
        for key, on in (("tsn", tsn_on), ("besteffort", be_on)):
            if on:
                spans[key].append([frame, END_FRAME])

    def close_spans(frame):
        for key in spans:
            if spans[key] and spans[key][-1][1] == END_FRAME:
                spans[key][-1][1] = frame

    # USD interpolates between time samples, so a value written only at the two
    # ends of an interval ramps across the whole of it: a cell would fade out over
    # the 40 minutes before its fault and fade back in over the hour it is meant
    # to be dead. Every stretch needs a hold sample one frame before it ends.
    on = emit(0, track.start)
    open_span(0, *on)
    current = track.start
    for d in track.disruptions:
        f_fail = frame_of(d.t_ms)
        f_back = max(frame_of(d.t_ms + d.down_ms), f_fail + 1)
        if f_fail > 1:
            emit(f_fail - 1, current)                # hold running up to the fault
        emit(f_fail, current, live=False)
        close_spans(f_fail)
        if f_back > f_fail + 1:
            emit(f_back - 1, current, live=False)    # hold dead through the outage
        on = emit(f_back, d.resume, glow=0.9)
        emit(f_back + GLOW_FRAMES, d.resume)
        open_span(f_back, *on)
        current = d.resume
    emit(END_FRAME, current)                         # hold the final state

    for key, rig in packet_rigs.items():
        bake_packets(rig, [(a, b) for a, b in spans[key]])


def bake_pod_bob(bob_op, spin_op, marker_z=0.0):
    """A floating, slowly turning marker reads as a live Pod on that machine.

    A still box looks like a decal stuck to the rack. The bob alone was 0.075 m,
    about two pixels from the overview camera, which is not motion anyone sees.
    """
    period = 2 * CYCLE_FRAMES
    step = CYCLE_FRAMES // 6
    for frame in range(0, END_FRAME + step, step):
        phase = 2.0 * math.pi * (frame % period) / period
        bob_op.Set(Gf.Vec3d(0.0, 0.0, marker_z + POD_BOB * math.sin(phase)), Usd.TimeCode(frame))
        spin_op.Set(360.0 * frame / period, Usd.TimeCode(frame))


# --- cluster overlay (v11) --------------------------------------------------
# The abstract C/N/S resource layer above the two K8s zones (ADR-0033 후기):
# node boxes hold CPU-request block stacks (C), each node hangs a two-lane
# ribbon uplink onto its floor switch cabinet (N: protected lane = slack budget,
# BE lane = declared-bandwidth ledger fill), and an inert storage slab says S is
# assumed away. The physical-PLC zone deliberately gets nothing: the absence of
# a cloud-native stack is the contrast. Every ledger number is a request-side
# declaration (feed cluster.net_model), no traffic was measured.

OVERLAY_TRACKS = (("vanilla", "VanillaK8s"), ("dratsn", "DRATSN"))
OVERLAY_SWITCH_X = {"VanillaK8s": {"tsn": 1.05, "be": 2.55},
                    "DRATSN": {"tsn": -5.95, "be": -4.45}}
NODE_W, NODE_D, NODE_H = 1.30, 0.95, 1.05
TOWER_Y = 6.15                     # towers sit directly above their class rack
TOWER_Z0 = 3.55                    # first level base, above the 2.85 pod marker
LEVEL_H = 1.34                     # level pitch (box + gap)
RIB_LEN = 0.95                     # lane shelf reaching toward the camera (-y)
UNIT_MCPU = 1500.0                 # one blade = 1.5 cores of requests (8 per node)
UNIT_ROWS, UNIT_COLS = 8, 1        # a single column of slim blades, left of the
UNIT_W, UNIT_D, UNIT_H = 0.72, 0.62, 0.105   # v12 GPU box takes the left strip
UNIT_PITCH = 0.115
UNIT_X_OFF = -0.18                 # blades sit right of the cage centre
SEG_LEN, SEG_GAP = 0.26, 0.06      # one segment = one active TSN stream
VPLC_MCPU_DEFAULT = 2000           # v1-feed peers carry no mcpu field
BE_MAX_MS = 1.6                    # best-effort lane on the SAME m-per-ms
                                   # scale; the network deadline is the same
                                   # 1 ms as the reserved budget (the other
                                   # 1 ms of the 2 ms cycle is vPLC compute) —
                                   # the extra run just shows the overshoot
SHELF_DZ = -0.02                   # shelf rides the level's floor lip, so the
                                   # block stack above stays fully visible
OVL_SW_Z = 2.90                    # overlay switch box, right above the real one
SWITCH_TOP_Z = 0.955
RIBBON_W, RIBBON_T = 1.04, 0.10
LANE_W, LANE_GAP = 0.36, 0.05
NODE_ALLOC_MCPU = 12000.0          # kind worker allocatable (util.csv 36000/3)
STACK_MARGIN = 0.04
ARRIVE_GLOW = 3.0                  # emissive spike factor on an actor's landing

# --- v12 (ADR-0040) ---------------------------------------------------------
SCENE_VER = "v12"
ZONE_CELL_X = {"VanillaK8s": 0.0, "DRATSN": -7.0}   # the zone's tracked cell
# Device axis: feed v2 has no device block, so the fig_mixed topology is
# declared here (ADR-0039 §0 — TSN+GPU x2 are the contested nodes, GPU x2 are
# the elastic pool). A node name ending in one of these digits owns GPUs.
GPU_NODE_SUFFIX = "2345"
# ADR-0039 §9: presence, not quantity. One box left of the CPU column, about
# half the cage tall and one socket wide. Nodes without a GPU get nothing at
# all. Claim reads like a core socket — dark when free, workload colour when
# taken (§9's achromatic-body-plus-cap did not read on screen).
GPU_W, GPU_D, GPU_H = 0.24, 0.52, 0.56
GPU_X_OFF = -0.46                  # left of the blade column inside the cage
PATH_CELL_Y, PATH_CELL_Z = -2.55, 2.10   # cell-side anchor of a stream path
PATH_LEG_DX = -0.18               # keeps the TSN leg beside the camera feed
PATH_DROP = 0.22                  # the chain leaves the shelf tip downward and
                                  # crosses UNDER both lanes: the BE shelf runs
                                  # 1.6x RIB_LEN toward the camera, so a run at
                                  # shelf height passes straight through it
PATH_W_AVAIL, PATH_W_LIT = 0.042, 0.115
ALLOC_FRAMES = 36                  # opening CNC allocation beat (1.5 s @ 24fps)
# The CNC used to sit at x=0, directly behind the vanilla zone's rack column,
# where the tower above that rack hid it from the overview camera. It belongs
# in clear sight: it is the authority the whole premise rests on. Right of the
# vanilla switches, behind the infrastructure row.
CNC_X, CNC_Y, CNC_Z = 4.30, 9.60, 0.88
CNC_W, CNC_D, CNC_H = 1.15, 0.68, 1.35


# The ribbon hangs straight down from the node box, so its lanes always split
# left/right on screen (a node->switch diagonal twisted the face and stacked
# the lanes in depth); a thin trunk cable carries the fan-in to the switch.
RIB_BOT = 1.30


def _lane(stage, root, name, x, y_front, x_off, width, z, mat, length=RIB_LEN):
    """A horizontal lane shelf reaching from the box front toward the camera."""
    return cube(stage, f"{root}/{name}", (x + x_off, y_front - length / 2, z),
                (width, length, RIBBON_T), mat)


def _has_gpu(n):
    """Does this node own a GPU at all (ADR-0039 §9 — presence, not quantity).

    The feed has no device axis, so the ADR-0039 topology is declared via
    GPU_NODE_SUFFIX; a feed that grows a `gpu` field wins.
    """
    g = n.get("gpu")
    if isinstance(g, dict):
        return bool(g.get("count", 1))
    if g is not None:
        return bool(g)
    return n["name"][-1] in GPU_NODE_SUFFIX


def _path_points(x, y_tip, z, sw_x, level, cell_x):
    """The whole stream path, node port -> switch -> cell, as one polyline.

    The first half IS the trunk (same points, so the lit sleeve sits on the
    cable); the second half is what v11 never drew — the leg that makes a path
    reach an end station. A node whose trunk stops at the switch has a NIC,
    not a path, and that is the distinction the scene has to carry.
    """
    lx = cell_x + PATH_LEG_DX
    return _chain_points(x, y_tip, z, sw_x, level, True) + [
        (sw_x, TOWER_Y, OVL_SW_Z), (lx, TOWER_Y, OVL_SW_Z),
        (lx, PATH_CELL_Y, OVL_SW_Z), (lx, PATH_CELL_Y, PATH_CELL_Z)]


def _ai_pods(pods):
    """AI-tier pod count under either vocabulary (it-ai / ai-inference / ai-training)."""
    return sum(v["count"] for t, v in pods.items()
               if t.startswith("it-ai") or t.startswith("ai-"))


def _has_path(nodes, tracked_domain=None):
    """name -> does this node publish the tracked line's stream.

    MEASURED, not declared: the HA layout provisions a workload's stream on k
    of the M TSN nodes (ADR-0039 §2), so a TSN node outside that set has a NIC
    and no path. `cluster.nodes[].domains` carries what the node actually
    publishes; a feed too old to have the field falls back to "every TSN node
    has one", which is what that older experiment in fact did.
    """
    out = {}
    for n in nodes:
        if not n["tsn"]:
            out[n["name"]] = False
            continue
        doms = n.get("domains")
        if doms is None:
            out[n["name"]] = True
        elif tracked_domain:
            out[n["name"]] = tracked_domain in doms
        else:
            out[n["name"]] = bool(doms)
    return out


def _label_card(stage, path, tex, pos, width, aspect=0.16, tilt=-18.0):
    """Bake-time caption: a text PNG (scene-relative path) on a tilted quad."""
    mesh = UsdGeom.Mesh.Define(stage, path)
    h = width * aspect
    mesh.CreatePointsAttr([Gf.Vec3f(-width / 2, 0, -h / 2), Gf.Vec3f(width / 2, 0, -h / 2),
                           Gf.Vec3f(width / 2, 0, h / 2), Gf.Vec3f(-width / 2, 0, h / 2)])
    mesh.CreateFaceVertexCountsAttr([4])
    mesh.CreateFaceVertexIndicesAttr([0, 1, 2, 3])
    mesh.CreateDoubleSidedAttr(True)
    mesh.CreateExtentAttr([Gf.Vec3f(-width / 2, -0.01, -h / 2),
                           Gf.Vec3f(width / 2, 0.01, h / 2)])
    UsdGeom.PrimvarsAPI(mesh.GetPrim()).CreatePrimvar(
        "st", Sdf.ValueTypeNames.TexCoord2fArray,
        UsdGeom.Tokens.faceVarying).Set([(0, 0), (1, 0), (1, 1), (0, 1)])
    mesh.AddTranslateOp().Set(Gf.Vec3d(*pos))
    mesh.AddRotateXYZOp().Set(Gf.Vec3f(tilt, 0.0, 0.0))
    mat = UsdShade.Material.Define(stage, f"{path}/Mat")
    reader = UsdShade.Shader.Define(stage, f"{path}/Mat/StReader")
    reader.CreateIdAttr("UsdPrimvarReader_float2")
    reader.CreateInput("varname", Sdf.ValueTypeNames.Token).Set("st")
    tex_sh = UsdShade.Shader.Define(stage, f"{path}/Mat/Tex")
    tex_sh.CreateIdAttr("UsdUVTexture")
    tex_sh.CreateInput("file", Sdf.ValueTypeNames.Asset).Set(tex)
    tex_sh.CreateInput("st", Sdf.ValueTypeNames.Float2).ConnectToSource(
        reader.ConnectableAPI(), "result")
    surf = UsdShade.Shader.Define(stage, f"{path}/Mat/Surf")
    surf.CreateIdAttr("UsdPreviewSurface")
    surf.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).ConnectToSource(
        tex_sh.ConnectableAPI(), "rgb")
    surf.CreateInput("emissiveColor", Sdf.ValueTypeNames.Color3f).ConnectToSource(
        tex_sh.ConnectableAPI(), "rgb")
    surf.CreateInput("opacity", Sdf.ValueTypeNames.Float).ConnectToSource(
        tex_sh.ConnectableAPI(), "a")
    mat.CreateSurfaceOutput().ConnectToSource(surf.ConnectableAPI(), "surface")
    bind(mesh.GetPrim(), mat)


def _chain_points(x, y_tip, z, sw_x, level, is_tsn):
    """Shelf tip -> down below the lanes -> a short step out front -> across
    -> back to the switch column -> drop onto the overlay switch roof.

    Levels separate by height, the two towers by their own front street
    (y offset), and the final drops by x offset — nothing ever overlaps. The
    PATH_DROP step is what keeps the run clear of the lane shelves it would
    otherwise cut through.
    """
    y_run = y_tip - (0.16 if is_tsn else 0.34)
    zr = z - PATH_DROP
    xd = sw_x + (level - 1) * 0.24
    return [(x, y_tip, z), (x, y_tip, zr), (x, y_run, zr), (xd, y_run, zr),
            (xd, TOWER_Y, zr), (xd, TOWER_Y, OVL_SW_Z + 0.17)]


def _trunk(stage, root, x, y_tip, z, sw_x, level, is_tsn, mat):
    return cable(stage, f"{root}/Trunk",
                 _chain_points(x, y_tip, z, sw_x, level, is_tsn), mat, 0.042)


def _frame_box(stage, root, x, y, base, mat):
    """An open cage instead of a translucent shell: four posts and a top rim,
    so the block stack inside stays fully saturated and the empty headroom up
    to the rim is the visible spare capacity."""
    for i, (dx, dy) in enumerate(((-1, -1), (-1, 1), (1, -1), (1, 1))):
        cube(stage, f"{root}/Post_{i}",
             (x + dx * NODE_W / 2, y + dy * NODE_D / 2, base + NODE_H / 2),
             (0.07, 0.07, NODE_H), mat)
    cube(stage, f"{root}/RimF", (x, y - NODE_D / 2, base + NODE_H), (NODE_W, 0.06, 0.06), mat)
    cube(stage, f"{root}/RimB", (x, y + NODE_D / 2, base + NODE_H), (NODE_W, 0.06, 0.06), mat)
    cube(stage, f"{root}/RimL", (x - NODE_W / 2, y, base + NODE_H), (0.06, NODE_D, 0.06), mat)
    cube(stage, f"{root}/RimR", (x + NODE_W / 2, y, base + NODE_H), (0.06, NODE_D, 0.06), mat)


def _tier_mcpu(pods, tier_prefix):
    return sum(v["mcpu"] for t, v in pods.items() if t.startswith(tier_prefix))


def _stack_h(mcpu):
    return max(mcpu / NODE_ALLOC_MCPU * (NODE_H * 0.94), 0.02)


def overlay_zone(stage, zone, track, cluster, arm_net, verdicts):
    """Build and bake one zone's cluster layer (geometry + choreography)."""
    root = f"/World/ClusterOverlay/{zone}"
    mats = Mats(stage)

    def zmat(name, color, **kw):
        full = f"Ovl{zone}_{name}"
        mats.add(full, color, **kw)
        return full

    tsn_blue = (0.02, 0.45, 1.0)
    be_orange = (1.0, 0.28, 0.03)
    c_vplc = tsn_blue              # OT vPLC rides the protected class colour
    # AI workload colour. Violet, not orange: orange is the best-effort
    # NETWORK, and an AI pod's CPU blades and the GPU it claims are the same
    # workload — one colour for both, or the device column reads as unrelated.
    ai_violet = (0.58, 0.22, 0.86)
    c_ai = ai_violet
    c_log = (0.42, 0.44, 0.48)     # droppable bulk: logs + AI training
    dead = DEAD_WIRE

    m_shell = zmat("Shell", (0.66, 0.73, 0.84), roughness=0.5)
    m_shell_dead = zmat("ShellDead", (0.66, 0.73, 0.84), roughness=0.5)
    m_plat_tsn = zmat("PlatTSN", tsn_blue, metallic=0.25, roughness=0.3)
    m_plat_gen = zmat("PlatGen", be_orange, metallic=0.25, roughness=0.3)
    m_plat_dead = zmat("PlatDead", tsn_blue, metallic=0.25, roughness=0.3)
    m_ai = zmat("BlockAI", c_ai, roughness=0.35,
                emissive=tuple(c * 0.10 for c in c_ai))
    m_log = zmat("BlockLog", c_log, roughness=0.45)
    m_ai_dead = zmat("BlockAIDead", c_ai, roughness=0.35)
    m_lane_prot = zmat("LaneProt", tsn_blue, roughness=0.35, opacity=0.30)
    m_fill_prot = zmat("FillProt", tsn_blue, roughness=0.25,
                       emissive=tuple(c * 0.35 for c in tsn_blue))
    m_lane_be = zmat("LaneBE", be_orange, roughness=0.4, opacity=0.26)
    m_lane_dead = zmat("LaneDead", dead, roughness=0.5, opacity=0.4)
    m_lane_dead_prot = zmat("LaneDeadProt", dead, roughness=0.5, opacity=0.30)
    m_cap_dead = zmat("CapDead", dead, roughness=0.3)
    m_empty = zmat("UnitEmpty", (0.055, 0.062, 0.075), roughness=0.7)
    m_tick = zmat("AccTick", (0.92, 0.96, 1.0), roughness=0.2,
                  emissive=(0.35, 0.38, 0.42))
    m_grad = zmat("Graduation", (0.75, 0.82, 0.92), roughness=0.4, opacity=0.55)
    m_used = zmat("BudgetUsed", (0.02, 0.13, 0.34), roughness=0.4)
    m_slackrun = zmat("BudgetSlack", (0.30, 0.62, 1.0), roughness=0.3)
    m_cap = zmat("BudgetLimit", (0.90, 0.10, 0.08), roughness=0.3,
                 emissive=(0.22, 0.02, 0.02))
    m_fill_dead_prot = zmat("FillDeadProt", dead, roughness=0.5)
    m_bind = zmat("Bind", (0.75, 0.78, 0.82), roughness=0.5)
    # v12: three path states. Path = configured and reaching the cell (the CNC
    # premise, present in BOTH zones — the underlay is the same network);
    # PathLit = a stream is riding it right now (vanilla never lights one, it
    # never tags); NoPath = a TSN NIC with no configured path to this cell.
    # One hue for the path, two weights: a configured path and a path in use
    # are the SAME road, so the difference is width and glow, never colour.
    # Grey is reserved for its own statement — no path at all.
    path_blue = tsn_blue
    m_path = zmat("Path", path_blue, roughness=0.3,
                  emissive=tuple(c * 0.18 for c in tsn_blue))
    m_path_dead = zmat("PathDead", path_blue, roughness=0.3)
    m_path_lit = zmat("PathLit", tsn_blue, roughness=0.2,
                      emissive=tuple(c * 0.70 for c in tsn_blue))
    m_nopath = zmat("NoPath", (0.30, 0.33, 0.38), roughness=0.55)
    # trunks are cables, not shelves: opaque, or the BE orange washes out to
    # the same grey as "no path" and the two statements collide
    m_trunk_be = zmat("TrunkBE", be_orange, roughness=0.4)
    m_trunk_dead = zmat("TrunkDead", be_orange, roughness=0.45)
    m_gpu = zmat("Gpu", ai_violet, roughness=0.3,
                 emissive=tuple(c * 0.18 for c in ai_violet))
    m_gpu_dead = zmat("GpuDead", ai_violet, roughness=0.3)

    nodes = cluster["nodes"]
    fault = cluster["fault_node"]
    cap = float(cluster["net"]["capacity_mbps"])
    sw = OVERLAY_SWITCH_X[zone]
    has_dra = zone == "DRATSN"
    cell_x = ZONE_CELL_X[zone]
    has_path = _has_path(nodes, cluster.get("tracked_domain"))

    d = track.disruptions[0]
    st = d.stages or {}
    f_fault = frame_of(d.t_ms)
    f_detect = frame_of(d.t_ms + st.get("detect_ms", 0.0))
    f_ready = max(frame_of(d.t_ms + d.down_ms), f_detect + 1)

    # a TSN node the run actually used had better publish the stream; if the
    # feed says otherwise the two halves of the data disagree. A general node
    # without a path is not a contradiction, it is the point.
    tsn_named = {n["name"] for n in nodes if n["tsn"]}
    for used in (track.start.node, d.resume.node):
        if used in tsn_named and not has_path[used]:
            print(f"  overlay[{zone}]: WARNING {used} carries a measured "
                  f"placement but publishes no tracked stream")

    # --- geometry: node towers stacked above the class racks ----------------
    # Rack A carries the TSN nodes, rack B the general ones (ADR-0033 §6: the
    # rack is the class, the tower level is the node), so placement reads
    # vertically: the pod marker on the rack sits directly under its tower.
    rack_tsn_x, rack_be_x = RACK_X[zone]
    pos = {}     # name -> (x, y, level, base_z)
    for is_tsn, rack_x in ((True, rack_tsn_x), (False, rack_be_x)):
        for i, n in enumerate([n for n in nodes if n["tsn"] == is_tsn]):
            pos[n["name"]] = (rack_x, TOWER_Y, i, TOWER_Z0 + i * LEVEL_H)

    # actors: the two vPLCs that leave the dead node, plus each preemption victim
    actors = []
    vplc_pods = {t: v for t, v in
                 arm_net.get(fault, {}).get("pre_pods", {}).items()
                 if t.startswith("vplc")}
    n_vplc = sum(v["count"] for v in vplc_pods.values()) or 1
    u_vplc = max(1, round(_tier_mcpu(arm_net.get(fault, {}).get("pre_pods", {}), "vplc")
                          / n_vplc / UNIT_MCPU))
    actors.append({"name": "Tracked", "base": c_vplc,
                   "mat": zmat("ActTracked", c_vplc, roughness=0.25,
                               emissive=tuple(c * 0.18 for c in c_vplc)),
                   "u": u_vplc, "src": track.start.node, "dst": d.resume.node,
                   "m0": f_detect, "m1": f_ready, "stale": True})
    if d.peer:
        f_peer = max(frame_of(d.t_ms + float(d.peer["down_ms"])), f_detect + 1)
        u_peer = max(1, round(d.peer.get("mcpu", VPLC_MCPU_DEFAULT) / UNIT_MCPU))
        actors.append({"name": "OtherLoad", "base": c_log,
                       "mat": zmat("ActOther", c_log, roughness=0.4),
                       "u": u_peer, "src": d.peer["start"]["node"],
                       "dst": d.peer["resume"]["node"],
                       "m0": f_detect, "m1": f_peer, "stale": True})
    for i, ev in enumerate(d.evictions or []):
        if "resched_node" not in ev:
            continue
        ai = arm_net.get(ev["node"], {}).get("pre_pods", {}).get("it-ai")
        u_ai = max(1, round(ai["mcpu"] / ai["count"] / UNIT_MCPU)) if ai else 2
        f_ev = max(frame_of(d.t_ms + float(st.get("evict_ms", 0.0))), f_detect)
        f_land = max(frame_of(d.t_ms + float(ev["ai_resched_ms"])), f_ev + 1)
        actors.append({"name": f"Victim_{i}", "base": c_ai,
                       "mat": zmat(f"ActVictim{i}", c_ai, roughness=0.35),
                       "u": u_ai, "src": ev["node"], "dst": ev["resched_node"],
                       "m0": f_ev, "m1": f_land, "stale": False})

    # arrivals that will occupy a protected-lane stream segment, per node
    seg_arrivals = {}
    for a in actors:
        if a["name"] == "Tracked":
            seg_arrivals.setdefault(a["dst"], []).append(a["m1"])

    # static stacks: the pre-fault inventory minus every pod an actor represents
    units_used = {}
    lane_anchor = {}
    for n in nodes:
        name = n["name"]
        x, y, level, base = pos[name]
        nroot = f"{root}/Nodes/{name.replace('-', '_')}"
        is_fault = name == fault
        net = arm_net.get(name, {})
        pods = {t: dict(v) for t, v in net.get("pre_pods", {}).items()}
        if is_fault:
            for t in list(pods):
                if t.startswith("vplc"):
                    del pods[t]
            # the peer (fixture v2: the camfeed gateway) is drawn as an actor,
            # so its share leaves the static stack too
            pt = (d.peer or {}).get("tier")
            if pt and pt in pods:
                pods[pt]["mcpu"] = max(0, pods[pt]["mcpu"]
                                       - int(d.peer.get("mcpu", 0)))
                pods[pt]["count"] = max(0, pods[pt]["count"] - 1)
        for a in actors:
            if not a["name"].startswith("Victim") or a["src"] != name:
                continue
            ai = pods.get("it-ai")
            if ai and ai["count"]:
                ai["mcpu"] -= ai["mcpu"] // ai["count"]
                ai["count"] -= 1

        # platform width < the 1.35 m rack-column gap (RACK_X), or adjacent
        # TSN/general platforms interpenetrate
        cube(stage, f"{nroot}/Platform", (x, y, base - 0.08),
             (NODE_W - 0.06, NODE_D + 0.24, 0.10),
             mats[m_plat_dead if is_fault else (m_plat_tsn if n["tsn"] else m_plat_gen)])
        # CPU as core units: block colour = workload tier (gold vPLC, violet
        # AI, slate historian), one block = ~one core of requests; dark
        # sockets = free cores, so the 90% regime is literally 11/12 filled
        # two vocabularies live here: fig_regime's PriorityClass names and
        # fig_mixed's workload names (ADR-0039 §1). Both map onto the same two
        # colours — slate for droppable bulk and the control plane's own
        # requests, AI orange for the GPU-hungry inference/training tiers.
        tiers = (("it-historian", m_log, m_log), ("ot-monitoring", m_log, m_log),
                 ("logging", m_log, m_log), ("vplc", m_log, m_log),
                 ("it-ai", m_ai, m_ai_dead), ("ai-", m_ai, m_ai_dead))
        exact = [max(_tier_mcpu(pods, t), 0) / UNIT_MCPU for t, _l, _d in tiers]
        counts = [int(v) for v in exact]
        want = round(sum(exact))
        for _ in range(want - sum(counts)):
            i = max(range(len(exact)), key=lambda k: exact[k] - counts[k])
            counts[i] += 1
        core_state = []
        for (tier, mat_live, mat_dead), cnt in zip(tiers, counts):
            core_state += [mat_dead if is_fault else mat_live] * cnt
        core_state = core_state[: UNIT_ROWS * UNIT_COLS]
        used = len(core_state)
        for i in range(UNIT_ROWS * UNIT_COLS):
            col, row = divmod(i, UNIT_ROWS)
            filled = i < used
            cube(stage, f"{nroot}/Cores/Core_{i}",
                 (x + (col * 2 - 1) * UNIT_X_OFF, y, base + 0.10 + row * UNIT_PITCH),
                 (UNIT_W, UNIT_D, UNIT_H),
                 mats[core_state[i]] if filled else mats[m_empty])
        units_used[name] = used

        # the device axis (ADR-0039 §9): one achromatic box left of the blades,
        # capped in the workload colour when claimed. Presence, not quantity —
        # the same "special resource = structure, not gauge" grammar as the
        # protected lane, so only the contested TSN+GPU nodes carry both.
        if _has_gpu(n):
            claimed = _ai_pods(pods) > 0
            cube(stage, f"{nroot}/Gpu/Box",
                 (x + GPU_X_OFF, y, base + 0.10 + GPU_H / 2),
                 (GPU_W, GPU_D, GPU_H),
                 mats[(m_gpu_dead if is_fault else m_gpu) if claimed
                      else m_empty])

        # uplink: a horizontal lane shelf reaching toward the camera from the
        # level's box front, then one straight trunk to the elevated overlay
        # switch — the shelf never occludes the block stack behind it
        rib_root = f"{nroot}/Ribbon"
        UsdGeom.Xform.Define(stage, rib_root)
        sw_x = sw["tsn" if n["tsn"] else "be"]
        y_front = y - NODE_D / 2 - 0.06
        z_shelf = base + SHELF_DZ
        # v12: the trunk now states the path premise. A TSN node with a
        # configured path to the cell gets the path colour in BOTH zones (the
        # underlay does not care which scheduler runs); a TSN node without one
        # gets grey, and no chain is drawn for it at all.
        if n["tsn"]:
            trunk_mat = mats[m_path_dead if is_fault else
                             (m_path if has_path[name] else m_nopath)]
        else:
            trunk_mat = mats[m_trunk_dead if is_fault else m_trunk_be]
        y_tip = y_front - RIB_LEN + 0.03
        _trunk(stage, rib_root, x, y_tip, z_shelf, sw_x, level, n["tsn"],
               trunk_mat)
        # the lit chain: trunk + the leg that reaches the cell, as one sleeve
        # over the cable, visible only while this node actually carries the
        # stream. vanilla never tags, so vanilla never lights a chain.
        # One prim per node, all of its windows on the single visibility attr —
        # live mode then only has to flip the chain of whichever node the vPLC
        # is on, and the prim is already there on every path-having node.
        if has_dra and n["tsn"] and has_path[name]:
            windows = []
            if name == track.start.node:
                windows.append((0, f_fault))
            if name == d.resume.node:
                windows.append((f_ready, None))
            pts = _path_points(x, y_tip, z_shelf, sw["tsn"], level, cell_x)
            lit = cable(stage, f"{rib_root}/PathLit", pts,
                        mats[m_path_lit], PATH_W_LIT)
            vis = visibility(lit.GetPrim())
            vis.Set(UsdGeom.Tokens.inherited if windows and windows[0][0] == 0
                    else UsdGeom.Tokens.invisible, Usd.TimeCode(0))
            for f_on, f_off in windows:
                if f_on:
                    vis.Set(UsdGeom.Tokens.invisible, Usd.TimeCode(f_on - 1))
                    vis.Set(UsdGeom.Tokens.inherited, Usd.TimeCode(f_on))
                if f_off is not None:
                    vis.Set(UsdGeom.Tokens.inherited, Usd.TimeCode(f_off - 1))
                    vis.Set(UsdGeom.Tokens.invisible, Usd.TimeCode(f_off))
        if n["tsn"]:
            _lane(stage, rib_root, "LaneProtected", x, y_front, -(LANE_W + LANE_GAP) / 2,
                  LANE_W, z_shelf, mats[m_lane_dead_prot if is_fault else m_lane_prot])
            # the protected queue only ever holds TSN streams: one bright
            # segment per active vPLC stream, nothing else may fill it
            x_prot = x - (LANE_W + LANE_GAP) / 2
            # one line, one vPLC, one stream: only the tracked controller ever
            # occupies the protected queue (the scene follows a single line)
            pre_vplc = 1 if has_dra and name == track.start.node else 0
            seg_idx = 0
            for i in range(pre_vplc):
                seg = cube(stage, f"{rib_root}/Stream_{seg_idx}",
                           (x_prot, y_front - 0.18 - seg_idx * (SEG_LEN + SEG_GAP),
                            z_shelf + RIBBON_T / 2 + 0.015),
                           (LANE_W * 0.78, SEG_LEN, 0.03),
                           mats[m_fill_dead_prot if is_fault else m_fill_prot])
                vis = visibility(seg)
                vis.Set(UsdGeom.Tokens.inherited, Usd.TimeCode(0))
                if is_fault:
                    vis.Set(UsdGeom.Tokens.inherited, Usd.TimeCode(f_fault - 1))
                    vis.Set(UsdGeom.Tokens.invisible, Usd.TimeCode(f_fault))
                seg_idx += 1
            for m1 in seg_arrivals.get(name, []):
                seg = cube(stage, f"{rib_root}/Stream_{seg_idx}",
                           (x_prot, y_front - 0.18 - seg_idx * (SEG_LEN + SEG_GAP),
                            z_shelf + RIBBON_T / 2 + 0.015),
                           (LANE_W * 0.78, SEG_LEN, 0.03), mats[m_fill_prot])
                vis = visibility(seg)
                vis.Set(UsdGeom.Tokens.invisible, Usd.TimeCode(0))
                vis.Set(UsdGeom.Tokens.invisible, Usd.TimeCode(m1 - 1))
                vis.Set(UsdGeom.Tokens.inherited, Usd.TimeCode(m1))
                seg_idx += 1
            # two-tone budget lane (bullet-graph idiom): dark run from the box
            # = the path delay this node already spends, bright run = the slack
            # the Score compares, red cap = the 1 ms limit. The best-effort
            # lane deliberately carries no scale at all: no reservation, no
            # budget — the absence is the statement.
            cube(stage, f"{rib_root}/BudgetLimit",
                 (x_prot, y_front - RIB_LEN + 0.028,
                  z_shelf + RIBBON_T / 2 + 0.02),
                 (LANE_W * 0.95, 0.055, 0.05),
                 mats[m_cap_dead if is_fault else m_cap])
            _lane(stage, rib_root, "LaneBE", x, y_front, (LANE_W + LANE_GAP) / 2, LANE_W,
                  z_shelf, mats[m_lane_dead if is_fault else m_lane_be],
                  length=BE_MAX_MS * RIB_LEN)
            cube(stage, f"{rib_root}/Deadline",
                 (x + (LANE_W + LANE_GAP) / 2, y_front - 1.0 * RIB_LEN,
                  z_shelf + RIBBON_T / 2 + 0.02),
                 (LANE_W * 0.95, 0.055, 0.05),
                 mats[m_cap_dead if is_fault else m_cap])
            lane_anchor[name] = (x, y_front, z_shelf)
        else:
            _lane(stage, rib_root, "LaneBE", x, y_front, 0.0, RIBBON_W * 0.84,
                  z_shelf, mats[m_lane_dead if is_fault else m_lane_be],
                  length=BE_MAX_MS * RIB_LEN)
            cube(stage, f"{rib_root}/Deadline",
                 (x, y_front - 1.0 * RIB_LEN,
                  z_shelf + RIBBON_T / 2 + 0.02),
                 (RIBBON_W * 0.88, 0.055, 0.05),
                 mats[m_cap_dead if is_fault else m_cap])
            lane_anchor[name] = (x, y_front, z_shelf)

    # caption cards: the ruler compares, the caption names the unit
    mid_x = (rack_tsn_x + rack_be_x) / 2
    _label_card(stage, f"{root}/Labels/CPU", "labels/label_cpu.png",
                (mid_x, TOWER_Y + 0.2, TOWER_Z0 + 3 * LEVEL_H + 0.10), 1.7,
                aspect=0.2103)
    _label_card(stage, f"{root}/Labels/TSN", "labels/label_tsn.png",
                (mid_x, 3.30, 3.30), 4.4, aspect=0.0706)

    # the TSN leg: switch -> cell, the half of the path v11 never drew. Dim by
    # default — configured and idle — with the lit chains riding over it. Its
    # presence next to the camera feed is the "one wire, two planes" statement.
    lx = cell_x + PATH_LEG_DX
    cable(stage, f"{root}/PathLeg",
          [(sw["tsn"], TOWER_Y, OVL_SW_Z), (lx, TOWER_Y, OVL_SW_Z),
           (lx, PATH_CELL_Y, OVL_SW_Z), (lx, PATH_CELL_Y, PATH_CELL_Z)],
          mats[m_path], PATH_W_AVAIL)

    # 5) the AI-inference feed: the cell's inspection camera streams frames
    # over the best-effort network — that is what the orange AI blocks consume
    m_feed = zmat("CamFeed", be_orange, roughness=0.4,
                  emissive=tuple(c * 0.10 for c in be_orange))
    cable(stage, f"{root}/InspectionFeed",
          [(cell_x, -2.55, 2.10), (cell_x, -2.55, OVL_SW_Z),
           (cell_x, 6.15, OVL_SW_Z), (sw["be"], 6.15, OVL_SW_Z)],
          mats[m_feed], 0.03)

    # elevated overlay switches: the network plane's copy of the two cabinets,
    # vertically aligned with the real switch below (thin guide line = the
    # correspondence); trunks from every shelf converge here
    for kind, hue in (("tsn", tsn_blue), ("be", be_orange)):
        m_sw = zmat(f"OvlSwitch_{kind}", hue, metallic=0.25, roughness=0.3)
        cube(stage, f"{root}/Switches/{kind.upper()}", (sw[kind], 6.15, OVL_SW_Z),
             (0.95, 0.55, 0.34), mats[m_sw])
        cable(stage, f"{root}/Switches/{kind.upper()}_Guide",
              [(sw[kind], 6.15, OVL_SW_Z - 0.17), (sw[kind], 6.15, SWITCH_TOP_Z)],
              mats[m_sw], 0.022)

    # --- choreography -------------------------------------------------------
    # unit-slot allocation: actors were part of their source node's pre stack,
    # victims free their cores, and the vPLC that preempted them lands in
    # exactly the freed slots
    cursor_units = dict(units_used)

    def take(node, u_n):
        slot = cursor_units[node]
        if slot % UNIT_ROWS + u_n > UNIT_ROWS:
            slot = (slot // UNIT_ROWS + 1) * UNIT_ROWS
        cursor_units[node] = slot + u_n
        return slot

    def unit_block(node, slot, u_n):
        x, y, _level, base = pos[node]
        col, row = divmod(slot, UNIT_ROWS)
        h = (u_n - 1) * UNIT_PITCH + UNIT_H
        return Gf.Vec3d(x + (col * 2 - 1) * UNIT_X_OFF, y,
                        base + 0.10 + (row + (u_n - 1) / 2) * UNIT_PITCH), h

    for a in actors:
        a["slot0"] = take(a["src"], a["u"])
    freed = {}
    for a in actors:
        if a["name"].startswith("Victim"):
            freed.setdefault(a["src"], []).append(a["slot0"])
    for a in sorted(actors, key=lambda a: a["m1"]):
        pool = freed.get(a["dst"])
        if pool and not a["name"].startswith("Victim"):
            a["slot1"] = pool.pop(0)
        else:
            a["slot1"] = take(a["dst"], a["u"])

    for a in actors:
        aroot = f"{root}/Actors/{a['name']}"
        p0, h = unit_block(a["src"], a["slot0"], a["u"])
        p1, _ = unit_block(a["dst"], a["slot1"], a["u"])
        obj = UsdGeom.Cube.Define(stage, aroot)
        obj.CreateSizeAttr(1.0)
        t_op = obj.AddTranslateOp()
        # slightly larger than the socket cubes it travels over, so the
        # coincident empty-socket faces stay strictly inside (no z-fighting)
        obj.AddScaleOp().Set(Gf.Vec3d(UNIT_W * 1.04, UNIT_D * 1.04, h + 0.012))
        bind(obj.GetPrim(), mats[a["mat"]])
        t_op.Set(p0, Usd.TimeCode(0))
        t_op.Set(p0, Usd.TimeCode(a["m0"]))          # hold until the move starts
        t_op.Set(p1, Usd.TimeCode(a["m1"]))
        a["p0"], a["p1"], a["t_op"] = p0, p1, t_op

        # the transition is authored at the true frames; the glow is the
        # readability device, same contract as the wire afterglow
        base = a["base"]
        mats.emissive(a["mat"], (0.0, 0.0, 0.0), 0)
        if a["stale"]:
            mats.diffuse(a["mat"], base, f_fault - 1)
            mats.diffuse(a["mat"], DEAD_WIRE, f_fault)     # stale on a dead node
            mats.diffuse(a["mat"], DEAD_WIRE, a["m0"] - 1)
            mats.diffuse(a["mat"], base, a["m0"])          # picked up by the scheduler
        mats.emissive(a["mat"], (0.0, 0.0, 0.0), a["m1"] - 1)
        mats.emissive(a["mat"], tuple(c * ARRIVE_GLOW for c in base), a["m1"])
        mats.emissive(a["mat"], (0.0, 0.0, 0.0), a["m1"] + GLOW_FRAMES)

    # opening beat: CNC allocation. Before anything moves, the configured
    # paths light up once and settle — the premise ("the roads were laid
    # offline, by the CNC, and not every node is on one") is on screen before
    # the fault is. Both zones get it: allocation is not arm-specific.
    for m, hue in ((m_path, tuple(c * 0.85 for c in tsn_blue)),
                   (m_path_lit, tuple(c * 1.4 for c in tsn_blue))):
        mats.emissive(m, (0.0, 0.0, 0.0), 0)
        mats.emissive(m, hue, ALLOC_FRAMES // 3)
        mats.emissive(m, tuple(c * 0.12 for c in tsn_blue), ALLOC_FRAMES)
    mats.emissive(m_path_lit, tuple(c * 0.55 for c in tsn_blue),
                  ALLOC_FRAMES + 2)

    # detect pulse: the Filter moment. dratsn narrows to surviving TSN
    # platforms; vanilla considers every node — blindness as a visual
    pulse = [(m_plat_tsn, tuple(c * 1.6 for c in tsn_blue))]
    if not has_dra:
        pulse.append((m_plat_gen, tuple(c * 1.6 for c in be_orange)))
    for m, hue in pulse:
        mats.emissive(m, (0.0, 0.0, 0.0), f_detect - 1)
        mats.emissive(m, hue, f_detect + 1)
        mats.emissive(m, (0.0, 0.0, 0.0), f_detect + 6)

    # Score moment: the surviving protected lanes' translucent slack glows
    if has_dra:
        f_sched = max(frame_of(d.t_ms + st.get("sched_ms", 0.0)), f_detect)
        mats.emissive(m_lane_prot, (0.0, 0.0, 0.0), f_sched - 1)
        mats.emissive(m_lane_prot, tuple(c * 1.2 for c in tsn_blue), f_sched + 1)
        mats.emissive(m_lane_prot, (0.0, 0.0, 0.0), f_sched + 6)

    # the dead node: shell, platform, stack, ribbon all lose their colour at
    # the fault frame — same "stopped kills the hue" rule as the wires
    for name, base_c in ((m_shell_dead, (0.60, 0.68, 0.80)),
                         (m_plat_dead, tsn_blue),
                         (m_ai_dead, c_ai),
                         (m_lane_dead, be_orange),
                         (m_lane_dead_prot, tsn_blue),
                         (m_fill_dead_prot, tsn_blue),
                         (m_path_dead, path_blue),
                         (m_trunk_dead, be_orange),
                         (m_gpu_dead, ai_violet),
                         (m_cap_dead, (0.90, 0.10, 0.08))):
        mats.diffuse(name, base_c, f_fault - 1)
        mats.diffuse(name, DEAD_WIRE, f_fault)

    # live delay gauge: the same declared lognormal that drives the floor
    # verdicts (ADR-0036), resampled for display on the lane the vPLC actually
    # rides. dratsn bobs inside the reserved 1 ms bound on the protected lane;
    # vanilla rides the best-effort lane (0..3 ms scale) and flashes red every
    # time a draw crosses the 2 ms cycle deadline. Same model as the defects,
    # independent draws — a display, not a measurement (caption carries this).
    m_gauge = zmat("Gauge", (0.95, 0.90, 0.35), roughness=0.25,
                   emissive=(0.45, 0.40, 0.10))
    gp = UsdGeom.Cube.Define(stage, f"{root}/DelayGauge")
    gp.CreateSizeAttr(1.0)
    g_t = gp.AddTranslateOp()
    g_s = gp.AddScaleOp()
    bind(gp.GetPrim(), mats[m_gauge])
    g_vis = visibility(gp.GetPrim())
    g_vis.Set(UsdGeom.Tokens.inherited, Usd.TimeCode(0))
    rng = random.Random(97 if has_dra else 53)
    tsn_names = {nn["name"] for nn in nodes if nn["tsn"]}
    hidden = False
    for f in range(0, END_FRAME + 1, 4):
        if f_fault <= f < f_ready:
            if not hidden:
                g_vis.Set(UsdGeom.Tokens.invisible, Usd.TimeCode(f))
                hidden = True
            rng.random()  # keep the draw sequence deterministic across states
            continue
        node = track.start.node if f < f_fault else d.resume.node
        ax, ay_front, az_shelf = lane_anchor[node]
        if has_dra:
            delay, scale = rng.uniform(0.08, 0.92), 1.0
            x_off, w, hot = -(LANE_W + LANE_GAP) / 2, LANE_W * 0.55, False
            # both lanes share one m-per-ms scale; only the lane lengths differ
        else:
            delay = min(rng.lognormvariate(math.log(ARGS.lam), M.SIGMA),
                        BE_MAX_MS - 0.05)
            ci = f // CYCLE_FRAMES
            ph = f % CYCLE_FRAMES
            v = verdicts[ci] if ci < len(verdicts) else None
            if v is not None and v.running and not v.assembled and \
                    MOVE_FRAMES <= ph <= MOVE_FRAMES + DWELL_FRAMES - 2:
                # the cycle that fails on the floor fails here first: a
                # sustained run past the deadline through its dwell
                delay = max(delay, 1.06 + 0.45 * rng.random())
            else:
                delay = min(delay, 0.94)
            scale = 1.0
            on_tsn_node = node in tsn_names
            x_off = (LANE_W + LANE_GAP) / 2 if on_tsn_node else 0.0
            w = (LANE_W if on_tsn_node else RIBBON_W * 0.84) * 0.55
            hot = delay > 1.0
        ln = max(delay / scale, 0.03) * RIB_LEN
        if hidden:
            g_vis.Set(UsdGeom.Tokens.inherited, Usd.TimeCode(f))
            hidden = False
        g_t.Set(Gf.Vec3d(ax + x_off, ay_front - ln / 2,
                         az_shelf + RIBBON_T / 2 + 0.045), Usd.TimeCode(f))
        g_s.Set(Gf.Vec3d(w, ln, 0.05), Usd.TimeCode(f))
        mats.diffuse(m_gauge, (0.95, 0.15, 0.10) if hot else (0.95, 0.90, 0.35), f)
        mats.emissive(m_gauge, (0.90, 0.08, 0.05) if hot else (0.45, 0.40, 0.10), f)

    # vertical binding: the tracked vPLC block to the zone's rack pod marker
    if zone in RACK_X:
        tsn_x, be_x = RACK_X[zone]
        tracked = actors[0]
        mk0 = Gf.Vec3d(tsn_x if track.start.on_tsn else be_x, 6.15, 2.85)
        mk1 = Gf.Vec3d(tsn_x if d.resume.on_tsn else be_x, 6.15, 2.85)
        curve = cable(stage, f"{root}/Binding", [tuple(tracked["p0"]), tuple(mk0)],
                      mats[m_bind], 0.085)
        pts = curve.GetPointsAttr()
        pts.Set([Gf.Vec3f(tracked["p0"]), Gf.Vec3f(mk0)], Usd.TimeCode(0))
        pts.Set([Gf.Vec3f(tracked["p0"]), Gf.Vec3f(mk0)], Usd.TimeCode(tracked["m0"]))
        pts.Set([Gf.Vec3f(tracked["p1"]), Gf.Vec3f(mk1)], Usd.TimeCode(tracked["m1"]))


def build_cluster_overlay(stage, feed, runs):
    cluster = getattr(feed, "cluster", None)
    if not cluster or "net" not in cluster:
        print("  overlay: feed has no v2 cluster block, skipping")
        return
    track_of = {t.name: t for t in feed.tracks}
    for track_name, zone in OVERLAY_TRACKS:
        track = track_of.get(track_name)
        arm_net = cluster["net"]["by_arm"].get(track_name, {})
        if track is None or not track.disruptions or not arm_net:
            print(f"  overlay: no data for {track_name}, skipping zone {zone}")
            continue
        overlay_zone(stage, zone, track, cluster, arm_net, runs.get(track_name, []))

    # the overlay's own framing: an oblique view (the near-top Overview
    # collapses a tower's levels onto one screen spot); the Angled camera's
    # elevation, panned up so the towers and their shelves clear the top edge.
    # The base cameras stay untouched.
    cam = UsdGeom.Camera.Define(stage, "/World/Cameras/OverviewCluster")
    view = Gf.Matrix4d(1.0)
    view.SetLookAt(Gf.Vec3d(2.30, -28.5, 20.5), Gf.Vec3d(2.30, 1.60, 2.90),
                   Gf.Vec3d(0.0, 0.0, 1.0))
    cam.AddTransformOp().Set(view.GetInverse())
    cam.CreateFocalLengthAttr(30.0)
    cam.CreateClippingRangeAttr(Gf.Vec2f(0.1, 1000.0))


# --- stage -----------------------------------------------------------------


def build_scene(output: Path, feed, runs) -> None:
    """Write the stage as three composed layers (v11).

    base     the whole v10 stage, rebuilt from this feed — standalone openable
    overlay  the cluster resource layer, nothing else
    root     subLayers = [overlay, base]; what Isaac Sim opens. Muting the
             overlay layer is the before/after comparison, and the planned live
             mode rewrites overlay attributes in place while base stays baked.
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    base_path = output.with_name(output.stem + "_base.usda")
    if ARGS.live_base:
        # reuse the finished replay overlay untouched; only the rhythm base
        # and a new root are written (e.g. output factory_three_cells_v11_live
        # -> overlay factory_three_cells_v11_overlay.usda)
        overlay_path = output.with_name(
            output.stem.replace("_live", "") + "_overlay.usda")
    else:
        overlay_path = output.with_name(output.stem + "_overlay.usda")
    end_frame = LIVE_CYCLES * CYCLE_FRAMES if ARGS.live_base else END_FRAME
    stage = Usd.Stage.CreateNew(str(base_path))
    stage.SetMetadata("upAxis", "Z")
    stage.SetMetadata("metersPerUnit", 1.0)
    stage.SetTimeCodesPerSecond(FPS)
    stage.SetStartTimeCode(0)
    stage.SetEndTimeCode(end_frame)

    world = UsdGeom.Xform.Define(stage, "/World")
    stage.SetDefaultPrim(world.GetPrim())
    UsdGeom.Scope.Define(stage, "/World/Looks")

    mats = Mats(stage)
    mats.add("Floor", (0.18, 0.20, 0.22), roughness=0.8)
    mats.add("CellZone", (0.12, 0.19, 0.22), roughness=0.85)
    mats.add("InfraZone", (0.14, 0.16, 0.23), roughness=0.85)
    mats.add("SafetyYellow", (0.95, 0.60, 0.04), metallic=0.15)
    mats.add("ConveyorBelt", (0.055, 0.065, 0.075), roughness=0.7)
    mats.add("Steel", (0.43, 0.47, 0.52), metallic=0.75, roughness=0.25)
    mats.add("RackDark", (0.055, 0.065, 0.08), metallic=0.75)
    mats.add("Black", (0.012, 0.016, 0.022), metallic=0.3)
    mats.add("Server", (0.12, 0.16, 0.20), metallic=0.65)
    mats.add("StatusGreen", (0.05, 0.95, 0.26), roughness=0.2)
    mats.add("StatusRed", (0.95, 0.06, 0.04), roughness=0.2)
    mats.add("TSNBlue", (0.02, 0.45, 1.0), metallic=0.25, roughness=0.25)
    mats.add("BestEffortOrange", (1.0, 0.28, 0.03), metallic=0.1)
    mats.add("CNC", (0.02, 0.85, 0.88), metallic=0.2)
    mats.add("PLC", (0.36, 0.68, 0.27), metallic=0.15)
    mats.add("Product", (0.78, 0.34, 0.08), roughness=0.75)
    mats.add("Sign", (0.8, 0.84, 0.88), roughness=0.7)
    alias = {
        "floor": "Floor", "cell": "CellZone", "infra": "InfraZone", "yellow": "SafetyYellow",
        "belt": "ConveyorBelt", "steel": "Steel", "dark": "RackDark", "black": "Black",
        "server": "Server", "green": "StatusGreen", "red": "StatusRed", "blue": "TSNBlue",
        "orange": "BestEffortOrange", "cyan": "CNC", "plc": "PLC", "box": "Product", "white": "Sign",
    }

    class Alias:
        def __getitem__(self, key):
            return mats[alias.get(key, key)]

    m = Alias()

    # Per-cell material instances. The shared blue/orange cannot express three
    # independent path states, so every zone gets its own copy to animate.
    cell_mats = {}
    for track, (_cell_name, _x, zone) in CELL_OF_TRACK.items():
        cell_mats[track] = {
            "tsn": f"TSN_{zone}", "besteffort": f"BestEffort_{zone}",
            "alert": f"Alert_{zone}", "plc": f"Plc_{zone}",
            "rack_tsn": f"RackLed_TSN_{zone}", "rack_be": f"RackLed_BE_{zone}",
            "pod": f"Pod_{zone}",
        }
        mats.add(cell_mats[track]["tsn"], (0.02, 0.45, 1.0), metallic=0.25, roughness=0.25)
        mats.add(cell_mats[track]["besteffort"], (1.0, 0.28, 0.03), metallic=0.1)
        mats.add(cell_mats[track]["alert"], (0.10, 0.11, 0.12), roughness=0.35)
        mats.add(cell_mats[track]["plc"], (0.36, 0.68, 0.27), metallic=0.15)
        mats.add(cell_mats[track]["rack_tsn"], (0.05, 0.95, 0.26), roughness=0.2)
        mats.add(cell_mats[track]["rack_be"], (0.05, 0.95, 0.26), roughness=0.2)
        mats.add(cell_mats[track]["pod"], (0.02, 0.45, 1.0), metallic=0.2, roughness=0.3)

    cube(stage, "/World/Factory/Floor", (1.5, 1.5, -0.08), (27.0, 17.0, 0.16), m["floor"])

    cells = {}
    for track, (cell_name, center_x, zone) in CELL_OF_TRACK.items():
        cells[track] = build_cell(stage, cell_name, center_x, zone, m, cell_mats[track])
    build_agv_showcase(stage, m)

    for zone_name, zone_x, identity_key in (
        ("PhysicalPLC", 7.0, "plc"), ("VanillaK8s", 0.0, "orange"), ("DRATSN", -7.0, "blue"),
    ):
        cube(stage, f"/World/Factory/Infrastructure_{zone_name}", (zone_x, 5.55, 0.014), (5.8, 3.0, 0.024), m["infra"])
        cube(stage, f"/World/Factory/Infrastructure_{zone_name}_Bar", (zone_x, 4.10, 0.055), (5.5, 0.22, 0.08), m[identity_key])

    by_zone = {zone: cell_mats[track] for track, (_n, _x, zone) in CELL_OF_TRACK.items()}

    # Switch cabinets keep a fixed colour: they are equipment identity, not
    # traffic state. A blue TSN switch fed by an orange wire is exactly the
    # P1 picture — the path exists, the traffic is not using it.
    cabinet(stage, "/World/Infrastructure/PhysicalPLC/TSNSwitch", (6.20, 6.15, 0.68), (1.30, 0.58, 0.55), m["dark"], m["blue"])
    cabinet(stage, "/World/Infrastructure/PhysicalPLC/BestEffortSwitch", (7.80, 6.15, 0.68), (1.30, 0.58, 0.55), m["dark"], m["orange"])

    rack(stage, "/World/Infrastructure/VanillaK8s/Rack_A", -1.75, 6.15, m, mats[by_zone["VanillaK8s"]["rack_tsn"]])
    rack(stage, "/World/Infrastructure/VanillaK8s/Rack_B", -0.40, 6.15, m, mats[by_zone["VanillaK8s"]["rack_be"]])
    cabinet(stage, "/World/Infrastructure/VanillaK8s/TSNSwitch", (1.05, 6.15, 0.68), (1.30, 0.58, 0.55), m["dark"], m["blue"])
    cabinet(stage, "/World/Infrastructure/VanillaK8s/BestEffortSwitch", (2.55, 6.15, 0.68), (1.30, 0.58, 0.55), m["dark"], m["orange"])

    rack(stage, "/World/Infrastructure/DRATSN/Rack_A", -8.75, 6.15, m, mats[by_zone["DRATSN"]["rack_tsn"]])
    rack(stage, "/World/Infrastructure/DRATSN/Rack_B", -7.40, 6.15, m, mats[by_zone["DRATSN"]["rack_be"]])
    cabinet(stage, "/World/Infrastructure/DRATSN/TSNSwitch", (-5.95, 6.15, 0.68), (1.30, 0.58, 0.55), m["dark"], m["blue"])
    cabinet(stage, "/World/Infrastructure/DRATSN/BestEffortSwitch", (-4.45, 6.15, 0.68), (1.30, 0.58, 0.55), m["dark"], m["orange"])

    cabinet(stage, "/World/Infrastructure/CentralCNC", (CNC_X, CNC_Y, CNC_Z),
            (CNC_W, CNC_D, CNC_H), m["dark"], m["cyan"])

    for switch_root, switch_x in (
        ("PhysicalPLC/TSNSwitch", 6.20), ("PhysicalPLC/BestEffortSwitch", 7.80),
        ("VanillaK8s/TSNSwitch", 1.05), ("VanillaK8s/BestEffortSwitch", 2.55),
        ("DRATSN/TSNSwitch", -5.95), ("DRATSN/BestEffortSwitch", -4.45),
    ):
        for port_idx in range(6):
            cube(stage, f"/World/Infrastructure/{switch_root}/Port_{port_idx}", (switch_x - 0.47 + port_idx * 0.19, 5.825, 0.68), (0.10, 0.025, 0.10), m["black"])

    for tray_name, tray_x in (("PhysicalPLC", 7.0), ("VanillaK8s", 0.0), ("DRATSN", -7.0)):
        cube(stage, f"/World/Infrastructure/{tray_name}/CableTray", (tray_x, 4.50, 0.15), (5.30, 0.30, 0.12), m["steel"])

    # Pod markers ride the anchors that v9 reserved for them. The anchor carries
    # the placement (which rack), the child carries the idle bob.
    pod_ops, pod_bobs = {}, {}
    for zone, anchor_x in (("VanillaK8s", -1.08), ("DRATSN", -8.08)):
        anchor = UsdGeom.Xform.Define(stage, f"/World/Infrastructure/{zone}/PodAnimationAnchor")
        op = anchor.AddTranslateOp()
        op.Set(Gf.Vec3d(anchor_x, 6.15, 2.85), Usd.TimeCode(0))
        marker = UsdGeom.Xform.Define(stage, f"/World/Infrastructure/{zone}/PodAnimationAnchor/PodMarker")
        bob = marker.AddTranslateOp()
        spin = marker.AddRotateZOp()
        cube(stage, f"/World/Infrastructure/{zone}/PodAnimationAnchor/PodMarker/Body", (0.0, 0.0, 0.0),
             (POD_MARKER, POD_MARKER, POD_MARKER * 0.62), mats[by_zone[zone]["pod"]])
        pod_ops[zone] = op
        pod_bobs[zone] = (bob, spin)

    # Controller/node connections: blue is always TSN, orange is always best effort.
    curves = {z: {"tsn": [], "besteffort": []} for z in by_zone}
    plc_tsn = [(4.75, 2.15, 0.24), (4.75, 4.40, 0.24), (6.20, 4.40, 0.24), (6.20, 5.82, 0.24)]
    plc_be = [(4.75, 2.15, 0.34), (4.95, 4.62, 0.34), (7.80, 4.62, 0.34), (7.80, 5.82, 0.34)]
    curves["PhysicalPLC"]["tsn"].append(cable(stage, "/World/Topology/PhysicalPLC/PLC_ToTSN", plc_tsn, mats[by_zone["PhysicalPLC"]["tsn"]]))
    curves["PhysicalPLC"]["besteffort"].append(cable(stage, "/World/Topology/PhysicalPLC/PLC_ToBestEffort", plc_be, mats[by_zone["PhysicalPLC"]["besteffort"]]))

    for zone, a_pts, b_pts in (
        ("VanillaK8s",
         [(-1.75, 5.72, 0.26), (-1.75, 4.40, 0.26), (1.05, 4.40, 0.26), (1.05, 5.82, 0.26)],
         [(-0.40, 5.72, 0.36), (-0.40, 4.62, 0.36), (2.55, 4.62, 0.36), (2.55, 5.82, 0.36)]),
        ("DRATSN",
         [(-8.75, 5.72, 0.26), (-8.75, 4.40, 0.26), (-5.95, 4.40, 0.26), (-5.95, 5.82, 0.26)],
         [(-7.40, 5.72, 0.36), (-7.40, 4.62, 0.36), (-4.45, 4.62, 0.36), (-4.45, 5.82, 0.36)]),
    ):
        curves[zone]["tsn"].append(cable(stage, f"/World/Topology/{zone}/RackA_ToTSN", a_pts, mats[by_zone[zone]["tsn"]]))
        curves[zone]["besteffort"].append(cable(stage, f"/World/Topology/{zone}/RackB_ToBestEffort", b_pts, mats[by_zone[zone]["besteffort"]]))

    packet_rigs = {}
    for zone, cell_x, tsn_switch_x, be_switch_x in (
        ("PhysicalPLC", 7.0, 6.20, 7.80), ("VanillaK8s", 0.0, 1.05, 2.55), ("DRATSN", -7.0, -5.95, -4.45),
    ):
        packet_rigs[zone] = {}
        for robot_idx, robot_x in enumerate((cell_x - 1.15, cell_x + 1.15)):
            tsn_pts = [(tsn_switch_x, 5.82, 0.30), (tsn_switch_x, 3.70, 0.30), (cell_x - 0.32, 3.70, 0.30), (cell_x - 0.32, -0.10, 0.30), (robot_x, -0.10, 0.30), (robot_x, -0.58, 0.30)]
            be_pts = [(be_switch_x, 5.82, 0.20), (be_switch_x, 3.90, 0.20), (cell_x + 0.32, 3.90, 0.20), (cell_x + 0.32, 0.10, 0.20), (robot_x, 0.10, 0.20), (robot_x, -0.48, 0.20)]
            curves[zone]["tsn"].append(cable(stage, f"/World/Topology/{zone}/TSN_ToRobot_{robot_idx}", tsn_pts, mats[by_zone[zone]["tsn"]]))
            curves[zone]["besteffort"].append(cable(stage, f"/World/Topology/{zone}/BestEffort_ToRobot_{robot_idx}", be_pts, mats[by_zone[zone]["besteffort"]], CABLE_W_IDLE - 0.005))
            if robot_idx == 0:
                packet_rigs[zone]["tsn"] = build_packets(stage, f"/World/Topology/{zone}/TSNPackets", tsn_pts, mats[by_zone[zone]["tsn"]])
                packet_rigs[zone]["besteffort"] = build_packets(stage, f"/World/Topology/{zone}/BestEffortPackets", be_pts, mats[by_zone[zone]["besteffort"]])

    for zone, tsn_switch_x in (("PhysicalPLC", 6.20), ("VanillaK8s", 1.05), ("DRATSN", -5.95)):
        cable(stage, f"/World/Topology/CentralCNC/To_{zone}",
              [(CNC_X, CNC_Y - CNC_D / 2, 0.48), (CNC_X, 7.45, 0.48),
               (tsn_switch_x, 7.45, 0.48), (tsn_switch_x, 5.82, 0.48)],
              m["blue"], 0.035)

    # inspection camera uplink: across to the gate post, down the post, then a
    # floor run beside the belt to the best-effort switch
    for zone, cell_x, be_x in (("PhysicalPLC", 7.0, 7.80), ("VanillaK8s", 0.0, 2.55), ("DRATSN", -7.0, -4.45)):
        cable(stage, f"/World/Topology/{zone}/CameraFeed",
              [(cell_x + 0.16, -2.65, 1.86), (cell_x + 0.87, -2.65, 1.86),
               (cell_x + 0.87, -2.65, 0.24), (cell_x + 0.87, 4.02, 0.24),
               (be_x, 4.02, 0.24), (be_x, 5.82, 0.24)], m["orange"], 0.03)

    # --- animation layer ---
    if ARGS.live_base:
        # rhythm only; the dratsn.live extension drives everything else
        for track in CELL_OF_TRACK:
            bake_rhythm(cells[track])
    else:
        track_of = {t.name: t for t in feed.tracks}
        for track, (_cell_name, _x, zone) in CELL_OF_TRACK.items():
            verdicts = runs[track]
            bake_cell(cells[track], verdicts, mats, cell_mats[track])
            bake_paths(mats, cell_mats[track], zone, track_of[track], pod_ops.get(zone),
                       curves[zone], packet_rigs[zone])
            if zone == "PhysicalPLC":
                bake_plc_fault(mats, cell_mats[track], verdicts)
    for bob, spin in pod_bobs.values():
        bake_pod_bob(bob, spin)

    dome = UsdLux.DomeLight.Define(stage, "/World/Lights/Dome")
    dome.CreateIntensityAttr(900.0)
    dome.CreateColorAttr(Gf.Vec3f(0.72, 0.80, 1.0))
    key = UsdLux.DistantLight.Define(stage, "/World/Lights/Key")
    key.CreateIntensityAttr(2500.0)
    key.CreateAngleAttr(0.6)
    key.AddRotateXYZOp().Set(Gf.Vec3f(-48.0, 22.0, -28.0))

    # Near-top view, 20 degrees off vertical, framing all three cells plus the
    # AGV showcase. USD's default horizontalAperture is 20.955 mm, not the 36 mm
    # a 35 mm-film intuition assumes, so covering the 26 m floor takes far more
    # height than it looks: half-FOV is atan(10.478 / focal).
    # A true 90-degree top view fits too, but flattens the racks and robots into
    # unreadable silhouettes, so the slight tilt stays.
    def add_camera(name, eye, target, focal, up=(0.0, 0.0, 1.0)):
        cam = UsdGeom.Camera.Define(stage, f"/World/Cameras/{name}")
        view = Gf.Matrix4d(1.0)
        view.SetLookAt(Gf.Vec3d(*eye), Gf.Vec3d(*target), Gf.Vec3d(*up))
        cam.AddTransformOp().Set(view.GetInverse())
        cam.CreateFocalLengthAttr(focal)
        cam.CreateClippingRangeAttr(Gf.Vec2f(0.1, 1000.0))
        return cam

    add_camera("Overview", (2.30, -8.995, 30.670), (2.30, 1.95, 0.60), 24.0)
    add_camera("Angled", (1.5, -31.0, 23.0), (1.5, 0.8, 0.90), 34.0)   # v9 framing
    ver = "live" if ARGS.live_base else feed.scenario
    stage.SetMetadata("customLayerData", {
        "cameraPrim": "/World/Cameras/Overview",
        "sceneVersion": f"scenario-{SCENE_VER}-base-{ver}",
        "lambdaMs": ARGS.lam,
        "compression": feed.compression,
        "cycles": LIVE_CYCLES if ARGS.live_base else N_CYCLES,
    })

    stage.GetRootLayer().Save()
    print(f"Created {base_path}  frames 0..{end_frame} @ {FPS}fps")

    # --- overlay + root composition (v11) ---
    if ARGS.live_base:
        # the replay overlay is sublayered by NAME, never rewritten
        overlay_layer = None
    else:
        overlay_layer = Sdf.Layer.CreateNew(str(overlay_path))
    root_stage = Usd.Stage.CreateNew(str(output))
    root_layer = root_stage.GetRootLayer()
    root_layer.subLayerPaths = [overlay_path.name, base_path.name]
    root_stage.SetMetadata("upAxis", "Z")
    root_stage.SetMetadata("metersPerUnit", 1.0)
    root_stage.SetTimeCodesPerSecond(FPS)
    root_stage.SetStartTimeCode(0)
    root_stage.SetEndTimeCode(end_frame)
    root_stage.SetDefaultPrim(root_stage.GetPrimAtPath("/World"))
    root_stage.SetMetadata("customLayerData", {
        "cameraPrim": "/World/Cameras/OverviewCluster",
        "sceneVersion": f"scenario-{SCENE_VER}-{ver}",
        "lambdaMs": ARGS.lam,
        "compression": feed.compression,
        "cycles": LIVE_CYCLES if ARGS.live_base else N_CYCLES,
    })
    if overlay_layer is not None:
        root_stage.SetEditTarget(Usd.EditTarget(overlay_layer))
        build_cluster_overlay(root_stage, feed, runs)
        overlay_layer.Save()
    root_layer.Save()
    print(f"Created {output} (root; overlay={overlay_path.name})")


def main() -> None:
    feed = M.load_feed(ARGS.feed) if ARGS.feed else M.default_fault_feed()
    runs = M.run_feed(feed, ARGS.lam, seed=ARGS.seed)
    for name, verdicts in runs.items():
        s = M.summarize(verdicts)
        print(f"  {name:9s} produced={s['produced']:3d} defect={s['defect']:3d} stopped={s['stopped']:3d}")
    build_scene(Path(ARGS.output).resolve(), feed, runs)


if __name__ == "__main__":
    main()
    simulation_app.close()
