# Scene helpers: the R15 mannequin (from the synced rig or the box stand-in), the R6 preview dummy, and the
# thumbnail camera (70 deg vertical FOV, square 420 render, mannequin hidden).

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


def _smooth(me):
    """shade like Roblox Studio: weld the seams Studio's meshes are split along, then smooth everything except edges
    sharper than SMOOTH_ANGLE"""
    import bmesh
    bm = bmesh.new()
    bm.from_mesh(me)
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-5)
    bm.to_mesh(me)
    bm.free()
    try:
        me.shade_smooth()
        me.set_sharp_from_angle(angle=SMOOTH_ANGLE)
    except AttributeError:                                    # Blender before 4.1
        for poly in me.polygons:
            poly.use_smooth = True


def _collection(context, name):
    coll = bpy.data.collections.new(name)
    context.scene.collection.children.link(coll)
    return coll


SMOOTH_ANGLE = math.radians(60)    # Studio-like shading: soft across bevels, hard on real corners


def _part_object(coll, name, V, T):
    me = bpy.data.meshes.new(name)
    me.from_pydata(np.asarray(V, float).tolist(), [], np.asarray(T, np.int64).reshape(-1, 3).tolist())
    _smooth(me)
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
