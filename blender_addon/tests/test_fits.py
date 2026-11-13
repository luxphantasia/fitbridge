"""Your fits told to Paradyse Studio (POST /fits) against a local fake of its Blender API (never the real site).
Runs in a background Blender with FitBridge installed as an extension (tests/run.sh)."""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import bmesh
import bpy

SEEN = {"posts": [], "status": 201}


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        SEEN["posts"].append({"path": self.path, "auth": self.headers.get("Authorization"),
                              "ua": self.headers.get("User-Agent"), "type": self.headers.get("Content-Type"),
                              "body": body})
        d = json.dumps({"id": "ft_1"} if SEEN["status"] < 400 else {"error": "no"}).encode()
        self.send_response(SEEN["status"])
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(d)))
        self.end_headers()
        self.wfile.write(d)


srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
threading.Thread(target=srv.serve_forever, daemon=True).start()
os.environ["FITBRIDGE_STUDIO_URL"] = f"http://127.0.0.1:{srv.server_address[1]}"
os.environ.pop("FITBRIDGE_NO_UPLOAD", None)

from bl_ext.user_default.fitbridge import evaluate, fits, host, thumb  # noqa: E402

done = []


def fail(msg):
    print("FAIL", msg)
    sys.exit(1)


def parts(post):
    """the multipart body as {name: bytes}"""
    b = post["type"].split("boundary=")[1].encode()
    out = {}
    for chunk in post["body"].split(b"--" + b)[1:-1]:
        head, _, data = chunk.partition(b"\r\n\r\n")
        name = head.split(b'name="')[1].split(b'"')[0].decode()
        out[name] = data[:-2]
    return out


ctx = bpy.context
p = host.prefs()
st = ctx.scene.fitbridge
for o in list(bpy.data.objects):
    bpy.data.objects.remove(o)
bm = bmesh.new()
bmesh.ops.create_cube(bm, size=1.0)
me = bpy.data.meshes.new("Beanie")
bm.to_mesh(me)
bm.free()
me.uv_layers.new(name="UV")
beanie = bpy.data.objects.new("Beanie", me)
ctx.scene.collection.objects.link(beanie)
beanie.location = (0, 0, 5.5)
beanie.scale = (1.2, 1.2, 0.6)
bpy.ops.fitbridge.show_rig(kind="R15")
for o in ctx.view_layer.objects:
    o.select_set(o is beanie)
ctx.view_layer.objects.active = beanie
evaluate.follow_selection(ctx)
res = evaluate.evaluate(ctx)
if res.get("fit") is None:
    fail("the test beanie wasn't fitted")
g = thumb.owner(ctx)[0]

# --- no Paradyse key: nothing is sent
p.studio_key = ""
if fits.tell("Beanie", res, g) is not None or SEEN["posts"]:
    fail("nothing may be sent without a Paradyse key")
done.append("no Paradyse key: nothing sent")

# --- with a key: the fit, the mesh as a GLB, the key only in the header
p.studio_key = "blender-test-key"
mesh = evaluate.build_mesh(res)
t = fits.tell("Beanie", res, g, mesh, {}, False)
if t is None:
    fail("with a key the fit is told")
t.join(10)
if len(SEEN["posts"]) != 1:
    fail(f"one request, not {len(SEEN['posts'])}")
post = SEEN["posts"][0]
if post["path"] != "/api/blender/v1/fits" or post["auth"] != "Bearer blender-test-key" or "FitBridge" not in post["ua"]:
    fail(f"the request: {post['path']} {post['ua']}")
got = parts(post)
fit = json.loads(got["fit"])
want_keys = {"name", "kind", "placement", "rule", "thumb", "size", "triangles", "asset_id", "fitbridge", "mesh"}
if set(fit) != want_keys:
    fail(f"the fit's fields: {sorted(fit)}")
pl = fit["placement"]
if pl["attachment"] != res["attachment"] or pl["rig"] != res["scale"] or pl["scale"] != [1.0, 1.0, 1.0] \
        or pl["cframe"][3:] != [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0] \
        or max(abs(a - b) for a, b in zip(pl["cframe"][:3], res["fit"]["offset"])) > 1e-5:
    fail(f"the placement: {pl}")
if fit["rule"] is not None or fit["asset_id"] is not None or fit["triangles"] != 12 or fit["kind"] != res["acc_type"]:
    fail(f"rule, asset_id, triangles, kind: {fit}")
s = res["fit"]["size"]
if fit["size"] != [round(s[0], 4), round(s[2], 4), round(s[1], 4)]:
    fail(f"size wide, deep, tall: {fit['size']}")
if set(fit["thumb"]) != {"turn", "tilt", "distance", "pan", "pivot"}:
    fail(f"the shot: {fit['thumb']}")
if got.get("mesh", b"")[:4] != b"glTF":
    fail("the mesh goes as a GLB")
if b"blender-test-key" in post["body"] or b"@" in got["fit"]:
    fail("the key is only in the header, and nothing like an email address is sent")
if not fits.STATE["last"].startswith("told Paradyse"):
    fail(f"its quiet note: {fits.STATE['last']}")
done.append("with a key: POST /fits with the placement (identity and the box centre), the shot, the size, triangles, "
            "rule null, the mesh as a GLB, the key only in the header")

# --- a failure is one quiet line, never an error
SEEN["status"] = 500
t = fits.tell("Beanie", res, g)
t.join(10)
if "didn't take" not in fits.STATE["last"]:
    fail(f"a refused fit is said quietly: {fits.STATE['last']}")
SEEN["status"] = 201
done.append("a refused fit: one quiet line")

# --- the upload lock (the UGC autopilot's Blender): never told
os.environ["FITBRIDGE_NO_UPLOAD"] = "1"
n = len(SEEN["posts"])
if fits.tell("Beanie", res, g) is not None or len(SEEN["posts"]) != n:
    fail("a Blender with the upload lock never tells Paradyse")
os.environ.pop("FITBRIDGE_NO_UPLOAD")
done.append("the upload lock on: never told")

# --- Send to Studio tells it too, after the send has started
st.override = True
n = len(SEEN["posts"])
r = bpy.ops.fitbridge.send()
end = time.time() + 10
while len(SEEN["posts"]) == n and time.time() < end:
    time.sleep(0.05)
if "FINISHED" not in r or len(SEEN["posts"]) != n + 1:
    fail(f"Send to Studio tells Paradyse once: {r}, {len(SEEN['posts']) - n}")
done.append("Send to Studio tells Paradyse once")

print("FITS OK:", "; ".join(done))
srv.shutdown()
