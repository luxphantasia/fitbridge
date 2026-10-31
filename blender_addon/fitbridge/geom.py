# Pure maths shared by the checks, the drawing and the package (no bpy): the Blender <-> Roblox axis swap, CFrames,
# the fit (vertices into the body attachment's frame, centred box) and Roblox's own bounds test
# (UGCValidation/validateMeshBounds.lua: every vertex inside the box of bounds.size centred at bounds.offset in the
# attachment frame, and the Handle size no bigger than the box on any axis).

import math

import numpy as np

# Blender -> Roblox for positions and directions (docs/PROTOCOL.md). A proper rotation and its own inverse.
P = np.array([[-1.0, 0, 0], [0, 0, 1.0], [0, 1.0, 0]])
P4 = np.eye(4)
P4[:3, :3] = P


def cframe_to_matrix(cf):
    """[x, y, z, R00, R01, R02, R10, R11, R12, R20, R21, R22] (row-major, like CFrame:GetComponents()) -> 4x4"""
    m = np.eye(4)
    m[:3, 3] = cf[:3]
    m[:3, :3] = np.asarray(cf[3:12], float).reshape(3, 3)
    return m


def matrix_to_cframe(m):
    m = np.asarray(m, float)
    return [float(x) for x in m[:3, 3]] + [float(x) for x in m[:3, :3].ravel()]


def translation(v):
    m = np.eye(4)
    m[:3, 3] = v
    return m


def swap(m):
    """a frame between Blender and Roblox space (the same both ways: P is its own inverse); local axes unchanged"""
    return P4 @ np.asarray(m, float)


def unscaled(m):
    """a 4x4 with its rotation columns normalised (cameras and objects can carry scale)"""
    m = np.array(m, float)
    for i in range(3):
        n = np.linalg.norm(m[:3, i])
        if n > 1e-12:
            m[:3, i] /= n
    return m


def look_at(eye, target, up=(0.0, 1.0, 0.0)):
    """a camera frame at eye looking at target (cameras look down local -Z, +Y up), as a 4x4"""
    eye = np.asarray(eye, float)
    back = eye - np.asarray(target, float)
    back /= np.linalg.norm(back)
    right = np.cross(up, back)
    if np.linalg.norm(right) < 1e-9:
        right = np.array([1.0, 0, 0])
    right /= np.linalg.norm(right)
    upv = np.cross(back, right)
    m = np.eye(4)
    m[:3, 0], m[:3, 1], m[:3, 2], m[:3, 3] = right, upv, back, eye
    return m


def to_attachment(V_blender_world, A):
    """Blender world points -> the body attachment's local frame (Roblox space): A^-1 . P . v"""
    Ai = np.linalg.inv(A)
    R = Ai[:3, :3] @ P
    return V_blender_world @ R.T + Ai[:3, 3]


def fit_box(v_local):
    lo, hi = v_local.min(axis=0), v_local.max(axis=0)
    return (lo + hi) / 2, hi - lo


# which way each attachment-local axis points, in words (Roblox: +X character's right, +Y up, -Z forward)
SIDES = {(0, 1): "right side", (0, -1): "left side", (1, 1): "top", (1, -1): "bottom", (2, 1): "back", (2, -1): "front"}
AXIS = "XYZ"


def bounds_test(v_local, bounds_size, bounds_offset):
    """Roblox's test. -> dict: inside (bool), outside (vertex count), over (per axis, studs past the box, >= 0),
    over_side (per axis: +1 / -1 which side is worst), size, offset, size_ok, size_over (per axis)"""
    bs = np.asarray(bounds_size, float)
    rel = v_local - np.asarray(bounds_offset, float)
    half = bs / 2
    out = np.any((rel < -half) | (rel > half), axis=1)
    over_hi = rel.max(axis=0) - half
    over_lo = -half - rel.min(axis=0)
    over = np.maximum(np.maximum(over_hi, over_lo), 0.0)
    side = np.where(over_hi >= over_lo, 1, -1)
    offset, size = fit_box(v_local)
    size_over = np.maximum(size - bs, 0.0)
    return {"inside": not out.any(), "outside": int(out.sum()), "over": over, "over_side": side,
            "size": size, "offset": offset, "size_ok": not (size_over > 0).any(), "size_over": size_over}


def describe_over(t):
    """'Top (Y): 0.14 studs over' style lines for the axes that fail"""
    lines = []
    for a in range(3):
        if t["over"][a] > 0:
            side = SIDES[(a, int(t["over_side"][a]))]
            lines.append(f"{side.capitalize()} ({AXIS[a]}): {t['over'][a]:.2f} studs over")
    if not lines:
        for a in range(3):
            if t["size_over"][a] > 0:
                lines.append(f"{AXIS[a]}: {t['size_over'][a]:.2f} studs too big")
    return lines


def box_corners(frame, size):
    """the 8 corners of a box (frame 4x4, size) in that frame's parent space"""
    h = np.asarray(size, float) / 2
    c = np.array([[sx, sy, sz] for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)], float) * h
    return c @ frame[:3, :3].T + frame[:3, 3]


BOX_EDGES = [(0, 1), (2, 3), (4, 5), (6, 7), (0, 2), (1, 3), (4, 6), (5, 7), (0, 4), (1, 5), (2, 6), (3, 7)]
def _box_tris():
    """12 outward-facing triangles over the box_corners order (corner index = 4 sx + 2 sy + sz)"""
    c = np.array([[sx, sy, sz] for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)], float)
    tris = []
    for a in range(3):
        for s in (-1, 1):
            q = [i for i in range(8) if c[i][a] == s]          # 4 corners of this face, as a 2x2 grid
            b, d = [x for x in range(3) if x != a]
            q.sort(key=lambda i: (c[i][b], c[i][d]))
            quad = [q[0], q[1], q[3], q[2]]
            for t in ([quad[0], quad[1], quad[2]], [quad[0], quad[2], quad[3]]):
                n = np.cross(c[t[1]] - c[t[0]], c[t[2]] - c[t[0]])
                tris.append(t if n[a] * s > 0 else t[::-1])
    return tris


BOX_TRIS = _box_tris()


def box_mesh(size):
    """a box as positions + triangles (for rig parts Studio couldn't read, and the stand-in rig)"""
    return box_corners(np.eye(4), size), np.array(BOX_TRIS, np.int64)


def pow2(n):
    return n > 0 and (n & (n - 1)) == 0


def fov_ok(deg):
    return math.isfinite(deg) and 1 < deg < 179
