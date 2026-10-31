"""Lock tilt, with FitBridge installed as a real extension (it lives in the addon preferences): Thumbnail view borrows
Turntable orbiting and gives the user's own orbit style back; a style left borrowed by a crash is restored on start."""
import sys

import bpy

MOD = "bl_ext.user_default.fitbridge"


def fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


bpy.ops.preferences.addon_enable(module=MOD)
fb = sys.modules[MOD]
inputs = bpy.context.preferences.inputs
p = bpy.context.preferences.addons[MOD].preferences
inputs.view_rotate_method = "TRACKBALL"
st = bpy.context.scene.fitbridge
st.thumb_view = True
if inputs.view_rotate_method != "TURNTABLE" or p.saved_orbit != "TRACKBALL":
    fail(f"Lock tilt should orbit as Turntable while framing: {inputs.view_rotate_method} {p.saved_orbit!r}")
p.lock_tilt = False
if inputs.view_rotate_method != "TRACKBALL":
    fail("switching Lock tilt off gives Trackball back at once")
p.lock_tilt = True
if inputs.view_rotate_method != "TURNTABLE":
    fail("and on again")
st.thumb_view = False
if inputs.view_rotate_method != "TRACKBALL" or p.saved_orbit:
    fail("leaving Thumbnail view gives the user's orbit style back")
# a crash mid-framing left Turntable borrowed: the next start puts Trackball back
p.saved_orbit = "TRACKBALL"
inputs.view_rotate_method = "TURNTABLE"
fb._restore_orbit()
if inputs.view_rotate_method != "TRACKBALL" or p.saved_orbit:
    fail("a borrowed orbit style should be restored on start")
# disabling FitBridge mid-framing never leaves it borrowed
st.thumb_view = True
bpy.ops.preferences.addon_disable(module=MOD)
if inputs.view_rotate_method != "TRACKBALL":
    fail("disabling FitBridge while framing must give the orbit style back")
print("PASS tilt lock: Turntable while framing, Trackball back on leave / toggle / crash / disable")
