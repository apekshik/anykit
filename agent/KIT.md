# What makes a kit good

A kit is judged as a set. One beautiful item and one off-style item is a failed kit; eight solid items that all look
like the same world is a good one. These are the things that decide it, what the earlier builds (four full scenes and
~150 generated props, then the first kits) learned about the generators, and the process for each item.

## The loop

What separated good results from first drafts was never a model or a setting. It was this loop, run many times:

1. **Render it and look.** After every change, render the views that matter (small and fast while working) and open
   the images. A script that ran without errors proves nothing about how the item looks.
2. **Name the problem precisely.** "The handset floats 2 cm above the cradle", not "the phone looks off".
3. **Write the fix as code** in `scenes/kit/scripts/`, so a rebuild keeps it.
4. **Check the fix from a view that shows it.** A three-quarter thumbnail hides a floating handset; a close, low view
   shows it at once.
5. **Get an outside opinion.** The agent that built a kit stops seeing its flaws; the critic catches them.

Keep `scenes/kit/NOTES.md`: one line per thing that cost you time.

## Cohesion comes from the references, not the 3D models

- Tripo and Meshy copy the reference image. If the references don't match each other, nothing later will fix it.
  Generate every item reference with the customer's style image as an input image (`kit/gen.mjs edit`), with one
  shared style paragraph (era, palette, materials, wear, fictional maker) pasted into every prompt.
- Check the references together, on one sheet, before any 3D: same camera, same light, same level of wear, same
  palette. Regenerate the outliers. This is the cheapest place to fix cohesion.
- The style reference is often a whole scene. Its items are a guide to the world, not a list to copy unless the
  customer asked for exactly those things.

## Per item: reference -> 3D -> compare -> prep

### 1. Reference

Each item's prompt describes ONE object, its condition and its materials, then the shared style paragraph, then this
scan template (the exact text the earlier builds used; it gives the 3D generators a clean, fully visible object):

> No real-world brand names or logos. Product reference photograph for 3D scanning. The entire object fully visible
> and centered, three-quarter front view from slightly above, plain seamless light-grey background, soft even diffuse
> studio lighting, no harsh shadows, no other objects, no hands, realistic used and worn condition, sharp focus,
> true-to-life materials.

(Swap "realistic used and worn condition" for what the `wear` dial says.) References come back with real brands even
when told not to: check every one, and regenerate those that carry one as `<id>_nb` with the kit's fictional maker.

**Gate:** the reference sheet: the right objects, one style, no real brands, nothing cropped.

### 2. 3D, both generators

Send the same reference to **both** Tripo H3.1 (`--model tripo-h31`) and Meshy 7.1 (`--model meshy`) with
`generate/fal.mjs 3d`. About 10-20% of jobs fail transiently (504, ECONNRESET, Meshy timeouts after about 10 min):
re-run the same ids. The script skips GLBs that already exist and picks up a job left in flight (`pending.json`), so
re-running is free; don't start a second pass over ids that are still running in another shell.

### 3. Compare and pick

Render reference | Tripo beauty | Tripo clay | Meshy beauty | Meshy clay under the same light (`kit/tools/compare.py`)
and pick per item by looking. Record the pick and a one-line why. What ~150 props showed:

- **Meshy 7.1** wins about 70% of the time: small consumer items, sharper printed labels. Its front faces -Y.
- **Tripo H3.1** wins big hard-surface heroes where real geometry matters (knobs, bezels, dents). Its front faces +X,
  and with `orientation: align_image` its glTF node also carries a +-20-30 deg Z rotation that matches the reference
  photo's three-quarter view: drop it before turning the mesh (`normalize.py` and `compare.py` do). Raw it is ~2M
  tris: generate with `--face-limit` (150000 heroes, 60000 small items).
- **Both** bake glass opaque, fake screens into the texture, invent depth for boxy things and put real brands on
  labels. Expect all four.
- Meshy speckles flat panels. Look at the clay render, not just the beauty render: it shows which details are real
  geometry.

**Gate:** every item has a winner, or is regenerated because both are bad.

### 4. Prep

`kit/tools/normalize.py` turns every pick into a kit item:
- **Front faces -Y.** Tripo's front faces +X, so it is turned -90 deg; Meshy already faces -Y. The mesh data is
  rotated, not the object, and framing uses vertex bounds, not `bound_box`. Decide from the compare renders which
  way each model really faces; `--yaw` fixes anything that isn't front -Y after the generator's rotation.
- **Real scale** from `h` (or `fit` dims for boxy things, because generators invent depth).
- **Bottom-centre origin.**
- **Triangles** within the `detail` budget, and a LOD1 at about a quarter.
- **Textures** at 2K or less.

Gotchas, if you write your own prep or fix scripts:
- glTF imports use QUATERNION rotation mode, so `rotation_euler` is silently ignored. Set `rotation_mode = "XYZ"` first.
- glTF images arrive packed. Calling `pack()` again on an unmodified packed image drops its data and the item renders
  magenta. Repack only the images you changed.
- Pass absolute paths to Blender, or relative paths resolve against the wrong directory.

### 5. Fixes by script

- **Glass:** give the glass part a real transmissive material (mask by UV island or by material) and keep labels and
  contents opaque.
- **Screens:** a screen baked into the texture reads as a grey slab. Make it emissive with content in the kit's one
  invented world, at the screen's real aspect ratio; fit a CRT's screen geometry to the curved tube.
- **Brands, without regenerating:** render ortho views and mark the brand rectangles. Raycast the rectangles to
  texels, fill each with the label's ground colour plus grain, and flatten the same texels in the normal map, or
  embossed text survives. Or regenerate the item with the fictional maker.
- **Assembly:** cords plugged in at both ends, handsets on their cradles, lamp heads on their arms, lids on their
  hinges, nothing hovering or intersecting. Snap or bridge cord ends to their sockets, re-seat parts, remove stray
  floating bits.

## Scale

- Every item gets a real height (or dims for boxy things) before prep. Look up typical sizes rather than guessing:
  a desk phone is ~0.12 m tall, a coffee mug ~0.095 m, a ring binder ~0.32 m, a desk lamp ~0.45 m.
- Check scale in the lineup: items side by side must make sense relative to each other and to the scale bar.

## Presentation

- The lineup is the kit's cover. Items sorted for a readable silhouette rhythm, a scale bar, labels legible, nothing
  cut off, light that shows the materials. Thumbnails: three-quarter front, filling the frame.
- Look at every render you make. A script that ran proves nothing.

## Budget and time

| Item | Cost |
|---|---|
| Style-conditioned reference (`kit/gen.mjs edit`) | ~$0.30 |
| Tripo H3.1 | ~$0.60 |
| Meshy 7.1 | ~$0.80 |
| Per item, both generators | **~$1.70** |

Every call is logged to `generate/spend.jsonl`; `node generate/fal.mjs spend` totals it. Most of a kit's time is
waiting on the generators and launching Blender: stream and batch (see the prompt's Speed section).
