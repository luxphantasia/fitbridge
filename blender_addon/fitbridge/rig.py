# Scene helpers: the R15 mannequin (from the synced rig or the box stand-in), the R6 preview dummy, and the
# thumbnail camera (70 deg vertical FOV, square 420 render, mannequin hidden).
# Both mannequins carry marks that say which way they look: a simple face on the front of the head (two eyes and a
# mouth) and a dark stripe down the back of the head and torso. They are drawn here, as dark faces lying on the surface
# (so they show in every viewport mode), each part's marks an object of its own beside it ("Marks Head"); nothing of
# Roblox's own face picture is used. Display only, like the rest of the mannequin: the checks, the thumbnails and what
# is sent to Roblox never see them.

import math

import bpy
import numpy as np
from mathutils import Vector
from mathutils.bvhtree import BVHTree

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


MARKS = 1                                # the marks' version: a mannequin built with another one gets rebuilt
MARK_LIFT = 0.006                        # studs the marks stand off the surface, so they never flicker against it
MARK_CELL = 0.03                         # studs: the marks are cut this fine, so a curved surface never pokes through
MARK_COLOUR = (0.045, 0.045, 0.055, 1.0)
STRIPE_HALF = 0.09                       # studs: half the width of the stripe down the back
_STRIPED = ("Head", "Torso", "UpperTorso", "LowerTorso")


def _material():
    mat = bpy.data.materials.get("FitBridge Mannequin")
    if mat is None:
        mat = bpy.data.materials.new("FitBridge Mannequin")
        mat.diffuse_color = (0.55, 0.57, 0.6, 1.0)
    return mat


def _mark_material():
    mat = bpy.data.materials.get("FitBridge Mannequin Mark")
    if mat is None:
        mat = bpy.data.materials.new("FitBridge Mannequin Mark")
        mat.diffuse_color = MARK_COLOUR
        mat.roughness = 1.0
    return mat


def _disc(cx, cz, rx, rz, around=24):
    """an ellipse in the picture plane: (points, faces), a fan with rings inside it so it can follow a curved surface"""
    rings = max(2, math.ceil(max(rx, rz) / MARK_CELL))
    pts, faces = [(cx, cz)], []
    for r in range(1, rings + 1):
        for k in range(around):
            a = 2 * math.pi * k / around
            pts.append((cx + rx * r / rings * math.cos(a), cz + rz * r / rings * math.sin(a)))
    for k in range(around):
        n = (k + 1) % around
        faces.append((0, 1 + k, 1 + n))
        for r in range(rings - 1):
            a, b = 1 + r * around, 1 + (r + 1) * around
            faces.append((a + k, b + k, b + n, a + n))
    return pts, faces


def _band(x0, x1, z0, z1):
    """an upright rectangle in the picture plane, cut into a fine grid so it can follow a curved surface"""
    across, down = max(2, math.ceil((x1 - x0) / MARK_CELL)), max(2, math.ceil((z1 - z0) / MARK_CELL))
    pts = [(x0 + (x1 - x0) * i / across, z0 + (z1 - z0) * j / down) for j in range(down + 1) for i in range(across + 1)]
    faces = [(j * (across + 1) + i, j * (across + 1) + i + 1, (j + 1) * (across + 1) + i + 1, (j + 1) * (across + 1) + i)
             for j in range(down) for i in range(across)]
    return pts, faces


def _marks(name, V, T):
    """the marks of one part, as faces lying on its surface: (vertices, faces), in Blender world like V. The
    character looks along +Y: the face goes on the head's +Y side, the stripe on the -Y side of the head and torso."""
    if name not in _STRIPED or not len(T):
        return np.zeros((0, 3)), []
    lo, hi = V.min(axis=0), V.max(axis=0)
    w, h = float(hi[0] - lo[0]), float(hi[2] - lo[2])
    cx = float(lo[0] + hi[0]) / 2
    shapes = [(-1.0, _band(cx - STRIPE_HALF, cx + STRIPE_HALF, lo[2] + h * 0.01, hi[2] - h * 0.01))]
    if name == "Head":
        eye_z, mouth_z = lo[2] + h * 0.60, lo[2] + h * 0.32
        shapes += [(1.0, _disc(cx - w * 0.17, eye_z, w * 0.05, h * 0.075)),
                   (1.0, _disc(cx + w * 0.17, eye_z, w * 0.05, h * 0.075)),
                   (1.0, _disc(cx, mouth_z, w * 0.16, h * 0.028))]
    tree = BVHTree.FromPolygons([Vector(v) for v in V.tolist()], np.asarray(T, np.int64).reshape(-1, 3).tolist())
    reach = float(hi[1] - lo[1]) + 2.0
    verts, faces = [], []
    for side, (pts, shape_faces) in shapes:               # side: +1 the front (+Y), -1 the back
        start = float(hi[1] + 1.0) if side > 0 else float(lo[1] - 1.0)
        toward = Vector((0.0, -side, 0.0))
        at = {}
        for i, (x, z) in enumerate(pts):
            loc, normal, _index, _dist = tree.ray_cast(Vector((x, start, z)), toward, reach)
            if loc is None:
                continue                                   # off the edge of the part: that corner isn't drawn
            if normal.dot(toward) > 0:
                normal = -normal
            at[i] = len(verts)
            verts.append(tuple(loc + normal * MARK_LIFT))
        for f in shape_faces:
            if all(i in at for i in f):
                ids = [at[i] for i in f]
                a, b, c = (Vector(verts[ids[0]]), Vector(verts[ids[1]]), Vector(verts[ids[2]]))
                if (b - a).cross(c - a).dot(toward) > 0:   # facing into the part: turn it to face out
                    ids.reverse()
                faces.append(tuple(ids))
    return np.asarray(verts, float).reshape(-1, 3), faces


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


def _smooth(me, sharp=True):
    """shade like Roblox Studio: weld the seams Studio's meshes are split along, then smooth everything except edges
    sharper than SMOOTH_ANGLE"""
    import bmesh
    bm = bmesh.new()
    bm.from_mesh(me)
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-5)
    bm.to_mesh(me)
    bm.free()
    try:
        me.shade_smooth()                                     # also clears every sharp edge
        if sharp:
            me.set_sharp_from_angle(angle=SMOOTH_ANGLE)
    except AttributeError:                                    # Blender before 4.1
        for poly in me.polygons:
            poly.use_smooth = True


def _collection(context, name):
    coll = bpy.data.collections.new(name)
    context.scene.collection.children.link(coll)
    return coll


SMOOTH_ANGLE = math.radians(60)    # Studio-like shading: soft across bevels, hard on real corners (+ weighted normals)


def _part_object(coll, name, V, T, sharp=True):
    me = bpy.data.meshes.new(name)
    me.from_pydata(np.asarray(V, float).tolist(), [], np.asarray(T, np.int64).reshape(-1, 3).tolist())
    _smooth(me, sharp)
    me.validate()
    me.update()
    me.materials.append(_material())
    ob = bpy.data.objects.new(name, me)
    ob["fitbridge_rig"] = True
    ob.hide_select = True
    _weighted_normals(ob)
    coll.objects.link(ob)
    _mark_object(coll, name, V, T)
    return ob


def _mark_object(coll, name, V, T):
    """the marks of one part as their own object beside it ("Marks Head"), so the part's own mesh stays exactly
    Studio's. None for a part that carries no marks."""
    verts, faces = _marks(name, np.asarray(V, float).reshape(-1, 3), T)
    if not faces:
        return None
    me = bpy.data.meshes.new("Marks " + name)
    me.from_pydata(verts.tolist(), [], faces)
    me.validate()
    me.update()
    try:
        me.shade_smooth()
    except AttributeError:                                    # Blender before 4.1
        for poly in me.polygons:
            poly.use_smooth = True
    me.materials.append(_mark_material())
    ob = bpy.data.objects.new("Marks " + name, me)
    ob["fitbridge_rig"] = True                                # part of the mannequin: never an item, never checked
    ob["fitbridge_mark"] = True
    ob.hide_select = True
    coll.objects.link(ob)
    return ob


def _weighted_normals(ob):
    """Roblox's rig parts carry area-weighted normals: big flat faces stay flat and only the narrow bevels curve.
    Blender's Weighted Normal modifier (face area, sharp edges kept) gives the same shading."""
    mod = ob.modifiers.new("Weighted Normals", "WEIGHTED_NORMAL")
    mod.mode = "FACE_AREA"
    mod.weight = 100                                         # the biggest face wins: flat faces stay flat
    mod.keep_sharp = True


def shown():
    """which mannequin is in the file: "R15", "R6" or None"""
    if bpy.data.collections.get(R15_COLL) is not None:
        return "R15"
    if bpy.data.collections.get(R6_COLL) is not None:
        return "R6"
    return None


def marked():
    """True when the mannequin in the file carries the marks (the face in front, the stripe down the back)"""
    kind = shown()
    coll = bpy.data.collections.get(R15_COLL if kind == "R15" else R6_COLL) if kind else None
    return bool(coll is not None and coll.get("fitbridge_marks") == MARKS
                and any(o.get("fitbridge_mark") for o in coll.objects))


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
    coll["fitbridge_axes"] = geom.AXES
    coll["fitbridge_marks"] = MARKS
    _build_parts(coll, data["parts"], sharp=False)            # R15: no sharp edges, weighted normals only (Lux)
    return coll


def _source(data, key):
    """"standin" (boxes), "outdated" (an older synced rig) or "studio"; the first two get replaced on sync"""
    if data is None or data.get("standin"):
        return "standin"
    return "outdated" if spec.rig_outdated(key) else "studio"


def _build_parts(coll, parts, sharp=True):
    for p in parts:
        V, T = geom.part_mesh(p)
        V = _inset(V, _inset_for(p["name"]))
        C = geom.cframe_to_matrix(p["cframe"])
        Vw = (V @ C[:3, :3].T + C[:3, 3]) @ geom.P
        _part_object(coll, p["name"], Vw, T, sharp)


def _inset(V, amount):
    """pull a part in by `amount` on every side, toward its own centre"""
    c = (V.max(axis=0) + V.min(axis=0)) / 2
    half = np.maximum((V.max(axis=0) - V.min(axis=0)) / 2, 1e-9)
    return c + (V - c) * np.maximum(half - amount, half * 0.5) / half


def refresh_old_axes(context):
    """a mannequin built when the character faced the other way in Blender, or before the mannequins had their marks,
    gets rebuilt"""
    kind = shown()
    coll = bpy.data.collections.get(R15_COLL if kind == "R15" else R6_COLL)
    if coll is None or (coll.get("fitbridge_axes") == geom.AXES and coll.get("fitbridge_marks") == MARKS):
        return
    if kind == "R15":
        spawn_r15(context, coll.get("fitbridge_scale") or spec.OFFERED_SCALES[0])
    else:
        spawn_r6(context)


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
    coll["fitbridge_axes"] = geom.AXES
    coll["fitbridge_marks"] = MARKS
    if data is not None:
        _build_parts(coll, data["parts"])
        return coll
    for name, p in spec.SPEC["r6_preview"]["parts"].items():
        V, T = geom.box_mesh(p["size"])
        Vw = (_inset(V, _inset_for(name)) + np.asarray(p["cframe"][:3], float)) @ geom.P
        _part_object(coll, name, Vw, T)
    return coll


def spawned_r6_source():
    coll = bpy.data.collections.get(R6_COLL)
    return None if coll is None else coll.get("fitbridge_source")


# ------------------------------------------------------------------------------------------------------- thumbnail
