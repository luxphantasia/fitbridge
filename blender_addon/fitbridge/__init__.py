# FitBridge (Blender half): fit a rigid Roblox UGC accessory on an R15 mannequin with a live ghost bounds box and
# live spec checks, frame Roblox's thumbnail camera, then Send to Studio over a localhost bridge, where the
# FitBridge Studio plugin builds the finished Accessory. Contract: docs/PROTOCOL.md and spec/ugc_spec.json.

import os
import time

import bpy
from bpy.props import BoolProperty, EnumProperty, IntProperty, PointerProperty, StringProperty

from . import draw, evaluate, rig, server, spec, upload

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
        rig.thumbnail_on(context, evaluate.LIVE["result"])
    else:
        rig.thumbnail_off(context)


def _poll_mesh(self, ob):
    return ob.type == "MESH" and not ob.get("fitbridge_rig")


class FB_Settings(bpy.types.PropertyGroup):
    body_scale: EnumProperty(name="Body scale", items=[(s, s, spec.SPEC["body_scales"][s]["label"]) for s in spec.SCALES],
                             default="Normal", update=_scale_changed)
    acc_type: EnumProperty(name="Type", items=[(t, t, "") for t in spec.RIGID_TYPES], update=_type_changed)
    attachment: EnumProperty(name="Attachment", items=_attachment_items, update=mark_dirty)
    target: PointerProperty(name="Mesh", type=bpy.types.Object, poll=_poll_mesh, update=mark_dirty,
                            description="The accessory mesh (empty = the active object)")
    item_name: StringProperty(name="Name", description="Accessory name in Studio (empty = the object's name)")
    show_box: BoolProperty(name="Ghost box", default=True, description="Draw the attachment's bounds box")
    override: BoolProperty(name="Override checks (testing)", default=False,
                           description="Allow Send to Studio even when a blocking check fails")
    thumb_view: BoolProperty(name="Thumbnail view", default=False, update=_thumb_changed,
                             description="Roblox's thumbnail camera: 70 deg vertical FOV, square 420 render, "
                                         "mannequin hidden")
    thumb_camera: PointerProperty(name="Thumbnail camera", type=bpy.types.Object)
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
    bl_label = "Reset camera"
    bl_description = "Put the thumbnail camera back 6 studs in front of the accessory"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        res = evaluate.evaluate(context)
        if res.get("fit") is None:
            self.report({"ERROR"}, "Fit a mesh first")
            return {"CANCELLED"}
        rig.reset_camera(context, res)
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
        if cur == "R15":                                         # body scales only exist for the R15
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

        box = lay.box()
        box.label(text="Thumbnail", icon="CAMERA_DATA")
        row = box.row(align=True)
        row.prop(st, "thumb_view", toggle=True, icon="VIEW_CAMERA")
        row.operator("fitbridge.reset_camera", text="", icon="FILE_REFRESH")
        if st.thumb_view:
            box.label(text="Roblox may zoom slightly closer", icon="INFO")

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
                evaluate.evaluate(bpy.context)
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


@bpy.app.handlers.persistent
def _on_load(*_args):
    _dirty["flag"] = True
    evaluate.LIVE["result"] = None


classes = (FB_Settings, FB_Prefs, FB_OT_spawn_r15, FB_OT_spawn_r6, FB_OT_show_rig, FB_OT_reset_camera, FB_OT_send,
           FB_OT_restart_server, FB_PT_main)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.fitbridge = PointerProperty(type=FB_Settings)
    server.state["hello"]["blender_version"] = bpy.app.version_string
    p = prefs()
    server.start(int(os.environ.get("FITBRIDGE_PORT") or (p.port if p else 47231)))
    bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph)
    bpy.app.handlers.load_post.append(_on_load)
    bpy.app.timers.register(_tick, first_interval=0.2, persistent=True)
    draw.register()


def unregister():
    draw.unregister()
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
    for lst, fn in ((bpy.app.handlers.depsgraph_update_post, _on_depsgraph), (bpy.app.handlers.load_post, _on_load)):
        if fn in lst:
            lst.remove(fn)
    server.stop()
    del bpy.types.Scene.fitbridge
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
