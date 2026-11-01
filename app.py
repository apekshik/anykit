"""AnyKit as a fal Serverless app: one request = one Claude agent building a cohesive prop kit on a GPU worker.

    fal deploy app.py::AnyKitApp --app-name anykit --auth private
    python client.py kit <style reference path or url> [...] --prompt "..." [--items "a,b"] [--dial count=9]

POST /ref2kit (alias /kit)  style reference images (+ prompt, item list, dials) -> per-item GLB + LOD1 + thumbnail,
                            kit.blend, lineup render, kit.json, package zip, critic rounds, dials as built.
POST /kits, /kit_events     the gallery: every kit on this app's storage, and one kit's event stream.
POST /smoke                 container, agent and critic check.
The agent (agent/kit_builder.py, instructions in agent/PROMPT.md and agent/KIT.md) streams ANYSCENE_STAGE lines that
the fal queue relays to the caller as logs (README.md). Same container image, GPU wrapper and OptiX cache as AnyScene's
scene builder (github.com/apekshik/anyscene), so both can share one fal account's storage and image layers.

Configuration is all environment (fal secrets): OPENROUTER_API_KEY or ANTHROPIC_API_KEY, and FAL_KEY.
"""

import json
import os
import sys
from pathlib import PurePosixPath

import fal
from fal.container import ContainerImage
from pydantic import BaseModel, Field

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "agent"))
import llm  # noqa: E402,F401  (the critic's Claude client; module level so fal bundles it)
import kit_builder  # noqa: E402  (the agent: session, hooks, critic, workspace)
import gallery  # noqa: E402  (synced from AnyScene: /kits, /kit_events)

if os.name == "nt":
    # Deploying from Windows: ContainerImage's context_dir default is a WindowsPath Linux can't unpickle.
    _posix = PurePosixPath("/tmp")
    ContainerImage.__dataclass_fields__["context_dir"].default = _posix
    ContainerImage.context_dir = _posix
    ContainerImage.__init__.__defaults__ = tuple(
        _posix if isinstance(d, os.PathLike) else d for d in ContainerImage.__init__.__defaults__ or ()
    )

TOOLKIT = kit_builder.toolkit_files()  # PROMPT.md, KIT.md, kit/, generate/, pipeline/: read at deploy time
BLENDER_GPU = open(os.path.join(_HERE, "agent", "blender_gpu.py"), encoding="utf-8").read()
OPTIX_CACHE_LOCAL = "/var/tmp/OptixCache_root/optix7cache.db"
OPTIX_CACHE_SHARED = "/data/anyscene/cache/optix7cache.db"  # shared with AnyScene's scene builder on one account
WARMUP = """import bpy
s = bpy.context.scene
s.render.engine = "CYCLES"; s.cycles.samples = 4; s.render.resolution_x = s.render.resolution_y = 64
m = bpy.data.materials.new("glass"); m.use_nodes = True; m.node_tree.nodes.new("ShaderNodeBsdfGlass")
bpy.ops.mesh.primitive_uv_sphere_add(location=(0, 0, 0.5))
bpy.context.object.data.materials.append(m)
bpy.ops.render.render()
print("WARMUP done")
"""


def save_optix_cache():
    import shutil
    if os.path.exists(OPTIX_CACHE_LOCAL) and (not os.path.exists(OPTIX_CACHE_SHARED)
                                              or os.path.getsize(OPTIX_CACHE_LOCAL) > os.path.getsize(OPTIX_CACHE_SHARED)):
        os.makedirs(os.path.dirname(OPTIX_CACHE_SHARED), exist_ok=True)
        tmp = f"{OPTIX_CACHE_SHARED}.{os.getpid()}.tmp"
        shutil.copy(OPTIX_CACHE_LOCAL, tmp)
        os.replace(tmp, OPTIX_CACHE_SHARED)


BLENDER_WRAPPER = '#!/bin/sh\nexec /usr/local/bin/blender.real --python /opt/anyscene/blender_gpu.py "$@"\n'
KITS_DIR = "/data/anyscene/kit"  # kits + retry index (_index); AnyScene's path, kept so existing kits stay listed
BLENDER_VERSION = "5.0.1"

# identical to AnyScene's scene builder image, so the layers are shared when both run on one account
DOCKERFILE = f"""
FROM python:3.13-slim
ENV NVIDIA_DRIVER_CAPABILITIES=all
RUN apt-get update && apt-get install -y --no-install-recommends \\
      curl ca-certificates xz-utils git procps fonts-dejavu-core libxi6 libxxf86vm1 libxfixes3 libxrender1 libgl1 \\
      libegl1 libxkbcommon0 libsm6 libice6 libx11-6 libglib2.0-0 ffmpeg nodejs ripgrep zip \\
    && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL https://download.blender.org/release/Blender{BLENDER_VERSION[:3]}/blender-{BLENDER_VERSION}-linux-x64.tar.xz \\
      | tar -xJ -C /opt \\
    && ln -s /opt/blender-{BLENDER_VERSION}-linux-x64/blender /usr/local/bin/blender.real
"""


class KitRequest(BaseModel):
    reference_image_urls: list[str] = Field(description="1-4 style reference images: the world the kit belongs to.")
    prompt: str = Field("", description="What the kit is, e.g. '1970s mission-control desk clutter'.")
    items: list[str] = Field(default_factory=list, description="Optional item list; built exactly, in order.")
    effort: str = Field("medium", description="Claude effort for the builder: low, medium, high, xhigh, max.")
    dials: dict[str, float | int | str] = Field(default_factory=dict, description=(
        "Kit dials (README.md); any left out are read from the reference. count: integer. variety, wear, "
        "wetness: 0-1. warmth: -1 (cold) to 1 (warm). detail: low|medium|high. look: clean|cinematic|gritty|vintage."))
    fresh: bool = Field(False, description="Build again even if an identical request already finished.")


class KitItem(BaseModel):
    id: str
    label: str = ""
    dims_m: list[float] = Field(default_factory=list, description="[width x, depth y, height z] in metres.")
    tris: int | None = None
    tris_lod1: int | None = None
    generator: str | None = None
    why: str | None = None
    glb_url: str | None = None
    lod1_url: str | None = None
    thumb_url: str | None = None
    turntable_url: str | None = None


class KitResponse(BaseModel):
    kit_id: str
    name: str | None
    style: str | None
    items: list[KitItem]
    lineup_url: str | None
    kit_blend_url: str | None
    kit_json_url: str | None
    package_url: str | None
    critiques: list[dict]
    dials: dict = Field(default_factory=dict, description="Every dial as built: value, source, means (out/dials.json).")
    missing: list[str]
    agent_cost_usd: float | None
    generation_cost_usd: float | None
    minutes: float


class GalleryRequest(BaseModel):
    limit: int = Field(200, description="Most kits to list, newest first.")


class KitEventsRequest(BaseModel):
    kit_id: str = Field(description="A kit id (k_...) from /kits.")
    after: int = Field(0, description="Byte offset into the kit's event stream: 0, then the previous call's `next`.")
    heartbeats: bool = Field(False, description="Include every heartbeat (default: only the newest in each page).")


class AnyKitApp(fal.App):
    machine_type = ["GPU-RTX5090", "GPU-RTXPRO6000"]
    image = ContainerImage.from_dockerfile_str(DOCKERFILE)
    requirements = ["pydantic", "anthropic", "pillow", "fal==1.84.0", "fal-client", "claude-agent-sdk==0.2.176"]
    local_python_modules = ["kit_builder", "llm", "gallery"]
    min_concurrency = 0
    max_concurrency = 10
    request_timeout = 7200
    startup_timeout = 1200

    def setup(self):
        import shutil
        import subprocess
        import time
        os.makedirs("/opt/anyscene", exist_ok=True)
        with open("/opt/anyscene/blender_gpu.py", "w") as f:
            f.write(BLENDER_GPU)
        with open("/usr/local/bin/blender", "w") as f:
            f.write(BLENDER_WRAPPER)
        os.chmod("/usr/local/bin/blender", 0o755)
        if os.path.exists(OPTIX_CACHE_SHARED):
            os.makedirs(os.path.dirname(OPTIX_CACHE_LOCAL), exist_ok=True)
            shutil.copy(OPTIX_CACHE_SHARED, OPTIX_CACHE_LOCAL)
        t = time.time()
        r = subprocess.run(["blender", "-b", "--factory-startup", "--python-expr", WARMUP], capture_output=True, text=True,
                           timeout=900)
        print(f"setup: OptiX warm-up {'ok' if 'WARMUP done' in r.stdout else 'FAILED'} in {time.time() - t:.0f} s", flush=True)
        save_optix_cache()

    # fal naming: target fal-ai/anykit/ref2kit; /kit stays as an alias.
    @fal.endpoint("/ref2kit")
    async def ref2kit(self, req: KitRequest) -> KitResponse:
        """Style reference images (+ prompt, items, dials) -> a cohesive kit of production-ready props."""
        return await self._kit(req)

    @fal.endpoint("/kit")
    async def kit(self, req: KitRequest) -> KitResponse:
        return await self._kit(req)

    async def _kit(self, req: KitRequest) -> KitResponse:
        import asyncio
        import hashlib
        import time
        import uuid

        t0 = time.time()
        # Retry-safe: fal's queue re-runs a request that times out or loses its worker, so an
        # identical request returns the finished kit, waits on a live attempt, or resumes a dead one in place.
        fp = hashlib.sha256(json.dumps({"refs": req.reference_image_urls, "prompt": req.prompt, "items": req.items,
                                        "dials": req.dials, "effort": req.effort}, sort_keys=True).encode()).hexdigest()[:20]
        index = os.path.join(KITS_DIR, "_index", fp + ".json")
        os.makedirs(os.path.dirname(index), exist_ok=True)
        resume = False
        if not req.fresh and os.path.exists(index):
            told = False
            while True:
                rec = json.load(open(index))
                if rec.get("response"):
                    print(kit_builder.PREFIX + json.dumps({"t": 0, "stage": "done", "status": "done", "message":
                          f"identical request: returning finished kit {rec['kit_id']}"}), flush=True)
                    return KitResponse(**rec["response"])
                alive = os.path.join(KITS_DIR, rec["kit_id"], "out", ".alive")
                if not os.path.exists(alive) or time.time() - os.path.getmtime(alive) > 90:
                    break  # its attempt died: resume it below
                if not told:
                    print(kit_builder.PREFIX + json.dumps({"t": 0, "stage": "received", "status": "running", "message":
                          f"identical request: attached to kit {rec['kit_id']}, still running"}), flush=True)
                    told = True
                await asyncio.sleep(20)
            kit_id, resume = rec["kit_id"], True
        else:
            kit_id = "k_" + uuid.uuid4().hex[:10]
            json.dump({"kit_id": kit_id, "fingerprint": fp, "started": time.time()}, open(index, "w"))
        work = os.path.join(KITS_DIR, kit_id)
        os.makedirs(os.path.join(work, "out"), exist_ok=True)

        async def keep_alive():  # proves this attempt is alive to any retry of the same request
            while True:
                open(os.path.join(work, "out", ".alive"), "w").close()
                await asyncio.sleep(20)
        alive_task = asyncio.create_task(keep_alive())
        try:
            return await self._kit_in(req, kit_id, work, index, resume, t0)
        finally:
            alive_task.cancel()

    async def _kit_in(self, req, kit_id, work, index, resume, t0) -> KitResponse:
        import asyncio
        import time
        import urllib.request
        import zipfile

        from fal.toolkit import File
        from PIL import Image

        os.makedirs(os.path.join(work, "input"), exist_ok=True)
        upload = lambda p: File.from_path(p).url
        if resume:  # the workspace is there: refresh the shared tools, keep the kit's own scripts and assets
            kit_builder.write_toolkit(work, TOOLKIT)
        else:
            pngs = []
            for i, url in enumerate(req.reference_image_urls[:4]):
                src = os.path.join(work, "input", f"src_{i + 1}")
                urllib.request.urlretrieve(url, src)
                out = os.path.join(work, f"ref_{i + 1}.png")
                Image.open(src).convert("RGB").save(out)
                pngs.append(out)
            kit_builder.make_workspace(work, pngs, req.prompt, TOOLKIT, req.dials, req.items,
                                       req.reference_image_urls[:4])
        refs = sorted(os.path.join(work, "input", f) for f in os.listdir(os.path.join(work, "input"))
                      if f.startswith("reference_") and f.endswith(".png"))

        b = kit_builder.KitBuilder(work, refs, uploader=upload, effort=req.effort)
        if resume:
            b.ASK = ("This kit build was cut off (its request was retried) and is resuming in the same workspace. Read "
                     "out/builder.log, out/events.jsonl and scenes/kit/NOTES.md to see how far it got. Everything "
                     "already generated is on disk, and generate/fal.mjs and kit/gen.mjs skip finished outputs and pick "
                     "up jobs that were in flight, so re-running your batch scripts costs nothing for done items. Don't "
                     "start over: continue from the current state to the deliverables.")
        b.emit("received", "running", f"{'resuming' if resume else 'build'} {kit_id}: kit from {len(refs)} style reference(s)",
               reference_urls=req.reference_image_urls, prompt=req.prompt, items=req.items, dials=req.dials,
               effort=req.effort)
        result = None
        try:
            result = await b.run(kit_builder.agent_env())
        except Exception as e:
            import traceback
            print(traceback.format_exc(), flush=True)
            b.emit("error", "failed", f"kit build stopped: {type(e).__name__}: {str(e)[:300]}")

        out = os.path.join(work, "out")
        url_if = lambda p: upload(p) if os.path.exists(p) else None
        kit = b.kit() or {}
        items = []
        for it in kit.get("items", []):
            i = it.get("id", "")
            d = os.path.join(out, "items", i)
            items.append(KitItem(
                id=i, label=it.get("label", ""), dims_m=it.get("dims_m") or [], tris=it.get("tris"),
                tris_lod1=it.get("tris_lod1"), generator=it.get("generator"), why=it.get("why"),
                glb_url=url_if(os.path.join(d, f"{i}.glb")), lod1_url=url_if(os.path.join(d, f"{i}_lod1.glb")),
                thumb_url=url_if(os.path.join(out, "renders", "thumbs", f"{i}.png")),
                turntable_url=url_if(os.path.join(out, "renders", "turntables", f"{i}.mp4"))))
        pkg = os.path.join(work, f"{kit_id}.zip")
        with zipfile.ZipFile(pkg, "w", zipfile.ZIP_DEFLATED) as z:
            for root, _, names in os.walk(out):
                for n in names:
                    z.write(os.path.join(root, n), os.path.relpath(os.path.join(root, n), work))
            for sub in ("scenes/kit", "refs/kit"):
                for root, _, names in os.walk(os.path.join(work, sub)):
                    for n in names:
                        if n.endswith((".py", ".json", ".md", ".sh", ".png")):
                            z.write(os.path.join(root, n), os.path.relpath(os.path.join(root, n), work))
        dials_path = os.path.join(out, "dials.json")
        res = KitResponse(
            kit_id=kit_id, name=kit.get("name"), style=kit.get("style"), items=items,
            lineup_url=url_if(os.path.join(out, "renders", "lineup.png")),
            kit_blend_url=url_if(os.path.join(out, "kit.blend")), kit_json_url=url_if(os.path.join(out, "kit.json")),
            package_url=upload(pkg),
            critiques=[{k: c[k] for k in ("round", "scores", "passed", "summary", "fixes")} for c in b.critiques],
            dials=json.load(open(dials_path)) if os.path.exists(dials_path) else {},
            missing=b.missing(), agent_cost_usd=getattr(result, "total_cost_usd", None),
            generation_cost_usd=kit_builder.generation_spend(work), minutes=round((time.time() - t0) / 60, 1))
        save_optix_cache()
        rec = json.load(open(index))
        rec.update(response=res.model_dump(), finished=time.time())
        json.dump(rec, open(index, "w"))
        b.emit("packaged", "done", f"package ready after {res.minutes:.0f} min", package_url=res.package_url,
               lineup_url=res.lineup_url)
        await asyncio.sleep(2)  # let the last stage lines reach the queue logs before COMPLETED
        return res

    # The shared gallery (agent/gallery.py, synced from AnyScene): every kit anyone ran, read from storage.
    @fal.endpoint("/kits")
    async def kits(self, req: GalleryRequest) -> dict:
        """Every kit on this app's storage, newest first: status, inputs, critic, lineup and thumbnail URLs."""
        import asyncio
        return await asyncio.to_thread(gallery.list_builds, KITS_DIR, "kit", max(1, min(req.limit, 1000)))

    @fal.endpoint("/kit_events")
    async def kit_events(self, req: KitEventsRequest) -> dict:
        """One kit's stage events from byte offset `after`, its summary, and on the first page of a finished kit its
        response."""
        import asyncio

        from fal.toolkit import File
        return await asyncio.to_thread(gallery.build_events, KITS_DIR, req.kit_id, req.after, "kit",
                                       req.heartbeats, 8 << 20, lambda p: File.from_path(p).url)

    @fal.endpoint("/smoke")
    async def smoke(self, req: KitRequest) -> dict:
        """Container check: tools present, the agent starts and runs Bash/Blender/node, the kit critic answers."""
        import subprocess
        vers = {c[0]: subprocess.run(c, capture_output=True, text=True).stdout.splitlines()[:1]
                for c in (["blender", "--version"], ["node", "--version"], ["ffmpeg", "-version"], ["nvidia-smi", "-L"])}
        from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, ResultMessage, TextBlock
        work = "/tmp/smoke"
        os.makedirs(work, exist_ok=True)
        said, cost = [], None
        env = kit_builder.agent_env()
        opts = ClaudeAgentOptions(model=env.get("ANTHROPIC_MODEL") or kit_builder.MODEL, effort="low",
                                  permission_mode="bypassPermissions", cwd=work, setting_sources=[],
                                  settings='{"fastMode": true}', env=env)
        async with ClaudeSDKClient(opts) as c:
            await c.query("Run `blender -b --python-expr \"import bpy; print('BPY', bpy.app.version_string)\"` and "
                          "`node -e \"console.log(typeof fetch)\"`, then reply with both outputs in one line.")
            async for m in c.receive_response():
                if isinstance(m, AssistantMessage):
                    said += [b.text for b in m.content if isinstance(b, TextBlock)]
                elif isinstance(m, ResultMessage):
                    cost = m.total_cost_usd
        from PIL import Image
        Image.new("RGB", (256, 128), (40, 60, 50)).save(f"{work}/plate.png")
        try:
            crit = kit_builder.run_critic([f"{work}/plate.png"], [f"{work}/plate.png"], "smoke test: a grey plate",
                                          "- plate: test plate, 20 x 20 x 2 cm, 2 tris")
            critic = {"ok": True, "scores": crit["scores"]}
        except Exception as e:
            critic = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
        toolkit = sorted(TOOLKIT)
        return {"versions": vers, "agent": said[-1] if said else None, "cost": cost, "critic": critic,
                "toolkit": toolkit}
