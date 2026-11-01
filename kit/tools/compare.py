"""Tripo vs Meshy for kit items, in ONE Blender session: one sheet per item, ready the moment its meshes land.

    blender -b --factory-startup --python kit/tools/compare.py -- [--ids a,b] [--gen assets/gen] [--refs refs/kit] \
        [--out scenes/kit/compare] [--res 512] [--samples 24] [--clay workbench|cycles]

Per item: reference | tripo beauty | tripo clay | meshy beauty | meshy clay, written to <out>/<id>.png, with the
tris and dims of each model in <out>/<id>.json. Beauty is a quick Cycles render; clay is Workbench (shows what is
real geometry; `--clay cycles` if Workbench can't open a context). Both models are turned to face -Y first (Tripo
-90 deg), so the views match. Items whose sheet is newer than both GLBs are skipped, so call it again as more land.
"""

import json
import math
import os
import sys

import bpy
import numpy as np
from mathutils import Vector

argv = sys.argv[sys.argv.index("--") + 1:]
opt = lambda k, d: type(d)(argv[argv.index(k) + 1]) if k in argv else d
GEN = os.path.abspath(opt("--gen", "assets/gen"))
REFS = os.path.abspath(opt("--refs", "refs/kit"))
OUT = os.path.abspath(opt("--out", "scenes/kit/compare"))
RES = opt("--res", 512)
SAMPLES = opt("--samples", 24)
CLAY = opt("--clay", "workbench")
IDS = [s for s in opt("--ids", "").split(",") if s]
MODELS = ("tripo-h31", "meshy")
os.makedirs(OUT, exist_ok=True)


def glb(model, iid):
    return os.path.join(GEN, model, iid, "model.glb")


def ids():
    found = set(IDS)
    if not found:
        for m in MODELS:
            d = os.path.join(GEN, m)
            if os.path.isdir(d):
                found |= {i for i in os.listdir(d) if os.path.exists(glb(m, i))}
    return sorted(found)


def reset():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    sc = bpy.context.scene
    sc.render.resolution_x = sc.render.resolution_y = RES
    sc.view_settings.view_transform = "AgX"
    w = bpy.data.worlds.new("w")
    sc.world = w
    w.use_nodes = True
    w.node_tree.nodes["Background"].inputs["Color"].default_value = (0.5, 0.5, 0.52, 1)
    w.node_tree.nodes["Background"].inputs["Strength"].default_value = 0.6
    return sc


def light(name, loc, energy, size):
    d = bpy.data.lights.new(name, "AREA")
    d.energy, d.size = energy, size
    o = bpy.data.objects.new(name, d)
    bpy.context.scene.collection.objects.link(o)
    o.location = loc
    o.rotation_euler = (-Vector(loc)).to_track_quat("-Z", "Y").to_euler()


def bounds(objs):
    """Tight world bounds from the vertices (a rotated object's bound_box is loose: Tripo meshes carry a rotation)."""
    los, his = [], []
    for o in objs:
        co = np.empty(len(o.data.vertices) * 3, np.float32)
        o.data.vertices.foreach_get("co", co)
        m = np.array(o.matrix_world, np.float32)
        w = co.reshape(-1, 3) @ m[:3, :3].T + m[:3, 3]
        los.append(w.min(0))
        his.append(w.max(0))
    return Vector(np.min(los, 0).tolist()), Vector(np.max(his, 0).tolist())


def unpose(objs):
    """Tripo's orientation=align_image puts a Z rotation (about +-20-30 deg) on the glTF node to match the reference
    photo's three-quarter view. Drop it so the mesh sits in Tripo's canonical frame (front +X)."""
    for o in objs:
        o.rotation_mode = "QUATERNION"
        o.rotation_quaternion = (1, 0, 0, 0)
    bpy.context.view_layer.update()


def stats(objs, size):
    t = 0
    for o in objs:
        o.data.calc_loop_triangles()
        t += len(o.data.loop_triangles)
    return {"tris": t, "raw_size": [round(v, 3) for v in size]}


def render_model(model, iid, prefix):
    sc = reset()
    before = set(bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=glb(model, iid))
    new = [o for o in bpy.data.objects if o not in before]
    objs = [o for o in new if o.type == "MESH"]
    if model.startswith("tripo"):
        unpose(new)
    lo, hi = bounds(objs)
    st = stats(objs, hi - lo)
    root = bpy.data.objects.new("root", None)
    sc.collection.objects.link(root)
    for o in new:
        if o.parent is None:
            o.parent = root
    s = 1.0 / max(hi - lo)
    c = (lo + hi) / 2
    root.scale = (s, s, s)
    if model.startswith("tripo"):
        root.rotation_euler.z = math.radians(-90)
    bpy.context.view_layer.update()
    lo, hi = bounds(objs)
    root.location -= Vector(((lo.x + hi.x) / 2, (lo.y + hi.y) / 2, lo.z))
    bpy.context.view_layer.update()
    lo, hi = bounds(objs)
    centre, radius = (lo + hi) / 2, (hi - lo).length / 2
    light("key", (2.2, -2.6, 3.0), 260, 1.6)
    light("fill", (-3.0, -1.5, 1.6), 70, 2.5)
    light("rim", (-1.0, 3.0, 2.5), 160, 1.2)
    cd = bpy.data.cameras.new("cam")
    cd.lens = 70
    cam = bpy.data.objects.new("cam", cd)
    sc.collection.objects.link(cam)
    sc.camera = cam
    az, el = math.radians(-30), math.radians(18)
    cam.location = centre + Vector((math.sin(az) * math.cos(el), -math.cos(az) * math.cos(el), math.sin(el))) \
        * (radius / math.sin(cd.angle / 2) * 1.02)
    cam.rotation_euler = (centre - cam.location).to_track_quat("-Z", "Y").to_euler()
    sc.render.engine = "CYCLES"
    sc.cycles.device = "GPU"
    sc.cycles.samples = SAMPLES
    sc.cycles.use_denoising = True
    sc.render.filepath = prefix + "_beauty.png"
    bpy.ops.render.render(write_still=True)
    if CLAY == "workbench":
        sc.render.engine = "BLENDER_WORKBENCH"
        sh = sc.display.shading
        sh.light, sh.color_type, sh.show_cavity = "STUDIO", "SINGLE", True
        sh.single_color = (0.6, 0.6, 0.6)
    else:
        clay = bpy.data.materials.new("clay")
        clay.use_nodes = True
        clay.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (0.55, 0.55, 0.55, 1)
        for o in objs:
            o.data.materials.clear()
            o.data.materials.append(clay)
        sc.cycles.samples = 16
    sc.render.filepath = prefix + "_clay.png"
    bpy.ops.render.render(write_still=True)
    return st


def pixels(path):
    """An RGBA float array (RES x RES), or grey when the file is missing."""
    if not path or not os.path.exists(path):
        return np.full((RES, RES, 4), 0.25, np.float32)
    img = bpy.data.images.load(path)
    w, h = img.size
    f = RES / max(w, h)
    img.scale(max(1, int(w * f)), max(1, int(h * f)))
    w, h = img.size
    a = np.empty(w * h * 4, np.float32)
    img.pixels.foreach_get(a)
    a = a.reshape(h, w, 4)
    bpy.data.images.remove(img)
    cell = np.full((RES, RES, 4), 0.25, np.float32)
    y0, x0 = (RES - h) // 2, (RES - w) // 2
    cell[y0:y0 + h, x0:x0 + w] = a
    return cell


for iid in ids():
    sheet = os.path.join(OUT, f"{iid}.png")
    srcs = [glb(m, iid) for m in MODELS if os.path.exists(glb(m, iid))]
    if os.path.exists(sheet) and all(os.path.getmtime(sheet) > os.path.getmtime(s) for s in srcs) \
            and len(srcs) == len(MODELS):
        print("skip", iid)
        continue
    info = {}
    cells = [os.path.join(REFS, f"{iid}_0.png")]
    for m in MODELS:
        prefix = os.path.join(OUT, "_r", f"{iid}_{m}")
        os.makedirs(os.path.dirname(prefix), exist_ok=True)
        if os.path.exists(glb(m, iid)):
            try:
                info[m] = render_model(m, iid, prefix)
                cells += [prefix + "_beauty.png", prefix + "_clay.png"]
                continue
            except Exception as e:
                info[m] = {"error": repr(e)}
        cells += [None, None]
    row = np.concatenate([pixels(p) for p in cells], axis=1)
    out = bpy.data.images.new("sheet", row.shape[1], row.shape[0], alpha=True)
    out.pixels.foreach_set(row.ravel())
    out.filepath_raw = sheet
    out.file_format = "PNG"
    out.save()
    json.dump(info, open(os.path.join(OUT, f"{iid}.json"), "w"), indent=1)
    print("SHEET", iid, json.dumps(info), flush=True)
