# The ghost bounds box: a GPU overlay (no scene objects) at A . CFrame.new(bounds.offset) with bounds.size, green
# when every vertex is inside, red when not, plus a small text readout of what's over and by how much.

import blf
import bpy
import gpu
from gpu_extras.batch import batch_for_shader

from . import geom
from .evaluate import LIVE

_handles = []
GREEN = (0.25, 0.9, 0.4)
RED = (1.0, 0.28, 0.22)
GREY = (0.7, 0.7, 0.7)


def _colour(res):
    if res.get("inside") is None:
        return GREY
    return GREEN if res["inside"] else RED


def _visible(context):
    sc = context.scene
    st = getattr(sc, "fitbridge", None)
    return st is not None and st.show_box and not st.thumb_view and ready(context)


def ready(context):
    """only once this file has a FitBridge rig and there's an accessory mesh (so a fresh scene stays clean)"""
    from . import evaluate, rig
    has_rig = any(bpy.data.collections.get(n) is not None for n in (rig.R15_COLL, rig.R6_COLL))
    return has_rig and evaluate.target_object(context) is not None


def draw_view():
    context = bpy.context
    res = LIVE["result"]
    if res is None or res.get("box") is None or not _visible(context):
        return
    if res.get("scale") != context.scene.fitbridge.body_scale:
        return
    frame, size = res["box"]
    corners = geom.box_corners(frame, size) @ geom.P.T                # Roblox world -> Blender world
    pts = [tuple(c) for c in corners]
    col = _colour(res)
    gpu.state.blend_set("ALPHA")
    gpu.state.depth_test_set("NONE")
    sh = gpu.shader.from_builtin("UNIFORM_COLOR")
    tris = batch_for_shader(sh, "TRIS", {"pos": pts}, indices=geom.BOX_TRIS)
    sh.uniform_float("color", (*col, 0.10))
    tris.draw(sh)
    ls = gpu.shader.from_builtin("POLYLINE_UNIFORM_COLOR")
    region = context.region
    ls.uniform_float("viewportSize", (region.width, region.height))
    ls.uniform_float("lineWidth", 2.0)
    ls.uniform_float("color", (*col, 0.95))
    batch_for_shader(ls, "LINES", {"pos": pts}, indices=geom.BOX_EDGES).draw(ls)
    # the attachment point
    A = res.get("A")
    if A is not None:
        o = A[:3, 3]
        s = 0.15
        cross = [o + d for d in ((s, 0, 0), (-s, 0, 0), (0, s, 0), (0, -s, 0), (0, 0, s), (0, 0, -s))]
        cross = [tuple(geom.P @ c) for c in cross]
        ls.uniform_float("color", (1.0, 0.85, 0.2, 1.0))
        batch_for_shader(ls, "LINES", {"pos": cross}).draw(ls)
    gpu.state.blend_set("NONE")


def _hint(context):
    """until the FitBridge tab has been opened once: where to find it"""
    a = context.preferences.addons.get(__package__)
    if a is None or a.preferences.welcomed:
        return False
    ui = context.preferences.system.ui_scale
    blf.size(0, 13 * ui)
    blf.color(0, 0.85, 0.85, 0.85, 1.0)
    blf.position(0, 20 * ui, 60 * ui, 0)
    blf.draw(0, "FitBridge: press N for the sidebar, then open the FitBridge tab")
    return True


def draw_text():
    context = bpy.context
    if _hint(context):
        return
    res = LIVE["result"]
    if res is None or not _visible(context):
        return
    if res.get("inside") is None:
        lines = []
    elif res["inside"]:
        lines = [f"FitBridge: inside {res['attachment']}"]
    else:
        lines = ["FitBridge: outside the box"] + res.get("over_lines", [])
    if not lines:
        return
    font = 0
    ui = context.preferences.system.ui_scale
    blf.size(font, 13 * ui)
    col = _colour(res)
    y = 60 * ui
    for i, line in enumerate(reversed(lines)):
        blf.position(font, 20 * ui, y + i * 18 * ui, 0)
        blf.color(font, *col, 1.0)
        blf.draw(font, line)


# ------------------------------------------------------------------------------------------------- thumbnail view

_ghost = {"key": None, "batch": None}


def _ghost_batch(sh):
    """the shown mannequin's triangles (its objects are hidden in Thumbnail view), cached until the rig changes"""
    from .rig import R15_COLL, R6_COLL
    coll = bpy.data.collections.get(R15_COLL) or bpy.data.collections.get(R6_COLL)
    if coll is None:
        return None
    key = (coll.name, tuple((o.name, len(o.data.vertices)) for o in coll.objects if o.type == "MESH"))
    if _ghost["key"] != key:
        pts = []
        for o in coll.objects:
            if o.type != "MESH":
                continue
            me = o.data
            me.calc_loop_triangles()
            M = o.matrix_world
            co = [M @ v.co for v in me.vertices]
            for t in me.loop_triangles:
                pts.extend(tuple(co[i]) for i in t.vertices)
        _ghost["key"], _ghost["batch"] = key, batch_for_shader(sh, "TRIS", {"pos": pts}) if pts else None
    return _ghost["batch"]


def _in_thumb_camera(context):
    st = getattr(context.scene, "fitbridge", None)
    r3 = context.region_data
    return st is not None and st.thumb_view and r3 is not None and r3.view_perspective == "CAMERA"


def draw_ghost():
    """the mannequin as a faint see-through ghost while framing (never rendered)"""
    context = bpy.context
    st = getattr(context.scene, "fitbridge", None)
    if st is None or not st.thumb_view:
        return
    sh = gpu.shader.from_builtin("UNIFORM_COLOR")
    batch = _ghost_batch(sh)
    if batch is None:
        return
    gpu.state.blend_set("ALPHA")
    gpu.state.depth_test_set("LESS_EQUAL")
    gpu.state.depth_mask_set(False)
    sh.uniform_float("color", (0.75, 0.78, 0.85, 0.12))
    batch.draw(sh)
    gpu.state.depth_mask_set(True)
    gpu.state.depth_test_set("NONE")
    gpu.state.blend_set("NONE")


def draw_crop_guide():
    """in the thumbnail camera: the circle Roblox's own thumbnail tool fits the Handle into by default (the Handle's
    bounding sphere), as a reference for how Roblox sizes the item"""
    context = bpy.context
    if not _in_thumb_camera(context):
        return
    import math
    from bpy_extras.view3d_utils import location_3d_to_region_2d
    from mathutils import Vector
    from .evaluate import LIVE
    from .thumb import handle_circle
    got = handle_circle(LIVE["result"])
    cam = context.scene.camera
    if got is None or cam is None:
        return
    centre, radius = Vector(got[0]), got[1]
    right = cam.matrix_world.col[0].xyz.normalized()
    up = cam.matrix_world.col[1].xyz.normalized()
    pts = []
    for k in range(64):
        t = 2 * math.pi * k / 64
        p = location_3d_to_region_2d(context.region, context.region_data,
                                     centre + (right * math.cos(t) + up * math.sin(t)) * radius)
        if p is None:
            return
        pts.append((p.x, p.y, 0))
    ls = gpu.shader.from_builtin("POLYLINE_UNIFORM_COLOR")
    ls.uniform_float("viewportSize", (context.region.width, context.region.height))
    ls.uniform_float("lineWidth", 1.0)
    ls.uniform_float("color", (1.0, 1.0, 1.0, 0.4))
    gpu.state.blend_set("ALPHA")
    batch_for_shader(ls, "LINE_LOOP", {"pos": pts}).draw(ls)
    gpu.state.blend_set("NONE")


def register():
    _handles.append(bpy.types.SpaceView3D.draw_handler_add(draw_view, (), "WINDOW", "POST_VIEW"))
    _handles.append(bpy.types.SpaceView3D.draw_handler_add(draw_ghost, (), "WINDOW", "POST_VIEW"))
    _handles.append(bpy.types.SpaceView3D.draw_handler_add(draw_text, (), "WINDOW", "POST_PIXEL"))
    _handles.append(bpy.types.SpaceView3D.draw_handler_add(draw_crop_guide, (), "WINDOW", "POST_PIXEL"))


def unregister():
    while _handles:
        bpy.types.SpaceView3D.draw_handler_remove(_handles.pop(), "WINDOW")
