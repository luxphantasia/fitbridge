# Open Cloud upload (PROTOCOL v1.1): Studio plugins can't upload assets, so Blender does, with the API key Lux types
# into the addon preferences. Runs on a worker thread from the frozen Send snapshot (plain data, no bpy):
# wire mesh -> .glb (our own tiny writer, so no hidden axis change) -> POST /assets/v1/assets -> poll the operation.
# The key is only ever put in the x-api-key header: never logged, never in a manifest, never over the bridge.

import json
import os
import struct
import threading
import time
import urllib.error
import urllib.request
import uuid
import zlib

import numpy as np

from . import spec

API = os.environ.get("FITBRIDGE_ROBLOX_API", "https://apis.roblox.com")   # tests point this at a local fake
USER_AGENT = "FitBridge-Blender/0.2"
POLL_EVERY = float(os.environ.get("FITBRIDGE_POLL_EVERY", "2.0"))
POLL_FOR = 300.0


def png_bytes(rgba):
    """RGBA8 (top row first) -> PNG"""
    h, w = rgba.shape[:2]
    raw = b"".join(b"\x00" + rgba[y].tobytes() for y in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


# --------------------------------------------------------------------------------------------------------------- glb

def write_glb(mesh, textures, pbr, name="FitBridge"):
    """a .glb from the wire mesh (Roblox space, centred) and RGBA8 textures {map: (h, w, 4) uint8 or None}.
    Positions and normals go in as they are; v_gltf = 1 - v_wire."""
    pos = np.asarray(mesh["positions"], np.float32).reshape(-1, 3)
    nrm = np.asarray(mesh["normals"], np.float32).reshape(-1, 3)
    uv = np.asarray(mesh["uvs"], np.float32).reshape(-1, 2).copy()
    uv[:, 1] = 1 - uv[:, 1]
    idx = np.asarray(mesh["triangles"], np.uint32)
    blob = bytearray()
    views, accessors = [], []

    def add_view(data, target=None):
        while len(blob) % 4:
            blob.append(0)
        views.append({"buffer": 0, "byteOffset": len(blob), "byteLength": len(data), **({"target": target} if target else {})})
        blob.extend(data)
        return len(views) - 1

    def add_accessor(arr, ctype, typ, target, minmax=False):
        v = add_view(arr.tobytes(), target)
        acc = {"bufferView": v, "componentType": ctype, "count": len(arr), "type": typ}
        if minmax:
            acc["min"] = [float(x) for x in arr.min(axis=0)]
            acc["max"] = [float(x) for x in arr.max(axis=0)]
        accessors.append(acc)
        return len(accessors) - 1

    a_pos = add_accessor(pos, 5126, "VEC3", 34962, minmax=True)
    a_nrm = add_accessor(nrm, 5126, "VEC3", 34962)
    a_uv = add_accessor(uv, 5126, "VEC2", 34962)
    a_idx = add_accessor(idx, 5125, "SCALAR", 34963)

    images, gl_textures = [], []

    def add_image(rgba):
        v = add_view(png_bytes(rgba))
        images.append({"bufferView": v, "mimeType": "image/png"})
        gl_textures.append({"source": len(images) - 1})
        return len(gl_textures) - 1

    pbr_block = {"metallicFactor": 0.0, "roughnessFactor": 1.0}
    material = {"name": name, "pbrMetallicRoughness": pbr_block}
    if textures.get("color") is not None:
        pbr_block["baseColorTexture"] = {"index": add_image(textures["color"])}
    if pbr:
        if textures.get("normal") is not None:
            material["normalTexture"] = {"index": add_image(textures["normal"])}
        rough, metal = textures.get("roughness"), textures.get("metalness")
        if rough is not None or metal is not None:
            ref = rough if rough is not None else metal
            h, w = ref.shape[:2]
            mr = np.zeros((h, w, 4), np.uint8)
            mr[..., 3] = 255
            mr[..., 1] = _channel(rough, h, w, 128)          # glTF: G = roughness, B = metalness
            mr[..., 2] = _channel(metal, h, w, 0)
            pbr_block["metallicRoughnessTexture"] = {"index": add_image(mr)}
            pbr_block["metallicFactor"] = 1.0
    gltf = {
        "asset": {"version": "2.0", "generator": USER_AGENT},
        "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0, "name": name}],
        "meshes": [{"name": name, "primitives": [{"attributes": {"POSITION": a_pos, "NORMAL": a_nrm,
                                                                  "TEXCOORD_0": a_uv},
                                                   "indices": a_idx, "material": 0, "mode": 4}]}],
        "materials": [material], "accessors": accessors, "bufferViews": views,
        "buffers": [{"byteLength": len(blob)}],
    }
    if images:
        gltf["images"], gltf["textures"] = images, gl_textures
        gltf["samplers"] = [{}]
        for t in gl_textures:
            t["sampler"] = 0
    js = json.dumps(gltf, separators=(",", ":")).encode()
    js += b" " * (-len(js) % 4)
    while len(blob) % 4:
        blob.append(0)
    total = 12 + 8 + len(js) + 8 + len(blob)
    return (struct.pack("<III", 0x46546C67, 2, total) + struct.pack("<II", len(js), 0x4E4F534A) + js
            + struct.pack("<II", len(blob), 0x004E4942) + bytes(blob))


def _channel(rgba, h, w, default):
    if rgba is None:
        return default
    if rgba.shape[:2] != (h, w):                              # different sizes: nearest-neighbour to the first one
        ys = np.arange(h) * rgba.shape[0] // h
        xs = np.arange(w) * rgba.shape[1] // w
        rgba = rgba[ys][:, xs]
    return rgba[..., 0]


# ------------------------------------------------------------------------------------------------------------ worker

lock = threading.Lock()
state = {"state": "off", "model_asset_id": None, "creator": None, "message": "", "revision": 0}


def public():
    """the manifest's upload block (no key in it, ever)"""
    with lock:
        return {k: state[k] for k in ("state", "model_asset_id", "creator", "message")}


def _set(revision, **kw):
    with lock:
        if revision != state["revision"]:                      # a newer Send took over
            return False
        state.update(kw)
    return True


def _cache():
    f = spec.cache_dir() / "upload_cache.json"
    try:
        return json.loads(f.read_text())
    except (OSError, ValueError):
        return {}


def _cache_put(key, asset_id):
    with lock:
        c = _cache()
        c[key] = asset_id
        f = spec.cache_dir() / "upload_cache.json"
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(c))
        tmp.replace(f)


def cache_key(manifest, creator):
    shas = [manifest["mesh"]["sha256"]] + [(t or {}).get("sha256", "-") for _k, t in sorted(manifest["textures"].items())]
    return f"{creator['type']}:{creator['id']}:" + ":".join(shas)


class OpenCloudError(Exception):
    pass


def _request(method, url, key, body=None, headers=None):
    h = {"x-api-key": key, "User-Agent": USER_AGENT, **(headers or {})}
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("message")
        except (ValueError, AttributeError):
            msg = None
        raise OpenCloudError(f"Roblox said {e.code}: {msg or e.reason}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise OpenCloudError(f"Couldn't reach Roblox ({getattr(e, 'reason', e)})") from None


def _multipart(request_json, glb, filename):
    b = uuid.uuid4().hex
    parts = [
        f"--{b}\r\nContent-Disposition: form-data; name=\"request\"\r\nContent-Type: application/json\r\n\r\n".encode()
        + json.dumps(request_json).encode() + b"\r\n",
        f"--{b}\r\nContent-Disposition: form-data; name=\"fileContent\"; filename=\"{filename}\"\r\n"
        f"Content-Type: model/gltf-binary\r\n\r\n".encode() + glb + b"\r\n",
        f"--{b}--\r\n".encode(),
    ]
    return b"".join(parts), f"multipart/form-data; boundary={b}"


def _run(revision, key, creator, name, mesh, textures, pbr, ckey):
    try:
        glb = write_glb(mesh, textures, pbr, name)
        who = {"userId" if creator["type"] == "User" else "groupId": str(creator["id"])}
        body, ctype = _multipart({"assetType": "Model", "displayName": name[:50],
                                  "description": "Made with FitBridge", "creationContext": {"creator": who}},
                                 glb, "fitbridge.glb")
        data = _request("POST", f"{API}/assets/v1/assets", key, body, {"Content-Type": ctype})
        op = data.get("operationId") or (data.get("path") or "").rsplit("/", 1)[-1]
        if not op:
            raise OpenCloudError("Roblox didn't return an operation id")
        _set(revision, message=f"Uploading to Roblox… (operation {op[:8]})")
        end = time.time() + POLL_FOR
        while time.time() < end:
            data = _request("GET", f"{API}/assets/v1/operations/{op}", key)
            if data.get("done"):
                if data.get("error"):
                    err = data["error"]
                    raise OpenCloudError(f"Roblox rejected the upload: {err.get('message', err) if isinstance(err, dict) else err}")
                aid = (data.get("response") or {}).get("assetId")
                if not aid:
                    raise OpenCloudError("Roblox finished without an asset id")
                aid = int(aid)
                _cache_put(ckey, aid)
                _set(revision, state="done", model_asset_id=aid, message=f"Uploaded ✓ model {aid}")
                return
            time.sleep(POLL_EVERY)
        raise OpenCloudError("Roblox is still processing after 5 minutes. Send again to retry")
    except OpenCloudError as e:
        _set(revision, state="failed", message=str(e))
    except Exception as e:                                     # never let the key or a traceback leak
        _set(revision, state="failed", message=f"Upload failed ({type(e).__name__})")


def start(revision, manifest, mesh, textures, key, creator_type, creator_id):
    """called on Send (main thread) with plain data; returns at once"""
    creator = {"type": creator_type, "id": int(creator_id)} if str(creator_id).strip().isdigit() else None
    with lock:
        state.update(revision=revision, model_asset_id=None, creator=creator)
    if not key:
        _set(revision, state="off", message="Add your Open Cloud key in FitBridge preferences to make this uploadable")
        return
    if creator is None:
        _set(revision, state="failed", message="Set the creator id (your user or group id) in FitBridge preferences")
        return
    ckey = cache_key(manifest, creator)
    cached = _cache().get(ckey)
    if cached:
        _set(revision, state="done", model_asset_id=int(cached), message=f"Uploaded ✓ model {cached} (unchanged)")
        return
    _set(revision, state="pending", message="Uploading to Roblox…")
    threading.Thread(target=_run, args=(revision, key, creator, manifest["name"], mesh, textures, manifest["pbr"], ckey),
                     name="FitBridge upload", daemon=True).start()


def hello_fields():
    with lock:
        return {"upload_state": state["state"], "upload_revision": state["revision"]}
