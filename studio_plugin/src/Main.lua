-- FitBridge Studio plugin.
-- Pulls a fitted rigid accessory from the FitBridge Blender addon over localhost, uploads its mesh and
-- textures under your account, builds a Marketplace-ready Accessory (Handle, attachment, thumbnail
-- camera) on a test avatar, and re-runs Roblox's checks. You then right-click → Save to Roblox.
-- Also sends Blender Roblox's live validation rules and the exact R15 rig for each body scale.
-- Built by studio_plugin/build.py; edit src/ files. Protocol: docs/PROTOCOL.md.

local HttpService = game:GetService("HttpService")
local AssetService = game:GetService("AssetService")
local InsertService = game:GetService("InsertService")
local Players = game:GetService("Players")
local Selection = game:GetService("Selection")
local ChangeHistoryService = game:GetService("ChangeHistoryService")
local StudioService = game:GetService("StudioService")

--#GEOMETRY

local SPEC = HttpService:JSONDecode([==[--#SPEC]==])

local PROTOCOL = 1
local RIG_VERSION = 3 -- bump when what /v1/rig sends changes, so Blender re-asks
local BASE_URL = plugin:GetSetting("FitBridgeURL") or "http://127.0.0.1:47231"
local FOLDER_NAME = "FitBridge"
local ATTR = "FitBridge"
local FAMILY_ATTR = ATTR .. "Family" -- on a colour set's folder in Workspace
local SET_INDEX_ATTR = ATTR .. "SetIndex" -- on each dummy in that folder, for its place in the row

---------------------------------------------------------------- settings + cache

-- sha256 (from Blender) → asset id, so unchanged meshes/textures are never uploaded twice.
local uploadCache: { [string]: number } = plugin:GetSetting("FitBridgeUploads") or {}
local function rememberUpload(sha, id)
	uploadCache[sha] = id
	plugin:SetSetting("FitBridgeUploads", uploadCache)
end

local function creator()
	local kind = plugin:GetSetting("FitBridgeCreatorType") or "User"
	local id = tonumber(plugin:GetSetting("FitBridgeCreatorId") or "") or StudioService:GetUserId()
	return kind, id
end

---------------------------------------------------------------- HTTP

local function request(method, path, body)
	local ok, res = pcall(function()
		return HttpService:RequestAsync({
			Url = BASE_URL .. path,
			Method = method,
			Headers = { ["Content-Type"] = "application/json" },
			Body = body and HttpService:JSONEncode(body) or nil,
		})
	end)
	if not ok then
		return nil, "Can't reach Blender at " .. BASE_URL .. " (is the FitBridge addon enabled?)"
	end
	local decoded = nil
	pcall(function()
		decoded = HttpService:JSONDecode(res.Body)
	end)
	if not res.Success then
		return nil, (decoded and decoded.error) or ("Blender returned " .. res.StatusCode)
	end
	if decoded and decoded.protocol and decoded.protocol ~= PROTOCOL then
		return nil, ("Blender speaks FitBridge protocol %s, this plugin speaks %d. Update the older one."):format(
			tostring(decoded.protocol),
			PROTOCOL
		)
	end
	return decoded, nil
end

---------------------------------------------------------------- conversions

local function v3(t)
	return Vector3.new(t[1], t[2], t[3])
end

local function arr(v: Vector3)
	return { v.X, v.Y, v.Z }
end

local function cframeFrom(t)
	return CFrame.new(table.unpack(t, 1, 12))
end

local function cframeArr(cf: CFrame)
	return { cf:GetComponents() }
end

-- Turn GetValidationRules() output into plain JSON (Vector3 → array, enum keys → names).
local function toJson(value, depth)
	depth = depth or 0
	if depth > 12 then
		return nil
	end
	local t = typeof(value)
	if t == "Vector3" then
		return arr(value)
	elseif t == "Vector2" then
		return { value.X, value.Y }
	elseif t == "EnumItem" then
		return value.Name
	elseif t == "table" then
		local out = {}
		for k, v in value do
			local key = if typeof(k) == "EnumItem" then k.Name else k
			out[key] = toJson(v, depth + 1)
		end
		return out
	elseif t == "number" or t == "string" or t == "boolean" then
		return value
	end
	return tostring(value)
end

---------------------------------------------------------------- rules

local liveRules = nil
local function getLiveRules()
	if liveRules == nil then
		local ok, rules = pcall(function()
			return game:GetService("AvatarCreationService"):GetValidationRules()
		end)
		liveRules = if ok then rules else false
	end
	return liveRules or nil
end

-- Active bounds + limits for a package: live rules first, bundled spec otherwise.
local function activeRules(manifest)
	local typeSpec = SPEC.accessory_types[manifest.accessory_type]
	local rules = {
		maxTriangles = SPEC.limits.max_triangles,
		maxTextureSize = Geometry.textureLimit(manifest.pbr, SPEC.limits.texture_size_on_send),
		source = "bundled spec",
	}
	local size, offset = nil, nil
	if typeSpec then
		size, offset = Geometry.resolveBounds(typeSpec.attachments, manifest.attachment, manifest.body_scale)
	end
	local live = getLiveRules()
	if live and typeSpec then
		pcall(function()
			rules.maxTriangles = live.MeshRules.AccessoryMaxTriangles or rules.maxTriangles
			local assetLimits = live.TextureRules.AssetTextureLimits
			local assetLimit = assetLimits and assetLimits[Enum.AssetType[typeSpec.asset_type]]
			rules.maxTextureSize = Geometry.textureLimit(
				manifest.pbr,
				SPEC.limits.texture_size_on_send,
				live.TextureRules.MaxTextureSize,
				assetLimit and assetLimit.ColorMapSize
			)
			local info = live.AccessoryRules[Enum.AssetType[typeSpec.asset_type]]
			for _, a in info.Attachments do
				if a.Name == manifest.attachment then
					size = arr(a.Size)
					offset = if a.Offset then arr(a.Offset) else { 0, 0, 0 }
					rules.source = "live Roblox rules"
				end
			end
		end)
	end
	rules.size = size
	rules.offset = offset or { 0, 0, 0 }
	return rules
end

local function sendRules()
	local live = getLiveRules()
	if not live then
		return false, "Studio didn't return validation rules."
	end
	local accessories = {}
	for assetType, info in live.AccessoryRules or {} do
		local atts = {}
		for _, a in info.Attachments or {} do
			atts[a.Name] = { size = arr(a.Size), offset = if a.Offset then arr(a.Offset) else { 0, 0, 0 } }
		end
		accessories[assetType.Name] = { rigid_allowed = info.RigidAllowed, attachments = atts }
	end
	-- The smallest colour map limit of any accessory type, for SurfaceAppearance textures.
	local maxPbr = nil
	local assetLimits = live.TextureRules and live.TextureRules.AssetTextureLimits
	if assetLimits then
		for _, typeSpec in SPEC.accessory_types do
			local limit = assetLimits[Enum.AssetType[typeSpec.asset_type]]
			if limit and limit.ColorMapSize then
				maxPbr = math.min(maxPbr or limit.ColorMapSize, limit.ColorMapSize)
			end
		end
	end
	local _, err = request("POST", "/v1/rules", {
		protocol = PROTOCOL,
		fetched = DateTime.now():ToIsoDate(),
		studio_version = version(),
		limits = {
			max_triangles = live.MeshRules and live.MeshRules.AccessoryMaxTriangles,
			max_texture_size = live.TextureRules and live.TextureRules.MaxTextureSize,
			max_pbr_texture_size = maxPbr,
		},
		accessories = accessories,
		raw = toJson(live),
	})
	return err == nil, err
end

---------------------------------------------------------------- rig

local RTHRO_MANNEQUIN = "rbxasset://models/Thumbnails/Mannequins/Rthro.rbxm"

-- `scaleName` is a body scale from the spec, or "R6" for the classic preview rig.
local function makeRig(scaleName)
	local desc = Instance.new("HumanoidDescription")
	local rigType = Enum.HumanoidRigType.R15
	if scaleName == "R6" then
		rigType = Enum.HumanoidRigType.R6
	else
		local scale = SPEC.body_scales[scaleName] or SPEC.body_scales.Classic
		desc.BodyTypeScale = scale.humanoid_description.BodyTypeScale
		desc.ProportionScale = scale.humanoid_description.ProportionScale
	end
	local rig
	if rigType == Enum.HumanoidRigType.R15 and desc.BodyTypeScale > 0 then
		-- Rthro scales use Studio's own Rthro mannequin, as its thumbnailer does (MannequinUtility). Scaling
		-- the blocky body by BodyTypeScale gives a different, taller shape with different attachments.
		rig = game:GetObjects(RTHRO_MANNEQUIN)[1]
		local humanoid = rig:FindFirstChildOfClass("Humanoid")
		rig.PrimaryPart = rig:FindFirstChild("HumanoidRootPart")
		rig.Parent = workspace -- the Humanoid only rescales in the DataModel
		for name, value in { BodyTypeScale = desc.BodyTypeScale, BodyProportionScale = desc.ProportionScale } do
			local v = humanoid:FindFirstChild(name) or Instance.new("NumberValue")
			v.Name = name
			v.Value = value
			v.Parent = humanoid
		end
		task.wait(1)
	else
		rig = Players:CreateHumanoidModelFromDescription(desc, rigType)
	end
	rig.Name = "FitBridge_" .. scaleName
	rig:SetAttribute(ATTR .. "Scale", scaleName)
	for _, d in rig:GetDescendants() do
		if d:IsA("BasePart") then
			d.Anchored = true
		elseif d:IsA("Script") or d:IsA("LocalScript") then
			d:Destroy()
		end
	end
	return rig
end

local function readMesh(meshId: string, scale: Vector3)
	if meshId == "" then
		return nil
	end
	local ok, em = pcall(function()
		return AssetService:CreateEditableMeshAsync(Content.fromUri(meshId))
	end)
	if not ok or not em then
		return nil
	end
	local index, positions, triangles = {}, {}, {}
	for i, vid in em:GetVertices() do
		index[vid] = i - 1
		local p = em:GetPosition(vid) * scale
		table.insert(positions, p.X)
		table.insert(positions, p.Y)
		table.insert(positions, p.Z)
	end
	for _, fid in em:GetFaces() do
		for _, vid in em:GetFaceVertices(fid) do
			table.insert(triangles, index[vid])
		end
	end
	em:Destroy()
	return { positions = positions, triangles = triangles }
end

-- Read a mesh and stretch its box to exactly `size` (for stand-in meshes that aren't the part's own).
local function fittedMesh(meshId: string, size: Vector3)
	local mesh = readMesh(meshId, Vector3.one)
	if not mesh then
		return nil
	end
	local mn, mx = Geometry.minMax(mesh.positions)
	local k = { size.X / math.max(mx[1] - mn[1], 1e-6), size.Y / math.max(mx[2] - mn[2], 1e-6), size.Z / math.max(mx[3] - mn[3], 1e-6) }
	for i = 1, #mesh.positions, 3 do
		for a = 1, 3 do
			mesh.positions[i + a - 1] = (mesh.positions[i + a - 1] - (mn[a] + mx[a]) / 2) * k[a]
		end
	end
	return mesh
end

-- Classic R6 body meshes that ship with Studio (the default R6 parts are plain blocks of the same size).
local R6_MESHES = {
	Torso = "rbxasset://avatar/meshes/torso.mesh",
	["Left Arm"] = "rbxasset://avatar/meshes/leftarm.mesh",
	["Right Arm"] = "rbxasset://avatar/meshes/rightarm.mesh",
	["Left Leg"] = "rbxasset://avatar/meshes/leftleg.mesh",
	["Right Leg"] = "rbxasset://avatar/meshes/rightleg.mesh",
}
local CLASSIC_HEAD = "rbxasset://avatar/heads/head.mesh"

-- The default blocky R15 body's mesh ids by part name. Studio can read these, so they stand in for
-- body meshes it won't read (the Rthro mannequin's are Roblox-owned).
local blockyMeshIds: { [string]: string }? = nil
local function blockyMeshId(partName: string): string?
	if not blockyMeshIds then
		blockyMeshIds = {}
		local rig = Players:CreateHumanoidModelFromDescription(Instance.new("HumanoidDescription"), Enum.HumanoidRigType.R15)
		for _, d in rig:GetDescendants() do
			if d:IsA("MeshPart") then
				blockyMeshIds[d.Name] = d.MeshId
			end
		end
		rig:Destroy()
	end
	return blockyMeshIds[partName]
end

-- A body part's shape for Blender: its mesh, a stand-in mesh (`approximate = true`), or a shape hint.
-- Studio won't read Roblox-owned meshes ("no permission to load asset"): the default Dynamic Head and the
-- Rthro mannequin's parts. Heads then get the classic head mesh and body parts the blocky R15 mesh of the
-- same name, each fitted to the part's real size.
-- The part's size, cframe and attachments are always the real ones.
local function partShape(part: BasePart)
	if part:IsA("MeshPart") then
		local mesh = readMesh(part.MeshId, part.Size / part.MeshSize)
		if mesh then
			return mesh, nil, false
		end
		if part.Name == "Head" then
			local standIn = fittedMesh(CLASSIC_HEAD, part.Size)
			if standIn then
				return standIn, nil, true
			end
			return nil, { kind = "classic_head", scale = { 1, 1, 1 } }, true
		end
		local blocky = blockyMeshId(part.Name)
		return if blocky then fittedMesh(blocky, part.Size) else nil, nil, true
	end
	local special = part:FindFirstChildOfClass("SpecialMesh")
	if special then
		if special.MeshType == Enum.MeshType.FileMesh then
			return readMesh(special.MeshId, special.Scale), nil, false
		elseif special.MeshType == Enum.MeshType.Head then
			-- MeshType.Head is drawn as a 1-stud rounded head (the part's smallest side) times Scale.
			local side = math.min(part.Size.X, part.Size.Y, part.Size.Z)
			local mesh = fittedMesh(CLASSIC_HEAD, special.Scale * side)
			if mesh then
				return mesh, nil, false
			end
			return nil, { kind = "classic_head", scale = arr(special.Scale) }, false
		end
	end
	if R6_MESHES[part.Name] then
		local mesh = fittedMesh(R6_MESHES[part.Name], part.Size)
		if mesh then
			return mesh, nil, false
		end
	end
	return nil, nil, false
end

local function sendRig(scaleName)
	local ok, rig = pcall(makeRig, scaleName)
	if not ok then
		return false, "Couldn't build the " .. scaleName .. " rig: " .. tostring(rig)
	end
	rig.Parent = workspace -- joints only resolve in the DataModel
	-- Stand it on the ground (feet at y = 0) facing -Z, like the R6 preview, instead of centring the root on the origin.
	rig:PivotTo(CFrame.new(0, 0, 0))
	local lowest = math.huge
	for _, d in rig:GetDescendants() do
		if d:IsA("BasePart") and d.Name ~= "HumanoidRootPart" then
			local cf, size = d.CFrame, d.Size
			local halfY = math.abs(cf.RightVector.Y) * size.X / 2
				+ math.abs(cf.UpVector.Y) * size.Y / 2
				+ math.abs(cf.LookVector.Y) * size.Z / 2
			lowest = math.min(lowest, cf.Position.Y - halfY)
		end
	end
	rig:PivotTo(CFrame.new(0, -lowest, 0))
	local parts, attachments = {}, {}
	for _, d in rig:GetDescendants() do
		if d:IsA("BasePart") and d.Name ~= "HumanoidRootPart" then
			local mesh, shape, approximate = partShape(d)
			table.insert(parts, {
				name = d.Name,
				size = arr(d.Size),
				cframe = cframeArr(d.CFrame),
				mesh = mesh,
				shape = shape,
				approximate = approximate or nil,
			})
		elseif d:IsA("Attachment") and not string.find(d.Name, "RigAttachment") then
			table.insert(attachments, { name = d.Name, part = d.Parent.Name, world_cframe = cframeArr(d.WorldCFrame) })
		end
	end
	rig:Destroy()
	local _, err = request("POST", "/v1/rig", {
		protocol = PROTOCOL,
		rig_version = RIG_VERSION,
		rig_type = if scaleName == "R6" then "R6" else "R15",
		body_scale = scaleName,
		parts = parts,
		attachments = attachments,
	})
	return err == nil, err
end

---------------------------------------------------------------- uploads

local function uploadParams(name)
	local kind, id = creator()
	return {
		Name = name,
		Description = "Made with FitBridge",
		CreatorId = id,
		CreatorType = Enum.AssetCreatorType[kind],
	}
end

local function upload(object, assetType, name)
	local ok, result, assetId = pcall(function()
		return AssetService:CreateAssetAsync(object, assetType, uploadParams(name))
	end)
	if not ok then
		return nil, tostring(result)
	end
	if result ~= Enum.CreateAssetResult.Success then
		return nil, "Upload result: " .. tostring(result)
	end
	return assetId, nil
end

local function buildEditableMesh(mesh)
	local em = AssetService:CreateEditableMesh()
	local P, N, U = mesh.positions, mesh.normals, mesh.uvs
	local vids, nids, uids = {}, {}, {}
	local count = #P // 3
	for i = 0, count - 1 do
		vids[i] = em:AddVertex(Vector3.new(P[i * 3 + 1], P[i * 3 + 2], P[i * 3 + 3]))
		if N and #N >= (i + 1) * 3 then
			nids[i] = em:AddNormal(Vector3.new(N[i * 3 + 1], N[i * 3 + 2], N[i * 3 + 3]))
		end
		if U and #U >= (i + 1) * 2 then
			uids[i] = em:AddUV(Vector2.new(U[i * 2 + 1], U[i * 2 + 2]))
		end
	end
	local T = mesh.triangles
	for t = 1, #T, 3 do
		local a, b, c = T[t], T[t + 1], T[t + 2]
		local fid = em:AddTriangle(vids[a], vids[b], vids[c])
		if nids[a] then
			em:SetFaceNormals(fid, { nids[a], nids[b], nids[c] })
		end
		if uids[a] then
			em:SetFaceUVs(fid, { uids[a], uids[b], uids[c] })
		end
	end
	return em
end

local function meshAssetId(manifest, mesh)
	local cached = uploadCache[manifest.mesh.sha256]
	if cached then
		return cached, nil
	end
	local em = buildEditableMesh(mesh)
	local id, err = upload(em, Enum.AssetType.Mesh, manifest.name .. " mesh")
	if id then
		em:Destroy()
		rememberUpload(manifest.mesh.sha256, id)
		return id, nil
	end
	-- Hand back the in-memory mesh so the accessory can still be previewed in place.
	return nil, err, em
end

-- Returns asset id, or nil + a message (with a manual fallback when the texture is too big).
local function textureAssetId(manifest, map, info)
	local cached = uploadCache[info.sha256]
	if cached then
		return cached, nil
	end
	local tex, err = request("GET", "/v1/package/texture/" .. map)
	if not tex then
		return nil, err
	end
	local okImg, img = pcall(function()
		return AssetService:CreateEditableImage({ Size = Vector2.new(tex.width, tex.height) })
	end)
	if not okImg or not img then
		return nil,
			("Couldn't create a %d×%d image for %s, so it won't show: %s. Import %s with the 3D Importer and set it by hand."):format(
				tex.width,
				tex.height,
				map,
				tostring(img),
				tostring(tex.png_path)
			)
	end
	img:WritePixelsBuffer(Vector2.zero, img.Size, Geometry.base64ToBuffer(tex.rgba_base64))
	local id, upErr = upload(img, Enum.AssetType.Image, manifest.name .. " " .. map)
	if id then
		img:Destroy()
		rememberUpload(info.sha256, id)
		return id, nil
	end
	return nil, upErr, img
end

---------------------------------------------------------------- building

local CAMERA_AIM_SIN = math.sin(math.rad(20))
local CAMERA_REACH = 100
local ROW_GAP = 2

local function folder()
	local f = workspace:FindFirstChild(FOLDER_NAME)
	if not f then
		f = Instance.new("Folder")
		f.Name = FOLDER_NAME
		f.Parent = workspace
	end
	return f
end

-- The single-build folder and every colour set's folder.
local function ownFolders()
	local list = {}
	for _, c in workspace:GetChildren() do
		if c:IsA("Folder") and (c.Name == FOLDER_NAME or c:GetAttribute(FAMILY_ATTR)) then
			table.insert(list, c)
		end
	end
	return list
end

local function groundRayParams()
	local params = RaycastParams.new()
	params.FilterType = Enum.RaycastFilterType.Exclude
	params.FilterDescendantsInstances = ownFolders()
	params.RespectCanCollide = true
	return params
end

-- Where to stand something this big so it's on the ground and on screen. Excludes our own folders from every ray.
local function groundInFrontOfCamera(height: number, width: number?): (Vector3, Vector3)
	local cam = workspace.CurrentCamera
	local origin, look = cam.CFrame.Position, cam.CFrame.LookVector
	local flat = Vector3.new(look.X, 0, look.Z)
	if flat.Magnitude < 0.01 then
		flat = Vector3.new(cam.CFrame.UpVector.X, 0, cam.CFrame.UpVector.Z) -- looking straight down
	end
	local forward = flat.Unit
	local params = groundRayParams()

	-- Aim at the middle of the view, or a little below the horizon when looking level or up.
	local aim = look
	if aim.Y > -CAMERA_AIM_SIN then
		aim = forward * math.sqrt(1 - CAMERA_AIM_SIN ^ 2) - Vector3.yAxis * CAMERA_AIM_SIN
	end
	local viewport = cam.ViewportSize
	local aspect = if viewport.X > 0 and viewport.Y > 0 then viewport.X / viewport.Y else 16 / 9
	local halfView = math.max(height, (width or 0) / aspect) / 2
	local near = halfView / math.tan(math.rad(cam.FieldOfView / 2)) * 1.5 -- far enough to see all of it
	local hit = workspace:Raycast(origin, aim * CAMERA_REACH, params)
	if not hit then
		return origin + forward * near - Vector3.yAxis * (height / 2), forward -- no ground in reach: stand at eye level
	end
	local distance = (hit.Position - origin):Dot(forward)
	distance = if hit.Normal.Y > 0.5 then math.max(distance, near) else math.max(distance - 2, 2) -- step back from walls
	local spot = origin + forward * distance
	local floor = workspace:Raycast(spot, -Vector3.yAxis * (CAMERA_REACH * 2), params)
	return Vector3.new(spot.X, if floor then floor.Position.Y else hit.Position.Y, spot.Z), forward
end

-- Stands the rig at `feet`, looking back along `forward`.
local function standAt(rig: Model, feet: Vector3, forward: Vector3)
	rig:PivotTo(CFrame.lookAt(feet, feet - forward))
	local box, size = rig:GetBoundingBox()
	rig:PivotTo(rig:GetPivot() + Vector3.yAxis * (feet.Y - (box.Position.Y - size.Y / 2)))
end

-- Stands the rig facing the camera. Accessories already on it go with it.
local function standInFrontOfCamera(rig: Model)
	local before = rig:GetPivot()
	local feet, forward = groundInFrontOfCamera(rig:GetExtentsSize().Y)
	standAt(rig, feet, forward)
	local moved = rig:GetPivot() * before:Inverse()
	for _, acc in folder():GetChildren() do
		local handle = acc:IsA("Accessory") and acc:FindFirstChild("Handle") :: BasePart?
		if handle and (handle.Position - before.Position).Magnitude < 10 then
			handle.CFrame = moved * handle.CFrame
		end
	end
end

local WEAR_SCRIPT_NAME = "FitBridgeWear"
local WEAR_SCRIPT = [[
-- Added by FitBridge. In Play, touch this dummy to wear what it's wearing.
local rig = script.Parent
local from = rig.Parent:GetFullName()

-- Handles have to stay unanchored for Roblox, so in Play they'd fall. Hold ours in place.
local worn = {}
local wornNames = {}
for _, accessory in rig.Parent:GetChildren() do
	local handle = accessory:IsA("Accessory") and accessory:FindFirstChild("Handle")
	local attachment = handle and handle:FindFirstChildOfClass("Attachment")
	local seat = attachment and rig:FindFirstChild(attachment.Name, true)
	if seat and (seat.WorldPosition - attachment.WorldPosition).Magnitude < 0.1 then
		handle.Anchored = true
		table.insert(worn, accessory)
		wornNames[accessory.Name] = true
	end
end

local function onTouched(hit)
	local character = hit:FindFirstAncestorOfClass("Model")
	local humanoid = character and character ~= rig and character:FindFirstChildOfClass("Humanoid")
	if not humanoid then
		return
	end
	-- In a row of colours, trying another one swaps it instead of stacking them on one head.
	for _, other in character:GetChildren() do
		if other:GetAttribute("FitBridgeFrom") == from and not wornNames[other.Name] then
			other:Destroy()
		end
	end
	for _, accessory in worn do
		if not character:FindFirstChild(accessory.Name) then
			local copy = accessory:Clone()
			copy.Handle.Anchored = false
			copy:SetAttribute("FitBridgeFrom", from)
			humanoid:AddAccessory(copy)
		end
	end
end

for _, part in rig:GetDescendants() do
	if part:IsA("BasePart") then
		part.Touched:Connect(onTouched)
	end
end
]]

-- Returns a message if Studio won't let the plugin write the script.
local function addWearScript(rig: Model): string?
	local script = rig:FindFirstChild(WEAR_SCRIPT_NAME) :: Script? or Instance.new("Script")
	local ok, err = pcall(function()
		if script.Source ~= WEAR_SCRIPT then
			script.Source = WEAR_SCRIPT
		end
	end)
	if not ok then
		return "Touching the dummy won't equip its accessories: " .. tostring(err)
	end
	script.Name = WEAR_SCRIPT_NAME
	script.Parent = rig
	return nil
end

local function dummyFor(scaleName, moveToCamera)
	local rig = nil
	for _, m in folder():GetChildren() do
		if m:GetAttribute(ATTR .. "Scale") == scaleName then
			rig = m
			break
		end
	end
	local isNew = rig == nil
	if isNew then
		rig = makeRig(scaleName)
		rig.Parent = folder()
	end
	if isNew or moveToCamera then
		standInFrontOfCamera(rig)
	end
	return rig
end

-- `maps` values are asset ids (uploaded) or EditableImages (preview only, not uploadable).
local function setTexture(handle: MeshPart, pbr, maps)
	local function content(v)
		return if type(v) == "number" then Content.fromUri("rbxassetid://" .. v) else Content.fromObject(v)
	end
	if pbr then
		local sa = Instance.new("SurfaceAppearance")
		local props = { color = "ColorMap", normal = "NormalMap", roughness = "RoughnessMap", metalness = "MetalnessMap" }
		for map, prop in props do
			if maps[map] then
				local ok = pcall(function()
					(sa :: any)[prop .. "Content"] = content(maps[map])
				end)
				if not ok and type(maps[map]) == "number" then
					(sa :: any)[prop] = "rbxassetid://" .. maps[map]
				end
			end
		end
		sa.Parent = handle
	elseif maps.color then
		local ok = pcall(function()
			(handle :: any).TextureContent = content(maps.color)
		end)
		if not ok and type(maps.color) == "number" then
			handle.TextureID = "rbxassetid://" .. maps.color
		end
	end
end

local function instanceTreeChecks(accessory: Accessory)
	local checks = {}
	local allowedTop = {}
	for _, n in SPEC.marketplace_properties.allowed_accessory_children do
		allowedTop[n] = true
	end
	local allowedHandle = {}
	for _, n in SPEC.marketplace_properties.allowed_handle_children do
		allowedHandle[n] = true
	end
	local extras = {}
	for _, c in accessory:GetChildren() do
		if not allowedTop[c.Name] then
			table.insert(extras, c.Name)
		end
	end
	local handle = accessory:FindFirstChild("Handle")
	if handle then
		for _, c in handle:GetChildren() do
			if not allowedHandle[c.ClassName] then
				table.insert(extras, "Handle." .. c.Name)
			end
		end
	end
	table.insert(checks, {
		id = "instance_tree",
		status = if #extras == 0 then "pass" else "fail",
		message = if #extras == 0 then "Only Handle, attachment and thumbnail config" else "Extra objects: " .. table.concat(extras, ", "),
	})
	local cfg = accessory:FindFirstChild("ThumbnailConfiguration")
	local target = cfg and cfg:FindFirstChild("ThumbnailCameraTarget")
	table.insert(checks, {
		id = "thumbnail_target",
		status = if target and target.Value == handle then "pass" else "fail",
		message = "Thumbnail camera targets the Handle",
	})
	return checks
end

local lastBuilt: Accessory? = nil

-- The MeshPart inside the model Blender uploaded through Open Cloud (mesh + textures, owned by the creator).
local function loadUploadedPart(assetId)
	local ok, objects = pcall(function()
		return game:GetObjects("rbxassetid://" .. assetId)
	end)
	if not ok or not objects or #objects == 0 then
		local ok2, model = pcall(function()
			return InsertService:LoadAsset(tonumber(assetId))
		end)
		if not ok2 then
			return nil, tostring(model)
		end
		objects = { model }
	end
	local best: MeshPart? = nil
	for _, obj in objects do
		local list = obj:GetDescendants()
		table.insert(list, obj)
		for _, d in list do
			if d:IsA("MeshPart") and (not best or d.Size.Magnitude > best.Size.Magnitude) then
				best = d
			end
		end
	end
	if not best then
		return nil, "the uploaded model has no MeshPart"
	end
	local part = best:Clone()
	for _, child in part:GetChildren() do
		if not child:IsA("SurfaceAppearance") then
			child:Destroy()
		end
	end
	for name in part:GetAttributes() do
		part:SetAttribute(name, nil)
	end
	return part, nil
end

-- 0 or 180: how far Roblox's importer turned the mesh about Y (see PROTOCOL.md, Uploading).
local function detectTurn(part: MeshPart, mesh, size: Vector3)
	local ok, em = pcall(function()
		return AssetService:CreateEditableMeshAsync(Content.fromUri(part.MeshId))
	end)
	if not ok or not em then
		return 0, "couldn't read the uploaded mesh to check its orientation; assumed unturned"
	end
	local scale = size / part.MeshSize
	local actual = {}
	for _, vid in em:GetVertices() do
		local p = em:GetPosition(vid) * scale
		table.insert(actual, p.X)
		table.insert(actual, p.Y)
		table.insert(actual, p.Z)
	end
	em:Destroy()
	local deg = Geometry.bestYRotation(mesh.positions, actual)
	return deg, nil
end

local function isRefused(loadErr)
	return string.find(tostring(loadErr), "not authorized", 1, true) ~= nil
end

local UPLOAD_NOTES = {
	off = "Preview only: add your Open Cloud API key in Blender's FitBridge preferences to make it uploadable.",
	pending = "Preview only: Blender is still uploading to Roblox. Studio rebuilds when it's done.",
}

-- The Marketplace-ready Accessory around a loaded Handle. `turn` undoes any turn from Roblox's importer.
local function assembleAccessory(manifest, handle: MeshPart, turn: CFrame)
	handle.Name = "Handle"
	handle.Size = v3(manifest.mesh.size)
	handle.Material = Enum.Material.Plastic
	handle.Transparency = 0
	handle.Color = Color3.fromRGB(table.unpack(SPEC.marketplace_properties.Color)) -- the texture shows over it
	handle.CanCollide = false
	handle.CanTouch = false
	handle.CanQuery = false
	handle.Massless = SPEC.marketplace_properties.Massless
	handle.Anchored = false

	local attachment = Instance.new("Attachment")
	attachment.Name = manifest.attachment
	attachment.CFrame = turn * CFrame.new(-v3(manifest.mesh.offset))
	attachment.Parent = handle

	local accessory = Instance.new("Accessory")
	accessory.Name = manifest.name
	local typeSpec = SPEC.accessory_types[manifest.accessory_type]
	pcall(function()
		accessory.AccessoryType = Enum.AccessoryType[typeSpec.accessory_type]
	end)
	handle.Parent = accessory

	local cfg = Instance.new("Configuration")
	cfg.Name = "ThumbnailConfiguration"
	local target = Instance.new("ObjectValue")
	target.Name = "ThumbnailCameraTarget"
	target.Value = handle
	target.Parent = cfg
	local camValue = Instance.new("CFrameValue")
	camValue.Name = "ThumbnailCameraValue"
	camValue.Value = turn * cframeFrom(manifest.thumbnail.camera_cframe)
	camValue.Parent = cfg
	cfg.Parent = accessory
	return accessory, attachment
end

-- Puts the Handle on the rig's matching attachment. Returns a message if the rig has none.
local function seatOn(rig: Model, handle: MeshPart, attachment: Attachment): string?
	local bodyAttachment = nil
	for _, d in rig:GetDescendants() do
		if d:IsA("Attachment") and d.Name == attachment.Name then
			bodyAttachment = d
			break
		end
	end
	if bodyAttachment then
		handle.CFrame = bodyAttachment.WorldCFrame * attachment.CFrame:Inverse()
		return nil
	end
	handle.CFrame = rig:GetPivot() * CFrame.new(0, 6, 0)
	return "Test avatar has no " .. attachment.Name .. "; placed above it."
end

local function build(manifest, moveToCamera)
	local mesh, err = request("GET", "/v1/package/mesh")
	if not mesh then
		return nil, { err }
	end
	local rules = activeRules(manifest)
	if not rules.size then
		return nil, { "No bounds known for " .. manifest.accessory_type .. " / " .. manifest.attachment }
	end
	local checks = Geometry.studioChecks(manifest, mesh, rules)
	local problems = {}

	local handle = nil
	local uploaded = false
	local uploadNote = nil
	local turn = CFrame.new()
	local upload = manifest.upload
	local meshId, textureIds = nil, {}
	if upload and upload.state == "done" and upload.model_asset_id then
		local part, loadErr = loadUploadedPart(upload.model_asset_id)
		if part then
			local deg, turnErr = detectTurn(part, mesh, v3(manifest.mesh.size))
			if deg == 180 then
				turn = CFrame.Angles(0, math.pi, 0)
			end
			if turnErr then
				table.insert(problems, turnErr)
			end
			if part.TextureID == "" and not part:FindFirstChildOfClass("SurfaceAppearance") and manifest.textures.color then
				table.insert(problems, "The uploaded model came without its texture.")
			end
			handle = part
			uploaded = true
			meshId = upload.model_asset_id
			uploadNote = ("Uploaded as model %s%s"):format(
				tostring(upload.model_asset_id),
				if deg == 180 then " (Roblox's importer turned it 180°; compensated)" else ""
			)
		else
			uploadNote = if isRefused(loadErr)
				then ("This place isn't allowed to load model %s, so this is only a preview. To submit it, build it in a new place (File > New)."):format(
					tostring(upload.model_asset_id)
				)
				else "Couldn't load uploaded model " .. tostring(upload.model_asset_id) .. ": " .. tostring(loadErr)
		end
	elseif upload and upload.state == "failed" then
		uploadNote = "Upload failed: " .. tostring(upload.message)
	elseif upload then
		uploadNote = UPLOAD_NOTES[upload.state]
	end

	if not handle then
		-- Preview: try the plugin upload API (not available yet), else show the in-memory mesh.
		local pluginMeshId, meshErr, previewMesh = meshAssetId(manifest, mesh)
		local pluginUploaded = pluginMeshId ~= nil
		local textureMaps = {}
		for map, info in manifest.textures or {} do
			if info then
				local id, texErr, previewImage = textureAssetId(manifest, map, info)
				if id then
					textureIds[map] = id
					textureMaps[map] = id
				else
					pluginUploaded = false
					textureMaps[map] = previewImage
					if not uploadNote or not previewImage then
						table.insert(problems, texErr)
					end
				end
			end
		end
		local okPart, part = pcall(function()
			if pluginMeshId then
				return AssetService:CreateMeshPartAsync(Content.fromUri("rbxassetid://" .. pluginMeshId))
			end
			return AssetService:CreateMeshPartAsync(Content.fromObject(previewMesh))
		end)
		if not okPart then
			return nil, { "Couldn't create the MeshPart: " .. tostring(part) }, checks
		end
		handle = part
		setTexture(handle, manifest.pbr, textureMaps)
		uploaded = pluginUploaded
		meshId = pluginMeshId
		uploadNote = uploadNote
			or (if pluginUploaded
				then "Mesh and textures uploaded from Studio"
				else "Preview only: Studio can't upload (" .. tostring(meshErr) .. ")")
	end
	table.insert(checks, { id = "uploaded", status = if uploaded then "pass" else "fail", message = uploadNote })

	local recording = ChangeHistoryService:TryBeginRecording("FitBridge build " .. manifest.name)

	local accessory, attachment = assembleAccessory(manifest, handle, turn)

	-- Replace any earlier build of this item, then seat it on the matching test avatar's attachment.
	-- Not Humanoid:AddAccessory: wearing adds an AccessoryRigidConstraint that validation doesn't strip.
	-- No attributes on the Accessory either: Roblox's AttributesAllowed check rejects them.
	local rig = dummyFor(manifest.body_scale, moveToCamera)
	local wearErr = addWearScript(rig)
	if wearErr then
		table.insert(problems, wearErr)
	end
	for _, old in folder():GetChildren() do
		if old:IsA("Accessory") and old.Name == manifest.name then
			old:Destroy()
		end
	end
	local seatErr = seatOn(rig, handle, attachment)
	if seatErr then
		table.insert(problems, seatErr)
	end
	accessory.Parent = folder()

	for _, c in instanceTreeChecks(accessory) do
		table.insert(checks, c)
	end
	if recording then
		ChangeHistoryService:FinishRecording(recording, Enum.FinishRecordingOperation.Commit)
	end

	lastBuilt = accessory
	Selection:Set({ accessory })
	return {
		accessory = accessory,
		meshId = meshId,
		textureIds = textureIds,
		rulesSource = rules.source,
	},
		problems,
		checks
end

---------------------------------------------------------------- colour sets

local function setFolderFor(family)
	for _, f in ownFolders() do
		if f:GetAttribute(FAMILY_ATTR) == family then
			return f
		end
	end
	return nil
end

-- Feet for a row facing the camera, first on the left, behind any dummies already standing there.
local function rowInFrontOfCamera(count: number, spacing: number, height: number, replacing: Folder?)
	local centre, forward = groundInFrontOfCamera(height, count * spacing)
	local right = forward:Cross(Vector3.yAxis)
	local taken = {}
	for _, f in ownFolders() do
		if f ~= replacing then
			for _, m in f:GetChildren() do
				if m:IsA("Model") then
					table.insert(taken, m:GetPivot().Position * Vector3.new(1, 0, 1))
				end
			end
		end
	end
	local function isClear(spot: Vector3)
		for _, p in taken do
			if (spot * Vector3.new(1, 0, 1) - p).Magnitude < spacing then
				return false
			end
		end
		return true
	end
	local row = 0
	local spots = {}
	repeat
		local clear = true
		for i = 1, count do
			spots[i] = centre + right * ((i - (count + 1) / 2) * spacing) + forward * (row * spacing)
			clear = clear and isClear(spots[i])
		end
		row += 1
	until clear or row == 10
	local params = groundRayParams()
	local top = workspace.CurrentCamera.CFrame.Position.Y
	for i, spot in spots do
		local floor = workspace:Raycast(Vector3.new(spot.X, top, spot.Z), -Vector3.yAxis * (CAMERA_REACH * 2), params)
		spots[i] = Vector3.new(spot.X, if floor then floor.Position.Y else centre.Y, spot.Z)
	end
	return spots, forward
end

-- Where the old set's dummies stood, in row order, so a resend lands in the same place.
local function oldRowPivots(old: Folder)
	local rigs = {}
	for _, m in old:GetChildren() do
		if m:IsA("Model") and m:GetAttribute(SET_INDEX_ATTR) then
			table.insert(rigs, m)
		end
	end
	table.sort(rigs, function(a, b)
		return a:GetAttribute(SET_INDEX_ATTR) < b:GetAttribute(SET_INDEX_ATTR)
	end)
	local pivots = {}
	for i, rig in rigs do
		pivots[i] = rig:GetPivot()
	end
	return pivots
end

-- Every colour or none: all the models load before the place changes, so a refused one leaves it as it was.
-- Returns what was built, or nil + a message.
local function buildSet(set, moveToCamera)
	local first = set.items[1]
	if not first then
		return nil, "The set has no colours."
	end
	if not activeRules(first).size then
		return nil, "No bounds known for " .. first.accessory_type .. " / " .. first.attachment
	end
	local mesh, meshErr = request("GET", "/v1/set/mesh")
	if not mesh then
		return nil, meshErr
	end

	local parts = {}
	local notes = {}
	for i, item in set.items do
		local id = item.upload.model_asset_id
		local part, loadErr = loadUploadedPart(id)
		if not part then
			return nil,
				if isRefused(loadErr)
					then ("This place isn't allowed to load model %s, so nothing was built. In a new place (File > New), press Build colours."):format(
						tostring(id)
					)
					else "Couldn't load uploaded model " .. tostring(id) .. ", so nothing was built: " .. tostring(loadErr)
		end
		-- Per colour, not once: each is its own upload, and a cached one can predate an addon update.
		local deg, turnErr = detectTurn(part, mesh, v3(item.mesh.size))
		if turnErr then
			table.insert(notes, item.name .. ": " .. turnErr)
		end
		parts[i] = { part = part, turn = if deg == 180 then CFrame.Angles(0, math.pi, 0) else CFrame.new() }
	end

	local newFolder = Instance.new("Folder")
	newFolder.Name = set.name
	newFolder:SetAttribute(FAMILY_ATTR, set.family)

	-- One rig, cloned along the row, so the clones carry its wear script.
	local template = makeRig(first.body_scale)
	template.Parent = newFolder
	local wearErr = addWearScript(template)
	if wearErr then
		table.insert(notes, wearErr)
	end
	local rigSize = template:GetExtentsSize()
	local meshSize = v3(first.mesh.size)
	local spacing = math.max(rigSize.X, rigSize.Z, meshSize.X, meshSize.Z) + ROW_GAP

	local old = setFolderFor(set.family)
	local oldPivots = if old and not moveToCamera then oldRowPivots(old) else {}
	local count = #set.items
	local spots, forward
	if #oldPivots == 0 then
		spots, forward = rowInFrontOfCamera(count, spacing, rigSize.Y, old)
	end

	local lines = {}
	local failures = {}
	local firstAccessory = nil
	for i, item in set.items do
		local rig = if i == 1 then template else template:Clone()
		rig.Name = (item.colour or "Original") .. " dummy"
		rig:SetAttribute(SET_INDEX_ATTR, i)
		rig.Parent = newFolder
		if spots then
			standAt(rig, spots[i], forward)
		else
			-- Extra colours carry on along the row: a rig's local -X is the camera's right.
			local last = oldPivots[#oldPivots]
			rig:PivotTo(oldPivots[i] or last * CFrame.new(-(i - #oldPivots) * spacing, 0, 0))
		end

		local handle = parts[i].part
		local accessory, attachment = assembleAccessory(item, handle, parts[i].turn)
		local seatErr = seatOn(rig, handle, attachment)
		accessory.Parent = newFolder
		firstAccessory = firstAccessory or accessory

		local issues = {}
		if seatErr then
			table.insert(issues, seatErr)
		end
		if handle.TextureID == "" and not handle:FindFirstChildOfClass("SurfaceAppearance") and item.textures.color then
			table.insert(issues, "the uploaded model came without its texture")
		end
		local checks = Geometry.studioChecks(item, mesh, activeRules(item))
		for _, c in instanceTreeChecks(accessory) do
			table.insert(checks, c)
		end
		for _, c in checks do
			if c.status == "fail" then
				table.insert(issues, c.message)
			end
		end
		local issue = table.concat(issues, "; ")
		if #issues > 0 then
			table.insert(failures, item.name .. ": " .. issue)
		end
		table.insert(lines, {
			status = if #issues > 0 then "fail" else "pass",
			message = if #issues > 0 then item.name .. ": " .. issue else item.name,
		})
	end

	-- Everything so far is off to the side, so this one step is all that undo has to reverse.
	local recording = ChangeHistoryService:TryBeginRecording("FitBridge build " .. set.name)
	if old then
		old:Destroy()
	end
	newFolder.Parent = workspace
	if recording then
		ChangeHistoryService:FinishRecording(recording, Enum.FinishRecordingOperation.Commit)
	end

	lastBuilt = firstAccessory
	Selection:Set({ firstAccessory })
	return { count = count, lines = lines, notes = notes, failures = failures }, nil
end

---------------------------------------------------------------- UI

local widget = plugin:CreateDockWidgetPluginGui(
	"FitBridge",
	DockWidgetPluginGuiInfo.new(Enum.InitialDockState.Right, true, false, 300, 480, 260, 320)
)
widget.Title = "FitBridge"

local theme = settings().Studio.Theme
local function color(c)
	return theme:GetColor(c)
end

local root = Instance.new("ScrollingFrame")
root.Size = UDim2.fromScale(1, 1)
root.BackgroundColor3 = color(Enum.StudioStyleGuideColor.MainBackground)
root.BorderSizePixel = 0
root.AutomaticCanvasSize = Enum.AutomaticSize.Y
root.CanvasSize = UDim2.new()
root.ScrollBarThickness = 6
root.Parent = widget
local pad = Instance.new("UIPadding")
pad.PaddingTop, pad.PaddingBottom, pad.PaddingLeft, pad.PaddingRight =
	UDim.new(0, 8), UDim.new(0, 8), UDim.new(0, 8), UDim.new(0, 8)
pad.Parent = root
local layout = Instance.new("UIListLayout")
layout.Padding = UDim.new(0, 6)
layout.SortOrder = Enum.SortOrder.LayoutOrder
layout.Parent = root

local order = 0
local function nextOrder()
	order += 1
	return order
end

local function label(text, bold)
	local l = Instance.new("TextLabel")
	l.LayoutOrder = nextOrder()
	l.Size = UDim2.new(1, 0, 0, 0)
	l.AutomaticSize = Enum.AutomaticSize.Y
	l.BackgroundTransparency = 1
	l.TextWrapped = true
	l.TextXAlignment = Enum.TextXAlignment.Left
	l.Font = if bold then Enum.Font.SourceSansBold else Enum.Font.SourceSans
	l.TextSize = 15
	l.TextColor3 = color(Enum.StudioStyleGuideColor.MainText)
	l.Text = text
	l.Parent = root
	return l
end

local function button(text, onClick)
	local b = Instance.new("TextButton")
	b.LayoutOrder = nextOrder()
	b.Size = UDim2.new(1, 0, 0, 28)
	b.BackgroundColor3 = color(Enum.StudioStyleGuideColor.Button)
	b.BorderSizePixel = 0
	b.Font = Enum.Font.SourceSansSemibold
	b.TextSize = 15
	b.TextColor3 = color(Enum.StudioStyleGuideColor.ButtonText)
	b.Text = text
	b.Parent = root
	local busy = false
	b.Activated:Connect(function()
		if busy then
			return
		end
		busy = true
		task.spawn(function()
			local ok, e = pcall(onClick)
			if not ok then
				warn("[FitBridge] " .. tostring(e))
			end
			busy = false
		end)
	end)
	return b
end

local function textBox(placeholder, initial, onChange)
	local t = Instance.new("TextBox")
	t.LayoutOrder = nextOrder()
	t.Size = UDim2.new(1, 0, 0, 26)
	t.BackgroundColor3 = color(Enum.StudioStyleGuideColor.InputFieldBackground)
	t.BorderSizePixel = 0
	t.ClearTextOnFocus = false
	t.Font = Enum.Font.SourceSans
	t.TextSize = 15
	t.TextColor3 = color(Enum.StudioStyleGuideColor.MainText)
	t.PlaceholderText = placeholder
	t.Text = initial
	t.TextXAlignment = Enum.TextXAlignment.Left
	t.FocusLost:Connect(function()
		onChange(t.Text)
	end)
	t.Parent = root
	return t
end

local ICON = { pass = "✓", warn = "!", fail = "✗" }

label("FitBridge", true)
local connLabel = label("Blender: not connected")
local rulesLabel = label("Rules: " .. (if getLiveRules() then "live from Roblox" else "bundled snapshot"))
local statusLabel = label("Fit the accessory in Blender, then press Send to Studio there.")
local checksLabel = label("")

local autoBuild = plugin:GetSetting("FitBridgeAutoBuild")
if autoBuild == nil then
	autoBuild = true
end

local function showChecks(checks, problems)
	local lines = {}
	for _, c in checks or {} do
		table.insert(lines, ("%s %s"):format(ICON[c.status] or "?", c.message))
	end
	for _, p in problems or {} do
		table.insert(lines, "! " .. p)
	end
	checksLabel.Text = table.concat(lines, "\n")
end

local function buildCurrent(moveToCamera)
	local manifest, err = request("GET", "/v1/package")
	if not manifest then
		statusLabel.Text = err
		return
	end
	statusLabel.Text = ("Building %s (%s · %s · %s)…"):format(
		manifest.name,
		manifest.accessory_type,
		manifest.attachment,
		manifest.body_scale
	)
	local result, problems, checks = build(manifest, moveToCamera)
	showChecks(checks, problems)
	if result then
		rulesLabel.Text = "Rules: " .. result.rulesSource
	end
	local failed, uploadCheck = false, nil
	for _, c in checks or {} do
		if c.id == "uploaded" then
			uploadCheck = c
		elseif c.status == "fail" then
			failed = true
		end
	end
	local message
	if not result then
		message = "Build failed: " .. table.concat(problems or {}, "; ")
	elseif not failed and uploadCheck and uploadCheck.status == "fail" then
		message = "Built " .. manifest.name .. " (fit is right). " .. uploadCheck.message
		failed = true
	elseif failed then
		message = "Built " .. manifest.name .. ", but some checks fail (see list). Fix in Blender and send again."
	else
		message = "Built " .. manifest.name .. ". Right-click it in Explorer → Save to Roblox."
	end
	statusLabel.Text = message
	request("POST", "/v1/package/result", {
		protocol = PROTOCOL,
		revision = manifest.revision,
		ok = result ~= nil and not failed,
		mesh_asset_id = result and result.meshId,
		texture_asset_ids = result and result.textureIds,
		studio_checks = checks,
		problems = problems,
		message = message,
	})
end

local setBuilding = false

local function buildLatestSet(moveToCamera)
	if setBuilding then
		return
	end
	setBuilding = true
	local set, err = request("GET", "/v1/set")
	if not set then
		statusLabel.Text = err
		setBuilding = false
		return
	end
	statusLabel.Text = ("Building %d colours of %s…"):format(#set.items, set.name)
	local ran, built, buildErr = pcall(buildSet, set, moveToCamera)
	local message
	local ok = false
	if not ran then
		message = "Couldn't build the colours: " .. tostring(built)
	elseif not built then
		message = buildErr
	elseif #built.failures > 0 then
		message = ("Built %d colours, but %s%s Fix in Blender and send again."):format(
			built.count,
			built.failures[1],
			if #built.failures > 1 then (", and %d more."):format(#built.failures - 1) else "."
		)
	else
		message = ("Built %d colours. Right-click each → Save to Roblox."):format(built.count)
		ok = true
	end
	if ran and built then
		showChecks(built.lines, built.notes)
	else
		showChecks()
	end
	statusLabel.Text = message
	setBuilding = false
	request("POST", "/v1/set/result", { protocol = PROTOCOL, revision = set.revision, ok = ok, message = message })
end

-- Only Build now moves the rig. An auto-build on Send leaves it where you're looking at it.
button("Build now", function()
	buildCurrent(true)
end)

button("Build colours", function()
	buildLatestSet(true)
end)

button("Preview thumbnail camera", function()
	local acc = lastBuilt
	if not acc or not acc.Parent then
		statusLabel.Text = "Build an accessory first."
		return
	end
	local handle = acc:FindFirstChild("Handle") :: BasePart
	local value = acc.ThumbnailConfiguration.ThumbnailCameraValue.Value
	local cam = workspace.CurrentCamera
	cam.FieldOfView = SPEC.thumbnail.field_of_view
	cam.CFrame = handle.CFrame * value
	statusLabel.Text = "Camera set to the saved thumbnail view (FOV "
		.. SPEC.thumbnail.field_of_view
		.. "). Roblox may zoom slightly closer when it renders."
end)

button("Select built accessory", function()
	if lastBuilt and lastBuilt.Parent then
		Selection:Set({ lastBuilt })
	end
end)

button("Sync rules + rigs to Blender", function()
	statusLabel.Text = "Sending rules and rigs…"
	local ok, err = sendRules()
	local msgs = { if ok then "Rules sent." else "Rules: " .. tostring(err) }
	-- Blender only offers Classic (plus the R6 preview). Normal and Slender are still built if Blender asks.
	for _, name in { "R6", "Classic" } do
		local okRig, rigErr = sendRig(name)
		table.insert(msgs, if okRig then name .. " rig sent." else name .. " rig: " .. tostring(rigErr))
	end
	statusLabel.Text = table.concat(msgs, " ")
end)

local autoButton
autoButton = button("", function()
	autoBuild = not autoBuild
	plugin:SetSetting("FitBridgeAutoBuild", autoBuild)
	autoButton.Text = "Auto-build on Send: " .. (if autoBuild then "on" else "off")
end)
autoButton.Text = "Auto-build on Send: " .. (if autoBuild then "on" else "off")

label("Connection", true)
textBox("http://127.0.0.1:47231", BASE_URL, function(text)
	BASE_URL = string.gsub(text, "/+$", "")
	plugin:SetSetting("FitBridgeURL", BASE_URL)
end)
label("Upload as (User or Group) and its id. Must match who uploads the UGC item.")
local kind, id = creator()
textBox("User", kind, function(text)
	plugin:SetSetting("FitBridgeCreatorType", if string.lower(text) == "group" then "Group" else "User")
end)
textBox(tostring(id), tostring(id), function(text)
	plugin:SetSetting("FitBridgeCreatorId", text)
end)

---------------------------------------------------------------- polling

local lastSent = nil
local lastUploadBuilt = nil
local lastSet = nil
local sentRules = false
local sentRigs = {}

local function handleNeeds(needs)
	for _, need in needs or {} do
		if need == "rules" and not sentRules then
			sentRules = sendRules()
		else
			local scale = string.match(need, "^rig:(%w+)$")
			if scale and not sentRigs[scale] then
				sentRigs[scale] = sendRig(scale)
			end
		end
	end
end

task.spawn(function()
	while true do
		if widget.Enabled then
			local hello, err = request("GET", "/v1/hello")
			if hello then
				connLabel.Text = ("Blender: connected (%s %s)"):format(hello.app or "?", hello.app_version or "")
				pcall(handleNeeds, hello.needs)
				-- Build on a new Send (preview), then again once Blender's Open Cloud upload for it is done.
				local uploadDone = hello.upload_state == "done" and hello.upload_revision == hello.sent_revision
				if lastSent == nil then
					lastSent = hello.sent_revision -- don't rebuild an old send on first connect
					lastUploadBuilt = if uploadDone then hello.sent_revision else nil
				elseif hello.sent_revision and hello.sent_revision ~= lastSent then
					lastSent = hello.sent_revision
					lastUploadBuilt = nil
					if autoBuild then
						if uploadDone then
							lastUploadBuilt = hello.sent_revision
						end
						pcall(buildCurrent)
					else
						statusLabel.Text = "New version sent from Blender. Press Build now."
					end
				elseif uploadDone and lastUploadBuilt ~= hello.sent_revision and autoBuild then
					lastUploadBuilt = hello.sent_revision
					pcall(buildCurrent)
				end
				-- A set takes a while, so it builds beside this loop and Blender keeps seeing us connected.
				if lastSet == nil then
					lastSet = hello.set_revision -- don't rebuild an old set on first connect
				elseif hello.set_revision and hello.set_revision ~= lastSet and not setBuilding then
					lastSet = hello.set_revision
					if lastSet > 0 then -- 0 means Blender restarted and has no set yet
						if autoBuild then
							task.spawn(buildLatestSet)
						else
							statusLabel.Text = "New colours sent from Blender. Press Build colours."
						end
					end
				end
			else
				connLabel.Text = "Blender: " .. tostring(err)
				sentRules = false
				table.clear(sentRigs)
			end
		end
		task.wait(1)
	end
end)

---------------------------------------------------------------- toolbar

local toolbar = plugin:CreateToolbar("FitBridge")
local toggle = toolbar:CreateButton("FitBridge", "Build accessories sent from Blender", "rbxassetid://4458901886")
toggle.ClickableWhenViewportHidden = true
toggle.Click:Connect(function()
	widget.Enabled = not widget.Enabled
end)
widget:GetPropertyChangedSignal("Enabled"):Connect(function()
	toggle:SetActive(widget.Enabled)
end)
