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
    return st is not None and st.show_box and not st.thumb_view


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


def draw_text():
    context = bpy.context
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


def register():
    _handles.append(bpy.types.SpaceView3D.draw_handler_add(draw_view, (), "WINDOW", "POST_VIEW"))
    _handles.append(bpy.types.SpaceView3D.draw_handler_add(draw_text, (), "WINDOW", "POST_PIXEL"))


def unregister():
    while _handles:
        bpy.types.SpaceView3D.draw_handler_remove(_handles.pop(), "WINDOW")
