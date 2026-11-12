"""The one-click "Reduce to 3,999" button: greyed without LODForge, then with the Modeling Suite installed it replaces
the mesh in place, bakes modifiers, gives colour copies the same mesh, keeps Auto's answer, and puts LODForge's own
settings and your selection back. Runs in a background Blender with FitBridge installed as an extension and the
Modeling Suite copied into the same throwaway profile (tests/run.sh; MODELING_SUITE_BUILD points at its build)."""
import json
import os
import sys

import bmesh
import bpy

import bl_ext.user_default.fitbridge as fb  # noqa: E402,F401
from bl_ext.user_default.fitbridge import auto, evaluate, panel, reduce, spec  # noqa: E402
from bl_ext.user_default.fitbridge import ui as kit  # noqa: E402

done = []


def fail(msg):
    print("FAIL", msg)
    sys.exit(1)


OPS = []


class Lay:
    """a stand-in for Blender's layout that remembers which buttons the panel made"""
    scale_y = 1.0
    enabled = True

    def __getattr__(self, name):
        if name == "operator":
            def operator(idname, *a, **k):
                OPS.append(idname)
                return type("Op", (), {})()
            return operator
        return lambda *a, **k: Lay()


class Ctx:
    region = type("R", (), {"width": 300, "as_pointer": lambda self: 1})()

    def __getattr__(self, name):
        return getattr(bpy.context, name)


def buttons():
    OPS.clear()
    panel.build(kit.Rows(Lay(), Ctx()), Ctx())
    return list(OPS)


def checks():
    evaluate.evaluate(bpy.context)
    return {c["id"]: c for c in evaluate.LIVE["result"]["checks"]}


def tris(ob):
    me = ob.evaluated_get(bpy.context.evaluated_depsgraph_get()).data
    me.calc_loop_triangles()
    return len(me.loop_triangles)


ctx = bpy.context
st = ctx.scene.fitbridge
for o in list(bpy.data.objects):
    bpy.data.objects.remove(o)

# a dense textured helmet-ish dome: ~16k triangles, then a Subdivision modifier takes it to ~64k
bm = bmesh.new()
bm.loops.layers.uv.new("UVMap")
bmesh.ops.create_uvsphere(bm, u_segments=96, v_segments=48, radius=0.5, calc_uvs=True)
me = bpy.data.meshes.new("HelmetMesh")
bm.to_mesh(me)
bm.free()
hat = bpy.data.objects.new("Helmet", me)
ctx.scene.collection.objects.link(hat)
hat.location = (0, 0, 5.4)
g = hat.vertex_groups.new(name="Brim")
g.add([v.index for v in me.vertices if v.co.z < 0], 1.0, "REPLACE")
sub = hat.modifiers.new("Subdivision", "SUBSURF")
sub.levels = 1
copy = bpy.data.objects.new("Helmet Red", me)               # a colour copy: same mesh, its own place
ctx.scene.collection.objects.link(copy)
copy.location = (3, 0, 5.4)
copy.modifiers.new("Subdivision", "SUBSURF").levels = 1

bpy.ops.fitbridge.show_rig(kind="R15")
for o in ctx.view_layer.objects:
    o.select_set(o is hat)
ctx.view_layer.objects.active = hat
evaluate.follow_selection(ctx)
lim = int(spec.limits()["max_triangles"])
before = tris(hat)
if before < 4 * lim or checks()["triangles"]["status"] != "fail":
    fail(f"the test mesh should be far over the limit ({before:,})")

# --- without the Modeling Suite: the button is there, greyed, and says why
if reduce.available() or bpy.ops.fitbridge.reduce_tris.poll():
    fail("without LODForge the button must be off")
if "fitbridge.reduce_tris" not in buttons():
    fail("the failing triangles line should carry the button even when it's greyed")
if reduce.NEEDS != "Needs the Modeling Suite (LODForge) turned on":
    fail("the greyed button should say what it needs")
done.append("no Modeling Suite: button shown greyed, saying it needs LODForge")

# --- turn the Modeling Suite on
bpy.ops.preferences.addon_enable(module="bl_ext.user_default.modeling_suite")
if not reduce.available():
    fail("with the Modeling Suite on, LODForge should be found")
if reduce.label() != "Reduce to 3,999":
    fail(f"label: {reduce.label()}")
s = ctx.scene.lodforge                                     # your own LODForge settings, which must come back
s.target_mode, s.ratio, s.count, s.priority, s.replace, s.clean_mesh = "RATIO", 37.0, 1234, "SHAPE", False, True
s.protect_group = "Mine"

# Auto's answer for this mesh (as if Claude had looked already): it must survive the reduce
fp = auto.fingerprint(hat)
hat[auto.ANSWER_KEY] = json.dumps({"version": auto.ANSWER_VERSION, "fingerprint": fp, "summary": "A helmet"})
hat[auto.IGNORE_FACING_KEY] = fp
fk = auto.fit_key(hat, fp)
hat[auto.FIT_KEY] = json.dumps({"key": fk, "ok": True, "problem": ""})
if "FINISHED" not in bpy.ops.fitbridge.reduce_tris():
    fail("the reduce didn't run")
after = tris(hat)
if not (lim * 0.85 <= after < lim):
    fail(f"expected just under {lim:,}, got {after:,} (from {before:,})")
if hat.data.name != "HelmetMesh" or "HelmetMesh.001" in bpy.data.meshes:
    fail(f"the mesh should keep its name in place: {hat.data.name}, {[m.name for m in bpy.data.meshes]}")
if copy.data is not hat.data:
    fail("the colour copy should get the same reduced mesh")
if hat.modifiers.get("Subdivision") or copy.modifiers.get("Subdivision"):
    fail("the Subdivision should be baked in, on the copy too")
if "Brim" not in hat.vertex_groups or not hat.data.uv_layers:
    fail(f"weights and UVs should be kept: groups {list(hat.vertex_groups.keys())}, uvs {list(hat.data.uv_layers.keys())}")
w = [g.weight for v in hat.data.vertices for g in v.groups if g.group == hat.vertex_groups["Brim"].index]
if len(w) < 50:
    fail(f"the Brim weights should come across ({len(w)} weighted vertices)")
if checks()["triangles"]["status"] != "pass":
    fail("the triangles check should pass now")
if "fitbridge.reduce_tris" in buttons():
    fail("no button once it's under the limit")
done.append(f"reduce: {before:,} → {after:,} in place, Subdivision baked, colour copy shares it, UVs and weights kept")

if (s.target_mode, round(s.ratio), s.count, s.priority, s.replace, s.clean_mesh, s.protect_group) != \
        ("RATIO", 37, 1234, "SHAPE", False, True, "Mine"):
    fail("LODForge's own settings should be put back")
if [o for o in ctx.view_layer.objects.selected] != [hat] or ctx.view_layer.objects.active is not hat:
    fail("the selection should be as it was")
done.append("LODForge's panel settings and the selection are as they were")

if auto.answer(hat) is None or auto.answer(hat)["summary"] != "A helmet":
    fail("Auto's answer should be kept for the reduced mesh")
nfp = auto.fingerprint(hat)
if nfp == fp or hat[auto.IGNORE_FACING_KEY] != nfp or json.loads(hat[auto.FIT_KEY])["key"] != auto.fit_key(hat, nfp):
    fail("Auto's notes should be re-stamped for the new mesh")
done.append("Auto mode's answer and its overrules carried over (no second look)")

# --- already under: nothing happens
if "CANCELLED" not in bpy.ops.fitbridge.reduce_tris():
    fail("under the limit the button should do nothing")
done.append("already under the limit: nothing changes")

print("\n".join("ok  " + d for d in done))
print(f"PASS {len(done)} reduce checks")
sys.stdout.flush()
os._exit(0)
