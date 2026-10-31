# FitBridge: brief

## The real problem
Fitting and framing a rigid UGC accessory in Studio takes rounds of import, fit, check, fix and re-export. FitBridge does the fit, the checks and the thumbnail once in Blender, where that's easy. Studio then rebuilds the accessory exactly, ready to press Upload.

## Goal and how we'll know it worked
- From finished model in Blender to an upload-ready Accessory in Studio in **under 2 minutes**, with **zero re-fits**.
- Test items: one hat, one waist item and one back item. Each passes Studio's **Save to Roblox** validation on the first try.

## Decisions (the user's)
- **Audience:** the user first, but clean enough to release publicly later.
- **Blender 4.2+**, shipped as a Blender extension.
- **Rigs:** an R15 mannequin with a body-scale picker (Classic, Normal, Slender). R6 is a preview dummy only.
- **Scope:** rigid accessories only. No layered clothing in v1.
- **Transfer:** a live local bridge. Blender serves on localhost and the Studio plugin pulls from it.
- **Specs:** one shared spec file in a public GitHub repo, plus a weekly checker that proposes edits for the user's approval. Live rules from Studio override the file whenever Studio is connected.
- **Studio:** builds the Accessory and runs the same checks as Roblox's validation. The user presses Save to Roblox.
- **Split:** the Blender Plugins Dev session builds the addon. The FitBridge session builds the shared contract, the Studio plugin and the spec checker.
- **Failing checks** warn live in Blender and block **Send to Studio**. An override exists for testing.
  - **Blocking checks:** bounds, triangles (4k), texture size (2048), single mesh, single material, UVs inside 0–1, power-of-two textures.
  - **Warnings only:** watertight / holes, and floating more than 0.2 studs from the body.
- **Textures:** detect PBR automatically. Colour only gives a TextureID; any extra map gives a SurfaceAppearance.
- **Thumbnail:** exact framing (same camera, FOV 70, zoom estimate) with approximate lighting.

## Research findings that shape the build
- Studio ships Roblox's own validation code. `UGCValidation/validateMeshBounds.lua` checks **every vertex** against a box in the Handle attachment's frame. It also needs the mesh box centred on origin and the Handle `Size` inside the bounds. The addon copies these checks exactly.
- `AvatarCreationService:GetValidationRules()` returns Roblox's **live** upload rules. Studio syncs them to Blender, so specs stay current without scraping docs.
- **Thumbnail storage:** `Accessory/ThumbnailConfiguration` holds `ThumbnailCameraTarget` (ObjectValue set to the Handle) and `ThumbnailCameraValue` (a CFrame relative to the Handle). No FOV is stored. The thumbnail tool uses 70, and Roblox re-frames slightly at render time, so the angle is exact and the zoom is an estimate.
- **Mesh upload:** `AssetService:CreateAssetAsync(EditableMesh, Enum.AssetType.Mesh)` lets the Studio plugin upload the mesh under the user's own account, with no API key. This still has to be confirmed in a real Studio.
- **Rig from Studio:** the plugin builds the exact R15 rig for each scale and sends it to Blender. No Roblox mesh files ship in the addon, which avoids licensing problems when it goes public.

## Out of scope / later
Layered clothing and cages, batch sending, Open Cloud upload, matching Roblox's thumbnail lighting exactly.

## Risks / to verify in real Studio
1. Whether `CreateAssetAsync` accepts `EditableImage` as `Enum.AssetType.Image`. If not, textures go through Studio's importer, or Open Cloud later.
2. EditableImage is capped at 1024², so a 2048 texture needs the fallback path.
3. Which body scale the live rules' single box per attachment matches. The first sync logs it.
4. UV V-flip and triangle winding. The protocol fixes both, and the first hat test confirms them.

## The Blender addon (for the Blender Plugins Dev session)
Read `docs/PROTOCOL.md` and `spec/ugc_spec.json` first. They are the contract.

**Panel (N-panel tab "FitBridge"):**
- **Rig:** Body scale dropdown (Classic, Normal, Slender) with **Spawn R15**; **Spawn R6 preview**.
  - Spawn R15 uses the rig synced from Studio. If none is cached yet, show "Open Studio with the FitBridge plugin to fetch the rig" and draw a box rig from the bundled R15 sizes.
- **Accessory:** Type dropdown (Hat, Hair, Face, Eyebrow, Eyelash, Neck, Shoulder, Front, Back, Waist), then an attachment dropdown filtered by type (for example Shoulder has Neck, Left/Right Collar and Left/Right Shoulder). Pick the mesh object (defaults to the active object).
- **Ghost bounds box:** a wireframe plus a translucent box at `A · CFrame.new(bounds.offset)` with `bounds.size`.
  - Green when every vertex is inside, red when any vertex is outside. It updates live while you move, rotate or scale, or edit in Edit Mode.
  - Use a depsgraph handler with throttling, plus a GPU draw handler; don't create scene objects for it.
  - Also show which axis fails and by how much ("Y: 0.14 studs over").
- **Checks list:** each check from `spec.checks` with a pass, warn or fail icon and a one-line message, updated live. Triangles count after triangulation; use the evaluated mesh, so modifiers count.
- **Floating check:** the closest distance from the mesh to the rig part's surface (`rig_part`, a BVH tree). Warn if it's over `checks.floating_gap`.
- **Thumbnail:** a **Thumbnail view** toggle sets up a camera with a 70° vertical FOV, a square 420×420 render, and hides the mannequin.
  - The camera starts at `default_camera` and the user orbits and pans it. Show a "Roblox may zoom slightly closer" hint.
- **Send to Studio:** enabled only when `sendable`. An "Override checks (testing)" checkbox sits next to it. Sending builds the package as described in PROTOCOL and bumps `sent_revision`.
- **Status line:** Bridge (listening / Studio connected), Rules (live <date> or bundled), and the last Studio result from `POST /v1/package/result`.

**Server:** a `http.server` thread on 127.0.0.1:47231, started when the addon is enabled and stopped on unregister. Never touch `bpy` from the server thread. Main-thread code snapshots the package into plain Python data, and the server only serves those snapshots.

**Scale:** 1 Blender unit = 1 stud. Warn if the scene unit scale isn't 1.0.
