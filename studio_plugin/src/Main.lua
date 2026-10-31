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
		maxTextureSize = SPEC.limits.max_texture_size,
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
			rules.maxTextureSize = live.TextureRules.MaxTextureSize or rules.maxTextureSize
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
	local _, err = request("POST", "/v1/rules", {
		protocol = PROTOCOL,
		fetched = DateTime.now():ToIsoDate(),
		studio_version = version(),
		limits = {
			max_triangles = live.MeshRules and live.MeshRules.AccessoryMaxTriangles,
			max_texture_size = live.TextureRules and live.TextureRules.MaxTextureSize,
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
	local maxEditable = SPEC.limits.editable_image_max_size
	local tex, err = request("GET", "/v1/package/texture/" .. map)
	if not tex then
		return nil, err
	end
	if tex.width > maxEditable or tex.height > maxEditable then
		return nil,
			("%s is %d×%d; Studio can only upload up to %d×%d from a plugin. Import %s with the 3D Importer and set it by hand."):format(
				map,
				tex.width,
				tex.height,
				maxEditable,
				maxEditable,
				tostring(tex.png_path)
			)
	end
	local okImg, img = pcall(function()
		return AssetService:CreateEditableImage({ Size = Vector2.new(tex.width, tex.height) })
	end)
	if not okImg or not img then
		return nil, "Couldn't create an image for " .. map .. ": " .. tostring(img)
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

local function folder()
	local f = workspace:FindFirstChild(FOLDER_NAME)
	if not f then
		f = Instance.new("Folder")
		f.Name = FOLDER_NAME
		f.Parent = workspace
	end
	return f
end

local function dummyFor(scaleName)
	for _, m in folder():GetChildren() do
		if m:GetAttribute(ATTR .. "Scale") == scaleName then
			return m
		end
	end
	local rig = makeRig(scaleName)
	local slot = #folder():GetChildren()
	rig.Parent = folder()
	rig:PivotTo(CFrame.new(slot * 10, rig:GetExtentsSize().Y / 2, 0))
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

local UPLOAD_NOTES = {
	off = "Preview only: add your Open Cloud API key in Blender's FitBridge preferences to make it uploadable.",
	pending = "Preview only: Blender is still uploading to Roblox. Studio rebuilds when it's done.",
}

local function build(manifest)
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
			uploadNote = "Couldn't load uploaded model " .. tostring(upload.model_asset_id) .. ": " .. tostring(loadErr)
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
					if not uploadNote then
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

	handle.Name = "Handle"
	handle.Size = v3(manifest.mesh.size)
	handle.Material = Enum.Material.Plastic
	handle.Transparency = 0
	handle.Color = Color3.new(1, 1, 1)
	handle.CanCollide = false
	handle.CanTouch = false
	handle.CanQuery = false
	handle.Massless = true
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

	-- Replace any earlier build of this item, then seat it on the matching test avatar's attachment.
	-- Not Humanoid:AddAccessory: wearing adds an AccessoryRigidConstraint that validation doesn't strip.
	-- No attributes on the Accessory either: Roblox's AttributesAllowed check rejects them.
	local rig = dummyFor(manifest.body_scale)
	for _, old in folder():GetChildren() do
		if old:IsA("Accessory") and old.Name == manifest.name then
			old:Destroy()
		end
	end
	local bodyAttachment = nil
	for _, d in rig:GetDescendants() do
		if d:IsA("Attachment") and d.Name == manifest.attachment and d.Parent ~= handle then
			bodyAttachment = d
			break
		end
	end
	if bodyAttachment then
		handle.CFrame = bodyAttachment.WorldCFrame * attachment.CFrame:Inverse()
	else
		table.insert(problems, "Test avatar has no " .. manifest.attachment .. "; placed above it.")
		handle.CFrame = rig:GetPivot() * CFrame.new(0, 6, 0)
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

local function buildCurrent()
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
	local result, problems, checks = build(manifest)
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

button("Build now", buildCurrent)

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
