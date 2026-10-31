-- luau studio_plugin/tests/geometry.test.lua   (run from the repo root)
local Geometry = require("../src/Geometry")

local failures = 0
local function check(name, cond)
	if cond then
		print("ok   " .. name)
	else
		print("FAIL " .. name)
		failures += 1
	end
end

-- A 1-stud cube centred on origin, worn 0.5 up from the attachment.
local cube = { -0.5, -0.5, -0.5, 0.5, 0.5, 0.5, -0.5, 0.5, -0.5, 0.5, -0.5, 0.5 }
local ok, over = Geometry.vertsInBounds(cube, { 0, 0.5, 0 }, { 1.87, 2.5, 1.87 }, { 0, 0, 0 })
check("cube fits hat box", ok)
ok, over = Geometry.vertsInBounds(cube, { 0, 1.0, 0 }, { 1.87, 2.5, 1.87 }, { 0, 0, 0 })
check("cube pushed up overshoots Y", not ok and math.abs(over[2] - 0.25) < 1e-6 and over[1] == 0)
-- Back box is shifted 0.8115 behind; a cube 2.4 behind the attachment fits only thanks to the offset.
ok = Geometry.vertsInBounds(cube, { 0, 0, 2.4 }, { 9.86, 8.59, 4.87 }, { 0, 0, 0.8115 })
check("back offset honoured", ok)
ok = Geometry.vertsInBounds(cube, { 0, 0, 2.4 }, { 9.86, 8.59, 4.87 }, { 0, 0, 0 })
check("back without offset fails", not ok)

check("centred cube", Geometry.centreError(cube) < 1e-9)
check("shifted mesh", math.abs(Geometry.centreError({ 0, 0, 0, 1, 0, 0 }) - 0.5) < 1e-9)

check("pow2 1024", Geometry.isPow2(1024))
check("pow2 1000", not Geometry.isPow2(1000))

local hair = {
	HairAttachment = { bounds = { Normal = { size = { 1, 2, 3 }, offset = { 0, -0.3, 0.1 } } } },
	Alias = { bounds = "same_as:HairAttachment" },
}
local s, o = Geometry.resolveBounds(hair, "Alias", "Normal")
check("same_as resolves", s and s[3] == 3 and o and o[2] == -0.3)

local b = Geometry.base64ToBuffer("AAECAwQ=") -- bytes 0,1,2,3,4
check("base64 length", buffer.len(b) == 5)
check("base64 bytes", buffer.readu8(b, 0) == 0 and buffer.readu8(b, 4) == 4)
local b2 = Geometry.base64ToBuffer("/w==")
check("base64 padding", buffer.len(b2) == 1 and buffer.readu8(b2, 0) == 255)

local checks = Geometry.studioChecks(
	{ attachment = "HatAttachment", mesh = { offset = { 0, 0.5, 0 } }, textures = { color = { width = 4096, height = 4096 } } },
	{ positions = cube, triangles = { 0, 1, 2, 0, 2, 3 } },
	{ maxTriangles = 4000, maxTextureSize = 2048, size = { 1.87, 2.5, 1.87 }, offset = { 0, 0, 0 } }
)
local byId = {}
for _, c in checks do
	byId[c.id] = c.status
end
check("studio: bounds pass", byId.bounds == "pass")
check("studio: 4k texture fails", byId.texture_color == "fail")
check("studio: tris pass", byId.triangles == "pass")

-- An asymmetric shape: a point sticking out the front (-Z) and one to the right (+X).
local shape = { 0, 0, -1, 1, 0, 0, 0, 0.5, 0, -0.2, -0.3, 0.4 }
local rot = Geometry.bestYRotation(shape, shape)
check("orientation: identical mesh is 0°", rot == 0)
local turned = {}
for i = 1, #shape, 3 do
	table.insert(turned, -shape[i])
	table.insert(turned, shape[i + 1])
	table.insert(turned, -shape[i + 2])
end
rot = Geometry.bestYRotation(shape, turned)
check("orientation: importer-turned mesh is 180°", rot == 180)

if failures > 0 then
	error(failures .. " test(s) failed")
end
print("all passed")
