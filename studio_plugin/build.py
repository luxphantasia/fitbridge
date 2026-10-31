"""Bundles src/ (plus the shared spec) into one plugin file and optionally installs it into Studio.

  python3 studio_plugin/build.py            # writes studio_plugin/dist/FitBridge.lua
  python3 studio_plugin/build.py --install  # also copies it to ~/Documents/Roblox/Plugins
"""

import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "src"
SPEC = HERE.parent / "spec" / "ugc_spec.json"
DIST = HERE / "dist" / "FitBridge.lua"
PLUGINS = Path.home() / "Documents" / "Roblox" / "Plugins"


def build() -> Path:
    geometry = (SRC / "Geometry.lua").read_text().replace("--!strict\n", "", 1)
    main = (SRC / "Main.lua").read_text()
    spec = json.dumps(json.loads(SPEC.read_text()), separators=(",", ":"))
    for marker in ("--#GEOMETRY", "--#SPEC"):
        if marker not in main:
            raise SystemExit(f"Main.lua is missing the {marker} marker")
    if "]==]" in spec:
        raise SystemExit("spec contains ]==], which would break the Lua long string")
    main = main.replace("--#GEOMETRY", "local Geometry = (function()\n" + geometry + "end)()", 1)
    main = main.replace("--#SPEC", spec, 1)
    DIST.parent.mkdir(exist_ok=True)
    DIST.write_text(main)
    return DIST


if __name__ == "__main__":
    out = build()
    print(f"Built {out}")
    if "--install" in sys.argv:
        PLUGINS.mkdir(parents=True, exist_ok=True)
        shutil.copy(out, PLUGINS / out.name)
        print(f"Installed to {PLUGINS / out.name}  (restart Studio or reload plugins)")
