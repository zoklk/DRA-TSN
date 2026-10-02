"""Apply overlay_map ops to the stage's session layer. pxr only — no omni,
so the local usd-core smoke test exercises the identical code path.

Everything is authored as session-layer *defaults* (no timecodes): the
session layer is the strongest layer in the stack, so its defaults mask the
baked overlay layer's time samples — that is the entire live-mode masking
mechanism (plan §applier; timeline additionally stays stopped at frame 0).
The files on disk are never written.
"""
from pxr import Gf, Sdf, Usd, UsdGeom, UsdShade

_TYPES = {
    "color3f": (Sdf.ValueTypeNames.Color3f, lambda v: Gf.Vec3f(*v)),
    "double3": (Sdf.ValueTypeNames.Double3, lambda v: Gf.Vec3d(*v)),
    "float": (Sdf.ValueTypeNames.Float, float),
}


def _set_attr(prim, name, type_name, value):
    sdf_type, conv = _TYPES[type_name]
    attr = prim.GetAttribute(name)
    if not attr:
        if name == "xformOp:translate":
            attr = UsdGeom.Xformable(prim).AddTranslateOp().GetAttr()
        elif name == "xformOp:scale":
            attr = UsdGeom.Xformable(prim).AddScaleOp().GetAttr()
        else:
            attr = prim.CreateAttribute(name, sdf_type)
    attr.Set(conv(value))


def _bind(stage, prim, material_name):
    path = f"/World/Looks/{material_name}"
    mat = UsdShade.Material(stage.GetPrimAtPath(path))
    if not mat:
        # live can need more actor materials than the baked trial produced
        # (e.g. a longer eviction cascade -> ActVictim2). Define a minimal
        # UsdPreviewSurface in the session; overlay_map colours it via the
        # usual Shader attr ops.
        mat = UsdShade.Material.Define(stage, path)
        shader = UsdShade.Shader.Define(stage, f"{path}/Shader")
        shader.CreateIdAttr("UsdPreviewSurface")
        shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(
            Gf.Vec3f(0.5, 0.5, 0.5))
        shader.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(0.4)
        shader.CreateInput("emissiveColor", Sdf.ValueTypeNames.Color3f).Set(
            Gf.Vec3f(0.0, 0.0, 0.0))
        mat.CreateSurfaceOutput().ConnectToSource(
            shader.ConnectableAPI(), "surface")
    UsdShade.MaterialBindingAPI.Apply(prim).Bind(mat)


def _ensure_cube(stage, path, translate, scale, material_name, visible):
    """Existing prims are updated in place; a prim the baked scene does not
    have (a live placement that differs from the baked trial) is defined in
    the session layer, inside the fixed naming contract (ADR-0033 §6)."""
    prim = stage.GetPrimAtPath(path)
    if not prim:
        cube = UsdGeom.Cube.Define(stage, path)
        cube.CreateSizeAttr(1.0)
        prim = cube.GetPrim()
    _set_attr(prim, "xformOp:translate", "double3", translate)
    _set_attr(prim, "xformOp:scale", "double3", scale)
    _bind(stage, prim, material_name)
    if visible is not None:
        _set_vis(prim, visible)


def _set_vis(prim, visible):
    UsdGeom.Imageable(prim).CreateVisibilityAttr().Set(
        UsdGeom.Tokens.inherited if visible else UsdGeom.Tokens.invisible)


def apply(stage, ops):
    """Apply ops under the session edit target. Unknown prim paths are
    counted, not raised — a live run must never kill the render loop.

    Two passes: prim/schema-creating ops (cube define, material fallback of
    bind) run outside, then every value write runs inside one Sdf.ChangeBlock
    so a whole frame's worth of edits produces a single change notification —
    per-op notification storms were the frame-hitch culprit."""
    missing = []
    creators = [op for op in ops if op[0] in ("cube", "bind")]
    setters = [op for op in ops if op[0] not in ("cube", "bind")]
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        for op in creators:
            if op[0] == "cube":
                _, path, translate, scale, mat, visible = op
                _ensure_cube(stage, path, translate, scale, mat, visible)
            else:
                prim = stage.GetPrimAtPath(op[1])
                if not prim:
                    missing.append(op[1])
                    continue
                _bind(stage, prim, op[2])
        with Sdf.ChangeBlock():
            for op in setters:
                kind = op[0]
                prim = stage.GetPrimAtPath(op[1])
                if not prim:
                    missing.append(op[1])
                    continue
                if kind == "attr":
                    _, _path, name, type_name, value = op
                    _set_attr(prim, name, type_name, value)
                elif kind == "vis":
                    _set_vis(prim, op[2])
                elif kind == "curve":
                    prim.GetAttribute("points").Set(
                        [Gf.Vec3f(*p) for p in op[2]])
                elif kind == "widths":
                    # uniform width for every CV of a BasisCurves (wires)
                    pts = prim.GetAttribute("points").Get()
                    if pts:
                        prim.GetAttribute("widths").Set([op[2]] * len(pts))
                elif kind == "freeze":
                    # pin the resolved value at `timecode` as a session
                    # default — the timeline loses the attr until "clear"
                    _, _path, names, timecode = op
                    for name in names:
                        attr = prim.GetAttribute(name)
                        if attr:
                            v = attr.Get(Usd.TimeCode(timecode))
                            if v is not None:
                                attr.Set(v)
                elif kind == "clear":
                    # drop the session opinion; baked samples resume
                    for name in op[2]:
                        attr = prim.GetAttribute(name)
                        if attr:
                            attr.Clear()
    return missing


def clear_session(stage):
    """Drop every live opinion — dev-loop reset and shutdown hygiene."""
    stage.GetSessionLayer().Clear()


def baked_node_leaves(stage, zone):
    """Child prim names of /World/ClusterOverlay/<zone>/Nodes in authoring
    order (= the baked feed's cluster.nodes order: TSN levels then general).
    Used to alias live node names onto baked prims when the live cluster is
    named differently (dual-cluster demo)."""
    nodes = stage.GetPrimAtPath(f"/World/ClusterOverlay/{zone}/Nodes")
    if not nodes:
        return []
    return [p.GetName() for p in nodes.GetChildren()]
