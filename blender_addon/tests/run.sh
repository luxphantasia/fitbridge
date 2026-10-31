#!/bin/sh
# Runs the FitBridge tests in a throwaway background Blender (fresh HOME, cache and port; your real profile is untouched).
BLENDER="${BLENDER:-/Applications/Blender.app/Contents/MacOS/Blender}"
T="$(mktemp -d)"
HERE="$(cd "$(dirname "$0")" && pwd)"
HOME="$T" BLENDER_USER_RESOURCES="$T/blender" FITBRIDGE_CACHE="$T/cache" FITBRIDGE_PORT="${FITBRIDGE_PORT:-47298}" \
  "$BLENDER" -b --factory-startup --python-exit-code 1 --python "$HERE/test_fitbridge.py"
code=$?
if [ $code -eq 0 ]; then                       # the parts that need FitBridge installed as a real extension
  HOME="$T" BLENDER_USER_RESOURCES="$T/blender" "$BLENDER" --factory-startup --command extension build \
    --source-dir "$HERE/../fitbridge" --output-dir "$T" >/dev/null 2>&1
  HOME="$T" BLENDER_USER_RESOURCES="$T/blender" "$BLENDER" --factory-startup --command extension install-file \
    -r user_default -e "$T"/fitbridge-*.zip >/dev/null 2>&1
  HOME="$T" BLENDER_USER_RESOURCES="$T/blender" FITBRIDGE_CACHE="$T/cache2" FITBRIDGE_PORT="${FITBRIDGE_PORT:-47298}" \
    "$BLENDER" -b --factory-startup --python-exit-code 1 --python "$HERE/test_tilt_lock.py"
  code=$?
fi
rm -rf "$T"
exit $code
