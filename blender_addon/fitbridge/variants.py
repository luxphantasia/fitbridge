"""Colour versions: type how many, click once, and get that many recoloured copies of the accessory, all sharing its
mesh (so Send all colours sees one family) and laid out in a grid of 4 beside it. Lux's choices, 2026-11-12.

- Claude picks colourways that suit the item from one small picture and its name (about 1-2 cents). Without a key,
  a fixed shop palette is used instead, and the panel says so.
- The whole colour texture is recoloured with a gradient map: every pixel keeps how light or dark it is (shading,
  folds, stitching) and only its colour changes, so it lines up with the UVs exactly. Dark items are stretched so the
  new colour shows. Normal / roughness maps stay shared. No colour texture: the material's base colour is set.
- New textures are PNGs in "<item> colours/" next to the .blend (FitBridge's own folder while the file is unsaved).
- Copies are named "<item> - <Colour>", which Send all colours reads as the colour. A second click adds new colours.
"""

import base64
import json
import os
import queue
import re
import threading
import urllib.request

import bpy
import numpy as np
from mathutils import Vector

from . import auto, batch, evaluate, host, thumb
from .version import VERSION

MAX = 12
STRETCH = 2.2                                  # how far the recolour may stretch the texture's own contrast
PER_ROW = 4
PALETTE = [("Black", "#1b1b1f"), ("White", "#ecebe6"), ("Red", "#c4232b"), ("Navy", "#1f2f5c"), ("Pink", "#f08cb4"),
           ("Purple", "#6c3fa8"), ("Forest Green", "#2f6b3a"), ("Brown", "#6b4428"), ("Baby Blue", "#8ec5ec"),
           ("Yellow", "#f2c531"), ("Grey", "#7c7f86"), ("Orange", "#e8742a"), ("Teal", "#1f8f8a"), ("Lavender", "#b9a3e3"),
           ("Mint", "#9fdcc2"), ("Maroon", "#6a1a26"), ("Cream", "#efe2c4"), ("Lime", "#a6d63a")]
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["colours"], "properties": {"colours": {
    "type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["name", "hex"],
                               "properties": {"name": {"type": "string"}, "hex": {"type": "string"}}}}}}

_done = queue.Queue()
STATE = {"busy": False, "message": "", "kind": "info"}


# ------------------------------------------------------------------------------------------------- colour maths

def _srgb_to_lin(c):
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def _lin_to_srgb(c):
    c = np.clip(c, 0.0, 1.0)
    return np.where(c <= 0.0031308, c * 12.92, 1.055 * np.power(c, 1 / 2.4) - 0.055)


_M1 = np.array([[0.4122214708, 0.5363325363, 0.0514459929], [0.2119034982, 0.6806995451, 0.1073969566],
                [0.0883024619, 0.2817188376, 0.6299787005]])
_M2 = np.array([[0.2104542553, 0.7936177850, -0.0040720468], [1.9779984951, -2.4285922050, 0.4505937099],
                [0.0259040371, 0.7827717662, -0.8086757660]])
_M2i = np.linalg.inv(_M2)
_M1i = np.linalg.inv(_M1)


def _to_oklab(rgb):
    """sRGB (0-1, ..., 3) -> OKLab"""
    lms = _srgb_to_lin(rgb) @ _M1.T
    return np.cbrt(lms) @ _M2.T


def _from_oklab(lab):
    return _lin_to_srgb((lab @ _M2i.T) ** 3 @ _M1i.T)


def _in_gamut(lab):
    lin = ((lab @ _M2i.T) ** 3) @ _M1i.T
    return np.all((lin >= -1e-4) & (lin <= 1 + 1e-4), axis=-1)


def hex_rgb(h):
    h = h.strip().lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", h):
        raise ValueError(f"not a colour: {h}")
    return np.array([int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)])


def gradient_map(rgb, alpha, target):
    """recolour rgb (N x 3, sRGB 0-1) to `target` (sRGB 0-1), keeping each pixel's lightness order and detail.
    The item's most common tone (its median lightness) becomes the target colour itself; darker pixels ramp down to
    a deep shade of it, lighter ones up to a pale one. So a black velvet hood turns into red velvet, not black with red
    highlights."""
    L = _to_oklab(rgb)[:, 0]
    seen = alpha > 0.5 if alpha is not None and np.any(alpha > 0.5) else np.ones(len(L), bool)
    lo, mid, hi = np.percentile(L[seen], [2, 50, 98]) if seen.any() else (0.0, 0.5, 1.0)
    below = np.clip((L - lo) / max(mid - lo, 0.02), 0, 1)           # 0 = deepest shadow, 1 = the main tone
    above = np.clip((L - mid) / max(hi - mid, 0.02), 0, 1)          # 0 = the main tone, 1 = brightest highlight
    tl = _to_oklab(target[None])[0]
    Lt = tl[0]
    ct = np.hypot(tl[1], tl[2])
    hue = np.arctan2(tl[2], tl[1])
    dark = Lt - (Lt - 0.08) * 0.55                                  # the ramp: shadows, the colour itself, highlights
    light = Lt + (0.98 - Lt) * 0.7
    up = L > mid
    # differences in lightness are stretched to fill the ramp, but at most STRETCH times, so the faint grain of a dark
    # fabric doesn't turn into blotches on a light colour
    k_dn = min((Lt - dark) / max(mid - lo, 0.02), STRETCH)
    k_up = min((light - Lt) / max(hi - mid, 0.02), STRETCH)
    newL = np.where(up, np.minimum(Lt + (L - mid) * k_up, light), np.maximum(Lt - (mid - L) * k_dn, dark))
    newC = ct * np.where(up, 1 - 0.6 * above, 0.75 + 0.25 * below)  # a little less colour in shadow and highlight
    lab = np.stack([newL, newC * np.cos(hue), newC * np.sin(hue)], -1)
    scale = np.ones(len(lab))                                       # bring out-of-gamut pixels in by lowering colour
    for _ in range(10):
        bad = ~_in_gamut(lab)
        if not bad.any():
            break
        scale[bad] *= 0.8
        lab[bad, 1:] = np.stack([newC[bad] * scale[bad] * np.cos(hue), newC[bad] * scale[bad] * np.sin(hue)], -1)
    return _from_oklab(lab)


# -------------------------------------------------------------------------------------------- what's on the item

def colour_image(ob):
    mat = ob.active_material
    return mat, evaluate.find_textures(mat).get("color") if mat else None


def base_name(context, ob):
    return batch.base_name(context, ob)


def used_colours(context, ob):
    fam = batch.family(context, ob)
    return [w for w in (batch.colour_word(m, ob) for m in fam if m is not ob) if w]


def out_dir(context, base):
    safe = re.sub(r'[\\/:*?"<>|]+', "", base).strip() or "Item"
    if bpy.data.filepath:
        d = os.path.join(os.path.dirname(bpy.data.filepath), f"{safe} colours")
    else:
        d = os.path.join(host.user_dir(), "colours", safe)
    os.makedirs(d, exist_ok=True)
    return d


def palette_pick(n, avoid, current=None):
    taken = {a.lower() for a in avoid}
    out = []
    cur = _to_oklab(current[None])[0] if current is not None else None
    for name, h in PALETTE:
        if name.lower() in taken:
            continue
        if cur is not None and np.linalg.norm(_to_oklab(hex_rgb(h)[None])[0] - cur) < 0.08:
            continue                                                 # too close to what it already is
        out.append({"name": name, "hex": h})
        if len(out) == n:
            break
    return out


def _mean_colour(img):
    if img is None or img.size[0] == 0:
        return None
    px = np.empty(img.size[0] * img.size[1] * 4, np.float32)
    img.pixels.foreach_get(px)
    px = px.reshape(-1, 4)[:: max(1, len(px) // 16384 // 4)]
    keep = px[:, 3] > 0.5
    return px[keep, :3].mean(0) if keep.any() else None


# ------------------------------------------------------------------------------------------------- asking Claude

def _prompt(n, name, avoid):
    return (f"This is a Roblox UGC accessory called \"{name}\". Suggest {n} colourways to sell it in: colours that suit "
            "its style and that buyers would want, clearly different from each other and from its current colour"
            + (f" and from these existing versions: {', '.join(avoid)}" if avoid else "") + ". The whole texture will be "
            "recoloured, keeping its shading. For each, give a short shop name (one or two words, like \"Blood Red\" or "
            "\"Arctic\") and its main colour as a hex code like #8a1c2b.")


def _worker(key, png, n, name, avoid):
    try:
        body = {"model": auto.MODEL, "max_tokens": 2048, "fallbacks": "default",
                "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
                "messages": [{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                 "data": base64.b64encode(png).decode()}},
                    {"type": "text", "text": _prompt(n, name, avoid)}]}]}
        req = urllib.request.Request(auto.API + "/v1/messages", data=json.dumps(body).encode(), method="POST", headers={
            "x-api-key": key, "anthropic-version": auto.API_VERSION, "content-type": "application/json",
            "anthropic-beta": "server-side-fallback-2026-07-01", "User-Agent": f"FitBridge-Blender/{VERSION}"})
        with urllib.request.urlopen(req, timeout=120) as r:
            got = auto.parse_reply(json.load(r))["colours"]
        _done.put((got, None))
    except Exception as e:  # noqa: BLE001 - any failure falls back to the palette, in words
        _done.put((None, auto._sentence(e)))


def clean_picks(picks, n, avoid):
    """Claude's answer, checked: real hex codes, no repeats, nothing already made, at most n"""
    out, seen = [], {a.lower() for a in avoid}
    for p in picks or []:
        name = re.sub(r"\s+", " ", str(p.get("name", ""))).strip().title()[:24]
        try:
            hex_rgb(str(p.get("hex", "")))
        except ValueError:
            continue
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append({"name": name, "hex": "#" + str(p["hex"]).strip().lstrip("#").lower()})
        if len(out) == n:
            break
    return out


# ---------------------------------------------------------------------------------------------- making the copies

def _grid_spot(ob, k):
    """where copy number k (0-based) goes: rows of 4 to the item's right, each row further back"""
    xs = [ob.matrix_world @ Vector(c) for c in ob.bound_box]
    w = max(v.x for v in xs) - min(v.x for v in xs)
    d = max(v.y for v in xs) - min(v.y for v in xs)
    step_x, step_y = max(w, 0.5) * 1.35, max(d, 0.5) * 1.35
    start = max(v.x for v in xs) + max(2.5, w)                       # clear of the mannequin's arm
    col, row = k % PER_ROW, k // PER_ROW
    loc = ob.matrix_world.translation.copy()
    loc.x = start + step_x * (col + 0.5)
    loc.y += step_y * row
    return loc


def make_copies(context, ob, picks):
    """the copies themselves (main thread). Returns the new objects."""
    mat, img = colour_image(ob)
    base = base_name(context, ob)
    folder = out_dir(context, base) if img is not None else None
    if img is not None:
        w, h = img.size
        px = np.empty(w * h * 4, np.float32)
        img.pixels.foreach_get(px)
        px = px.reshape(-1, 4)
        alpha = px[:, 3] if img.alpha_mode != "NONE" else None
    existing = len([m for m in batch.family(context, ob) if m is not ob])
    made = []
    for i, p in enumerate(picks):
        target = hex_rgb(p["hex"])
        cp = ob.copy()                                               # same mesh: the family Send all colours knows
        cp.name = f"{base} - {p['name']}"
        for c in ob.users_collection:
            c.objects.link(cp)
        cp.select_set(False)                                         # the original stays the one selected accessory
        cp.matrix_world.translation = _grid_spot(ob, existing + i)
        for key in list(cp.keys()):
            if key.startswith("fitbridge_listing"):
                del cp[key]
        if mat is not None:
            m2 = mat.copy()
            m2.name = f"{mat.name} {p['name']}"
            if img is not None:
                out = px.copy()
                out[:, :3] = gradient_map(px[:, :3].astype(np.float64), alpha, target)
                im2 = bpy.data.images.new(f"{base} {p['name']}", w, h, alpha=img.alpha_mode != "NONE")
                im2.colorspace_settings.name = img.colorspace_settings.name
                im2.pixels.foreach_set(out.ravel().astype(np.float32))
                path = os.path.join(folder, re.sub(r'[\\/:*?"<>|]+', "", p["name"]) + ".png")
                im2.filepath_raw = path
                im2.file_format = "PNG"
                im2.save()
                for node in m2.node_tree.nodes:
                    if node.type == "TEX_IMAGE" and node.image is img:
                        node.image = im2
            else:
                bsdf = evaluate._principled(m2)
                if bsdf is not None:
                    lin = _srgb_to_lin(target)
                    bsdf.inputs["Base Color"].default_value = (*lin, 1.0)
            if cp.material_slots:
                cp.material_slots[0].link = "OBJECT"
                cp.material_slots[0].material = m2
        made.append(cp)
    return made


# --------------------------------------------------------------------------------------------- start and finish

_job = {}


def start(context, n):
    """begin making n colour versions. Returns a sentence when it can't start."""
    ob = evaluate.target_object(context)
    if ob is None or evaluate.selection_problem(context):
        return evaluate.selection_problem(context) or "Select the accessory mesh"
    if STATE["busy"]:
        return "Already making colours"
    n = max(1, min(MAX, int(n)))
    avoid = used_colours(context, ob)
    _job.clear()
    _job.update(object=ob.name, n=n, avoid=avoid, scene=context.scene.name)
    p = host.prefs()
    key = getattr(p, "claude_api_key", "") if p else ""
    if not key:
        _finish(context, palette_pick(n, avoid, _mean_colour(colour_image(ob)[1])),
                "No Anthropic key, so these come from the shop palette")
        return None
    try:
        centre, V = thumb.centred_verts(context, ob)
        shot = (35.0, 15.0, *thumb.frame_shot(V, 35.0, 15.0))
        png = thumb.render_shots(context, ob, centre, [shot], 384)[0]
    except Exception as e:  # noqa: BLE001 - a picture is nice to have; the name still works
        print("FitBridge colours: no picture:", e)
        _finish(context, palette_pick(n, avoid, _mean_colour(colour_image(ob)[1])),
                "Couldn't take a picture for Claude, so these come from the shop palette")
        return None
    STATE.update(busy=True, message=f"Claude is picking {n} colours...", kind="busy")
    threading.Thread(target=_worker, args=(key, png, n, base_name(context, ob), avoid), daemon=True).start()
    bpy.app.timers.register(_tick, first_interval=0.3)
    return None


def _tick():
    try:
        got, error = _done.get_nowait()
    except queue.Empty:
        return 0.3
    ctx = bpy.context
    ob = bpy.data.objects.get(_job.get("object", ""))
    if ob is None:
        STATE.update(busy=False, message="The accessory was removed before the colours came back", kind="bad")
        return None
    n, avoid = _job["n"], _job["avoid"]
    picks = clean_picks(got, n, avoid) if got else []
    note = None
    if len(picks) < n:
        more = palette_pick(n - len(picks), avoid + [p["name"] for p in picks],
                            _mean_colour(colour_image(ob)[1]))
        picks += more
        note = (f"{error} Used the shop palette instead" if error else
                "Claude gave fewer colours than asked; the rest come from the shop palette") if more else None
    _finish(ctx, picks, note, ob)
    return None


def _finish(context, picks, note=None, ob=None):
    ob = ob or bpy.data.objects.get(_job.get("object", ""))
    try:
        made = make_copies(context, ob, picks) if picks else []
    except Exception as e:  # noqa: BLE001 - say what happened instead of a silent half job
        STATE.update(busy=False, message=f"Couldn't make the colours: {e}", kind="bad")
        return
    names = ", ".join(p["name"] for p in picks)
    msg = f"Made {len(made)} colour{'s' if len(made) != 1 else ''}: {names}" if made else "No new colours to make"
    if note:
        msg += f". {note}"
    STATE.update(busy=False, message=msg, kind="good" if made and not note else "info")
    if made:
        try:
            bpy.ops.ed.undo_push(message="Make colours")       # Ctrl+Z takes the whole set back off
        except RuntimeError:
            pass
    evaluate.mark_changed()
    for w in bpy.context.window_manager.windows:
        for a in w.screen.areas:
            a.tag_redraw()


class FB_OT_make_colours(bpy.types.Operator):
    bl_idname = "fitbridge.make_colours"
    bl_label = "Make colours"
    bl_description = "Make this many recoloured copies of the accessory, sharing its mesh, in a grid beside it. " \
                     "Claude picks colours that suit it (about 1-2 cents); without a key, a shop palette is used. " \
                     "Textures are saved next to your .blend"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return context.mode == "OBJECT" and not STATE["busy"] and evaluate.target_object(context) is not None

    def execute(self, context):
        why = start(context, context.scene.fitbridge.colour_count)
        if why:
            self.report({"WARNING"}, why)
            return {"CANCELLED"}
        return {"FINISHED"}


classes = (FB_OT_make_colours,)
