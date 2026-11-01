"""Kit presentation renders: the lineup (every item side by side on a neutral ground, with a scale bar and labels),
a thumbnail per item, and optionally a turntable per item.

    blender -b --factory-startup --python kit/tools/present.py -- --kit out/kit.json --items out/items --out out/renders \
        [--only lineup|thumbs|turntable] [--samples 128] [--warmth 0] [--turntable 48] [--ids a,b]

Reads item ids and labels from kit.json ("items": [{"id", "label"}, ...]) and loads out/items/<id>/<id>.glb as they
are (already normalised: metres, bottom-centre origin, facing -Y). Writes lineup.png, thumbs/<id>.png and
turntables/<id>.mp4 (needs ffmpeg). A starting point: copy it into scenes/kit/scripts/ to change the look.
"""

import json
import math
import os
import shutil
import subprocess
import sys

import bpy
from mathutils import Vector

argv = sys.argv[sys.argv.index("--") + 1:]
opt = lambda k, d: type(d)(argv[argv.index(k) + 1]) if k in argv else d
KIT = os.path.abspath(opt("--kit", "out/kit.json"))
ITEMS = os.path.abspath(opt("--items", "out/items"))
OUT = os.path.abspath(opt("--out", "out/renders"))
ONLY = opt("--only", "")
SAMPLES = opt("--samples", 128)
WARMTH = opt("--warmth", 0.0)
TURN = opt("--turntable", 0)
IDS = [s for s in opt("--ids", "").split(",") if s]
os.makedirs(OUT, exist_ok=True)

kit = json.load(open(KIT))
items = [it for it in kit["items"] if not IDS or it["id"] in IDS]


def kelvin_rgb(k):
    # rough blackbody tint, good enough for a warm/cool key
    t = k / 100
    r = 1.0 if t <= 66 else max(0, min(1, 1.292936 * (t - 60) ** -0.1332047592))
    g = max(0, min(1, (0.39008157876 * math.log(t) - 0.63184144) if t <= 66 else 1.129890861 * (t - 60) ** -0.0755148492))
    b = 1.0 if t >= 66 else (0 if t <= 19 else max(0, min(1, 0.54320678911 * math.log(t - 10) - 1.19625408914)))
    return (r, g, b)


def setup():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    sc = bpy.context.scene
    sc.render.engine = "CYCLES"
    sc.cycles.device = "GPU"
    sc.cycles.samples = SAMPLES
    sc.cycles.use_denoising = True
    sc.view_settings.view_transform = "AgX"
    sc.view_settings.look = "AgX - Medium High Contrast"
    w = bpy.data.worlds.new("w")
    sc.world = w
    w.use_nodes = True
    w.node_tree.nodes["Background"].inputs["Color"].default_value = (0.36, 0.37, 0.38, 1)
    w.node_tree.nodes["Background"].inputs["Strength"].default_value = 0.35
    # neutral ground + curved backdrop (a sweep), so the horizon never shows
    bpy.ops.mesh.primitive_plane_add(size=1)
    g = bpy.context.object
    g.name = "ground"
    g.scale = (60, 60, 1)
    gm = bpy.data.materials.new("ground")
    gm.use_nodes = True
    p = gm.node_tree.nodes["Principled BSDF"]
    p.inputs["Base Color"].default_value = (0.42, 0.42, 0.42, 1)
    p.inputs["Roughness"].default_value = 0.85
    g.data.materials.append(gm)
    return sc


def light(name, loc, target, energy, size, color=(1, 1, 1)):
    d = bpy.data.lights.new(name, "AREA")
    d.energy, d.size, d.color = energy, size, color
    o = bpy.data.objects.new(name, d)
    bpy.context.scene.collection.objects.link(o)
    o.location = loc
    o.rotation_euler = (Vector(target) - Vector(loc)).to_track_quat("-Z", "Y").to_euler()
    return o


def studio(centre, radius):
    key_k = 5600 + 1800 * -WARMTH  # warmth 1 -> ~3800 K, -1 -> ~7400 K
    c = Vector(centre)
    s = max(radius, 0.2)
    light("key", c + Vector((1.6, -2.2, 2.4)) * s, c, 220 * s * s, 1.6 * s, kelvin_rgb(key_k))
    light("fill", c + Vector((-2.6, -1.4, 1.2)) * s, c, 60 * s * s, 2.5 * s, kelvin_rgb(key_k + 1500))
    light("rim", c + Vector((-0.8, 2.6, 2.2)) * s, c, 140 * s * s, 1.4 * s)


def load(item):
    path = os.path.join(ITEMS, item["id"], f"{item['id']}.glb")
    before = set(bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=path)
    new = [o for o in bpy.data.objects if o not in before]
    root = bpy.data.objects.new(item["id"], None)
    bpy.context.scene.collection.objects.link(root)
    for o in new:
        if o.parent is None:
            o.parent = root
    bpy.context.view_layer.update()
    return root, [o for o in new if o.type == "MESH"]


def bounds(objs):
    pts = [o.matrix_world @ Vector(c) for o in objs for c in o.bound_box]
    lo = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
    hi = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
    return lo, hi


def camera(lens=50):
    d = bpy.data.cameras.new("cam")
    d.lens = lens
    o = bpy.data.objects.new("cam", d)
    bpy.context.scene.collection.objects.link(o)
    bpy.context.scene.camera = o
    return o


def aim(cam, centre, az, el, dist):
    a, e = math.radians(az), math.radians(el)
    cam.location = Vector(centre) + Vector((math.sin(a) * math.cos(e), -math.cos(a) * math.cos(e), math.sin(e))) * dist
    cam.rotation_euler = (Vector(centre) - cam.location).to_track_quat("-Z", "Y").to_euler()


def mat(name, rgb, emit=0.0):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    p = m.node_tree.nodes["Principled BSDF"]
    p.inputs["Base Color"].default_value = (*rgb, 1)
    p.inputs["Roughness"].default_value = 0.6
    return m


def text(body, loc, size, m):
    c = bpy.data.curves.new("t", "FONT")
    c.body, c.size, c.align_x = body, size, "CENTER"
    o = bpy.data.objects.new("label_" + body[:20], c)
    bpy.context.scene.collection.objects.link(o)
    o.location = loc
    o.rotation_euler = (math.pi / 2, 0, 0)  # stand it up, facing the camera (-Y)
    o.data.materials.append(m)
    return o


def lineup():
    sc = setup()
    loaded = [(it, *load(it)) for it in items]
    heights = [bounds(m)[1].z for _, _, m in loaded]
    gap = max(0.06, 0.12 * max(heights))
    x = 0.0
    depth = 0.0
    for it, root, meshes in loaded:
        lo, hi = bounds(meshes)
        w = hi.x - lo.x
        root.location.x = x + w / 2 - (lo.x + hi.x) / 2
        x += w + gap
        depth = max(depth, hi.y - lo.y)
    bpy.context.view_layer.update()
    allm = [o for _, _, m in loaded for o in m]
    lo, hi = bounds(allm)
    total = hi.x - lo.x
    # scale bar: alternating 10 cm (or 1 cm for tiny kits) blocks, a "nice" length about a fifth of the width
    nice = min((0.05, 0.1, 0.2, 0.25, 0.5, 1.0, 2.0, 5.0), key=lambda v: abs(v - total / 5))
    step = nice / 10 if nice <= 0.25 else 0.1
    dark, light_m, ink = mat("bar_dark", (0.03, 0.03, 0.03)), mat("bar_light", (0.85, 0.85, 0.85)), mat("ink", (0.04, 0.04, 0.04))
    by = lo.y - max(0.06, depth * 0.35)
    th = max(0.004, nice / 40)
    for i in range(round(nice / step)):
        bpy.ops.mesh.primitive_cube_add(size=1, location=(lo.x + step * (i + 0.5), by, 0.001))
        b = bpy.context.object
        b.scale = (step, th * 2, 0.002)
        b.data.materials.append(dark if i % 2 == 0 else light_m)
    label = f"{nice * 100:.0f} cm" if nice < 1 else f"{nice:g} m"
    t = text(label, (lo.x + nice / 2, by - th, th * 1.5), th * 4, ink)
    # item labels on the ground in front of each item
    lsize = max(0.01, min(0.04, gap * 0.22))
    for it, root, meshes in loaded:
        l2, h2 = bounds(meshes)
        text(it.get("label", it["id"])[:28], ((l2.x + h2.x) / 2, l2.y - lsize * 1.5, 0.003), lsize, ink)
    bpy.context.view_layer.update()
    lo2, hi2 = bounds(allm)
    centre = Vector(((lo.x + hi.x) / 2, (lo.y + hi.y) / 2 - 0.1 * depth, (hi2.z) * 0.42))
    sc.render.resolution_x, sc.render.resolution_y = 2400, 1000
    cam = camera(50)
    fov_h = cam.data.angle
    width = max(total * 1.2, (hi2.z) * 2.4 * 1.5)
    dist = (width / 2) / math.tan(fov_h / 2) + depth
    aim(cam, centre, 0, 14, dist)
    studio(centre, max(total, hi2.z) * 0.6)
    sc.render.filepath = os.path.join(OUT, "lineup.png")
    bpy.ops.render.render(write_still=True)
    print("LINEUP", sc.render.filepath)


def per_item(turntable=False):
    for it in items:
        sc = setup()
        root, meshes = load(it)
        lo, hi = bounds(meshes)
        centre = (lo + hi) / 2
        radius = (hi - lo).length / 2
        studio(centre, radius * 1.4)
        cam = camera(70)
        dist = radius / math.sin(cam.data.angle / 2) * 1.05
        if not turntable:
            sc.render.resolution_x = sc.render.resolution_y = 1024
            aim(cam, centre, -30, 20, dist)
            os.makedirs(os.path.join(OUT, "thumbs"), exist_ok=True)
            sc.render.filepath = os.path.join(OUT, "thumbs", f"{it['id']}.png")
            bpy.ops.render.render(write_still=True)
            continue
        sc.render.resolution_x = sc.render.resolution_y = 640
        sc.cycles.samples = min(SAMPLES, 48)
        tmp = os.path.join(OUT, "turntables", f"_{it['id']}")
        os.makedirs(tmp, exist_ok=True)
        for f in range(TURN):
            aim(cam, centre, -30 + 360 * f / TURN, 18, dist)
            sc.render.filepath = os.path.join(tmp, f"{f:04d}.png")
            bpy.ops.render.render(write_still=True)
        mp4 = os.path.join(OUT, "turntables", f"{it['id']}.mp4")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", "24", "-i", os.path.join(tmp, "%04d.png"),
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20", mp4], check=True)
        shutil.rmtree(tmp)


if ONLY in ("", "lineup"):
    lineup()
if ONLY in ("", "thumbs"):
    per_item()
if ONLY == "turntable" or (TURN and ONLY == ""):
    TURN = TURN or 48
    per_item(turntable=True)
