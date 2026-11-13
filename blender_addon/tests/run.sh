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
if [ $code -eq 0 ]; then                       # a fresh start of that same throwaway profile (no factory settings)
  HOME="$T" BLENDER_USER_RESOURCES="$T/blender" FITBRIDGE_CACHE="$T/cache3" FITBRIDGE_PORT="${FITBRIDGE_PORT:-47298}" \
    "$BLENDER" -b --python-exit-code 1 --python "$HERE/test_centre_dot_kept.py"
  code=$?
fi
if [ $code -eq 0 ]; then                       # the Listing tab against a fake Studio, and the drawn panel's layout
  HOME="$T" BLENDER_USER_RESOURCES="$T/blender" FITBRIDGE_CACHE="$T/cache4" FITBRIDGE_PORT="${FITBRIDGE_PORT:-47298}" \
    FITBRIDGE_LISTING_POLL=0.1 "$BLENDER" -b --python-exit-code 1 --python "$HERE/test_listing.py"
  code=$?
fi
if [ $code -eq 0 ]; then                       # Colour versions against a fake Anthropic API
  HOME="$T" BLENDER_USER_RESOURCES="$T/blender" FITBRIDGE_CACHE="$T/cache6" FITBRIDGE_PORT="${FITBRIDGE_PORT:-47298}" \
    "$BLENDER" -b --python-exit-code 1 --python "$HERE/test_colours.py"
  code=$?
fi
if [ $code -eq 0 ]; then                       # the upload lock and the build-only set against a fake Open Cloud
  HOME="$T" BLENDER_USER_RESOURCES="$T/blender" FITBRIDGE_CACHE="$T/cache7" FITBRIDGE_PORT="${FITBRIDGE_PORT:-47298}" \
    "$BLENDER" -b --python-exit-code 1 --python "$HERE/test_no_upload.py"
  code=$?
fi
if [ $code -eq 0 ]; then                       # the marks on the mannequins (a face in front, a stripe down the back)
  HOME="$T" BLENDER_USER_RESOURCES="$T/blender" FITBRIDGE_CACHE="$T/cache8" FITBRIDGE_PORT="${FITBRIDGE_PORT:-47298}" \
    "$BLENDER" -b --python-exit-code 1 --python "$HERE/test_marks.py"
  code=$?
fi
if [ $code -eq 0 ]; then                       # the Reduce button, with a built Modeling Suite (private) if you have one
  if [ -n "$MODELING_SUITE_BUILD" ] && [ -d "$MODELING_SUITE_BUILD" ]; then
    mkdir -p "$T/blender/extensions/user_default"
    cp -R "$MODELING_SUITE_BUILD" "$T/blender/extensions/user_default/modeling_suite"
    HOME="$T" BLENDER_USER_RESOURCES="$T/blender" FITBRIDGE_CACHE="$T/cache5" FITBRIDGE_PORT="${FITBRIDGE_PORT:-47298}" \
      "$BLENDER" -b --python-exit-code 1 --python "$HERE/test_reduce.py"
    code=$?
  else
    echo "skipped test_reduce.py: set MODELING_SUITE_BUILD to a built modeling_suite folder"
  fi
fi
rm -rf "$T"
exit $code
