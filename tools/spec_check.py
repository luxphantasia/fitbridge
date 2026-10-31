"""Checks spec/ugc_spec.json against Roblox's current docs and this Mac's Studio validation code.

  python3 tools/spec_check.py            # report only; exit code 1 if anything changed
  python3 tools/spec_check.py --json     # machine-readable report (for the weekly agent)

What it compares:
  1. Bounds tables (every type, attachment and body scale, including off-centre offsets),
     the triangle budget and the max texture size from the Markdown version of the docs page.
  2. Hashes of the Studio UGCValidation files our checks copy, so we notice when Roblox
     changes how validation works (Studio updates itself weekly).
It never edits the spec. A person approves changes (see docs/UPDATING_SPECS.md).
"""

import hashlib
import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "spec" / "ugc_spec.json"
HASHES = ROOT / "spec" / "studio_validation_hashes.json"
DOCS = "https://create.roblox.com/docs/art/accessories/specifications.md"
UGCV = Path("/Applications/RobloxStudio.app/Contents/Resources/ExtraContent/LuaPackages/Packages/_Index/UGCValidation/UGCValidation")
WATCHED = [
    "validation/validateMeshBounds.lua",
    "validation/validateThumbnailConfiguration.lua",
    "validation/validateMeshPartAccessory.lua",
    "validation/validateMeshTriangles.lua",
    "util/createMeshPartAccessorySchema.lua",
    "util/ValidationRulesUtil.lua",
    "validationFolders/AttributesAllowed/AttributesAllowed.lua",
    "Constants.lua",
]
TOL = 0.006  # docs round to 2 decimals

# Docs row label → [(accessory type, attachment), ...] in our spec.
ROWS = {
    "Hat": [("Hat", "HatAttachment")],
    "Hair": [("Hair", "HairAttachment")],
    "Face": [("Face", "FaceFrontAttachment")],
    "Eyebrow and Eyelashes": [("Eyebrow", "FaceFrontAttachment"), ("Eyelash", "FaceFrontAttachment")],
    "Neck": [("Neck", "NeckAttachment")],
    "Shoulder (attached to NeckAttachment)": [("Shoulder", "NeckAttachment")],
    "Shoulder (other)": [("Shoulder", "LeftCollarAttachment"), ("Shoulder", "LeftShoulderAttachment")],
    "Shoulder (attached to RightCollarAttachment, LeftCollarAttachment)": [("Shoulder", "LeftCollarAttachment")],
    "Shoulder (attached to RightShoulderAttachment, LeftShoulderAttachment)": [("Shoulder", "LeftShoulderAttachment")],
    "Front": [("Front", "BodyFrontAttachment")],
    "Back": [("Back", "BodyBackAttachment")],
    "Waist": [("Waist", "WaistFrontAttachment")],
}


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "FitBridge-spec-check/0.1"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8")


def parse_cell(cell):
    """'4.87* <br> (Not centered: 1.623 front, 3.246 behind)' → (4.87, offset or None, raw note)."""
    value = float(re.match(r"\s*([\d.]+)", cell).group(1))
    m = re.search(r"([\d.]+)\s*(up|front),\s*([\d.]+)\s*(down|behind)", cell)
    if not m:
        return value, None, None
    a, b = float(m.group(1)), float(m.group(3))
    note = m.group(0)
    if abs((a + b) - value) > 0.02:
        return value, None, f"docs don't add up: {note} vs {value}"
    if m.group(2) == "up":
        return value, (a - b) / 2, None  # +Y is up
    return value, (b - a) / 2, None  # -Z is front, so more behind → +Z


def parse_docs(md):
    tables = {}
    for scale in ("Classic", "Normal", "Slender"):
        m = re.search(rf"#### {scale}\n(.*?)(?:\n#|\Z)", md, re.S)
        if not m:
            continue
        rows = {}
        for line in m.group(1).splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) != 4 or cells[0] in ("Asset Type", "---"):
                continue
            rows[cells[0]] = [parse_cell(c) for c in cells[1:]]
        tables[scale] = rows
    tris = re.search(r"\*\*(\d+)k\*\*\s*triangles", md)
    tex = re.search(r"cannot exceed (\d+)x(\d+)", md)
    return tables, (int(tris.group(1)) * 1000 if tris else None), (int(tex.group(1)) if tex else None)


def resolve(attachments, name):
    entry = attachments[name]
    while isinstance(entry["bounds"], str):
        entry = attachments[entry["bounds"].split(":", 1)[1]]
    return entry["bounds"]


def main():
    spec = json.loads(SPEC.read_text())
    changes, notes = [], []

    md = fetch(DOCS)
    tables, tris, tex = parse_docs(md)
    if tris != spec["limits"]["max_triangles"]:
        changes.append(f"max_triangles: spec {spec['limits']['max_triangles']} → docs {tris}")
    if tex != spec["limits"]["max_texture_size"]:
        changes.append(f"max_texture_size: spec {spec['limits']['max_texture_size']} → docs {tex}")
    if not tables:
        changes.append("Couldn't find the bounds tables on the docs page; the page layout changed.")

    for scale, rows in tables.items():
        for label, cells in rows.items():
            targets = ROWS.get(label)
            if not targets:
                changes.append(f"[{scale}] new docs row we don't know: {label!r}")
                continue
            for type_name, att in targets:
                b = resolve(spec["accessory_types"][type_name]["attachments"], att)[scale]
                for axis, (value, offset, warn) in enumerate(cells):
                    if abs(b["size"][axis] - value) > TOL:
                        changes.append(f"[{scale}] {type_name}/{att} size[{'XYZ'[axis]}]: spec {b['size'][axis]} → docs {value}")
                    if warn:
                        notes.append(f"[{scale}] {type_name}/{att}: {warn} (spec keeps {b['offset'][axis]})")
                    elif offset is not None and abs(b["offset"][axis] - offset) > TOL:
                        changes.append(f"[{scale}] {type_name}/{att} offset[{'XYZ'[axis]}]: spec {b['offset'][axis]} → docs {round(offset, 4)}")

    hashes = {}
    if UGCV.exists():
        for rel in WATCHED:
            p = UGCV / rel
            hashes[rel] = hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None
        old = json.loads(HASHES.read_text()) if HASHES.exists() else {}
        for rel, h in hashes.items():
            if rel in old and old[rel] != h:
                changes.append(f"Studio validation file changed: {rel} (re-read it; our checks copy its logic)")
        if not HASHES.exists() or "--record-hashes" in sys.argv:
            HASHES.write_text(json.dumps(hashes, indent=2) + "\n")
            notes.append("Recorded Studio validation hashes.")
    else:
        notes.append("Roblox Studio not installed here; skipped the validation-code check.")

    if "--json" in sys.argv:
        print(json.dumps({"changes": changes, "notes": notes}, indent=2))
    else:
        print("Spec matches Roblox's docs and Studio." if not changes else "Changes found:")
        for c in changes:
            print("  - " + c)
        for n in notes:
            print("  note: " + n)
    sys.exit(1 if changes else 0)


if __name__ == "__main__":
    main()
