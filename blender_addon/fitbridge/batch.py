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

from . import auto, evaluate, geom, host, server, spec, thumb, upload
from .version import VERSION

MAX_NAME = 50
_lock = threading.Lock()
_state = {"state": "idle", "done": 0, "total": 0, "message": "", "revision": 0}


def prefs():
    return host.prefs()


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


def colour_manifest(plain, rev, it, textures, mesh_sha, sendable=True):
    """one colour's manifest in a set: the /v1/package shape plus `colour` and `is_original`, without its `upload`
    block (Send all colours adds that once the colour has uploaded; a set that is only built never gets one)"""
    return {**plain, "protocol": 1, "revision": rev, "name": it["name"], "textures": textures, "pbr": it["pbr"],
            "mesh": {**plain["mesh"], "sha256": mesh_sha}, "sendable": bool(sendable), "override": False,
            "colour": it["colour"], "is_original": it["original"]}


def build_set(context, original, others, res, rev, labels=None, colour_only=False):
    """The set Send all colours would make, built here and never uploaded: no Open Cloud call, no thread, nothing
    told to Studio. For a caller that hands the finished item to something else (the UGC autopilot gives it to
    Paradyse, which publishes only after a check and an approval).

    original = the fitted mesh, others = its recolours (sharing its mesh), res = the original's evaluate() result.
    labels = {object name: colour word} for copies whose name doesn't say. colour_only leaves out normal, roughness
    and metalness maps, so each colour is one colour map at the colour-only size.

    Returns {"manifest": /v1/set's description (items carry no `upload`), "mesh": /v1/set/mesh's JSON as bytes,
    "items": [{"object", "colour", "name", "original", "pixels": {map: RGBA8, top row first}, "png": the colour
    map as PNG bytes or None, "glb": exactly what upload.write_glb would send}]}, the original first.
    Raises ValueError with a sentence when the set can't be built."""
    if res.get("fit") is None or res.get("mesh") is None:
        raise ValueError(f"{original.name} isn't fitted yet.")
    labels = labels or {}
    base = base_name(context, original)
    lim = spec.limits()
    words = unique([labels.get(m.name) or colour_word(m, original) or m.name for m in others])
    mesh = evaluate.build_mesh(res)
    mesh_sha = hashlib.sha256(json.dumps(mesh).encode()).hexdigest()
    plain = evaluate.manifest_fields(context, res, original.fitbridge_thumb)
    built, manifests = [], []
    for m, word in [(original, None), *zip(others, words)]:
        mat = m.material_slots[0].material if m.material_slots else None
        maps = evaluate.find_textures(mat)
        if colour_only:
            maps = {k: (img if k == "color" else None) for k, img in maps.items()}
        if res["textures"].get("color") is not None and maps.get("color") is None:
            raise ValueError(f"{m.name} has no colour texture.")
        pbr = any(maps[k] is not None for k in ("normal", "roughness", "metalness"))
        target = geom.send_target(lim, pbr)
        textures, pixels = {}, {}
        for k, img in maps.items():
            if img is None:
                textures[k] = None
                continue
            if not (geom.pow2(img.size[0]) and geom.pow2(img.size[1])):
                raise ValueError(f"{m.name}: its {k} map isn't a power-of-two size ({img.size[0]}x{img.size[1]}).")
            rgba = geom.shrink_rgba(evaluate.image_rgba8(img), k, target)
            textures[k] = {"sha256": hashlib.sha256(rgba.tobytes()).hexdigest(), "width": int(rgba.shape[1]),
                           "height": int(rgba.shape[0])}
            pixels[k] = rgba
        it = {"object": m.name, "colour": word, "original": m is original,
              "name": base[:MAX_NAME] if m is original else item_name(word, base), "pbr": pbr}
        manifests.append(colour_manifest(plain, rev, it, textures, mesh_sha, sendable=res["ok"]))
        built.append({**it, "pixels": pixels, "png": upload.png_bytes(pixels["color"]) if "color" in pixels else None,
                      "glb": upload.write_glb(mesh, pixels, pbr, it["name"])})
    return {"manifest": {"protocol": 1, "revision": rev, "family": original.name, "name": base, "items": manifests},
            "mesh": json.dumps({"protocol": 1, "revision": rev, "sha256": mesh_sha, **mesh}).encode(),
            "items": built}


def start(context):
    """gather everything from Blender and start the thread. Returns what's wrong in words, or None when it has started."""
    if busy():
        return "Already sending a set."
    if upload.locked():
        return upload.LOCKED
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
            manifest = colour_manifest(plain, rev, it, textures, mesh_sha)
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
