"""FitBridge's panel, drawn by hand (ui.py is the kit and explains how the two layers work).

One card: a header that folds it away, then Fit / Thumbnail / Listing, and Send at the bottom. Every control is a real
Blender button or field under the paint, so clicks, drags, pop-up lists and tooltips are Blender's own.

The look (unslop direction, 2026-11-10): a fitting room's measuring tools. Graphite surfaces like Blender's own,
hairlines, figures in mono, Roblox Studio's blue only on the main action and the chosen thing. The two rich moments
are the live thumbnail and the fit gauge, FitBridge's signature: per axis, where the item sits inside the box Roblox
allows, and what sticks out. Type is four sizes: 14 title, 12 everything you read or press, 11 labels and help,
10.5 mono counters. Switches are for settings, tick boxes only for pick-several lists.
"""

import math
import time
import webbrowser

import bpy
from bpy.props import IntProperty, StringProperty

from . import auto, batch, draw, evaluate, host, listing, rig, server, spec, thumb, upload
from . import ui as kit
from .ui import ACC, BAD, GOOD, LINE, S1, S2, S3, T1, T2, T3, WARN, mid_y

STATUS = {"pass": "pass", "warn": "warn", "fail": "bad"}
AXES = ("Width", "Height", "Depth")
ENDS = (("the left", "the right"), ("the bottom", "the top"), ("the front", "the back"))
SITE = "Paradyse"                            # what the panel calls the Studio website ("Studio" alone = Roblox Studio)
SHARED = {"status": None}                    # __init__ hands over its last-sent / Studio-result notes here


def _st(context):
    return context.scene.fitbridge


def op(layout, idname, **props):
    """a real button (it sits under the paint, so its own look never shows)"""
    o = layout.operator(idname, text=" ")
    for k, v in props.items():
        setattr(o, k, v)
    return o


def setter(layout, where, prop, value="!"):
    return op(layout, "fitbridge.ui_set", where=where, prop=prop, value=value)


def inner(rows):
    """the painted area's width in pixels"""
    return max(120.0, rows.full - 2 * kit.PAD * rows.ui)


def section_link(rows, label, link, idname, key=None, **props):
    """a section label with one small action at its right end"""
    f = kit.pill_frac(link, rows.full, rows.ui)

    def real(r):
        a, b = kit.split(r, 1 - f, f)
        a.label(text="")
        op(b, idname, **props)
    rows.add(24, real=real, paint=lambda x, y, w, h, ui: kit.section(x, y, w, h, label, ui, link=link, key=key))


def switch(rows, label, where, prop, on, col=T2, h=28):
    rows.add(h, real=lambda r: setter(r, where, prop),
             paint=lambda x, y, w, hh, ui: kit.switch_row(x, y, w, hh, label, on, ui, col))


# ------------------------------------------------------------------------------------------------------- the panel

def build(rows, context):
    st = _st(context)
    p = host.prefs()
    res = evaluate.LIVE["result"]
    rows.gap(4)
    rows.mark()

    # --- header: mark, name, the link to Roblox Studio, fold
    running = server.running()
    if not running:
        link, short, link_col = "Bridge off", "Bridge off", BAD
    elif server.state["protocol_mismatch"] is not None:
        link, short, link_col = "Update the Studio plugin", "Update plugin", WARN
    elif server.studio_connected():
        link, short, link_col = "Roblox Studio connected", "Connected", GOOD
    else:
        link, short, link_col = "Roblox Studio not connected", "Not connected", T3

    def p_head(x, y, w, h, ui):
        t, _down = kit.feel(x, y, w, h)
        kit.g_mark(x, y + h / 2 - 12 * ui, 24 * ui, ui)
        kit.text(x + 36 * ui, mid_y(y, h, 14, ui), "FitBridge", 14, T1, ui)
        kit.g_chev(x + w - 14 * ui, y + h / 2 - 7 * ui, 14 * ui, kit.mix(T3, T1, t), ui,
                   "right" if st.ui_collapsed else "down")
        room = w - 36 * ui - kit.text_w("FitBridge", 14, ui) - 52 * ui
        say = next((t for t in (link, short) if kit.text_w(t, 11, ui) + 12 * ui <= room), "")
        tw = kit.text(x + w - 24 * ui, mid_y(y, h, 11, ui), say, 11, T2, ui, align="right") if say else -6 * ui
        kit.box(x + w - 24 * ui - tw - 12 * ui, y + h / 2 - 3 * ui, 6 * ui, 6 * ui, 3 * ui, link_col)
    rows.add(36, real=lambda r: setter(r, "scene", "ui_collapsed"), paint=p_head)
    if st.ui_collapsed:
        rows.mark()
        return

    if not running:
        rows.gap(4)
        rows.add(28, real=lambda r: op(r, "fitbridge.restart_server"),
                 paint=lambda x, y, w, h, ui: kit.button(x, y, w, h, "Restart the bridge", ui))
    if abs(context.scene.unit_settings.scale_length - 1.0) > 1e-6:
        rows.gap(4)
        rows.status("The scene's unit scale isn't 1.0, and FitBridge reads 1 unit as 1 stud", "warn")

    # --- Auto: one switch, and its second one
    if p is not None:
        rows.gap(4)
        switch(rows, "Let Claude set it up", "prefs", "auto_mode", p.auto_mode)
        if p.auto_mode:
            switch(rows, "Also check how it sits on R6", "prefs", "auto_fit_check", p.auto_fit_check)

    # --- tabs
    is_auto = auto.enabled()
    tabs = [("FIT", "Summary")] if is_auto else [("FIT", "Fit"), ("THUMB", "Thumbnail")]
    if listing.available():
        tabs.append(("LISTING", "Listing"))
    tab = st.ui_tab if st.ui_tab in [t for t, _l in tabs] else "FIT"
    rows.gap(8)
    if len(tabs) > 1:
        def n_tabs(r):
            for col, (val, _label) in zip(kit.equal(r, len(tabs)), tabs):
                setter(col, "scene", "ui_tab", val)
        rows.add(32, real=n_tabs, paint=lambda x, y, w, h, ui: kit.segmented(x, y, w, h, tabs, tab, ui, "ui_tab",
                                                                            tabs=True))
        rows.mark()
    else:
        rows.gap(4)

    done = True
    if tab == "LISTING":
        done = build_listing(rows, context, st)
    elif is_auto:
        build_auto(rows, context, res)
    elif tab == "THUMB":
        build_thumb(rows, context, st, p, res)
    else:
        build_fit(rows, context, st, res)

    if not done:
        rows.mark()                                           # one main button at a time: Send comes back once it's written
        return
    rows.mark()
    build_send(rows, context, st, res, is_auto)
    rows.mark()                                               # the panel ends with a marker, as it starts with one


# ------------------------------------------------------------------------------------------------------------- Fit

def build_fit(rows, context, st, res):
    problem = evaluate.selection_problem(context)
    judged = not problem and res is not None and res.get("fit") is not None
    if judged:
        build_verdict(rows, st, res)
        rows.mark()
    build_setup(rows, context, st, res, problem)


def build_verdict(rows, st, res):
    """the answer first: does it fit, the gauge, and whatever needs a look"""
    checks = res["checks"]
    n = len(checks)
    fails = [c for c in checks if c["status"] == "fail"]
    warns = [c for c in checks if c["status"] == "warn"]
    if fails:
        line, kind = f"{len(fails)} check{'s' if len(fails) != 1 else ''} to fix", "bad"
    elif warns:
        line, kind = f"It fits, with {len(warns)} thing{'s' if len(warns) != 1 else ''} to look at", "warn"
    else:
        line, kind = f"It fits, and all {n} checks pass", "good"
    rows.add(28, paint=lambda x, y, w, h, ui: kit.status_line(x, y, w, h, line, kind, ui))
    build_gauge(rows, res)
    rows.gap(8)
    for c in fails + warns:
        if c["id"] == "watertight":
            open_edges_row(rows, c)
        else:
            rows.status(c["message"], STATUS[c["status"]])
    passing = [c for c in checks if c["status"] == "pass"]
    if passing:
        if st.ui_checks_open:
            for c in passing:
                rows.status(c["message"], "pass")
        label = "Hide the passing checks" if st.ui_checks_open else (
            f"See all {n} checks" if not (fails or warns) else f"{len(passing)} more pass")

        def p_fold(x, y, w, h, ui):
            t = kit.glow(x, y, w, h, ui)
            kit.text(x + 20 * ui, mid_y(y, h, 11, ui), label, 11, kit.mix(T3, T1, t), ui)
            kit.g_chev(x + w - 14 * ui, y + h / 2 - 7 * ui, 14 * ui, kit.mix(T3, T1, t), ui,
                       "up" if st.ui_checks_open else "down")
        rows.add(24, real=lambda r: setter(r, "scene", "ui_checks_open"), paint=p_fold)


def open_edges_row(rows, c):
    """the open-edges warning, with a button that shows them: Edit Mode, edge select, every hole's rim selected"""
    f = kit.pill_frac("Select them", rows.full, rows.ui)

    def real(r):
        a, b = kit.split(r, 1 - f, f)
        a.label(text="")
        op(b, "fitbridge.select_open_edges")

    def paint(x, y, w, h, ui):
        bw = kit.pill(x + w, y, h, "Select them", ui, align="right")
        kit.status_line(x, y, w - bw - 8 * ui, h, c["message"], STATUS[c["status"]], ui)
    rows.add(24, real=real, paint=paint)


def build_gauge(rows, res):
    """the fit gauge: per axis, where the item sits in the allowed box (the track), and what sticks out (red)"""
    t = res.get("fit")
    bounds = res.get("bounds")
    if t is None or bounds is None:
        return
    bs, boff = bounds
    s0 = rows.n()
    rows.gap(4)
    for a in range(3):
        size, off, lim, mid = float(t["size"][a]), float(t["offset"][a]), float(bs[a]), float(boff[a])
        lo = (off - size / 2 - (mid - lim / 2)) / lim if lim > 0 else 0.0
        hi = (off + size / 2 - (mid - lim / 2)) / lim if lim > 0 else 1.0
        past = float(t["over"][a])
        end = ENDS[a][1 if int(t["over_side"][a]) > 0 else 0]

        def p_axis(x, y, w, h, ui, a=a, lo=lo, hi=hi, size=size, lim=lim, past=past, end=end):
            x, w = x + 12 * ui, w - 24 * ui
            ty = y + h - 16 * ui
            lw = kit.text(x, ty, AXES[a], 12, T2, ui)
            rw = kit.text(x + w, ty, f" / {lim:.2f}", 12, T3, ui, align="right", mono=True)
            rw += kit.text(x + w - rw, ty, f"{size:.2f}", 12, BAD if size > lim + 1e-6 else T1, ui, align="right",
                           mono=True)
            if past > 0:
                note = f"{past:.2f} past {end}"
                if kit.text_w(note, 11, ui) > w - lw - rw - 20 * ui:
                    note = f"+{past:.2f}"
                kit.text(x + lw + 10 * ui, ty, note, 11, BAD, ui)
            m = w * 0.12                                      # room at each end for what sticks out
            ax, aw = x + m, w - 2 * m
            cy = y + 8 * ui
            kit.box(ax, cy - 1.5 * ui, aw, 3 * ui, 1.5 * ui, S3)                   # the allowed box
            for ex in (ax, ax + aw):
                kit.rect(ex - 0.5 * ui, cy - 5 * ui, 1 * ui, 10 * ui, T3)
            c0 = kit.tween(("gauge", a, 0), max(-0.14, min(1.14, lo)), kit.MOVE_S)
            c1 = kit.tween(("gauge", a, 1), max(-0.14, min(1.14, hi)), kit.MOVE_S)
            x0, x1 = ax + aw * c0, ax + aw * c1
            if x1 - x0 < 3 * ui:
                x1 = x0 + 3 * ui
            if c0 < 0:
                kit.box(x0, cy - 3 * ui, min(ax, x1) - x0, 6 * ui, 2 * ui, BAD)
            if c1 > 1:
                kit.box(max(ax + aw, x0), cy - 3 * ui, x1 - max(ax + aw, x0), 6 * ui, 2 * ui, BAD)
            i0, i1 = max(x0, ax), min(x1, ax + aw)
            if i1 > i0:
                kit.box(i0, cy - 3 * ui, i1 - i0, 6 * ui, 2 * ui, T1)
        rows.add(36, paint=p_axis)
    rows.gap(8)
    rows.card(s0)


def build_setup(rows, context, st, res, problem):
    cur = rig.shown()
    rows.add(24, paint=lambda x, y, w, h, ui: kit.section(x, y, w, h, "Mannequin", ui))

    def n_rig(r):
        a, b = kit.equal(r, 2)
        op(a, "fitbridge.show_rig", kind="R15")
        op(b, "fitbridge.show_rig", kind="R6")
    rows.add(28, real=n_rig, paint=lambda x, y, w, h, ui: kit.segmented(
        x, y, w, h, (("R15", "R15"), ("R6", "R6 preview")), cur, ui))
    key = "R6" if cur == "R6" else st.body_scale
    if cur == "R15" and len(spec.OFFERED_SCALES) > 1:
        rows.gap(4)
        s0 = rows.n()
        rows.add(32, real=lambda r: r.prop(st, "body_scale", text=""),
                 paint=lambda x, y, w, h, ui: kit.field(x, y, w, h, "Body scale", st.body_scale, ui, last=True))
        rows.card(s0)
    if cur and spec.rig_outdated(key):
        rows.gap(4)
        rows.text("Open Roblox Studio with the FitBridge plugin for the newer mannequin" if spec.cached_rig(key)
                  else "Plain blocks until Roblox Studio connects with the FitBridge plugin")

    rows.gap(12)
    rows.add(24, paint=lambda x, y, w, h, ui: kit.section(x, y, w, h, "Accessory", ui))
    sel = evaluate.selected_meshes(context)
    ob = sel[0] if sel else st.target
    att = dict(spec.attachments_for(st.acc_type)).get(st.attachment, st.attachment)
    s0 = rows.n()

    def p_mesh(x, y, w, h, ui):
        kit.text(x + 12 * ui, mid_y(y, h, 12, ui), problem or (ob.name if ob else ""), 12, BAD if problem else T1, ui,
                 maxw=w - 24 * ui)
        kit.hair(x + 12 * ui, y, w - 12 * ui, ui)
    rows.add(32, paint=p_mesh)
    rows.add(32, real=lambda r: r.prop(st, "acc_type", text=""),
             paint=lambda x, y, w, h, ui: kit.field(x, y, w, h, "Type", st.acc_type, ui))
    rows.add(32, real=lambda r: r.prop(st, "attachment", text=""),
             paint=lambda x, y, w, h, ui: kit.field(x, y, w, h, "Sits on", att, ui))
    rows.add(32, real=lambda r: op(r, "fitbridge.edit_text", target="name"),
             paint=lambda x, y, w, h, ui: kit.field(x, y, w, h, "Name", st.item_name or "Same as the mesh", ui,
                                                    kind="text", dim=not st.item_name, last=True))
    rows.card(s0)
    rows.gap(4)
    switch(rows, "Show the allowed box", "scene", "show_box", st.show_box)
    if not cur:
        rows.text("Show a mannequin to start")
    elif not draw.ready(context):
        rows.text("Select your accessory to see the box")


# ------------------------------------------------------------------------------------------------------- Thumbnail

TILE = 148.0


def p_tile(context, x, y, s, ui, empty="Fit a mesh to see its thumbnail", button=False):
    """the thumbnail as it stands, square, drawn live (button = a click on it renders it full size)"""
    t, _down = kit.feel(x, y, s, s, enabled=button)
    kit.box(x, y, s, s, 10 * ui, S1, kit.mix(LINE, (1, 1, 1, 0.3), t))
    shown = False
    if draw._live_ready(context) and not draw._live["broken"]:
        try:
            # (the same buffer the viewport's own live thumbnail uses, at its size: a zoomed sidebar must not resize it)
            draw._render_live(context, int(draw.LIVE_SIZE * (context.preferences.system.ui_scale or 1.0)))
            kit.picture(draw._live["offscreen"].texture_color, x + 1, y + 1, s - 2, s - 2)
            kit.corners(x + 1, y + 1, s - 2, s - 2, 9 * ui, kit.BG)
            if draw.centre_dot_on(context):
                draw._dot(x + s / 2, y + s / 2, ui)
            shown = True
        except ReferenceError:
            pass
        except Exception as e:  # noqa: BLE001 - a broken picture mustn't take the panel with it
            draw._live["broken"] = True
            print(f"FitBridge: live thumbnail off: {e}")
    if not shown:
        for i, part in enumerate(kit.wrap(empty, 11, s - 24 * ui, ui)):
            kit.text(x + s / 2, y + s / 2 + (4 - 16 * i) * ui, part, 11, T3, ui, align="center")
    return shown


def build_thumb(rows, context, st, p, res):
    g, gob = thumb.owner(context)
    can_render = evaluate.selection_problem(context) is None     # (the Preview operator's own rule)
    width = inner(rows)
    tile = min(TILE, max(96.0, (width / rows.ui) * 0.5))
    tile -= tile % 4
    frac = min(0.7, (kit.PAD + tile + 6) * rows.ui / rows.full)      # the real split: midway in the painted gap
    side = []
    if p is not None:
        side = [(("Show in the 3D view", "3D view"), "prefs", "live_preview", p.live_preview),
                (("Centre dot", "Centre"), "prefs", "centre_dot", p.centre_dot)]
    rh = 36.0

    def n_top(r):
        sp = r.split(factor=frac, align=True)
        a = sp.column(align=True)
        a.scale_y = tile / kit.UNIT
        op(a, "fitbridge.thumb_preview")
        col = sp.column(align=True)
        for _label, where, prop, _on in side:
            row = col.row(align=True)
            row.scale_y = rh / kit.UNIT
            setter(row, where, prop)
        row = col.row(align=True)
        row.scale_y = rh / kit.UNIT
        op(row, "fitbridge.live_window")
        if tile > rh * (len(side) + 1):
            row = col.row(align=True)
            row.scale_y = (tile - rh * (len(side) + 1)) / kit.UNIT
            row.label(text="")

    def p_top(x, y, w, h, ui):
        s = tile * ui
        p_tile(context, x, y + h - s, s, ui, button=can_render)
        rx, rw = x + s + 12 * ui, w - s - 12 * ui
        r = rh * ui
        roomy = all(kit.text_w(lab[0], 12, ui) <= rw - 34 * ui for lab, _w, _p, _o in side) and \
            kit.text_w("Open in a window", 12, ui) <= rw - 20 * ui
        for i, (label, _where, _prop, on) in enumerate(side):
            ry = y + h - (i + 1) * r
            kit.switch_row(rx, ry, rw, r, label[0 if roomy else 1], on, ui)
            kit.hair(rx, ry, rw, ui)
        ry = y + h - (len(side) + 1) * r
        t, _down = kit.feel(rx, ry, rw, r)
        kit.text(rx, mid_y(ry, r, 12, ui), "Open in a window" if roomy else "Window", 12, kit.mix(T2, T1, t), ui,
                 maxw=rw - 20 * ui)
        kit.g_chev(rx + rw - 14 * ui + 2 * ui * t, ry + r / 2 - 7 * ui, 14 * ui, kit.mix(T3, T1, t), ui, "right")
    rows.add(tile, real=n_top, paint=p_top, raw=True)
    rows.gap(8)
    rows.add(32, real=lambda r: setter(r, "scene", "thumb_view"),
             paint=lambda x, y, w, h, ui: kit.button(x, y, w, h, "Done framing" if st.thumb_view else "Frame the shot",
                                                     ui, key="thumb_view!", on=st.thumb_view))

    if st.thumb_view:
        rows.mark()
        section_link(rows, "Shot", "Reset", "fitbridge.reset_camera")
        presets = (("FRONT", "Front"), ("THREE_Q", "3/4"), ("SIDE", "Side"), ("BACK", "Back"), ("TOP", "Top"),
                   ("ROBLOX", "Roblox"))
        now = next((k for k, (tu, ti) in thumb.PRESETS.items() if abs(g.turn - tu) < 0.05 and abs(g.tilt - ti) < 0.05),
                   None)
        per = 6 if width / rows.ui >= 300 else 3
        for k in range(0, len(presets), per):
            part = presets[k:k + per]

            def n_presets(r, part=part):
                for col, (val, _l) in zip(kit.equal(r, len(part)), part):
                    op(col, "fitbridge.thumb_preset", angle=val)
            rows.add(28, real=n_presets, paint=lambda x, y, w, h, ui, part=part: kit.chips(
                x, y + 2 * ui, w, h - 4 * ui, part, now, ui))
        rows.gap(4)
        dist = math.log(max(g.distance, 0.2) / 0.2) / math.log(100 / 0.2)
        for prop, label, value, frac_, centred in (
                ("turn", "Turn", f"{g.turn:.1f}°", (g.turn + 180) / 360, True),
                ("tilt", "Tilt", f"{g.tilt:.1f}°", (g.tilt + 89) / 178, True),
                ("distance", "Distance", f"{g.distance:.2f}", dist, False)):
            rows.add(32, real=lambda r, prop=prop: r.prop(g, prop, text="", slider=True),
                     paint=lambda x, y, w, h, ui, a=(label, value, frac_, centred): kit.slider(
                         x, y, w, h, a[0], a[1], a[2], ui, centred=a[3]))

        def n_pan(r):
            a, b = kit.equal(r, 2)
            a.prop(g, "pan_x", text="")
            b.prop(g, "pan_y", text="")

        def p_pan(x, y, w, h, ui):
            for i, (label, v) in enumerate((("Pan across", g.pan_x), ("Pan up", g.pan_y))):
                hw = (w - 12 * ui) / 2
                kit.slider(x + i * (hw + 12 * ui), y, hw, h, label, f"{v:.2f}", 0.5 + max(-0.5, min(0.5, v / 4)), ui,
                           centred=True)
        rows.add(32, real=n_pan, paint=p_pan)
        if p is not None:
            rows.gap(4)
            switch(rows, "Keep the camera level", "prefs", "lock_tilt", p.lock_tilt)
        rows.gap(4)
        rows.add(28, real=lambda r: op(r, "fitbridge.thumb_preview"),
                 paint=lambda x, y, w, h, ui: kit.button(x, y, w, h, "Render it full size", ui, enabled=can_render))
        if res is not None and res.get("fit") is not None and thumb.out_of_frame(res, g):
            rows.gap(4)
            rows.status("Part of the accessory is out of frame", "warn")
        rows.gap(8)
        rows.text("Move the 3D view to move the camera. The circle shows how Roblox would frame it."
                  + ("" if gob is not None else " Select your accessory to give it its own shot."))
    elif g.manual:
        rows.gap(4)
        section_link(rows, "Framed by you", "Reset", "fitbridge.reset_camera")


# ------------------------------------------------------------------------------------------------------------ Auto

def build_auto(rows, context, res):
    kind, line = auto.status(context)
    s0 = rows.n()
    rows.gap(8)
    full = rows.width
    rows.width = full - 24 * rows.ui                          # these lines sit inside the card
    if kind == "ready":
        rows.status(line, "good")
        rows.status("Meets all the checks, and the thumbnail is ready", "good")
        if auto.fit_on():
            rows.status("Sits properly on the R6", "good")
        for hint in auto.heads_ups(res):
            rows.status(hint, "warn")
    elif kind == "looking":
        rows.status(line.rstrip("."), "busy")
    else:
        rows.status(line, "warn" if kind == "no_key" else "bad")
    rows.width = full
    rows.gap(8)
    for i in range(s0, rows.n()):                             # nudge those rows in from the card's edge
        h, paint = rows.items[i]
        if paint is not None:
            rows.items[i] = (h, lambda x, y, w, hh, ui, f=paint: f(x + 12 * ui, y, w - 24 * ui, hh, ui))
    rows.card(s0)
    action = {"failed": ("fitbridge.auto_retry", "Try again"), "wrong_way": ("fitbridge.auto_ignore", "It's fine, allow it"),
              "misfit": ("fitbridge.auto_ignore", "It's fine, allow it"),
              "no_key": ("fitbridge.open_prefs", "Open the preferences")}.get(kind)
    if action:
        rows.gap(8)
        rows.add(28, real=lambda r: op(r, action[0]),
                 paint=lambda x, y, w, h, ui: kit.button(x, y, w, h, action[1], ui))
    if kind == "ready":
        rows.gap(12)
        t = min(TILE, max(96.0, rows.full / rows.ui - 2 * kit.PAD))
        rows.add(t, paint=lambda x, y, w, h, ui: p_tile(context, x, y, h, ui))


# ------------------------------------------------------------------------------------------------------------ Send

def send_reason(context, st, res, is_auto):
    """why Send is off, in a few words (None when it's on)"""
    problem = evaluate.selection_problem(context)
    if problem:
        return problem
    if is_auto:
        kind = auto.status(context)[0]
        return {"ready": None, "looking": "Waiting for the summary above",
                "no_key": "Add the API key first"}.get(kind, "Sort out the problem above first")
    if res is None:
        return "Show a mannequin to start"
    if res.get("fit") is None:
        bad = [c["message"] for c in res["checks"] if c["status"] == "fail"]
        return bad[0] if bad else "Fit a mesh first"
    if res["ok"] or st.override:
        return None
    n = sum(1 for c in res["checks"] if c["status"] == "fail")
    return f"{n} check{'s' if n != 1 else ''} to fix first"


def build_send(rows, context, st, res, is_auto):
    reason = send_reason(context, st, res, is_auto)
    rows.add(40, real=lambda r: op(r, "fitbridge.send"),
             paint=lambda x, y, w, h, ui: kit.button(x, y, w, h, "Send to Studio", ui, key="send", primary=True,
                                                     enabled=reason is None))
    n = len(batch.family(context))
    if n > 1:
        can = not batch.busy()
        rows.gap(8)
        rows.add(32, real=lambda r: op(r, "fitbridge.send_set"),
                 paint=lambda x, y, w, h, ui: kit.button(x, y, w, h, "Send all colours", ui, enabled=can,
                                                         right=f"{n} items"))
    notes = []
    if reason:
        notes.append((reason, "info"))
    elif server.running() and not server.studio_connected():
        notes.append(("Roblox Studio isn't connected. It picks this up when the FitBridge plugin is open.", "info"))
    got = batch.public()
    if got["message"]:
        text = f"{got['done']}/{got['total']}  {got['message']}" if got["state"] == "working" else got["message"]
        notes.append((text, {"failed": "bad", "done": "good"}.get(got["state"], "busy")))
    if server.state["sent_revision"]:
        up = upload.public()
        if up["message"]:
            notes.append((up["message"], {"done": "good", "failed": "bad", "pending": "busy", "off": "info"}[up["state"]]))
    status = SHARED["status"] or {}
    if status.get("message"):
        notes.append((status["message"], "info"))
    r = status.get("last_result")
    if r:
        if r.get("message"):
            notes.append((str(r["message"]), "good" if r.get("ok") else "bad"))
        for c in r.get("studio_checks") or []:
            if c.get("status") != "pass":
                notes.append((f"Studio: {c.get('message')}", STATUS.get(c.get("status"), "info")))
    if notes:
        rows.gap(8)
        for text, kind in notes:
            rows.status(text, kind)
    failing = res is not None and not res.get("ok") and res.get("fit") is not None
    if not is_auto and (failing or st.override):               # only on show while there's something to override
        rows.gap(4)
        switch(rows, "Send anyway (testing)", "scene", "override", st.override, col=T3)


# --------------------------------------------------------------------------------------------------------- Listing

def build_listing(rows, context, st):
    """-> True once there's a written listing on show (Send comes back under it)"""
    problem = evaluate.selection_problem(context)
    sel = evaluate.selected_meshes(context)
    ob = sel[0] if sel else st.target
    if problem or ob is None:
        rows.gap(8)
        rows.text(problem or "Select the accessory mesh", 12, T2)
        return False
    hello = listing.hello()
    kind, line = listing.state(ob)
    data = listing.stored(ob) or {}

    if kind in ("idle", "failed", "paused"):
        if kind == "failed":
            rows.status(line, "bad")
            rows.gap(8)
        if kind == "paused":
            rows.status(line, "info")
            rows.gap(8)
            rows.add(32, real=lambda r: op(r, "fitbridge.listing_write", again="resume"),
                     paint=lambda x, y, w, h, ui: kit.button(x, y, w, h, "See if it's done", ui))
            rows.gap(12)
        light = {"FLAT": "Flat colours", "STUDIO": "Soft light"}[st.listing_light]
        s0 = rows.n()
        rows.add(32, real=lambda r: r.prop(st, "listing_light", text=""),
                 paint=lambda x, y, w, h, ui: kit.field(x, y, w, h, "Pictures", light, ui))
        rows.add(32, real=lambda r: op(r, "fitbridge.edit_text", target="note"),
                 paint=lambda x, y, w, h, ui: kit.field(x, y, w, h, "Note", st.listing_note or "Who it is, a detail", ui,
                                                        kind="text", dim=not st.listing_note, last=True))
        rows.card(s0)
        why = (hello or {}).get("why") if hello is not None and not hello.get("ready", True) else ""
        if listing.HELLO["error"] and hello is None:
            why = listing.HELLO["error"]
        rows.gap(12)
        can = not listing.busy() and not why
        label = "Try again" if kind == "failed" else "Write the listing"
        rows.add(40, real=lambda r: op(r, "fitbridge.listing_write", again="no"),
                 paint=lambda x, y, w, h, ui: kit.button(x, y, w, h, label, ui, primary=True, enabled=can))
        rows.gap(4)
        if why:
            rows.status(why, "warn")
        elif listing.busy():
            rows.text("Another item's listing is being written")
        else:
            rows.text(f"Sends 4 pictures of {ob.name} to {SITE}, which names and describes it")
        return False

    if kind == "running":
        el = int(time.time() - listing.RUN["started"])
        rows.add(24, paint=lambda x, y, w, h, ui: (
            kit.status_line(x, y, w - 44 * ui, h, line, "busy", ui),
            kit.text(x + w, mid_y(y, h, 10.5, ui), f"{el // 60}:{el % 60:02d}", 10.5, T3, ui, align="right",
                     mono=True)))
        rows.gap(8)
        pics = data.get("pictures") or []
        if pics:
            cell = max(40.0, min(96.0, (inner(rows) / rows.ui - 24) / 4))
            cell -= cell % 4

            def p_pics(x, y, w, h, ui):
                s = min((w - 24 * ui) / 4, h)
                for i, path in enumerate(pics[:4]):
                    px = x + i * (s + 8 * ui)
                    kit.box(px, y + h - s, s, s, 8 * ui, S1, LINE)
                    if kit.image(path, px + 1, y + h - s + 1, s - 2, s - 2):
                        kit.corners(px + 1, y + h - s + 1, s - 2, s - 2, 7 * ui, kit.BG)
            rows.add(cell, paint=p_pics)
            rows.gap(8)
        rows.text("Usually about a minute")
        return False

    # --- done: names, description, closing lines
    copy = listing.copy_of(data) or {"names": [], "description": ""}
    item = listing.item_of(data) or {}
    got = data.get("listing") or {}
    vs = listing.variants(data)
    if vs:
        cur = data.get("variant") or "adjacent"

        def n_var(r):
            for col, (val, _l) in zip(kit.equal(r, 2), vs):
                op(col, "fitbridge.listing_set", what="variant", value=val)
        rows.add(28, real=n_var, paint=lambda x, y, w, h, ui: kit.segmented(x, y, w, h, vs, cur, ui))
        ch = item.get("character") or {}
        if ch.get("name"):
            rows.gap(4)
            rows.text(f"Looks like {ch['name']}" + (f" ({ch['series']})" if ch.get("series") else "")
                      + f". Wrong? Fix it in {SITE}.")
        rows.gap(4)

    rows.add(24, paint=lambda x, y, w, h, ui: kit.section(x, y, w, h, "Name", ui, note="Click one to use it"))
    s0 = rows.n()
    names = list(copy.get("names") or [])[:4]
    for i, name in enumerate(names):
        on = st.item_name == name
        parts = kit.wrap(name, 12, rows.width - 80 * rows.ui, rows.ui)[:3]

        def p_name(x, y, w, h, ui, name=name, on=on, last=i == len(names) - 1, parts=parts):
            t, down = kit.feel(x, y, w, h)
            if on:
                kit.box(x + 2 * ui, y + 2 * ui, w - 4 * ui, h - 4 * ui, 8 * ui, kit.mix(S3, kit.S4, t))
                kit.g_check(x + 10 * ui, y + h / 2 - 7 * ui, 14 * ui, ACC, ui)
            else:
                if t > 0.01 or down:
                    kit.box(x + 2 * ui, y + 2 * ui, w - 4 * ui, h - 4 * ui, 8 * ui, (1, 1, 1, 0.07 if down else 0.045 * t))
                kit.box(x + 11 * ui, y + h / 2 - 6 * ui, 12 * ui, 12 * ui, 6 * ui, S1,
                        kit.mix((1, 1, 1, 0.18), (1, 1, 1, 0.5), t))
                if not last:
                    kit.hair(x + 32 * ui, y, w - 32 * ui, ui)
            top = y + h / 2 + (len(parts) - 1) * 8 * ui
            for k, part in enumerate(parts):
                kit.text(x + 32 * ui, top - k * 16 * ui - 12 * ui * 0.36, part, 12, T1 if on else kit.mix(T2, T1, t), ui)
            if len(name) > 40:
                kit.text(x + w - 12 * ui, mid_y(y, h, 10.5, ui), f"{len(name)}/50", 10.5,
                         WARN if len(name) > 50 else T3, ui, align="right", mono=True)
        rows.add(16 + 16 * len(parts), real=lambda r, i=i: op(r, "fitbridge.listing_name", index=i), paint=p_name)
    rows.card(s0)

    desc = copy.get("description") or ""
    rows.gap(12)
    rows.add(24, paint=lambda x, y, w, h, ui: kit.section(x, y, w, h, "Description", ui))
    s0 = rows.n()
    rows.gap(8)
    for part in kit.wrap(desc, 12, rows.width - 24 * rows.ui, rows.ui):
        rows.add(16, paint=lambda x, y, w, h, ui, t=part: kit.text(x + 12 * ui, y + 4 * ui, t, 12, T2, ui))
    rows.gap(4)
    copied = time.time() - kit.FLASH.get("copied", 0) < 1.5
    clabel = "Copied" if copied else "Copy"
    cf = kit.pill_frac("Copied", rows.full, rows.ui, inset=8)

    def n_copy(r):
        a, b = kit.split(r, cf, 1 - cf)
        op(a, "fitbridge.listing_copy")
        b.label(text="")

    def p_copy(x, y, w, h, ui):
        kit.pill(x + 8 * ui, y, h, clabel, ui)
        kit.text(x + w - 12 * ui, mid_y(y, h, 10.5, ui), f"{len(desc):,} / 1,000", 10.5,
                 WARN if len(desc) > 1000 else T3, ui, align="right", mono=True)
    rows.add(28, real=n_copy, paint=p_copy)
    rows.gap(4)
    rows.card(s0)
    rows.gap(4)
    rows.text("Paste it into the Roblox upload. The name is sent for you.")

    chips = (hello or {}).get("chips") or []
    on_ids = [str(i) for i in got.get("chip_ids") or []]
    if chips:
        rows.gap(8)
        rows.add(24, paint=lambda x, y, w, h, ui: kit.section(x, y, w, h, "Closing lines", ui))
        for c in chips:
            rows.add(24, real=lambda r, c=c: op(r, "fitbridge.listing_set", what="chip", value=str(c["id"])),
                     paint=lambda x, y, w, h, ui, c=c: kit.checkbox(x, y, h, str(c.get("name") or c.get("text") or ""),
                                                                    str(c["id"]) in on_ids, ui, w=w))

    price = item.get("price")
    offer = listing.type_offer(data, st.acc_type)
    if price or offer:
        rows.gap(12)
        s0 = rows.n()
        if price:
            def p_price(x, y, w, h, ui):
                lw = kit.text(x + 12 * ui, mid_y(y, h, 12, ui), "Suggested price", 12, T2, ui)
                tail = f"  from {price['from']} similar"
                amount = f"{int(price['median']):,}"
                rw = 0.0
                if lw + kit.text_w(tail, 11, ui) + kit.text_w(amount + " Robux", 12, ui, mono=True) + 36 * ui <= w:
                    rw = kit.text(x + w - 12 * ui, mid_y(y, h, 11, ui), tail, 11, T3, ui, align="right")
                rw += kit.text(x + w - 12 * ui - rw, mid_y(y, h, 12, ui), " Robux", 12, T1, ui, align="right")
                kit.text(x + w - 12 * ui - rw, mid_y(y, h, 12, ui), amount, 12, T1, ui, align="right", mono=True)
                if offer:
                    kit.hair(x + 12 * ui, y, w - 12 * ui, ui)
            rows.add(32, paint=p_price)
        if offer:
            use = f"Use {offer}"
            uf = kit.pill_frac(use, rows.full, rows.ui, inset=8)

            def n_offer(r):
                a, b = kit.split(r, 1 - uf, uf)
                a.label(text="")
                op(b, "fitbridge.listing_set", what="type", value=offer)

            def p_offer(x, y, w, h, ui):
                bw = kit.pill(x + w - 8 * ui, y, h, use, ui, align="right")
                kit.text(x + 12 * ui, mid_y(y, h, 12, ui),
                         f"Looks like {item.get('category_label', offer)}", 12, T2, ui,
                         maxw=w - bw - 28 * ui)
            rows.add(32, real=n_offer, paint=p_offer)
        rows.card(s0)

    rows.gap(8)
    lf = kit.pill_frac(f"Open in {SITE}", rows.full, rows.ui)
    rf = kit.pill_frac("Write it again", rows.full, rows.ui)

    def n_foot(r):
        a, b, c = kit.split(r, lf, max(0.02, 1 - lf - rf), rf)
        op(a, "fitbridge.listing_open")
        b.label(text="")
        op(c, "fitbridge.listing_write", again="yes")

    def p_foot(x, y, w, h, ui):
        kit.pill(x, y, h, f"Open in {SITE}", ui)
        kit.pill(x + w, y, h, "Write it again", ui, align="right")
    rows.add(28, real=n_foot, paint=p_foot)
    if listing.RUN["error"] and listing.RUN["error_ob"] == ob.name:
        rows.status(listing.RUN["error"], "bad")
    elif got.get("error"):                                    # a later fix on the site that failed: the listing stands
        rows.status(str(got["error"]), "warn")
    return True


# ------------------------------------------------------------------------------------------------------- operators

class FB_OT_ui_set(bpy.types.Operator):
    bl_idname = "fitbridge.ui_set"
    bl_label = "Set"
    bl_description = "Change this"
    bl_options = {"INTERNAL"}
    where: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    prop: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    value: StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    TIPS = {"ui_tab": "Show this part",
            "listing_light": "How the four pictures are lit: flat shows the true colours, studio light shows the shape"}

    @classmethod
    def _target(cls, context, where):
        return host.prefs() if where == "prefs" else context.scene.fitbridge

    @classmethod
    def description(cls, context, props):
        if props.prop == "ui_collapsed":
            return f"Fold FitBridge away, or open it again. {server.status_text()}. {spec.rules_text()}"
        if props.prop in cls.TIPS:
            return cls.TIPS[props.prop]
        t = cls._target(context, props.where)
        rna = t.bl_rna.properties.get(props.prop) if t is not None else None
        return (rna.description if rna is not None and rna.description else "Change this")

    def execute(self, context):
        t = self._target(context, self.where)
        if t is None:
            return {"CANCELLED"}
        if self.value == "!":
            setattr(t, self.prop, not getattr(t, self.prop))
        else:
            setattr(t, self.prop, self.value)
        if self.where == "prefs":
            context.preferences.is_dirty = True
        kit.flash(f"{self.prop}{self.value}")
        return {"FINISHED"}


class FB_OT_edit_text(bpy.types.Operator):
    bl_idname = "fitbridge.edit_text"
    bl_label = "FitBridge"
    bl_options = {"INTERNAL"}
    target: StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    @classmethod
    def description(cls, context, props):
        return {"name": "The accessory's name in Roblox Studio. Leave it empty to use the mesh's name",
                "note": "Optional. Anything to know before it writes: who the character is, the style, a detail. "
                        "The note always wins over what it reads from the pictures"}.get(props.target, "Edit")

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=360, confirm_text="Done")

    def draw(self, context):
        st = context.scene.fitbridge
        if self.target == "note":
            self.layout.prop(st, "listing_note", text="Note")
        else:
            self.layout.prop(st, "item_name", text="Name")

    def execute(self, context):
        kit.redraw()
        return {"FINISHED"}


def _listing_ob(context):
    if evaluate.selection_problem(context):
        return None
    sel = evaluate.selected_meshes(context)
    return sel[0] if sel else context.scene.fitbridge.target


class FB_OT_listing_write(bpy.types.Operator):
    bl_idname = "fitbridge.listing_write"
    bl_label = "Write the listing"
    bl_description = "Take four pictures of the selected accessory and have Paradyse write its name options and " \
                     "description. Uses Claude on the site, like its UGC listings page does"
    again: StringProperty(default="no", options={"HIDDEN", "SKIP_SAVE"})

    @classmethod
    def description(cls, context, props):
        if props.again == "resume":
            return "Ask whether the listing that was being written is finished"
        if props.again == "yes":
            return "Take the pictures again and have a new listing written for this item"
        return cls.bl_description

    @classmethod
    def poll(cls, context):
        return listing.available() and not listing.busy() and _listing_ob(context) is not None

    def invoke(self, context, event):
        if self.again == "yes":
            return context.window_manager.invoke_confirm(
                self, event, title="Write it again?", message="This replaces the listing kept on this item and uses "
                                                              "Claude on the site again.",
                confirm_text="Write it again")
        return self.execute(context)

    def execute(self, context):
        ob = _listing_ob(context)
        st = context.scene.fitbridge
        if self.again == "resume":
            if not listing.resume(ob):
                listing.forget(ob)
            return {"FINISHED"}
        evaluate.follow_selection(context)
        problem = listing.start(context, ob, st.listing_note, st.listing_light)
        if problem:
            self.report({"ERROR"}, problem)
            return {"CANCELLED"}
        return {"FINISHED"}


class FB_OT_listing_name(bpy.types.Operator):
    bl_idname = "fitbridge.listing_name"
    bl_label = "Use this name"
    bl_description = "Make this the accessory's name: Send to Studio uses it. Click it again to go back to the mesh's " \
                     "own name"
    bl_options = {"INTERNAL", "UNDO"}
    index: IntProperty(options={"HIDDEN", "SKIP_SAVE"})

    def execute(self, context):
        ob = _listing_ob(context)
        copy = listing.copy_of(listing.stored(ob) or {}) or {}
        names = copy.get("names") or []
        if ob is None or self.index >= len(names):
            return {"CANCELLED"}
        st = context.scene.fitbridge
        evaluate.follow_selection(context)
        st.item_name = "" if st.item_name == names[self.index] else names[self.index]
        return {"FINISHED"}


class FB_OT_listing_copy(bpy.types.Operator):
    bl_idname = "fitbridge.listing_copy"
    bl_label = "Copy the description"
    bl_description = "Copy the description, with the closing lines that are ticked, ready to paste into the upload"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        copy = listing.copy_of(listing.stored(_listing_ob(context)) or {}) or {}
        context.window_manager.clipboard = copy.get("description") or ""
        kit.FLASH["copied"] = time.time()
        bpy.app.timers.register(lambda: (kit.redraw(), None)[1], first_interval=1.6)
        return {"FINISHED"}


class FB_OT_listing_set(bpy.types.Operator):
    bl_idname = "fitbridge.listing_set"
    bl_label = "Listing"
    bl_options = {"INTERNAL"}
    what: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    value: StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    @classmethod
    def description(cls, context, props):
        return {"variant": "Show the listing written with the character's real name, or with the adjacent name",
                "chip": "Add this closing line to the end of the description. Your choice is remembered for the "
                        "next listing too",
                "type": "Set Type to the category that was read from the pictures"}.get(props.what, "")

    def execute(self, context):
        ob = _listing_ob(context)
        data = listing.stored(ob)
        if data is None:
            return {"CANCELLED"}
        if self.what == "variant":
            listing.set_variant(ob, self.value)
        elif self.what == "chip":
            ids = [str(i) for i in (data.get("listing") or {}).get("chip_ids") or []]
            ids = [i for i in ids if i != self.value] if self.value in ids else ids + [self.value]
            listing.set_chips(ob, ids)
        elif self.what == "type":
            context.scene.fitbridge.acc_type = self.value
        kit.redraw()
        return {"FINISHED"}


class FB_OT_listing_open(bpy.types.Operator):
    bl_idname = "fitbridge.listing_open"
    bl_label = "Open in Paradyse"
    bl_description = "Open this listing on the website: the catalog words it used, the character it read, and " \
                     "your closing lines to edit"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        got = (listing.stored(_listing_ob(context)) or {}).get("listing") or {}
        url = str(got.get("url") or "")
        site = host.studio()[0]
        if not url.startswith(site + "/"):                      # only ever the Studio site that's set
            url = site + "/studio"
        webbrowser.open(url)
        return {"FINISHED"}


class FB_OT_select_open_edges(bpy.types.Operator):
    bl_idname = "fitbridge.select_open_edges"
    bl_label = "Select open edges"
    bl_description = "Go to Edit Mode on the accessory and select the edges around every hole, opening and ripped " \
                     "edge (the ones this warning counts), so you can see and close them"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return _listing_ob(context) is not None and context.mode in ("OBJECT", "EDIT_MESH")

    def execute(self, context):
        import bmesh
        ob = _listing_ob(context)
        if ob is None or ob.type != "MESH":
            return {"CANCELLED"}
        if context.mode == "EDIT_MESH" and context.edit_object is not ob:
            bpy.ops.object.mode_set(mode="OBJECT")
        if context.mode == "OBJECT":
            for o in context.selected_objects:
                o.select_set(False)
            ob.select_set(True)
            context.view_layer.objects.active = ob
            bpy.ops.object.mode_set(mode="EDIT")
        bm = bmesh.from_edit_mesh(ob.data)
        # an open edge has exactly one face: the rim of a hole, the edge of a sheet, both sides of a rip
        edges = [e for e in bm.edges if e.is_boundary and not e.hide]
        if not edges:
            self.report({"INFO"}, "No open edges on the mesh as it is in Edit Mode (a modifier may be opening it)")
            return {"CANCELLED"}
        bpy.ops.mesh.select_mode(type="EDGE")
        bpy.ops.mesh.select_all(action="DESELECT")
        for e in edges:
            e.select_set(True)
        bm.select_flush_mode()
        bmesh.update_edit_mesh(ob.data, loop_triangles=False, destructive=False)
        self.report({"INFO"}, f"{len(edges):,} open edge{'s' * (len(edges) != 1)} selected")
        return {"FINISHED"}


class FB_OT_open_prefs(bpy.types.Operator):
    bl_idname = "fitbridge.open_prefs"
    bl_label = "Open the preferences"
    bl_description = "Open FitBridge's settings in Blender's preferences"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        try:
            bpy.ops.preferences.addon_show(module=host.ROOT)
        except Exception:  # noqa: BLE001 - the plain preferences window is still the right place
            bpy.ops.screen.userpref_show("INVOKE_DEFAULT")
        return {"FINISHED"}


classes = (FB_OT_ui_set, FB_OT_edit_text, FB_OT_open_prefs, FB_OT_select_open_edges, FB_OT_listing_write, FB_OT_listing_name, FB_OT_listing_copy,
           FB_OT_listing_set, FB_OT_listing_open)
