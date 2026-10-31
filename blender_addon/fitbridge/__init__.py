# FitBridge (Blender half): fit a rigid Roblox UGC accessory on an R15 mannequin with a live ghost bounds box and
# live spec checks, frame Roblox's thumbnail camera, then Send to Studio over a localhost bridge, where the
# FitBridge Studio plugin builds the finished Accessory. Contract: docs/PROTOCOL.md and spec/ugc_spec.json.

import os
import time

import bpy
from bpy.props import (BoolProperty, EnumProperty, FloatProperty, FloatVectorProperty, IntProperty,
                       PointerProperty, StringProperty)

from . import draw, evaluate, rig, server, spec, thumb, upload

ICONS = {"pass": "CHECKMARK", "warn": "ERROR", "fail": "CANCEL"}
_dirty = {"flag": True, "last": 0.0}
_status = {"last_result": None, "last_sent": None, "message": ""}


def mark_dirty(self=None, context=None):
    _dirty["flag"] = True


# ----------------------------------------------------------------------------------------------------------- props

_att_items = {}


def _attachment_items(self, context):
    t = self.acc_type
    if t not in _att_items:                                   # Blender needs these strings kept alive
        _att_items[t] = [(k, label, k) for k, label in spec.attachments_for(t)]
    return _att_items[t]


def _scale_changed(self, context):
    if rig.shown() == "R15":                                 # the R15 follows the body scale
        _ask_for_rig(self.body_scale)
        rig.spawn_r15(context, self.body_scale)
    mark_dirty()


def _ask_for_rig(key):
    """put rig:<key> in /v1/hello's needs when we have no up-to-date synced rig for it; True if asked"""
    if spec.rig_outdated(key):
        with server.lock:
            server.state["need_rigs"].add(key)
        return True
    return False


def _type_changed(self, context):
    self.attachment = spec.attachments_for(self.acc_type)[0][0]
    mark_dirty()


def _thumb_changed(self, context):
    if self.thumb_view:
        thumb.view_on(context)
    else:
        thumb.view_off(context)


def _lock_tilt_changed(context):
    if context.scene.fitbridge.thumb_view:
        p = prefs()
        thumb.tilt_lock(context, bool(p and p.lock_tilt))


def _shot_changed(self, context):
    """a slider moved: that's you framing the shot"""
    if thumb._quiet["n"]:
        return
    self.manual = True
    thumb.apply_params(context)


class FB_Thumb(bpy.types.PropertyGroup):
    """one thumbnail shot: an orbit around a pivot in the worn Handle's frame"""
    camera: PointerProperty(type=bpy.types.Object)
    manual: BoolProperty(default=False, description="You've framed this shot yourself (Reset goes back to the default)")
    key: StringProperty()
    turn: FloatProperty(name="Turn", default=25.0, min=-180.0, max=180.0, step=100, precision=1, update=_shot_changed,
                        description="Around the item: 0 = from the front, + = toward the character's right")
    tilt: FloatProperty(name="Tilt", default=15.0, min=-89.0, max=89.0, step=100, precision=1, update=_shot_changed,
                        description="From above (+) or below (-)")
    distance: FloatProperty(name="Distance", default=2.0, min=0.2, max=100.0, step=10, precision=2, unit="LENGTH",
                            update=_shot_changed, description="How far the camera is from what it looks at (studs)")
    pan_x: FloatProperty(name="Pan X", default=0.0, step=5, precision=2, unit="LENGTH", update=_shot_changed,
                         description="Shift the picture left / right")
    pan_y: FloatProperty(name="Pan Y", default=0.0, step=5, precision=2, unit="LENGTH", update=_shot_changed,
                         description="Shift the picture up / down")
    pivot: FloatVectorProperty(size=3)


def _poll_mesh(self, ob):
    return ob.type == "MESH" and not ob.get("fitbridge_rig")


class FB_Settings(bpy.types.PropertyGroup):
    body_scale: EnumProperty(name="Body scale", items=[(s, s, spec.SPEC["body_scales"][s]["label"]) for s in spec.OFFERED_SCALES],
                             default=spec.OFFERED_SCALES[0], update=_scale_changed)
    acc_type: EnumProperty(name="Type", items=[(t, t, "") for t in spec.RIGID_TYPES], update=_type_changed)
    attachment: EnumProperty(name="Attachment", items=_attachment_items, update=mark_dirty)
    target: PointerProperty(name="Mesh", type=bpy.types.Object, poll=_poll_mesh, update=mark_dirty,
                            description="The accessory mesh (empty = the active object)")
    item_name: StringProperty(name="Name", description="Accessory name in Studio (empty = the object's name)")
    show_box: BoolProperty(name="Ghost box", default=True, description="Draw the attachment's bounds box")
    override: BoolProperty(name="Override checks (testing)", default=False,
                           description="Allow Send to Studio even when a blocking check fails")
    thumb_view: BoolProperty(name="Thumbnail view", default=False, update=_thumb_changed,
                             description="Look through Roblox's thumbnail camera (70 deg vertical FOV, square 420). "
                                         "Orbit, pan and zoom as usual to move it")
    thumb_default: PointerProperty(type=FB_Thumb)            # the shot before an accessory mesh is picked
    thumb_saved: StringProperty()
    revision: IntProperty(default=0)


class FB_Prefs(bpy.types.AddonPreferences):
    bl_idname = __package__

    port: IntProperty(name="Port", default=47231, min=1024, max=65535,
                      description="Must match the Studio plugin's setting")
    api_key: StringProperty(name="Open Cloud API key", subtype="PASSWORD",
                            description="Roblox Open Cloud key with Assets read + write. Stays on this computer: "
                                        "only sent to apis.roblox.com, never over the bridge")
    creator_type: EnumProperty(name="Creator", items=[("User", "User", ""), ("Group", "Group", "")])
    welcomed: BoolProperty(default=False, options={"HIDDEN"})
    lock_tilt: BoolProperty(name="Lock tilt", default=True, update=lambda self, context: _lock_tilt_changed(context),
                            description="While framing the thumbnail, orbit as Turntable so the camera never rolls "
                                        "sideways (your own orbit style comes back when you leave Thumbnail view)")
    saved_orbit: StringProperty(options={"HIDDEN"})          # your orbit style while Lock tilt borrows it
    creator_id: StringProperty(name="Creator id",
                               description="The user or group that will submit the UGC item (Roblox checks that "
                                           "it owns the mesh and textures)")

    def draw(self, context):
        lay = self.layout
        row = lay.row()
        row.prop(self, "port")
        row.operator("fitbridge.restart_server", text="Restart bridge")
        box = lay.box()
        box.label(text="Upload through Roblox Open Cloud", icon="URL")
        box.prop(self, "api_key")
        row = box.row()
        row.prop(self, "creator_type", expand=True)
        row.prop(self, "creator_id")


def prefs():
    a = bpy.context.preferences.addons.get(__package__)
    return a.preferences if a else None


# ------------------------------------------------------------------------------------------------------- operators

class FB_OT_spawn_r15(bpy.types.Operator):
    bl_idname = "fitbridge.spawn_r15"
    bl_label = "Spawn R15"
    bl_description = "Add the R15 mannequin for the chosen body scale (the rig synced from Studio, or boxes until then)"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scale = context.scene.fitbridge.body_scale
        if _ask_for_rig(scale) and spec.cached_rig(scale) is None:
            self.report({"WARNING"}, "Open Studio with the FitBridge plugin to fetch the rig. Showing a box rig.")
        rig.spawn_r15(context, scale)
        mark_dirty()
        return {"FINISHED"}


class FB_OT_show_rig(bpy.types.Operator):
    bl_idname = "fitbridge.show_rig"
    bl_label = "Mannequin"
    bl_description = "Show this mannequin (it replaces the other one, in the same place). Click it again to hide it"
    bl_options = {"REGISTER", "UNDO"}

    kind: EnumProperty(items=[("R15", "R15", "The R15 mannequin Roblox fits and checks against"),
                              ("R6", "R6", "A blocky R6 to look at (preview only)")])

    @classmethod
    def description(cls, context, props):
        if props.kind == "R6":
            return "R6 preview, in the same place as the R15. Roblox fits and checks rigid accessories against R15"
        return "R15 mannequin for the chosen body scale (synced from Studio, or boxes until then)"

    def execute(self, context):
        if rig.shown() == self.kind:
            rig.clear_all()
        elif self.kind == "R15":
            return bpy.ops.fitbridge.spawn_r15()
        else:
            _ask_for_rig("R6")
            rig.spawn_r6(context)
        mark_dirty()
        return {"FINISHED"}


class FB_OT_spawn_r6(bpy.types.Operator):
    bl_idname = "fitbridge.spawn_r6"
    bl_label = "Spawn R6 preview"
    bl_description = "A blocky R6 dummy to look at. Roblox fits and checks rigid accessories against R15"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        rig.spawn_r6(context)
        return {"FINISHED"}


class FB_OT_reset_camera(bpy.types.Operator):
    bl_idname = "fitbridge.reset_camera"
    bl_label = "Reset shot"
    bl_description = "Back to the default close-up for this type and attachment (it follows your choices again)"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        thumb.reset(context)
        return {"FINISHED"}


class FB_OT_thumb_preset(bpy.types.Operator):
    bl_idname = "fitbridge.thumb_preset"
    bl_label = "Angle"
    bl_options = {"REGISTER", "UNDO"}

    angle: EnumProperty(items=[("FRONT", "Front", "Straight from the front"),
                               ("THREE_Q", "3/4", "Three-quarter view, on the item's side"),
                               ("SIDE", "Side", "From the item's side"),
                               ("BACK", "Back", "From behind"),
                               ("TOP", "Top", "From above"),
                               ("ROBLOX", "Roblox", "The exact shot Roblox's own UGC Thumbnail Tool starts from")])

    @classmethod
    def description(cls, context, props):
        return {"FRONT": "Straight from the front", "THREE_Q": "Three-quarter view, on the item's side",
                "SIDE": "From the item's side", "BACK": "From behind", "TOP": "From above",
                "ROBLOX": "The exact shot Roblox's own UGC Thumbnail Tool starts from (needs a fitted mesh)"}[props.angle]

    def execute(self, context):
        if not thumb.preset(context, self.angle):
            self.report({"WARNING"}, "Pick your accessory mesh first")
            return {"CANCELLED"}
        return {"FINISHED"}


class FB_OT_thumb_preview(bpy.types.Operator):
    bl_idname = "fitbridge.thumb_preview"
    bl_label = "Preview"
    bl_description = "Render the 420 x 420 thumbnail with simple flat lighting (only the accessory) and show it"

    def execute(self, context):
        img = thumb.preview(context)
        if img is None:
            self.report({"ERROR"}, "The preview render failed")
            return {"CANCELLED"}
        try:
            bpy.ops.render.view_show("INVOKE_DEFAULT")
        except RuntimeError:
            pass
        return {"FINISHED"}


class FB_OT_send(bpy.types.Operator):
    bl_idname = "fitbridge.send"
    bl_label = "Send to Studio"
    bl_description = "Package the accessory for the FitBridge Studio plugin, which builds it automatically"

    @classmethod
    def poll(cls, context):
        res = evaluate.LIVE["result"]
        st = context.scene.fitbridge
        return res is not None and res.get("fit") is not None and (res["ok"] or st.override)

    def execute(self, context):
        st = context.scene.fitbridge
        res = evaluate.evaluate(context)
        if res.get("fit") is None:
            self.report({"ERROR"}, "Nothing to send: fit a mesh first")
            return {"CANCELLED"}
        if not res["ok"] and not st.override:
            self.report({"ERROR"}, "Fix the failing checks first (or tick Override for testing)")
            return {"CANCELLED"}
        rev = max(st.revision, server.state["sent_revision"]) + 1
        blobs, manifest, mesh, pixels = evaluate.build_package(context, res, rev, st.override)
        server.publish(blobs, manifest, rev)
        p = prefs()
        upload.start(rev, manifest, mesh, pixels, p.api_key if p else "", p.creator_type if p else "User",
                     p.creator_id if p else "")
        st.revision = rev
        _status["last_sent"] = time.time()
        _status["message"] = f"Sent {manifest['name']} (revision {rev})"
        self.report({"INFO"}, _status["message"] + (". Studio builds it now." if server.studio_connected()
                                                     else ". Studio picks it up when its panel is open."))
        return {"FINISHED"}


class FB_OT_restart_server(bpy.types.Operator):
    bl_idname = "fitbridge.restart_server"
    bl_label = "Restart bridge"

    def execute(self, context):
        p = prefs()
        server.start(p.port if p else 47231)
        return {"FINISHED"}


# ----------------------------------------------------------------------------------------------------------- panel

class FB_PT_main(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "FitBridge"
    bl_label = "FitBridge"

    def draw(self, context):
        st = context.scene.fitbridge
        lay = self.layout
        p = prefs()
        if p is not None and not p.welcomed:
            p.welcomed = True                       # the panel has been found: the viewport hint goes away
            context.preferences.is_dirty = True
        res = evaluate.LIVE["result"]
        if abs(context.scene.unit_settings.scale_length - 1.0) > 1e-6:
            lay.label(text="Scene unit scale isn't 1.0 (1 unit = 1 stud)", icon="ERROR")

        box = lay.box()
        box.label(text="Rig", icon="ARMATURE_DATA")
        row = box.row(align=True)
        cur = rig.shown()
        row.operator("fitbridge.show_rig", text="R15", icon="OUTLINER_OB_ARMATURE", depress=cur == "R15").kind = "R15"
        row.operator("fitbridge.show_rig", text="R6 preview", icon="MESH_CUBE", depress=cur == "R6").kind = "R6"
        if cur == "R15" and len(spec.OFFERED_SCALES) > 1:        # body scales only exist for the R15
            box.prop(st, "body_scale")
        key = "R6" if cur == "R6" else st.body_scale
        if spec.rig_outdated(key):
            col = box.column(align=True)
            col.scale_y = 0.8
            col.label(text="Open Studio with the FitBridge plugin", icon="INFO")
            col.label(text="to fetch the real rig meshes." if spec.cached_rig(key) else
                      "to fetch the rig (boxes until then).")

        box = lay.box()
        box.label(text="Accessory", icon="MOD_CLOTH")
        box.prop(st, "acc_type")
        box.prop(st, "attachment")
        box.prop(st, "target")
        if st.target is None:
            ob = evaluate.target_object(context)
            box.label(text=f"Using active: {ob.name}" if ob else "No mesh picked", icon="OBJECT_DATA")
        box.prop(st, "item_name")
        box.prop(st, "show_box")
        if not draw.ready(context):
            box.label(text="Spawn a rig and pick your accessory mesh to see the bounds box.", icon="INFO")

        box = lay.box()
        box.label(text="Checks", icon="CHECKBOX_HLT")
        col = box.column(align=True)
        if res is not None:
            for c in res["checks"]:
                col.label(text=c["message"], icon=ICONS[c["status"]])
        elif not evaluate.in_use(context):
            col.label(text="Show R15 or R6, or pick a mesh, to start", icon="INFO")

        box = lay.box()
        box.label(text="Thumbnail", icon="CAMERA_DATA")
        row = box.row(align=True)
        row.prop(st, "thumb_view", toggle=True, icon="VIEW_CAMERA")
        row.operator("fitbridge.reset_camera", text="", icon="FILE_REFRESH")
        g, gob = thumb.owner(context)
        if st.thumb_view:
            row = box.row(align=True)
            for key, label in (("FRONT", "Front"), ("THREE_Q", "3/4"), ("SIDE", "Side"), ("BACK", "Back"),
                               ("TOP", "Top"), ("ROBLOX", "Roblox")):
                row.operator("fitbridge.thumb_preset", text=label).angle = key
            col = box.column(align=True)
            col.prop(g, "turn")
            col.prop(g, "tilt")
            col.prop(g, "distance")
            row = box.row(align=True)
            row.prop(g, "pan_x", text="Pan X")
            row.prop(g, "pan_y", text="Pan Y")
            row = box.row(align=True)
            row.operator("fitbridge.thumb_preview", icon="RENDER_STILL")
            p = prefs()
            if p is not None:
                row.prop(p, "lock_tilt", toggle=True, icon="LOCKED" if p.lock_tilt else "UNLOCKED")
            if res is not None and res.get("fit") is not None and thumb.out_of_frame(res, g):
                box.label(text="Part of the accessory is out of frame", icon="ERROR")
            col = box.column(align=True)
            col.scale_y = 0.8
            col.label(text="Orbit / pan / zoom the view to move the camera", icon="INFO")
            col.label(text="Circle: the Handle fit Roblox's own tool frames")
            if gob is None:
                col.label(text="Pick your accessory mesh to give it its own shot")
        elif g.manual:
            box.label(text="Framed by you (reset to follow the type again)", icon="CAMERA_DATA")

        box = lay.box()
        row = box.row(align=True)
        row.scale_y = 1.4
        row.operator("fitbridge.send", icon="EXPORT")
        box.prop(st, "override")
        up = upload.public()
        if server.state["sent_revision"]:
            icon = {"done": "CHECKMARK", "failed": "ERROR", "pending": "SORTTIME", "off": "INFO"}[up["state"]]
            box.label(text=up["message"], icon=icon)
        col = box.column(align=True)
        col.scale_y = 0.8
        col.label(text=server.status_text(), icon="LINKED" if server.studio_connected() else "UNLINKED")
        col.label(text=spec.rules_text(), icon="FILE_TEXT")
        if _status["message"]:
            col.label(text=_status["message"])
        r = _status["last_result"]
        if r:
            col.label(text=str(r.get("message") or ""), icon="CHECKMARK" if r.get("ok") else "ERROR")
            for c in r.get("studio_checks") or []:
                if c.get("status") != "pass":
                    col.label(text=f"Studio: {c.get('message')}", icon=ICONS.get(c.get("status"), "QUESTION"))


# ------------------------------------------------------------------------------------------------- main-thread loop

def process_inbox():
    """apply what Studio POSTed (runs on the main thread)"""
    changed = False
    while True:
        try:
            path, body = server.inbox.get_nowait()
        except Exception:
            break
        try:
            if path == "/v1/rules":
                spec.save_rules(body)
            elif path == "/v1/rig":
                scale = spec.save_rig(body)
                evaluate._bvh_cache.clear()
                if scale == "R6":
                    if rig.spawned_r6_source() in ("standin", "outdated"):
                        rig.spawn_r6(bpy.context)              # swap the boxes for the real R6
                else:
                    sc, src = rig.spawned_r15()
                    if sc == scale and src in ("standin", "outdated"):
                        rig.spawn_r15(bpy.context, scale)      # swap the box / older rig for the new one
            elif path == "/v1/package/result":
                _status["last_result"] = body
        except Exception as e:                                # a bad payload mustn't stop the loop
            print(f"FitBridge: couldn't use {path}: {e}")
        changed = True
    return changed


def _tick():
    try:
        changed = process_inbox()
        now = time.time()
        if _dirty["flag"] and now - _dirty["last"] > 0.1 and bpy.context.scene is not None:
            _dirty["flag"] = False
            _dirty["last"] = now
            try:
                if evaluate.in_use(bpy.context):
                    res = evaluate.evaluate(bpy.context)
                    thumb.sync(bpy.context, res)
                else:
                    evaluate.LIVE["result"] = None
            except Exception as e:
                print(f"FitBridge: check failed: {e}")
            changed = True
        if changed or now - _dirty.get("redrawn", 0) > 1.0:          # status line (Studio connected) once a second
            _dirty["redrawn"] = now
            for win in bpy.context.window_manager.windows:
                for area in win.screen.areas:
                    if area.type == "VIEW_3D":
                        area.tag_redraw()
    except ReferenceError:
        pass
    return 0.15


@bpy.app.handlers.persistent
def _on_depsgraph(scene, depsgraph):
    _dirty["flag"] = True


def _restore_orbit():
    """a Blender that closed while Lock tilt borrowed the orbit style gets the user's own one back"""
    p = prefs()
    if p is not None and p.saved_orbit and not any(getattr(sc, "fitbridge", None) and sc.fitbridge.thumb_view
                                                   for sc in bpy.data.scenes):
        bpy.context.preferences.inputs.view_rotate_method = p.saved_orbit
        p.saved_orbit = ""
    return None


def _fix_scales():
    """files saved with a body scale that's no longer offered (Normal / Slender) go back to Classic; thumbnail cameras
    stay hidden unless Thumbnail view is on"""
    if not any(getattr(sc, "fitbridge", None) and sc.fitbridge.thumb_view for sc in bpy.data.scenes):
        thumb.show_cameras(False)
    for sc in bpy.data.scenes:
        st = getattr(sc, "fitbridge", None)
        if st is not None and st.body_scale not in spec.OFFERED_SCALES:
            st.body_scale = spec.OFFERED_SCALES[0]
    return None


@bpy.app.handlers.persistent
def _on_load(*_args):
    _fix_scales()
    _dirty["flag"] = True
    evaluate.LIVE["result"] = None


classes = (FB_Thumb, FB_Settings, FB_Prefs, FB_OT_spawn_r15, FB_OT_spawn_r6, FB_OT_show_rig, FB_OT_reset_camera, FB_OT_thumb_preset, FB_OT_thumb_preview,
           FB_OT_send,
           FB_OT_restart_server, FB_PT_main)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.fitbridge = PointerProperty(type=FB_Settings)
    bpy.types.Object.fitbridge_thumb = PointerProperty(type=FB_Thumb)
    server.state["hello"]["blender_version"] = bpy.app.version_string
    p = prefs()
    server.start(int(os.environ.get("FITBRIDGE_PORT") or (p.port if p else 47231)))
    bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph)
    bpy.app.handlers.load_post.append(_on_load)
    bpy.app.timers.register(_tick, first_interval=0.2, persistent=True)
    bpy.app.timers.register(_fix_scales, first_interval=0.1)
    bpy.app.timers.register(_restore_orbit, first_interval=0.3)
    draw.register()


def unregister():
    draw.unregister()
    if bpy.context.scene and getattr(bpy.context.scene, "fitbridge", None) is not None:
        thumb.tilt_lock(bpy.context, False)                   # never leave the user's orbit style borrowed
    for t in (_tick, _fix_scales, _restore_orbit):
        if bpy.app.timers.is_registered(t):
            bpy.app.timers.unregister(t)
    for lst, fn in ((bpy.app.handlers.depsgraph_update_post, _on_depsgraph), (bpy.app.handlers.load_post, _on_load)):
        if fn in lst:
            lst.remove(fn)
    server.stop()
    del bpy.types.Scene.fitbridge
    del bpy.types.Object.fitbridge_thumb
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
