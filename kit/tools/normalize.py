"""Generator GLB -> kit item: real metres, bottom-centre origin, front facing -Y, textures <= 2K, plus a LOD1.

One item:
    blender -b --factory-startup --python kit/tools/normalize.py -- \
        --glb assets/gen/meshy/<id>/model.glb --gen meshy --id <id> --out out/items/<id> \
        (--h 0.32 | --fit 0.40 0.30 0.32) [--yaw 0] [--tex 2048] [--max-tris 100000] [--lod1 0.25]

Many items in ONE Blender session, and kit.json written in the same pass (the fast path):
    blender -b --factory-startup --python kit/tools/normalize.py -- --jobs scenes/kit/prep.json \
        [--items out/items] [--kit out/kit.json] [--tex 2048] [--max-tris 100000] [--lod1 0.25]
  prep.json: {"name": "...", "style": "...", "prompt": "...",
              "items": [{"id", "label", "glb", "gen", "h" | "fit": [x, y, z], "yaw"?, "max_tris"?, "why", "tags"?,
                         "fixes"?}, ...]}
  Items whose GLB is missing are skipped (run it again as more land); kit.json is rewritten from every item that has
  a stats.json, in prep.json's order, so it is always current.

Writes out/items/<id>/<id>.glb, <id>_lod1.glb (about 1/4 the tris, half-size textures) and stats.json (dims_m, tris,
tris_lod1, textures).

--gen tripo rotates the mesh -90 deg about Z first (Tripo's front faces +X; Meshy's already faces -Y). --yaw adds
a further rotation (degrees, about Z) when a model's front is somewhere else. --h scales uniformly to that height.
--fit X Y Z scales each axis to exact dims (for boxy props whose depth the generator invented). A starting point:
copy it into scenes/kit/scripts/ and change it when an item needs more.
"""

import json
import math
import os
import sys

import bpy
from mathutils import Matrix, Vector

argv = sys.argv[sys.argv.index("--") + 1:]


def opt(k, d=None, n=1, cast=str):
    if k not in argv:
        return d
    i = argv.index(k)
    vals = [cast(v) for v in argv[i + 1:i + 1 + n]]
    return vals[0] if n == 1 else vals


TEX = opt("--tex", 2048, cast=int)
MAX_TRIS = opt("--max-tris", 100000, cast=int)
LOD1 = opt("--lod1", 0.25, cast=float)


def bbox(ob):
    vs = [v.co for v in ob.data.vertices]
    lo = Vector((min(v.x for v in vs), min(v.y for v in vs), min(v.z for v in vs)))
    hi = Vector((max(v.x for v in vs), max(v.y for v in vs), max(v.z for v in vs)))
    return lo, hi


def tris(ob):
    ob.data.calc_loop_triangles()
    return len(ob.data.loop_triangles)


def decimate(ob, target):
    t = tris(ob)
    if t <= target:
        return
    m = ob.modifiers.new("dec", "DECIMATE")
    m.ratio = target / t
    m.use_collapse_triangulate = True
    with bpy.context.temp_override(object=ob, active_object=ob):
        bpy.ops.object.modifier_apply(modifier=m.name)


def export(path):
    bpy.ops.export_scene.gltf(filepath=path, export_format="GLB", use_selection=True, export_apply=True,
                              export_image_format="AUTO", export_yup=True)


def normalize(glb, gen, iid, out, h=None, fit=None, yaw=0.0, max_tris=MAX_TRIS):
    glb, out = os.path.abspath(glb), os.path.abspath(out)
    os.makedirs(out, exist_ok=True)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=glb)
    meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]
    if not meshes:
        raise RuntimeError(f"no meshes in {glb}")
    if gen.startswith("tripo"):
        # Tripo's orientation=align_image puts a Z rotation (~+-20-30 deg) on the node to match the reference photo's
        # three-quarter view; drop it so the mesh is in Tripo's canonical frame (front +X) before the -90 below
        for o in bpy.context.scene.objects:
            o.rotation_mode = "QUATERNION"
            o.rotation_quaternion = (1, 0, 0, 0)
        bpy.context.view_layer.update()
    # bake every transform (glTF parents, quaternion rotations) into the mesh data, then join into one object
    for o in bpy.context.scene.objects:
        o.select_set(False)
    for o in meshes:
        mw = o.matrix_world.copy()
        o.parent = None
        o.data.transform(mw)
        o.matrix_world = Matrix.Identity(4)
    for o in [o for o in bpy.context.scene.objects if o.type != "MESH"]:
        bpy.data.objects.remove(o)
    vl = bpy.context.view_layer
    for o in meshes:
        o.select_set(True)
    vl.objects.active = meshes[0]
    if len(meshes) > 1:
        bpy.ops.object.join()
    ob = vl.objects.active
    ob.name = ob.data.name = iid
    ob.rotation_mode = "XYZ"
    # orientation: rotate the mesh data, never the object
    rot = (-90.0 if gen.startswith("tripo") else 0.0) + (yaw or 0.0)
    if rot:
        ob.data.transform(Matrix.Rotation(math.radians(rot), 4, "Z"))
    # decimate before anything else touches 2M-tri Tripo meshes
    decimate(ob, max_tris)
    lo, hi = bbox(ob)
    size = hi - lo
    if fit:
        s = Vector([fit[i] / max(size[i], 1e-6) for i in range(3)])
    elif h:
        s = Vector([h / max(size.z, 1e-6)] * 3)
    else:
        s = Vector((1, 1, 1))
    ob.data.transform(Matrix.Translation(-Vector(((lo.x + hi.x) / 2, (lo.y + hi.y) / 2, lo.z))))
    ob.data.transform(Matrix.Diagonal((s.x, s.y, s.z, 1)))
    lo, hi = bbox(ob)
    # textures: downscale anything over TEX, and repack ONLY those (pack() on an untouched packed image drops its data)
    textures = {}
    for img in bpy.data.images:
        if img.size[0] == 0:
            continue
        w, hh = img.size
        if max(w, hh) > TEX:
            f = TEX / max(w, hh)
            img.scale(max(1, int(w * f)), max(1, int(hh * f)))
            img.pack()
        textures[img.name] = list(img.size)
    bpy.ops.object.select_all(action="SELECT")
    export(os.path.join(out, f"{iid}.glb"))
    t0 = tris(ob)
    decimate(ob, max(500, int(t0 * LOD1)))
    for img in bpy.data.images:  # LOD1 textures at half resolution
        if img.size[0] > 256:
            img.scale(max(1, img.size[0] // 2), max(1, img.size[1] // 2))
            img.pack()
    export(os.path.join(out, f"{iid}_lod1.glb"))
    stats = {"id": iid, "source_glb": glb, "generator": gen, "rot_deg": rot, "dims_m": [round(v, 4) for v in hi - lo],
             "tris": t0, "tris_lod1": tris(ob), "textures": textures}
    json.dump(stats, open(os.path.join(out, "stats.json"), "w"), indent=1)
    print("NORMALIZED", json.dumps(stats), flush=True)
    return stats


if "--jobs" in argv:
    jobs = json.load(open(opt("--jobs")))
    items_dir = os.path.abspath(opt("--items", "out/items"))
    kit_path = os.path.abspath(opt("--kit", "out/kit.json"))
    force = "--force" in argv
    for j in jobs["items"]:
        out = os.path.join(items_dir, j["id"])
        st = os.path.join(out, "stats.json")
        if not os.path.exists(j["glb"]):
            print("WAITING", j["id"], "(no GLB yet)", flush=True)
            continue
        if not force and os.path.exists(st) and os.path.getmtime(st) > os.path.getmtime(j["glb"]) \
                and os.path.getmtime(st) > os.path.getmtime(opt("--jobs")):
            continue  # already prepped from this GLB and these settings
        try:
            normalize(j["glb"], j.get("gen", "meshy"), j["id"], out, j.get("h"), j.get("fit"), j.get("yaw", 0),
                      j.get("max_tris", MAX_TRIS))
        except Exception as e:  # keep going; report it
            print("FAILED", j["id"], repr(e), flush=True)
    items = []
    for j in jobs["items"]:
        st = os.path.join(items_dir, j["id"], "stats.json")
        if not os.path.exists(st):
            continue
        s = json.load(open(st))
        items.append({"id": j["id"], "label": j.get("label", j["id"]), "dims_m": s["dims_m"], "tris": s["tris"],
                      "tris_lod1": s["tris_lod1"], "generator": j.get("gen", s["generator"]), "why": j.get("why", ""),
                      "tags": j.get("tags", []), "fixes": j.get("fixes", [])})
    kit = {k: jobs.get(k, "") for k in ("name", "style", "prompt")}
    kit["items"] = items
    os.makedirs(os.path.dirname(kit_path), exist_ok=True)
    json.dump(kit, open(kit_path, "w"), indent=1)
    print("KIT", kit_path, len(items), "of", len(jobs["items"]), "items", flush=True)
else:
    normalize(opt("--glb"), opt("--gen", "meshy"), opt("--id"), opt("--out"), opt("--h", None, cast=float),
              opt("--fit", None, n=3, cast=float), opt("--yaw", 0.0, cast=float))
