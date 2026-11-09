"""Procedural model of the station's orange USB-A to USB-C cable, rendered with domain randomization.

    blender -b -P render_cable.py -- OUT_DIR N_IMAGES BG_DIR [SEED]

Model (mm, from the station photos IMG_2702-2704 and the USB specs):
- USB-A plug: steel shell 12.0 x 4.5 x 12.0, two latch windows on the top face, dark tongue inside.
- USB-C plug: steel shell 8.25 x 2.4 x 6.65, stadium cross-section, dark slot.
- Ferrules: orange anodised aluminium, rounded boxes; A end 13.0 x 7.0 x 19.0, C end 11.0 x 6.0 x 18.0.
- Strain reliefs: black, 15 mm, ribbed, tapering 6.0 -> 4.6 mm, orange showing between the ribs.
- Cable: orange rubber, 4.0 mm, 150-220 mm long, random smooth bend in 3D.
Each image: random cable shape and pose on a plane textured with a random crop of a real station frame or photo,
random lights (sun + 1-3 area lights, colour temperature, strength), random camera (wide wrist-like or narrow
lens, 5-40 cm away) aimed at one of: the USB-C end, the USB-A end, the cable middle, a strain relief, empty table.
Random material roughness/colour jitter, depth of field and motion-free noise. Writes OUT_DIR/<label>/<n>.png
(224 x 224) and OUT_DIR/labels.jsonl with the projected ferrule length in pixels.
"""

import json
import math
import os
import random
import sys

import bpy
from mathutils import Vector

argv = sys.argv[sys.argv.index("--") + 1:]
OUT, N, BG = os.path.abspath(argv[0]), int(argv[1]), os.path.abspath(argv[2])
SEED = int(argv[3]) if len(argv) > 3 else 0
rng = random.Random(SEED)
MM = 0.001
LABELS = ("usb_c", "usb_a", "none")

BGS = [os.path.join(BG, f) for f in sorted(os.listdir(BG)) if f.lower().endswith((".jpg", ".png"))]


# ------------------------------------------------------------------------------------------------- scene setup
def reset():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    sc = bpy.context.scene
    sc.render.engine = "CYCLES"
    prefs = bpy.context.preferences.addons["cycles"].preferences
    for kind in ("OPTIX", "CUDA"):
        try:
            prefs.compute_device_type = kind
            prefs.get_devices()
            for d in prefs.devices:
                d.use = True
            sc.cycles.device = "GPU"
            break
        except Exception:  # noqa: BLE001
            continue
    sc.cycles.samples = 48
    sc.cycles.use_denoising = True
    sc.render.resolution_x = sc.render.resolution_y = 224
    sc.render.film_transparent = False
    sc.view_settings.view_transform = "Standard"
    sc.view_settings.exposure = rng.uniform(-1.8, 0.1)
    return sc


def mat(name, color, metallic=0.0, rough=0.5, spec=0.5):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    b = m.node_tree.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = (*color, 1.0)
    b.inputs["Metallic"].default_value = metallic
    b.inputs["Roughness"].default_value = rough
    return m


def jitter(c, amt):
    return tuple(min(1.0, max(0.0, v * (1 + rng.uniform(-amt, amt)))) for v in c)


def mix(a, b, t):
    return tuple(x + (y - x) * t for x, y in zip(a, b))


BRIGHT = (0.9, 0.16, 0.005)      # linear RGB: saturated anodised orange
SALMON = (0.80, 0.22, 0.10)      # linear RGB: the station cable's copper / salmon look on the wrist cameras


def striped(name, base, rough):
    """Black / orange braid bands along the object's local axis (cable and relief core)."""
    m = mat(name, base, rough=rough)
    nt = m.node_tree
    wave = nt.nodes.new("ShaderNodeTexWave")
    wave.wave_type = "BANDS"
    wave.bands_direction = rng.choice(["X", "Y", "Z", "DIAGONAL"])
    wave.inputs["Scale"].default_value = rng.uniform(40, 160)
    wave.inputs["Distortion"].default_value = rng.uniform(0, 2)
    coord = nt.nodes.new("ShaderNodeTexCoord")
    nt.links.new(coord.outputs["Object"], wave.inputs["Vector"])
    ramp = nt.nodes.new("ShaderNodeValToRGB")
    ramp.color_ramp.elements[0].color = (0.02, 0.02, 0.02, 1)
    ramp.color_ramp.elements[1].color = (*base, 1)
    ramp.color_ramp.elements[0].position = rng.uniform(0.3, 0.5)
    ramp.color_ramp.elements[1].position = ramp.color_ramp.elements[0].position + 0.05
    nt.links.new(wave.outputs["Fac"], ramp.inputs["Fac"])
    nt.links.new(ramp.outputs["Color"], nt.nodes["Principled BSDF"].inputs["Base Color"])
    return m


def materials():
    orange = jitter(mix(BRIGHT, SALMON, rng.random()), 0.1)
    rubber_rough = rng.uniform(0.4, 0.8)
    rubber = striped("rubber", orange, rubber_rough) if rng.random() < 0.5 else mat("rubber", orange, rough=rubber_rough)
    return {
        "orange_anod": mat("anod", jitter(orange, 0.06), metallic=rng.uniform(0.2, 0.9),
                           rough=rng.uniform(0.2, 0.55)),
        "orange_rubber": rubber,
        "black": mat("black", jitter((0.02, 0.02, 0.02), 0.5), rough=rng.uniform(0.4, 0.8)),
        "steel": mat("steel", jitter((0.75, 0.75, 0.76), 0.1), metallic=1.0, rough=rng.uniform(0.1, 0.35)),
        "dark": mat("dark", (0.03, 0.03, 0.035), rough=0.6),
        "strip": mat("strip", jitter((0.03, 0.03, 0.03), 0.5), rough=rng.uniform(0.3, 0.7)),
        "ball": mat("ball", jitter(mix((0.9, 0.25, 0.08), SALMON, rng.random()), 0.1), rough=rng.uniform(0.3, 0.7)),
        "skin": mat("skin", jitter(rng.choice([(0.6, 0.35, 0.25), (0.35, 0.17, 0.1), (0.15, 0.07, 0.04),
                                              (0.8, 0.5, 0.4)]), 0.15), rough=0.5),
        "blue": mat("blue", jitter((0.02, 0.25, 0.6), 0.15), rough=0.5),
    }


def power_strip(M, face_at, side):
    """Black power strip whose long side face (the USB ports) is at face_at, facing `side` (horizontal unit).
    Returns (along unit vector, strip height)."""
    L, W, H = rng.uniform(0.18, 0.3), rng.uniform(0.045, 0.06), rng.uniform(0.03, 0.045)
    box = rbox("strip", (L, W, H), 0.004, M["strip"])
    c = face_at - side * (W / 2)
    box.location = (c.x + side.y * rng.uniform(-0.3, 0.3) * L, c.y - side.x * rng.uniform(-0.3, 0.3) * L, H / 2)
    box.rotation_euler = (0, 0, math.atan2(side.x, -side.y))
    bpy.context.view_layer.update()
    return Vector((-side.y, side.x, 0)), H


def yaw_for(d):
    """Root yaw so the end's local +Y (towards the tip) points along horizontal d."""
    return math.atan2(-d.x, d.y)


def distractors(M, centre, n):
    """Golf ball, finger stand-ins, blue block near the cable: the orange-ish things that are not ends."""
    for _ in range(n):
        p = centre + Vector((rng.uniform(-0.06, 0.06), rng.uniform(-0.06, 0.06), 0))
        k = rng.random()
        if k < 0.4:
            r = 0.02135
            bpy.ops.mesh.primitive_uv_sphere_add(radius=r, location=(p.x, p.y, r))
            o = bpy.context.object
            o.data.materials.append(M["ball"])
            bpy.ops.object.shade_smooth()
        elif k < 0.75:
            r = rng.uniform(0.007, 0.01)
            bpy.ops.mesh.primitive_cylinder_add(radius=r, depth=rng.uniform(0.04, 0.08),
                                                location=(p.x, p.y, r + rng.uniform(0, 0.04)))
            o = bpy.context.object
            o.rotation_euler = (math.pi / 2 + rng.uniform(-0.6, 0.6), 0, rng.uniform(0, 6.28))
            bev = o.modifiers.new("b", "BEVEL")
            bev.width, bev.segments = r * 0.9, 6
            o.data.materials.append(M["skin"])
        else:
            s = rng.uniform(0.025, 0.04)
            o = rbox("block", (s, s, s), 0.002, M["blue"])
            o.location = (p.x, p.y, s / 2)
            o.rotation_euler = (0, 0, rng.uniform(0, 6.28))
    bpy.context.view_layer.update()


def rbox(name, size, bevel, m):
    bpy.ops.mesh.primitive_cube_add(size=1)
    o = bpy.context.object
    o.name = name
    o.scale = size
    bpy.ops.object.transform_apply(scale=True)
    if bevel > 0:
        mod = o.modifiers.new("bev", "BEVEL")
        mod.width = bevel
        mod.segments = 4
    o.data.materials.append(m)
    return o


def stadium(name, w, h, length, m):
    """Rounded-end slab along +Y (the USB-C shell)."""
    o = rbox(name, (w, length, h), h * 0.49, m)
    return o


def build_end(kind, M):
    """One plug + ferrule + strain relief, along +Y from the origin (relief at y<0, tip at y>0). Returns
    (parent empty, tip centre local, ferrule centre local, ferrule length)."""
    parts = []
    if kind == "a":
        fw, fh, fl = 13.0, 7.0, 19.0
        tw, th, tl = 12.0, 4.5, 12.0
    else:
        fw, fh, fl = 11.0, 6.0, 18.0
        tw, th, tl = 8.25, 2.4, 6.65
    fw, fh, fl = (v * rng.uniform(0.95, 1.05) for v in (fw, fh, fl))
    fer = rbox("ferrule", (fw * MM, fl * MM, fh * MM), min(fw, fh) * 0.25 * MM, M["orange_anod"])
    fer.location = (0, fl / 2 * MM, 0)
    parts.append(fer)
    if kind == "a":
        shell = rbox("shell", (tw * MM, tl * MM, th * MM), 0.3 * MM, M["steel"])
        shell.location = (0, (fl + tl / 2) * MM, 0)
        tongue = rbox("tongue", ((tw - 1.2) * MM, (tl - 0.5) * MM, (th - 1.6) * MM), 0, M["dark"])
        tongue.location = (0, (fl + tl / 2 + 0.3) * MM, -0.3 * MM)
        parts += [shell, tongue]
        for sx in (-2.5, 2.5):   # latch windows on the top face
            win = rbox("win", (2.0 * MM, 2.5 * MM, 0.3 * MM), 0, M["dark"])
            win.location = (sx * MM, (fl + tl * 0.55) * MM, (th / 2 + 0.05) * MM)
            parts.append(win)
    else:
        shell = stadium("shell", tw * MM, th * MM, tl * MM, M["steel"])
        shell.location = (0, (fl + tl / 2) * MM, 0)
        slot = rbox("slot", ((tw - 1.4) * MM, 0.3 * MM, (th - 1.0) * MM), (th - 1.0) * 0.45 * MM, M["dark"])
        slot.location = (0, (fl + tl + 0.05) * MM, 0)
        parts += [shell, slot]
    # strain relief: tapering ribs
    n_rib = 7
    for k in range(n_rib):
        r = (3.0 - 0.7 * k / n_rib) * MM
        bpy.ops.mesh.primitive_cylinder_add(radius=r, depth=1.3 * MM, vertices=24)
        rib = bpy.context.object
        rib.rotation_euler = (math.pi / 2, 0, 0)
        rib.location = (0, -(1.0 + k * 2.1) * MM, 0)
        rib.data.materials.append(M["black"])
        parts.append(rib)
    bpy.ops.mesh.primitive_cylinder_add(radius=2.2 * MM, depth=15 * MM, vertices=24)
    core = bpy.context.object
    core.rotation_euler = (math.pi / 2, 0, 0)
    core.location = (0, -7.5 * MM, 0)
    core.data.materials.append(M["orange_rubber"])
    parts.append(core)
    root = bpy.data.objects.new(f"end_{kind}", None)
    bpy.context.collection.objects.link(root)
    for p in parts:
        p.parent = root
    tip = Vector((0, (fl + tl * 0.5) * MM, 0))
    return root, tip, Vector((0, fl / 2 * MM, 0)), fl * MM


def cable_curve(p0, d0, p1, d1, M):
    """Orange cable from the C relief end to the A relief end: a Bezier with tangents along each end's axis."""
    cu = bpy.data.curves.new("cable", "CURVE")
    cu.dimensions = "3D"
    cu.bevel_depth = 2.0 * MM
    cu.bevel_resolution = 4
    sp = cu.splines.new("BEZIER")
    sp.bezier_points.add(1)
    k = (p1 - p0).length * rng.uniform(0.3, 0.6)
    for bp, p, d, sign in ((sp.bezier_points[0], p0, d0, -1), (sp.bezier_points[1], p1, d1, -1)):
        bp.co = p
        bp.handle_left_type = bp.handle_right_type = "FREE"
        bp.handle_right = p + d * sign * k * (1 if bp is sp.bezier_points[0] else -1)
        bp.handle_left = p - (bp.handle_right - p)
    o = bpy.data.objects.new("cable", cu)
    bpy.context.collection.objects.link(o)
    o.data.materials.append(M["orange_rubber"])
    return o


def place(root, pos, yaw, pitch=0.0, roll=0.0):
    root.location = pos
    root.rotation_euler = (pitch, roll, yaw)
    bpy.context.view_layer.update()


def world_pt(root, local):
    return root.matrix_world @ local


def table(sc):
    bpy.ops.mesh.primitive_plane_add(size=1.2)
    pl = bpy.context.object
    m = bpy.data.materials.new("table")
    m.use_nodes = True
    nt = m.node_tree
    bsdf = nt.nodes["Principled BSDF"]
    bsdf.inputs["Roughness"].default_value = rng.uniform(0.3, 0.9)
    if BGS and rng.random() < 0.85:
        tex = nt.nodes.new("ShaderNodeTexImage")
        tex.image = bpy.data.images.load(rng.choice(BGS))
        mapn = nt.nodes.new("ShaderNodeMapping")
        coord = nt.nodes.new("ShaderNodeTexCoord")
        s = rng.uniform(0.6, 3.0)
        mapn.inputs["Scale"].default_value = (s, s, 1)
        mapn.inputs["Rotation"].default_value = (0, 0, rng.uniform(0, 6.28))
        mapn.inputs["Location"].default_value = (rng.random(), rng.random(), 0)
        nt.links.new(coord.outputs["UV"], mapn.inputs["Vector"])
        nt.links.new(mapn.outputs["Vector"], tex.inputs["Vector"])
        nt.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
    else:
        g = rng.uniform(0.05, 0.9)
        bsdf.inputs["Base Color"].default_value = (*jitter((g, g, g * rng.uniform(0.9, 1.05)), 0.1), 1)
    pl.data.materials.append(m)
    return pl


def kelvin(t):
    t = t / 100.0
    r = 1.0 if t <= 66 else min(1.0, 1.29 * (t - 60) ** -0.133)
    g = min(1.0, (0.39 * math.log(t) - 0.63)) if t <= 66 else min(1.0, 1.13 * (t - 60) ** -0.0755)
    b = 1.0 if t >= 66 else (0.0 if t <= 19 else min(1.0, 0.543 * math.log(t - 10) - 1.19))
    return (max(r, 0), max(g, 0), max(b, 0))


def lights(sc):
    w = bpy.data.worlds.new("w")
    sc.world = w
    w.use_nodes = True
    w.node_tree.nodes["Background"].inputs["Color"].default_value = (*kelvin(rng.uniform(3000, 7500)), 1)
    w.node_tree.nodes["Background"].inputs["Strength"].default_value = rng.uniform(0.05, 0.6)
    bpy.ops.object.light_add(type="SUN", rotation=(rng.uniform(0, 1.2), rng.uniform(-0.6, 0.6), rng.uniform(0, 6.28)))
    sun = bpy.context.object
    sun.data.energy = rng.uniform(0.0, 2.5)
    sun.data.color = kelvin(rng.uniform(3500, 7000))
    for _ in range(rng.randint(1, 3)):
        bpy.ops.object.light_add(type="AREA", location=(rng.uniform(-0.6, 0.6), rng.uniform(-0.6, 0.6),
                                                         rng.uniform(0.4, 2.5)))
        a = bpy.context.object
        a.data.size = rng.uniform(0.2, 1.5)
        a.data.energy = rng.uniform(5, 70)
        a.data.color = kelvin(rng.uniform(3000, 7500))
        direction = Vector((0, 0, 0)) - a.location
        a.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()


def camera(sc, target, ferrule_len, kind):
    cam_data = bpy.data.cameras.new("cam")
    wide = rng.random() < 0.6
    cam_data.lens = rng.uniform(10, 16) if wide else rng.uniform(24, 60)
    cam_data.clip_start = 0.001   # the camera is 2-30 cm from the plug; the 10 cm default clips it away
    cam = bpy.data.objects.new("cam", cam_data)
    bpy.context.collection.objects.link(cam)
    sc.camera = cam
    # distance so the ferrule spans a random share of the frame (what the classifier crop will look like)
    frac = rng.uniform(0.18, 0.55)
    fov = 2 * math.atan(cam_data.sensor_width / 2 / cam_data.lens)
    dist = ferrule_len / frac / (2 * math.tan(fov / 2))
    el = rng.uniform(math.radians(20), math.radians(88))
    az = rng.uniform(0, 2 * math.pi)
    off = Vector((math.cos(az) * math.cos(el), math.sin(az) * math.cos(el), math.sin(el))) * dist
    cam.location = target + off
    q = (target - cam.location).to_track_quat("-Z", "Y")
    cam.rotation_euler = q.to_euler()
    cam.rotation_euler.rotate_axis("Z", rng.uniform(-math.pi, math.pi))
    if rng.random() < 0.3:
        cam_data.dof.use_dof = True
        cam_data.dof.focus_distance = dist * rng.uniform(0.85, 1.15)
        cam_data.dof.aperture_fstop = rng.uniform(2.0, 8.0)
    return cam, frac


def seen(sc, cam, tgt, root):
    """Line of sight from the camera to the target: for an end, the first thing hit must be part of that end (or
    nothing before it); for 'none', the camera must not be inside or right against an object."""
    dg = bpy.context.evaluated_depsgraph_get()
    o = cam.matrix_world.translation
    if sc.ray_cast(dg, o, Vector((0, 0, 1)), distance=1.0)[0]:
        return False     # something right above the camera: it is inside an object (the strip, the ball)
    v = tgt - o
    dist = v.length
    hit, loc, _, _, obj, _ = sc.ray_cast(dg, o, v.normalized(), distance=dist * 1.2)
    if not hit:
        return root is None
    if root is None:
        return (loc - o).length > 0.4 * dist
    return obj.parent == root or (loc - o).length > dist * 0.97


def one(i):
    sc = reset()
    M = materials()
    table(sc)
    lights(sc)
    c_root, c_tip, c_fer, c_fl = build_end("c", M)
    a_root, a_tip, a_fer, a_fl = build_end("a", M)
    length = rng.uniform(0.15, 0.22)
    yaw = rng.uniform(0, 2 * math.pi)
    lift = 3.5 * MM
    place(c_root, Vector((0, 0, lift)), yaw + rng.uniform(-0.9, 0.9), rng.uniform(-0.15, 0.15),
          rng.choice([0, math.pi / 2, math.pi, -math.pi / 2]) + rng.uniform(-0.2, 0.2))
    p_a = Vector((math.cos(yaw) * length * 0.7 + rng.uniform(-0.05, 0.05),
                  math.sin(yaw) * length * 0.7 + rng.uniform(-0.05, 0.05), lift))
    place(a_root, p_a, yaw + math.pi + rng.uniform(-1.2, 1.2), rng.uniform(-0.15, 0.15),
          rng.choice([0, math.pi / 2, math.pi, -math.pi / 2]) + rng.uniform(-0.2, 0.2))
    if rng.random() < 0.15:      # held in the air: tilt the whole end up off the table
        c_root.location.z += rng.uniform(0.01, 0.08)
        c_root.rotation_euler.x += rng.uniform(-0.8, 0.8)
    bpy.context.view_layer.update()
    rel = lambda root: (root.matrix_world @ Vector((0, -15 * MM, 0)),
                        (root.matrix_world.to_3x3() @ Vector((0, -1, 0))).normalized())
    label = rng.choices(LABELS, weights=(0.4, 0.3, 0.3))[0]
    seated = rng.random() < (0.5 if label == "usb_a" else 0.25)
    if seated:
        # the A end seated in a power strip's USB port, often with other cables' A ends next to it
        side = Vector((-p_a.x, -p_a.y, 0)).normalized()
        along, H = power_strip(M, p_a, side)
        z = H * rng.uniform(0.35, 0.65)
        gap = rng.uniform(0, 1.5) * MM
        a_root.location = p_a + side * (a_fl + gap) + Vector((0, 0, z))
        a_root.rotation_euler = (0, rng.choice([0, math.pi]), yaw_for(-side))
        for k in rng.sample([-2, -1, 1, 2], rng.randint(0, 3)):
            n_root, _, _, n_fl = build_end("a", M)
            n_root.location = (p_a + along * k * rng.uniform(0.022, 0.035) + side * (n_fl + rng.uniform(0, 1.5) * MM)
                               + Vector((0, 0, z)))
            n_root.rotation_euler = (0, rng.choice([0, math.pi]), yaw_for(-side))
            bpy.context.view_layer.update()
            pn, dn = rel(n_root)
            far = pn + side * rng.uniform(0.08, 0.15) + along * rng.uniform(-0.05, 0.05)
            far.z = 2 * MM
            cable_curve(pn, dn, far, -side, M)
        bpy.context.view_layer.update()
    pc, dc = rel(c_root)
    pa, da = rel(a_root)
    cable_curve(pc, dc, pa, da, M)
    spots = []
    if rng.random() < (0.7 if label == "none" else 0.35):
        centre = rng.choice([pc, pa, (pc + pa) / 2])
        centre = Vector((centre.x, centre.y, 0))
        before = set(bpy.data.objects)
        distractors(M, centre, rng.randint(1, 3))
        spots = [o.location.copy() for o in bpy.data.objects if o not in before]
    if label == "usb_c":
        tgt = (world_pt(c_root, c_fer) + world_pt(c_root, c_tip)) / 2
        fl = c_fl
    elif label == "usb_a":
        tgt = world_pt(a_root, a_fer) if seated else (world_pt(a_root, a_fer) + world_pt(a_root, a_tip)) / 2
        fl = a_fl
    else:
        what = rng.random()
        if spots and rng.random() < 0.5:
            tgt = rng.choice(spots).copy()
        elif what < 0.4:
            tgt = (pc + pa) / 2 + Vector((rng.uniform(-0.03, 0.03), rng.uniform(-0.03, 0.03), 0))
        elif what < 0.7:
            tgt = rng.choice([pc, pa]) + Vector((0, 0, 0))
        else:
            tgt = Vector((rng.uniform(-0.3, 0.3), rng.uniform(-0.3, 0.3), 0))
        fl = rng.uniform(15, 19) * MM
    tgt += Vector((rng.uniform(-1, 1), rng.uniform(-1, 1), 0)) * fl * 0.25   # not always dead centre
    want = {"usb_c": c_root, "usb_a": a_root}.get(label)
    for _ in range(15):
        cam, frac = camera(sc, tgt, fl, label)
        if seen(sc, cam, tgt, want):
            break
        bpy.data.objects.remove(cam, do_unlink=True)
    else:
        return None      # no clear view of the end in this scene: skip it rather than mislabel it
    d = os.path.join(OUT, label)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{SEED:03d}_{i:05d}.png")
    sc.render.filepath = path
    bpy.ops.render.render(write_still=True)
    return {"file": os.path.relpath(path, OUT), "label": label, "ferrule_frac": round(frac, 3), "seed": SEED, "i": i}


os.makedirs(OUT, exist_ok=True)
with open(os.path.join(OUT, f"labels_{SEED:03d}.jsonl"), "a", encoding="utf-8") as f:
    for i in range(N):
        rec = one(i)
        if rec is None:
            continue
        f.write(json.dumps(rec) + "\n")
        f.flush()
