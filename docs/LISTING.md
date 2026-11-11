# The Listing tab

FitBridge's third tab asks a Paradyse Studio site (the owner's own web tool, not Roblox Studio) to write a Roblox item's
listing: three name options and a description. It only appears when a Studio Blender key is set in FitBridge's
preferences (inside the UGC Suite, Tripo Loop's key is used when FitBridge's own is empty). Without a key nothing is
ever sent. Code: `blender_addon/fitbridge/listing.py`, the tab itself in `panel.py`, tests in `tests/test_listing.py`
(against a local fake, never the real site).

## What happens

1. **Pictures.** The selected accessory alone, from the front, three-quarter, side and back (768 px JPEG, Workbench,
   through the thumbnail's own lens). "Flat colours" (the default) shows the texture as it is; "Soft light" is Blender's
   studio light. Your render settings are put back afterwards.
2. **Send.** The four pictures, the optional note, and `no_colour=1` when other objects share the mesh (recolours: Send
   all colours adds the colour word itself, so the names must not carry one).
3. **Wait.** Blender asks every 3 seconds until the listing is done (up to 10 minutes). If the file is closed
   meanwhile, "See if it's done" picks it up again.
4. **Use.** A click on a name makes it FitBridge's Name (Send to Studio uses it); the description has a Copy. For a
   known character there is a Real name / Adjacent name switch. Closing lines are ticked per listing, and the site
   remembers the choice for the next one. The site's category is offered as FitBridge's Type, never set by itself.

The listing is kept on the object (custom property `fitbridge_listing`, JSON), so it is saved with the file.

## The contract

Under `<site>/api/blender/v1`, with `Authorization: Bearer <Blender key>`. The key travels in that header only.

| Call | Body | Answer |
|---|---|---|
| `GET /listings/hello` | | `{ ready, why, chips: [{id, name, text}], chips_on: [id], max_pictures }` |
| `POST /listings` | multipart: `image` (1 to 6), `note`, `no_colour` | `201 { listing }` |
| `GET /listings/<id>` | | `{ listing }` |
| `POST /listings/<id>/chips` | `{ ids }` | `{ listing }` |

```
listing = { id, status: "running" | "done" | "failed", step, error, url, chip_ids,
            item: null | { what, category, category_label, character: {name, series} | null,
                           alias: {name, strength} | null,
                           real, adjacent, plain: null | { names: [..], description },
                           price: null | { low, median, high, from } } }
```

- `item` is the listing's one item, and null until `status` is `"done"`. A character has `real` and maybe `adjacent`
  (none was found: Blender shows `real`, with no switch); anything else has only `plain`. `names` holds 1 to 3.
- `error` can be set on a done listing (a later fix on the site failed): Blender shows it and keeps the item.
- `no_colour` is an instruction to the name writer, not a filter: the item's own colour stays out of the names, a colour
  that is part of what the thing is ("Black Cat Ears") stays. The listing echoes it back as `no_colour`.
- `category` may be a layered-clothing key FitBridge has no rigid type for; then no Type is offered.
- Descriptions arrive with the ticked closing lines already added.
- `url` is the listing's page on the site. Blender only opens it when it starts with the site's own address.
- Blender starts on `adjacent` when there is one, as the site's page does. An adjacent name lowers an item's
  visibility; nothing here says an item is safe to sell.
- Errors are `{ error }` in words, shown as they are. 401 and 403 mean the key was refused.
- The site refuses any request over 1 MB ("That request is too big."), so FitBridge keeps the four pictures under
  900 KB together: JPEG at quality 90, and for a texture too busy to fit, taken again at a lower quality and size
  (`SHOT_TRIES` in `listing.py`). PNGs of a textured item were 2.3 MB and never arrived.
