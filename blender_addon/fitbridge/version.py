# The add-on's version, in one place: read from blender_manifest.toml so it can't drift from what's installed.

import tomllib
from pathlib import Path

with open(Path(__file__).with_name("blender_manifest.toml"), "rb") as _f:
    VERSION = tomllib.load(_f)["version"]
