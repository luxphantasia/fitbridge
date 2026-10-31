# The thumbnail camera. Each accessory (or the scene, before a mesh is picked) owns one shot, kept as an orbit around a
# pivot in the worn Handle's frame (Roblox stores the camera relative to the Handle, so the shot follows the item):
# turn (0 = from the character's front, + = toward its right), tilt (+ = from above), distance, and pan in the
# picture's plane. Until you move the camera yourself, the shot re-aims to a side-aware close-up of the attachment's
# body part whenever type, attachment or mesh change. Viewport navigation moves the camera (Lock Camera to View) and the
# sliders follow it; the sliders move the camera too.

import json
import math

import bpy
import numpy as np
from mathutils import Matrix

from . import geom, spec

FOV = spec.SPEC["thumbnail"]["field_of_view"]
TAN = math.tan(math.radians(FOV) / 2)
# Roblox's own UGC Thumbnail Tool (plugin 11628621048) starts from this shot: the view direction is its
# CFrame.fromEulerAnglesXYZ(20, 30, 45) applied to the Handle's look vector (camera ~32 deg to the character's left,
# ~17 deg above), centred on the Handle, at the distance where the Handle's bounding sphere fills the 70 deg view.
ROBLOX_DIR = np.array([-0.5, 0.29620, -0.81380])
ROBLOX_DIR = ROBLOX_DIR / np.linalg.norm(ROBLOX_DIR)
COLL = "FitBridge Thumbnails"
_quiet = {"n": 0}                   # > 0 while we set the sliders ourselves (their update must not mark "moved by you")
_last = {}                          # camera name -> the matrix we last put it at (to notice your own moves)
_views = {}                         # area pointer -> the viewport you had before Thumbnail view

# which side of the character each attachment lives on: (turn, tilt) of the default shot
_SIDE = {
    "BodyBackAttachment": (155.0, 15.0), "WaistBackAttachment": (155.0, 15.0),
    "LeftCollarAttachment": (-65.0, 15.0), "LeftShoulderAttachment": (-65.0, 15.0),
    "RightCollarAttachment": (65.0, 15.0), "RightShoulderAttachment": (65.0, 15.0),
    "HatAttachment": (25.0, 20.0), "HairAttachment": (25.0, 20.0),
}
_FRONT = (25.0, 15.0)
PRESETS = {"FRONT": (0.0, 5.0), "THREE_Q": (35.0, 15.0), "SIDE": (90.0, 5.0), "BACK": (180.0, 10.0),
           "TOP": (0.0, 85.0)}


# ------------------------------------------------------------------------------------------------------------- maths

def _dir(turn, tilt):
    t, p = math.radians(turn), math.radians(tilt)
    return np.array([math.sin(t) * math.cos(p), math.sin(p), -math.cos(t) * math.cos(p)])


def _basis(d):
    """camera right / up for a view from direction d (camera -> target is -d), no roll"""
    m = geom.look_at(d, [0, 0, 0])
    return m[:3, 0], m[:3, 1]


def local_matrix(turn, tilt, distance, pan, pivot):
    """the camera's frame in the Handle's frame"""
    d = _dir(turn, tilt)
    right, up = _basis(d)
    target = np.asarray(pivot, float) + right * pan[0] + up * pan[1]
    return geom.look_at(target + d * distance, target)


def params_from_local(M, pivot):
    """(turn, tilt, distance, pan) that reproduce a camera frame M (Handle space) around pivot (roll is dropped)"""
    M = geom.unscaled(M)
    eye, d = M[:3, 3], M[:3, 2] / np.linalg.norm(M[:3, 2])
    pivot = np.asarray(pivot, float)
    t = max(float(np.dot(pivot - eye, -d)), 0.05)
    target = eye - d * t
    tilt = math.degrees(math.asin(max(-1.0, min(1.0, d[1]))))
    tilt = max(-89.0, min(89.0, tilt))
    turn = math.degrees(math.atan2(d[0], -d[2]))
    right, up = _basis(_dir(turn, tilt))
    off = target - pivot
    return turn, tilt, t, (float(off @ right), float(off @ up))


# ------------------------------------------------------------------------------------------------------- the shot

def owner(context):
    """the property group holding the current shot: the accessory's, or the scene's before a mesh is picked"""
    from .evaluate import target_object
    ob = target_object(context)
    return (ob.fitbridge_thumb, ob) if ob is not None else (context.scene.fitbridge.thumb_default, None)


def handle_frame(res):
    """the worn Handle in Roblox world (the attachment frame until a mesh is fitted)"""
    A = res.get("A")
    if A is None:
        return None
    off = res["fit"]["offset"] if res.get("fit") is not None else np.zeros(3)
    return A @ geom.translation(off)


def default_shot(res, H):
    """side-aware close-up around the attachment, sized by its body part: (turn, tilt, distance, pan, pivot in Handle
    space)"""
    turn, tilt = _SIDE.get(res["attachment"], _FRONT)
    rig_data = spec.rig(res["scale"])
    part_name = spec.rig_part_for(res["acc_type"], res["attachment"])
    part = next((p for p in rig_data["parts"] if p["name"] == part_name), None)
    A = res["A"]
    size = float(max(part["size"])) if part is not None else 1.5
    pivot_world = A[:3, 3]                                     # where the item attaches: it sits around this point
    Hi = np.linalg.inv(H)
    pivot = Hi[:3, :3] @ pivot_world + Hi[:3, 3]
    extent = size * 2.2                                        # the body part with room around it, not filling the shot
    distance = extent / 2 / TAN + size / 2
    return turn, tilt, distance, (0.0, 0.0), pivot


def _set(g, turn, tilt, distance, pan, pivot=None):
    _quiet["n"] += 1
    try:
        g.turn, g.tilt, g.distance = turn, tilt, distance
        g.pan_x, g.pan_y = pan
        if pivot is not None:
            g.pivot = [float(x) for x in pivot]
    finally:
        _quiet["n"] -= 1


def expected_world(res, g):
    """the camera's Blender world matrix for the shot in g (None without an attachment frame)"""
    H = handle_frame(res)
    if H is None:
        return None
    L = local_matrix(g.turn, g.tilt, g.distance, (g.pan_x, g.pan_y), g.pivot)
    return geom.swap(H @ L)


def target_world(res, g):
    """what the shot looks at (pivot + pan), Blender world"""
    H = handle_frame(res)
    d = _dir(g.turn, g.tilt)
    right, up = _basis(d)
    t = np.asarray(g.pivot, float) + right * g.pan_x + up * g.pan_y
    return geom.P @ (H[:3, :3] @ t + H[:3, 3])


def aim_views(context, res, g):
    """keep each camera-view viewport's own pivot on the shot's target at the shot's distance, so Blender's orbit,
    zoom and pan (which move the camera through Lock Camera to View) work around the accessory"""
    cam = g.camera
    if cam is None or res is None or res.get("A") is None:
        return
    target = target_world(res, g)
    rot = cam.matrix_world.to_quaternion()
    for area in _view3d_areas(context):
        sp = area.spaces.active
        r3 = sp.region_3d
        if r3.view_perspective != "CAMERA" or context.scene.camera != cam:
            continue
        r3.view_rotation = rot
        r3.view_distance = g.distance
        r3.view_location = tuple(target)


def camera_cframe(res, g):
    """what goes in the manifest: the camera relative to the worn Handle (Roblox CFrame components)"""
    H = handle_frame(res)
    cam = g.camera
    if cam is not None and cam.users_scene:
        C = geom.unscaled(geom.swap(np.array(cam.matrix_world)))
        return geom.matrix_to_cframe(np.linalg.inv(H) @ C)
    return geom.matrix_to_cframe(local_matrix(g.turn, g.tilt, g.distance, (g.pan_x, g.pan_y), g.pivot))


def ensure_camera(context, g, ob):
    """the shot's camera object, created (or put back in the scene after being deleted) as needed"""
    sc = context.scene
    coll = bpy.data.collections.get(COLL)
    if coll is None:
        coll = bpy.data.collections.new(COLL)
    if coll.name not in sc.collection.children:
        sc.collection.children.link(coll)
    cam = g.camera
    if cam is None or cam.type != "CAMERA":
        data = bpy.data.cameras.new("FitBridge Thumbnail")
        cam = bpy.data.objects.new(f"FitBridge Thumbnail: {ob.name}" if ob else "FitBridge Thumbnail", data)
        g.camera = cam
    if not cam.users_scene:                                    # deleted from the scene but still remembered
        coll.objects.link(cam)
    cam.data.sensor_fit = "VERTICAL"
    cam.data.angle_y = math.radians(FOV)
    cam.data.clip_start = 0.02
    cam.data.passepartout_alpha = 0.6
    cam.hide_select = False
    _last.pop(cam.name, None)
    return cam


def _close(a, b, tol=1e-4):
    return np.abs(np.asarray(a) - np.asarray(b)).max() < tol


def sync(context, res):
    """every tick: re-aim until you've moved it, follow the accessory, and pick up your own camera moves"""
    if res is None or res.get("A") is None:
        return
    g, ob = owner(context)
    H = handle_frame(res)
    key = f"{res['acc_type']}|{res['attachment']}|{res['scale']}"
    if not g.manual:
        turn, tilt, dist, pan, pivot = default_shot(res, H)
        if g.key != key or not _close([g.turn, g.tilt, g.distance, g.pan_x, g.pan_y, *g.pivot],
                                      [turn, tilt, dist, *pan, *pivot], 1e-6):
            _set(g, turn, tilt, dist, pan, pivot)
            g.key = key
    cam = g.camera
    st = context.scene.fitbridge
    if cam is None or not cam.users_scene:
        if not st.thumb_view:
            return
        cam = ensure_camera(context, g, ob)
    actual = np.array(cam.matrix_world)
    last = _last.get(cam.name)
    if last is not None and not _close(actual, last):          # you moved the camera (viewport or by hand)
        L = np.linalg.inv(H) @ geom.unscaled(geom.swap(actual))
        turn, tilt, dist, pan = params_from_local(L, g.pivot)
        _set(g, turn, tilt, dist, pan)
        g.manual = True
        _last[cam.name] = actual
        if st.thumb_view:
            aim_views(context, res, g)                         # orbit next time around the (possibly new) target
        return
    want = expected_world(res, g)
    if st.thumb_view and context.scene.camera != cam:
        context.scene.camera = cam
    if not _close(actual, want):
        cam.matrix_world = Matrix(want.tolist())
        _last[cam.name] = want
        if st.thumb_view:
            aim_views(context, res, g)
    else:
        _last[cam.name] = actual


def apply_params(context):
    """push slider values to the camera now (called from the sliders' update)"""
    from .evaluate import LIVE
    res = LIVE["result"]
    g, _ob = owner(context)
    cam = g.camera
    if res is None or cam is None or res.get("A") is None:
        return
    want = expected_world(res, g)
    cam.matrix_world = Matrix(want.tolist())
    _last[cam.name] = want
    if context.scene.fitbridge.thumb_view:
        aim_views(context, res, g)


def out_of_frame(res, g):
    """True when part of the accessory falls outside the picture"""
    m = res.get("mesh")
    cam = g.camera
    if m is None or cam is None or not len(m["co"]):
        return False
    Ci = np.linalg.inv(geom.unscaled(np.array(cam.matrix_world)))
    P = m["co"] @ Ci[:3, :3].T + Ci[:3, 3]
    z = -P[:, 2]
    if (z <= 1e-6).any():
        return True
    return bool((np.abs(P[:, 0] / z) > TAN).any() or (np.abs(P[:, 1] / z) > TAN).any())


# ------------------------------------------------------------------------------------------------------- the view

def _addon_prefs(context):
    a = context.preferences.addons.get(__package__)
    return a.preferences if a else None


def tilt_lock(context, on):
    """Lock tilt: while framing, orbit as Turntable (no sideways roll), then give back your own orbit style. The
    original is kept in the addon prefs too, so a crash mid-framing is undone on the next start."""
    p = _addon_prefs(context)
    inputs = context.preferences.inputs
    if on:
        if p is not None and not p.saved_orbit:
            p.saved_orbit = inputs.view_rotate_method
        inputs.view_rotate_method = "TURNTABLE"
    elif p is not None and p.saved_orbit:
        inputs.view_rotate_method = p.saved_orbit
        p.saved_orbit = ""


def view_on(context):
    """look through the shot's camera; viewport navigation moves the camera; mannequin ghosted (drawn, not rendered)"""
    from .evaluate import LIVE
    sc = context.scene
    st = sc.fitbridge
    g, ob = owner(context)
    cam = ensure_camera(context, g, ob)
    show_cameras(True)
    res = LIVE["result"]
    if res is not None and res.get("A") is not None:
        sync(context, res)
    r = sc.render
    st.thumb_saved = json.dumps({"x": r.resolution_x, "y": r.resolution_y, "pct": r.resolution_percentage,
                                 "camera": sc.camera.name if sc.camera else ""})
    r.resolution_x = r.resolution_y = spec.SPEC["thumbnail"]["render_size"]
    r.resolution_percentage = 100
    sc.camera = cam
    _hide_rigs(True)
    for area in _view3d_areas(context):
        sp = area.spaces.active
        r3 = sp.region_3d
        _views[area.as_pointer()] = {"loc": tuple(r3.view_location), "rot": tuple(r3.view_rotation),
                                     "dist": r3.view_distance, "persp": r3.view_perspective,
                                     "lock": sp.lock_camera}
        r3.view_perspective = "CAMERA"
        sp.lock_camera = True
    p = _addon_prefs(context)
    if p is not None and p.lock_tilt:
        tilt_lock(context, True)
    if res is not None and res.get("A") is not None:
        aim_views(context, res, g)
    for area in _view3d_areas(context):                         # make the camera frame fill the viewport
        region = next((r for r in area.regions if r.type == "WINDOW"), None)
        if region is not None:
            try:
                with context.temp_override(area=area, region=region):
                    bpy.ops.view3d.view_center_camera()
            except (RuntimeError, TypeError):
                pass
    return cam


def view_off(context):
    sc = context.scene
    st = sc.fitbridge
    tilt_lock(context, False)
    try:
        saved = json.loads(st.thumb_saved or "{}")
    except ValueError:
        saved = {}
    r = sc.render
    if saved:
        r.resolution_x, r.resolution_y, r.resolution_percentage = saved["x"], saved["y"], saved["pct"]
        cam = bpy.data.objects.get(saved.get("camera") or "")
        sc.camera = cam if cam is not None and cam.users_scene else None
    st.thumb_saved = ""
    _hide_rigs(False)
    show_cameras(False)
    for area in _view3d_areas(context):
        sp = area.spaces.active
        r3 = sp.region_3d
        v = _views.pop(area.as_pointer(), None)
        if v is None:
            if r3.view_perspective == "CAMERA":
                r3.view_perspective = "PERSP"
            sp.lock_camera = False
            continue
        r3.view_perspective = v["persp"] if v["persp"] != "CAMERA" else "PERSP"
        r3.view_location, r3.view_rotation, r3.view_distance = v["loc"], v["rot"], v["dist"]
        sp.lock_camera = v["lock"]


def show_cameras(show):
    """the thumbnail cameras only show while you're in Thumbnail view"""
    coll = bpy.data.collections.get(COLL)
    if coll is not None and coll.hide_viewport == show:
        coll.hide_viewport = not show


def _view3d_areas(context):
    wm = context.window_manager
    for win in wm.windows:
        for area in win.screen.areas:
            if area.type == "VIEW_3D":
                yield area


def _hide_rigs(hide):
    from .rig import R15_COLL, R6_COLL
    for name in (R15_COLL, R6_COLL):
        coll = bpy.data.collections.get(name)
        if coll is not None:
            coll.hide_viewport = hide
            coll.hide_render = hide


def roblox_shot(res):
    """Roblox's tool default as a camera frame in the Handle's frame, or None before a mesh is fitted"""
    if res is None or res.get("fit") is None:
        return None
    radius = float(np.linalg.norm(res["fit"]["size"])) / 2
    return geom.look_at(ROBLOX_DIR * (radius / TAN), [0, 0, 0])


def handle_circle(res):
    """the Handle's bounding sphere (Blender world centre, radius): what Roblox's tool fits in its default view"""
    if res is None or res.get("fit") is None or res.get("A") is None:
        return None
    H = handle_frame(res)
    return geom.P @ H[:3, 3], float(np.linalg.norm(res["fit"]["size"])) / 2


def preset(context, name):
    """jump to a named angle (keeps distance and pan); Side and 3/4 face the item's own side. ROBLOX = the exact
    shot Roblox's own thumbnail tool starts from"""
    g, _ob = owner(context)
    if name == "ROBLOX":
        from .evaluate import LIVE
        L = roblox_shot(LIVE["result"])
        if L is None:
            return False
        turn, tilt, dist, pan = params_from_local(L, g.pivot)
        _set(g, turn, tilt, dist, pan)
        g.manual = True
        apply_params(context)
        return True
    turn, tilt = PRESETS[name]
    side_turn = _SIDE.get(context.scene.fitbridge.attachment, _FRONT)[0]
    if name in ("SIDE", "THREE_Q") and side_turn < 0:
        turn = -turn
    _set(g, turn, tilt, g.distance, (g.pan_x, g.pan_y))
    g.manual = True
    apply_params(context)
    return True


def reset(context):
    g, _ob = owner(context)
    g.manual = False
    g.key = ""
    from .evaluate import LIVE
    if LIVE["result"] is not None:
        sync(context, LIVE["result"])
        apply_params(context)


# ---------------------------------------------------------------------------------------------------- preview render

def preview(context):
    """render the 420 x 420 shot with simple flat lighting, only the accessory, and show it"""
    from .evaluate import target_object
    sc = context.scene
    g, ob = owner(context)
    cam = ensure_camera(context, g, ob)
    keep = target_object(context)
    r = sc.render
    shading = sc.display.shading
    saved = {"engine": r.engine, "x": r.resolution_x, "y": r.resolution_y, "pct": r.resolution_percentage,
             "transparent": r.film_transparent, "camera": sc.camera, "light": shading.light,
             "color": shading.color_type}
    hidden = []
    try:
        r.engine = "BLENDER_WORKBENCH"
        r.resolution_x = r.resolution_y = spec.SPEC["thumbnail"]["render_size"]
        r.resolution_percentage = 100
        r.film_transparent = True
        shading.light = "STUDIO"
        shading.color_type = "TEXTURE"
        sc.camera = cam
        for o in sc.objects:
            if o is not keep and o is not cam and not o.hide_render:
                o.hide_render = True
                hidden.append(o)
        bpy.ops.render.render()
    finally:
        for o in hidden:
            o.hide_render = False
        r.engine, r.resolution_x, r.resolution_y = saved["engine"], saved["x"], saved["y"]
        r.resolution_percentage, r.film_transparent = saved["pct"], saved["transparent"]
        sc.camera = saved["camera"]
        shading.light, shading.color_type = saved["light"], saved["color"]
    return bpy.data.images.get("Render Result")
