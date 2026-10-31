# FitBridge protocol v1

The Blender addon and the Studio plugin talk over plain HTTP on this computer. **Blender runs the server; Studio is the client.** Studio plugins can only make outgoing requests, so they cannot host a server.

- Address: `http://127.0.0.1:47231` (configurable on both sides).
- All bodies are JSON (`Content-Type: application/json`). Studio's HttpService only handles text, so binary data is base64.
- Every response includes `"protocol": 1`. If the other side reports a different number, show "update the other plugin" and stop.
- Both sides must ignore unknown fields, so either side can add fields without breaking the other.

## Coordinate rules (read this first)

Everything on the wire is in **Roblox space**:

| | Roblox (wire) | Blender |
|---|---|---|
| Up | +Y | +Z |
| Character forward | -Z | -Y |
| Character right | +X | -X |
| Units | 1 stud | 1 Blender unit (the addon keeps scene scale 1.0) |

Blender to Roblox for positions and directions is `r = P · b` with

```
P = [[-1, 0, 0],
     [ 0, 0, 1],
     [ 0, 1, 0]]       # P is a proper rotation (det +1) and its own inverse.
```

- **Object frames and cameras:** `R_roblox = P · R_blender` (apply P to each basis column and to the position). Blender cameras and Roblox cameras both look down local -Z with local +Y up, so no extra local correction is needed.
- **Triangle winding:** P is a rotation, so winding is preserved. Send triangles counter-clockwise when seen from the front, as Blender stores them.
- **UVs:** Blender's V origin is at the bottom, Roblox's is at the top. On the wire, `v = 1 - v_blender`.
- **CFrame encoding:** 12 numbers `[x, y, z, R00, R01, R02, R10, R11, R12, R20, R21, R22]`, the same order as `CFrame.new(...)` / `CFrame:GetComponents()`. This is row-major, so `R0x` is the first row.

## How a fitted accessory is described

The addon bakes the user's placement into the exported mesh. Studio never has to undo a rotation:

1. Take the body attachment's world frame `A` (from the synced rig, in Roblox space).
2. Transform every vertex into `A`'s local frame: `v_local = A⁻¹ · P · v_world_blender`.
3. Compute the axis-aligned box of `v_local`. `offset` = box centre and `size` = box size.
4. Send the vertices as `v_local - offset`, so the mesh is **centred on its own origin**. Roblox rejects meshes whose box centre is more than about 0.01 studs off origin.

Studio then builds:

- `Handle.Size = size`
- `Handle.<AttachmentName>.CFrame = CFrame.new(-offset)`

When the item is worn, the Handle's centre sits at `offset` in the body attachment's frame. That is exactly where the user put it in Blender.

The bounds check is the same one Roblox runs (from `UGCValidation/validateMeshBounds.lua`). Every `v_local` must lie inside the box of `bounds.size` centred at `bounds.offset`, and `size` must be no bigger than `bounds.size` on any axis.

The thumbnail camera is sent relative to the worn Handle: `camera_cframe = (A · CFrame.new(offset))⁻¹ · C`, where `C` is the Blender camera converted to Roblox space. Studio writes it straight into `ThumbnailCameraValue`.

## Endpoints

### `GET /v1/hello`
```json
{ "protocol": 1, "app": "FitBridge Blender", "app_version": "0.1.0", "blender_version": "4.2.3",
  "package_revision": 7, "sent_revision": 7, "needs": ["rules", "rig:Normal"] }
```
Studio polls this about once a second while its panel is open.
- When `sent_revision` goes up (the user pressed **Send to Studio**), Studio fetches the package and builds it automatically.
- `needs` lists what Blender wants from Studio. Studio answers with `POST /v1/rules` and `POST /v1/rig` for each `rig:<Scale>`. Blender asks for `rules` once per Studio session, and for a rig whenever the user spawns a scale it doesn't have cached yet.

### `GET /v1/package`
The current accessory: its manifest only, without the heavy data.
```json
{
  "protocol": 1,
  "revision": 7,
  "name": "Star Halo",
  "accessory_type": "Hat",
  "attachment": "HatAttachment",
  "body_scale": "Normal",
  "mesh": { "sha256": "…", "triangles": 3120, "vertices": 4011,
            "size": [1.6, 0.4, 1.6], "offset": [0, 0.35, 0] },
  "textures": {
    "color":     { "sha256": "…", "width": 1024, "height": 1024 },
    "normal":    { "sha256": "…", "width": 1024, "height": 1024 },
    "roughness": null,
    "metalness": null
  },
  "pbr": true,
  "thumbnail": { "camera_cframe": [0, 0.2, -6, 1, 0, 0, 0, 1, 0, 0, 0, 1], "field_of_view": 70 },
  "checks": [
    { "id": "bounds", "status": "pass", "message": "Inside HatAttachment box (Normal)" },
    { "id": "watertight", "status": "warn", "message": "12 open edges" }
  ],
  "sendable": true,
  "override": false,
  "spec_source": "live-2026-10-30T18:22:10Z"
}
```
- `checks[].status` is `pass`, `warn` or `fail`. `sendable` is false when any `block` check (severity from the spec) fails, unless `override` is true.
- `pbr` is true when the material has any map besides colour. Studio then builds a `SurfaceAppearance`. Otherwise it sets `MeshPart.TextureID`.
- A missing texture map is `null`.

### `GET /v1/package/mesh`
```json
{ "protocol": 1, "revision": 7, "sha256": "…",
  "positions": [x, y, z, …], "normals": [x, y, z, …], "uvs": [u, v, …],
  "triangles": [i0, i1, i2, …] }
```
Vertices are split per corner wherever the UV or normal differs, so each index addresses one position, normal and UV triple. Indices are 0-based. All data is in Roblox space, with the centred `v_local - offset` positions described above.

### `GET /v1/package/texture/{map}`
`{map}` is `color`, `normal`, `roughness` or `metalness`.
```json
{ "protocol": 1, "sha256": "…", "width": 1024, "height": 1024,
  "rgba_base64": "…", "png_path": "/abs/path/on/disk/color.png" }
```
- `rgba_base64` holds raw RGBA8 pixels, top row first, ready for `EditableImage:WritePixelsBuffer`.
- `png_path` is there for the manual fallback (Studio's importer) when a texture is bigger than `limits.editable_image_max_size`.

### `POST /v1/package/result` (Studio to Blender)
Studio reports what happened, so the Blender panel can show it.
```json
{ "protocol": 1, "revision": 7, "ok": true,
  "mesh_asset_id": 1234567890, "texture_asset_ids": { "color": 1234567891 },
  "studio_checks": [ { "id": "bounds", "status": "pass", "message": "…" } ],
  "message": "Built Star Halo. Right-click it in Explorer → Save to Roblox." }
```
Studio caches `sha256` → asset id, so an unchanged mesh or texture is never uploaded twice.

### `POST /v1/rules` (Studio to Blender)
Studio sends the output of `AvatarCreationService:GetValidationRules()`, converted to JSON. Vector3 values become `[x, y, z]` and enum keys become their names (`"HatAccessory"`). The addon saves it and prefers it over the bundled `spec/ugc_spec.json`. The panel shows "Rules: live from Studio, <date>" or "Rules: bundled snapshot".
```json
{ "protocol": 1, "fetched": "2026-10-30T18:22:10Z", "studio_version": "0.6xx",
  "limits": { "max_triangles": 4000, "max_texture_size": 2048 },
  "accessories": { "HatAccessory": { "rigid_allowed": true,
      "attachments": { "HatAttachment": { "size": [..], "offset": [..] } } } },
  "raw": { … }
}
```
Live rules give **one** box per attachment, not one per body scale. On the first sync the addon records which scale column in the bundled spec they match, and logs it so we learn which scale Roblox validates against.

### `POST /v1/rig` (Studio to Blender)
Studio builds a real R15 avatar for one body scale (`Players:CreateHumanoidModelFromDescription` with the spec's `BodyTypeScale` and `ProportionScale`) and sends it to Blender:
```json
{ "protocol": 1, "body_scale": "Normal",
  "parts": [ { "name": "Head", "size": [..], "cframe": [12 numbers],
               "mesh": { "positions": [..], "triangles": [..] } | null } ],
  "attachments": [ { "name": "HatAttachment", "part": "Head", "world_cframe": [12 numbers] } ] }
```
`mesh` is null when Studio can't read the part's mesh. Blender then draws that part as a box of `size`. The rig is posed exactly as Roblox would wear it, so the body attachments are exact. No Roblox mesh files ship inside the addon.

## Uploading through Open Cloud (v1.1)

Studio plugins can't upload assets yet (`CreateAssetAsync` reports "not available yet"), so **Blender uploads** with the user's Open Cloud API key. Lux types the key into the addon preferences themselves. It never travels over the bridge, never goes in a manifest or log, and never goes through chat.

**Blender preferences:** API key (Assets API read + write), creator type (`User` or `Group`) and creator id. The creator must be the one that will submit the UGC item, because Roblox checks that the mesh and textures belong to the uploader.

**On Send:**
1. Freeze the package snapshot (as before) and bump `sent_revision` straight away. Studio builds a preview from the raw mesh right away, so you get feedback in seconds.
2. If the mesh `sha256` plus every texture `sha256` is already in the upload cache, reuse that `model_asset_id`. Otherwise export a `.glb` from the **snapshot's wire data**: centred positions in Roblox space, normals, UVs with V flipped back for glTF, and the texture maps as a glTF material (PBR maps go in when `pbr` is true). Then `POST https://apis.roblox.com/assets/v1/assets` (multipart: `request` JSON with `assetType: "Model"`, `displayName`, `description`, `creationContext.creator.userId|groupId`, plus `fileContent` with the glb), and poll `GET /assets/v1/operations/{id}` until `done`.
   - Cache key: mesh sha plus texture shas, so moving or rotating the item never re-uploads it.
3. The manifest gains an `upload` block, and `/v1/hello` gains `upload_state` so Studio knows when to rebuild:

```json
"upload": { "state": "pending" | "done" | "failed" | "off",
            "model_asset_id": 1234567890,
            "creator": { "type": "Group", "id": 123 },
            "message": "Uploading to Roblox… (operation 1a2b)" }
```
`/v1/hello` gets `"upload_state"` (the same value) and `"upload_revision"` (the `sent_revision` that upload belongs to).
- `off` means no API key is set. Studio then only builds previews and says "Add your Open Cloud key in Blender preferences to make this uploadable."
- `failed` carries Roblox's error sentence in `message`.

**Studio, when `upload.state` becomes `done` for the current revision:**
1. Load the model (`game:GetObjects("rbxassetid://" .. model_asset_id)`, falling back to `InsertService:LoadAsset`) and take its MeshPart. That gives `MeshId` plus `TextureID` or a `SurfaceAppearance`, all owned by the creator.
2. **Orientation check:** Roblox's importer may turn a mesh 180° around Y. Studio reads the uploaded mesh (`AssetService:CreateEditableMeshAsync`), samples the manifest vertices and picks whichever of `R = identity` or `R = 180° about Y` matches. If R isn't identity, Studio folds it in:
   - `Attachment.CFrame = R · CFrame.new(-offset)`
   - `ThumbnailCameraValue = R · camera_cframe`
   - `Handle.Size = |R · size|`
   It reports which R it used in the result.
3. Rebuild the Accessory the same way as the preview build, with the uploaded MeshPart as the Handle, and post the result with `"uploaded": true`.

**GLB writing:** write the `.glb` straight from the wire arrays with a small glTF writer (positions as-is, `v_gltf = 1 - v_wire`, PNG images embedded), not with Blender's glTF exporter. That way no axis conversion happens behind our back, and the orientation check in Studio only has to catch Roblox's own importer behaviour.

## Errors
Any non-2xx response has `{ "protocol": 1, "error": "human readable sentence" }`. Show that sentence as-is.
