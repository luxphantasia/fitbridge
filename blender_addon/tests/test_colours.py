"""Colour versions against a local fake of Anthropic's API (never the real one): Claude's picks (checked and topped up
from the palette), the palette without a key, the gradient-map recolour, linked copies in a grid of 4, textures saved
next to the .blend, adding more, the 12 cap, and items with no texture. Runs in a background Blender with FitBridge
installed as an extension (tests/run.sh)."""
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import bmesh
import bpy
import numpy as np

from bl_ext.user_default.fitbridge import auto, batch, evaluate, host, panel, variants  # noqa: E402
from bl_ext.user_default.fitbridge import ui as kit  # noqa: E402

SEEN = {"posts": [], "reply": None}


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        SEEN["posts"].append({"path": self.path, "body": body, "key": self.headers.get("x-api-key")})
        d = json.dumps({"stop_reason": "end_turn", "content": [{"type": "text", "text": json.dumps(SEEN["reply"])}]})
        d = d.encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(d)))
        self.end_headers()
        self.wfile.write(d)


srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
threading.Thread(target=srv.serve_forever, daemon=True).start()
auto.API = f"http://127.0.0.1:{srv.server_address[1]}"
done = []


def fail(msg):
    print("FAIL", msg)
    sys.stdout.flush()
    os._exit(1)


def wait():
    """run the timer by hand until the job ends (a background Blender has no event loop)"""
    for _ in range(400):
        if variants._tick() is None:
            return
        import time
        time.sleep(0.05)
    fail("the colours never came back")


ctx = bpy.context
st = ctx.scene.fitbridge
p = host.prefs()
for o in list(bpy.data.objects):
    bpy.data.objects.remove(o)

# a textured hood-ish cube: a dark navy texture with a light stripe (shading the recolour must keep)
W = 64
img = bpy.data.images.new("HoodTex", W, W, alpha=True)
px = np.zeros((W, W, 4), np.float32)
px[..., :3] = (0.05, 0.06, 0.12)
px[:, 20:28, :3] = (0.5, 0.52, 0.6)
px[40:, :, :3] *= 0.5
px[..., 3] = 1.0
img.pixels.foreach_set(px.ravel())
bm = bmesh.new()
bm.loops.layers.uv.new("UVMap")
bmesh.ops.create_cube(bm, size=1.0, calc_uvs=True)
me = bpy.data.meshes.new("Hood")
bm.to_mesh(me)
bm.free()
mat = bpy.data.materials.new("HoodMat")
mat.use_nodes = True
bsdf = next(n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
tex = mat.node_tree.nodes.new("ShaderNodeTexImage")
tex.image = img
mat.node_tree.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
me.materials.append(mat)
hood = bpy.data.objects.new("Bunny Hood", me)
ctx.scene.collection.objects.link(hood)
hood.location = (0, 0, 5.5)
bpy.ops.fitbridge.show_rig(kind="R15")
for o in ctx.view_layer.objects:
    o.select_set(o is hood)
ctx.view_layer.objects.active = hood
evaluate.follow_selection(ctx)

# --- the recolour itself: lightness order kept, the hue is the target's
rgb = px.reshape(-1, 4)[:, :3].astype(np.float64)
out = variants.gradient_map(rgb, None, variants.hex_rgb("#c4232b"))
L0, L1 = variants._to_oklab(rgb)[:, 0], variants._to_oklab(out)[:, 0]
order = L0.argsort(kind="stable")
if np.any(np.diff(L1[order]) < -1e-6) or len(np.unique(L1.round(4))) < len(np.unique(L0.round(4))):
    fail("the recolour should keep the shading: lightness order kept, no tones merged")
lab = variants._to_oklab(out[L1.argsort()[len(L1) // 2]][None])[0]
if not (lab[1] > 0.05 and abs(np.degrees(np.arctan2(lab[2], lab[1])) - 27) < 25):
    fail(f"the mid tone should be red: {lab}")
if out.min() < 0 or out.max() > 1:
    fail("the recolour must stay inside sRGB")
done.append("gradient map: shading kept (lightness order, no tones merged), red hue, in gamut")

# --- no key: the shop palette, in a grid of 4, sharing the mesh
p.claude_api_key = ""
st.colour_count = 5
if "FINISHED" not in bpy.ops.fitbridge.make_colours():
    fail("make colours didn't run")
fam = batch.family(ctx, hood)
copies = [m for m in fam if m is not hood]
if [o.name for o in ctx.view_layer.objects.selected] != ["Bunny Hood"]:
    fail("only the original stays selected")
if len(copies) != 5 or SEEN["posts"]:
    fail(f"5 palette copies and no request without a key: {len(copies)}, {len(SEEN['posts'])}")
if "shop palette" not in variants.STATE["message"]:
    fail("the panel should say the palette was used")
names = sorted(c.name for c in copies)
if not all(n.startswith("Bunny Hood - ") for n in names):
    fail(f"names should be '<item> - <Colour>': {names}")
words = [batch.colour_word(c, hood) for c in copies]
if None in words:
    fail("Send all colours must read each colour from the name")
mats = {c.active_material.name for c in copies}
if len(mats) != 5 or hood.active_material is not mat or any(c.data is not hood.data for c in copies):
    fail("each copy: same mesh, its own material; the original keeps its own")
imgs = [variants.colour_image(c)[1] for c in copies]
if any(i is None or i is img or not os.path.isfile(i.filepath_raw) for i in imgs):
    fail("each copy should have its own saved texture")
if not imgs[0].filepath_raw.startswith(os.path.join(host.user_dir(), "colours")):
    fail(f"an unsaved file keeps its colours in FitBridge's folder: {imgs[0].filepath_raw}")
spots = sorted((round(c.location.y, 3), round(c.location.x, 3)) for c in copies)
rows = sorted({y for y, _x in spots})
if len(rows) != 2 or sum(1 for y, _x in spots if y == rows[0]) != 4 or min(x for _y, x in spots) <= 1.0:
    fail(f"a grid of 4 per row, clear of the mannequin: {spots}")
done.append("no key: 5 palette colours, '<item> - <Colour>', shared mesh, own materials + saved PNGs, grid of 4")

# --- with a key: Claude's picks are checked (bad hex, repeats, existing names dropped) and topped up
p.claude_api_key = "test-claude-key"
SEEN["reply"] = {"colours": [{"name": "blood red", "hex": "#8a1c2b"}, {"name": "Blood Red", "hex": "#8a1c2b"},
                             {"name": "Bone White", "hex": "not-a-colour"}, {"name": words[0], "hex": "#123456"},
                             {"name": "Ghost Grey", "hex": "b0b3b8"}]}
st.colour_count = 3
bpy.ops.fitbridge.make_colours()
if not variants.STATE["busy"]:
    fail("with a key it should be waiting for Claude: " + variants.STATE["message"])
wait()
post = SEEN["posts"][-1]
text = next(c["text"] for c in post["body"]["messages"][0]["content"] if c["type"] == "text")
imgs_sent = [c for c in post["body"]["messages"][0]["content"] if c["type"] == "image"]
if post["key"] != "test-claude-key" or "test-claude-key" in json.dumps(post["body"]) or len(imgs_sent) != 1:
    fail("one picture, the key only in its header")
if "Bunny Hood" not in text or words[0] not in text:
    fail("Claude should get the name and the colours already made")
new = [m for m in batch.family(ctx, hood) if m is not hood and m not in copies]
got = sorted(batch.colour_word(m, hood) for m in new)
if len(new) != 3 or "Blood Red" not in got or "Ghost Grey" not in got:
    fail(f"Claude's two good picks plus one from the palette: {got}")
if "fewer colours" not in variants.STATE["message"]:
    fail("it should say the palette filled in")
spots = {(round(m.location.x, 3), round(m.location.y, 3)) for m in batch.family(ctx, hood) if m is not hood}
if len(spots) != 8:
    fail("added copies continue the grid, no two in one spot")
done.append("with a key: one picture + name + existing colours sent, picks checked, topped up, grid continues")

# --- the cap, and a saved .blend keeps its colours next to it
folder = tempfile.mkdtemp()
bpy.ops.wm.save_as_mainfile(filepath=os.path.join(folder, "Hood.blend"))
st.colour_count = 99
if st.colour_count != 12:
    fail("at most 12 at a time")
p.claude_api_key = ""
st.colour_count = 2
bpy.ops.fitbridge.make_colours()
latest = [m for m in batch.family(ctx, hood) if m is not hood][-2:]
paths = [variants.colour_image(m)[1].filepath_raw for m in latest]
if not all(pth.startswith(os.path.join(folder, "Bunny Hood colours")) for pth in paths):
    fail(f"textures go next to the .blend: {paths}")
done.append("12 at most; a saved file keeps its colours in '<item> colours/' beside it")

# --- no texture: the base colour is set instead
mat2 = bpy.data.materials.new("Plain")
mat2.use_nodes = True
me2 = bpy.data.meshes.new("Plain")
bm = bmesh.new()
bmesh.ops.create_cube(bm, size=1.0)
bm.to_mesh(me2)
bm.free()
me2.materials.append(mat2)
plain = bpy.data.objects.new("Plain Cap", me2)
ctx.scene.collection.objects.link(plain)
plain.location = (0, 0, 5.5)
for o in ctx.view_layer.objects:
    o.select_set(o is plain)
ctx.view_layer.objects.active = plain
evaluate.follow_selection(ctx)
st.colour_count = 1
bpy.ops.fitbridge.make_colours()
c = [m for m in batch.family(ctx, plain) if m is not plain][0]
col = evaluate._principled(c.active_material).inputs["Base Color"].default_value
want = variants._srgb_to_lin(variants.hex_rgb(variants.palette_pick(1, [])[0]["hex"]))
if np.abs(np.array(col[:3]) - want).max() > 1e-3:
    fail(f"an untextured item gets the base colour: {col[:]}")
done.append("no texture: the material's base colour is set")

# --- the panel: the row and its button are there
ops = []


class Lay:
    scale_y = 1.0

    def __getattr__(self, name):
        if name == "operator":
            return lambda idname, *a, **k: ops.append(idname) or type("Op", (), {})()
        return lambda *a, **k: Lay()


class Ctx:
    region = type("R", (), {"width": 300, "as_pointer": lambda self: 1})()

    def __getattr__(self, name):
        return getattr(bpy.context, name)


st.ui_tab = "FIT"
panel.build(kit.Rows(Lay(), Ctx()), Ctx())
if "fitbridge.make_colours" not in ops:
    fail("the Fit tab should carry the Make colours button")
done.append("the Fit tab shows Colour versions")

print("\n".join("ok  " + d for d in done))
print(f"PASS {len(done)} colour checks")
sys.stdout.flush()
os._exit(0)
