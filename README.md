# FitBridge

Fit a rigid Roblox UGC accessory in Blender, then get an upload-ready Accessory in Studio in one click.

```
Blender addon                                   Studio plugin
─────────────                                   ─────────────
spawn R15 (Classic / Normal / Slender)  ◄─ rig ─  builds the real R15 rig per scale
pick type + attachment                  ◄─ rules─ AvatarCreationService:GetValidationRules()
ghost bounds box, green / red live
live checks (tris, textures, UVs …)
thumbnail camera (FOV 70, square)
Send to Studio ── localhost:47231 ──────────────►  uploads mesh + textures (your account)
                                                  builds Accessory, Handle, attachment,
                                                  ThumbnailConfiguration, wears it on a test avatar,
                                                  re-runs Roblox's checks
                                    ◄─ result ──  YOU: right-click → Save to Roblox
```

## Layout

| Path | What it is |
|---|---|
| `docs/BRIEF.md` | Decisions, research findings, risks, and the Blender addon's requirements |
| `docs/PROTOCOL.md` | The bridge contract both plugins follow (coordinates, endpoints, JSON) |
| `docs/UPDATING_SPECS.md` | How spec changes are found and approved |
| `spec/ugc_spec.json` | Bounds, limits, check severities and thumbnail settings, shared by both plugins |
| `studio_plugin/` | Studio plugin source. `python3 studio_plugin/build.py --install` builds and installs it |
| `studio_plugin/tests/` | `luau studio_plugin/tests/geometry.test.lua` |
| `tools/mock_bridge.py` | A fake Blender server with a test halo, for testing Studio without Blender |
| `tools/spec_check.py` | Compares the spec with Roblox's docs and Studio's validation code |
| `blender_addon/` | The Blender extension (built by the Blender Plugins Dev session) |

## Try the Studio side now (no Blender needed)

1. `python3 studio_plugin/build.py --install`, then restart Studio and open any place.
2. `python3 tools/mock_bridge.py`
3. In Studio, go to Plugins → **FitBridge** and allow the HTTP request to 127.0.0.1 the first time.
4. `curl -X POST 127.0.0.1:47231/mock/send`. Studio uploads the halo, builds it on a Normal-scale test avatar in `Workspace.FitBridge`, and lists the checks.

Uploading a mesh or image from the plugin creates real (free) assets on the account set under **Upload as**.
