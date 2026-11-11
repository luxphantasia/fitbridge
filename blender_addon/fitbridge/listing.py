"""The Listing tab's other end: Paradyse Studio's UGC listings, over its Blender API (docs/LISTING.md).

Blender renders the selected accessory alone from four sides, sends the pictures (and an optional note) with the
Studio Blender key, and shows what comes back: name options, the description, the closing lines that are switched on,
a suggested price. The answer is kept on the object, so it's still there when the file is opened again.

Only ever talks to the Studio site set in the preferences, and only when a key is set. Network calls run on threads;
everything that touches Blender data happens in tick() on the main thread.
"""

import json
import os
import queue
import re
import threading
import time
import urllib.error
import urllib.request
import uuid

import bpy

from . import batch, host, thumb
from .version import VERSION

KEY = "fitbridge_listing"                    # on the object: the listing as JSON
ANGLES = ((0.0, 5.0), (35.0, 15.0), (90.0, 5.0), (180.0, 10.0))      # front, three-quarter, side, back
VIEW_NAMES = ("Front", "Three-quarter", "Side", "Back")
# Paradyse refuses a request over 1 MB, so the four pictures go as JPEGs that fit with room to spare for the note:
# full size when they do (nearly always), stepped down (size, JPEG quality) for a texture too busy to fit
SEND_MAX = 900 << 10
SHOT_TRIES = ((768, 90), (768, 80), (640, 80), (512, 75))
POLL_EVERY = float(os.environ.get("FITBRIDGE_LISTING_POLL", "3.0"))
POLL_FOR = 600.0
HELLO_FOR = 60.0
UA = f"FitBridge-Blender/{VERSION}"
TYPE_OF = {"hair": "Hair", "hat": "Hat", "face": "Face", "neck": "Neck", "shoulder": "Shoulder", "front": "Front",
           "back": "Back", "waist": "Waist"}

_done = queue.Queue()
_stop = threading.Event()                    # set when FitBridge is switched off: waiting threads end at once
RUN = {"ob": None, "step": "", "started": 0.0, "error": "", "error_ob": None}
HELLO = {"at": 0.0, "busy": False, "data": None, "error": ""}
_cache = {}                                  # object name -> (json string, parsed)


class StudioError(Exception):
    pass


def available():
    return bool(host.studio()[1])


def busy():
    return RUN["ob"] is not None


# ------------------------------------------------------------------------------------------------------- the calls

def _request(path, method="GET", body=None, headers=None, timeout=60):
    url, key = host.studio()
    if not key:
        raise StudioError("Add your Paradyse Blender key in the preferences first")
    data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
    req = urllib.request.Request(url + "/api/blender/v1" + path, method=method, data=data)
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("User-Agent", UA)
    if body is not None and not isinstance(body, bytes):
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        msg = ""
        try:
            msg = json.loads(e.read() or b"{}").get("error", "")
        except ValueError:
            pass
        if e.code in (401, 403):
            raise StudioError("Paradyse refused the Blender key. Make a new one there, under Settings, Blender key")
        if e.code == 404 and not msg:
            raise StudioError("Paradyse doesn't have listings for Blender yet")
        raise StudioError(msg or f"Paradyse answered {e.code}")
    except urllib.error.URLError as e:
        raise StudioError(f"Can't reach Paradyse ({getattr(e, 'reason', e)})")
    except TimeoutError:
        raise StudioError("Paradyse took too long to answer")
    try:
        return json.loads(raw or b"{}")
    except ValueError:
        raise StudioError("Paradyse doesn't have listings for Blender yet")


def _kind(pic):
    """(file ending, type) of a picture's bytes"""
    return ("jpg", "image/jpeg") if pic[:2] == b"\xff\xd8" else ("png", "image/png")


def _multipart(fields, pics):
    edge = uuid.uuid4().hex
    out = []
    for k, v in fields.items():
        out.append(f'--{edge}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    for i, pic in enumerate(pics):
        end, kind = _kind(pic)
        out.append(f'--{edge}\r\nContent-Disposition: form-data; name="image"; filename="view{i + 1}.{end}"\r\n'
                   f'Content-Type: {kind}\r\n\r\n'.encode() + pic + b"\r\n")
    out.append(f"--{edge}--\r\n".encode())
    return b"".join(out), {"Content-Type": f"multipart/form-data; boundary={edge}"}


def _work(name, fields, pics):
    """send the pictures, then ask until it's written"""
    try:
        body, headers = _multipart(fields, pics)
        got = _request("/listings", "POST", body, headers, timeout=120)["listing"]
        _done.put(("listing", name, got, None))
        until = time.time() + POLL_FOR
        while got.get("status") == "running":
            if time.time() > until:
                raise StudioError("Still writing after ten minutes. Open it in Paradyse to see where it is")
            if _stop.wait(POLL_EVERY):
                return
            got = _request(f"/listings/{got['id']}")["listing"]
            _done.put(("listing", name, got, None))
        _done.put(("end", name, got, None))
    except Exception as e:  # noqa: BLE001 - whatever went wrong is told in the panel
        _done.put(("end", name, None, str(e) or "Something went wrong"))


def _chips_work(name, lid, ids):
    try:
        _done.put(("listing", name, _request(f"/listings/{lid}/chips", "POST", {"ids": ids})["listing"], None))
    except Exception as e:  # noqa: BLE001
        _done.put(("note", name, None, str(e)))


def _hello_work():
    try:
        _done.put(("hello", None, _request("/listings/hello", timeout=20), None))
    except Exception as e:  # noqa: BLE001
        _done.put(("hello", None, None, str(e)))


def hello():
    """what Studio says it can do (asked again once a minute while the tab is open); None until it has answered"""
    if available() and not HELLO["busy"] and time.time() - HELLO["at"] > HELLO_FOR:
        HELLO.update(busy=True, at=time.time())
        threading.Thread(target=_hello_work, daemon=True).start()
    return HELLO["data"]


# -------------------------------------------------------------------------------------------- what's kept on the item

def stored(ob):
    """the listing kept on this object: {"listing", "variant", "pictures", "light"} or None"""
    raw = ob.get(KEY) if ob is not None else None
    if not isinstance(raw, str) or not raw:
        return None
    hit = _cache.get(ob.name)
    if hit is None or hit[0] != raw:
        try:
            hit = (raw, json.loads(raw))
        except ValueError:
            return None
        if len(_cache) > 64:
            _cache.clear()
        _cache[ob.name] = hit
    return hit[1]


def _keep(ob, data):
    if ob is not None and ob.library is None:
        ob[KEY] = json.dumps(data, separators=(",", ":"))


def item_of(data):
    return ((data or {}).get("listing") or {}).get("item")


def variants(data):
    """the ways this item is written: [("real", "Real name"), ("adjacent", "Adjacent")] for a character, else []"""
    it = item_of(data) or {}
    out = [(k, label) for k, label in (("real", "Real name"), ("adjacent", "Adjacent name")) if it.get(k)]
    return out if len(out) == 2 else []


def copy_of(data):
    """the names and description on show: {"names": [...], "description": str} or None"""
    it = item_of(data) or {}
    want = data.get("variant") or "adjacent"
    for k in (want, "plain", "adjacent", "real"):
        if it.get(k):
            return it[k]
    return None


def type_offer(data, current):
    """the FitBridge type Studio's category points at, when it isn't the one set"""
    it = item_of(data) or {}
    t = TYPE_OF.get(str(it.get("category") or ""))
    from .spec import RIGID_TYPES
    return t if t and t != current and t in RIGID_TYPES else None


def set_variant(ob, variant):
    data = stored(ob)
    if data is not None:
        _keep(ob, {**data, "variant": variant})


def set_chips(ob, ids):
    """switch the closing lines for this listing (Studio remembers it as your choice for the next one too)"""
    data = stored(ob)
    lid = ((data or {}).get("listing") or {}).get("id")
    if not lid:
        return
    _keep(ob, {**data, "listing": {**data["listing"], "chip_ids": list(ids)}})      # shows at once; Studio confirms
    threading.Thread(target=_chips_work, args=(ob.name, lid, list(ids)), daemon=True).start()


def resume(ob):
    """Studio was still writing when Blender last looked (the file was closed): ask again instead of starting over"""
    data = stored(ob)
    got = (data or {}).get("listing") or {}
    if busy() or got.get("status") != "running" or not got.get("id"):
        return False
    RUN.update(ob=ob.name, step=got.get("step") or "Writing", started=time.time(), error="", error_ob=None)

    def work(name=ob.name, lid=got["id"]):
        try:
            cur = _request(f"/listings/{lid}")["listing"]
            until = time.time() + POLL_FOR
            while cur.get("status") == "running" and time.time() < until:
                _done.put(("listing", name, cur, None))
                if _stop.wait(POLL_EVERY):
                    return
                cur = _request(f"/listings/{lid}")["listing"]
            _done.put(("end", name, cur, None))
        except Exception as e:  # noqa: BLE001
            _done.put(("end", name, None, str(e) or "Something went wrong"))
    threading.Thread(target=work, daemon=True).start()
    return True


def forget(ob):
    if ob is not None and KEY in ob:
        del ob[KEY]


# -------------------------------------------------------------------------------------------------------- starting

def pictures_dir(ob):
    safe = "".join(c if c.isalnum() or c in "-_ " else "_" for c in ob.name)[:60] or "item"
    d = os.path.join(host.user_dir(), "listing", safe)
    os.makedirs(d, exist_ok=True)
    return d


def take_pictures(context, ob, light):
    """the four views as JPEG bytes that fit in one request to Paradyse (None for a mesh with no faces)"""
    centre, verts = thumb.centred_verts(context, ob)
    if not len(verts):
        return None
    shots = [(t, ti, *thumb.frame_shot(verts, t, ti)) for t, ti in ANGLES]
    pics = []
    for size, quality in SHOT_TRIES:
        pics = thumb.render_shots(context, ob, centre, shots, size, light="STUDIO" if light == "STUDIO" else "FLAT",
                                  jpeg=quality)
        if sum(len(p) for p in pics) <= SEND_MAX:
            break
    return pics


def start(context, ob, note, light):
    """render the four views and send them. Returns what's wrong in words, or None once it's on its way."""
    if busy():
        return "A listing is already being written"
    if not available():
        return "Add your Paradyse Blender key in the preferences first"
    try:
        pics = take_pictures(context, ob, light)
    except Exception as e:  # noqa: BLE001
        return f"Couldn't take the pictures: {e}"
    if pics is None:
        return "The mesh has no faces"
    if sum(len(p) for p in pics) > SEND_MAX:
        return "The pictures are too big to send, even made smaller"
    paths = []
    d = pictures_dir(ob)
    for old in os.listdir(d):                                  # the last listing's pictures of this item
        if re.fullmatch(r"view\d\.(png|jpg)", old):
            os.remove(os.path.join(d, old))
    for i, pic in enumerate(pics):
        path = os.path.join(d, f"view{i + 1}.{_kind(pic)[0]}")
        with open(path, "wb") as f:
            f.write(pic)
        paths.append(path)
    fields = {"note": note.strip()[:500]}
    if len(batch.family(context, ob)) > 1:
        fields["no_colour"] = "1"                              # recolours: Send all colours adds the colour word itself
    old = stored(ob) or {}
    _keep(ob, {"listing": None, "variant": old.get("variant") or "", "pictures": paths, "light": light})
    RUN.update(ob=ob.name, step="Sending the pictures", started=time.time(), error="", error_ob=None)
    threading.Thread(target=_work, args=(ob.name, fields, pics), daemon=True).start()
    return None


def tick():
    """apply what the threads brought back (main thread). True when the panel should redraw."""
    changed = False
    while True:
        try:
            kind, name, got, error = _done.get_nowait()
        except queue.Empty:
            break
        changed = True
        if kind == "hello":
            HELLO.update(busy=False, data=got if got is not None else HELLO["data"], error=error or "")
            continue
        ob = bpy.data.objects.get(name) if name else None
        if kind == "note":
            RUN.update(error=error or "", error_ob=name)
            continue
        if got is not None and ob is not None:
            data = stored(ob) or {"variant": "", "pictures": [], "light": "FLAT"}
            it = got.get("item")
            if not data.get("variant") and it:
                data = {**data, "variant": got.get("start") or ("adjacent" if it.get("adjacent") else "real")}
            _keep(ob, {**data, "listing": got})
            if RUN["ob"] == name:
                RUN["step"] = got.get("step") or RUN["step"]
        if kind == "end" and RUN["ob"] == name:
            failed = error or ((got or {}).get("status") == "failed" and ((got or {}).get("error") or "It didn't work"))
            RUN.update(ob=None, error=failed or "", error_ob=name if failed else None)
    return changed or busy()


def state(ob):
    """(kind, line) for this object: idle / running / done / failed"""
    if ob is None:
        return "idle", ""
    if RUN["ob"] == ob.name:
        return "running", RUN["step"] or "Working"
    if RUN["error"] and RUN["error_ob"] == ob.name:
        return "failed", RUN["error"]
    data = stored(ob)
    got = (data or {}).get("listing")
    if got and got.get("status") == "done" and copy_of(data):
        return "done", ""
    if got and got.get("status") == "failed":
        return "failed", got.get("error") or "It didn't work"
    if got and got.get("status") == "running":
        return "paused", "It was still being written when this file was closed"
    return "idle", ""


def register():
    _stop.clear()


def unregister():
    _stop.set()
    RUN.update(ob=None, error="", error_ob=None)
