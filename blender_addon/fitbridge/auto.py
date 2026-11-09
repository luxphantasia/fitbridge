# Auto mode (experimental): Claude looks at the selected mesh and sets everything: type, attachment, name, thumbnail.
# It compares real thumbnails, through the thumbnail's own lens and framed to the item, then small moves around the best.
# The main thread renders the views and applies the answer; only the HTTP request runs on a thread, and it never touches
# bpy. An answer is kept on the object with a fingerprint of its mesh, so it costs one look (two requests) until the mesh changes.
# The checks still decide whether it can be sent: the answer only fills in settings.

import base64
import json
import math
import os
import queue
import threading
import time
import urllib.error
import urllib.request

import bpy
import numpy as np
from mathutils import Vector

from . import evaluate, geom, host, rig, spec, thumb
from .version import VERSION

API = os.environ.get("FITBRIDGE_CLAUDE_API", "https://api.anthropic.com")   # tests point this at a local fake
API_VERSION = "2026-10-21"
MODEL = "claude-opus-5-5"
SHOT_SIZE = 420      # the size Roblox's thumbnail is
ANGLES = ((0, 5), (0, 25), (30, 10), (30, 30), (-30, 10), (-30, 30), (60, 15), (-60, 15), (90, 5), (-90, 5), (180, 10),
          (0, 75))           # (turn, tilt) of the first set of candidates: every side, so it also shows what the item is
NUDGES = ((0, 0), (-12, 0), (12, 0), (0, -10), (0, 10))    # the second set: small moves around the one it picked
ANSWER_KEY = "fitbridge_auto"
ANSWER_VERSION = 3   # older answers have no thumbnail that was looked at, or no facing: they get asked again
FIT_KEY = "fitbridge_auto_fit"
IGNORE_FACING_KEY = "fitbridge_ignore_facing"
IGNORE_FIT_KEY = "fitbridge_ignore_fit"
FIT_ANGLES = ((0, 5), (90, 5), (-90, 5), (180, 5), (35, 25))  # the worn item from every side, with the body around it
FACING = {"front": 0.0, "back": 180.0, "right": 90.0, "left": -90.0}    # where its front points, as a thumbnail turn
SETTLE = 1.0         # seconds a changed mesh must hold still before it's worth a request

_done = queue.Queue()
_run = {"busy": None, "failed": None, "message": "", "seen": None, "since": 0.0, "applied": None, "shots": [],
        "settings": None, "centre": None, "verts": None}


def prefs():
    return host.prefs()


def enabled():
    p = prefs()
    return bool(p and p.auto_mode)


def fit_on():
    """the second switch only counts with the first"""
    p = prefs()
    return enabled() and bool(p.auto_fit_check)


def fingerprint(ob):
    """changes when the mesh or its textures do, not when the object moves"""
    me = ob.data
    box = [round(x, 4) for corner in ob.bound_box for x in corner]
    mat = ob.material_slots[0].material if ob.material_slots else None
    maps = [(k, v.name, tuple(v.size)) for k, v in evaluate.find_textures(mat).items() if v is not None]
    return json.dumps([len(me.vertices), len(me.polygons), box, maps])


def answer(ob):
    """the answer kept on ob if it still describes this mesh, else None"""
    try:
        d = json.loads(ob.get(ANSWER_KEY, ""))
    except ValueError:
        return None
    return d if d.get("version") == ANSWER_VERSION and d.get("fingerprint") == fingerprint(ob) else None


# ------------------------------------------------------------------------------------------------------- the request

def _schema():
    attachments = sorted({k for t in spec.RIGID_TYPES for k, _label in spec.attachments_for(t)})
    return {"type": "object", "additionalProperties": False,
            "required": ["accessory_type", "attachment", "name", "summary", "facing", "shot"],
            "properties": {"accessory_type": {"type": "string", "enum": list(spec.RIGID_TYPES)},
                           "attachment": {"type": "string", "enum": attachments},
                           "name": {"type": "string"}, "summary": {"type": "string"},
                           "facing": {"type": "string", "enum": [*FACING, "unclear"]},
                           "shot": {"type": "integer"}}}


THUMBNAIL_BAR = """A good marketplace thumbnail makes the item instantly recognisable at small size: its most distinctive \
side faces the viewer, you can see its shape and depth rather than a flat silhouette, the main features aren't hidden or \
cut short by the angle, and you aren't looking into the hollow underside."""


def _prompt_first(ob):
    options = "\n".join(f"- {t}: " + ", ".join(f"{k} ({label})" for k, label in spec.attachments_for(t))
                        for t in spec.RIGID_TYPES)
    x, y, z = ob.dimensions
    return f"""These numbered shots show one rigid accessory for a Roblox avatar from many angles, each exactly as it \
would appear as its marketplace thumbnail. Each shot gives its camera angle. Someone wearing the item as it's placed \
here would look toward the camera at turn 0, their right is toward positive turn, and turn 180 is behind them. Its size is {x:.2f} wide, {y:.2f} deep, {z:.2f} tall, in studs. The mesh may \
have been modelled facing any way, so judge what it is from how it looks.

Accessory types and their attachment points:
{options}

Decide:
- accessory_type and attachment: what this item is and where it's worn. The attachment must belong to the type.
- name: a short marketplace title of at most 50 characters, describing the item plainly.
- summary: one short plain sentence saying what it is and where it goes, like "Hat, worn on top of the head."
- facing: where the item's own front points, as placed in these shots: "front" if it points the way the wearer looks, \
"back", "right" or "left" (the wearer's) if it's turned that way, or "unclear" if the item has no obvious front. Judge \
this from features with a clear front, like goggles, a peak or a logo.
- shot: the number of the best thumbnail. {THUMBNAIL_BAR}"""


def _prompt_refine():
    return f"""These are small variations of the thumbnail chosen for this Roblox accessory. Look at each one as the \
finished thumbnail and give the number of the best in `shot`. {THUMBNAIL_BAR}"""


def _prompt_fit():
    return """These shots show a rigid accessory worn on a Roblox R6 character, a plain grey blocky figure with no face. \
Each shot gives its camera angle: the character looks toward the camera at turn 0, its right is toward positive turn, and \
turn 180 is behind it.

Judge whether the accessory sits properly, the way a player would expect it to look when worn. Look for: a visible gap \
where it floats off the body, sinking too far into the body, sitting on the wrong part, being off-centre or crooked when \
it shouldn't be, being far too big or too small for the character, and pointing the wrong way. A worn item overlapping the \
body where it wraps around it is normal.

Answer with sits_properly, and in problem one short plain sentence saying what's wrong and which way to move, turn or \
scale it, like "Floating above the head: move it down." Leave problem empty if it sits properly."""


FIT_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["sits_properly", "problem"],
              "properties": {"sits_properly": {"type": "boolean"}, "problem": {"type": "string"}}}


def _body(shots, pngs, prompt, schema):
    content = []
    for i, png in enumerate(pngs):
        content.append({"type": "text", "text": f"Shot {i + 1} (turn {shots[i][0]:g}, tilt {shots[i][1]:g}):"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                    "data": base64.b64encode(png).decode()}})
    content.append({"type": "text", "text": prompt})
    return {"model": MODEL, "max_tokens": 4096, "fallbacks": "default",
            "output_config": {"effort": "medium", "format": {"type": "json_schema", "schema": schema}},
            "messages": [{"role": "user", "content": content}]}


REFINE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["shot"],
                 "properties": {"shot": {"type": "integer"}}}


def _sentence(e):
    """a failed request in words (the key is only ever in a header, so it can't end up in here)"""
    if isinstance(e, urllib.error.HTTPError):
        if e.code in (401, 403):
            return "Anthropic didn't accept the API key. Check it in FitBridge's preferences."
        if e.code in (429, 529):
            return "Anthropic is busy right now."
        return f"Anthropic answered with error {e.code}."
    if isinstance(e, urllib.error.URLError):
        return "Couldn't reach Anthropic. Check your connection."
    return str(e)


def parse_reply(reply):
    """the JSON answer out of a Messages response, or raises with a sentence"""
    if reply.get("stop_reason") == "refusal":
        raise ValueError("Claude declined to look at this item.")
    if reply.get("stop_reason") == "max_tokens":
        raise ValueError("Claude's answer was cut off.")
    for block in reply.get("content") or []:
        if block.get("type") == "text":
            return json.loads(block["text"])
    raise ValueError("Claude's answer was empty.")


def _worker(stage, name, fp, key, body):
    try:
        req = urllib.request.Request(API + "/v1/messages", data=json.dumps(body).encode(), method="POST", headers={
            "x-api-key": key, "anthropic-version": API_VERSION, "content-type": "application/json",
            "anthropic-beta": "server-side-fallback-2026-07-01", "User-Agent": f"FitBridge-Blender/{VERSION}"})
        with urllib.request.urlopen(req, timeout=180) as r:
            _done.put((stage, name, fp, parse_reply(json.load(r)), None))
    except Exception as e:
        _done.put((stage, name, fp, None, _sentence(e)))


def _shot_for(turn, tilt):
    distance, pan = thumb.frame_shot(_run["verts"], turn, tilt)
    return turn, tilt, distance, pan


def _ask(context, stage, ob, fp, key, angles, prompt, schema):
    shots = [_shot_for(t, ti) for t, ti in angles]
    pngs = thumb.render_shots(context, ob, _run["centre"], shots, SHOT_SIZE)
    _run["shots"] = shots
    threading.Thread(target=_worker, args=(stage, ob.name, fp, key, _body(shots, pngs, prompt, schema)),
                     daemon=True).start()


def _start(context, ob, fp, key):
    _run.update(busy=fp, failed=None, message="", settings=None)
    try:
        _run["centre"], _run["verts"] = thumb.centred_verts(context, ob)
        _ask(context, "first", ob, fp, key, ANGLES, _prompt_first(ob), _schema())
    except Exception as e:
        _run.update(busy=None, failed=fp, message=f"Couldn't render the item: {e}")


def _picked(d):
    """the shot a reply numbered (counted from 1), or the first if it's out of range"""
    i = int(d["shot"]) - 1
    return _run["shots"][i if 0 <= i < len(_run["shots"]) else 0]


def _settings(d):
    """the first reply checked: the type must exist, and the attachment must belong to it"""
    t = d["accessory_type"]
    if t not in spec.RIGID_TYPES:
        raise ValueError(f"Claude chose a type FitBridge doesn't have: {t}")
    options = [k for k, _label in spec.attachments_for(t)]
    return {"type": t, "attachment": d["attachment"] if d["attachment"] in options else options[0],
            "name": str(d["name"]).strip()[:50], "summary": str(d["summary"]).strip(), "facing": FACING.get(d["facing"])}


def _store(name, fp, settings, shot):
    ob = bpy.data.objects.get(name)
    if ob is not None:
        turn, tilt, distance, pan = shot
        ob[ANSWER_KEY] = json.dumps({"version": ANSWER_VERSION, "fingerprint": fp, **settings, "turn": turn, "tilt": tilt,
                                     "distance": distance, "pan": list(pan), "yaw": _yaw(ob)})


def _yaw(ob):
    """how far ob is turned around the vertical, in degrees (counter-clockwise from above, 0 = as modelled)"""
    y = ob.matrix_world.col[1]
    return math.degrees(math.atan2(-y.x, y.y))


def facing_now(ob, d):
    """where ob's front points now as a thumbnail turn (0 = the way the wearer looks), or None if it has no clear front.
    Turning the object is the fix, and that doesn't change the mesh, so this follows the object without asking again."""
    if d.get("facing") is None:
        return None
    return (d["facing"] - (_yaw(ob) - d["yaw"]) + 180.0) % 360.0 - 180.0


def wrong_way(ob, d):
    """a sentence if the item points the wrong way and you haven't said it's fine, else None"""
    t = facing_now(ob, d)
    if t is None or abs(t) <= 45.0 or ob.get(IGNORE_FACING_KEY) == d["fingerprint"]:
        return None
    if abs(t) > 135.0:
        return "This looks like it faces backwards. Turn it 180 degrees around Z."
    side = "right" if t > 0 else "left"
    return f"This looks turned to the character's {side}. Turn it {round(t / 5) * 5:+d} degrees around Z."


def fit_key(ob, fp):
    """what a fit verdict is about: the mesh, exactly where it is, and which body"""
    place = [round(x, 3) for row in ob.matrix_world for x in row]
    return json.dumps([fp, place, ob.fitbridge_thumb.key, rig.shown()])


def fit_verdict(ob, key):
    try:
        v = json.loads(ob.get(FIT_KEY, ""))
    except ValueError:
        return None
    return v if v.get("key") == key else None


def _fit_parts(ob):
    """the body parts around the item, so the shots are close enough to judge; all of them if it touches none"""
    coll = bpy.data.collections.get(rig.R6_COLL) or bpy.data.collections.get(rig.R15_COLL)
    parts = list(coll.objects) if coll else []
    a = np.array([ob.matrix_world @ Vector(c) for c in ob.bound_box])
    margin = max(float((a.max(axis=0) - a.min(axis=0)).max()) * 0.2, 0.1)
    near = []
    for part in parts:
        b = np.array([part.matrix_world @ Vector(c) for c in part.bound_box])
        if (b.max(axis=0) >= a.min(axis=0) - margin).all() and (b.min(axis=0) <= a.max(axis=0) + margin).all():
            near.append(part)
    return near or parts


def _world_verts(part):
    n = len(part.data.vertices)
    co = np.empty(n * 3, np.float32)
    part.data.vertices.foreach_get("co", co)
    m = np.array(part.matrix_world)
    return co.reshape(-1, 3) @ m[:3, :3].T + m[:3, 3]


def fit_shots(context, ob):
    """(centre, shots, parts) for the fit check: the item and the body around it, filling the frame from each side"""
    parts = _fit_parts(ob)
    _c, item_r = thumb.centred_verts(context, ob)
    item = item_r @ geom.P + _c
    pts = np.concatenate([item, *[_world_verts(p) for p in parts]]) if parts else item
    centre = (pts.min(axis=0) + pts.max(axis=0)) / 2
    V = (pts - centre) @ geom.P.T
    return centre, [(t, ti, *thumb.frame_shot(V, t, ti)) for t, ti in FIT_ANGLES], parts


def _start_fit(context, ob, key, api_key):
    _run.update(busy=key, failed=None, message="")
    try:
        centre, shots, parts = fit_shots(context, ob)
        pngs = thumb.render_shots(context, ob, centre, shots, SHOT_SIZE, also=parts, background=(0.13, 0.17, 0.24))
        body = _body(shots, pngs, _prompt_fit(), FIT_SCHEMA)
        threading.Thread(target=_worker, args=("fit", ob.name, key, api_key, body), daemon=True).start()
    except Exception as e:
        _run.update(busy=None, failed=key, message=f"Couldn't render the item on the character: {e}")


def _check_fit(context, ob, fp, d):
    """the item is decided and passes the checks: ask how it looks on the body, once it has stopped moving"""
    res = evaluate.LIVE["result"]
    if res is None or res.get("object") != ob.name or res.get("fit") is None or not res["ok"] or wrong_way(ob, d):
        return
    key = fit_key(ob, fp)
    if fit_verdict(ob, key) is not None:
        return
    if _run["seen"] != key:
        _run.update(seen=key, since=time.time())
    p = prefs()
    if _run["busy"] or _run["failed"] == key or not p.claude_api_key or time.time() - _run["since"] < SETTLE:
        return
    _start_fit(context, ob, key, p.claude_api_key)


def _finished(context, stage, name, fp, d, error):
    """a reply came back. After the first, look closer around the shot it picked; a failed second look isn't worth losing
    the first one's answer over."""
    if stage == "fit":
        if error:
            raise ValueError(error)
        ob = bpy.data.objects.get(name)
        if ob is not None:
            ob[FIT_KEY] = json.dumps({"key": fp, "ok": bool(d["sits_properly"]), "problem": str(d["problem"]).strip()})
        _run["busy"] = None
        return
    if stage == "refine":
        best = _run["shots"][0] if error else _picked(d)
        _store(name, fp, _run["settings"], best)
        _run["busy"] = None
        return
    if error:
        raise ValueError(error)
    _run["settings"] = _settings(d)
    best = _picked(d)
    ob = bpy.data.objects.get(name)
    if ob is None:
        _run["busy"] = None
        return
    angles = [((best[0] + dt + 180.0) % 360.0 - 180.0, max(-89.0, min(89.0, best[1] + di))) for dt, di in NUDGES]
    p = prefs()
    try:
        _ask(context, "refine", ob, fp, p.claude_api_key, angles, _prompt_refine(), REFINE_SCHEMA)
    except Exception:
        _store(name, fp, _run["settings"], best)
        _run["busy"] = None


def _apply(context, ob, d):
    """put the answer into the settings, with a mannequin to check against and the shot it chose"""
    st = context.scene.fitbridge
    st.target = ob
    if st.acc_type != d["type"]:
        st.acc_type = d["type"]
    st.attachment = d["attachment"]
    if not st.item_name:
        st.item_name = d["name"]
    if rig.shown() is None:
        from . import _ask_for_rig
        _ask_for_rig(st.body_scale)
        rig.spawn_r15(context, st.body_scale)
    g = ob.fitbridge_thumb
    thumb._set(g, d["turn"], d["tilt"], d["distance"], tuple(d["pan"]), (0.0, 0.0, 0.0))
    g.manual, g.axes = True, geom.AXES
    evaluate.mark_changed()


def tick(context):
    """every timer tick: take in finished requests, ask about a mesh with no answer, apply the one with an answer"""
    if not enabled():
        _run["applied"] = None
        return
    while not _done.empty():
        stage, name, fp, d, error = _done.get()
        try:
            _finished(context, stage, name, fp, d, error)
        except (ValueError, KeyError, TypeError) as e:
            _run.update(busy=None, failed=fp, message=str(e))
    evaluate.follow_selection(context)
    ob = context.scene.fitbridge.target
    if ob is None or evaluate.selection_problem(context):
        return
    fp = fingerprint(ob)
    d = answer(ob)
    if d is not None:
        if _run["applied"] != (ob.name, fp):
            _apply(context, ob, d)
            _run["applied"] = (ob.name, fp)
        if fit_on():
            if rig.shown() != "R6":
                from . import _ask_for_rig
                _ask_for_rig("R6")
                rig.spawn_r6(context)
                evaluate.mark_changed()
            _check_fit(context, ob, fp, d)
        return
    if _run["seen"] != fp:
        _run.update(seen=fp, since=time.time())
    p = prefs()
    if _run["busy"] or _run["failed"] == fp or not p.claude_api_key or time.time() - _run["since"] < SETTLE:
        return
    _start(context, ob, fp, p.claude_api_key)


def retry():
    _run.update(failed=None, message="")


# --------------------------------------------------------------------------------------------------------- the panel

def status(context):
    """what the panel says: (kind, line), kind = problem / no_key / looking / failed / blocked / wrong_way / misfit / ready"""
    problem = evaluate.selection_problem(context)
    if problem:
        return "problem", problem
    p = prefs()
    ob = context.scene.fitbridge.target
    d = answer(ob) if ob is not None else None
    if d is None:
        if ob is not None and _run["failed"] == fingerprint(ob):
            return "failed", _run["message"]
        if not (p and p.claude_api_key):
            return "no_key", "Add your Anthropic API key in FitBridge's preferences"
        return "looking", "Picking the best thumbnail..." if _run["settings"] and _run["busy"] else "Looking at the item..."
    res = evaluate.LIVE["result"]
    if _run["applied"] != (ob.name, fingerprint(ob)) or res is None or res.get("object") != ob.name:
        return "looking", "Setting it up..."
    way = wrong_way(ob, d)
    if way:
        return "wrong_way", way
    if res.get("fit") is None or not res["ok"]:
        bad = [c["message"] for c in res["checks"] if c["status"] == "fail"]
        return "blocked", bad[0] if bad else "It doesn't meet the checks yet"
    if fit_on():
        key = fit_key(ob, fingerprint(ob))
        v = fit_verdict(ob, key)
        if v is None:
            if _run["failed"] == key:
                return "failed", _run["message"]
            if not p.claude_api_key:
                return "no_key", "Add your Anthropic API key in FitBridge's preferences"
            return "looking", "Checking how it sits on the character..."
        if not v["ok"] and ob.get(IGNORE_FIT_KEY) != key:
            return "misfit", v["problem"] or "It doesn't look like it sits properly on the character"
    return "ready", d["summary"]


def ignore(context):
    """you've looked and it's fine: stop objecting to this item's current facing and placement"""
    ob = context.scene.fitbridge.target
    fp = fingerprint(ob)
    kind = status(context)[0]
    if kind == "wrong_way":
        ob[IGNORE_FACING_KEY] = fp
    elif kind == "misfit":
        ob[IGNORE_FIT_KEY] = fit_key(ob, fp)


def heads_ups(res):
    """the non-blocking warnings, which 'meets all the checks' shouldn't hide"""
    return [c["message"] for c in res["checks"] if c["status"] == "warn"]
