# Scene helpers: the R15 mannequin (from the synced rig or the box stand-in), the R6 preview dummy, and the
# thumbnail camera (70 deg vertical FOV, square 420 render, mannequin hidden).

import json
import math

import bpy
import numpy as np

from . import geom, spec

R15_COLL = "FitBridge R15"
R6_COLL = "FitBridge R6 preview"
# Display only: each part shrinks a little toward its centre, by a different amount from its neighbours, so faces of
# touching parts never sit in the same plane and flicker. Checks always use the real sizes.
_INSET_BY_PART = {"Head": 0.002, "UpperTorso": 0.006, "LowerTorso": 0.010,
                  "UpperArm": 0.002, "LowerArm": 0.008, "Hand": 0.004,
                  "UpperLeg": 0.002, "LowerLeg": 0.008, "Foot": 0.004,
                  "Torso": 0.006, "Arm": 0.002, "Leg": 0.010}


def _inset_for(name):
    key = name.replace("Left", "").replace("Right", "").strip()
    return _INSET_BY_PART.get(key, 0.004)
CAM_NAME = "FitBridge Thumbnail"


def _material():
    mat = bpy.data.materials.get("FitBridge Mannequin")
    if mat is None:
        mat = bpy.data.materials.new("FitBridge Mannequin")
        mat.diffuse_color = (0.55, 0.57, 0.6, 1.0)
    return mat


def _clear(name):
    coll = bpy.data.collections.get(name)
    if coll is None:
        return
    for ob in list(coll.objects):
        me = ob.data
        bpy.data.objects.remove(ob)
        if me is not None and me.users == 0:
            bpy.data.meshes.remove(me)
    bpy.data.collections.remove(coll)


def _collection(context, name):
    coll = bpy.data.collections.new(name)
    context.scene.collection.children.link(coll)
    return coll


def _part_object(coll, name, V, T):
    me = bpy.data.meshes.new(name)
    me.from_pydata(np.asarray(V, float).tolist(), [], np.asarray(T, np.int64).reshape(-1, 3).tolist())
    me.validate()
    me.update()
    me.materials.append(_material())
    ob = bpy.data.objects.new(name, me)
    ob["fitbridge_rig"] = True
    ob.hide_select = True
    coll.objects.link(ob)
    return ob


def shown():
    """which mannequin is in the file: "R15", "R6" or None"""
    if bpy.data.collections.get(R15_COLL) is not None:
        return "R15"
    if bpy.data.collections.get(R6_COLL) is not None:
        return "R6"
    return None


def clear_all():
    _clear(R15_COLL)
    _clear(R6_COLL)


def spawn_r15(context, scale):
    """the mannequin for a body scale, in place of whichever mannequin was there (Roblox world -> Blender world),
    unselectable"""
    clear_all()
    data = spec.rig(scale)
    coll = _collection(context, R15_COLL)
    coll["fitbridge_scale"] = scale
    coll["fitbridge_source"] = _source(data, scale)
    _build_parts(coll, data["parts"])
    return coll


def _source(data, key):
    """"standin" (boxes), "outdated" (an older synced rig) or "studio"; the first two get replaced on sync"""
    if data is None or data.get("standin"):
        return "standin"
    return "outdated" if spec.rig_outdated(key) else "studio"


def _build_parts(coll, parts):
    for p in parts:
        V, T = geom.part_mesh(p)
        V = _inset(V, _inset_for(p["name"]))
        C = geom.cframe_to_matrix(p["cframe"])
        Vw = (V @ C[:3, :3].T + C[:3, 3]) @ geom.P.T
        _part_object(coll, p["name"], Vw, T)


def _inset(V, amount):
    """pull a part in by `amount` on every side, toward its own centre"""
    c = (V.max(axis=0) + V.min(axis=0)) / 2
    half = np.maximum((V.max(axis=0) - V.min(axis=0)) / 2, 1e-9)
    return c + (V - c) * np.maximum(half - amount, half * 0.5) / half


def spawned_r15():
    coll = bpy.data.collections.get(R15_COLL)
    if coll is None:
        return None, None
    return coll.get("fitbridge_scale"), coll.get("fitbridge_source")


def spawn_r6(context):
    """the R6 preview, standing where the R15 stands (it replaces it): Studio's real R6 meshes once synced, boxes
    from the spec until then"""
    clear_all()
    coll = _collection(context, R6_COLL)
    data = spec.cached_rig("R6")
    coll["fitbridge_source"] = _source(data, "R6")
    if data is not None:
        _build_parts(coll, data["parts"])
        return coll
    for name, p in spec.SPEC["r6_preview"]["parts"].items():
        V, T = geom.box_mesh(p["size"])
        Vw = (_inset(V, _inset_for(name)) + np.asarray(p["cframe"][:3], float)) @ geom.P.T
        _part_object(coll, name, Vw, T)
    return coll


def spawned_r6_source():
    coll = bpy.data.collections.get(R6_COLL)
    return None if coll is None else coll.get("fitbridge_source")


# ------------------------------------------------------------------------------------------------------- thumbnail

def _mat(m):
    from mathutils import Matrix
    return Matrix(np.asarray(m, float).tolist())


def thumbnail_on(context, res):
    sc = context.scene
    st = sc.fitbridge
    cam = st.thumb_camera
    if cam is None:
        data = bpy.data.cameras.new(CAM_NAME)
        cam = bpy.data.objects.new(CAM_NAME, data)
        sc.collection.objects.link(cam)
        st.thumb_camera = cam
        if res and res.get("fit") is not None:
            cam.matrix_world = _mat(geom.swap(_default(res)))
    cam.data.sensor_fit = "VERTICAL"
    cam.data.angle_y = math.radians(spec.SPEC["thumbnail"]["field_of_view"])
    cam.data.clip_start = 0.05
    r = sc.render
    st.thumb_saved = json.dumps({"x": r.resolution_x, "y": r.resolution_y, "pct": r.resolution_percentage,
                                 "camera": sc.camera.name if sc.camera else ""})
    n = spec.SPEC["thumbnail"]["render_size"]
    r.resolution_x = r.resolution_y = n
    r.resolution_percentage = 100
    sc.camera = cam
    _hide_rigs(True)
    for area in context.screen.areas if context.screen else []:
        if area.type == "VIEW_3D":
            area.spaces.active.region_3d.view_perspective = "CAMERA"
    return cam


def _default(res):
    from .evaluate import default_camera
    return default_camera(res)


def reset_camera(context, res):
    cam = context.scene.fitbridge.thumb_camera
    if cam is not None and res and res.get("fit") is not None:
        cam.matrix_world = _mat(geom.swap(_default(res)))


def thumbnail_off(context):
    sc = context.scene
    st = sc.fitbridge
    try:
        saved = json.loads(st.thumb_saved or "{}")
    except ValueError:
        saved = {}
    r = sc.render
    if saved:
        r.resolution_x, r.resolution_y, r.resolution_percentage = saved["x"], saved["y"], saved["pct"]
        if saved.get("camera") and saved["camera"] in bpy.data.objects:
            sc.camera = bpy.data.objects[saved["camera"]]
    st.thumb_saved = ""
    _hide_rigs(False)
    for area in context.screen.areas if context.screen else []:
        if area.type == "VIEW_3D" and area.spaces.active.region_3d.view_perspective == "CAMERA":
            area.spaces.active.region_3d.view_perspective = "PERSP"


def _hide_rigs(hide):
    for name in (R15_COLL, R6_COLL):
        coll = bpy.data.collections.get(name)
        if coll is not None:
            coll.hide_viewport = hide
            coll.hide_render = hide
