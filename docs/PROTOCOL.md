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

## Errors
Any non-2xx response has `{ "protocol": 1, "error": "human readable sentence" }`. Show that sentence as-is.
