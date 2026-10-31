"""FitBridge Blender addon, end to end in a background Blender (run with tests/run.sh): live checks, the ghost box
fit, Send to Studio over the real HTTP bridge, Studio's rules / rig / result POSTs, the thumbnail camera.
Exits non-zero on the first failure."""
import json
import math
import os
import sys
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
if not np.allclose(geom.P @ geom.P, np.eye(3)) or abs(np.linalg.det(geom.P) - 1) > 1e-9:
    fail("P must be a rotation and its own inverse")
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
done.append("/v1/hello answers, asks for rules; /v1/package 404 until sent")

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
hat.location = (0.3, -0.2, 5.1)                          # Roblox: x -0.3, z -0.2 (in front), bottom 0.1 above head
ctx.view_layer.objects.active = hat
st.acc_type = "Hat"
st.body_scale = "Normal"
bpy.ops.fitbridge.spawn_r15()
if abs(spec.lowest_point(spec.rig("Normal"))) > 1e-9:
    fail("the stand-in R15 should stand on the ground")
zs = [v.co.z for o in bpy.data.collections["FitBridge R15"].objects for v in o.data.vertices]
if not 0 <= min(zs) < 0.01:
    fail(f"spawned R15 feet at {min(zs)}")
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
if "Normal" not in server.state["need_rigs"]:
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
done.append("hat inside HatAttachment (Normal), offset (-0.3, 0.3, -0.2) in Roblox space, 12 tris, 0.1 gap ok")

# --- too high and floating
hat.location.z = 6.5
ctx.view_layer.update()
res = evaluate.evaluate(ctx)
by = {c["id"]: c for c in res["checks"]}
if by["bounds"]["status"] != "fail" or not res["over_lines"][0].startswith("Top (Y): 0.65 studs over"):
    fail(f"over the top: {res['over_lines']}")
if by["floating"]["status"] != "warn" or "1.50" not in by["floating"]["message"]:
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
done.append("too high: 'Top (Y): 0.65 studs over', floating 1.50 warns, Send blocked unless overridden")

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

# --- thumbnail view
st.thumb_view = True
cam = st.thumb_camera
if cam is None or ctx.scene.render.resolution_x != 420 or abs(math.degrees(cam.data.angle_y) - 70) > 1e-4:
    fail("thumbnail camera / render size")
if not bpy.data.collections["FitBridge R15"].hide_viewport:
    fail("mannequin should hide in thumbnail view")
Hb = geom.swap(evaluate.handle_frame(res))[:3, 3]
cpos = np.array(cam.matrix_world.translation)
if abs(np.linalg.norm(cpos - Hb) - 6) > 1e-4 or cpos[1] > Hb[1] - 5.9:
    fail(f"camera 6 studs in front (Blender -Y) of the handle: {cpos} vs {Hb}")
fwd = -np.array(cam.matrix_world.col[2][:3])
if np.dot(fwd, (Hb - cpos) / 6) < 0.9999:
    fail("camera should look at the handle")
done.append("thumbnail view: 70 deg vertical, 420 square, rig hidden, camera 6 studs in front looking at the Handle")

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
# thumbnail camera relative to the worn handle = 6 studs in front (-Z) looking at it
Cr = geom.cframe_to_matrix(man["thumbnail"]["camera_cframe"])
if not np.allclose(Cr[:3, 3], [0, 0, -6], atol=1e-4) or man["thumbnail"]["field_of_view"] != 70:
    fail(f"camera_cframe {man['thumbnail']}")
if not np.allclose(Cr[:3, 2], [0, 0, -1], atol=1e-4):         # camera looks down -Z = toward +Z: its back is -Z
    fail(f"camera orientation {Cr[:3, :3]}")
done.append("Send: manifest matches the mock's fields, mesh centred + split per corner + CCW, "
            "colour texture top row first + PNG, camera_cframe (0, 0, -6) relative to the Handle")

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
if not np.allclose(guv[:, 1], 1 - wuv[:, 1], atol=1e-6) or len(gl["images"]) != 2 or "normalTexture" not in gl["materials"][0]:
    fail("glb uv flip / embedded colour + normal maps")
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
done.append("Open Cloud (fake): glb straight from the wire arrays (V flipped, maps embedded), multipart request, "
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
big = image("big", 2048, 2048, (1, 1, 1, 1))
tc.image = big
res = evaluate.evaluate(ctx)
if {c["id"]: c for c in res["checks"]}["texture_size"]["status"] != "fail":
    fail("a 2048 texture must fail against the live 1024 limit")
tc.image = bpy.data.images["col"]
bpy.data.images.remove(big)
if spec.rigid_allowed("Eyebrow") or "Eyebrow" in spec.RIGID_TYPES or not spec.rigid_allowed("Hat"):
    fail("Eyebrow isn't rigid (spec + live), Hat is")
if "Eyebrow" in [i.identifier for i in st.bl_rna.properties["acc_type"].enum_items]:
    fail("Eyebrow shouldn't be offered")
code, e = post("/v1/rules", {"protocol": 2})
if code != 409 or "Update the other plugin" not in e["error"]:
    fail("protocol mismatch")
server.state["protocol_mismatch"] = None
done.append("live rules (real Studio shape) override limits + boxes, matched to 'Classic', 1024 textures, rigid_allowed; protocol mismatch -> 409")

head = {"name": "Head", "size": [1.2, 1.2, 1.2], "cframe": [0, 4.6, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1],
        "mesh": {"positions": [-0.6, -0.6, -0.6, 0.6, -0.6, -0.6, 0, 0.6, 0, 0, -0.6, 0.6], "triangles": [0, 1, 2, 0, 2, 3, 0, 3, 1, 1, 3, 2]}}
code, _ = post("/v1/rig", {"protocol": 1, "body_scale": "Normal", "parts": [head],
                           "attachments": [{"name": "HatAttachment", "part": "Head",
                                            "world_cframe": [0, 5.2, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1]}]})
if "rig:Normal" in get("/v1/hello")[1]["needs"]:
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
post("/v1/rig", {"protocol": 1, "body_scale": "Slender", "parts": [leg, chead], "attachments": []})
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
st.body_scale = "Slender"
bpy.ops.fitbridge.spawn_r15()
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
st.body_scale = "Normal"
done.append("classic_head drawn as a rounded cylinder (size x scale); rigs below y = -0.05 are stale and re-requested; "
            "R15 stands on the ground; R15 / R6 toggle in one place; per-part insets; mesh-less Head = classic head")

# --- off again
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
