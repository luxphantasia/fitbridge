"""Run after test_tilt_lock.py saved the throwaway profile with Centre dot on: a new Blender starts with it still on."""
import sys

import bpy

MOD = "bl_ext.user_default.fitbridge"
a = bpy.context.preferences.addons.get(MOD)
if a is None or not a.preferences.centre_dot:
    print("FAIL: Centre dot should still be on after a restart")
    sys.exit(1)
print("PASS centre dot remembered after a restart")
