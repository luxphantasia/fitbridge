# Send all colours: a fitted hat and its recolours go to Studio as one set. Recolours are the objects sharing the original's
# mesh, and they borrow its fit, type, attachment and thumbnail, so re-fitting the original once updates them all. Every
# colour uploads first and Studio only hears about the set once all of them have, so a failure never leaves half a row.
# The main thread reads what's in Blender; everything after that runs on a thread and never touches bpy.

import base64
import hashlib
import json
import re
import threading
import urllib.request

import bpy

from . import auto, evaluate, geom, server, spec, thumb, upload
from .version import VERSION

MAX_NAME = 50
_lock = threading.Lock()
_state = {"state": "idle", "done": 0, "total": 0, "message": "", "revision": 0}


def prefs():
    a = bpy.context.preferences.addons.get(__package__)
    return a.preferences if a else None


def public():
    with _lock:
        return dict(_state)


def _set(**kw):
    with _lock:
        _state.update(kw)


def busy():
    return public()["state"] == "working"


def family(context, ob=None):
    """ob and every object sharing its mesh, or [] when there's nothing to go on"""
    ob = ob or evaluate.target_object(context)
    if ob is None or ob.type != "MESH" or ob.get("fitbridge_rig"):
        return []
    return [o for o in context.scene.objects if o.type == "MESH" and o.data is ob.data and not o.get("fitbridge_rig")]


def _sits(res):
    by = {c["id"]: c for c in res["checks"]}
    return res.get("fit") is not None and by.get("bounds", {}).get("status") == "pass"


def find_original(context, members):
    """(the one on the character, its checks, None) or (None, None, what's wrong in words)"""
    sitting = [(m, r) for m, r in ((m, evaluate.evaluate(context, m, publish=False)) for m in members) if _sits(r)]
    if not sitting:
        return None, None, "None of these sit on the character. Fit the original first."
    if len(sitting) > 1:
        active = context.view_layer.objects.active
        for m, r in sitting:
            if m is active and m.select_get():
                return m, r, None
        names = ", ".join(m.name for m, _r in sitting)
        return None, None, f"{len(sitting)} sit on the character ({names}). Select the original."
    return (*sitting[0], None)


def clean_name(name):
    """an object name as a title: no Blender number endings, no LOD tag, spaces instead of underscores"""
    name = re.sub(r"\.\d{3}$", "", name)
    name = re.sub(r"[_ ]LOD\d*$", "", name, flags=re.I)
    return re.sub(r"\s+", " ", name.replace("_", " ")).strip()


def base_name(context, ob):
    """the original's title: one you typed, else the one Auto chose, else its object name"""
    st = context.scene.fitbridge
    typed = (st.item_name if st.target is ob else "") or ob.get("fitbridge_name", "")
    if typed:
        return typed
    d = auto.answer(ob)
    return (d or {}).get("name") or clean_name(ob.name)


def colour_word(ob, original):
    """the colour from "Helmet 1 - Arctic", or None when the object name doesn't say"""
    name = re.sub(r"\.\d{3}$", "", ob.name)
    if " - " in name and name != original.name:
        word = name.rsplit(" - ", 1)[1].strip()
        return word or None
    return None


def item_name(colour, base):
    """colour + the original's name, shortened at a word to fit Roblox's limit. The colour always survives."""
    full = f"{colour} {base}"
    if len(full) <= MAX_NAME:
        return full
    room = MAX_NAME - len(colour) - 1
    cut = base[:room]
    if " " in cut and base[room:room + 1] not in ("", " "):
        cut = cut.rsplit(" ", 1)[0]
    return f"{colour} {cut.rstrip()}"


def unique(words):
    """two objects called the same colour would make two items of the same name"""
    seen, out = {}, []
    for w in words:
        seen[w] = seen.get(w, 0) + 1
        out.append(w if seen[w] == 1 else f"{w} {seen[w]}")
    return out


COLOUR_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["colour"],
                 "properties": {"colour": {"type": "string"}}}


def _ask_colour(key, png):
    """one or two plain marketplace words for this recolour"""
    body = {"model": auto.MODEL, "max_tokens": 1024, "fallbacks": "default",
            "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": COLOUR_SCHEMA}},
            "messages": [{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                             "data": base64.b64encode(png).decode()}},
                {"type": "text", "text": "This is a colour variant of a Roblox accessory. In colour, give one or two plain "
                                         "words a shop would use for this colourway, like \"Arctic\" or \"Forest Green\"."}]}]}
    req = urllib.request.Request(auto.API + "/v1/messages", data=json.dumps(body).encode(), method="POST", headers={
        "x-api-key": key, "anthropic-version": auto.API_VERSION, "content-type": "application/json",
        "anthropic-beta": "server-side-fallback-2026-07-01", "User-Agent": f"FitBridge-Blender/{VERSION}"})
    with urllib.request.urlopen(req, timeout=120) as r:
        word = str(auto.parse_reply(json.load(r))["colour"]).strip()
    if not word:
        raise ValueError("Claude didn't give a colour.")
    return word[:20]


def start(context):
    """gather everything from Blender and start the thread. Returns what's wrong in words, or None when it has started."""
    if busy():
        return "Already sending a set."
    if not evaluate.selection_problem(context) is None:
        return evaluate.selection_problem(context)
    members = family(context)
    if len(members) < 2:
        return "This hat has no recolours."
    original, res, problem = find_original(context, members)
    if problem:
        return problem
    if not res["ok"]:
        bad = [c["message"] for c in res["checks"] if c["status"] == "fail"]
        return f"{original.name} doesn't meet the checks: {bad[0] if bad else 'unknown'}"
    p = prefs()
    api_key = p.api_key if p else ""
    creator_id = str(p.creator_id).strip() if p else ""
    if not api_key:
        return "Add your Open Cloud key in FitBridge preferences first."
    if not creator_id.isdigit():
        return "Set the creator id in FitBridge preferences first."
    creator = {"type": p.creator_type, "id": int(creator_id)}
    others = sorted((m for m in members if m is not original), key=lambda o: o.name)
    words = {m.name: colour_word(m, original) for m in others}
    unnamed = [m for m in others if words[m.name] is None]
    claude_key = getattr(p, "claude_api_key", "") if p else ""
    if unnamed and not claude_key:
        return f"\"{unnamed[0].name}\" has no colour in its name. Rename it like \"{original.name} - Red\", or add an " \
               "Anthropic key so Claude can name it."
    g = original.fitbridge_thumb
    items = []
    for m in [original, *others]:
        mat = m.material_slots[0].material if m.material_slots else None
        maps = evaluate.find_textures(mat)
        if res["textures"].get("color") is not None and maps.get("color") is None:
            return f"{m.name} has no colour texture."
        for k, img in maps.items():
            if img is not None and not (geom.pow2(img.size[0]) and geom.pow2(img.size[1])):
                return f"{m.name}: its {k} map isn't a power-of-two size ({img.size[0]}x{img.size[1]})."
        items.append({"object": m.name, "colour": words.get(m.name), "original": m is original,
                      "raw": {k: evaluate.image_rgba8(img) if img is not None else None for k, img in maps.items()},
                      "pbr": any(maps[k] is not None for k in ("normal", "roughness", "metalness"))})
    shots = {}
    if unnamed:
        c, V = thumb.centred_verts(context, original)
        shot = (g.turn, g.tilt, *thumb.frame_shot(V, g.turn, g.tilt))
        for m in unnamed:
            co, _v = thumb.centred_verts(context, m)
            shots[m.name] = thumb.render_shots(context, m, co, [shot], 256)[0]
    mesh = evaluate.build_mesh(res)
    plain = evaluate.manifest_fields(context, res, g)
    rev = server.state["set_revision"] + 1
    _set(state="working", done=0, total=len(items), message=f"Getting {len(items)} colours ready...", revision=rev)
    threading.Thread(target=_work, name="FitBridge set", daemon=True, args=(
        rev, items, mesh, plain, base_name(context, original), original.name, api_key, creator, claude_key, shots,
        spec.limits())).start()
    return None


def _work(rev, items, mesh, plain, base, family_name, api_key, creator, claude_key, shots, lim):
    try:
        for it in items:
            if it["colour"] is None and not it["original"]:
                it["colour"] = _ask_colour(claude_key, shots[it["object"]])
        words = unique([it["colour"] for it in items if not it["original"]])
        for it, w in zip([i for i in items if not i["original"]], words):
            it["colour"], it["name"] = w, item_name(w, base)
        for it in items:
            if it["original"]:
                it["name"] = base[:MAX_NAME]
        mesh_blob = json.dumps(mesh).encode()
        mesh_sha = hashlib.sha256(mesh_blob).hexdigest()
        blobs = {"/v1/set/mesh": json.dumps({"protocol": 1, "revision": rev, "sha256": mesh_sha, **mesh}).encode()}
        tdir = spec.cache_dir() / "textures"
        tdir.mkdir(exist_ok=True)
        out = []
        for i, it in enumerate(items):
            _set(message=f"Uploading {it['name']} ({i + 1} of {len(items)})")
            target = geom.send_target(lim, it["pbr"])
            textures, pixels = {}, {}
            for k, raw in it.pop("raw").items():
                if raw is None:
                    textures[k] = None
                    continue
                entry, blob, rgba = evaluate.texture_entry(k, geom.shrink_rgba(raw, k, target), tdir, f"set{i}_")
                textures[k] = entry
                pixels[k] = rgba
                blobs[f"/v1/set/item/{i}/texture/{k}"] = json.dumps(blob).encode()
            manifest = {**plain, "protocol": 1, "revision": rev, "name": it["name"], "textures": textures, "pbr": it["pbr"],
                        "mesh": {**plain["mesh"], "sha256": mesh_sha}, "sendable": True, "override": False,
                        "colour": it["colour"], "is_original": it["original"]}
            aid, cached = upload.upload_one(api_key, creator, it["name"], mesh, pixels, it["pbr"],
                                            upload.cache_key(manifest, creator))
            manifest["upload"] = {"state": "done", "model_asset_id": aid, "creator": creator,
                                  "message": f"Uploaded ✓ model {aid}" + (" (unchanged)" if cached else "")}
            out.append(manifest)
            _set(done=i + 1)
        server.publish_set(blobs, {"protocol": 1, "revision": rev, "family": family_name, "name": base, "items": out}, rev)
        _set(state="done", message=f"Sent {len(out)} colours to Studio")
    except upload.OpenCloudError as e:
        _set(state="failed", message=f"Stopped, nothing built: {e}. Press again to retry; finished uploads are kept.")
    except Exception as e:                                 # never let a key or a traceback reach the panel
        kind = str(e) if isinstance(e, ValueError) else type(e).__name__
        _set(state="failed", message=f"Stopped, nothing built ({kind}). Press again to retry; finished uploads are kept.")
