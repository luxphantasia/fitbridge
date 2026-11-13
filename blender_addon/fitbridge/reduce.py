"""Reduce to Roblox's triangle limit in one click, with LODForge.

LODForge (the Modeling Suite's decimator) is never copied into FitBridge: the repo is public and LODForge is sold.
The button drives the installed LODForge's own Decimate with a fixed preset (Lux's, 2026-11-12), then puts your
LODForge settings and selection back. Without the Modeling Suite the button is greyed out and says why.

What it does to the accessory (Lux's choices): the mesh is replaced in place (Ctrl+Z brings it back), modifiers are
baked in (Armature-type ones stay), every colour copy sharing the mesh gets the same reduced mesh, and Auto mode's
answer is kept, since the shape hasn't changed (no second paid look).
"""

import json

import bpy

from . import auto, batch, evaluate, spec

PRESET = {                                        # LODForge's panel as in Lux's screenshot, Replace Original on
    "target_mode": "COUNT",
    "priority": "AUTO",
    "clean_mesh": False,
    "transfer_data": True,
    "transfer_normals": True,
    "sharp_by_angle": False,
    "relax_if_needed": True,
    "clean_creases": False,
    "protect_group": "",
    "replace": True,
}
NEEDS = "Needs the Modeling Suite (LODForge) turned on"


def target():
    """one under Roblox's limit (3,999 for the 4,000 rule)"""
    return int(spec.limits()["max_triangles"]) - 1


def label():
    return f"Reduce to {target():,}"


def available():
    if not hasattr(bpy.types.Scene, "lodforge"):
        return False
    try:
        bpy.ops.lodforge.decimate.get_rna_type()
    except (KeyError, AttributeError):
        return False
    return True


def tri_check(res):
    """the failing triangles check, or None"""
    if res is None:
        return None
    return next((c for c in res["checks"] if c["id"] == "triangles" and c["status"] == "fail"), None)


# --------------------------------------------------------------------------------------- keeping Auto's answer

def _auto_keys(ob):
    """Auto mode's notes on ob, with the fingerprint they were written for"""
    fp = auto.fingerprint(ob)
    return {"fp": fp, "fit": auto.fit_key(ob, fp)}


def _carry_auto(ob, was):
    """re-stamp Auto's notes for the reduced mesh, so a reduce doesn't cost a second look"""
    now = _auto_keys(ob)
    try:
        d = json.loads(ob.get(auto.ANSWER_KEY, ""))
        if d.get("fingerprint") == was["fp"]:
            d["fingerprint"] = now["fp"]
            ob[auto.ANSWER_KEY] = json.dumps(d)
    except ValueError:
        pass
    try:
        v = json.loads(ob.get(auto.FIT_KEY, ""))
        if v.get("key") == was["fit"]:
            v["key"] = now["fit"]
            ob[auto.FIT_KEY] = json.dumps(v)
    except ValueError:
        pass
    if ob.get(auto.IGNORE_FACING_KEY) == was["fp"]:
        ob[auto.IGNORE_FACING_KEY] = now["fp"]
    if ob.get(auto.IGNORE_FIT_KEY) == was["fit"]:
        ob[auto.IGNORE_FIT_KEY] = now["fit"]


# ------------------------------------------------------------------------------------------------- the operator

def lodforge(context, ob, goal):
    """LODForge's own Decimate on ob with FitBridge's preset, aiming for `goal` triangles; your LODForge settings and
    your selection come back afterwards. True when ob's mesh was replaced by the reduced one."""
    old_me = ob.data
    s = context.scene.lodforge
    saved = {k: getattr(s, k) for k in list(PRESET) + ["count"]}
    sel = [o for o in context.view_layer.objects.selected]
    active = context.view_layer.objects.active
    try:
        for k, v in PRESET.items():
            setattr(s, k, v)
        s.count = goal
        for o in sel:
            o.select_set(False)
        ob.select_set(True)
        context.view_layer.objects.active = ob
        got = bpy.ops.lodforge.decimate()
    finally:
        for k, v in saved.items():
            setattr(s, k, v)
        for o in context.view_layer.objects.selected:
            o.select_set(False)
        for o in sel:
            try:
                o.select_set(True)
            except (RuntimeError, ReferenceError):
                pass
        context.view_layer.objects.active = active
    return "FINISHED" in got and ob.data is not old_me


def _count(context, ob):
    me = ob.evaluated_get(context.evaluated_depsgraph_get()).data
    me.calc_loop_triangles()
    return len(me.loop_triangles)


class FB_OT_reduce_tris(bpy.types.Operator):
    bl_idname = "fitbridge.reduce_tris"
    bl_label = "Reduce to the triangle limit"
    bl_description = "Bring the accessory just under Roblox's triangle limit with LODForge: keeps UVs, normals, " \
                     "weights and colours, replaces the mesh in place (Ctrl+Z undoes it), bakes in modifiers, and " \
                     "gives every colour copy sharing the mesh the same result"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        if not available():
            cls.poll_message_set(NEEDS)
            return False
        if context.mode not in ("OBJECT", "EDIT_MESH"):
            return False
        ob = evaluate.target_object(context)
        return ob is not None and ob.type == "MESH" and not evaluate.selection_problem(context)

    def execute(self, context):
        ob = evaluate.target_object(context)
        if ob is None or ob.type != "MESH":
            return {"CANCELLED"}
        if ob.library or ob.data.library:
            self.report({"ERROR"}, "The accessory is linked from another file, so it can't be replaced")
            return {"CANCELLED"}
        if context.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        goal = target()
        before = _count(context, ob)
        if before <= goal:
            self.report({"INFO"}, f"Already at {before:,} triangles, nothing to do")
            return {"CANCELLED"}

        members = [m for m in batch.family(context, ob) if m is not ob]
        notes = {o.name: _auto_keys(o) for o in [ob] + members}
        old_me = ob.data
        old_name = old_me.name
        if not lodforge(context, ob, goal):
            self.report({"ERROR"}, "LODForge couldn't reduce it (its message is in the Info log)")
            return {"CANCELLED"}

        # the colour copies: same reduced mesh, same modifiers left (the rest are baked into it now)
        keep = {m.name for m in ob.modifiers}
        for m in members:
            m.data = ob.data
            for mod in [mod for mod in m.modifiers if mod.name not in keep]:
                m.modifiers.remove(mod)
        if old_me.users == 0:
            bpy.data.meshes.remove(old_me)
            ob.data.name = old_name
        for o in [ob] + members:
            _carry_auto(o, notes[o.name])

        after = _count(context, ob)
        evaluate.mark_changed()
        lim = goal + 1
        more = f", and {len(members)} colour cop{'ies' if len(members) != 1 else 'y'}" if members else ""
        if after >= lim:
            self.report({"WARNING"}, f"{before:,} → {after:,} triangles{more}: still over {lim:,}")
        else:
            self.report({"INFO"}, f"{before:,} → {after:,} triangles{more}")
        return {"FINISHED"}


classes = (FB_OT_reduce_tris,)
