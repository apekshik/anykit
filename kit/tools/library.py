"""kit.blend: a Blender asset library holding every kit item, each marked as an asset with a preview, label, tags
and its LOD1 alongside.

    blender -b --factory-startup --python kit/tools/library.py -- --kit out/kit.json --items out/items --out out/kit.blend

Each item is one object named <id> (LOD0) in a collection named after the kit, laid out in a row so the file is also
browsable; <id>_lod1 sits in a hidden "LOD1" collection. Everything is packed. A blender_assets.cats.txt with one
catalog for the kit is written next to the .blend, and every asset is assigned to it.
"""

import json
import os
import sys
import uuid

import bpy
from mathutils import Vector

argv = sys.argv[sys.argv.index("--") + 1:]
opt = lambda k, d: argv[argv.index(k) + 1] if k in argv else d
KIT = os.path.abspath(opt("--kit", "out/kit.json"))
ITEMS = os.path.abspath(opt("--items", "out/items"))
OUT = os.path.abspath(opt("--out", "out/kit.blend"))
kit = json.load(open(KIT))
name = kit.get("name") or "Kit"

bpy.ops.wm.read_factory_settings(use_empty=True)
sc = bpy.context.scene
main = bpy.data.collections.new(name)
sc.collection.children.link(main)
lods = bpy.data.collections.new("LOD1")
sc.collection.children.link(lods)
cat_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, "anykit/" + name))


def load(path, obname, coll):
    before = set(bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=path)
    new = [o for o in bpy.data.objects if o not in before]
    meshes = [o for o in new if o.type == "MESH"]
    for o in bpy.context.selected_objects:
        o.select_set(False)
    for o in meshes:
        mw = o.matrix_world.copy()
        o.parent = None
        o.matrix_world = mw
        o.select_set(True)
    bpy.context.view_layer.objects.active = meshes[0]
    if len(meshes) > 1:
        bpy.ops.object.join()
    ob = bpy.context.view_layer.objects.active
    bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
    for o in new:
        if o.name in bpy.data.objects and o is not ob:
            bpy.data.objects.remove(o)
    ob.name = ob.data.name = obname
    for c in ob.users_collection:
        c.objects.unlink(ob)
    coll.objects.link(ob)
    return ob


x = 0.0
for it in kit["items"]:
    d = os.path.join(ITEMS, it["id"])
    ob = load(os.path.join(d, f"{it['id']}.glb"), it["id"], main)
    lo = min((ob.matrix_world @ Vector(c)).x for c in ob.bound_box)
    hi = max((ob.matrix_world @ Vector(c)).x for c in ob.bound_box)
    ob.location.x = x - lo
    x += (hi - lo) + 0.1
    ob.asset_mark()
    ad = ob.asset_data
    ad.description = it.get("label", it["id"])
    ad.catalog_id = cat_id
    for t in it.get("tags", []) + [name]:
        ad.tags.new(t, skip_if_exists=True)
    for k in ("dims_m", "tris", "generator"):
        if k in it:
            ob[k] = it[k]
    ob["label"] = it.get("label", it["id"])
    lod_path = os.path.join(d, f"{it['id']}_lod1.glb")
    if os.path.exists(lod_path):
        lod = load(lod_path, f"{it['id']}_lod1", lods)
        lod.location = ob.location
        ob["lod1"] = lod.name
    with bpy.context.temp_override(id=ob):
        try:
            bpy.ops.ed.lib_id_generate_preview()
        except Exception as e:  # previews need a GPU context in some builds; the asset still works without one
            print("preview skipped", it["id"], e)

lods.hide_render = True
lods.hide_viewport = True
bpy.ops.file.pack_all()
os.makedirs(os.path.dirname(OUT), exist_ok=True)
bpy.ops.wm.save_as_mainfile(filepath=OUT, compress=True)
with open(os.path.join(os.path.dirname(OUT), "blender_assets.cats.txt"), "w") as f:
    f.write(f"VERSION 1\n\n{cat_id}:{name}:{name}\n")
print("LIBRARY", OUT, len(kit["items"]), "assets")
