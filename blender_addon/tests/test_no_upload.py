"""The upload lock (FITBRIDGE_NO_UPLOAD) and the build-only set, against a local fake of Roblox Open Cloud.

With the switch set, nothing in FitBridge may reach Open Cloud: not Send, not Send all colours, not a direct call into
upload.py, not even a model that is already in the upload cache. The fake counts every request it gets, and that count
has to stay at zero. batch.build_set still builds the whole set (mesh, textures, a .glb per colour) with no `upload`
block anywhere. Without the switch the same calls do upload, which proves it is the switch that stops them.
Runs in a background Blender with FitBridge installed as an extension (tests/run.sh)."""
import hashlib
import json
import os
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import bpy
import numpy as np

from bl_ext.user_default.fitbridge import batch, evaluate, host, server, upload  # noqa: E402

HITS = []


class FakeOpenCloud(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, body):
        d = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(d)))
        self.end_headers()
        self.wfile.write(d)

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        HITS.append(("POST", self.path))
        self._json({"operationId": "op1"})

    def do_GET(self):
        HITS.append(("GET", self.path))
        self._json({"done": True, "response": {"assetId": "4242"}})


srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeOpenCloud)
threading.Thread(target=srv.serve_forever, daemon=True).start()
upload.API = f"http://127.0.0.1:{srv.server_address[1]}"
upload.POLL_EVERY = 0.02
done = []


def fail(msg):
    print("FAIL", msg)
    sys.stdout.flush()
    os._exit(1)


def settle(what, seconds=20):
    end = time.time() + seconds
    while what() and time.time() < end:
        time.sleep(0.02)


ctx = bpy.context
st = ctx.scene.fitbridge
p = host.prefs()
for o in list(bpy.data.objects):
    bpy.data.objects.remove(o)

# a small hat on the stand-in head, with a colour map and a normal map, and two recolours sharing its mesh
me = bpy.data.meshes.new("LockHat")
me.from_pydata([(-.3, -.3, 5.0), (.3, -.3, 5.0), (.3, .3, 5.0), (-.3, .3, 5.0), (0, 0, 5.4)], [],
               [(0, 1, 2, 3), (0, 1, 4), (1, 2, 4), (2, 3, 4), (3, 0, 4)])
uv = me.uv_layers.new(name="UV")
for i, loop in enumerate(me.loops):
    uv.data[i].uv = ((i % 4) / 4 + 0.05, (i % 3) / 3 + 0.05)
me.materials.append(None)


def material(name, rgb, normal=True):
    mt = bpy.data.materials.new(name)
    mt.use_nodes = True
    bsdf = mt.node_tree.nodes["Principled BSDF"]
    img = bpy.data.images.new(name, 2048, 2048, alpha=True)
    img.pixels.foreach_set(np.tile(np.array((*rgb, 1), np.float32), 2048 * 2048))
    node = mt.node_tree.nodes.new("ShaderNodeTexImage")
    node.image = img
    mt.node_tree.links.new(node.outputs["Color"], bsdf.inputs["Base Color"])
    if normal:
        nimg = bpy.data.images.new(name + " n", 64, 64, alpha=True)
        nimg.colorspace_settings.name = "Non-Color"
        nimg.pixels.foreach_set(np.tile(np.array((.5, .5, 1, 1), np.float32), 64 * 64))
        nnode = mt.node_tree.nodes.new("ShaderNodeTexImage")
        nnode.image = nimg
        nmap = mt.node_tree.nodes.new("ShaderNodeNormalMap")
        mt.node_tree.links.new(nnode.outputs["Color"], nmap.inputs["Color"])
        mt.node_tree.links.new(nmap.outputs["Normal"], bsdf.inputs["Normal"])
    return mt


objs = []
for name, x, rgb in (("Lock Hat", 0, (.5, .5, .5)), ("Lock Hat - Red", 4, (1, 0, 0)), ("Lock Hat - Blue", 8, (0, 0, 1))):
    o = bpy.data.objects.new(name, me)
    ctx.scene.collection.objects.link(o)
    o.material_slots[0].link = "OBJECT"
    o.material_slots[0].material = material(name, rgb)
    o.location.x = x
    objs.append(o)
hat, red, blue = objs
ctx.view_layer.update()
hat.select_set(True)
ctx.view_layer.objects.active = hat
st.target, st.acc_type, st.attachment = hat, "Hat", "HatAttachment"
res = evaluate.evaluate(ctx)
if not res["ok"] or res.get("fit") is None:
    fail(f"the test hat should pass the checks: {[c for c in res['checks'] if c['status'] != 'pass']}")
blobs, manifest, wire, pixels = evaluate.build_package(ctx, res, 1, False)
creator = {"type": "User", "id": 7}
ckey = upload.cache_key(manifest, creator)

# --- unlocked first: the same calls really do reach Open Cloud (so it is the switch that stops them below)
os.environ.pop("FITBRIDGE_NO_UPLOAD", None)
if upload.locked():
    fail("no switch set = not locked")
upload.start(1, manifest, wire, pixels, "a-key", "User", "7")
settle(lambda: upload.public()["state"] == "pending")
if upload.public()["state"] != "done" or upload.public()["model_asset_id"] != 4242 or not HITS:
    fail(f"unlocked, the upload should go through the fake: {upload.public()} {HITS}")
done.append("without the switch the upload reaches (the fake) Open Cloud")

# --- locked: every way in refuses, and the fake hears nothing at all
for value in ("1", "true", "yes"):
    os.environ["FITBRIDGE_NO_UPLOAD"] = value
    if not upload.locked():
        fail(f"FITBRIDGE_NO_UPLOAD={value} should lock")
for value in ("", "0", "false", "off"):
    os.environ["FITBRIDGE_NO_UPLOAD"] = value
    if upload.locked():
        fail(f"FITBRIDGE_NO_UPLOAD={value!r} should not lock")
os.environ["FITBRIDGE_NO_UPLOAD"] = "1"
HITS.clear()
threads_before = {t.name for t in threading.enumerate()}

upload.start(2, manifest, wire, pixels, "a-key", "User", "7")          # this model is in the upload cache by now
up = upload.public()
if up["state"] != "off" or up["message"] != upload.LOCKED or up["model_asset_id"] is not None:
    fail(f"locked Send must read 'off' with the reason, and hand out no model: {up}")
for call in (lambda: upload.upload_one("a-key", creator, "Lock Hat", wire, pixels, True, ckey),
             lambda: upload.upload_one("a-key", creator, "Lock Hat", wire, pixels, True, "a-new-cache-key"),
             lambda: upload._upload("a-key", creator, "Lock Hat", wire, pixels, True, "another-key"),
             lambda: upload._request("POST", upload.API + "/assets/v1/assets", "a-key", b"x"),
             lambda: upload._request("GET", upload.API + "/assets/v1/operations/op1", "a-key")):
    try:
        call()
        fail("a locked upload call must raise")
    except upload.OpenCloudError as e:
        if str(e) != upload.LOCKED:
            fail(f"the refusal should say why: {e}")
# the Send button itself: the package is built and published for Studio's preview, the upload stays off
p.api_key, p.creator_id = "a-key", "7"
if bpy.ops.fitbridge.send() != {"FINISHED"}:
    fail("Send should still build the package")
if upload.public()["state"] != "off" or upload.public()["message"] != upload.LOCKED:
    fail(f"Send under the lock: {upload.public()}")
# Send all colours: refuses before it reads anything
set_before = server.state["set_revision"]
msg = batch.start(ctx)
if msg != upload.LOCKED or batch.busy() or server.state["set_revision"] != set_before:
    fail(f"Send all colours under the lock: {msg!r}")
time.sleep(0.3)                                                          # anything a stray thread would have sent
if HITS:
    fail(f"the lock let requests through: {HITS}")
if {t.name for t in threading.enumerate()} - threads_before:
    fail(f"the lock must not even start an upload thread: {[t.name for t in threading.enumerate()]}")
done.append("FITBRIDGE_NO_UPLOAD: Send, Send all colours, upload_one (cached or not), _upload and the raw request all "
            "refuse with the reason, no thread starts, and the fake Open Cloud hears nothing")

# --- the build-only set: everything Send all colours would make, with no upload block and no request
res = evaluate.evaluate(ctx, hat, publish=False)
out = batch.build_set(ctx, hat, [red, blue], res, 5, colour_only=True)
man = out["manifest"]
if set(man) != {"protocol", "revision", "family", "name", "items"} or man["revision"] != 5 or man["family"] != "Lock Hat":
    fail(f"the set's description: {sorted(man)}")
if [i["name"] for i in man["items"]] != ["Lock Hat", "Red Lock Hat", "Blue Lock Hat"] or \
        [i["colour"] for i in man["items"]] != [None, "Red", "Blue"] or not man["items"][0]["is_original"]:
    fail(f"names, colours and order: {[(i['name'], i['colour']) for i in man['items']]}")
want_keys = {"protocol", "revision", "name", "accessory_type", "attachment", "body_scale", "mesh", "textures", "pbr",
             "thumbnail", "checks", "sendable", "override", "spec_source", "colour", "is_original"}
for it in man["items"]:
    if set(it) != want_keys:
        fail(f"an item is a /v1/package manifest plus colour and is_original, and no upload: {sorted(set(it) ^ want_keys)}")
    if it["pbr"] or it["textures"]["normal"] is not None or it["textures"]["color"]["width"] != 1024:
        fail(f"colour only: one colour map at 1024, no other maps: {it['textures']} pbr={it['pbr']}")
    for k in ("accessory_type", "attachment", "body_scale", "mesh", "thumbnail"):
        if it[k] != man["items"][0][k]:
            fail(f"every colour shares the original's {k}")
if "upload" in json.dumps(man) or "a-key" in json.dumps(man):
    fail("no upload block and no key anywhere in the set")
mesh = json.loads(out["mesh"])
if mesh["sha256"] != man["items"][0]["mesh"]["sha256"] or mesh["revision"] != 5 or len(mesh["triangles"]) != 18 or \
        hashlib.sha256(json.dumps({k: mesh[k] for k in ("positions", "normals", "uvs", "triangles")}).encode()).hexdigest() != mesh["sha256"]:
    fail("the shared mesh is /v1/set/mesh's JSON, and its sha256 is of the mesh itself")
for it, entry in zip(out["items"], man["items"]):
    rgba = it["pixels"]["color"]
    if rgba.shape != (1024, 1024, 4) or hashlib.sha256(rgba.tobytes()).hexdigest() != entry["textures"]["color"]["sha256"]:
        fail("a texture's sha256 is of its raw RGBA8 bytes")
    if it["png"][:8] != b"\x89PNG\r\n\x1a\n" or struct.unpack(">II", it["png"][16:24]) != (1024, 1024):
        fail("each colour comes with its colour map as a PNG")
    magic, _ver, total = struct.unpack("<III", it["glb"][:12])
    gl = json.loads(it["glb"][20:20 + struct.unpack("<I", it["glb"][12:16])[0]])
    if magic != 0x46546C67 or total != len(it["glb"]) or len(gl["images"]) != 1 or "normalTexture" in gl["materials"][0]:
        fail("each colour comes with the .glb Open Cloud would get (colour map only)")
if tuple(out["items"][1]["pixels"]["color"][0, 0]) != (255, 0, 0, 255) or tuple(out["items"][2]["pixels"]["color"][0, 0]) != (0, 0, 255, 255):
    fail("each colour has its own texture")
full = batch.build_set(ctx, hat, [red], evaluate.evaluate(ctx, hat, publish=False), 6)
if not full["manifest"]["items"][0]["pbr"] or full["manifest"]["items"][0]["textures"]["normal"] is None:
    fail("without colour_only the other maps go along")
relabel = batch.build_set(ctx, hat, [red, blue], evaluate.evaluate(ctx, hat, publish=False), 7,
                          labels={"Lock Hat - Blue": "Arctic"}, colour_only=True)
if [i["colour"] for i in relabel["manifest"]["items"]] != [None, "Red", "Arctic"]:
    fail("a colour word can be given for a copy")
if HITS or server.state["set_revision"] != set_before:
    fail(f"building a set must reach nobody and tell Studio nothing: {HITS}")
hat.location.z = 30                                     # not on the character: still built, but marked not sendable
ctx.view_layer.update()
off = batch.build_set(ctx, hat, [], evaluate.evaluate(ctx, hat, publish=False), 8, colour_only=True)
if off["manifest"]["items"][0]["sendable"]:
    fail("an item that fails the checks is not sendable")
done.append("build_set: /v1/set's description with no upload block, the shared mesh, a 1024 colour map (PNG) and a .glb "
            "per colour, sha256 of the raw pixels, and not one request")

os.environ.pop("FITBRIDGE_NO_UPLOAD", None)
srv.shutdown()
print("PASS")
for d in done:
    print(" -", d)
