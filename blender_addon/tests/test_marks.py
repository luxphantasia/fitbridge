"""The marks on the mannequins: a simple face on the front of the head (two eyes and a mouth) and a dark stripe down
the back of the head and torso, on the R15 and on the R6, so nobody has to guess which way the dummy looks.

They are extra dark faces lying on the surface (their own objects beside the parts), drawn by rig.py itself. The checks never see them
(they read the rig's own numbers, not the picture of it), a mannequin from an older file gets them when the file is
opened, and Auto mode's fit look is told what the figure looks like.
Runs in a background Blender with FitBridge installed as an extension (tests/run.sh)."""
import sys

import bpy
import numpy as np

from bl_ext.user_default.fitbridge import auto, rig  # noqa: E402

done = []


def fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def marks_of(coll, name):
    """the centres of a part's mark faces, in Blender world (the character looks along +Y)"""
    ob = coll.objects.get("Marks " + name)
    if ob is None:
        return np.zeros((0, 3))
    if [m.name for m in ob.data.materials] != ["FitBridge Mannequin Mark"] or not ob.get("fitbridge_mark"):
        fail(f"{name}: its marks should be flagged as marks and use the mark material")
    m = np.array(ob.matrix_world)
    pts = np.array([list(p.center) for p in ob.data.polygons]).reshape(-1, 3)
    return pts @ m[:3, :3].T + m[:3, 3]


def box(ob):
    m = np.array(ob.matrix_world)
    v = np.array([list(x.co) for x in ob.data.vertices]) @ m[:3, :3].T + m[:3, 3]
    return v.min(axis=0), v.max(axis=0)


bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.preferences.addon_enable(module="bl_ext.user_default.fitbridge")
ctx = bpy.context
if auto.FIGURE_MARKED not in auto.FIGURE_MARKED or rig.marked():
    fail("no mannequin in the file: nothing is marked yet")
if "plain grey blocky figure with no face" not in auto._prompt_fit():
    fail("with no marked mannequin the fit look is told the figure is plain")

for kind, striped in (("R15", ("Head", "UpperTorso", "LowerTorso")), ("R6", ("Head", "Torso"))):
    coll = rig.spawn_r15(ctx, "Classic") if kind == "R15" else rig.spawn_r6(ctx)
    if not rig.marked() or coll.get("fitbridge_marks") != rig.MARKS:
        fail(f"{kind}: the mannequin should say it is marked")
    parts = {o.name: o for o in coll.objects if not o.get("fitbridge_mark")}
    for name, ob in parts.items():
        if len(ob.data.materials) != 1:
            fail(f"{kind} {name}: a part's own mesh stays as it was, with the one material")
        pts = marks_of(coll, name)
        if name not in striped:
            if len(pts):
                fail(f"{kind} {name}: only the head and the torso carry marks")
            continue
        lo, hi = box(ob)
        mid, size = (lo + hi) / 2, hi - lo
        back = pts[pts[:, 1] < mid[1]]
        front = pts[pts[:, 1] > mid[1]]
        if len(back) < 8:
            fail(f"{kind} {name}: no stripe down its back")
        if np.abs(back[:, 0] - mid[0]).max() > rig.STRIPE_HALF + 1e-6:
            fail(f"{kind} {name}: the stripe should run down the middle of the back")
        if back[:, 2].max() - back[:, 2].min() < size[2] * 0.8:
            fail(f"{kind} {name}: the stripe should run the height of the part")
        if back[:, 1].max() > lo[1] + size[1] * 0.35:
            fail(f"{kind} {name}: the stripe belongs on the back (-Y), not round the sides")
        if name != "Head":
            if len(front):
                fail(f"{kind} {name}: the torso's front stays plain")
            continue
        eyes = front[front[:, 2] > mid[2]]
        mouth = front[front[:, 2] < mid[2]]
        left, right = eyes[eyes[:, 0] < mid[0]], eyes[eyes[:, 0] > mid[0]]
        if len(left) < 8 or len(right) < 8 or len(mouth) < 8:
            fail(f"{kind} head: two eyes above the middle and a mouth below it, on the front (+Y)")
        if abs(left[:, 0].mean() + right[:, 0].mean() - 2 * mid[0]) > 0.01:
            fail(f"{kind} head: the eyes should sit either side of the middle")
        if np.abs(eyes[:, 0] - mid[0]).min() < size[0] * 0.08:
            fail(f"{kind} head: the two eyes should be apart")
        if abs(mouth[:, 0].mean() - mid[0]) > 0.01 or mouth[:, 2].max() > lo[2] + size[2] * 0.45:
            fail(f"{kind} head: the mouth should be in the middle of the lower part of the face")
        if front[:, 1].min() < hi[1] - size[1] * 0.35:
            fail(f"{kind} head: the face belongs on the front, not round the sides")
        # every mark lies just off the surface: never sunk in it, never floating away from it
        inside = (pts > lo - rig.MARK_LIFT * 3).all(axis=1) & (pts < hi + rig.MARK_LIFT * 3).all(axis=1)
        if not inside.all():
            fail(f"{kind} head: the marks should lie on the surface")
    if "simple face drawn on the front of its head" not in auto._prompt_fit():
        fail(f"{kind}: the fit look should be told the figure is marked")
    done.append(f"{kind}: a face on the front of the head, a stripe down the back of the head and torso")

# a mannequin from an older file (no marks, or older ones) is rebuilt with them when the file is opened
coll = rig.spawn_r6(ctx)
del coll["fitbridge_marks"]
if rig.marked():
    fail("a mannequin that doesn't say it is marked counts as plain")
rig.refresh_old_axes(ctx)
if not rig.marked() or rig.shown() != "R6":
    fail("an older mannequin should be rebuilt with the marks, as the same mannequin")
coll = rig.spawn_r15(ctx, "Classic")
coll["fitbridge_marks"] = rig.MARKS - 1
rig.refresh_old_axes(ctx)
if not rig.marked() or rig.shown() != "R15":
    fail("a mannequin with older marks should be rebuilt")
before = [o.name for o in bpy.data.collections[rig.R15_COLL].objects]
rig.refresh_old_axes(ctx)
if [o.name for o in bpy.data.collections[rig.R15_COLL].objects] != before:
    fail("a mannequin that is up to date is left alone")
done.append("an older file's mannequin gets the marks when it is opened; an up-to-date one is left alone")

# the marks are the mannequin's alone: unselectable display parts, and both materials are FitBridge's own
for ob in bpy.data.collections[rig.R15_COLL].objects:
    if not ob.get("fitbridge_rig") or not ob.hide_select:
        fail("the marked parts are still mannequin parts: flagged and unselectable")
    if any(not m.name.startswith("FitBridge Mannequin") for m in ob.data.materials):
        fail("the mannequin only uses its own materials")
done.append("the marks stay on the mannequin: flagged as its parts, unselectable, its own two materials")

print("PASS: marks")
for d in done:
    print("  -", d)
