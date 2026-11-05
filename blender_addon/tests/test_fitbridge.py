"""FitBridge Blender addon, end to end in a background Blender (run with tests/run.sh): live checks, the ghost box
fit, Send to Studio over the real HTTP bridge, Studio's rules / rig / result POSTs, the thumbnail camera.
Exits non-zero on the first failure."""
import hashlib
import json
import math
import os
import sys
import tomllib
import urllib.request

import bmesh
import bpy
import numpy as np

# a local fake of Roblox Open Cloud: nothing in these tests may reach the real API
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402
import threading  # noqa: E402

OC = {"posts": [], "polls": 0, "fail": False}


class FakeOpenCloud(BaseHTTPRequestHandler):
    def _json(self, code, body):
        d = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(d)))
        self.end_headers()
        self.wfile.write(d)

    def do_POST(self):
        n = int(self.headers["Content-Length"])
        OC["posts"].append({"path": self.path, "headers": dict(self.headers), "body": self.rfile.read(n)})
        if OC["fail"]:
            self._json(400, {"message": "Asset name is moderated"})
        else:
            self._json(200, {"path": "operations/op123abc", "operationId": "op123abc"})

    def do_GET(self):
        OC["polls"] += 1
        OC.setdefault("poll_keys", []).append(self.headers.get("x-api-key"))
        if OC["polls"] % 2:
            self._json(200, {"path": "operations/op123abc", "done": False})
        else:
            self._json(200, {"path": "operations/op123abc", "done": True, "response": {"assetId": "777"}})

    def log_message(self, *a):
        pass


_oc = ThreadingHTTPServer(("127.0.0.1", 0), FakeOpenCloud)
threading.Thread(target=_oc.serve_forever, daemon=True).start()
os.environ["FITBRIDGE_ROBLOX_API"] = f"http://127.0.0.1:{_oc.server_address[1]}"
os.environ["FITBRIDGE_POLL_EVERY"] = "0.05"

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import fitbridge  # noqa: E402
from fitbridge import evaluate, geom, server, spec, upload  # noqa: E402

PORT = int(os.environ["FITBRIDGE_PORT"])
BASE = f"http://127.0.0.1:{PORT}"
done = []


def fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def get(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def post(path, body):
    req = urllib.request.Request(BASE + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


bpy.ops.wm.read_factory_settings(use_empty=True)
fitbridge.register()
ctx = bpy.context
st = ctx.scene.fitbridge

# --- the addon's copy of the spec (extensions must be self-contained) matches the shared one
shared = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "spec", "ugc_spec.json")
if os.path.exists(shared) and json.load(open(shared)) != spec.SPEC:
    fail("blender_addon/fitbridge/ugc_spec.json is out of date: copy spec/ugc_spec.json over it")

# --- geometry basics
if not np.allclose(geom.P @ geom.P.T, np.eye(3)) or abs(np.linalg.det(geom.P) - 1) > 1e-9:
    fail("P must be a rotation")
# the character faces +Y in Blender: Roblox forward is -Z, up is +Y, its right is +X
if not (np.allclose(geom.P @ (0, 1, 0), (0, 0, -1)) and np.allclose(geom.P @ (0, 0, 1), (0, 1, 0))
        and np.allclose(geom.P @ (1, 0, 0), (1, 0, 0))):
    fail(f"Blender +Y must be the character's forward: {geom.P}")
cf = [1, 2, 3, 0, -1, 0, 1, 0, 0, 0, 0, 1]
if geom.matrix_to_cframe(geom.cframe_to_matrix(cf)) != [float(x) for x in cf]:
    fail("cframe round trip")
done.append("P / CFrame encoding")

# --- hello before anything is sent
code, h = get("/v1/hello")
if code != 200 or h["protocol"] != 1 or h["sent_revision"] != 0 or "rules" not in h["needs"]:
    fail(f"hello: {code} {h}")
code, e = get("/v1/package")
if code != 404 or "Send to Studio" not in e.get("error", ""):
    fail(f"package before sending should be a 404 with a sentence: {code} {e}")
with open(os.path.join(os.path.dirname(fitbridge.__file__), "blender_manifest.toml"), "rb") as f:
    manifest_version = tomllib.load(f)["version"]
if h["app_version"] != manifest_version or upload.USER_AGENT != f"FitBridge-Blender/{manifest_version}":
    fail(f"version must come from the manifest ({manifest_version}): {h['app_version']}, {upload.USER_AGENT}")
done.append("/v1/hello answers with the manifest's version, asks for rules; /v1/package 404 until sent")

# --- only Classic is offered (Studio can't load the Rthro meshes; live rules use Classic boxes)
if [i.identifier for i in st.bl_rna.properties["body_scale"].enum_items] != ["Classic"] or st.body_scale != "Classic":
    fail("only Classic should be offered")
fitbridge._fix_scales()                                # a no-op on a valid scene
if st.body_scale != "Classic":
    fail("fix_scales")
done.append("only the Classic body scale is offered")

# --- no FitBridge use in the file (no rig, no picked mesh, no thumbnail view): nothing is checked, so selecting a
#     huge mesh costs nothing; and a huge mesh is never read even when FitBridge is in use
if evaluate.in_use(ctx):
    fail("a fresh file shouldn't count as using FitBridge")
big = bpy.data.meshes.new("Huge")
bmh = bmesh.new()
bmesh.ops.create_grid(bmh, x_segments=400, y_segments=400, size=1.0)       # 160,000 faces
bmh.to_mesh(big)
bmh.free()
huge = bpy.data.objects.new("Huge", big)
ctx.scene.collection.objects.link(huge)
ctx.view_layer.objects.active = huge
import time as _time  # noqa: E402
t0 = _time.perf_counter()
r = evaluate.evaluate(ctx)
dt = _time.perf_counter() - t0
if not r.get("heavy") or r["checks"][0]["status"] != "fail" or "160,000 faces" not in r["checks"][0]["message"] \
        or dt > 0.2:
    fail(f"a huge mesh should be refused at once: {r['checks']} in {dt:.3f} s")
bpy.data.objects.remove(huge)
bpy.data.meshes.remove(big)
done.append("FitBridge idle in files that don't use it; a 160k-face mesh is refused in %.0f ms, not read" % (dt * 1000))

# --- a fresh scene with a stray mesh and no rig: no ghost box yet
from fitbridge import draw  # noqa: E402
bpy.ops.mesh.primitive_cube_add()
stray = ctx.active_object
if draw.ready(ctx):
    fail("no ghost box before a rig is spawned")
bpy.data.objects.remove(stray)
done.append("no ghost box until a rig is spawned and a mesh is picked")

# --- a hat: 1 x 1 x 0.4 box sitting on the stand-in head (the stand-in stands on the ground: HatAttachment at Roblox (0, 5, 0) = Blender (0, 0, 5))
bm = bmesh.new()
bmesh.ops.create_cube(bm, size=1.0)
bmesh.ops.scale(bm, vec=(1, 1, 0.4), verts=bm.verts)
bmesh.ops.translate(bm, vec=(0, 0, 0.2), verts=bm.verts)
me = bpy.data.meshes.new("Hat")
bm.to_mesh(me)
bm.free()
me.uv_layers.new(name="UV")
hat = bpy.data.objects.new("Hat", me)
ctx.scene.collection.objects.link(hat)
hat.location = (-0.3, 0.2, 5.1)                          # Roblox: x -0.3, z -0.2 (in front), bottom 0.1 above head
ctx.view_layer.objects.active = hat
hat.select_set(True)
st.acc_type = "Hat"
st.body_scale = "Classic"
bpy.ops.fitbridge.spawn_r15()
if abs(spec.lowest_point(spec.rig("Classic"))) > 1e-9:
    fail("the stand-in R15 should stand on the ground")
zs = [v.co.z for o in bpy.data.collections["FitBridge R15"].objects for v in o.data.vertices]
if not 0 <= min(zs) < 0.01:
    fail(f"spawned R15 feet at {min(zs)}")
# rig shading like Studio: smooth, with hard edges only on real corners
heads = [o for o in bpy.data.collections["FitBridge R15"].objects if o.name.startswith("UpperTorso")]
me = heads[0].data
if not all(p.use_smooth for p in me.polygons) or len(me.vertices) != 8:
    fail("rig parts are welded and shaded smooth")
if any(e.use_edge_sharp for o in bpy.data.collections["FitBridge R15"].objects for e in o.data.edges):
    fail("the R15 should have no sharp edges at all")
# weighted normals like Roblox's parts: on a bevelled block, the big faces shade perfectly flat
mods = [m for m in heads[0].modifiers if m.type == "WEIGHTED_NORMAL"]
if len(mods) != 1 or mods[0].mode != "FACE_AREA" or not mods[0].keep_sharp:
    fail("rig parts should carry a face-area Weighted Normal modifier")
bm = bmesh.new()
bmesh.ops.create_cube(bm, size=2.0)
bmesh.ops.bevel(bm, geom=list(bm.edges), offset=0.1, segments=1, affect="EDGES")
bmesh.ops.triangulate(bm, faces=bm.faces)
BV = np.array([v.co[:] for v in bm.verts])
BT = np.array([[v.index for v in f.verts] for f in bm.faces])
bm.free()
tmp = bpy.data.collections.new("tmp")
ctx.scene.collection.children.link(tmp)
from fitbridge import rig as rigmod  # noqa: E402
bev = rigmod._part_object(tmp, "Bevel", BV, BT)
ev = bev.evaluated_get(ctx.evaluated_depsgraph_get()).to_mesh()
cn = np.empty(len(ev.loops) * 3)
ev.corner_normals.foreach_get("vector", cn)
cn = cn.reshape(-1, 3)
top = [li for p in ev.polygons if p.normal.z > 0.999 for li in p.loop_indices]
flat = np.abs(cn[top] - [0, 0, 1]).max()
bev.evaluated_get(ctx.evaluated_depsgraph_get()).to_mesh_clear()
for o in list(tmp.objects):
    bpy.data.objects.remove(o)
bpy.data.collections.remove(tmp)
if flat > 1e-3:
    fail(f"weighted normals should keep a bevelled block's big faces flat (off by {flat})")
# the R6 keeps its sharp corners (only the R15 drops them)
bpy.ops.fitbridge.show_rig(kind="R6")
r6t = bpy.data.collections["FitBridge R6 preview"].objects["Torso"].data
if sum(e.use_edge_sharp for e in r6t.edges) != 12:
    fail("the R6 box torso keeps its 12 sharp corners")
bpy.ops.fitbridge.show_rig(kind="R15")
heads = [o for o in bpy.data.collections["FitBridge R15"].objects if o.name.startswith("UpperTorso")]
# R15 / R6 is one toggle: the R6 replaces the R15 in the same place, clicking the shown one hides it
r15box = [v.co.copy() for o in bpy.data.collections["FitBridge R15"].objects for v in o.data.vertices]
bpy.ops.fitbridge.show_rig(kind="R6")
from fitbridge import rig as rigmod  # noqa: E402
if rigmod.shown() != "R6" or bpy.data.collections.get("FitBridge R15") is not None:
    fail("R6 should replace the R15")
r6 = [v.co for o in bpy.data.collections["FitBridge R6 preview"].objects for v in o.data.vertices]
r6c = (max(v.x for v in r6) + min(v.x for v in r6)) / 2
if abs(r6c) > 1e-6 or not 0 <= min(v.z for v in r6) < 0.02:
    fail("the R6 should stand where the R15 stands (centred, feet on the ground)")
bpy.ops.fitbridge.show_rig(kind="R6")
if rigmod.shown() is not None:
    fail("clicking the shown mannequin again hides it")
bpy.ops.fitbridge.show_rig(kind="R15")
if rigmod.shown() != "R15":
    fail("R15 back")
# neighbouring parts are inset by different amounts (display only), so their faces never share a plane
ins = {n: rigmod._inset_for(n) for n in ("LeftUpperArm", "LeftLowerArm", "LeftHand", "UpperTorso", "LowerTorso",
                                         "RightUpperLeg", "RightLowerLeg", "RightFoot", "Head")}
for a, b in (("LeftUpperArm", "LeftLowerArm"), ("LeftLowerArm", "LeftHand"), ("UpperTorso", "LowerTorso"),
             ("UpperTorso", "LeftUpperArm"), ("LowerTorso", "RightUpperLeg"), ("RightUpperLeg", "RightLowerLeg"),
             ("RightLowerLeg", "RightFoot"), ("UpperTorso", "Head")):
    if ins[a] == ins[b]:
        fail(f"{a} and {b} share an inset")
# a Head with no mesh and no shape (rigs synced before Studio sent shapes) still draws as the classic head
V, _T = geom.part_mesh({"name": "Head", "size": [1, 1, 1], "mesh": None})
if len(V) < 100:
    fail("mesh-less Head should fall back to the classic head")
if "Classic" not in server.state["need_rigs"]:
    fail("spawning an uncached scale should ask Studio for that rig")
ctx.view_layer.update()
if not draw.ready(ctx):
    fail("rig + mesh: the ghost box should show")
res = evaluate.evaluate(ctx)
by = {c["id"]: c for c in res["checks"]}
if set(by) != set(spec.CHECK_IDS):
    fail(f"checks {sorted(by)} vs spec {sorted(spec.CHECK_IDS)}")
if by["bounds"]["status"] != "pass" or not res["inside"]:
    fail(f"hat should be inside: {by['bounds']}")
off = res["fit"]["offset"]
if not np.allclose(off, [-0.3, 0.3, -0.2], atol=1e-6) or not np.allclose(res["fit"]["size"], [1, 0.4, 1]):
    fail(f"offset / size in the attachment frame: {off} {res['fit']['size']}")
if by["triangles"]["status"] != "pass" or "12 / 4,000" not in by["triangles"]["message"]:
    fail(f"triangles {by['triangles']}")
if by["floating"]["status"] != "pass":
    fail(f"0.1 gap is under 0.2: {by['floating']}")
if not res["ok"]:
    fail("all checks should pass: " + str(res["checks"]))
done.append("hat inside HatAttachment (Classic), offset (-0.3, 0.3, -0.2) in Roblox space, 12 tris, 0.1 gap ok")

# --- too high and floating
hat.location.z = 7.5
ctx.view_layer.update()
res = evaluate.evaluate(ctx)
by = {c["id"]: c for c in res["checks"]}
if by["bounds"]["status"] != "fail" or not res["over_lines"][0].startswith("Top (Y): 0.90 studs over"):
    fail(f"over the top: {res['over_lines']}")
if by["floating"]["status"] != "warn" or "2.50" not in by["floating"]["message"]:
    fail(f"floating: {by['floating']}")
if res["ok"]:
    fail("a failed bounds check must block")
if bpy.ops.fitbridge.send.poll():
    fail("Send must be disabled while a block check fails")
st.override = True
if not bpy.ops.fitbridge.send.poll():
    fail("override should enable Send")
st.override = False
hat.location.z = 5.1
done.append("too high: 'Top (Y): 0.90 studs over', floating 2.50 warns, Send blocked unless overridden")

# --- modifiers count; materials; textures; UVs
mod = hat.modifiers.new("Sub", "SUBSURF")
mod.levels = 5
ctx.view_layer.update()
res = evaluate.evaluate(ctx)
by = {c["id"]: c for c in res["checks"]}
if by["triangles"]["status"] != "fail":
    fail(f"subsurf 5 = 6144 quads = 12288 tris: {by['triangles']}")
hat.modifiers.remove(mod)


def image(name, w, h, rgba):
    img = bpy.data.images.new(name, w, h, alpha=True)
    px = np.tile(np.array(rgba, np.float32), w * h)
    px[:4] = (1, 0, 0, 1)                                 # bottom-left pixel red: must end up in the LAST row
    img.pixels.foreach_set(px)
    return img


mat = bpy.data.materials.new("HatMat")
mat.use_nodes = True
nt = mat.node_tree
bsdf = nt.nodes["Principled BSDF"]
tc = nt.nodes.new("ShaderNodeTexImage")
tc.image = image("col", 64, 32, (0, 0, 1, 1))
nt.links.new(tc.outputs["Color"], bsdf.inputs["Base Color"])
hat.data.materials.append(mat)
res = evaluate.evaluate(ctx)
by = {c["id"]: c for c in res["checks"]}
if res["pbr"] or by["texture_pow2"]["status"] != "pass" or by["single_material"]["status"] != "pass":
    fail(f"colour only: {res['checks']}")
tn = nt.nodes.new("ShaderNodeTexImage")
tn.image = image("nrm", 48, 48, (0.5, 0.5, 1, 1))
nm = nt.nodes.new("ShaderNodeNormalMap")
nt.links.new(tn.outputs["Color"], nm.inputs["Color"])
nt.links.new(nm.outputs["Normal"], bsdf.inputs["Normal"])
res = evaluate.evaluate(ctx)
by = {c["id"]: c for c in res["checks"]}
if not res["pbr"] or by["texture_pow2"]["status"] != "fail":
    fail(f"normal map = PBR, 48 isn't a power of two: {res['pbr']} {by['texture_pow2']}")
tn.image.scale(64, 64)
mat2 = bpy.data.materials.new("Other")
hat.data.materials.append(mat2)
hat.data.polygons[0].material_index = 1
res = evaluate.evaluate(ctx)
if {c["id"]: c for c in res["checks"]}["single_material"]["status"] != "fail":
    fail("two used materials must fail")
hat.data.polygons[0].material_index = 0
uvd = hat.data.uv_layers.active.data
uvd[0].uv = (1.5, 0.2)
res = evaluate.evaluate(ctx)
if {c["id"]: c for c in res["checks"]}["uv_in_range"]["status"] != "fail":
    fail("UV outside 0-1 must fail")
uvd[0].uv = (1.0, 0.2)
res = evaluate.evaluate(ctx)
if {c["id"]: c for c in res["checks"]}["watertight"]["status"] != "pass" or not res["ok"]:
    fail(f"closed box, all fine again: {res['checks']}")
done.append("subsurf counts, colour vs PBR, power-of-two, two materials, UVs outside 0-1")

# --- edit mode updates
ctx.view_layer.objects.active = hat
hat.select_set(True)
bpy.ops.object.mode_set(mode="EDIT")
ebm = bmesh.from_edit_mesh(hat.data)
for v in ebm.verts:
    v.co.z += 3.0
bmesh.update_edit_mesh(hat.data)
res = evaluate.evaluate(ctx)
if res["inside"]:
    fail("moving vertices up in Edit Mode should fail the bounds live")
for v in ebm.verts:
    v.co.z -= 3.0
bmesh.update_edit_mesh(hat.data)
bpy.ops.object.mode_set(mode="OBJECT")
res = evaluate.evaluate(ctx)
if not res["inside"]:
    fail("back inside after leaving Edit Mode")
done.append("Edit Mode edits update the bounds live")

# --- thumbnail view: side-aware close-up, follows the item, notices your moves, sliders, presets, one shot per item
from fitbridge import thumb  # noqa: E402


def shot():
    r = evaluate.evaluate(ctx)
    thumb.sync(ctx, r)
    H = thumb.handle_frame(r)
    return r, np.linalg.inv(H) @ geom.unscaled(geom.to_roblox(np.array(g.camera.matrix_world)))


st.thumb_view = True
g = hat.fitbridge_thumb
cam = g.camera
if cam is None or not cam.users_scene or ctx.scene.camera != cam:
    fail("Thumbnail view should make (and look through) the hat's own camera")
if ctx.scene.render.resolution_x != 420 or abs(math.degrees(cam.data.angle_y) - 70) > 1e-4:
    fail("70 deg vertical, 420 square")
if not bpy.data.collections["FitBridge R15"].hide_render:
    fail("the mannequin must not render in the thumbnail")
res, L = shot()
rel = L[:3, 3] - np.array(g.pivot)
if not (rel[2] < 0 and rel[0] > 0 and rel[1] > 0) or g.manual:
    fail(f"a hat is shot from the front, a little to the right and above: {rel}")
fwd = -L[:3, 2]
to_pivot = np.array(g.pivot) - L[:3, 3]
if np.linalg.norm(np.cross(fwd, to_pivot)) > 1e-4 or not 0.5 < g.distance < 5:
    fail(f"close-up looking at the attachment area: distance {g.distance}")
st.acc_type = "Back"
res, Lb = shot()
if not (Lb[:3, 3] - np.array(g.pivot))[2] > 0:
    fail("a back item is shot from behind")
st.acc_type = "Hat"
res, L = shot()
hat.location.x += 0.4
res, L2 = shot()
W1 = geom.to_blender(thumb.handle_frame(res)) @ L2
if not np.allclose(np.array(cam.matrix_world)[:3, 3], W1[:3, 3], atol=1e-5) or g.manual:
    fail("until you frame it, the default shot keeps framing the attachment area")
hat.location.x -= 0.4
res, L = shot()
# you move the camera (as viewport navigation does): the sliders pick it up and it stops re-aiming
M = cam.matrix_world.copy()
M.translation += M.col[2].xyz * 1.5                    # dolly back 1.5 studs
cam.matrix_world = M
res, L3 = shot()
if not g.manual or abs(g.distance - (np.linalg.norm(rel) + 1.5)) > 1e-3:
    fail(f"your camera move should become the shot: manual {g.manual}, distance {g.distance}")
if not np.allclose(np.array(cam.matrix_world), np.array(M), atol=1e-4):
    fail("the shot from your move should reproduce your camera exactly")
st.acc_type = "Back"
res, L4 = shot()
if not np.allclose(L3, L4, atol=1e-5):
    fail("once you've framed it yourself, changing the type must not re-aim")
st.acc_type = "Hat"
hat.location.x += 0.4
res, L4b = shot()
if not np.allclose(L3, L4b, atol=1e-5):
    fail("a shot you framed follows the accessory (it's stored relative to the Handle)")
hat.location.x -= 0.4
res, _ = shot()
st.acc_type = "Hat"
g.turn = 90.0                                           # slider
res, L5 = shot()
if abs(math.degrees(math.atan2(L5[0, 2], -L5[2, 2])) - 90) > 1e-3:
    fail("the Turn slider should move the camera")
bpy.ops.fitbridge.thumb_preset(angle="BACK")
if abs(abs(g.turn) - 180) > 1e-6:
    fail("Back preset")
bpy.ops.fitbridge.thumb_preset(angle="ROBLOX")
res, Lr = shot()
want = thumb.roblox_shot(res)
if not np.allclose(Lr, want, atol=1e-5):
    fail("the Roblox preset should reproduce Roblox's UGC Thumbnail Tool default exactly")
d = Lr[:3, 3] / np.linalg.norm(Lr[:3, 3])
if not (d[0] < 0 and d[2] < 0 and d[1] > 0) or abs(np.linalg.norm(Lr[:3, 3]) * thumb.TAN
                                                    - np.linalg.norm(res["fit"]["size"]) / 2) > 1e-5:
    fail("Roblox default: front-left, above, Handle sphere filling the 70 deg view")
man_cf = geom.cframe_to_matrix(thumb.camera_cframe(res, g))
if not np.allclose(man_cf, want, atol=1e-4):
    fail("what's sent should be exactly that shot, relative to the Handle")
bpy.ops.fitbridge.reset_camera()
res, L6 = shot()
if g.manual or not np.allclose(L6, L, atol=1e-5):
    fail("Reset should bring back the default shot")
# deleted camera: unlinked (still remembered) or gone entirely -> back on the next Thumbnail view
for c in list(cam.users_collection):
    c.objects.unlink(cam)
st.thumb_view = False
st.thumb_view = True
if not g.camera.users_scene or ctx.scene.camera != g.camera:
    fail("a camera deleted from the scene should come back")
bpy.data.objects.remove(g.camera)
st.thumb_view = False
st.thumb_view = True
if g.camera is None or not g.camera.users_scene or ctx.scene.camera != g.camera:
    fail("a removed camera should be recreated")
cam = g.camera
res, L7 = shot()
if not np.allclose(L7, L, atol=1e-5):
    fail("a recreated camera keeps the shot")
# the accessory too big for the default close-up gets a warning
hat.scale = (4, 4, 4)
res, _ = shot()
if not thumb.out_of_frame(res, g):
    fail("out of frame warning")
hat.scale = (1, 1, 1)
res, _ = shot()
if thumb.out_of_frame(res, g):
    fail("the default close-up should hold the hat")
# one shot per accessory
other = bpy.data.objects.new("Other", hat.data.copy())
ctx.scene.collection.objects.link(other)
other.location = (0, 0, 5.1)
ctx.view_layer.objects.active = other
res = evaluate.evaluate(ctx)
thumb.sync(ctx, res)
if other.fitbridge_thumb.camera is None or other.fitbridge_thumb.camera == hat.fitbridge_thumb.camera \
        or ctx.scene.camera != other.fitbridge_thumb.camera:
    fail("each accessory should have its own camera, and the view should switch to it")
bpy.data.objects.remove(other.fitbridge_thumb.camera)
bpy.data.objects.remove(other)
ctx.view_layer.objects.active = hat
res, L = shot()
try:
    img = thumb.preview(ctx)
    preview_note = "preview renders" if img is not None else "preview: no image"
except RuntimeError as e:
    preview_note = f"preview not testable in background ({str(e)[:60]})"
if ctx.scene.render.engine == "BLENDER_WORKBENCH" and preview_note == "preview renders":
    pass
done.append("thumbnail: own camera per accessory, hat from front-right-above, back item from behind, follows the item, "
            "your camera moves become the shot (no re-aim after), sliders + presets, Roblox-tool preset exact, reset, deleted camera comes "
            "back, out-of-frame warning; " + preview_note)

# --- send
code, _ = get("/v1/hello")
bpy.ops.fitbridge.send()
code, h = get("/v1/hello")
if h["sent_revision"] != 1 or h["package_revision"] != 1:
    fail(f"sent revision: {h}")
code, man = get("/v1/package")
mock_keys = {"protocol", "revision", "name", "accessory_type", "attachment", "body_scale", "mesh", "textures", "pbr",
             "thumbnail", "checks", "sendable", "override", "spec_source", "upload"}
if code != 200 or set(man) != mock_keys or set(man["mesh"]) != {"sha256", "triangles", "vertices", "size", "offset"}:
    fail(f"manifest keys {sorted(man)}")
if man["accessory_type"] != "Hat" or man["attachment"] != "HatAttachment" or not man["pbr"] or not man["sendable"]:
    fail(f"manifest {man}")
if set(man["textures"]) != {"color", "normal", "roughness", "metalness"} or man["textures"]["roughness"] is not None:
    fail(f"textures {man['textures']}")
if man["upload"]["state"] != "off" or h["upload_state"] != "off" or h["upload_revision"] != 1:
    fail(f"no key = upload off: {man['upload']} {h}")
code, mesh = get("/v1/package/mesh")
Pp = np.array(mesh["positions"]).reshape(-1, 3)
T = np.array(mesh["triangles"]).reshape(-1, 3)
N = np.array(mesh["normals"]).reshape(-1, 3)
lo, hi = Pp.min(0), Pp.max(0)
if np.abs((lo + hi) / 2).max() > 1e-5 or not np.allclose(hi - lo, man["mesh"]["size"], atol=1e-5):
    fail(f"mesh must be centred on origin: {(lo + hi) / 2}")
if len(T) != 12 or len(Pp) != 24 or len(mesh["uvs"]) != 48 or mesh["sha256"] != man["mesh"]["sha256"]:
    fail(f"split per corner: {len(Pp)} verts, {len(T)} tris")
fn = np.cross(Pp[T[:, 1]] - Pp[T[:, 0]], Pp[T[:, 2]] - Pp[T[:, 0]])
if (np.einsum("ij,ij->i", fn, N[T[:, 0]]) <= 0).any():
    fail("winding must agree with the normals (counter-clockwise from the front)")
top = N[:, 1] > 0.99                                   # the top face (Blender +Z) faces Roblox +Y
if top.sum() != 4:
    fail("normals into Roblox space")
code, tex = get("/v1/package/texture/color")
import base64  # noqa: E402
raw = np.frombuffer(base64.b64decode(tex["rgba_base64"]), np.uint8).reshape(tex["height"], tex["width"], 4)
if tex["width"] != 64 or tex["height"] != 32 or tuple(raw[-1, 0]) != (255, 0, 0, 255) or tuple(raw[0, 0]) != (0, 0, 255, 255):
    fail("texture must be top row first")
if not os.path.exists(tex["png_path"]):
    fail("png fallback missing")
code, e = get("/v1/package/texture/roughness")
if code != 404:
    fail("missing map should 404")
# the thumbnail camera, relative to the worn Handle: exactly the shot in the viewport
Cr = geom.cframe_to_matrix(man["thumbnail"]["camera_cframe"])
if not np.allclose(Cr, L, atol=1e-4) or man["thumbnail"]["field_of_view"] != 70:
    fail(f"camera_cframe {man['thumbnail']}")
done.append("Send: manifest matches the mock's fields, mesh centred + split per corner + CCW, "
            "colour texture top row first + PNG, camera_cframe = the viewport shot, relative to the Handle")

# --- Open Cloud upload (fake server): glb, multipart, key only in the header, poll, cache, failure
import struct  # noqa: E402
import time  # noqa: E402
blobs, manifest, wire, pixels = evaluate.build_package(ctx, evaluate.evaluate(ctx), 2, False)
server.publish(blobs, manifest, 2)
KEY = "test-key-not-real"
upload.start(2, manifest, wire, pixels, KEY, "User", "123")
for _ in range(200):
    if upload.public()["state"] != "pending":
        break
    time.sleep(0.05)
code, man2 = get("/v1/package")
up = man2["upload"]
if up["state"] != "done" or up["model_asset_id"] != 777 or up["creator"] != {"type": "User", "id": 123}:
    fail(f"upload: {up}")
h = get("/v1/hello")[1]
if h["upload_state"] != "done" or h["upload_revision"] != 2:
    fail(f"hello upload fields {h}")
if len(OC["posts"]) != 1 or OC["posts"][0]["path"] != "/assets/v1/assets":
    fail(f"one POST to /assets/v1/assets: {[p['path'] for p in OC['posts']]}")
hd = {k.lower(): v for k, v in OC["posts"][0]["headers"].items()}
if hd.get("x-api-key") != KEY or not hd.get("user-agent", "").startswith("FitBridge-Blender") or "@" in hd["user-agent"]:
    fail("key goes in x-api-key only; neutral user agent")
if set(OC["poll_keys"]) != {KEY}:
    fail("polls must carry the key header")
body = OC["posts"][0]["body"]
req = json.loads(body.split(b"application/json\r\n\r\n", 1)[1].split(b"\r\n--", 1)[0])
if req != {"assetType": "Model", "displayName": "Hat", "description": "Made with FitBridge",
           "creationContext": {"creator": {"userId": "123"}}}:
    fail(f"request json {req}")
if KEY.encode() in body:
    fail("the key must not be in the body")
glb = body.split(b"model/gltf-binary\r\n\r\n", 1)[1].rsplit(b"\r\n--", 1)[0]
magic, ver, total = struct.unpack("<III", glb[:12])
jlen, jtype = struct.unpack("<II", glb[12:20])
gl = json.loads(glb[20:20 + jlen])
if magic != 0x46546C67 or ver != 2 or total != len(glb) or jtype != 0x4E4F534A:
    fail("glb header")
binstart = 20 + jlen + 8
acc = gl["accessors"][gl["meshes"][0]["primitives"][0]["attributes"]["POSITION"]]
bv = gl["bufferViews"][acc["bufferView"]]
gpos = np.frombuffer(glb[binstart + bv["byteOffset"]:binstart + bv["byteOffset"] + bv["byteLength"]], np.float32).reshape(-1, 3)
if not np.allclose(gpos, np.array(wire["positions"]).reshape(-1, 3), atol=1e-6):
    fail("glb positions = wire positions, unchanged")
acc = gl["accessors"][gl["meshes"][0]["primitives"][0]["attributes"]["TEXCOORD_0"]]
bv = gl["bufferViews"][acc["bufferView"]]
guv = np.frombuffer(glb[binstart + bv["byteOffset"]:binstart + bv["byteOffset"] + bv["byteLength"]], np.float32).reshape(-1, 2)
wuv = np.array(wire["uvs"]).reshape(-1, 2)
if not np.allclose(guv, wuv, atol=1e-6) or len(gl["images"]) != 2 or "normalTexture" not in gl["materials"][0]:
    fail("glb uvs must equal the wire's (glTF's V starts at the top too) / embedded colour + normal maps")
import tempfile  # noqa: E402
gpath = os.path.join(tempfile.mkdtemp(), "check.glb")
open(gpath, "wb").write(glb)
before = set(bpy.data.objects)
bpy.ops.import_scene.gltf(filepath=gpath)                # an independent reader must accept it
new = [o for o in bpy.data.objects if o not in before and o.type == "MESH"]
if len(new) != 1 or len(new[0].data.polygons) != 12 or len(new[0].data.materials[0].node_tree.nodes) < 3:
    fail(f"Blender's glTF importer should read the glb: {new}")
for o in new:
    bpy.data.objects.remove(o)
ctx.view_layer.objects.active = hat
for txt in (json.dumps(get("/v1/hello")[1]), json.dumps(man2), json.dumps(upload.public())):
    if KEY in txt:
        fail("the key leaked over the bridge")
upload.start(3, manifest, wire, pixels, KEY, "User", "123")
if upload.public()["state"] != "done" or "unchanged" not in upload.public()["message"] or len(OC["posts"]) != 1:
    fail("unchanged mesh + textures must reuse the cached model")
OC["fail"] = True
upload.start(4, manifest, wire, pixels, KEY, "Group", "55")
for _ in range(200):
    if upload.public()["state"] != "pending":
        break
    time.sleep(0.05)
up = upload.public()
if up["state"] != "failed" or up["message"] != "Roblox said 400: Asset name is moderated" or KEY in up["message"]:
    fail(f"failure message {up}")
if json.loads(OC["posts"][-1]["body"].split(b"application/json\r\n\r\n", 1)[1].split(b"\r\n--", 1)[0])["creationContext"] != {"creator": {"groupId": "55"}}:
    fail("group creator")
upload.start(5, manifest, wire, pixels, KEY, "User", "")
if upload.public()["state"] != "failed" or "creator id" not in upload.public()["message"]:
    fail("missing creator id")
upload.start(6, manifest, wire, pixels, "", "User", "123")
if upload.public()["state"] != "off":
    fail("no key = off")
_oc.shutdown()
done.append("Open Cloud (fake): glb straight from the wire arrays (same V as the wire, maps embedded), multipart request, "
            "key only in x-api-key, poll -> model 777 in manifest + hello, cache reuse, Roblox error shown, no key = off")

# --- Studio POSTs: rules, rig, result
# shaped like real Studio output (Studio 0.744): keyed "Hat", Classic-column boxes, 1024 textures, Eyebrow not rigid
cb = spec.bundled_bounds("Hat", "HatAttachment", "Classic")
code, _ = post("/v1/rules", {"protocol": 1, "fetched": "2026-10-31T10:00:00Z",
                             "limits": {"max_triangles": 10, "max_texture_size": 1024},
                             "accessories": {"Hat": {"rigid_allowed": True, "attachments": {
                                 "HatAttachment": {"size": cb["size"], "offset": [0, 0, 0]}}},
                                 "EyebrowAccessory": {"rigid_allowed": False, "attachments": {}}}, "raw": {"x": 1}})
if code != 200 or "rules" in get("/v1/hello")[1]["needs"]:
    fail("rules POST")
fitbridge.process_inbox()
res = evaluate.evaluate(ctx)
by = {c["id"]: c for c in res["checks"]}
if res["bounds_source"] != "live" or by["triangles"]["status"] != "fail" or spec.live_rules()["matched_scale"] != "Classic":
    fail(f"live rules should win: {res['bounds_source']} {by['triangles']} {spec.live_rules().get('matched_scale')}")
# Send shrinks oversized maps: colour only to 1024, PBR to 2048, each capped by the live limit
big = image("big", 2048, 2048, (1, 1, 1, 1))
tc.image = big
res = evaluate.evaluate(ctx)
size = {c["id"]: c for c in res["checks"]}["texture_size"]
if not res["pbr"] or size["status"] != "pass" or "sends as" in size["message"]:
    fail(f"a PBR item keeps 2048 (no live PBR cap known): {size}")
_b, pman, _m, ppix = evaluate.build_package(ctx, res, 900, True)
if pman["textures"]["color"]["width"] != 2048 or ppix["color"].shape[:2] != (2048, 2048):
    fail(f"PBR colour map should stay 2048: {pman['textures']['color']}")
nlink = next(l for l in nt.links if l.to_socket == bsdf.inputs["Normal"])
nt.links.remove(nlink)
res = evaluate.evaluate(ctx)
size = {c["id"]: c for c in res["checks"]}["texture_size"]
if res["pbr"] or size["status"] != "pass" or "color 2048x2048, sends as 1024x1024" not in size["message"]:
    fail(f"colour only over 1024 should pass and say what it sends: {size}")
sblobs, sman, smesh, spix = evaluate.build_package(ctx, res, 901, True)
st_ = sman["textures"]["color"]
stex = json.loads(sblobs["/v1/package/texture/color"])
if (st_["width"], st_["height"]) != (1024, 1024) or spix["color"].shape != (1024, 1024, 4) or \
        st_["sha256"] != hashlib.sha256(spix["color"].tobytes()).hexdigest() or st_["sha256"] == pman["textures"]["color"]["sha256"]:
    fail(f"the manifest must describe the shrunk copy: {st_}")
if (stex["width"], stex["height"], stex["sha256"]) != (1024, 1024, st_["sha256"]) or \
        len(base64.b64decode(stex["rgba_base64"])) != 1024 * 1024 * 4:
    fail("the texture endpoint must serve the shrunk copy")


def png_size(data, at=0):
    at = data.index(b"\x89PNG", at)
    return struct.unpack(">II", data[at + 16:at + 24])


with open(stex["png_path"], "rb") as f:
    if png_size(f.read()) != (1024, 1024):
        fail("the PNG file must be the shrunk copy")
if png_size(upload.write_glb(smesh, spix, sman["pbr"])) != (1024, 1024):
    fail("the uploaded GLB must carry the shrunk copy")
if tuple(big.size) != (2048, 2048) or tuple(big.pixels[:4]) != (1, 0, 0, 1):
    fail("the image in Blender must not change")
nt.links.new(nm.outputs["Normal"], bsdf.inputs["Normal"])
tc.image = bpy.data.images["col"]
bpy.data.images.remove(big)

# which target, live caps, halving
T = {"texture_size_on_send": {"color_only": 1024, "pbr": 2048}}
for lim, pbr, want in ((T, False, 1024), (T, True, 2048), ({**T, "max_texture_size": 512}, False, 512),
                       ({**T, "max_texture_size": 4096}, False, 1024), ({**T, "max_texture_size": 512}, True, 2048),
                       ({**T, "max_pbr_texture_size": 1024}, True, 1024)):
    if geom.send_target(lim, pbr) != want:
        fail(f"send_target {lim} {pbr} should be {want}")
if geom.send_size(4096, 1024, 1024) != (1024, 256) or geom.send_size(512, 512, 1024) != (512, 512) or \
        geom.send_size(4096, 1, 1024) != (1024, 1):
    fail("send_size should halve both sides until the longest fits")
# how each kind of map averages
small = np.zeros((2, 2, 4), np.uint8)
if geom.shrink_rgba(small, "color", 2) is not small:
    fail("a map that fits is passed through untouched")
checker = np.zeros((2, 2, 4), np.uint8)
checker[..., 3] = 255
checker[0, 0, :3] = checker[1, 1, :3] = 255
c = geom.shrink_rgba(checker, "color", 1)[0, 0]
if not 186 <= c[0] <= 189 or c[3] != 255:
    fail(f"black + white colour should average as light (188), not 128: {c}")
if geom.shrink_rgba(checker, "roughness", 1)[0, 0, 0] != 128:
    fail("data maps average plainly")
for col in ((0, 0, 255), (17, 99, 200), (255, 255, 255)):
    solid = np.full((4, 4, 4), 255, np.uint8)
    solid[..., :3] = col
    if tuple(geom.shrink_rgba(solid, "color", 2)[0, 0, :3]) != col:
        fail(f"a solid colour must stay exactly {col}")
fringe = np.zeros((1, 2, 4), np.uint8)
fringe[0, 0] = (255, 0, 0, 255)
fringe[0, 1] = (0, 255, 0, 0)
c = geom.shrink_rgba(fringe, "color", 1)[0, 0]
if c[0] != 255 or c[1] != 0 or c[3] != 128:
    fail(f"colour under transparent pixels mustn't bleed in: {c}")
nrm = np.full((1, 2, 4), 255, np.uint8)
nrm[0, 0, :3] = ((np.array((0.6, 0, 0.8)) + 1) / 2 * 255 + 0.5).astype(np.uint8)
nrm[0, 1, :3] = ((np.array((-0.6, 0, 0.8)) + 1) / 2 * 255 + 0.5).astype(np.uint8)
n = geom.shrink_rgba(nrm, "normal", 1)[0, 0, :3] / 255 * 2 - 1
if abs(np.linalg.norm(n) - 1) > 0.02 or n[2] < 0.98:
    fail(f"a shrunk normal must be unit length: {n}")
flat = np.full((4, 4, 4), 255, np.uint8)
flat[..., :2] = 128
if tuple(geom.shrink_rgba(flat, "normal", 2)[0, 0, :3]) != (128, 128, 255):
    fail("a flat normal map must stay flat")
done.append("Send shrinks: colour only 2048 -> 1024 (check passes and says so; manifest, endpoint, PNG and GLB all 1024; "
            "new sha), PBR stays 2048, live caps win; colour averages as light without fringes, normals stay unit length")
if spec.rigid_allowed("Eyebrow") or "Eyebrow" in spec.RIGID_TYPES or not spec.rigid_allowed("Hat"):
    fail("Eyebrow isn't rigid (spec + live), Hat is")
if "Eyebrow" in [i.identifier for i in st.bl_rna.properties["acc_type"].enum_items]:
    fail("Eyebrow shouldn't be offered")
code, e = post("/v1/rules", {"protocol": 2})
if code != 409 or "Update the other plugin" not in e["error"]:
    fail("protocol mismatch")
server.state["protocol_mismatch"] = None
done.append("live rules (real Studio shape) override limits + boxes, matched to 'Classic', 1024 textures, rigid_allowed; protocol mismatch -> 409")

# --- files from when the character faced -Y: the shot keeps its picture, the mannequin is rebuilt facing +Y
res = evaluate.evaluate(ctx)
g_, _ob = thumb.owner(ctx)
keep_shot = (g_.turn, g_.manual, g_.axes, tuple(g_.pivot))
g_.manual, g_.axes = True, 0
thumb._set(g_, 150.0, g_.tilt, g_.distance, (g_.pan_x, g_.pan_y), (0.1, 0.0, 0.2))
thumb._convert_old_axes(g_)
if abs(g_.turn + 30.0) > 1e-4 or not np.allclose(g_.pivot, (-0.1, 0, -0.2)) or g_.axes != geom.AXES:
    fail(f"an old shot should turn 180: {g_.turn} {tuple(g_.pivot)} {g_.axes}")
thumb._convert_old_axes(g_)
if abs(g_.turn + 30.0) > 1e-4:
    fail("the conversion must only happen once")
thumb._set(g_, keep_shot[0], g_.tilt, g_.distance, (g_.pan_x, g_.pan_y), keep_shot[3])
g_.manual, g_.axes = keep_shot[1], keep_shot[2]
kind_ = rigmod.shown()
coll_ = bpy.data.collections[rigmod.R15_COLL if kind_ == "R15" else rigmod.R6_COLL]
names_ = sorted(o.name for o in coll_.objects)
del coll_["fitbridge_axes"]
rigmod.refresh_old_axes(ctx)
coll_ = bpy.data.collections[rigmod.R15_COLL if kind_ == "R15" else rigmod.R6_COLL]
if coll_.get("fitbridge_axes") != geom.AXES or sorted(o.name for o in coll_.objects) != names_ or rigmod.shown() != kind_:
    fail("an old mannequin should be rebuilt as the same mannequin")
done.append("old files: a framed shot turns 180 once and keeps its picture, the mannequin is rebuilt facing +Y")

# --- Auto mode (fake Anthropic): renders go up, the answer sets everything, one request per mesh, checks still decide
from types import SimpleNamespace  # noqa: E402
from fitbridge import auto  # noqa: E402

AI = {"posts": [], "reply": None, "second": None, "fit": None, "code": 200}


class FakeClaude(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers["Content-Length"])
        AI["posts"].append({"path": self.path, "headers": dict(self.headers), "body": json.loads(self.rfile.read(n))})
        fields = AI["posts"][-1]["body"]["output_config"]["format"]["schema"]["properties"]
        second = "accessory_type" not in fields
        chosen = AI["fit"] if "sits_properly" in fields else AI["second"] if second and AI["second"] is not None else AI["reply"]
        d = json.dumps(chosen).encode()
        self.send_response(AI["code"])
        self.send_header("Content-Length", str(len(d)))
        self.end_headers()
        self.wfile.write(d)

    def log_message(self, *a):
        pass


_ai = ThreadingHTTPServer(("127.0.0.1", 0), FakeClaude)
threading.Thread(target=_ai.serve_forever, daemon=True).start()
auto.API = f"http://127.0.0.1:{_ai.server_address[1]}"
auto.SETTLE = 0.0
fake_prefs = SimpleNamespace(auto_mode=False, claude_api_key="", auto_fit_check=False)
real_prefs = auto.prefs
auto.prefs = lambda: fake_prefs


def said(**over):
    d = {"accessory_type": "Face", "attachment": "FaceFrontAttachment", "name": "Test Mask",
         "summary": "Mask, worn on the front of the face.", "facing": "front", "shot": 3}
    d.update(over)
    return {"stop_reason": "end_turn", "content": [{"type": "thinking", "thinking": ""},
                                                   {"type": "text", "text": json.dumps(d)}]}


def settle(want, seconds=60):
    end = _time.time() + seconds
    while _time.time() < end:
        auto.tick(ctx)
        evaluate.evaluate(ctx)
        if auto.status(ctx)[0] in want:
            return auto.status(ctx)
        _time.sleep(0.05)
    fail(f"Auto mode never reached {want}: {auto.status(ctx)}")


rig_before_auto = rigmod.shown()
keep_auto = (st.target, st.item_name, st.acc_type, st.attachment, ctx.view_layer.objects.active,
             list(ctx.view_layer.objects.selected))
for o in keep_auto[5]:
    o.select_set(False)
mask_me = bpy.data.meshes.new("Mask")
mask_me.from_pydata([(-.2, 0, 5.1), (.2, 0, 5.1), (.2, 0, 5.4), (-.2, 0, 5.4)], [], [(0, 1, 2, 3)])
mask = bpy.data.objects.new("Mask", mask_me)
ctx.scene.collection.objects.link(mask)
mask.select_set(True)
ctx.view_layer.objects.active = mask
ctx.view_layer.update()
st.target, st.item_name = None, ""
if auto.enabled() or evaluate.auto_on():
    fail("Auto mode is off by default")
auto.tick(ctx)
if AI["posts"]:
    fail("with the switch off nothing may go to Anthropic")
fake_prefs.auto_mode = True
auto.tick(ctx)
if auto.status(ctx)[0] != "no_key" or AI["posts"]:
    fail(f"no key: say so, send nothing: {auto.status(ctx)}")
st.override = True
if bpy.ops.fitbridge.send.poll():
    fail("in Auto mode Override must not open Send")
st.override = False
fake_prefs.claude_api_key = "test-claude-key"
AI["reply"] = said()
AI["second"] = {"stop_reason": "end_turn", "content": [{"type": "text", "text": json.dumps({"shot": 3})}]}
kind, line = settle(("ready", "blocked", "failed"))
if kind != "ready" or line != "Mask, worn on the front of the face.":
    fail(f"Auto mode should end ready: {kind} {line}")
if len(AI["posts"]) != 2:
    fail(f"one look at a mesh = 2 requests: {len(AI['posts'])}")
second_look = AI["posts"][1]["body"]["messages"][0]["content"]
if sum(c["type"] == "image" for c in second_look) != len(auto.NUDGES):
    fail("the second look should carry the small moves around the first pick")
ai_post = AI["posts"][0]
content = ai_post["body"]["messages"][0]["content"]
pngs = [base64.b64decode(c["source"]["data"]) for c in content if c["type"] == "image"]
if ai_post["path"] != "/v1/messages" or ai_post["body"]["model"] != "claude-opus-5-5" or len(pngs) != len(auto.ANGLES) or \
        any(not p.startswith(b"\x89PNG") for p in pngs) or len(set(pngs)) < 3:
    fail(f"the request should carry {len(auto.ANGLES)} different PNG shots: {post['path']} {len(pngs)}")
sent_headers = {k.lower(): v for k, v in ai_post["headers"].items()}
if sent_headers.get("x-api-key") != "test-claude-key" or "test-claude-key" in json.dumps(ai_post["body"]) or \
        not sent_headers.get("anthropic-version") or sent_headers.get("anthropic-beta") != "server-side-fallback-2026-07-01":
    fail("the key goes only in x-api-key")
for k in ai_post["body"]["output_config"]["format"]["schema"]["properties"]:
    if k not in ai_post["body"]["output_config"]["format"]["schema"]["required"]:
        fail(f"schema field {k} should be required")
g_ = mask.fitbridge_thumb
if (st.acc_type, st.attachment, st.item_name, st.target) != ("Face", "FaceFrontAttachment", "Test Mask", mask) or \
        abs(g_.turn - 42) > 1e-3 or abs(g_.tilt - 10) > 1e-3 or not g_.manual:      # shot 3 (30, 10), then shot 3 (+12, 0)
    fail(f"the answer should set everything: {st.acc_type} {st.attachment} '{st.item_name}' {g_.turn} {g_.tilt}")
if not rigmod.shown():
    fail("Auto mode needs a mannequin to check against")
# the shot fills the frame through the real lens, centred on the item
evaluate.evaluate(ctx)
thumb.sync(ctx, evaluate.LIVE["result"])
Lm = thumb.local_matrix(g_.turn, g_.tilt, g_.distance, (g_.pan_x, g_.pan_y), g_.pivot)
_c, Vm = thumb.centred_verts(ctx, mask)
Pc = (Vm - Lm[:3, 3]) @ Lm[:3, :3]
nx, ny = Pc[:, 0] / -Pc[:, 2] / thumb.TAN, Pc[:, 1] / -Pc[:, 2] / thumb.TAN
reach = max(np.abs(nx).max(), np.abs(ny).max())
if not 0.84 < reach <= 0.885 or abs(nx.min() + nx.max()) > 0.02 or abs(ny.min() + ny.max()) > 0.02:
    fail(f"framing should centre the item and fill 88%: reach {reach}, x {nx.min()} {nx.max()}, y {ny.min()} {ny.max()}")
stale = json.loads(mask[auto.ANSWER_KEY])
stale["version"] = 1
keep_answer = mask[auto.ANSWER_KEY]
mask[auto.ANSWER_KEY] = json.dumps(stale)
if auto.answer(mask) is not None:
    fail("an answer from before real thumbnails must be asked again")
mask[auto.ANSWER_KEY] = keep_answer
if ctx.scene.camera is not None and ctx.scene.camera.name.startswith("FitBridge Auto") or "FitBridge Auto" in bpy.data.objects:
    fail("the temporary camera must be gone")
if not auto.heads_ups(evaluate.LIVE["result"]):
    fail("an open mesh should still show its heads-up")
if not bpy.ops.fitbridge.send.poll():
    fail("ready = Send on")
for _i in range(3):
    auto.tick(ctx)
mask.location.x += 0.001
ctx.view_layer.update()
auto.tick(ctx)
if len(AI["posts"]) != 2:
    fail("moving the object must not cost another request")
mask.location.x -= 0.001
# the checks still decide: too high = blocked, however confident the answer
mask.location.z += 3.0
ctx.view_layer.update()
kind, line = settle(("blocked",))
if bpy.ops.fitbridge.send.poll():
    fail("a failing check must block Send in Auto mode")
mask.location.z -= 3.0
ctx.view_layer.update()
# a changed mesh asks again; a nonsense attachment falls back to the type's own; a failure is said once, not repeated
mask_me.vertices[0].co.x -= 0.05
mask_me.update()
ctx.view_layer.update()
AI["reply"] = said(accessory_type="Hat", attachment="BodyBackAttachment", shot=999, name="N" * 80)
AI["second"] = {"stop_reason": "refusal", "content": []}       # a failed second look keeps the first pick
st.item_name = ""
settle(("ready", "blocked"))
if len(AI["posts"]) != 4 or st.acc_type != "Hat" or st.attachment != "HatAttachment" or \
        abs(g_.turn - 0) > 1e-3 or abs(g_.tilt - 5) > 1e-3 or len(st.item_name) != 50:
    fail(f"second answer: {len(AI['posts'])} {st.acc_type} {st.attachment} {g_.turn} {g_.tilt} {len(st.item_name)}")
AI["second"] = None
for code, reply, words in ((401, {"error": {}}, "API key"), (529, {"error": {}}, "busy"),
                           (200, {"stop_reason": "refusal", "content": []}, "declined"),
                           (200, {"stop_reason": "max_tokens", "content": []}, "cut off"),
                           (200, {"stop_reason": "end_turn", "content": [{"type": "text", "text": "not json"}]}, "")):
    mask_me.vertices[1].co.x += 0.01
    mask_me.update()
    ctx.view_layer.update()
    AI["code"], AI["reply"] = code, reply
    n_before = len(AI["posts"])
    kind, line = settle(("failed",))
    if words not in line or "test-claude-key" in line:
        fail(f"failure {code}: {line}")
    for _i in range(5):
        auto.tick(ctx)
        _time.sleep(0.02)
    if len(AI["posts"]) != n_before + 1:
        fail("a failure must not keep asking")
    if bpy.ops.fitbridge.send.poll():
        fail("no answer = Send off")
AI["code"], AI["reply"] = 200, said()
AI["second"] = {"stop_reason": "end_turn", "content": [{"type": "text", "text": json.dumps({"shot": 1})}]}
if bpy.ops.fitbridge.auto_retry() != {"FINISHED"}:
    fail("Try again")
settle(("ready", "blocked"))

# backwards check: a turned item is held back, turning it clears that without asking again, and you can overrule it
fitted = lambda ok, why="": {"stop_reason": "end_turn", "content": [{"type": "text", "text": json.dumps({"sits_properly": ok, "problem": why})}]}
mask_me.vertices[1].co.x += 0.01
mask_me.update()
ctx.view_layer.update()
AI["reply"] = said(facing="back")
kind, line = settle(("wrong_way", "failed"))
if kind != "wrong_way" or "backwards" not in line or bpy.ops.fitbridge.send.poll():
    fail(f"a backwards item should be held back: {kind} {line}")
posts_now = len(AI["posts"])
mask.rotation_euler.z = math.pi
ctx.view_layer.update()
keep_z = mask.location.copy()
for _i in range(3):
    auto.tick(ctx)
if auto.wrong_way(mask, auto.answer(mask)) is not None or len(AI["posts"]) != posts_now:
    fail("turning it 180 should clear the warning without another request")
mask.rotation_euler.z = math.pi / 2
ctx.view_layer.update()
if "turned to the character's" not in (auto.wrong_way(mask, auto.answer(mask)) or ""):
    fail(f"a quarter turn should be called out: {auto.wrong_way(mask, auto.answer(mask))}")
mask.rotation_euler.z = 0.0
ctx.view_layer.update()
settle(("wrong_way",))
if bpy.ops.fitbridge.auto_ignore() != {"FINISHED"} or auto.wrong_way(mask, auto.answer(mask)) is not None:
    fail("It's fine should stop the objection")
mask_me.vertices[1].co.x += 0.01
mask_me.update()
ctx.view_layer.update()
AI["reply"] = said(facing="unclear")
settle(("ready", "blocked"))
if auto.facing_now(mask, auto.answer(mask)) is not None:
    fail("no clear front = nothing to object to")
done.append("backwards check: a turned item is held back, rotating it clears that with no new request, you can overrule it, "
            "an item with no clear front is left alone")

# stage two: only with the first switch; on R6; every side with the body; a bad verdict holds Send; moving it asks again
AI["fit"] = fitted(True)
if auto.fit_on():
    fail("off by default")
fake_prefs.auto_fit_check = True
fake_prefs.auto_mode = False
if auto.fit_on():
    fail("the second switch must do nothing without the first")
fake_prefs.auto_mode = True
n_before_fit = len(AI["posts"])
end_fit = _time.time() + 60
while len(AI["posts"]) == n_before_fit and _time.time() < end_fit:
    auto.tick(ctx)
    evaluate.evaluate(ctx)
    _time.sleep(0.05)
kind, line = settle(("ready", "misfit", "failed"))
fit_post = AI["posts"][-1]["body"]
if kind != "ready" or rigmod.shown() != "R6" or "sits_properly" not in fit_post["output_config"]["format"]["schema"]["properties"]:
    fail(f"stage two: {kind} {line} {rigmod.shown()}")
fit_imgs = [base64.b64decode(c["source"]["data"]) for c in fit_post["messages"][0]["content"] if c["type"] == "image"]
if len(fit_imgs) != len(auto.FIT_ANGLES) or len(set(fit_imgs)) < 3:
    fail(f"stage two needs a view from each side: {len(fit_imgs)}")
c_, shots_, parts_ = auto.fit_shots(ctx, mask)
if not parts_ or not all(p.get("fitbridge_rig") for p in parts_):
    fail("the fit shots must include the body")
if any(o.hide_render for o in parts_):
    fail("rendering must put the rig back as it was")
posts_now = len(AI["posts"])
for _i in range(3):
    auto.tick(ctx)
if len(AI["posts"]) != posts_now:
    fail("the same placement must not ask again")
AI["fit"] = fitted(False, "Floating above the head: move it down.")
mask.location.z += 0.02
ctx.view_layer.update()
kind, line = settle(("misfit", "failed"))
if kind != "misfit" or "Floating" not in line or bpy.ops.fitbridge.send.poll() or len(AI["posts"]) != posts_now + 1:
    fail(f"a bad fit should hold Send, with a new request: {kind} {line} {len(AI['posts'])}")
if bpy.ops.fitbridge.auto_ignore() != {"FINISHED"} or auto.status(ctx)[0] != "ready":
    fail("It's fine should let it through")
AI["code"], AI["fit"] = 529, {"error": {}}
mask.location.z -= 0.02
ctx.view_layer.update()
kind, line = settle(("failed",))
if "busy" not in line or bpy.ops.fitbridge.send.poll():
    fail(f"a failed fit check holds Send and says so: {kind} {line}")
AI["code"] = 200
fake_prefs.auto_fit_check = False
done.append("stage two: needs the first switch, R6 from 5 sides with the body in shot, a bad verdict holds Send with Claude's "
            "reason, you can overrule it, moving asks again, the same placement doesn't")

fake_prefs.auto_mode = False
auto.prefs = real_prefs
if rig_before_auto == "R15":
    rigmod.spawn_r15(ctx, st.body_scale)
st.target, st.item_name, st.acc_type, st.attachment = keep_auto[0], keep_auto[1], keep_auto[2], keep_auto[3]
ctx.view_layer.objects.active = keep_auto[4]
bpy.data.objects.remove(mask)
for o in keep_auto[5]:
    o.select_set(True)
_ai.shutdown()
done.append("Auto mode (fake Anthropic): off = nothing sent, no key = says so, real thumbnail candidates + key only in the header, "
            "a second look around the pick, framing fills the frame, answer sets type / attachment / name / shot, one look per mesh, moving doesn't re-ask, checks and Override can't be "
            "got around, bad answers corrected, each failure said once")

# --- live thumbnail: the same camera and mesh as the real shot (background Blender can't draw, so the sums are checked)
res = evaluate.evaluate(ctx)
g_, _ob = thumb.owner(ctx)
vp, toward = draw.live_camera(res, g_)
arr = draw.live_arrays(res)
clip = np.c_[arr["pos"], np.ones(len(arr["pos"]))] @ vp.T
L_ = thumb.local_matrix(g_.turn, g_.tilt, g_.distance, (g_.pan_x, g_.pan_y), g_.pivot)
H_ = thumb.handle_frame(res)
Pr = (np.c_[arr["pos"] @ geom.P.T, np.ones(len(arr["pos"]))] @ np.linalg.inv(H_ @ L_).T)[:, :3]
want_x = Pr[:, 0] / -Pr[:, 2] / thumb.TAN
if not np.allclose(clip[:, 0] / clip[:, 3], want_x, atol=1e-4) or not np.allclose(toward, thumb.expected_world(res, g_)[:3, 2]):
    fail("the live thumbnail must use the thumbnail's own camera and lens")
n_corners = len(res["mesh"]["tri_loops"]) * 3
if any(len(arr[k]) != n_corners for k in ("pos", "normal", "uv")):
    fail("live thumbnail arrays: one entry per triangle corner")
draw.draw_live_thumbnail()
done.append("live thumbnail: same camera, lens and mesh as the real shot")

# --- Send all colours (fake Open Cloud): find the family, name it, upload everything first, then one set for Studio
from fitbridge import batch  # noqa: E402

_oc2 = ThreadingHTTPServer(("127.0.0.1", 0), FakeOpenCloud)     # the first fake was shut down with the upload tests
threading.Thread(target=_oc2.serve_forever, daemon=True).start()
keep_api = upload.API
upload.API = f"http://127.0.0.1:{_oc2.server_address[1]}"
fake_batch = SimpleNamespace(api_key="set-key", creator_type="User", creator_id="5", claude_api_key="")
real_batch_prefs = batch.prefs
batch.prefs = lambda: fake_batch
keep_set = (st.target, st.item_name, st.acc_type, st.attachment, ctx.view_layer.objects.active,
            list(ctx.view_layer.objects.selected))
for o in keep_set[5]:
    o.select_set(False)
st.target, st.item_name, st.acc_type = None, "", "Hat"
hat_me = bpy.data.meshes.new("SetHat")
hat_me.from_pydata([(-.2, -.2, 5.1), (.2, -.2, 5.1), (.2, .2, 5.1), (-.2, .2, 5.1)], [], [(0, 1, 2, 3)])
hat_me.uv_layers.new(name="UV")


def set_material(name, rgb):
    img = bpy.data.images.new(name, 32, 32, alpha=True)
    img.pixels.foreach_set(np.tile(np.array((*rgb, 1), np.float32), 32 * 32))
    mt = bpy.data.materials.new(name)
    mt.use_nodes = True
    node = mt.node_tree.nodes.new("ShaderNodeTexImage")
    node.image = img
    mt.node_tree.links.new(node.outputs["Color"], mt.node_tree.nodes["Principled BSDF"].inputs["Base Color"])
    return mt


def set_object(name, x, rgb):
    o = bpy.data.objects.new(name, hat_me)
    ctx.scene.collection.objects.link(o)
    o.material_slots[0].link = "OBJECT" if o.material_slots else "DATA"
    return o, x, rgb


hat_me.materials.append(None)
members = []
for nm, x, rgb in (("Big_Hat_LOD", 0, (.5, .5, .5)), ("Big_Hat_LOD - Red", 3, (1, 0, 0)), ("Big_Hat_LOD - Deep Sea Blue", 6, (0, 0, 1)),
                   ("Big_Hat_LOD - Red.001", 9, (.8, 0, 0))):
    o, x, rgb = set_object(nm, x, rgb)
    o.material_slots[0].link = "OBJECT"
    o.material_slots[0].material = set_material(nm, rgb)
    o.location.x = x
    members.append(o)
orig_hat = members[0]
ctx.view_layer.update()
members[2].select_set(True)                                 # selecting any colour finds the whole family
ctx.view_layer.objects.active = members[2]
st.target = members[2]
ctx.view_layer.update()
if sorted(o.name for o in batch.family(ctx)) != sorted(o.name for o in members):
    fail(f"family: {[o.name for o in batch.family(ctx)]}")
found, fres, why = batch.find_original(ctx, batch.family(ctx))
if found is not orig_hat or why:
    fail(f"the original is the one on the character: {found} {why}")
for name_in, want in (("Y2K New York_LOD", "Y2K New York"), ("tripo_mesh.001", "tripo mesh"), ("Hat", "Hat")):
    if batch.clean_name(name_in) != want:
        fail(f"clean_name {name_in}: {batch.clean_name(name_in)}")
long_base = "Tan Fur Tactical Hat with Goggles Headset And More"
short = batch.item_name("Black Wash", long_base)
if len(short) > 50 or not short.startswith("Black Wash Tan Fur") or short.endswith(" "):
    fail(f"shortened name: '{short}'")
if batch.item_name("Red", "Big Hat") != "Red Big Hat" or batch.unique(["Red", "Blue", "Red"]) != ["Red", "Blue", "Red 2"]:
    fail("names")
if batch.colour_word(members[1], orig_hat) != "Red" or batch.colour_word(members[3], orig_hat) != "Red":
    fail("colour from the object name")


def finish_set():
    end = _time.time() + 60
    while batch.busy() and _time.time() < end:
        _time.sleep(0.02)
    return batch.public()


# nothing to go on
fake_batch.api_key = ""
if "Open Cloud key" not in (batch.start(ctx) or ""):
    fail("no key should stop it")
fake_batch.api_key = "set-key"
# all or nothing: Roblox refuses, so Studio hears nothing and nothing is built
set_rev_before = server.state["set_revision"]
posts_before = len(OC["posts"])
OC["fail"] = True
if batch.start(ctx) is not None:
    fail("start should go")
got = finish_set()
OC["fail"] = False
if got["state"] != "failed" or "nothing built" not in got["message"] or server.state["set_revision"] != set_rev_before:
    fail(f"a failed upload must build nothing: {got} {server.state['set_revision']}")
if "set-key" in got["message"]:
    fail("the key must never reach the message")
code, e_ = get("/v1/set")
if server.state["set_manifest"] is None and code != 404:
    fail(f"/v1/set before a set: {code}")
# works: uploads each colour, publishes once, Studio's view is complete
OC["posts"].clear()
if batch.start(ctx) is not None:
    fail("start should go")
got = finish_set()
if got["state"] != "done" or got["done"] != 4 or server.state["set_revision"] != set_rev_before + 1:
    fail(f"set: {got}")
if len(OC["posts"]) != 4:
    fail(f"four colours = four uploads: {len(OC['posts'])}")
code, hello = get("/v1/hello")
code, st_set = get("/v1/set")
if code != 200 or hello["set_revision"] != st_set["revision"] or st_set["family"] != "Big_Hat_LOD":
    fail(f"/v1/set: {code} {st_set.get('family')}")
names = [i["name"] for i in st_set["items"]]
if names != ["Big Hat", "Deep Sea Blue Big Hat", "Red Big Hat", "Red 2 Big Hat"] or not st_set["items"][0]["is_original"]:
    fail(f"names and order: {names}")
first = st_set["items"][0]
for it in st_set["items"]:
    if it["upload"]["state"] != "done" or not it["upload"]["model_asset_id"] or set(it["textures"]) != {"color", "normal", "roughness", "metalness"}:
        fail(f"item: {it['name']} {it['upload']}")
    for k in ("accessory_type", "attachment", "body_scale", "mesh", "thumbnail"):
        if it[k] != first[k]:
            fail(f"every colour shares the original's {k}: {it['name']}")
shas = {it["textures"]["color"]["sha256"] for it in st_set["items"]}
if len(shas) != 4:
    fail("each colour has its own texture")
code, sm = get("/v1/set/mesh")
if code != 200 or sm["sha256"] != first["mesh"]["sha256"] or not sm["positions"]:
    fail("the shared mesh")
code, stx = get("/v1/set/item/1/texture/color")
px_ = np.frombuffer(base64.b64decode(stx["rgba_base64"]), np.uint8)
if code != 200 or stx["sha256"] != st_set["items"][1]["textures"]["color"]["sha256"] or px_[2] < 200 or px_[0] > 50:
    fail(f"item 1 is the blue one: {code} {px_[:4]}")
# pressing again: nothing changed, nothing uploads, the set is rebuilt with the same models
OC["posts"].clear()
if batch.start(ctx) is not None:
    fail("start should go")
got = finish_set()
_code, st_again = get("/v1/set")
if got["state"] != "done" or OC["posts"] or [i["upload"]["model_asset_id"] for i in st_again["items"]] != [i["upload"]["model_asset_id"] for i in st_set["items"]]:
    fail(f"an unchanged set must reuse its uploads: {got} {len(OC['posts'])}")
# no colour in the name and no Anthropic key: stops and says how to fix it
members[3].name = "Spare"
msg = batch.start(ctx)
if "no colour in its name" not in (msg or "") or batch.busy():
    fail(f"unnamed colour: {msg}")
# no one on the character
keep_x = orig_hat.location.x
orig_hat.location.x = 20
ctx.view_layer.update()
msg = batch.start(ctx)
if "None of these sit" not in (msg or ""):
    fail(f"nobody fitted: {msg}")
orig_hat.location.x = keep_x
members[1].location.x = 0.01
ctx.view_layer.update()
msg = batch.start(ctx)
if "sit on the character" not in (msg or "") or "Select the original" not in msg:
    fail(f"two fitted: {msg}")
members[1].location.x = 3
ctx.view_layer.update()
batch.prefs = real_batch_prefs
upload.API = keep_api
_oc2.shutdown()
st.target, st.item_name, st.acc_type, st.attachment = keep_set[0], keep_set[1], keep_set[2], keep_set[3]
ctx.view_layer.objects.active = keep_set[4]
for o in members:
    bpy.data.objects.remove(o)
for o in keep_set[5]:
    o.select_set(True)
done.append("Send all colours: finds the family from any colour, the original is the one on the character, names colour + original, "
            "all or nothing, one set for Studio with shared fit, re-send reuses uploads, says what's wrong")

# --- the selected mesh is the accessory: one follows, several ask you to choose, none keeps the last
keep_target, keep_name, keep_active = st.target, st.item_name, ctx.view_layer.objects.active
keep_sel = list(ctx.view_layer.objects.selected)
for o in keep_sel:
    o.select_set(False)
first = bpy.data.objects.new("FirstItem", hat.data)
second = bpy.data.objects.new("SecondItem", hat.data)
ctx.scene.collection.objects.link(first)
ctx.scene.collection.objects.link(second)
first.select_set(True)
ctx.view_layer.update()
evaluate.follow_selection(ctx)
if st.target is not first or evaluate.selection_problem(ctx):
    fail(f"one selected mesh is the accessory: {st.target} {evaluate.selection_problem(ctx)}")
st.item_name = "First name"
second.select_set(True)
evaluate.follow_selection(ctx)
if st.target is not first or evaluate.selection_problem(ctx) != "2 meshes selected: only select one":
    fail(f"two selected: stay and say so: {st.target} {evaluate.selection_problem(ctx)}")
if bpy.ops.fitbridge.send.poll() or bpy.ops.fitbridge.thumb_preview.poll():
    fail("Send and Preview must be off with two meshes selected")
first.select_set(False)
evaluate.follow_selection(ctx)
if st.target is not second or st.item_name != "":
    fail(f"the selection moved on, without the old name: {st.target} '{st.item_name}'")
second.select_set(False)
evaluate.follow_selection(ctx)
if st.target is not second or evaluate.selection_problem(ctx) or not bpy.ops.fitbridge.thumb_preview.poll():
    fail("nothing selected (clicked empty space) keeps the last accessory")
second.hide_set(True)
if evaluate.selection_problem(ctx) != "Select the accessory mesh" or bpy.ops.fitbridge.thumb_preview.poll():
    fail("nothing selected and the last accessory hidden must ask for a mesh")
second.hide_set(False)
rig_part = next(o for o in ctx.scene.objects if o.get("fitbridge_rig") and o.type == "MESH")
second.select_set(True)
rig_part.select_set(True)
if evaluate.selected_meshes(ctx) != [second]:
    fail("rig parts don't count as selected meshes")
rig_part.select_set(False)
second.select_set(False)
first.select_set(True)
evaluate.follow_selection(ctx)
if st.item_name != "First name":
    fail(f"going back should bring the first item's name back: '{st.item_name}'")
first.select_set(False)
st.target = keep_target
st.item_name = keep_name
ctx.view_layer.objects.active = keep_active
for o in (first, second):
    bpy.data.objects.remove(o)
for o in keep_sel:
    o.select_set(True)
done.append("the selected mesh is the accessory: two selected = red message + Send and Preview off, none keeps the last, "
            "a hidden last one asks for a mesh, the rig doesn't count, each item keeps its own Studio name")

head = {"name": "Head", "size": [1.2, 1.2, 1.2], "cframe": [0, 4.6, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1],
        "mesh": {"positions": [-0.6, -0.6, -0.6, 0.6, -0.6, -0.6, 0, 0.6, 0, 0, -0.6, 0.6], "triangles": [0, 1, 2, 0, 2, 3, 0, 3, 1, 1, 3, 2]}}
code, _ = post("/v1/rig", {"protocol": 1, "rig_version": spec.RIG_VERSION, "body_scale": "Classic", "parts": [head],
                           "attachments": [{"name": "HatAttachment", "part": "Head",
                                            "world_cframe": [0, 5.2, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1]}]})
if "rig:Classic" in get("/v1/hello")[1]["needs"]:
    fail("rig need should clear")
fitbridge.process_inbox()
coll = bpy.data.collections["FitBridge R15"]
if coll["fitbridge_source"] != "studio" or len(coll.objects) != 1 or len(coll.objects[0].data.polygons) != 4:
    fail("the box rig should be swapped for Studio's rig")
res = evaluate.evaluate(ctx)
if not np.allclose(res["fit"]["offset"], [-0.3, 0.1, -0.2], atol=1e-6):
    fail(f"offset against the synced attachment: {res['fit']['offset']}")
post("/v1/package/result", {"protocol": 1, "revision": 1, "ok": True, "message": "Built Hat.",
                            "studio_checks": [{"id": "bounds", "status": "pass", "message": "ok"}]})
fitbridge.process_inbox()
if fitbridge._status["last_result"]["message"] != "Built Hat.":
    fail("result")
done.append("rig POST cached + swaps the box rig, attachment frame used; result shown")

# --- the classic head shape, and stale (pre-grounding) cached rigs
leg = {"name": "LeftFoot", "size": [1, 0.3, 1], "cframe": [-0.5, 0.15, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1], "mesh": None}
chead = {"name": "Head", "size": [1, 1, 1], "cframe": [0, 4.5, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1], "mesh": None,
         "shape": {"kind": "classic_head", "scale": [1.2, 1.2, 1.2]}}
post("/v1/rig", {"protocol": 1, "rig_version": spec.RIG_VERSION, "body_scale": "Slender", "parts": [leg, chead], "attachments": []})
fitbridge.process_inbox()
V, T = geom.part_mesh(chead)
ext = V.max(0) - V.min(0)
if not np.allclose(ext, [1.2, 1.2, 1.2], atol=1e-6) or len(V) < 100:
    fail(f"classic head = rounded cylinder of size x scale: {ext} {len(V)}")
a, b, c = V[T[:, 0]], V[T[:, 1]], V[T[:, 2]]
if np.einsum("ij,ij->i", a, np.cross(b, c)).sum() <= 0:
    fail("classic head faces outward")
r = np.hypot(V[:, 0], V[:, 2])
mid = np.abs(V[:, 1]) < 0.3
if not np.allclose(r[mid], 0.6, atol=1e-6) or r[np.abs(V[:, 1]) > 0.599].max() > 0.55:
    fail("straight sides, rounded top / bottom edges")
rigmod.spawn_r15(ctx, "Slender")  # still accepted from Studio, just not offered
heads = [o for o in bpy.data.collections["FitBridge R15"].objects if o.name.startswith("Head")]
if not heads or len(heads[0].data.vertices) < 100:
    fail("the spawned R15 head should be the rounded cylinder")
old = {"body_scale": "Classic", "parts": [{"name": "LeftFoot", "size": [1, 0.3, 1],
                                           "cframe": [-0.5, -2.85, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1], "mesh": None}],
       "attachments": []}
(spec.cache_dir() / "rig_Classic.json").write_text(json.dumps(old))
spec._rigs.pop("Classic", None)
if spec.cached_rig("Classic") is not None:
    fail("a rig with its feet at y = -3 is stale")
st.body_scale = "Classic"
bpy.ops.fitbridge.spawn_r15()
if "rig:Classic" not in get("/v1/hello")[1]["needs"]:
    fail("a stale rig should be asked for again")
st.body_scale = "Classic"
done.append("classic_head drawn as a rounded cylinder (size x scale); rigs below y = -0.05 are stale and re-requested; "
            "R15 stands on the ground; R15 / R6 toggle in one place; per-part insets; mesh-less Head = classic head")

# --- rig_version: older synced rigs still show but are asked for again; the R6 preview gets real meshes
cube = {"positions": [-0.5, -0.5, -0.5, 0.5, -0.5, -0.5, 0, 0.5, 0, 0, -0.5, 0.5], "triangles": [0, 1, 2, 0, 2, 3, 0, 3, 1, 1, 3, 2]}
v1 = {"body_scale": "Classic", "rig_version": spec.RIG_VERSION - 1, "parts": [{"name": "Head", "size": [1, 1, 1], "cframe": [0, 4.5, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1],
                                          "mesh": cube}],
      "attachments": [{"name": "HatAttachment", "part": "Head", "world_cframe": [0, 5, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1]}]}
(spec.cache_dir() / "rig_Classic.json").write_text(json.dumps(v1))
spec._rigs.pop("Classic", None)
with server.lock:
    server.state["need_rigs"].clear()
st.body_scale = "Classic"
bpy.ops.fitbridge.show_rig(kind="R15")
if rigmod.shown() != "R15":
    bpy.ops.fitbridge.show_rig(kind="R15")
coll = bpy.data.collections["FitBridge R15"]
if coll["fitbridge_source"] != "outdated" or len(coll.objects) != 1:
    fail(f"a v1 rig should still show, marked outdated: {coll.get('fitbridge_source')}")
if "rig:Classic" not in get("/v1/hello")[1]["needs"]:
    fail("an outdated rig should be asked for again")
dyn = {"name": "Head", "size": [1.2, 1.2, 1.2], "cframe": [0, 4.6, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1], "mesh": cube}
post("/v1/rig", {"protocol": 1, "rig_version": spec.RIG_VERSION, "body_scale": "Classic", "parts": [dyn, leg],
                 "attachments": v1["attachments"]})
fitbridge.process_inbox()
coll = bpy.data.collections["FitBridge R15"]
if coll["fitbridge_source"] != "studio" or len(coll.objects) != 2 or spec.rig_outdated("Classic"):
    fail("the v2 rig should replace the outdated one")
# R6: boxes until Studio sends the real R6, then swapped in place
bpy.ops.fitbridge.show_rig(kind="R6")
if rigmod.spawned_r6_source() != "standin" or "rig:R6" not in get("/v1/hello")[1]["needs"]:
    fail("R6 boxes until synced, and ask Studio for rig:R6")
r6parts = [{"name": n, "size": [1, 2, 1], "cframe": [x, 1, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1], "mesh": cube}
           for n, x in (("Left Leg", -0.5), ("Right Leg", 0.5))]
r6parts.append({"name": "Head", "size": [2, 1, 1], "cframe": [0, 4.5, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1], "mesh": None,
                "shape": {"kind": "classic_head", "scale": [1.25, 1.25, 1.25]}})
post("/v1/rig", {"protocol": 1, "rig_version": spec.RIG_VERSION, "rig_type": "R6", "body_scale": "R6", "parts": r6parts, "attachments": []})
if "rig:R6" in get("/v1/hello")[1]["needs"]:
    fail("rig:R6 should clear when it arrives")
fitbridge.process_inbox()
c6 = bpy.data.collections["FitBridge R6 preview"]
if rigmod.spawned_r6_source() != "studio" or len(c6.objects) != 3:
    fail(f"the real R6 should replace the boxes: {rigmod.spawned_r6_source()} {len(c6.objects)}")
if not (spec.cache_dir() / "rig_R6.json").exists():
    fail("R6 rig should be cached")
bpy.ops.fitbridge.show_rig(kind="R15")
done.append("older rig_version shown but re-asked, current swaps in; R6 boxes until Studio's real R6 arrives")

# --- off again: the thumbnail cameras hide with it
st.thumb_view = False
if not bpy.data.collections["FitBridge Thumbnails"].hide_viewport:
    fail("the thumbnail camera should hide when Thumbnail view is off")
st.thumb_view = True
if bpy.data.collections["FitBridge Thumbnails"].hide_viewport:
    fail("and show again when it's on")
st.thumb_view = False
if ctx.scene.render.resolution_x == 420 or bpy.data.collections["FitBridge R15"].hide_viewport:
    fail("thumbnail off should restore")
code, e = get("/v1/nothing")
if code != 404 or "error" not in e:
    fail("unknown endpoint")
fitbridge.unregister()
if server.running():
    fail("unregister should stop the server")
if not server.start(PORT):                             # the port is free again (a re-enable can serve)
    fail("server should free the port on unregister")
server.stop()
done.append("thumbnail off restores, unknown endpoint 404, unregister frees the port")
print("PASS")
for d in done:
    print(" -", d)
