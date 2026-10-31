# The rules and the rig: the bundled spec (ugc_spec.json, a copy of the shared contract's spec), live Roblox rules
# synced from Studio (they win when present), and the R15 rig per body scale (synced from Studio, or a stand-in of
# boxes until then). Rules and rigs are cached on disk so they survive restarts. No bpy at import time.

import json
import os
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC = json.loads((HERE / "ugc_spec.json").read_text())
SCALES = list(SPEC["body_scales"])
TYPES = list(SPEC["accessory_types"])
CHECK_IDS = [k for k, v in SPEC["checks"].items() if isinstance(v, dict)]

_cache_dir = None


def cache_dir():
    """where rules, rigs and sent textures are kept: the extension's user folder (or FITBRIDGE_CACHE for tests)"""
    global _cache_dir
    if _cache_dir is None:
        p = os.environ.get("FITBRIDGE_CACHE")
        if not p:
            import bpy
            try:
                p = bpy.utils.extension_path_user(__package__, path="", create=True)
            except (ValueError, TypeError, AttributeError):
                p = bpy.utils.user_resource("CONFIG", path="fitbridge", create=True)
        _cache_dir = Path(p)
        _cache_dir.mkdir(parents=True, exist_ok=True)
    return _cache_dir


def _read(name):
    f = cache_dir() / name
    if f.exists():
        try:
            return json.loads(f.read_text())
        except (OSError, ValueError):
            return None
    return None


def _write(name, data):
    f = cache_dir() / name
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(f)


# ------------------------------------------------------------------------------------------------------------- rules

_rules = {"loaded": False, "data": None}


def live_rules():
    if not _rules["loaded"]:
        _rules["data"] = _read("rules.json")
        _rules["loaded"] = True
    return _rules["data"]


def attachments_for(acc_type):
    """[(attachment name, label)] for a type"""
    t = SPEC["accessory_types"][acc_type]
    return [(k, v.get("label", k)) for k, v in t["attachments"].items()]


def rig_part_for(acc_type, attachment):
    t = SPEC["accessory_types"][acc_type]
    return t["attachments"][attachment].get("rig_part") or t.get("rig_part")


def bundled_bounds(acc_type, attachment, scale):
    atts = SPEC["accessory_types"][acc_type]["attachments"]
    b = atts[attachment]["bounds"]
    while isinstance(b, str) and b.startswith("same_as:"):
        b = atts[b.split(":", 1)[1]]["bounds"]
    return b[scale]


def bounds(acc_type, attachment, scale):
    """(size, offset, source text). Live rules hold one box per attachment, which is what Roblox validates every
    upload against, so when they're synced they win for every body scale."""
    live = live_rules()
    if live:
        asset = SPEC["accessory_types"][acc_type]["asset_type"]
        box = ((live.get("accessories") or {}).get(asset) or {}).get("attachments", {}).get(attachment)
        if box and box.get("size"):
            return list(box["size"]), list(box.get("offset") or (0, 0, 0)), "live"
    b = bundled_bounds(acc_type, attachment, scale)
    return list(b["size"]), list(b.get("offset") or (0, 0, 0)), "bundled"


def limits():
    lim = dict(SPEC["limits"])
    live = live_rules()
    if live:
        for k, v in (live.get("limits") or {}).items():
            if v:
                lim[k] = v
    return lim


def spec_source():
    live = live_rules()
    if live:
        return "live-" + str(live.get("fetched", "unknown"))
    return "bundled-" + SPEC.get("updated", "")


def rules_text():
    live = live_rules()
    if live:
        return f"Rules: live from Studio, {str(live.get('fetched', '?'))[:10]}"
    return "Rules: bundled snapshot"


def matched_scale(live):
    """which body-scale column of the bundled spec the live boxes agree with (tells us what Roblox validates at)"""
    votes = {s: 0 for s in SCALES}
    total = 0
    for tname, t in SPEC["accessory_types"].items():
        box_by_att = ((live.get("accessories") or {}).get(t["asset_type"]) or {}).get("attachments") or {}
        for att in t["attachments"]:
            box = box_by_att.get(att)
            if not box or not box.get("size"):
                continue
            total += 1
            for s in SCALES:
                b = bundled_bounds(tname, att, s)
                if all(abs(x - y) < 0.011 for x, y in zip(b["size"], box["size"])):
                    votes[s] += 1
    best = max(votes, key=votes.get)
    return (best if votes[best] else None), votes, total


def save_rules(body):
    data = {k: body.get(k) for k in ("fetched", "studio_version", "limits", "accessories")}
    data["fetched"] = data["fetched"] or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    scale, votes, total = matched_scale(data)
    data["matched_scale"] = scale
    data["match_votes"] = votes
    _write("rules.json", data)
    if body.get("raw") is not None:
        _write("rules_raw.json", body["raw"])
    _rules["data"], _rules["loaded"] = data, True
    print(f"FitBridge: live rules synced ({total} attachment boxes); they match the bundled '{scale}' column "
          f"(votes {votes})")
    return data


# --------------------------------------------------------------------------------------------------------------- rig

# A stand-in R15 made of boxes (Roblox space, root part at the origin, like Studio's rig after PivotTo(0, 0, 0)).
# Roughly the classic blocky R15; the exact rig per body scale comes from Studio.
_STANDIN_PARTS = {
    "Head": ((2, 1, 1), (0, 1.5, 0)),
    "UpperTorso": ((2, 1.6, 1), (0, 0.2, 0)),
    "LowerTorso": ((2, 0.4, 1), (0, -0.8, 0)),
    "LeftUpperArm": ((1, 0.9, 1), (-1.5, 0.55, 0)),
    "LeftLowerArm": ((1, 0.8, 1), (-1.5, -0.3, 0)),
    "LeftHand": ((1, 0.3, 1), (-1.5, -0.85, 0)),
    "RightUpperArm": ((1, 0.9, 1), (1.5, 0.55, 0)),
    "RightLowerArm": ((1, 0.8, 1), (1.5, -0.3, 0)),
    "RightHand": ((1, 0.3, 1), (1.5, -0.85, 0)),
    "LeftUpperLeg": ((1, 0.9, 1), (-0.5, -1.45, 0)),
    "LeftLowerLeg": ((1, 0.8, 1), (-0.5, -2.3, 0)),
    "LeftFoot": ((1, 0.3, 1), (-0.5, -2.85, 0)),
    "RightUpperLeg": ((1, 0.9, 1), (0.5, -1.45, 0)),
    "RightLowerLeg": ((1, 0.8, 1), (0.5, -2.3, 0)),
    "RightFoot": ((1, 0.3, 1), (0.5, -2.85, 0)),
}
_STANDIN_ATTS = {
    "HatAttachment": ("Head", (0, 2.0, 0)), "HairAttachment": ("Head", (0, 2.0, 0)),
    "FaceFrontAttachment": ("Head", (0, 1.5, -0.5)), "FaceCenterAttachment": ("Head", (0, 1.5, 0)),
    "NeckAttachment": ("UpperTorso", (0, 1.0, 0)),
    "BodyFrontAttachment": ("UpperTorso", (0, 0.2, -0.5)), "BodyBackAttachment": ("UpperTorso", (0, 0.2, 0.5)),
    "LeftCollarAttachment": ("UpperTorso", (-1, 1.0, 0)), "RightCollarAttachment": ("UpperTorso", (1, 1.0, 0)),
    "LeftShoulderAttachment": ("LeftUpperArm", (-1.5, 1.0, 0)),
    "RightShoulderAttachment": ("RightUpperArm", (1.5, 1.0, 0)),
    "WaistFrontAttachment": ("LowerTorso", (0, -1.0, -0.5)), "WaistCenterAttachment": ("LowerTorso", (0, -1.0, 0)),
    "WaistBackAttachment": ("LowerTorso", (0, -1.0, 0.5)),
}


def standin_rig(scale):
    ident = [1, 0, 0, 0, 1, 0, 0, 0, 1]
    return {"body_scale": scale, "standin": True,
            "parts": [{"name": n, "size": list(s), "cframe": list(p) + ident, "mesh": None}
                      for n, (s, p) in _STANDIN_PARTS.items()],
            "attachments": [{"name": n, "part": part, "world_cframe": list(p) + ident}
                            for n, (part, p) in _STANDIN_ATTS.items()]}


_rigs = {}


def cached_rig(scale):
    if scale not in _rigs:
        _rigs[scale] = _read(f"rig_{scale}.json")
    return _rigs[scale]


def rig(scale):
    """the synced rig for a body scale, or the stand-in"""
    return cached_rig(scale) or standin_rig(scale)


def save_rig(body):
    scale = body.get("body_scale")
    if scale not in SCALES:
        raise ValueError(f"Unknown body scale {scale!r}")
    data = {"body_scale": scale, "parts": body.get("parts") or [], "attachments": body.get("attachments") or []}
    _write(f"rig_{scale}.json", data)
    _rigs[scale] = data
    return scale


def attachment_frame(rig_data, name):
    from .geom import cframe_to_matrix
    for a in rig_data["attachments"]:
        if a["name"] == name:
            return cframe_to_matrix(a["world_cframe"])
    return None
