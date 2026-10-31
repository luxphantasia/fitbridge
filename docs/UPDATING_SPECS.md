# Keeping specs current

Three layers, from most to least current:

1. **Live rules (automatic).** Whenever the Studio plugin is connected, it sends `AvatarCreationService:GetValidationRules()` to Blender. Blender then checks against exactly what Roblox enforces today. The Blender panel shows "Rules: live".
2. **`spec/ugc_spec.json` (bundled fallback).** Used when Studio isn't open. It also holds what live rules don't give: per-body-scale boxes, labels, check severities and thumbnail settings.
3. **Weekly check.** `python3 tools/spec_check.py` compares the spec file with:
   - the Markdown version of Roblox's [accessory specifications](https://create.roblox.com/docs/art/accessories/specifications) (bounds per type, attachment and scale, offsets, the triangle budget, max texture size);
   - hashes of the Studio validation files our checks copy (`spec/studio_validation_hashes.json`). Studio updates weekly, and a changed hash means re-read that file.

It exits 1 when something changed, and never edits the spec itself.

## Approving a change
A scheduled agent runs the check weekly. When it finds changes, it opens a pull request that edits `spec/ugc_spec.json` (and bumps `updated`). The PR lists every changed number with its docs line. You merge it. Once the repo exists, both plugins will fetch the spec from its raw URL at startup, falling back to their bundled copy, so nobody has to reinstall. Today the Studio plugin bundles the spec at build time.

After re-reading a changed validation file and updating our checks, refresh the hashes with `python3 tools/spec_check.py --record-hashes`.

## Known docs quirk
The Slender Hair depth says "1.892 front, 1.189 behind", which can't add up to 2.08. The spec assumes 0.892, and the checker reports it as a note, not a change.
