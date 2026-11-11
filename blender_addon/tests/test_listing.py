"""The Listing tab against a local fake of Paradyse Studio's Blender API (never the real site), and the drawn panel's
layout in every state. Runs in a background Blender with FitBridge installed as an extension (tests/run.sh)."""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import bmesh
import bpy

SEEN = {"posts": [], "gets": 0, "chips": [], "auth": set(), "fail": False}
DONE = {"id": "ul_1", "status": "done", "step": "Done", "error": None, "url": "", "chip_ids": ["c1"], "item": {
    "what": "cat ears", "category": "hair", "category_label": "Hair", "character": {"name": "Blake", "series": "RWBY"},
    "alias": {"name": "Shadow Cat", "strength": "strong"},
    "real": {"names": ["Blake Cat Ears", "B2", "B3"], "description": "Real words.\n\nLine one"},
    "adjacent": {"names": ["Shadow Cat Ears", "A2", "A3"], "description": "Adjacent words.\n\nLine one"},
    "plain": None, "price": {"low": 50, "median": 75, "high": 100, "from": 12}}}


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, body):
        d = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(d)))
        self.end_headers()
        self.wfile.write(d)

    def do_GET(self):
        SEEN["auth"].add(self.headers.get("Authorization"))
        if self.path == "/api/blender/v1/listings/hello":
            return self._json(200, {"ready": True, "why": None, "max_pictures": 6, "chips_on": ["c1"],
                                    "chips": [{"id": "c1", "name": "Group", "text": "Line one"},
                                              {"id": "c2", "name": "Tail", "text": "Line two"}]})
        SEEN["gets"] += 1
        if SEEN["fail"]:
            return self._json(200, {"listing": dict(DONE, status="failed", error="Claude couldn't read it", item=None)})
        self._json(200, {"listing": DONE if SEEN["gets"] >= 2 else dict(DONE, status="running", step="Writing", item=None)})

    def do_POST(self):
        SEEN["auth"].add(self.headers.get("Authorization"))
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path.endswith("/chips"):
            SEEN["chips"].append(json.loads(body)["ids"])
            return self._json(200, {"listing": dict(DONE, chip_ids=json.loads(body)["ids"])})
        SEEN["posts"].append({"path": self.path, "type": self.headers.get("Content-Type"), "body": body})
        self._json(201, {"listing": dict(DONE, status="running", step="Reading the pictures", item=None)})


srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
threading.Thread(target=srv.serve_forever, daemon=True).start()
os.environ["FITBRIDGE_STUDIO_URL"] = f"http://127.0.0.1:{srv.server_address[1]}"

import bl_ext.user_default.fitbridge as fb  # noqa: E402
from bl_ext.user_default.fitbridge import batch, evaluate, host, listing, panel  # noqa: E402
from bl_ext.user_default.fitbridge import ui as kit  # noqa: E402

done = []


def fail(msg):
    print("FAIL", msg)
    sys.exit(1)


def pump(until, seconds=20):
    end = time.time() + seconds
    while time.time() < end:
        listing.tick()
        if until():
            return True
        time.sleep(0.05)
    return False


class Lay:
    """a stand-in for Blender's layout: takes any call, so the panel can be laid out without a window"""
    scale_y = 1.0

    def __getattr__(self, name):
        if name in ("operator",):
            return lambda *a, **k: type("Op", (), {})()
        return lambda *a, **k: Lay()


class Ctx:
    """the real context plus a region, which a background Blender doesn't have"""
    region = type("R", (), {"width": 300, "as_pointer": lambda self: 1})()

    def __getattr__(self, name):
        return getattr(bpy.context, name)


def lay_out():
    rows = kit.Rows(Lay(), Ctx())
    panel.build(rows, Ctx())
    return rows


ctx = bpy.context
p = host.prefs()
st = ctx.scene.fitbridge
for o in list(bpy.data.objects):
    bpy.data.objects.remove(o)

# --- no key: the tab isn't there, nothing is asked
p.studio_key = ""
if listing.available() or host.studio() != ("", ""):
    fail("no Studio key must mean no Listing tab")
if listing.hello() is not None or SEEN["auth"]:
    fail("nothing may be sent without a key")
done.append("no Studio key: no Listing tab, nothing sent")

p.studio_key = "blender-test-key"
bm = bmesh.new()
bmesh.ops.create_cube(bm, size=1.0)
me = bpy.data.meshes.new("Ears")
bm.to_mesh(me)
bm.free()
me.uv_layers.new(name="UV")
ears = bpy.data.objects.new("Ears", me)
ctx.scene.collection.objects.link(ears)
ears.location = (0, 0, 5.5)
ctx.view_layer.objects.active = ears
ears.select_set(True)
bpy.ops.fitbridge.show_rig(kind="R15")
for o in ctx.view_layer.objects:
    o.select_set(o is ears)
ctx.view_layer.objects.active = ears
evaluate.follow_selection(ctx)
evaluate.evaluate(ctx)

# --- the panel lays out in every state without a window
for tab in ("FIT", "THUMB", "LISTING"):
    st.ui_tab = tab
    if not lay_out().items:
        fail(f"the {tab} tab laid out nothing")
st.thumb_view = True
st.ui_tab = "THUMB"
lay_out()
st.thumb_view = False
st.ui_collapsed = True
if len(lay_out().items) > 5:
    fail("folded, the panel is only its header")
st.ui_collapsed = False
done.append("the drawn panel lays out: Fit, Thumbnail, Thumbnail with camera view, Listing, folded")

# --- write a listing: four pictures, the note, the key only in the header
listing.hello()
if not pump(lambda: listing.HELLO["data"] is not None):
    fail("Studio's hello never arrived")
st.listing_note = "Blake from RWBY"
st.listing_light = "FLAT"
st.ui_tab = "LISTING"
saved = (ctx.scene.render.engine, ctx.scene.render.resolution_x, ctx.scene.display.shading.light)
if bpy.ops.fitbridge.listing_write(again="no") != {"FINISHED"}:
    fail("Write the listing didn't start")
if (ctx.scene.render.engine, ctx.scene.render.resolution_x, ctx.scene.display.shading.light) != saved:
    fail("taking the pictures must put the render settings back")
if listing.state(ears)[0] != "running":
    fail("it should be running")
lay_out()
if not pump(lambda: listing.state(ears)[0] == "done"):
    fail(f"the listing never finished: {listing.state(ears)}")
post = SEEN["posts"][0]
if post["path"] != "/api/blender/v1/listings" or not post["type"].startswith("multipart/form-data"):
    fail("the pictures go to /listings as a form")
if post["body"].count(b'name="image"') != 4 or post["body"].count(b"\x89PNG") != 4:
    fail("four PNG pictures")
if b"Blake from RWBY" not in post["body"] or b'name="no_colour"' in post["body"]:
    fail("the note is sent; no_colour only for an item with recolours")
if SEEN["auth"] != {"Bearer blender-test-key"} or b"blender-test-key" in post["body"]:
    fail("the key travels in the Authorization header only")
data = listing.stored(ears)
if data["variant"] != "adjacent" or listing.copy_of(data)["names"][0] != "Shadow Cat Ears":
    fail("a character starts on the adjacent name")
if len(data["pictures"]) != 4 or not all(os.path.isfile(x) for x in data["pictures"]):
    fail("the four pictures are kept for the panel")
lay_out()
done.append("Write the listing: 4 PNGs + note as a form, key in the header only, polls until done, kept on the object")

# --- picking a name fills FitBridge's Name; again clears it
bpy.ops.fitbridge.listing_name(index=0)
if st.item_name != "Shadow Cat Ears" or ears.get("fitbridge_name") != "Shadow Cat Ears":
    fail("clicking a name makes it the item's name")
bpy.ops.fitbridge.listing_name(index=0)
if st.item_name != "":
    fail("clicking the chosen name again goes back to the mesh's name")
bpy.ops.fitbridge.listing_name(index=1)
bpy.ops.fitbridge.listing_set(what="variant", value="real")
if listing.copy_of(listing.stored(ears))["names"][0] != "Blake Cat Ears":
    fail("the Real name switch")
if bpy.ops.fitbridge.listing_copy() != {"FINISHED"} or \
        listing.copy_of(listing.stored(ears))["description"] != "Real words.\n\nLine one":
    fail("Copy takes the description on show")              # (a background Blender has no clipboard to read back)
done.append("a name click fills Name (again clears it); Real / Adjacent switch; Copy")

# --- closing lines: the tick goes to Studio
bpy.ops.fitbridge.listing_set(what="chip", value="c2")
if not pump(lambda: SEEN["chips"] == [["c1", "c2"]]):
    fail(f"ticking a closing line tells Studio: {SEEN['chips']}")
bpy.ops.fitbridge.listing_set(what="chip", value="c1")
if not pump(lambda: SEEN["chips"][-1] == ["c2"]):
    fail("unticking too")
done.append("closing lines: ticks are sent to Studio")

# --- Studio's category offers the type, never sets it by itself
if st.acc_type != "Hat" or listing.type_offer(listing.stored(ears), st.acc_type) != "Hair":
    fail("Studio's category is offered, not applied")
bpy.ops.fitbridge.listing_set(what="type", value="Hair")
if st.acc_type != "Hair" or listing.type_offer(listing.stored(ears), st.acc_type) is not None:
    fail("Use Hair sets the type and the offer goes away")
done.append("category: offered as a type, set only on a click")

# --- an item with recolours: Studio is told to leave the colour out of the names
twin = bpy.data.objects.new("Ears Purple", me)
ctx.scene.collection.objects.link(twin)
twin.location = (4, 0, 5.5)
if len(batch.family(ctx, ears)) < 2:
    fail("the recolour should count as family")
SEEN["gets"] = 0
if listing.start(ctx, ears, "", "STUDIO") is not None:
    fail("writing again should start")
pump(lambda: listing.state(ears)[0] == "done")
if b'name="no_colour"' not in SEEN["posts"][-1]["body"]:
    fail("recolours: no_colour must be sent")
done.append("recolours: Studio is told to leave the colour out of the names")

# --- a failure is told in words, and the old key never leaks into it
SEEN["fail"] = True
listing.start(ctx, ears, "", "FLAT")
if not pump(lambda: listing.state(ears)[0] == "failed"):
    fail("a failed listing should say so")
if "Claude couldn't read it" not in listing.state(ears)[1]:
    fail("Studio's own words are shown")
lay_out()
SEEN["fail"] = False
done.append("a failed listing shows Studio's words")

# --- the file remembers it
if not isinstance(ears.get(listing.KEY), str):
    fail("the listing is kept on the object")
done.append("the listing is kept on the object (saved with the file)")

# --- the painter's own check: markers must be as far apart on screen as the row heights between them say
if kit._agree([(0.0, 500.0), (60.0, 440.0), (200.0, 300.0)], 1.0) != 0.0:
    fail("markers that match the row heights agree")
if abs(kit._agree([(0.0, 500.0), (60.0, 432.0)], 1.0) - 8.0) > 1e-6:
    fail("a marker row 8 px taller than its height says must show up as 8 px of disagreement")
if abs(kit._agree([(0.0, 1000.0), (60.0, 880.0)], 2.0)) > 1e-6:
    fail("the check follows the UI scale")
rows = lay_out()
if rows.marks:
    fail("a background Blender has no marker icons, so no row may claim one")
kit.STATE["ids"], kit.STATE["side"] = list(range(1, kit.MARK_MAX + 1)), 99      # pretend the icons exist
try:
    rows = lay_out()
finally:
    kit.STATE["ids"], kit.STATE["side"] = [], 0
tops = [above for _i, above, _h in rows.marks]
total = sum(h for h, _p in rows.items)
if not rows.marks or tops[0] > 8 or total - (tops[-1] + rows.marks[-1][2]) > 0.5:
    fail(f"the panel must start and end with a marker: {tops} of {total}")
if max(b - a for a, b in zip(tops, tops[1:])) > 200:
    fail(f"markers must come often enough that one is always in view: {tops}")
if any(not 12 <= h <= 36 for _i, _a, h in rows.marks):
    fail("only rows of 12 to 36 px carry markers")
done.append("markers: at the panel's start and end and never more than 200 px apart; their self-check catches an 8 px error")

for d in done:
    print(" -", d)
print("PASS listing tab")
