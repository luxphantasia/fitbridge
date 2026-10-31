# The localhost bridge (docs/PROTOCOL.md). Blender serves, Studio polls. This thread never touches bpy: it serves
# snapshots the main thread made (plain bytes / dicts under a lock) and queues what Studio POSTs for the main thread.

import json
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import upload

PROTOCOL = 1
HOST = "127.0.0.1"
STUDIO_TIMEOUT = 3.0           # seconds without a request before Studio counts as gone
STUDIO_SESSION_GAP = 15.0      # a gap this long means a new Studio session (ask for the rules again)

lock = threading.Lock()
inbox = queue.Queue()
state = {
    "hello": {"app": "FitBridge Blender", "app_version": "0.4.2", "blender_version": "?"},
    "package_revision": 0,
    "sent_revision": 0,
    "blobs": {},                # path -> JSON bytes (mesh, textures) of the last Send
    "manifest": None,           # the last Send's manifest (dict; its upload block is filled in per request)
    "need_rules": True,
    "need_rigs": set(),
    "last_request": 0.0,
    "protocol_mismatch": None,
}
_server = {"httpd": None, "thread": None, "port": None, "error": None}


def studio_connected():
    return time.time() - state["last_request"] < STUDIO_TIMEOUT


def needs():
    with lock:
        out = ["rules"] if state["need_rules"] else []
        out += [f"rig:{s}" for s in sorted(state["need_rigs"])]
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "FitBridge"

    def _send(self, code, body):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, code, sentence):
        self._send(code, {"protocol": PROTOCOL, "error": sentence})

    def _touch(self):
        now = time.time()
        with lock:
            if now - state["last_request"] > STUDIO_SESSION_GAP:
                state["need_rules"] = True
            state["last_request"] = now

    def do_GET(self):
        self._touch()
        path = self.path.split("?", 1)[0].rstrip("/")
        if path == "/v1/hello":
            with lock:
                body = {"protocol": PROTOCOL, **state["hello"], "package_revision": state["package_revision"],
                        "sent_revision": state["sent_revision"]}
            body["needs"] = needs()
            body.update(upload.hello_fields())
            self._send(200, body)
            return
        if path == "/v1/package":
            with lock:
                manifest = state["manifest"]
            if manifest is not None:
                self._send(200, {**manifest, "upload": upload.public()})
                return
        with lock:
            blob = state["blobs"].get(path)
            sent = state["manifest"] is not None
        if blob is not None:
            self._send(200, blob)
        elif path.startswith("/v1/package/texture/"):
            self._error(404, f"This package has no {path.rsplit('/', 1)[1]} texture.")
        elif path.startswith("/v1/package") and not sent:
            self._error(404, "Nothing sent yet. Press Send to Studio in Blender.")
        else:
            self._error(404, f"No such endpoint {path}")

    def do_POST(self):
        self._touch()
        path = self.path.split("?", 1)[0].rstrip("/")
        if path not in ("/v1/rules", "/v1/rig", "/v1/package/result"):
            self._error(404, f"No such endpoint {path}")
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except (ValueError, UnicodeDecodeError):
            self._error(400, "The request body isn't valid JSON.")
            return
        if body.get("protocol", PROTOCOL) != PROTOCOL:
            with lock:
                state["protocol_mismatch"] = body.get("protocol")
            self._error(409, f"Blender speaks FitBridge protocol {PROTOCOL}, Studio sent "
                             f"{body.get('protocol')}. Update the other plugin.")
            return
        if path == "/v1/rules":
            with lock:
                state["need_rules"] = False
        elif path == "/v1/rig":
            with lock:
                key = "R6" if body.get("rig_type") == "R6" or body.get("body_scale") == "R6" else body.get("body_scale")
                state["need_rigs"].discard(key)
        inbox.put((path, body))
        self._send(200, {"protocol": PROTOCOL})

    def log_message(self, *args):
        pass


def start(port):
    stop()
    try:
        httpd = ThreadingHTTPServer((HOST, port), Handler)
    except OSError as e:
        _server["error"] = f"Port {port} is busy ({e.strerror}). Another Blender may be serving FitBridge."
        _server["port"] = None
        return False
    httpd.daemon_threads = True
    t = threading.Thread(target=httpd.serve_forever, name="FitBridge server", daemon=True)
    t.start()
    _server.update(httpd=httpd, thread=t, port=port, error=None)
    return True


def stop():
    httpd = _server["httpd"]
    if httpd is not None:
        httpd.shutdown()
        httpd.server_close()
    _server.update(httpd=None, thread=None, port=None)


def running():
    return _server["httpd"] is not None


def status_text():
    if not running():
        return _server["error"] or "Bridge: off"
    if state["protocol_mismatch"] is not None:
        return "Studio plugin speaks another protocol: update the other plugin"
    if studio_connected():
        return f"Bridge: Studio connected (port {_server['port']})"
    return f"Bridge: listening on {HOST}:{_server['port']}"


def publish(blobs, manifest, revision):
    with lock:
        state["blobs"] = blobs
        state["manifest"] = manifest
        state["package_revision"] = revision
        state["sent_revision"] = revision
