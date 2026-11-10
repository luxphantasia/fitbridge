"""The drawing kit behind FitBridge's panel.

Blender only lets an add-on draw ON TOP of the sidebar, so the panel is two layers that share one list of rows:
  * the panel's draw() lays out ordinary Blender buttons and fields in rows of known heights: they take the clicks,
    drags and typing;
  * a POST_PIXEL handler paints the visible panel over exactly those rows.
The layout never says where a panel ended up (other panels sit above it, the sidebar scrolls), so a few rows carry
three tiny marker icons in colours nothing else uses. The painter reads those pixels back to find the rows: the middle
one gives the height, the outer two the left and right edges.
"""

import math
import os
import time

import blf
import bpy
import bpy.utils.previews
import gpu
import numpy as np
from gpu_extras.batch import batch_for_shader

# ------------------------------------------------------------------------------------------------ palette (graphite)

BG = (0.094, 0.094, 0.102, 1.0)         # the card (opaque: the real buttons underneath must not show)
S1 = (0.125, 0.125, 0.137, 1.0)         # wells: fields, tracks, tiles
S2 = (0.157, 0.157, 0.172, 1.0)         # buttons
S3 = (0.212, 0.212, 0.231, 1.0)         # the chosen segment, a pressed button
LINE = (1.0, 1.0, 1.0, 0.08)
T1 = (0.925, 0.925, 0.941, 1.0)
T2 = (0.651, 0.651, 0.690, 1.0)
T3 = (0.431, 0.431, 0.471, 1.0)
ACC = (0.176, 0.549, 0.941, 1.0)        # FitBridge's one accent: the main action and the chosen thing
ACC_HI = (0.300, 0.640, 0.980, 1.0)
GOOD = (0.310, 0.749, 0.478, 1.0)
WARN = (0.851, 0.643, 0.255, 1.0)
BAD = (0.878, 0.341, 0.357, 1.0)
WHITE = (1.0, 1.0, 1.0, 1.0)

UNIT = 20.0                             # Blender's row height at UI scale 1
PAD = 12.0                              # the card's inner margin
MARK_H = 12.0                           # a marker row
SIDE_L, SIDE_R, TABS_W = 10.0, 10.0, 22.0   # a first guess at the layout's edges (the markers give the real ones)

FONT = {"ui": 0, "mono": None}
DRAW = {}                               # region pointer -> {"rows", "marks", "token", "seen", "cache"}
FLASH = {}                              # key -> time of the click
STATE = {"broken": False, "handle": None, "icons": None, "ids": [], "side": 0}
_TEX = {}

MARK_MAX = 8
MARK_SIDE_B = 250


def _mark_b(i):
    return 40 + 20 * i


def _mono():
    if FONT["mono"] is None:
        path = os.path.join(bpy.utils.system_resource("DATAFILES", path="fonts") or "", "DejaVuSansMono.woff2")
        FONT["mono"] = blf.load(path) if os.path.isfile(path) else 0
        if FONT["mono"] == -1:
            FONT["mono"] = 0
    return FONT["mono"]


def usable():
    """can the drawn panel work here? (not without a window, and not after its drawing failed once)"""
    return not bpy.app.background and not STATE["broken"] and bool(STATE["ids"])


# ------------------------------------------------------------------------------------------------ primitives

def _rr(x, y, w, h, r, seg=5):
    r = max(0.0, min(r, w / 2, h / 2))
    pts = []
    for cx, cy, a0 in ((x + w - r, y + h - r, 0), (x + r, y + h - r, 90), (x + r, y + r, 180), (x + w - r, y + r, 270)):
        for k in range(seg + 1):
            a = math.radians(a0 + 90 * k / seg)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def _fill(pts, col):
    cx = sum(p[0] for p in pts) / len(pts)
    cy = sum(p[1] for p in pts) / len(pts)
    tris = []
    for k in range(len(pts)):
        tris += [(cx, cy), pts[k], pts[(k + 1) % len(pts)]]
    sh = gpu.shader.from_builtin("UNIFORM_COLOR")
    gpu.state.blend_set("ALPHA")                     # text drawing turns blending off; hairlines need it
    sh.bind()
    sh.uniform_float("color", col)
    batch_for_shader(sh, "TRIS", {"pos": tris}).draw(sh)


def box(x, y, w, h, r, col, line=None):
    if w <= 0 or h <= 0:
        return
    if line is not None:
        _fill(_rr(x, y, w, h, r), line)
        _fill(_rr(x + 1, y + 1, w - 2, h - 2, max(0, r - 1)), col)
    else:
        _fill(_rr(x, y, w, h, r), col)


def rect(x, y, w, h, col):
    if w <= 0 or h <= 0:
        return
    sh = gpu.shader.from_builtin("UNIFORM_COLOR")
    gpu.state.blend_set("ALPHA")
    sh.bind()
    sh.uniform_float("color", col)
    batch_for_shader(sh, "TRI_FAN", {"pos": ((x, y), (x + w, y), (x + w, y + h), (x, y + h))}).draw(sh)


def corners(x, y, w, h, r, col):
    """paint over the four corners of a square picture so it reads as rounded"""
    tris = []
    for cx, cy, ox, oy, a0 in ((x + w - r, y + h - r, x + w, y + h, 0), (x + r, y + h - r, x, y + h, 90),
                               (x + r, y + r, x, y, 180), (x + w - r, y + r, x + w, y, 270)):
        arc = [(cx + r * math.cos(math.radians(a0 + 90 * k / 6)), cy + r * math.sin(math.radians(a0 + 90 * k / 6)))
               for k in range(7)]
        for k in range(6):
            tris += [(ox, oy), arc[k], arc[k + 1]]
    sh = gpu.shader.from_builtin("UNIFORM_COLOR")
    gpu.state.blend_set("ALPHA")
    sh.bind()
    sh.uniform_float("color", col)
    batch_for_shader(sh, "TRIS", {"pos": tris}).draw(sh)


def lines(pts, col, width=1.0, kind="LINES"):
    reg = bpy.context.region
    sh = gpu.shader.from_builtin("POLYLINE_UNIFORM_COLOR")
    gpu.state.blend_set("ALPHA")
    sh.bind()
    sh.uniform_float("viewportSize", (reg.width, reg.height))
    sh.uniform_float("lineWidth", width)
    sh.uniform_float("color", col)
    batch_for_shader(sh, kind, {"pos": pts}).draw(sh)


def hair(x, y, w, ui, col=LINE):
    rect(x, round(y), w, max(1.0, round(ui)), col)


def text_w(s, size, ui, mono=False):
    fid = _mono() if mono else FONT["ui"]
    blf.size(fid, size * ui)
    return blf.dimensions(fid, s)[0]


def text(x, y, s, size, col, ui, align="left", maxw=None, mono=False):
    fid = _mono() if mono else FONT["ui"]
    blf.size(fid, size * ui)
    if maxw is not None and blf.dimensions(fid, s)[0] > maxw:
        while s and blf.dimensions(fid, s + "…")[0] > maxw:
            s = s[:-1]
        s += "…"
    w = blf.dimensions(fid, s)[0]
    if align == "right":
        x -= w
    elif align == "center":
        x -= w / 2
    blf.color(fid, *col)
    blf.position(fid, round(x), round(y), 0)
    blf.draw(fid, s)
    return w


def mid_y(y, h, size, ui):
    """the baseline that centres `size` text in a row"""
    return y + h / 2 - size * ui * 0.36


def wrap(s, size, width, ui=1.0, mono=False):
    """the text in lines no wider than `width` pixels at this UI scale (measured, so nothing is ever cut off)"""
    fid = _mono() if mono else FONT["ui"]
    blf.size(fid, size * ui)
    out = []
    for para in str(s).split("\n"):
        cur = ""
        for word in para.split():
            trial = f"{cur} {word}".strip()
            if cur and blf.dimensions(fid, trial)[0] > width:
                out.append(cur)
                cur = word
            else:
                cur = trial
            while blf.dimensions(fid, cur)[0] > width and len(cur) > 4:      # one word wider than the panel
                n = len(cur) - 1
                while n > 4 and blf.dimensions(fid, cur[:n])[0] > width:
                    n -= 1
                out.append(cur[:n])
                cur = cur[n:]
        out.append(cur)
    return out or [""]


def flashing(key):
    return time.time() - FLASH.get(key, 0) < 0.18


def flash(key):
    FLASH[key] = time.time()
    bpy.app.timers.register(lambda: (redraw(), None)[1], first_interval=0.2)


def redraw():
    for win in bpy.context.window_manager.windows:
        for area in win.screen.areas:
            if area.type == "VIEW_3D":
                for r in area.regions:
                    if r.type == "UI":
                        r.tag_redraw()


def _texture(path):
    """a GPU texture of a picture file (loaded once per version of the file, never saved with the .blend)"""
    if not path or not os.path.isfile(path):
        return None
    mt = os.path.getmtime(path)
    hit = _TEX.get(path)
    if hit and hit[0] == mt:
        return hit
    try:
        img = bpy.data.images.load(path, check_existing=False)
        img.name = ".fitbridge_view"
        tex = gpu.texture.from_image(img)
        w, h = img.size
        bpy.data.images.remove(img)
    except Exception:  # noqa: BLE001 - an unreadable file just shows as empty
        return None
    if len(_TEX) > 24:
        _TEX.clear()
    _TEX[path] = (mt, tex, max(w, 1), max(h, 1))
    return _TEX[path]


def picture(tex, x, y, w, h):
    sh = gpu.shader.from_builtin("IMAGE")
    gpu.state.blend_set("NONE")
    sh.bind()
    sh.uniform_sampler("image", tex)
    batch_for_shader(sh, "TRI_FAN", {"pos": ((x, y), (x + w, y), (x + w, y + h), (x, y + h)),
                                     "texCoord": ((0, 0), (1, 0), (1, 1), (0, 1))}).draw(sh)
    gpu.state.blend_set("ALPHA")


def image(path, x, y, w, h):
    t = _texture(path)
    if t is None:
        return False
    picture(t[1], x, y, w, h)
    return True


# ------------------------------------------------------------------------------------------------ glyphs (1.5 px lines)

def g_check(x, y, s, col, ui):
    lines([(x + s * 0.20, y + s * 0.50), (x + s * 0.42, y + s * 0.28), (x + s * 0.42, y + s * 0.28),
           (x + s * 0.82, y + s * 0.72)], col, 1.6 * ui)


def g_x(x, y, s, col, ui):
    k = s * 0.26
    cx, cy = x + s / 2, y + s / 2
    lines([(cx - k, cy - k), (cx + k, cy + k), (cx - k, cy + k), (cx + k, cy - k)], col, 1.6 * ui)


def g_bang(x, y, s, col, ui):
    cx = x + s / 2
    lines([(cx, y + s * 0.80), (cx, y + s * 0.40)], col, 1.7 * ui)
    box(cx - 1.1 * ui, y + s * 0.16, 2.2 * ui, 2.2 * ui, 1.1 * ui, col)


def g_chev(x, y, s, col, ui, d="right"):
    cx, cy, k = x + s / 2, y + s / 2, s * 0.18
    pts = {"right": [(cx - k, cy + 2 * k), (cx + k, cy), (cx - k, cy - 2 * k)],
           "down": [(cx - 2 * k, cy + k), (cx, cy - k), (cx + 2 * k, cy + k)],
           "up": [(cx - 2 * k, cy - k), (cx, cy + k), (cx + 2 * k, cy - k)]}[d]
    lines(pts, col, 1.5 * ui, "LINE_STRIP")


def g_updown(x, y, s, col, ui):
    """the two small arrows of a pop-up list"""
    cx, cy, k = x + s / 2, y + s / 2, s * 0.16
    lines([(cx - k, cy + k * 0.9), (cx, cy + k * 1.9), (cx + k, cy + k * 0.9)], col, 1.4 * ui, "LINE_STRIP")
    lines([(cx - k, cy - k * 0.9), (cx, cy - k * 1.9), (cx + k, cy - k * 0.9)], col, 1.4 * ui, "LINE_STRIP")


def g_mark(x, y, s, ui):
    """FitBridge's mark: the bounds box as four corner brackets, the item sitting inside it"""
    box(x, y, s, s, s * 0.28, S2)
    a, b, k = x + s * 0.24, x + s * 0.76, s * 0.16
    c, d = y + s * 0.24, y + s * 0.76
    for px, py, dx, dy in ((a, c, 1, 1), (b, c, -1, 1), (a, d, 1, -1), (b, d, -1, -1)):
        lines([(px + dx * k, py), (px, py), (px, py + dy * k)], T2, 1.4 * ui, "LINE_STRIP")
    r = s * 0.13
    box(x + s / 2 - r, y + s / 2 - r, 2 * r, 2 * r, r * 0.45, ACC)


# ------------------------------------------------------------------------------------------------ widgets

def button(x, y, w, h, label, ui, key=None, primary=False, enabled=True, right=None, on=False):
    """primary = the accent fill; otherwise a raised graphite surface with a hairline. `on` = a toggle that's on."""
    hit = flashing(key) if key else False
    if primary and enabled:
        box(x, y, w, h, 10 * ui, ACC_HI if hit else ACC)
        tc, rc = WHITE, (1, 1, 1, 0.75)
    elif primary:
        box(x, y, w, h, 10 * ui, S1, LINE)
        tc, rc = T3, T3
    else:
        box(x, y, w, h, 8 * ui, S3 if (hit or on) else S2, LINE)
        tc, rc = (T1 if enabled else T3), T3
    size = 12
    if right:
        text(x + 14 * ui, mid_y(y, h, size, ui), label, size, tc, ui, maxw=w - 28 * ui - text_w(right, 11, ui) - 8 * ui)
        text(x + w - 14 * ui, mid_y(y, h, 11, ui), right, 11, rc, ui, align="right")
    else:
        text(x + w / 2, mid_y(y, h, size, ui), label, size, tc, ui, align="center", maxw=w - 12 * ui)


def checkbox(x, y, h, label, on, ui, col=T2):
    s = 14 * ui
    by = y + h / 2 - s / 2
    if on:
        box(x, by, s, s, 4 * ui, ACC)
        g_check(x, by, s, WHITE, ui)
    else:
        box(x, by, s, s, 4 * ui, S1, (1, 1, 1, 0.18))
    text(x + s + 8 * ui, mid_y(y, h, 12, ui), label, 12, col, ui)


def switch(x, y, h, on, ui):
    """a small on/off switch, right-aligned at x"""
    w, s = 26 * ui, 14 * ui
    by = y + h / 2 - s / 2
    box(x - w, by, w, s, s / 2, ACC if on else S3)
    k = s - 4 * ui
    box((x - k - 2 * ui) if on else (x - w + 2 * ui), by + 2 * ui, k, k, k / 2, WHITE if on else T2)


def segmented(x, y, w, h, items, active, ui, key_prefix="", tabs=False):
    """a segmented control: a graphite track, the chosen segment a surface step. Only the panel's own tabs get the
    accent underline, so a choice inside a tab never reads as a second tab bar."""
    box(x, y, w, h, 8 * ui, S1, LINE)
    sw = (w - 4 * ui) / len(items)
    for i, (val, label) in enumerate(items):
        sx = x + 2 * ui + i * sw
        is_on = val == active
        if is_on or flashing(f"{key_prefix}{val}"):
            box(sx, y + 2 * ui, sw, h - 4 * ui, 6 * ui, S3)
        if is_on and tabs:
            box(sx + sw * 0.32, y + 3 * ui, sw * 0.36, 2 * ui, 1 * ui, ACC)
        text(sx + sw / 2, mid_y(y, h, 12, ui), label, 12, T1 if is_on else T2, ui, align="center", maxw=sw - 6 * ui)


def chips(x, y, w, h, items, active, ui):
    """small equal buttons in a row (the chosen one stepped up)"""
    gap = 4 * ui
    cw = (w - gap * (len(items) - 1)) / len(items)
    for i, (val, label) in enumerate(items):
        cx = x + i * (cw + gap)
        on = val == active
        box(cx, y, cw, h, 8 * ui, S3 if on else S2, LINE)
        text(cx + cw / 2, mid_y(y, h, 12, ui), label, 12, T1 if on else T2, ui, align="center", maxw=cw - 4 * ui)


def pill(x, y, h, label, ui, align="left", key=None, enabled=True):
    """a small outlined action (the one look for every text-sized button). Returns its width."""
    w = text_w(label, 11, ui) + 20 * ui
    ph = 20 * ui
    px = x - w if align == "right" else x
    box(px, y + h / 2 - ph / 2, w, ph, 6 * ui, S3 if (key and flashing(key)) else S2, LINE)
    text(px + w / 2, mid_y(y, h, 11, ui), label, 11, T1 if enabled else T3, ui, align="center")
    return w


def pill_frac(label, width, ui):
    """the share of a row `width` pixels wide that a pill with this label needs (for the real button under it)"""
    return max(0.05, min(0.9, (text_w(label, 11, ui) + 24 * ui) / max(width, 1.0)))


def section(x, y, w, h, label, ui, link=None, note=None, key=None):
    text(x, y + 6 * ui, label, 11, T3, ui)
    if link:
        pill(x + w, y + 2 * ui, h - 4 * ui, link, ui, align="right", key=key)
    elif note:
        text(x + w, y + 6 * ui, note, 11, T3, ui, align="right")


def switch_row(x, y, w, h, label, on, ui, col=T2):
    text(x, mid_y(y, h, 12, ui), label, 12, col, ui, maxw=w - 34 * ui)
    switch(x + w, y, h, on, ui)


def field(x, y, w, h, label, value, ui, kind="list", dim=False, first=False, last=False):
    """one row of a grouped list: label on the left, its value on the right, hairline under it"""
    text(x + 12 * ui, mid_y(y, h, 12, ui), label, 12, T2, ui)
    lw = text_w(label, 12, ui)
    rx = x + w - 12 * ui
    if kind == "list":
        g_updown(rx - 12 * ui, y + h / 2 - 7 * ui, 14 * ui, T3, ui)
        rx -= 18 * ui
    text(rx, mid_y(y, h, 12, ui), value, 12, T3 if dim else T1, ui, align="right", maxw=w - lw - 60 * ui)
    if not last:
        hair(x + 12 * ui, y, w - 12 * ui, ui)


def slider(x, y, w, h, label, value, frac, ui, centred=False):
    """label, the figure, and a thin track under them (centred = the fill grows from the middle)"""
    text(x, y + h - 15 * ui, label, 12, T2, ui)
    text(x + w, y + h - 15 * ui, value, 12, T1, ui, align="right", mono=True)
    ty = y + 5 * ui
    box(x, ty, w, 3 * ui, 1.5 * ui, S3)
    frac = max(0.0, min(1.0, frac))
    if centred:
        a, b = sorted((0.5, frac))
        box(x + w * a, ty, max(2 * ui, w * (b - a)), 3 * ui, 1.5 * ui, T2)
        rect(x + w / 2 - 0.5 * ui, ty - 2 * ui, 1 * ui, 7 * ui, T3)
    else:
        box(x, ty, max(2 * ui, w * frac), 3 * ui, 1.5 * ui, T2)
    k = 9 * ui
    box(x + w * frac - k / 2, ty + 1.5 * ui - k / 2, k, k, k / 2, T1)


def status_line(x, y, w, h, s, kind, ui, size=12):
    """a line of text with its mark: good / warn / bad / info / busy"""
    col = {"good": GOOD, "pass": T3, "warn": WARN, "bad": BAD, "info": T3, "busy": T2, "none": T2}[kind]
    g = 14 * ui
    gy = y + h / 2 - g / 2
    if kind in ("good", "pass"):
        g_check(x, gy, g, col, ui)
    elif kind == "warn":
        g_bang(x, gy, g, col, ui)
    elif kind == "bad":
        g_x(x, gy, g, col, ui)
    elif kind in ("info", "busy"):
        box(x + g / 2 - 2 * ui, y + h / 2 - 2 * ui, 4 * ui, 4 * ui, 2 * ui, ACC if kind == "busy" else T3)
    tx = x + (20 * ui if kind != "none" else 0)
    text(tx, mid_y(y, h, size, ui), s, size, T1 if kind in ("bad", "warn") else T2, ui, maxw=w - (tx - x))


# ------------------------------------------------------------------------------------------------ rows

class Rows:
    """rows of (height at UI scale 1, real(layout), paint(x, y, w, h, ui)). Heights are multiples of 4.

    `width` is how wide the text area will be in pixels, for wrapping. A row's real buttons sit in the same inner
    area the painter gets, so a fraction of one is the same fraction of the other."""

    def __init__(self, layout, context):
        self.col = layout.column(align=True)
        self.items = []
        self.marks = []                      # (marker index, height above it)
        self.h = 0.0
        self.ui = context.preferences.system.ui_scale or 1.0
        reg_w = context.region.width if context.region else 300
        self.full = max(120.0, reg_w - (SIDE_L + SIDE_R + TABS_W) * self.ui)       # the layout's width, px
        self.width = self.full - 2 * PAD * self.ui - 4 * self.ui                    # safe text width, px

    def add(self, h, real=None, paint=None, raw=False, full=False):
        r = self.col.row(align=True)
        if not raw:
            r.scale_y = h / UNIT
        if real is None:
            r.label(text="")
        elif full:
            real(r)
        else:
            f = min(0.3, PAD * self.ui / self.full)
            sp = r.split(factor=f, align=True)
            sp.label(text="")
            rest = sp.split(factor=(1 - 2 * f) / (1 - f), align=True)
            real(rest.row(align=True))
            rest.label(text="")
        self.items.append((h, paint))
        self.h += h

    def gap(self, h):
        self.add(h)

    def mark(self):
        """a marker row: lets the painter find where the panel is (see the module's note)"""
        i = len(self.marks)
        if i >= MARK_MAX or not STATE["ids"]:
            return self.gap(MARK_H)
        r = self.col.row(align=True)
        a = r.split(factor=0.25, align=True)
        a.template_icon(icon_value=STATE["side"], scale=MARK_H / UNIT)
        b = a.split(factor=2 / 3, align=True)
        b.template_icon(icon_value=STATE["ids"][i], scale=MARK_H / UNIT)
        b.template_icon(icon_value=STATE["side"], scale=MARK_H / UNIT)
        self.marks.append((i, self.h))
        self.items.append((MARK_H, None))
        self.h += MARK_H

    def text(self, s, size=11, col=T2, indent=0.0, line=16, mono=False):
        """wrapped text, one row per line"""
        for part in wrap(s, size, self.width - indent * self.ui, self.ui, mono):
            self.add(line, paint=lambda x, y, w, h, ui, t=part: text(x + indent * ui, y + (h - size * ui) / 2 + 1.5 * ui,
                                                                    t, size, col, ui, mono=mono))

    def status(self, s, kind, size=12):
        """a wrapped line with a status mark on its first row"""
        parts = wrap(s, size, self.width - 20 * self.ui, self.ui)
        for i, part in enumerate(parts):
            if i == 0:
                self.add(20, paint=lambda x, y, w, h, ui, t=part: status_line(x, y, w, h, t, kind, ui, size))
            else:
                self.add(16, paint=lambda x, y, w, h, ui, t=part: text(
                    x + 20 * ui, mid_y(y, h, size, ui), t, size, T1 if kind in ("bad", "warn") else T2, ui))

    def card(self, start, r=10.0, col=S1, line=LINE, inset=0.0):
        """put a rounded surface behind every row added since index `start` (rows.n() before adding them)"""
        if start >= len(self.items):
            return
        total = sum(h for h, _p in self.items[start:])
        h0, inner = self.items[start]

        def paint(x, y, w, h, ui):
            box(x + inset * ui, y + h - total * ui, w - 2 * inset * ui, total * ui, r * ui, col, line)
            if inner is not None:
                inner(x, y, w, h, ui)
        self.items[start] = (h0, paint)

    def n(self):
        return len(self.items)

    def publish(self, context):
        if context.region is None:
            return
        key = context.region.as_pointer()
        d = DRAW.setdefault(key, {"token": 0, "seen": -1, "cache": None})
        d.update(rows=self.items, marks=self.marks)
        d["token"] += 1
        if len(DRAW) > 12:
            for k in list(DRAW)[:-12]:
                del DRAW[k]


def split(layout, *fracs):
    """layouts for consecutive fractions of a row (of the full width; the last takes the rest)"""
    out, rest, left = [], layout, 1.0
    for f in fracs[:-1]:
        sp = rest.split(factor=min(0.999, f / left), align=True)
        out.append(sp.column(align=True))
        rest = sp.column(align=True)
        left -= f
    out.append(rest)
    return out


def equal(layout, n):
    return split(layout, *([1.0 / n] * n))


# ------------------------------------------------------------------------------------------------ finding the panel

def _read(fb, x, y, w, h):
    buf = fb.read_color(x, y, w, h, 4, 0, "UBYTE")
    try:
        a = np.frombuffer(buf, dtype=np.uint8)
    except (TypeError, ValueError, BufferError):
        a = np.array(buf.to_list(), dtype=np.uint8)
    return a.reshape(h, w, 4)


def _is_mark(a):
    return (a[..., 0] > 238) & (a[..., 1] < 18)


def _find(region, d, ui):
    """(x0, x1, top) of the rows in region pixels, from the marker icons; None when none is in view"""
    W, H = region.width, region.height
    cx = (SIDE_L * ui + W - (SIDE_R + TABS_W) * ui) / 2
    sw = int(64 * ui)
    sx = int(max(0, min(W - sw, cx - sw / 2)))
    fb = gpu.state.active_framebuffer_get()
    a = _read(fb, sx, 0, sw, H)
    m = _is_mark(a)
    if not m.any():
        return None
    b = a[..., 2].astype(np.int16)
    want = (MARK_H - 6) * ui
    for i, above in d["marks"]:
        rows = np.nonzero((m & (np.abs(b - _mark_b(i)) <= 5)).any(axis=1))[0]
        if not len(rows):
            continue
        y0, y1 = int(rows[0]), int(rows[-1])
        if (y1 - y0 + 1) < want - 1.5 or y0 == 0 or y1 == H - 1:      # cut off by the region's edge
            continue
        cy = (y0 + y1 + 1) / 2
        line = _read(fb, 0, int(cy), W, 1)[0]
        side = np.nonzero(_is_mark(line) & (np.abs(line[..., 2].astype(np.int16) - MARK_SIDE_B) <= 5))[0]
        if len(side) < 2:
            continue
        cut = int(np.argmax(np.diff(side))) + 1
        left, right = side[:cut], side[cut:]
        if not len(left) or not len(right) or side[cut] - side[cut - 1] < 20 * ui:
            continue
        xl = (left[0] + left[-1] + 1) / 2
        xr = (right[0] + right[-1] + 1) / 2
        width = (xr - xl) / 0.75
        x0 = xl - width / 8
        return x0, x0 + width, cy + MARK_H * ui / 2 + above * ui
    return None


def _paint():
    if STATE["broken"]:
        return
    region = bpy.context.region
    d = DRAW.get(region.as_pointer()) if region is not None else None
    if d is None or d["seen"] == d["token"]:
        return
    d["seen"] = d["token"]                          # paint only right after the panel was laid out
    try:
        ui = bpy.context.preferences.system.ui_scale
        scroll = region.view2d.view_to_region(0, 0, clip=False)[1]
        got = _find(region, d, ui)
        if got is not None:
            d["cache"] = (got[0], got[1], got[2] - scroll)
        elif d["cache"] is not None:                # no marker in view: where it was, moved with the scrolling
            got = (d["cache"][0], d["cache"][1], d["cache"][2] + scroll)
        else:
            return
        x0, x1, top = got
        if os.environ.get("FITBRIDGE_UI_BARE"):         # for checking the real buttons under the paint
            return
        total = sum(h for h, _p in d["rows"]) * ui
        gpu.state.blend_set("ALPHA")
        box(x0 - 2 * ui, top - total - 2 * ui, (x1 - x0) + 4 * ui, total + 4 * ui, 12 * ui, BG, LINE)
        pad = PAD * ui
        y = top
        for h, paint in d["rows"]:
            hh = h * ui
            y -= hh
            if paint is not None and y < region.height and y + hh > 0:
                paint(x0 + pad, y, (x1 - x0) - 2 * pad, hh, ui)
        gpu.state.blend_set("NONE")
    except Exception as e:  # noqa: BLE001 - never repeat a drawing error on every redraw: the plain panel takes over
        STATE["broken"] = True
        print(f"FitBridge: the drawn panel is off ({e!r}); showing the plain one")
        redraw()


def register():
    if bpy.app.background:
        return
    try:
        pc = bpy.utils.previews.new()

        def make(name, blue):
            p = pc.new(name)
            px = [1.0, 0.0, blue / 255.0, 1.0] * 64
            p.image_size = (8, 8)
            p.image_pixels_float = px
            p.icon_size = (8, 8)
            p.icon_pixels_float = px
            return p.icon_id
        STATE["ids"] = [make(f"m{i}", _mark_b(i)) for i in range(MARK_MAX)]
        STATE["side"] = make("side", MARK_SIDE_B)
        STATE["icons"] = pc
    except Exception as e:  # noqa: BLE001
        STATE["ids"] = []
        print(f"FitBridge: no marker icons ({e!r}); showing the plain panel")
        return
    STATE["broken"] = False
    STATE["handle"] = bpy.types.SpaceView3D.draw_handler_add(_paint, (), "UI", "POST_PIXEL")


def unregister():
    if STATE["handle"] is not None:
        bpy.types.SpaceView3D.draw_handler_remove(STATE["handle"], "UI")
        STATE["handle"] = None
    if STATE["icons"] is not None:
        bpy.utils.previews.remove(STATE["icons"])
        STATE["icons"] = None
    STATE["ids"] = []
    DRAW.clear()
    _TEX.clear()
