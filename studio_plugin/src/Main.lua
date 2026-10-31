-- FitBridge Studio plugin.
-- Pulls a fitted rigid accessory from the FitBridge Blender addon over localhost, uploads its mesh and
-- textures under your account, builds a Marketplace-ready Accessory (Handle, attachment, thumbnail
-- camera) on a test avatar, and re-runs Roblox's checks. You then right-click → Save to Roblox.
-- Also sends Blender Roblox's live validation rules and the exact R15 rig for each body scale.
-- Built by studio_plugin/build.py; edit src/ files. Protocol: docs/PROTOCOL.md.

local HttpService = game:GetService("HttpService")
local AssetService = game:GetService("AssetService")
local Players = game:GetService("Players")
local Selection = game:GetService("Selection")
local ChangeHistoryService = game:GetService("ChangeHistoryService")
local StudioService = game:GetService("StudioService")

--#GEOMETRY

local SPEC = HttpService:JSONDecode([==[--#SPEC]==])

local PROTOCOL = 1
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

local function makeRig(scaleName)
	local scale = SPEC.body_scales[scaleName] or SPEC.body_scales.Classic
	local desc = Instance.new("HumanoidDescription")
	desc.BodyTypeScale = scale.humanoid_description.BodyTypeScale
	desc.ProportionScale = scale.humanoid_description.ProportionScale
	local rig = Players:CreateHumanoidModelFromDescription(desc, Enum.HumanoidRigType.R15)
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

local function readPartMesh(part: MeshPart)
	local ok, em = pcall(function()
		return AssetService:CreateEditableMeshAsync(Content.fromUri(part.MeshId))
	end)
	if not ok or not em then
		return nil
	end
	local scale = part.Size / part.MeshSize
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

local function sendRig(scaleName)
	local ok, rig = pcall(makeRig, scaleName)
	if not ok then
		return false, "Couldn't build the R15 rig: " .. tostring(rig)
	end
	rig.Parent = workspace -- joints only resolve in the DataModel
	rig:PivotTo(CFrame.new(0, 0, 0))
	local parts, attachments = {}, {}
	for _, d in rig:GetDescendants() do
		if d:IsA("BasePart") and d.Name ~= "HumanoidRootPart" then
			table.insert(parts, {
				name = d.Name,
				size = arr(d.Size),
				cframe = cframeArr(d.CFrame),
				mesh = if d:IsA("MeshPart") then readPartMesh(d) else nil,
			})
		elseif d:IsA("Attachment") and not string.find(d.Name, "RigAttachment") then
			table.insert(attachments, { name = d.Name, part = d.Parent.Name, world_cframe = cframeArr(d.WorldCFrame) })
		end
	end
	rig:Destroy()
	local _, err = request("POST", "/v1/rig", {
		protocol = PROTOCOL,
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
	em:Destroy()
	if id then
		rememberUpload(manifest.mesh.sha256, id)
	end
	return id, err
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
	img:Destroy()
	if id then
		rememberUpload(info.sha256, id)
	end
	return id, upErr
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

local function setTexture(handle: MeshPart, pbr, ids)
	local function uri(id)
		return "rbxassetid://" .. tostring(id)
	end
	if pbr then
		local sa = Instance.new("SurfaceAppearance")
		local props = { color = "ColorMap", normal = "NormalMap", roughness = "RoughnessMap", metalness = "MetalnessMap" }
		for map, prop in props do
			if ids[map] then
				local ok = pcall(function()
					(sa :: any)[prop .. "Content"] = Content.fromUri(uri(ids[map]))
				end)
				if not ok then
					(sa :: any)[prop] = uri(ids[map])
				end
			end
		end
		sa.Parent = handle
	elseif ids.color then
		handle.TextureID = uri(ids.color)
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

	local meshId, meshErr = meshAssetId(manifest, mesh)
	if not meshId then
		return nil, { "Mesh upload failed: " .. tostring(meshErr) }, checks
	end
	local textureIds = {}
	for map, info in manifest.textures or {} do
		if info then
			local id, texErr = textureAssetId(manifest, map, info)
			if id then
				textureIds[map] = id
			else
				table.insert(problems, texErr)
			end
		end
	end

	local okPart, handle = pcall(function()
		return AssetService:CreateMeshPartAsync(Content.fromUri("rbxassetid://" .. meshId))
	end)
	if not okPart then
		okPart, handle = pcall(function()
			return (AssetService :: any):CreateMeshPartAsync("rbxassetid://" .. meshId)
		end)
	end
	if not okPart then
		return nil, { "Couldn't create the MeshPart: " .. tostring(handle) }, checks
	end

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
	setTexture(handle, manifest.pbr, textureIds)

	local attachment = Instance.new("Attachment")
	attachment.Name = manifest.attachment
	attachment.CFrame = CFrame.new(-v3(manifest.mesh.offset))
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
	camValue.Value = cframeFrom(manifest.thumbnail.camera_cframe)
	camValue.Parent = cfg
	cfg.Parent = accessory

	-- Replace any earlier build of this item, then wear it on the matching test avatar.
	local rig = dummyFor(manifest.body_scale)
	for _, old in rig:GetChildren() do
		if old:IsA("Accessory") and old.Name == manifest.name then
			old:Destroy()
		end
	end
	local humanoid = rig:FindFirstChildOfClass("Humanoid")
	-- No attributes on the Accessory: Roblox's AttributesAllowed check rejects them.
	humanoid:AddAccessory(accessory)

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
	local failed = false
	for _, c in checks or {} do
		if c.status == "fail" then
			failed = true
		end
	end
	local message
	if not result then
		message = "Build failed: " .. table.concat(problems or {}, "; ")
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
	for name in SPEC.body_scales do
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
				if lastSent == nil then
					lastSent = hello.sent_revision -- don't rebuild an old send on first connect
				elseif hello.sent_revision and hello.sent_revision ~= lastSent then
					lastSent = hello.sent_revision
					if autoBuild then
						pcall(buildCurrent)
					else
						statusLabel.Text = "New version sent from Blender. Press Build now."
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
