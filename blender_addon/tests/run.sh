#!/bin/sh
# Runs the FitBridge tests in a throwaway background Blender (fresh HOME, cache and port; your real profile is untouched).
BLENDER="${BLENDER:-/Applications/Blender.app/Contents/MacOS/Blender}"
T="$(mktemp -d)"
HERE="$(cd "$(dirname "$0")" && pwd)"
HOME="$T" BLENDER_USER_RESOURCES="$T/blender" FITBRIDGE_CACHE="$T/cache" FITBRIDGE_PORT="${FITBRIDGE_PORT:-47298}" \
  "$BLENDER" -b --factory-startup --python-exit-code 1 --python "$HERE/test_fitbridge.py"
code=$?
rm -rf "$T"
exit $code
