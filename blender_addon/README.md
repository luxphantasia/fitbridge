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
| `rig.py` | Spawns R15 / R6 preview, and the thumbnail camera (70° vertical FOV, 420 square) |
| `draw.py` | Ghost bounds box (GPU overlay, green or red) and the "Top (Y): 0.14 studs over" readout |
