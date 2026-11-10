"""Where FitBridge is running: as its own extension, or as a tool inside a luxphantasia suite (the UGC Suite).

Inside a suite the package is <suite>.tools.fitbridge: Blender only knows the suite as an add-on, so FitBridge's
settings live in the suite's preferences (suitekit hosts them as `tool_fitbridge`) and its user folder is the suite's.
"""
import bpy

PKG = __package__                                   # this add-on's own package (host.py sits at its top level)
IN_SUITE = ".tools." in PKG
ROOT = PKG.split(".tools.", 1)[0] if IN_SUITE else PKG   # the module Blender knows as an add-on/extension
TOOL_ID = PKG.rsplit(".", 1)[-1]


def prefs(context=None):
    """FitBridge's settings (the AddonPreferences on its own, the suite-hosted PropertyGroup in a suite), or None"""
    a = (context or bpy.context).preferences.addons.get(ROOT)
    if a is None:
        return None
    return getattr(a.preferences, f"tool_{TOOL_ID}", None) if IN_SUITE else a.preferences


def user_dir():
    """the extension's user folder (the suite's, plus a fitbridge subfolder, inside a suite)"""
    p = bpy.utils.extension_path_user(ROOT, path="fitbridge" if IN_SUITE else "", create=True)
    return p


def studio():
    """(site, key) for Paradyse Studio's Blender API: FitBridge's own settings, or inside the UGC Suite the key Tripo
    Loop already has. ("", "") when there is none: the Listing tab stays hidden."""
    import os
    p = prefs()
    key = (getattr(p, "studio_key", "") or "").strip() if p is not None else ""
    url = (getattr(p, "studio_url", "") or "").strip() if p is not None else ""
    if not key and IN_SUITE:
        a = bpy.context.preferences.addons.get(ROOT)
        t = getattr(a.preferences, "tool_tripo_loop", None) if a is not None else None
        if t is not None:
            key = (getattr(t, "studio_key", "") or "").strip()
            url = url or (getattr(t, "studio_url", "") or "").strip()
    url = (os.environ.get("FITBRIDGE_STUDIO_URL") or url or "https://paradyse.art").rstrip("/")
    return (url, key) if key else ("", "")
