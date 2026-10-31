"""Fake FitBridge Blender server: serves a test hat over the real protocol (docs/PROTOCOL.md).

  python3 tools/mock_bridge.py                 # listen on 127.0.0.1:47231
  curl -X POST 127.0.0.1:47231/mock/send       # act like pressing "Send to Studio"

Lets the Studio plugin be tested without Blender, and is the reference the Blender addon must match.
Whatever Studio POSTs back (rules, rigs, results) is saved under tools/mock_out/.
"""

import base64
import hashlib
import json
import math
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 47231
OUT = Path(__file__).resolve().parent / "mock_out"
OUT.mkdir(exist_ok=True)


def halo_mesh(segments=24, rings=8, major=0.7, minor=0.12):
    """A torus lying flat (a halo), vertices split per corner, already centred on origin."""
    positions, normals, uvs, tris = [], [], [], []
    for i in range(segments + 1):
        u = i / segments
        a = u * 2 * math.pi
        for j in range(rings + 1):
            v = j / rings
            b = v * 2 * math.pi
            cx, cz = math.cos(a), math.sin(a)
            x = (major + minor * math.cos(b)) * cx
            y = minor * math.sin(b)
            z = (major + minor * math.cos(b)) * cz
            positions += [x, y, z]
            normals += [math.cos(b) * cx, math.sin(b), math.cos(b) * cz]
            uvs += [u, 1 - v]  # already Roblox-style (V from the top)
    row = rings + 1
    for i in range(segments):
        for j in range(rings):
            a, b = i * row + j, (i + 1) * row + j
            # Counter-clockwise seen from outside the tube (front face).
            tris += [a, a + 1, b, b, a + 1, b + 1]
    return positions, normals, uvs, tris


def checker(size=256, cells=8):
    px = bytearray()
    for y in range(size):
        for x in range(size):
            on = ((x * cells // size) + (y * cells // size)) % 2
            px += bytes((255, 210, 60, 255) if on else (255, 245, 200, 255))
    return bytes(px)


positions, normals, uvs, tris = halo_mesh()
mesh_body = {"positions": positions, "normals": normals, "uvs": uvs, "triangles": tris}
mesh_sha = hashlib.sha256(json.dumps(mesh_body).encode()).hexdigest()
tex = checker()
tex_sha = hashlib.sha256(tex).hexdigest()
xs, ys, zs = positions[0::3], positions[1::3], positions[2::3]
size = [max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)]

state = {"revision": 1, "sent": 0, "needs": ["rules", "rig:Normal"]}


def manifest():
    return {
        "protocol": 1,
        "revision": state["revision"],
        "name": "FitBridge Test Halo",
        "accessory_type": "Hat",
        "attachment": "HatAttachment",
        "body_scale": "Normal",
        "mesh": {"sha256": mesh_sha, "triangles": len(tris) // 3, "vertices": len(xs), "size": size, "offset": [0, 0.9, 0]},
        "textures": {"color": {"sha256": tex_sha, "width": 256, "height": 256}, "normal": None, "roughness": None, "metalness": None},
        "pbr": False,
        # 6 studs in front (-Z) of the Handle, 1.5 up, looking back at it (camera looks down its own -Z).
        "thumbnail": {"camera_cframe": look_at([0, 1.5, -6], [0, 0, 0]), "field_of_view": 70},
        "checks": [{"id": "bounds", "status": "pass", "message": "mock"}],
        "sendable": True,
        "override": False,
        "spec_source": "mock",
    }


def look_at(eye, target):
    f = [t - e for t, e in zip(target, eye)]
    n = math.sqrt(sum(c * c for c in f))
    f = [c / n for c in f]
    back = [-c for c in f]
    up = [0, 1, 0]
    right = [up[1] * back[2] - up[2] * back[1], up[2] * back[0] - up[0] * back[2], up[0] * back[1] - up[1] * back[0]]
    rn = math.sqrt(sum(c * c for c in right))
    right = [c / rn for c in right]
    up = [back[1] * right[2] - back[2] * right[1], back[2] * right[0] - back[0] * right[2], back[0] * right[1] - back[1] * right[0]]
    # Rows of the rotation matrix; columns are right, up, back.
    return [*eye, right[0], up[0], back[0], right[1], up[1], back[1], right[2], up[2], back[2]]


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/v1/hello":
            self._send(200, {"protocol": 1, "app": "FitBridge Mock", "app_version": "0.0", "blender_version": "mock",
                             "package_revision": state["revision"], "sent_revision": state["sent"], "needs": state["needs"]})
        elif self.path == "/v1/package":
            self._send(200, manifest())
        elif self.path == "/v1/package/mesh":
            self._send(200, {"protocol": 1, "revision": state["revision"], "sha256": mesh_sha, **mesh_body})
        elif self.path == "/v1/package/texture/color":
            self._send(200, {"protocol": 1, "sha256": tex_sha, "width": 256, "height": 256,
                             "rgba_base64": base64.b64encode(tex).decode(), "png_path": None})
        else:
            self._send(404, {"protocol": 1, "error": f"No such endpoint {self.path}"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        if self.path == "/mock/send":
            state["sent"] += 1
            self._send(200, {"protocol": 1, "sent_revision": state["sent"]})
            print(f"sent_revision -> {state['sent']}")
            return
        body = json.loads(raw or b"{}")
        name = {"/v1/rules": "rules.json", "/v1/package/result": "result.json"}.get(self.path)
        if self.path == "/v1/rig":
            name = f"rig_{body.get('body_scale', 'unknown')}.json"
            state["needs"] = [n for n in state["needs"] if n != f"rig:{body.get('body_scale')}"]
        if self.path == "/v1/rules":
            state["needs"] = [n for n in state["needs"] if n != "rules"]
        if not name:
            self._send(404, {"protocol": 1, "error": f"No such endpoint {self.path}"})
            return
        (OUT / name).write_text(json.dumps(body, indent=1))
        print(f"saved {name} ({len(raw)} bytes)")
        if self.path == "/v1/package/result":
            print("  ", body.get("message"))
            for c in body.get("studio_checks") or []:
                print("   ", c["status"], c["message"])
        self._send(200, {"protocol": 1})

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print(f"FitBridge mock on http://127.0.0.1:{PORT}  ({len(tris)//3} tris, size {[round(s, 3) for s in size]})")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
