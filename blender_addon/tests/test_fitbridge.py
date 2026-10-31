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

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import fitbridge  # noqa: E402
from fitbridge import evaluate, geom, server, spec  # noqa: E402

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

# --- a hat: 1 x 1 x 0.4 box sitting on the stand-in head (HatAttachment at Roblox (0, 2, 0) = Blender (0, 0, 2))
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
hat.location = (0.3, -0.2, 2.1)                          # Roblox: x -0.3, z -0.2 (in front), bottom 0.1 above head
ctx.view_layer.objects.active = hat
st.acc_type = "Hat"
st.body_scale = "Normal"
bpy.ops.fitbridge.spawn_r15()
if "Normal" not in server.state["need_rigs"]:
    fail("spawning an uncached scale should ask Studio for that rig")
ctx.view_layer.update()
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
hat.location.z = 3.5
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
hat.location.z = 2.1
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
             "thumbnail", "checks", "sendable", "override", "spec_source"}
if code != 200 or set(man) != mock_keys or set(man["mesh"]) != {"sha256", "triangles", "vertices", "size", "offset"}:
    fail(f"manifest keys {sorted(man)}")
if man["accessory_type"] != "Hat" or man["attachment"] != "HatAttachment" or not man["pbr"] or not man["sendable"]:
    fail(f"manifest {man}")
if set(man["textures"]) != {"color", "normal", "roughness", "metalness"} or man["textures"]["roughness"] is not None:
    fail(f"textures {man['textures']}")
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

# --- Studio POSTs: rules, rig, result
nb = spec.bundled_bounds("Hat", "HatAttachment", "Normal")
code, _ = post("/v1/rules", {"protocol": 1, "fetched": "2026-10-31T10:00:00Z", "limits": {"max_triangles": 10},
                             "accessories": {"HatAccessory": {"rigid_allowed": True, "attachments": {
                                 "HatAttachment": {"size": nb["size"], "offset": [0, 0, 0]}}}}, "raw": {"x": 1}})
if code != 200 or "rules" in get("/v1/hello")[1]["needs"]:
    fail("rules POST")
fitbridge.process_inbox()
res = evaluate.evaluate(ctx)
by = {c["id"]: c for c in res["checks"]}
if res["bounds_source"] != "live" or by["triangles"]["status"] != "fail" or spec.live_rules()["matched_scale"] != "Normal":
    fail(f"live rules should win: {res['bounds_source']} {by['triangles']} {spec.live_rules().get('matched_scale')}")
if "live from Studio, 2026-10-31" not in spec.rules_text():
    fail(spec.rules_text())
code, e = post("/v1/rules", {"protocol": 2})
if code != 409 or "Update the other plugin" not in e["error"]:
    fail("protocol mismatch")
server.state["protocol_mismatch"] = None
done.append("live rules override limits + boxes, matched to the 'Normal' column; protocol mismatch -> 409")

head = {"name": "Head", "size": [1.2, 1.2, 1.2], "cframe": [0, 1.6, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1],
        "mesh": {"positions": [-0.6, -0.6, -0.6, 0.6, -0.6, -0.6, 0, 0.6, 0, 0, -0.6, 0.6], "triangles": [0, 1, 2, 0, 2, 3, 0, 3, 1, 1, 3, 2]}}
code, _ = post("/v1/rig", {"protocol": 1, "body_scale": "Normal", "parts": [head],
                           "attachments": [{"name": "HatAttachment", "part": "Head",
                                            "world_cframe": [0, 2.2, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1]}]})
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
