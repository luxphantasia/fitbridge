# Main-thread work: read the accessory (evaluated mesh, so modifiers count), run every check live, and build the
# package Studio pulls (manifest, centred mesh in the attachment frame, RGBA textures, thumbnail camera).

import base64
import hashlib
import json
import struct
import zlib

import bpy
import numpy as np
from mathutils import Vector
from mathutils.bvhtree import BVHTree

from . import geom, spec

LIVE = {"result": None}          # the last evaluation, read by the panel and the drawing
_bvh_cache = {}


# ------------------------------------------------------------------------------------------------------ mesh reading

def target_object(context):
    st = context.scene.fitbridge
    ob = st.target or context.active_object
    if ob is not None and ob.type == "MESH" and not ob.get("fitbridge_rig"):
        return ob
    return None


def read_mesh(ob, depsgraph):
    """the evaluated mesh as plain arrays (world space, Blender axes), triangulated, only vertices used by faces"""
    ev = ob.evaluated_get(depsgraph)
    me = ev.to_mesh()
    try:
        me.calc_loop_triangles()
        nv, nl, nt = len(me.vertices), len(me.loops), len(me.loop_triangles)
        co = np.empty(nv * 3)
        me.vertices.foreach_get("co", co)
        co = co.reshape(-1, 3)
        tri_loops = np.empty(nt * 3, np.int64)
        me.loop_triangles.foreach_get("loops", tri_loops)
        tri_poly = np.empty(nt, np.int64)
        me.loop_triangles.foreach_get("polygon_index", tri_poly)
        loop_vert = np.empty(nl, np.int64)
        me.loops.foreach_get("vertex_index", loop_vert)
        loop_edge = np.empty(nl, np.int64)
        me.loops.foreach_get("edge_index", loop_edge)
        try:
            ln = np.empty(nl * 3)
            me.corner_normals.foreach_get("vector", ln)
        except AttributeError:                               # Blender before 4.1
            me.calc_normals_split()
            ln = np.empty(nl * 3)
            me.loops.foreach_get("normal", ln)
        uv = None
        if me.uv_layers.active is not None:
            uv = np.empty(nl * 2)
            me.uv_layers.active.data.foreach_get("uv", uv)
            uv = uv.reshape(-1, 2)
        pmat = np.zeros(len(me.polygons), np.int64)
        me.polygons.foreach_get("material_index", pmat)
        mats = [m for m in me.materials]
        used_slots = sorted(set(pmat[tri_poly].tolist())) if nt else []
        open_edges = 0
        if len(me.edges) and len(me.polygons):
            uses = np.bincount(loop_edge, minlength=len(me.edges))
            open_edges = int(((uses == 1) | (uses > 2)).sum())
    finally:
        ev.to_mesh_clear()
    M = np.array(ob.matrix_world)
    world = co @ M[:3, :3].T + M[:3, 3]
    N = np.linalg.inv(M[:3, :3]).T
    return {"co": world, "tri_loops": tri_loops.reshape(-1, 3), "loop_vert": loop_vert,
            "normals": ln.reshape(-1, 3) @ N.T, "uv": uv, "used_slots": used_slots, "materials": mats,
            "open_edges": open_edges, "flip": np.linalg.det(M[:3, :3]) < 0}


# ---------------------------------------------------------------------------------------------------------- textures

def _upstream_image(socket, depth=5):
    """the first image texture feeding a socket (through mixes, separate colour, normal map...)"""
    if not socket.is_linked or depth == 0:
        return None
    node = socket.links[0].from_node
    if node.type == "TEX_IMAGE" and node.image is not None:
        return node.image
    for s in node.inputs:
        img = _upstream_image(s, depth - 1)
        if img is not None:
            return img
    return None


def _principled(mat):
    if mat is None or not mat.use_nodes or mat.node_tree is None:
        return None
    out = next((n for n in mat.node_tree.nodes if n.type == "OUTPUT_MATERIAL" and n.is_active_output), None)
    if out is not None and out.inputs["Surface"].is_linked:
        n = out.inputs["Surface"].links[0].from_node
        if n.type == "BSDF_PRINCIPLED":
            return n
    return next((n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)


MAPS = (("color", "Base Color"), ("normal", "Normal"), ("roughness", "Roughness"), ("metalness", "Metallic"))


def find_textures(mat):
    """{map: image or None} from the material's Principled BSDF"""
    bsdf = _principled(mat)
    found = {m: None for m, _ in MAPS}
    if bsdf is None:
        return found
    for m, sock in MAPS:
        if sock in bsdf.inputs:
            found[m] = _upstream_image(bsdf.inputs[sock])
    return found


def image_rgba8(img):
    """raw RGBA8, top row first (Roblox / EditableImage order)"""
    w, h = img.size
    px = np.empty(w * h * 4, np.float32)
    img.pixels.foreach_get(px)
    a = (np.clip(px, 0, 1) * 255 + 0.5).astype(np.uint8).reshape(h, w, 4)[::-1]
    return np.ascontiguousarray(a)


def write_png(path, rgba):
    h, w = rgba.shape[:2]
    raw = b"".join(b"\x00" + rgba[y].tobytes() for y in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(data)


# ------------------------------------------------------------------------------------------------------------ checks

def _check(cid, status, message):
    return {"id": cid, "status": status, "message": message}


def _part_tree(rig_data, part_name):
    key = (id(rig_data), part_name)
    if key in _bvh_cache:
        return _bvh_cache[key]
    tree = None
    for p in rig_data["parts"]:
        if p["name"] != part_name:
            continue
        V, T = geom.part_mesh(p)
        C = geom.cframe_to_matrix(p["cframe"])
        Vw = (V @ C[:3, :3].T + C[:3, 3]) @ geom.P.T              # Roblox world -> Blender world
        # +1 when the triangles face outward (positive volume), so "inside" works whatever the winding
        a, b, c = Vw[T[:, 0]], Vw[T[:, 1]], Vw[T[:, 2]]
        sign = 1.0 if np.einsum("ij,ij->i", a, np.cross(b, c)).sum() >= 0 else -1.0
        tree = (BVHTree.FromPolygons([Vector(v) for v in Vw], T.tolist()), sign)
    if len(_bvh_cache) > 32:
        _bvh_cache.clear()
    _bvh_cache[key] = tree
    return tree


def floating_gap(V, rig_data, part_name, limit=4000):
    """the closest distance from the mesh to the rig part's surface (0 if any of it is inside the part)"""
    got = _part_tree(rig_data, part_name)
    if got is None or len(V) == 0:
        return None
    tree, sign = got
    if len(V) > limit:
        V = V[np.linspace(0, len(V) - 1, limit).astype(int)]
    best = float("inf")
    for v in V:
        loc, nrm, _i, d = tree.find_nearest(Vector(v))
        if loc is None:
            continue
        if (Vector(v) - loc).dot(nrm) * sign < 0:
            return 0.0
        best = min(best, d)
    return best


HEAVY_FACES = 150_000          # beyond this a mesh isn't read for live checks (Roblox allows 4,000 triangles anyway)


def in_use(context):
    """FitBridge only checks live in a file where you're using it: a mannequin shown, an accessory mesh picked, or
    Thumbnail view on. Otherwise selecting things (a sculpt with millions of faces...) costs nothing."""
    st = context.scene.fitbridge
    if st.target is not None or st.thumb_view:
        return True
    from .rig import R15_COLL, R6_COLL
    return any(bpy.data.collections.get(n) is not None for n in (R15_COLL, R6_COLL))


def evaluate(context):
    """run every check on the current accessory -> a result dict (also kept in LIVE)"""
    st = context.scene.fitbridge
    scale, acc_type, att = st.body_scale, st.acc_type, st.attachment
    lim = spec.limits()
    res = {"checks": [], "ok": False, "box": None, "inside": None, "over_lines": [], "object": None,
           "scale": scale, "acc_type": acc_type, "attachment": att}
    ob = target_object(context)
    rig_data = spec.rig(scale)
    A = spec.attachment_frame(rig_data, att)
    bsize, boff, bsrc = spec.bounds(acc_type, att, scale)
    res["bounds_source"] = bsrc
    if A is not None:
        res["box"] = (A @ geom.translation(boff), bsize)          # Roblox world
        res["A"] = A
    if ob is None:
        res["checks"].append(_check("single_mesh", "fail", "Pick a mesh object (or make one active)"))
        LIVE["result"] = res
        return res
    res["object"] = ob.name
    faces = len(ob.data.polygons)
    if faces > HEAVY_FACES:                                   # never read millions of faces just to say "too many"
        mx = int(lim["max_triangles"])
        res["checks"].append(_check("triangles", "fail", f"{faces:,} faces: far over {mx:,} triangles "
                                                         "(live checks paused for this mesh)"))
        res["heavy"] = True
        LIVE["result"] = res
        return res
    dg = context.evaluated_depsgraph_get()
    m = read_mesh(ob, dg)
    used = np.unique(m["loop_vert"][m["tri_loops"].ravel()]) if len(m["tri_loops"]) else np.zeros(0, np.int64)
    V = m["co"][used]
    ntri = len(m["tri_loops"])
    checks = res["checks"]

    # bounds (Roblox's own test)
    if not spec.rigid_allowed(acc_type):
        checks.append(_check("bounds", "fail", f"Roblox doesn't allow rigid {acc_type} accessories (layered only)"))
    elif ntri == 0:
        checks.append(_check("bounds", "fail", "The mesh has no faces"))
    elif A is None:
        checks.append(_check("bounds", "fail", f"The {scale} rig has no {att}"))
    else:
        vl = geom.to_attachment(V, A)
        t = geom.bounds_test(vl, bsize, boff)
        res["fit"] = t
        res["inside"] = t["inside"] and t["size_ok"]
        res["over_lines"] = geom.describe_over(t)
        where = f"{att} box ({scale}{', live rules' if bsrc == 'live' else ''})"
        if res["inside"]:
            checks.append(_check("bounds", "pass", f"Inside {where}"))
        else:
            checks.append(_check("bounds", "fail", "; ".join(res["over_lines"]) +
                                 (f" ({t['outside']} vertices outside {where})" if t["outside"] else "")))

    mx = int(lim["max_triangles"])
    checks.append(_check("triangles", "pass" if ntri <= mx else "fail", f"{ntri:,} / {mx:,} triangles"))

    checks.append(_check("single_mesh", "pass", f"One mesh object ({ob.name})"))

    slots = [i for i in m["used_slots"] if i < len(m["materials"]) and m["materials"][i] is not None]
    mats = {m["materials"][i].name for i in slots}
    mat = m["materials"][slots[0]] if slots else None
    if len(mats) > 1:
        checks.append(_check("single_material", "fail", f"{len(mats)} materials used ({', '.join(sorted(mats))})"))
    else:
        checks.append(_check("single_material", "pass", f"Material: {mat.name}" if mat else "No material"))

    tex = find_textures(mat)
    res["textures"] = tex
    images = [(k, v) for k, v in tex.items() if v is not None]
    if m["uv"] is None:
        checks.append(_check("uv_in_range", "fail" if images else "pass",
                             "No UV map" if images else "No UV map (no textures)"))
    else:
        uvs = m["uv"][m["tri_loops"].ravel()] if ntri else np.zeros((0, 2))
        bad = int(np.any((uvs < -1e-4) | (uvs > 1 + 1e-4), axis=1).sum())
        checks.append(_check("uv_in_range", "fail" if bad else "pass",
                             f"{bad} UV corners outside 0-1" if bad else "UVs inside 0-1"))

    mts = int(lim["max_texture_size"])
    if not images:
        checks.append(_check("texture_size", "pass", "No textures"))
        checks.append(_check("texture_pow2", "pass", "No textures"))
    else:
        big = [f"{k} {v.size[0]}x{v.size[1]}" for k, v in images if max(v.size) > mts]
        npot = [f"{k} {v.size[0]}x{v.size[1]}" for k, v in images
                if not (geom.pow2(v.size[0]) and geom.pow2(v.size[1]))]
        sizes = ", ".join(f"{k} {v.size[0]}x{v.size[1]}" for k, v in images)
        checks.append(_check("texture_size", "fail" if big else "pass",
                             f"Over {mts}: {', '.join(big)}" if big else sizes))
        checks.append(_check("texture_pow2", "fail" if npot else "pass",
                             f"Not a power of two: {', '.join(npot)}" if npot else "Power-of-two sizes"))

    oe = m["open_edges"]
    checks.append(_check("watertight", "warn" if oe else "pass", f"{oe} open edges" if oe else "Watertight"))

    part = spec.rig_part_for(acc_type, att)
    gap = floating_gap(V, rig_data, part) if part else None
    lim_gap = float(spec.SPEC["checks"].get("floating_gap", 0.2))
    if gap is None:
        checks.append(_check("floating", "pass", "No body part to measure against"))
    elif gap > lim_gap:
        checks.append(_check("floating", "warn", f"Floating {gap:.2f} studs from {part}"))
    else:
        checks.append(_check("floating", "pass", f"Touches {part}" if gap == 0 else f"{gap:.2f} studs from {part}"))

    for c in checks:
        sev = spec.SPEC["checks"].get(c["id"], {}).get("severity", "warn")
        if c["status"] == "fail" and sev != "block":
            c["status"] = "warn"
    res["ok"] = not any(c["status"] == "fail" for c in checks)
    res["pbr"] = any(tex[k] is not None for k in ("normal", "roughness", "metalness"))
    res["mesh"] = m
    LIVE["result"] = res
    return res


# ----------------------------------------------------------------------------------------------------------- package

def handle_frame(res):
    """the worn Handle's frame in Roblox world: A . CFrame.new(offset)"""
    return res["A"] @ geom.translation(res["fit"]["offset"])


def build_mesh(res):
    """positions / normals / uvs / triangles in the attachment frame, centred, split per corner"""
    m = res["mesh"]
    A, off = res["A"], res["fit"]["offset"]
    tl = m["tri_loops"]
    if m["flip"]:
        tl = tl[:, ::-1]
    loops = tl.ravel()
    verts = m["loop_vert"][loops]
    Ai = np.linalg.inv(A)
    pos = geom.to_attachment(m["co"][verts], A) - off
    nrm = m["normals"][loops] @ (Ai[:3, :3] @ geom.P).T
    nl = np.linalg.norm(nrm, axis=1, keepdims=True)
    nrm = nrm / np.where(nl > 1e-12, nl, 1)
    uv = m["uv"][loops] if m["uv"] is not None else np.zeros((len(loops), 2))
    uv = np.column_stack([uv[:, 0], 1 - uv[:, 1]])
    key = np.column_stack([verts, np.round(nrm, 4), np.round(uv, 5)])
    _u, first, inverse = np.unique(key, axis=0, return_index=True, return_inverse=True)
    order = np.argsort(first)                                   # keep vertices in first-use order
    remap = np.empty(len(order), np.int64)
    remap[order] = np.arange(len(order))
    idx = remap[inverse.ravel()]
    keep = first[order]
    r = lambda a, n: [round(float(x), n) for x in a.ravel()]
    return {"positions": r(pos[keep], 6), "normals": r(nrm[keep], 5), "uvs": r(uv[keep], 6),
            "triangles": idx.tolist()}


def build_package(context, res, revision, override):
    """everything Studio pulls, as ready-to-send JSON bytes per endpoint (built here on the main thread)"""
    st = context.scene.fitbridge
    mesh = build_mesh(res)
    mesh_sha = hashlib.sha256(json.dumps(mesh).encode()).hexdigest()
    blobs = {}
    textures = {}
    pixels = {}
    tdir = spec.cache_dir() / "textures"
    tdir.mkdir(exist_ok=True)
    for k, img in res["textures"].items():
        if img is None:
            textures[k] = None
            continue
        rgba = image_rgba8(img)
        pixels[k] = rgba
        sha = hashlib.sha256(rgba.tobytes()).hexdigest()
        png = tdir / f"{k}.png"
        write_png(png, rgba)
        h, w = rgba.shape[:2]
        textures[k] = {"sha256": sha, "width": w, "height": h}
        blobs[f"/v1/package/texture/{k}"] = {"protocol": 1, "sha256": sha, "width": w, "height": h,
                                             "rgba_base64": base64.b64encode(rgba.tobytes()).decode(),
                                             "png_path": str(png)}
    from . import thumb
    g, _ob = thumb.owner(context)
    thumb.sync(context, res)
    cam_cf = thumb.camera_cframe(res, g)
    fov = thumb.FOV
    sendable = res["ok"] or override
    manifest = {
        "protocol": 1, "revision": revision,
        "name": st.item_name or res["object"],
        "accessory_type": SPEC_TYPE(res["acc_type"]), "attachment": res["attachment"], "body_scale": res["scale"],
        "mesh": {"sha256": mesh_sha, "triangles": len(mesh["triangles"]) // 3,
                 "vertices": len(mesh["positions"]) // 3,
                 "size": [float(x) for x in res["fit"]["size"]], "offset": [float(x) for x in res["fit"]["offset"]]},
        "textures": textures,
        "pbr": bool(res["pbr"]),
        "thumbnail": {"camera_cframe": [round(x, 6) for x in cam_cf], "field_of_view": round(float(fov), 4)},
        "checks": [{k: c[k] for k in ("id", "status", "message")} for c in res["checks"]],
        "sendable": bool(sendable), "override": bool(override),
        "spec_source": spec.spec_source(),
    }
    blobs["/v1/package/mesh"] = {"protocol": 1, "revision": revision, "sha256": mesh_sha, **mesh}
    # the manifest itself is served as a dict: its upload block changes while the upload runs
    return {k: json.dumps(v).encode() for k, v in blobs.items()}, manifest, mesh, pixels


def SPEC_TYPE(acc_type):
    return spec.SPEC["accessory_types"][acc_type]["accessory_type"]
