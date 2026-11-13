"""Your own fits, told to Paradyse Studio (its Blender API: POST /fits), so its hand-fit lessons can learn from them.

When you send an accessory to Roblox Studio or upload it from here, FitBridge also tells Paradyse where it sits: the
placement (its box centre in the body attachment's frame; the turn and the size are already in the mesh), the
thumbnail shot, its size and triangles, and the mesh itself as a GLB. Only when a Paradyse Blender key is set in the
preferences (the same one the Listing tab uses): the key is never in this code, and nothing personal is sent.

It never holds up the send or the upload: it is made after they have started, on its own thread, and a failure is one
quiet line in Blender's console. A Blender with the upload lock on (the UGC autopilot's) never calls it.
"""
import json
import threading
import urllib.error
import urllib.request
import uuid

from . import host, upload
from .version import VERSION

UA = f"FitBridge-Blender/{VERSION}"
TIMEOUT = 30
MESH_MAX = 12 << 20
STATE = {"last": "", "sending": 0}


def _size(res):
    """the item's size in studs as placed: wide, deep, tall (Roblox's x, z, y)"""
    size = (res.get("fit") or {}).get("size")
    s = [float(x) for x in (size if size is not None else (0.0, 0.0, 0.0))]
    return [round(s[0], 4), round(s[2], 4), round(s[1], 4)]


def payload(name, res, g):
    """the fit as POST /fits takes it (no bpy here except what is passed in)"""
    offset = [round(float(x), 6) for x in res["fit"]["offset"]]
    return {
        "name": name, "kind": res["acc_type"],
        "placement": {"rig": res["scale"], "attachment": res["attachment"],
                      "cframe": offset + [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0], "scale": [1.0, 1.0, 1.0]},
        "rule": None,                     # where the autopilot's rule would put it: not worked out in your Blender
        "thumb": {"turn": round(float(g.turn), 4), "tilt": round(float(g.tilt), 4),
                  "distance": round(float(g.distance), 5), "pan": [round(float(g.pan_x), 5), round(float(g.pan_y), 5)],
                  "pivot": [round(float(x), 5) for x in g.pivot]},
        "size": _size(res), "triangles": len(res["mesh"]["tri_loops"]) if res.get("mesh") is not None else None,
        "asset_id": None, "fitbridge": VERSION,
    }


def _post(url, key, fields, files):
    b = uuid.uuid4().hex
    parts = []
    for name, text in fields:
        parts.append(f"--{b}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n".encode()
                     + text.encode() + b"\r\n")
    for name, filename, data, ctype in files:
        parts.append(f"--{b}\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"{filename}\"\r\n"
                     f"Content-Type: {ctype}\r\n\r\n".encode() + data + b"\r\n")
    parts.append(f"--{b}--\r\n".encode())
    req = urllib.request.Request(url + "/api/blender/v1/fits", data=b"".join(parts), method="POST")
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("User-Agent", UA)
    req.add_header("Content-Type", f"multipart/form-data; boundary={b}")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.status, json.loads(r.read() or b"{}")


def _work(url, key, fit, glb):
    try:
        files = []
        if glb is not None and len(glb) <= MESH_MAX:
            fit = dict(fit, mesh="mesh")
            files.append(("mesh", "mesh.glb", glb, "model/gltf-binary"))
        status, body = _post(url, key, [("fit", json.dumps(fit))], files)
        STATE["last"] = f"told Paradyse the fit of {fit['name']} ({status}, {body.get('id', '')})"
    except urllib.error.HTTPError as e:
        STATE["last"] = f"Paradyse didn't take the fit of {fit['name']} ({e.code})"
        print("FitBridge:", STATE["last"])
    except Exception as e:  # noqa: BLE001 - never more than a quiet line: the send and the upload go on regardless
        STATE["last"] = f"couldn't tell Paradyse the fit of {fit['name']} ({type(e).__name__})"
        print("FitBridge:", STATE["last"])
    finally:
        STATE["sending"] -= 1


def tell(name, res, g, mesh=None, pixels=None, pbr=False):
    """after a send or an upload has started: tell Paradyse where this item sits, on a thread of its own. Returns the
    thread (for the tests), or None when nothing is sent (no Paradyse key, the upload lock on, no fit)."""
    if upload.locked() or res.get("fit") is None or res.get("A") is None:
        return None
    try:
        url, key = host.studio()
    except Exception:  # noqa: BLE001 - no settings: nothing to tell
        return None
    if not key:
        return None
    fit = payload(name, res, g)
    glb = None
    if mesh is not None:
        try:
            glb = upload.write_glb(mesh, pixels or {}, pbr, name)
        except Exception:  # noqa: BLE001 - the numbers go without the mesh
            glb = None
    STATE["sending"] += 1
    t = threading.Thread(target=_work, name="FitBridge fit to Paradyse", daemon=True, args=(url, key, fit, glb))
    t.start()
    return t
