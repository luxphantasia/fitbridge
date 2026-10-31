# FitBridge for Blender

The Blender half of FitBridge: fit a rigid Roblox UGC accessory on an R15 mannequin, watch the checks live,
frame the thumbnail, then press **Send to Studio**. The contract is `../docs/PROTOCOL.md` and `../spec/ugc_spec.json`.

- `fitbridge/` is the extension (Blender 4.2+). `fitbridge/ugc_spec.json` is a copy of `spec/ugc_spec.json`
  (an extension zip has to be self-contained). The tests fail if the two drift apart.
- Build: `blender --command extension build --source-dir fitbridge`
- Test: `tests/run.sh` runs everything in a throwaway background Blender on port 47298, never touching your profile.

| File | What it does |
|---|---|
| `geom.py` | Axis swap P, CFrames, the fit (vertices into the attachment frame, centred box), and Roblox's bounds test |
| `spec.py` | Bundled spec, live rules from Studio (they win), and the rig cache with a box stand-in until Studio sends one |
| `evaluate.py` | Live checks on the evaluated mesh, and the package build: manifest, mesh, RGBA textures, camera |
| `server.py` | The `http.server` thread on 127.0.0.1:47231. It only serves snapshots and queues POSTs; no bpy |
| `rig.py` | Spawns the R15 / R6 preview mannequin (one toggle, same place), shaded smooth like Studio |
| `thumb.py` | Thumbnail camera: one shot per accessory as an orbit (turn, tilt, distance, pan) around the attachment in the Handle's frame; side-aware defaults; viewport navigation moves it; presets, preview render |
| `draw.py` | Ghost bounds box (GPU overlay, green or red) and the "Top (Y): 0.14 studs over" readout |
| `upload.py` | Open Cloud upload (PROTOCOL v1.1): a tiny `.glb` writer from the wire arrays, then a multipart POST + operation poll on a worker thread, cached by mesh + texture hashes |

The Open Cloud key, creator type and creator id go in the addon preferences. The key is only ever sent to
apis.roblox.com in the `x-api-key` header: never over the bridge, in a manifest or in a log. The tests use a local
fake Open Cloud and never reach the real API.
