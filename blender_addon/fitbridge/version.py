# The add-on's version, in one place: read from blender_manifest.toml so it can't drift from what's installed.
# Inside a suite (the UGC Suite) the suite's build drops the tool's own manifest and lists every tool's version in
# suite_info.json two folders up, so it's read from there instead.

import json
import tomllib
from pathlib import Path

_here = Path(__file__).parent
try:
    with open(_here / "blender_manifest.toml", "rb") as _f:
        VERSION = tomllib.load(_f)["version"]
except FileNotFoundError:
    VERSION = "?"
    try:
        _info = json.loads((_here.parent.parent / "suite_info.json").read_text(encoding="utf-8"))
        VERSION = next(t["version"] for t in _info["tools"] if t["id"] == _here.name) or "?"
    except (OSError, ValueError, KeyError, StopIteration):
        pass
