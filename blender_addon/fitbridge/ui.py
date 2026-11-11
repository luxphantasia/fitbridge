"""The drawing kit behind FitBridge's panel.

Blender only lets an add-on draw ON TOP of the sidebar, so the panel is two layers that share one list of rows:
  * the panel's draw() lays out ordinary Blender buttons and fields in rows of known heights: they take the clicks,
    drags and typing;
  * a POST_PIXEL handler paints the visible panel over exactly those rows.
The layout never says where a panel ended up (other panels sit above or below it, they get dragged into another
order, the sidebar scrolls), so the rows that have no button of their own carry three small marker icons in colours
nothing else uses, every 60 pixels or so. The painter reads those pixels back before it paints over them: the middle
icon of each gives that row's height on screen, the outer two the left and right edges. Every row is then placed from
the marker nearest to it, so nothing is ever assumed about where the panel is, and a small rounding error can't add up
down the panel. The markers also check each other: if two of them aren't as far apart as the row heights say they
should be, the painted buttons can't be trusted to sit on the real ones, and the plain panel takes over instead.

Hover works the same way, with no listener of any kind running (a modal operator would hold back Blender's autosave):
Blender itself lightens the real button the mouse is over, and redraws. Before painting, the painter reads the pixels
under the panel and each painted control looks at its own patch: lighter than a resting button means the mouse is on
it, the pressed colour means it's held down. So the painted hover is always the button a click would really hit.
Changes of state are eased over about a tenth of a second (tween); while one runs, the sidebar is redrawn each frame.

Size: a sidebar can be zoomed (Ctrl + middle-drag, or a pinch), and Blender's interface scale can be anything. Both
scale the real buttons, so one number carries both through layout and painting: `scale(region)` = interface scale x
the sidebar's zoom, in pixels per layout unit. Nothing here may assume it is 1.

When the paint can't be trusted (markers disagreeing, or any error while painting), that sidebar shows the plain panel
instead. Not for good: it tries the drawn panel again as soon as the sidebar's size, zoom or scale changes, and
otherwise every so often.
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
S3 = (0.212, 0.212, 0.231, 1.0)         # the chosen segment, a toggle that's on
S4 = (0.262, 0.262, 0.286, 1.0)         # the mouse over a chosen thing
S2H = (0.196, 0.196, 0.214, 1.0)        # the mouse over a button
LINE = (1.0, 1.0, 1.0, 0.08)
T1 = (0.925, 0.925, 0.941, 1.0)
T2 = (0.651, 0.651, 0.690, 1.0)
T3 = (0.431, 0.431, 0.471, 1.0)
ACC = (0.176, 0.549, 0.941, 1.0)        # FitBridge's one accent: the main action and the chosen thing
ACC_HI = (0.300, 0.640, 0.980, 1.0)     # the mouse over the main action
ACC_LO = (0.130, 0.440, 0.800, 1.0)     # the main action held down
GOOD = (0.310, 0.749, 0.478, 1.0)
WARN = (0.851, 0.643, 0.255, 1.0)
BAD = (0.878, 0.341, 0.357, 1.0)
WHITE = (1.0, 1.0, 1.0, 1.0)

UNIT = 20.0                             # Blender's row height at UI scale 1
PAD = 8.0                               # the buttons' inset from the layout's edges
CARD_L, CARD_R = 8.0, 6.0               # how far the card reaches past the layout's edges (lines up with Tripo Loop's)
MARK_H = 12.0                           # a row that is only a marker
MARK_GAP = 60.0                         # a row without a button carries a marker when the last one is this far up
MARK_TOL = 3.0                          # pixels two markers may disagree by before the painting isn't trusted
SIDE_L, SIDE_R, TABS_W = 10.0, 10.0, 22.0   # a first guess at the layout's edges (the markers give the real ones)

FONT = {"ui": 0, "mono": None}
DRAW = {}                               # region pointer -> {"rows", "marks", "token", "seen", "cache", "strikes"}
FLASH = {}                              # key -> time of the click
LIVE = {"snap": None, "ox": 0, "oy": 0, "theme": None, "x0": 0.0, "top": 0.0, "ui": 1.0, "region": 0, "want": False,
        "timer": False}                 # what the painter knows while it paints one panel
TWEEN = {}                              # (region, key) -> [from, to, start time, value now]
HOVER_S, MOVE_S = 0.09, 0.14            # seconds: a hover fading in or out; something sliding to a new place
STATE = {"broken": False, "handle": None, "icons": None, "ids": [], "side": 0}
_TEX = {}

MARK_MAX = 24
MARK_SIDE_B = 250


def _mark_b(i):
    return 24 + 8 * i


def _mono():
    if FONT["mono"] is None:
        path = os.path.join(bpy.utils.system_resource("DATAFILES", path="fonts") or "", "DejaVuSansMono.woff2")
        FONT["mono"] = blf.load(path) if os.path.isfile(path) else 0
        if FONT["mono"] == -1:
            FONT["mono"] = 0
    return FONT["mono"]


def zoom_of(region):
    """how far this sidebar is zoomed: screen pixels per layout unit (1 = not zoomed)"""
    try:
        v = region.view2d
        z = 1000.0 / (v.region_to_view(0, 1000)[1] - v.region_to_view(0, 0)[1])
        return z if 0.2 < z < 6.0 else 1.0
    except Exception:  # noqa: BLE001 - a region without a view: not zoomed
        return 1.0


def unit_px():
    """screen pixels per layout unit before any zoom. Not simply the interface scale: Blender's row unit is 18 scaled
    pixels plus a line-width border on each side (20 at scale 1, 29 at scale 1.5), and rows are built from it."""
    return row_unit() / 20.0


def scale(region):
    """screen pixels per layout unit in this sidebar: Blender's row unit times the sidebar's own zoom"""
    return unit_px() * zoom_of(region)


def _signature(region):
    return (round(zoom_of(region), 3), region.width, round(unit_px(), 3))


def usable(region=None):
    """can the drawn panel work here, now? Not without a window or marker icons; and not in a sidebar where the paint
    just failed to line up, until that sidebar changes or a while has passed."""
    if bpy.app.background or STATE["broken"] or not STATE["ids"]:
        return False
    d = DRAW.get(region.as_pointer()) if region is not None else None
    return not (d is not None and _plain_now(region, d))


def _plain_now(region, d):
    """is this sidebar showing the plain panel for the moment? (ends when the sidebar changes or the wait is over)"""
    p = d.get("plain")
    if not p:
        return False
    if p["sig"] == _signature(region) and time.time() < p["until"]:
        d["rows"], d["marks"] = [], []              # nothing of the drawn panel is laid out: nothing to paint
        return True
    d["plain"] = None                               # it changed, or enough time has passed: try the drawn panel again
    d["strikes"] = 0
    return False


def _give_up(region, d, why):
    """this sidebar shows the plain panel for now (it comes back by itself, see usable)"""
    tries = d.get("gave_up", 0) + 1
    d["gave_up"] = tries
    d["plain"] = {"sig": _signature(region), "until": time.time() + min(120.0, 10.0 * 2 ** (tries - 1))}
    d["strikes"] = 0
    if tries <= 2:
        print(f"FitBridge: {why}; showing the plain panel in this sidebar until it lines up again")
    redraw()


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


# ------------------------------------------------------------------------------------------------ hover and easing

def mix(a, b, t):
    t = max(0.0, min(1.0, t))
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(4))


def _theme():
    """the resting and the pressed colour of Blender's own widgets, as bytes: what an untouched real button looks like"""
    t = bpy.context.preferences.themes[0].user_interface
    out = {}
    for kind, name in (("tool", "wcol_tool"), ("menu", "wcol_menu"), ("slider", "wcol_numslider")):
        w = getattr(t, name)
        out[kind] = ([int(round(v * 255)) for v in w.inner[:3]], [int(round(v * 255)) for v in w.inner_sel[:3]])
    return out


def hot(x, y, w, h, kind="tool"):
    """the real button under this patch: 0 = at rest, 1 = the mouse is over it, 2 = held down.
    kind = which of Blender's widgets it is: "tool" (a button), "menu" (a pop-up list), "slider"."""
    a = LIVE["snap"]
    if a is None or w < 8 or h < 8:
        return 0
    base, sel = LIVE["theme"][kind]
    H, W = a.shape[:2]
    lit, down = [], 0
    for fy in (0.22, 0.78):                         # above and below any text in the middle
        py = int(y + h * fy) - LIVE["oy"]
        if not 0 <= py < H:
            continue
        for fx in (0.2, 0.5, 0.8):
            px = int(x + w * fx) - LIVE["ox"]
            if not 0 <= px < W:
                continue
            c = a[py, px]
            r, g, bl = int(c[0]), int(c[1]), int(c[2])
            if max(abs(r - sel[0]), abs(g - sel[1]), abs(bl - sel[2])) <= 8:
                down += 1
                continue
            d = (r - base[0], g - base[1], bl - base[2])
            if min(d) >= 5 and max(d) <= 90 and max(d) - min(d) <= 28:      # evenly lighter than at rest
                lit.append((r, g, bl))
    if kind == "slider":                            # part of a slider is its fill, so two patches are enough
        return 1 if len(lit) + down >= 2 else 0
    if down >= 4:
        return 2
    # a lit button is one flat colour all over; anything else lighter (the 3D view showing through a gap) isn't
    if len(lit) >= 5 and max(max(abs(c[i] - lit[0][i]) for i in range(3)) for c in lit) <= 10:
        return 1
    return 0


def _key(x, y, w):
    ui = LIVE.get("ui", 1.0) or 1.0
    return (round((x - LIVE["x0"]) / ui), round((LIVE["top"] - y) / ui), round(w / ui))


def tween(key, target, dur=HOVER_S):
    """the value on its way to `target`, eased; asks for another frame while it's moving"""
    now = time.perf_counter()
    k = (LIVE["region"], key)
    s = TWEEN.get(k)
    if s is None:
        if len(TWEEN) > 600:
            TWEEN.clear()
        TWEEN[k] = [target, target, now, target]    # the first time it's seen, it's simply there
        return target
    if s[1] != target:
        s[0], s[1], s[2] = s[3], target, now
    p = 1.0 if dur <= 0 else min(1.0, (now - s[2]) / dur)
    s[3] = s[0] + (s[1] - s[0]) * (p * p * (3 - 2 * p))
    if p < 1.0:
        LIVE["want"] = True
    return s[3]


def feel(x, y, w, h, kind="tool", enabled=True):
    """(how far the hover has faded in, 0 to 1; whether it's held down) for the control painted at this patch"""
    s = hot(x, y, w, h, kind) if enabled else 0
    return tween(("hover",) + _key(x, y, w), 1.0 if s else 0.0), s == 2


def _next_frame():
    LIVE["timer"] = False
    try:
        redraw()
    except Exception:  # noqa: BLE001 - a file being loaded: there's nothing to redraw
        pass
    return None


# ------------------------------------------------------------------------------------------------ widgets

def button(x, y, w, h, label, ui, key=None, primary=False, enabled=True, right=None, on=False):
    """primary = the accent fill; otherwise a raised graphite surface with a hairline. `on` = a toggle that's on."""
    t, down = feel(x, y, w, h, enabled=enabled)
    down = down or (flashing(key) if key else False)
    if primary and enabled:
        box(x, y, w, h, 10 * ui, ACC_LO if down else mix(ACC, ACC_HI, t))
        tc, rc = WHITE, (1, 1, 1, 0.75)
    elif primary:
        box(x, y, w, h, 10 * ui, S1, LINE)
        tc, rc = T3, T3
    else:
        rest, lit = (S3, S4) if on else (S2, S2H)
        box(x, y, w, h, 8 * ui, S1 if down else mix(rest, lit, t), mix(LINE, (1, 1, 1, 0.16), t))
        tc, rc = (mix(T1, WHITE, t) if enabled else T3), T3
    size = 12
    dy = -1 * ui if down else 0                     # held down, the label sits a pixel lower
    if right:
        text(x + 14 * ui, mid_y(y, h, size, ui) + dy, label, size, tc, ui,
             maxw=w - 28 * ui - text_w(right, 11, ui) - 8 * ui)
        text(x + w - 14 * ui, mid_y(y, h, 11, ui) + dy, right, 11, rc, ui, align="right")
    else:
        text(x + w / 2, mid_y(y, h, size, ui) + dy, label, size, tc, ui, align="center", maxw=w - 12 * ui)


def checkbox(x, y, h, label, on, ui, col=T2, w=None):
    """a tick box and its label (w = the row's width, so the whole row answers to the mouse)"""
    t, down = feel(x, y, w, h) if w else (0.0, False)
    s = 14 * ui
    by = y + h / 2 - s / 2
    f = tween(("tick",) + _key(x, y, s), 1.0 if on else 0.0, MOVE_S)
    box(x, by, s, s, 4 * ui, S1, mix((1, 1, 1, 0.18), (1, 1, 1, 0.42), t))
    if f > 0.01:
        k = s * (0.6 + 0.4 * f)                     # the fill grows from the middle
        box(x + (s - k) / 2, by + (s - k) / 2, k, k, 4 * ui, mix(S1, ACC_LO if down else ACC, f))
    if f > 0.5:
        g_check(x, by, s, WHITE, ui)
    text(x + s + 8 * ui, mid_y(y, h, 12, ui), label, 12, mix(col, T1, t), ui,
         maxw=(w - s - 8 * ui) if w else None)


def switch(x, y, h, on, ui, t=0.0, key=None):
    """a small on/off switch, right-aligned at x (t = how far a hover has faded in)"""
    w, s = 26 * ui, 14 * ui
    by = y + h / 2 - s / 2
    f = tween(("switch",) + (key or _key(x - w, y, w)), 1.0 if on else 0.0, MOVE_S)
    box(x - w, by, w, s, s / 2, mix(mix(S3, S4, t), mix(ACC, ACC_HI, t), f))
    k = s - 4 * ui
    left, right = x - w + 2 * ui, x - k - 2 * ui
    box(left + (right - left) * f, by + 2 * ui, k, k, k / 2, mix(mix(T2, T1, t), WHITE, f))


def segmented(x, y, w, h, items, active, ui, key_prefix="", tabs=False):
    """a segmented control: a graphite track, the chosen segment a surface step that slides to where it's put. Only the
    panel's own tabs get the accent underline, so a choice inside a tab never reads as a second tab bar."""
    box(x, y, w, h, 8 * ui, S1, LINE)
    sw = (w - 4 * ui) / len(items)
    vals = [v for v, _l in items]
    feels = [feel(x + 2 * ui + i * sw, y, sw, h) for i in range(len(items))]
    for i, (t, down) in enumerate(feels):           # the mouse over one that isn't chosen
        lit = down or flashing(f"{key_prefix}{vals[i]}")
        if vals[i] != active and (t > 0.01 or lit):
            box(x + 2 * ui + i * sw, y + 2 * ui, sw, h - 4 * ui, 6 * ui, mix(S1, S2H, 1.0 if lit else t))
    if active in vals:
        at = vals.index(active)
        pos = tween(("seg",) + _key(x, y, w), float(at), MOVE_S)
        sx = x + 2 * ui + pos * sw
        box(sx, y + 2 * ui, sw, h - 4 * ui, 6 * ui, mix(S3, S4, feels[at][0]))
        if tabs:
            box(sx + sw * 0.32, y + 3 * ui, sw * 0.36, 2 * ui, 1 * ui, ACC)
    for i, (val, label) in enumerate(items):
        text(x + 2 * ui + i * sw + sw / 2, mid_y(y, h, 12, ui), label, 12,
             T1 if val == active else mix(T2, T1, feels[i][0]), ui, align="center", maxw=sw - 6 * ui)


def chips(x, y, w, h, items, active, ui):
    """small equal buttons in a row (the chosen one stepped up)"""
    gap = 4 * ui
    cw = (w - gap * (len(items) - 1)) / len(items)
    for i, (val, label) in enumerate(items):
        cx = x + i * (cw + gap)
        on = val == active
        t, down = feel(cx, y, cw, h)
        rest, lit = (S3, S4) if on else (S2, S2H)
        box(cx, y, cw, h, 8 * ui, S1 if down else mix(rest, lit, t), mix(LINE, (1, 1, 1, 0.16), t))
        text(cx + cw / 2, mid_y(y, h, 12, ui) - (1 * ui if down else 0), label, 12, T1 if on else mix(T2, T1, t), ui,
             align="center", maxw=cw - 4 * ui)


def pill(x, y, h, label, ui, align="left", key=None, enabled=True):
    """a small outlined action (the one look for every text-sized button). Returns its width."""
    w = text_w(label, 11, ui) + 20 * ui
    ph = 20 * ui
    px = x - w if align == "right" else x
    py = y + h / 2 - ph / 2
    t, down = feel(px, py, w, ph, enabled=enabled)
    down = down or bool(key and flashing(key))
    box(px, py, w, ph, 6 * ui, S1 if down else mix(S2, S4, t), mix(LINE, (1, 1, 1, 0.18), t))
    text(px + w / 2, mid_y(y, h, 11, ui) - (1 * ui if down else 0), label, 11, T1 if enabled else T3, ui,
         align="center")
    return w


def pill_frac(label, width, ui, inset=0.0):
    """the share of a layout row `width` pixels wide that the real button under a pill with this label takes, the
    pill sitting `inset` in from the painted area's end"""
    return max(0.05, min(0.9, (text_w(label, 11, ui) + (24 + PAD + inset) * ui) / max(width, 1.0)))


def section(x, y, w, h, label, ui, link=None, note=None, key=None):
    text(x, y + 6 * ui, label, 11, T3, ui)
    if link:
        pill(x + w, y + 2 * ui, h - 4 * ui, link, ui, align="right", key=key)
    elif note:
        text(x + w, y + 6 * ui, note, 11, T3, ui, align="right")


def switch_row(x, y, w, h, label, on, ui, col=T2):
    t, _down = feel(x, y, w, h)
    text(x, mid_y(y, h, 12, ui), label, 12, mix(col, T1, t), ui, maxw=w - 34 * ui)
    switch(x + w, y, h, on, ui, t, key=_key(x, y, w))


def glow(x, y, w, h, ui, kind="tool", r=6.0, inset=2.0):
    """a faint lit band behind a row the mouse is over (rows of a list, a line that folds); returns how lit it is"""
    t, down = feel(x, y, w, h, kind)
    if t > 0.01 or down:
        box(x + inset * ui, y + 1 * ui, w - 2 * inset * ui, h - 2 * ui, r * ui,
            (1, 1, 1, 0.075 if down else 0.045 * t))
    return t


def field(x, y, w, h, label, value, ui, kind="list", dim=False, first=False, last=False):
    """one row of a grouped list: label on the left, its value on the right, hairline under it"""
    t = glow(x, y, w, h, ui, "menu" if kind == "list" else "tool")
    text(x + 12 * ui, mid_y(y, h, 12, ui), label, 12, mix(T2, T1, t), ui)
    lw = text_w(label, 12, ui)
    rx = x + w - 12 * ui
    if kind == "list":
        g_updown(rx - 12 * ui, y + h / 2 - 7 * ui, 14 * ui, mix(T3, T1, t), ui)
        rx -= 18 * ui
    text(rx, mid_y(y, h, 12, ui), value, 12, mix(T3, T2, t) if dim else T1, ui, align="right",
         maxw=w - lw - 60 * ui)
    if not last:
        hair(x + 12 * ui, y, w - 12 * ui, ui)


def slider(x, y, w, h, label, value, frac, ui, centred=False):
    """label, the figure, and a thin track under them (centred = the fill grows from the middle)"""
    t, _down = feel(x, y, w, h, "slider")
    text(x, y + h - 15 * ui, label, 12, mix(T2, T1, t), ui)
    text(x + w, y + h - 15 * ui, value, 12, T1, ui, align="right", mono=True)
    ty = y + 5 * ui
    box(x, ty, w, 3 * ui, 1.5 * ui, mix(S3, S4, t))
    frac = max(0.0, min(1.0, frac))
    fill = mix(T2, ACC, t)                          # the mouse on it: the fill takes the accent
    if centred:
        a, b = sorted((0.5, frac))
        box(x + w * a, ty, max(2 * ui, w * (b - a)), 3 * ui, 1.5 * ui, fill)
        rect(x + w / 2 - 0.5 * ui, ty - 2 * ui, 1 * ui, 7 * ui, T3)
    else:
        box(x, ty, max(2 * ui, w * frac), 3 * ui, 1.5 * ui, fill)
    k = (9 + 3 * t) * ui                            # and the knob grows a little
    box(x + w * frac - k / 2, ty + 1.5 * ui - k / 2, k, k, k / 2, mix(T1, WHITE, t))


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

    `width` is how wide the text area will be in pixels, for wrapping. A row's real buttons span the whole layout
    width; the painter draws PAD inside that, so a real button is never smaller than the painted one. (An empty
    spacer cell can't hold the buttons in: Blender gives it at least 18 pixels whatever the split says.)"""

    def __init__(self, layout, context):
        self.col = layout.column(align=True)
        self.items = []
        self.marks = []                      # (marker index, which row it is, the row's height)
        self.h = 0.0
        self._marked = -1e9                  # the height at which the last marker sits
        base = unit_px()
        self.ui = scale(context.region) if context.region else base                # pixels per layout unit
        reg_w = context.region.width if context.region else 300
        # the layout's width in pixels (a guess for wrapping text; the painter measures the real edges). The margins
        # zoom with the sidebar, the strip of tabs doesn't.
        self.full = max(120.0, reg_w - (SIDE_L + SIDE_R) * self.ui - TABS_W * base)
        self.width = self.full - 2 * PAD * self.ui - 4 * self.ui                    # safe text width, px

    def add(self, h, real=None, paint=None, raw=False, mark=False):
        r = self.col.row(align=True)
        if not raw:
            r.scale_y = h / UNIT
        if real is None:
            i = len(self.marks)
            if (mark or self.h - self._marked >= MARK_GAP) and not raw and 12 <= h <= 36 and i < MARK_MAX \
                    and STATE["ids"]:
                # the row's height comes from scale_y (measured: an icon scaled by itself makes a 20 px row)
                a = r.split(factor=0.25, align=True)
                a.template_icon(icon_value=STATE["side"], scale=1.0)
                b = a.split(factor=2 / 3, align=True)
                b.template_icon(icon_value=STATE["ids"][i], scale=1.0)
                b.template_icon(icon_value=STATE["side"], scale=1.0)
                self.marks.append((i, len(self.items), float(h)))
                self._marked = self.h
            else:
                r.label(text="")
        else:
            real(r)
        self.items.append((h, paint))
        self.h += h

    def gap(self, h):
        self.add(h)

    def mark(self):
        """a row that is only a marker (the panel starts and ends with one)"""
        self.add(MARK_H, mark=True)

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
        d = DRAW.setdefault(key, {"token": 0, "seen": -1, "cache": None, "strikes": 0})
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


def row_unit():
    """Blender's row unit in whole pixels: 18 scaled pixels plus a line-width border on each side (20 at scale 1)"""
    s = bpy.context.preferences.system
    return int(math.floor(18.0 * (s.ui_scale or 1.0) + 0.5)) + 2 * int(s.pixel_size or 1)


def row_px(h, unit):
    """a row of `h` layout units in pixels, before zoom: Blender cuts every row down to whole pixels (measured at
    scales 0.75, 1.1, 1.25 and 1.5: only this rule puts every marker within a pixel or two)"""
    return float(math.floor(h * unit / 20.0 + 1e-6))


def _edges(d, zoom):
    """the top of every row below the panel's first, in pixels at this zoom (edges[k] = rows above row k)"""
    unit = row_unit()
    out, total = [0.0], 0.0
    for h, _p in d["rows"]:
        total += row_px(h, unit) * zoom
        out.append(total)
    return out


def _find(region, d, ui, edges):
    """(x0, x1, [(row index, top y on screen), ...]) from every marker in view; None when there is none"""
    W, H = region.width, region.height
    base = unit_px()
    cx = (SIDE_L * ui + W - SIDE_R * ui - TABS_W * base) / 2     # (tabs on the other side move it 22 px: still in the strip)
    sw = int(max(64 * ui, 72 * base))
    sx = int(max(0, min(W - sw, cx - sw / 2)))
    fb = gpu.state.active_framebuffer_get()
    a = _read(fb, sx, 0, sw, H)
    m = _is_mark(a)
    if not m.any():
        return None
    b = a[..., 2].astype(np.int16)
    tops, lines = [], []
    for i, row, _h in d["marks"]:
        if row + 1 >= len(edges):
            continue
        rows = np.nonzero((m & (np.abs(b - _mark_b(i)) <= 3)).any(axis=1))[0]
        if not len(rows):
            continue
        y0, y1 = int(rows[0]), int(rows[-1])
        hp = edges[row + 1] - edges[row]
        # the icon is its row less an edge of about 6.5 px that doesn't zoom (measured: zoom 0.79 to 1.36, scale 0.75 to 1.5)
        if abs((y1 - y0 + 1) - (hp - 6.5 * base)) > 2.5 * max(1.0, base) or y0 == 0 or y1 == H - 1:
            continue                                # cut off by the region's edge, or partly covered
        cy = (y0 + y1 + 1) / 2
        tops.append((row, cy + hp / 2))
        lines.append(int(cy))
    for y in lines:                                 # the edges, from the first marker whose outer icons both show
        line = _read(fb, 0, y, W, 1)[0]
        side = np.nonzero(_is_mark(line) & (np.abs(line[..., 2].astype(np.int16) - MARK_SIDE_B) <= 3))[0]
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
        return x0, x0 + width, tops
    return None


def _agree(tops, edges):
    """the most two neighbouring markers disagree by, in pixels (0 when there's only one): are they as far apart on
    screen as the rows between them are tall?"""
    worst = 0.0
    for (r0, t0), (r1, t1) in zip(tops, tops[1:]):
        worst = max(worst, abs((t0 - t1) - (edges[r1] - edges[r0])))
    return worst


def _paint():
    if STATE["broken"]:
        return
    region = bpy.context.region
    d = DRAW.get(region.as_pointer()) if region is not None else None
    if d is None:
        return
    # Painted on every redraw of this sidebar, not only after a new layout: when the mouse moves onto a button Blender
    # may redraw without laying the panels out again. The markers say whether the panel is on show at all.
    d["seen"] = d["token"]
    try:
        if not d.get("rows"):
            return                                  # the plain panel is on show in this sidebar
        zoom = zoom_of(region)
        ui = unit_px() * zoom
        edges = _edges(d, zoom)
        scroll = region.view2d.view_to_region(0, 0, clip=False)[1]
        got = _find(region, d, ui, edges)
        now = time.time()
        if got is not None:
            x0, x1, tops = got
            off = _agree(tops, edges)
            d["agree"] = off
            if off > MARK_TOL + 0.01 * edges[-1]:
                d["strikes"] = d.get("strikes", 0) + 1
                if d["strikes"] >= 3:               # the rows aren't where their heights say: don't paint lies
                    _give_up(region, d, f"the drawn panel doesn't line up with its buttons ({off:.1f} px off)")
                    return
            else:
                d["strikes"] = 0
                d["gave_up"] = 0
            d["cache"] = (x0, x1, [(r, t - scroll) for r, t in tops], now)
        elif d.get("cache") is not None and now - d["cache"][3] < 1.0:
            # nothing in view for a moment (something passing over it): where it just was, moved with the scrolling
            x0, x1 = d["cache"][0], d["cache"][1]
            tops = [(r, t + scroll) for r, t in d["cache"][2] if r < len(edges)]
            if not tops:
                return
        else:
            return
        bare = os.environ.get("FITBRIDGE_UI_BARE")      # for checking the real buttons under the paint
        if bare == "1":
            return
        ys, hs = [], []                                 # every row from the marker nearest to it
        for k in range(len(d["rows"])):
            r0, t0 = min(tops, key=lambda m: abs(edges[m[0]] - edges[k]))
            hs.append(edges[k + 1] - edges[k])
            ys.append(t0 - (edges[k + 1] - edges[r0]))
        if not ys:
            return
        # what Blender drew under the panel, read before it's painted over: hot() finds the hovered button in it
        top = ys[0] + hs[0]
        sx, sy = max(0, int(x0)), max(0, int(ys[-1]))
        sw, sh = min(region.width, int(x1) + 1) - sx, min(region.height, int(top) + 1) - sy
        LIVE.update(snap=None, ox=sx, oy=sy, x0=x0, top=top, ui=ui, region=region.as_pointer(), want=False)
        if sw > 8 and sh > 8:
            try:
                LIVE["snap"] = _read(gpu.state.active_framebuffer_get(), sx, sy, sw, sh)
                LIVE["theme"] = _theme()
            except Exception:  # noqa: BLE001 - no hover then; everything else still works
                LIVE["snap"] = None
        gpu.state.blend_set("ALPHA")
        pad = PAD * ui
        if bare:                                        # outlines only: do the rows sit on the real buttons?
            for y, hh in zip(ys, hs):
                lines([(x0 + pad, y), (x1 - pad, y), (x1 - pad, y + hh), (x0 + pad, y + hh)],
                      (1.0, 0.9, 0.1, 0.9), 1.0, "LINE_LOOP")
            gpu.state.blend_set("NONE")
            return
        box(x0 - CARD_L * ui, ys[-1], (x1 - x0) + (CARD_L + CARD_R) * ui, top - ys[-1], 12 * ui, BG, LINE)
        for (_h, paint), y, hh in zip(d["rows"], ys, hs):
            if paint is not None and y < region.height and y + hh > 0:
                paint(x0 + pad, y, (x1 - x0) - 2 * pad, hh, ui)
        gpu.state.blend_set("NONE")
        LIVE["snap"] = None
        if LIVE["want"] and not LIVE["timer"]:          # something is still easing: one more frame
            LIVE["timer"] = True
            bpy.app.timers.register(_next_frame, first_interval=1 / 60)
    except ReferenceError:
        gpu.state.blend_set("NONE")                 # something in the file went away mid-frame: the next layout has it right
    except Exception as e:  # noqa: BLE001 - never repeat a drawing error on every redraw: the plain panel takes over
        gpu.state.blend_set("NONE")
        _give_up(region, d, f"the drawn panel hit an error ({e!r})")


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
    TWEEN.clear()
    LIVE.update(snap=None, timer=False, want=False)
    if bpy.app.timers.is_registered(_next_frame):
        bpy.app.timers.unregister(_next_frame)
    _TEX.clear()
