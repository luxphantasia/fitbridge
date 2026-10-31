--!strict
-- Pure helpers for FitBridge (no Roblox types, so they run under the standalone `luau` CLI).
-- Vectors are {x, y, z} arrays. Mirrors Roblox's own UGCValidation checks where noted.

local Geometry = {}

export type Vec = { number }
export type Check = { id: string, status: string, message: string }

local EPS = 1e-4

-- Same test as UGCValidation/validateMeshBounds.lua: every vertex (already in the attachment's
-- frame, i.e. centred mesh + offset) must lie inside a box of `size` centred at `boundsOffset`.
-- Returns ok, and the worst overshoot per axis (0 when inside).
function Geometry.vertsInBounds(positions: { number }, offset: Vec, size: Vec, boundsOffset: Vec): (boolean, Vec)
	local over = { 0, 0, 0 }
	local half = { size[1] / 2, size[2] / 2, size[3] / 2 }
	for i = 1, #positions, 3 do
		for a = 1, 3 do
			local p = positions[i + a - 1] + offset[a] - boundsOffset[a]
			local excess = math.abs(p) - half[a]
			if excess > over[a] then
				over[a] = excess
			end
		end
	end
	local ok = over[1] <= EPS and over[2] <= EPS and over[3] <= EPS
	return ok, over
end

-- Axis-aligned min/max of a flat position list.
function Geometry.minMax(positions: { number }): (Vec, Vec)
	local mn = { math.huge, math.huge, math.huge }
	local mx = { -math.huge, -math.huge, -math.huge }
	for i = 1, #positions, 3 do
		for a = 1, 3 do
			local v = positions[i + a - 1]
			if v < mn[a] then
				mn[a] = v
			end
			if v > mx[a] then
				mx[a] = v
			end
		end
	end
	return mn, mx
end

-- Roblox rejects meshes whose box centre is off origin (validateMeshBounds "not centered").
function Geometry.centreError(positions: { number }): number
	local mn, mx = Geometry.minMax(positions)
	local c = { (mn[1] + mx[1]) / 2, (mn[2] + mx[2]) / 2, (mn[3] + mx[3]) / 2 }
	return math.sqrt(c[1] * c[1] + c[2] * c[2] + c[3] * c[3])
end

function Geometry.isPow2(n: number): boolean
	return n > 0 and math.floor(n) == n and bit32.band(n, n - 1) == 0
end

-- Resolve "same_as:OtherAttachment" references in the bundled spec.
function Geometry.resolveBounds(attachments: { [string]: any }, name: string, scale: string): (Vec?, Vec?)
	local entry = attachments[name]
	local guard = 0
	while entry and type(entry.bounds) == "string" and guard < 4 do
		local other = string.match(entry.bounds, "^same_as:(.+)$")
		entry = other and attachments[other] or nil
		guard += 1
	end
	if not entry or type(entry.bounds) ~= "table" then
		return nil, nil
	end
	local b = entry.bounds[scale]
	if not b then
		return nil, nil
	end
	return b.size, b.offset or { 0, 0, 0 }
end

-- Base64 → buffer (for EditableImage:WritePixelsBuffer). Ignores whitespace.
local DECODE: { [number]: number } = {}
do
	local chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
	for i = 1, #chars do
		DECODE[string.byte(chars, i)] = i - 1
	end
end

function Geometry.base64ToBuffer(s: string): buffer
	s = string.gsub(s, "[^%w%+/=]", "")
	local len = #s
	local pad = 0
	if string.sub(s, -2) == "==" then
		pad = 2
	elseif string.sub(s, -1) == "=" then
		pad = 1
	end
	local outLen = (len // 4) * 3 - pad
	local out = buffer.create(math.max(outLen, 0))
	local o = 0
	for i = 1, len, 4 do
		local a = DECODE[string.byte(s, i)] or 0
		local b = DECODE[string.byte(s, i + 1)] or 0
		local c = DECODE[string.byte(s, i + 2)] or 0
		local d = DECODE[string.byte(s, i + 3)] or 0
		local n = bit32.bor(bit32.lshift(a, 18), bit32.lshift(b, 12), bit32.lshift(c, 6), d)
		if o < outLen then
			buffer.writeu8(out, o, bit32.extract(n, 16, 8))
		end
		if o + 1 < outLen then
			buffer.writeu8(out, o + 1, bit32.extract(n, 8, 8))
		end
		if o + 2 < outLen then
			buffer.writeu8(out, o + 2, bit32.extract(n, 0, 8))
		end
		o += 3
	end
	return out
end

-- Studio-side re-check of a package manifest + mesh against the active rules.
-- `rules` = { maxTriangles, maxTextureSize, size = Vec, offset = Vec }.
function Geometry.studioChecks(manifest: any, mesh: any, rules: any): { Check }
	local checks: { Check } = {}
	local function add(id, ok, msg, warnOnly)
		table.insert(checks, { id = id, status = if ok then "pass" elseif warnOnly then "warn" else "fail", message = msg })
	end

	local tris = #mesh.triangles // 3
	add("triangles", tris <= rules.maxTriangles, ("%d / %d triangles"):format(tris, rules.maxTriangles))

	local centre = Geometry.centreError(mesh.positions)
	add("centred", centre <= 0.01, ("Mesh centre is %.4f studs from origin"):format(centre))

	local mn, mx = Geometry.minMax(mesh.positions)
	local meshSize = { mx[1] - mn[1], mx[2] - mn[2], mx[3] - mn[3] }
	local inside, over = Geometry.vertsInBounds(mesh.positions, manifest.mesh.offset, rules.size, rules.offset)
	if inside then
		add("bounds", true, "Every vertex is inside the " .. manifest.attachment .. " box")
	else
		local axes = { "X", "Y", "Z" }
		local parts = {}
		for a = 1, 3 do
			if over[a] > EPS then
				table.insert(parts, ("%s %.3f over"):format(axes[a], over[a]))
			end
		end
		add("bounds", false, "Outside the " .. manifest.attachment .. " box: " .. table.concat(parts, ", "))
	end
	local handleFits = meshSize[1] <= rules.size[1] + EPS
		and meshSize[2] <= rules.size[2] + EPS
		and meshSize[3] <= rules.size[3] + EPS
	add("handle_size", handleFits, ("Handle %.2f × %.2f × %.2f"):format(meshSize[1], meshSize[2], meshSize[3]))

	for map, info in manifest.textures or {} do
		if info then
			local ok = info.width <= rules.maxTextureSize and info.height <= rules.maxTextureSize
			add("texture_" .. map, ok, ("%s %d×%d (max %d)"):format(map, info.width, info.height, rules.maxTextureSize))
		end
	end
	return checks
end

return Geometry
