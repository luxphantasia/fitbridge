# The ghost bounds box: a GPU overlay (no scene objects) at A . CFrame.new(bounds.offset) with bounds.size, green
# when every vertex is inside, red when not, plus a small text readout of what's over and by how much.

import blf
import bpy
import gpu
import numpy as np
from gpu_extras.batch import batch_for_shader
from mathutils import Matrix

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
    corners = geom.box_corners(frame, size) @ geom.P                # Roblox world -> Blender world
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
        cross = [tuple(geom.P.T @ c) for c in cross]
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


# ------------------------------------------------------------------------------------------------ live thumbnail

LIVE_SIZE = 256            # pixels, before the UI scale
MAX_WINDOW_SIZE = 1024     # a big pop-out stretches this instead of drawing more
_live = {"mesh": None, "batch": None, "shader": None, "offscreen": None, "broken": False, "windows": set()}

_LIVE_VERT = """
void main() {
    uvInterp = uv;
    normalInterp = normal;
    gl_Position = viewProjection * vec4(pos, 1.0);
}
"""
_LIVE_FRAG = """
void main() {
    vec3 base = hasImage != 0 ? texture(image, uvInterp).rgb : vec3(0.6);
    float facing = abs(dot(normalize(normalInterp), lightDir));
    vec3 lit = base * (0.32 + 0.55 * facing);
    fragColor = vec4(pow(clamp(lit, 0.0, 1.0), vec3(1.0 / 2.2)), 1.0);
}
"""


def live_camera(res, g):
    """(view-projection as a nested list, direction toward the camera) for the shot in g, in Blender world, through the
    thumbnail's own lens"""
    from . import thumb
    world = np.array(thumb.expected_world(res, g), float)
    near, far = 0.01, 1000.0
    f = 1.0 / thumb.TAN
    proj = np.array([[f, 0, 0, 0], [0, f, 0, 0], [0, 0, (far + near) / (near - far), 2 * far * near / (near - far)],
                     [0, 0, -1.0, 0]])
    return proj @ np.linalg.inv(world), world[:3, 2]


def live_arrays(res):
    """the accessory per corner, in Blender world, ready for the GPU"""
    m = res["mesh"]
    tl = m["tri_loops"][:, ::-1] if m["flip"] else m["tri_loops"]
    loops = tl.ravel()
    uv = m["uv"][loops] if m["uv"] is not None else np.zeros((len(loops), 2))
    return {"pos": m["co"][m["loop_vert"][loops]].astype(np.float32), "normal": m["normals"][loops].astype(np.float32),
            "uv": uv.astype(np.float32)}


def _live_shader():
    if _live["shader"] is None:
        info = gpu.types.GPUShaderCreateInfo()
        info.vertex_in(0, "VEC3", "pos")
        info.vertex_in(1, "VEC3", "normal")
        info.vertex_in(2, "VEC2", "uv")
        between = gpu.types.GPUStageInterfaceInfo("fitbridge_live")
        between.smooth("VEC2", "uvInterp")
        between.smooth("VEC3", "normalInterp")
        info.vertex_out(between)
        info.push_constant("MAT4", "viewProjection")
        info.push_constant("VEC3", "lightDir")
        info.push_constant("INT", "hasImage")
        info.sampler(0, "FLOAT_2D", "image")
        info.fragment_out(0, "VEC4", "fragColor")
        info.vertex_source(_LIVE_VERT)
        info.fragment_source(_LIVE_FRAG)
        _live["shader"] = gpu.shader.create_from_info(info)
    return _live["shader"]


def _live_ready(context):
    """there's a fitted accessory whose shot means something (in Auto mode: once it has been decided)"""
    from . import auto
    res = LIVE["result"]
    if res is None or res.get("fit") is None or res.get("mesh") is None or res.get("A") is None:
        return False
    return not auto.enabled() or auto.status(context)[0] == "ready"


def _live_window_open(context):
    """is a pop-out still open? (closed ones are forgotten)"""
    if not _live["windows"]:
        return False
    alive = {a.as_pointer() for w in context.window_manager.windows for a in w.screen.areas if a.type == "VIEW_3D"}
    _live["windows"] &= alive
    return bool(_live["windows"])


def _rect(x, y, w, h, color):
    sh = gpu.shader.from_builtin("UNIFORM_COLOR")
    sh.uniform_float("color", color)
    batch_for_shader(sh, "TRI_FAN", {"pos": ((x, y), (x + w, y), (x + w, y + h), (x, y + h))}).draw(sh)


def _render_live(context, size):
    """draw the shot into the offscreen buffer at this size"""
    from . import thumb
    res = LIVE["result"]
    g, _ob = thumb.owner(context)
    shader = _live_shader()
    if _live["mesh"] is not res["mesh"]:
        _live["batch"] = batch_for_shader(shader, "TRIS", live_arrays(res))
        _live["mesh"] = res["mesh"]
    if _live["offscreen"] is None or _live["offscreen"].width != size:
        if _live["offscreen"] is not None:
            _live["offscreen"].free()
        _live["offscreen"] = gpu.types.GPUOffScreen(size, size)
    view_proj, toward = live_camera(res, g)
    image = (res.get("textures") or {}).get("color")
    with _live["offscreen"].bind():
        fb = gpu.state.active_framebuffer_get()
        fb.clear(color=(0.5, 0.5, 0.5, 1.0), depth=1.0)
        gpu.state.depth_test_set("LESS_EQUAL")
        gpu.state.depth_mask_set(True)
        shader.bind()
        shader.uniform_float("viewProjection", Matrix(view_proj.tolist()))
        shader.uniform_float("lightDir", tuple(float(x) for x in toward))
        shader.uniform_int("hasImage", 1 if image is not None else 0)
        if image is not None:
            shader.uniform_sampler("image", gpu.texture.from_image(image))
        _live["batch"].draw(shader)
        gpu.state.depth_test_set("NONE")
        gpu.state.depth_mask_set(False)


def _show_live(x, y, s):
    sh = gpu.shader.from_builtin("IMAGE")
    sh.bind()
    sh.uniform_sampler("image", _live["offscreen"].texture_color)
    batch_for_shader(sh, "TRI_FAN", {"pos": ((x, y), (x + s, y), (x + s, y + s), (x, y + s)),
                                     "texCoord": ((0, 0), (1, 0), (1, 1), (0, 1))}).draw(sh)


def draw_live_thumbnail():
    """the thumbnail as it stands, redrawn with the viewport: only the accessory. It fills a pop-out window, kept square
    however the window is sized, or else sits in the corner of the viewport."""
    if _live["broken"] or bpy.app.background:
        return
    context = bpy.context
    try:
        from . import auto
        scale = context.preferences.system.ui_scale
        popped = _live_window_open(context)
        if context.area is not None and context.area.as_pointer() in _live["windows"]:
            w, h = context.region.width, context.region.height
            _rect(0, 0, w, h, (0.11, 0.11, 0.11, 1.0))
            if _live_ready(context):
                size = min(w, h)
                _render_live(context, max(min(size, MAX_WINDOW_SIZE), 64))
                _show_live((w - size) // 2, (h - size) // 2, size)
            else:
                blf.size(0, 13 * scale)
                blf.color(0, 1, 1, 1, 0.7)
                blf.position(0, 12 * scale, h / 2, 0)
                blf.draw(0, "Select an accessory")
            return
        p = auto.prefs()
        if popped or not _live_ready(context) or not (auto.enabled() or (p and p.live_preview)):
            return
        size = int(LIVE_SIZE * scale)
        _render_live(context, size)
        x, y = int(20 * scale), int(85 * scale)
        _show_live(x, y, size)
        blf.size(0, 11 * scale)
        blf.color(0, 1, 1, 1, 0.85)
        blf.position(0, x, y + size + 5 * scale, 0)
        blf.draw(0, "Thumbnail")
    except ReferenceError:
        pass                                                   # the mesh or image went away: the next check catches up
    except Exception as e:
        _live["broken"] = True                                 # don't repeat a drawing error on every redraw
        print(f"FitBridge: live thumbnail off: {e}")


def mark_live_window(area):
    """this area is now a pop-out: nothing of the viewport underneath shows, only the thumbnail"""
    space = area.spaces.active
    space.show_region_header = False
    space.show_region_ui = False
    space.show_region_toolbar = False
    space.show_gizmo = False
    space.overlay.show_overlays = False
    space.shading.type = "SOLID"
    _live["windows"].add(area.as_pointer())


def register():
    _handles.append(bpy.types.SpaceView3D.draw_handler_add(draw_view, (), "WINDOW", "POST_VIEW"))
    _handles.append(bpy.types.SpaceView3D.draw_handler_add(draw_ghost, (), "WINDOW", "POST_VIEW"))
    _handles.append(bpy.types.SpaceView3D.draw_handler_add(draw_text, (), "WINDOW", "POST_PIXEL"))
    _handles.append(bpy.types.SpaceView3D.draw_handler_add(draw_crop_guide, (), "WINDOW", "POST_PIXEL"))
    _handles.append(bpy.types.SpaceView3D.draw_handler_add(draw_live_thumbnail, (), "WINDOW", "POST_PIXEL"))


def unregister():
    while _handles:
        bpy.types.SpaceView3D.draw_handler_remove(_handles.pop(), "WINDOW")
    if _live["offscreen"] is not None:
        _live["offscreen"].free()
    _live.update(mesh=None, batch=None, shader=None, offscreen=None, broken=False)
    _live["windows"].clear()
