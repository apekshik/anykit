# AnyKit: cohesive, production-ready 3D prop kits from style references

Part of a family: [AnyScene](https://github.com/apekshik/anyscene) builds whole sets, AnyKit builds the props that go
in them, and [AnyShot](https://github.com/apekshik/anyshot) shoots the result.

Give AnyKit one to four style reference images (and, if you like, a prompt, an item list and a few dials). A Claude
agent builds a **kit**: a set of static 3D props in that one style, at real-world scale, ready to drop into a game
engine or a Blender scene. Each item comes back as a GLB plus a LOD1, with a thumbnail; the kit comes with a Blender
asset library (`kit.blend`), a lineup render with a scale bar, a `kit.json` manifest and a zip of everything.

## Why: raw Meshy and Tripo output isn't production-ready

Image-to-3D models are good, but what they hand back is a single mesh in its own world: random orientation, no real
size, glass baked opaque, real brands on the labels, millions of triangles, and no relation to the next item you
generate. Ten of them side by side don't look like one set. AnyKit is the art department that fixes that, item by item
and as a whole:

| Raw generator output | What AnyKit does |
|---|---|
| Ten props, ten different styles | **Cohesion**: every item's reference is generated from your style images with one shared style paragraph (era, palette, materials, wear, one fictional maker); all references are checked together on one sheet before any 3D, and outliers are regenerated |
| No real size: a mug as big as a fridge | **Real-world scale**: every item gets a looked-up real height (or dims for boxy things), metres, bottom-centre origin, checked in a lineup with a scale bar |
| Every model faces a different way | **Orientation normalized** to front -Y: Meshy already faces -Y; Tripo faces +X *and* hides a 20-30 deg rotation in its glTF node to match the photo's three-quarter view, which is dropped before the mesh is turned |
| Glass baked opaque | **Opaque glass made transmissive**, masked so labels and contents stay opaque |
| Real brands on labels, even when told not to | **Brands painted out** (texels and normal map) or the item regenerated with the kit's fictional maker |
| ~2M triangles, 4K textures | **Triangle budgets** per item from the `detail` dial, textures at 2K or less, plus a **LOD1** at about a quarter |
| One generator for everything | **A per-item generator choice**: every item goes through both Meshy 7.1 and Tripo H3.1, and the pick is made by looking at same-light beauty and clay renders. Meshy wins about 70% (small items, sharp printed labels); Tripo wins big hard-surface items where real geometry matters |
| Nobody checks the set | **A critic that judges the set**: an independent Claude reviewer scores the lineup and thumbnails against your references (cohesion, fidelity, brief, mesh, texture, assembly, scale, labels) and the agent keeps fixing until it passes |

## AnyKit or AnyScene?

| | AnyScene | AnyKit |
|---|---|---|
| Builds | a whole set: room, dressing, lights, haze, cameras | granular props, one style, to drop into **your** scenes |
| Output | a dressed `scene.blend`, stills, walkthroughs | per-item GLB + LOD1, `kit.blend` asset library, lineup |
| Typical cost | about $100 | about $25 |
| Typical time | 1-2 hours | 25-40 minutes |

Same agent harness, same generators, same storage layout: deployed on one fal account they share image layers, an
OptiX cache and the `/data` volume.

## How it works

```
request ─▶ AnyKitApp (app.py, fal, 1 GPU)
           ├ fetch the style references, make the workspace
           └ Claude agent (agent/kit_builder.py, agent/PROMPT.md, agent/KIT.md)
               plan the kit: items, real sizes, one shared style paragraph
               references: GPT Image edit with your images as input (kit/gen.mjs), checked on one sheet
               3D: every reference to Tripo H3.1 AND Meshy 7.1 at once (generate/fal.mjs)
               as meshes land: same-light compare, pick, normalize + LOD1 in one Blender session (kit/tools/)
               fixes by script: glass, screens, brands, parts that should be connected
               lineup + thumbnails ─▶ critique ─▶ fix ─▶ critique ... until it passes
               kit.blend asset library
           └ per-item GLBs, kit.blend, lineup, kit.json, package zip ─▶ response
```

The agent is Claude Code driven through the Claude Agent SDK, with its instructions (`agent/PROMPT.md`) appended to
the system prompt and `KIT.md` in its workspace. `agent/kit_builder.py` runs it: a `report` tool for stage events, a
`critique` tool (the kit critic), hooks that stream every tool call, a 20-second heartbeat, a Stop hook that keeps it
going until every deliverable exists and the critic passes, a guard that stops it before fal's 2-hour request limit
so the app can still deliver, and session resume when the SDK session breaks. It is not a fixed pipeline: the tools in
`kit/tools/` are starting points, and the agent writes scripts for whatever it sees wrong.

Each request is retry-safe: an identical request (same references, prompt, items, dials and effort) returns the
finished kit, attaches to the attempt still running, or resumes a dead one in place, without paying again for
generations already on disk. The retry index lives under `/data/anyscene/kit/_index`.

## Repository

```
app.py              the fal app (AnyKitApp): /ref2kit (alias /kit), /kits, /kit_events, /smoke; retry index, packaging
client.py           the command-line client
agent/
  kit_builder.py    the agent harness (Claude Agent SDK), the kit critic, the workspace, the definition of done
  llm.py            the critic's Claude client (OpenRouter or the Anthropic API)
  PROMPT.md         the agent's instructions: the job, deliverables, dials, speed, the critic, reporting
  KIT.md            what makes a kit good, and the per-item process with every gotcha (in the agent's workspace)
  gallery.py        the kit gallery behind /kits and /kit_events (synced from AnyScene)
  blender_gpu.py    forces Cycles onto the GPU in every Blender the agent starts (synced from AnyScene)
kit/
  gen.mjs           style-conditioned item references: GPT Image edit with the style images as input
  tools/            compare.py (Tripo vs Meshy sheets), normalize.py (prep + LOD1 + kit.json), present.py (lineup,
                    thumbnails, turntables), library.py (kit.blend)
generate/fal.mjs    fal client: images and image-to-3D (Tripo H3.1, Meshy 7.1), with a spend log (synced from AnyScene)
pipeline/           env.sh (finds Blender and ffmpeg), gpu_run.sh (one Blender GPU job at a time) (synced from AnyScene)
scripts/            sync_from_anyscene.py: refresh the synced files from a sibling ../anyscene checkout
examples/           dai_pai_dong.jpg: a style reference to try (AI-generated)
```

The files marked "synced from AnyScene" carry a header naming their source. Their home is AnyScene; edit them there,
then run `python scripts/sync_from_anyscene.py` with AnyScene checked out next to this repo.

## Deploy your own

You need a fal account with Serverless access and an [OpenRouter](https://openrouter.ai) or Anthropic API key. Run
every command from the repository root.

1. **Secrets.** fal secrets are shared by every app on the account, so set each once:

   ```bash
   fal secrets set OPENROUTER_API_KEY=...     # Claude through OpenRouter (or ANTHROPIC_API_KEY=... for the Anthropic API)
   fal secrets set FAL_KEY=...                # a fal key of this account: generation (GPT Image, Tripo, Meshy) and uploads
   ```

   With both OPENROUTER_API_KEY and ANTHROPIC_API_KEY set, OpenRouter is used.

2. **Deploy:**

   ```bash
   fal deploy app.py::AnyKitApp --app-name anykit --auth private
   ```

   Requirements are listed on the app class (`fal==1.84.0`, `claude-agent-sdk==0.2.176`, `anthropic`, `pydantic`,
   `pillow`, `fal-client`); the deploying Python needs the same packages, e.g.

   ```bash
   uv run --python 3.13 --with fal==1.84.0 --with pydantic --with anthropic --with pillow --with fal-client \
       --with claude-agent-sdk==0.2.176 fal deploy app.py::AnyKitApp --app-name anykit --auth private
   ```

   The worker image installs Blender 5.0.1, ffmpeg and node, and runs on an RTX 5090 (or an RTX PRO 6000).
   `request_timeout` is 7200 s (fal's cap): the agent stops itself at 110 minutes to deliver what it has, and a retried
   request resumes the kit.

3. **Check it** (a couple of minutes, a few cents): `/smoke` checks the container (Blender, node, ffmpeg, the GPU),
   starts the agent once and calls the critic.

   ```bash
   export ANYKIT_APP=<your-fal-username>/anykit FAL_KEY=...
   python client.py smoke
   ```

Storage: kits, their workspaces and the retry index live under `/data/anyscene/kit/` on the account's persistent
`/data` volume, and the OptiX kernel cache at `/data/anyscene/cache/optix7cache.db`. These are AnyScene's paths on
purpose: deployed next to AnyScene, the two share the cache, and kits built before AnyKit had its own repo stay in the
gallery.

## API

### `POST /ref2kit` (alias `/kit`)

```jsonc
{
  "reference_image_urls": ["https://..."],   // 1-4 style references (required); more than 4 are ignored
  "prompt": "1970s mission-control desk clutter, 8-10 items",   // optional
  "items": ["rotary desk phone", "ashtray"], // optional: built exactly, in this order
  "dials": { "count": 9, "wear": 0.4 },      // optional, see Dials
  "effort": "medium",                        // agent effort: low | medium (default) | high | xhigh | max
  "fresh": false                             // build again even if an identical request already finished
}
```

Response:

```jsonc
{
  "kit_id": "k_3f9a1c2b7d",
  "name": "Apollo-era flight desk",
  "style": "one paragraph: era, palette, materials, wear, fictional maker",
  "items": [
    {
      "id": "rotary_phone",
      "label": "Rotary desk phone",
      "dims_m": [0.22, 0.24, 0.13],        // width x (X), depth y (Y), height z (Z), metres
      "tris": 98000, "tris_lod1": 24500,
      "generator": "meshy",                // meshy | tripo-h31 (the pick)
      "why": "sharper dial numerals; Tripo fused the handset to the cradle",
      "glb_url": "https://...",            // LOD0: metres, bottom-centre origin, front faces -Y, textures <= 2K
      "lod1_url": "https://...",
      "thumb_url": "https://...",          // three-quarter front
      "turntable_url": null                // mp4, when the agent made one
    }
  ],
  "lineup_url": "https://...",             // every item on a neutral ground, labels, scale bar
  "kit_blend_url": "https://...",          // asset library: every item marked as an asset, LOD1s in a hidden collection
  "kit_json_url": "https://...",           // the items above + tags and the fixes made per item
  "package_url": "https://...",            // zip: out/ (GLBs, renders, kit.blend, kit.json, dials.json, critique/),
                                           //      scenes/kit/ (the agent's scripts, NOTES.md), refs/kit/ (item references)
  "critiques": [
    { "round": 1, "passed": false, "summary": "...", "fixes": ["..."],
      "scores": { "cohesion": 7, "reference_fidelity": 8, "brief": 8, "mesh_quality": 6,
                  "texture_quality": 7, "assembly": 7, "scale": 8, "labels": 6 } }
  ],
  "dials": { "wear": { "value": 0.4, "source": "customer", "means": "scuffed bakelite, worn paint on edges" } },
  "missing": [],                           // deliverables not produced (empty when complete)
  "agent_cost_usd": 4.1,                   // the Claude agent
  "generation_cost_usd": 17.3,             // fal generation, estimated from the spend log
  "minutes": 31.0
}
```

The kit passes when no critic score is below 6 and the average is 7 or more: a production-ready base that a studio's
artists then refine, judged at normal viewing distance, with the reference setting the world rather than designs to
copy. The critic gets at most 8 rounds; a kit stopped at the time cap, after its 8th round, or after three rounds
without improvement is still returned, with the last critique and `missing` saying what is short.

### Dials

Plain settings the agent builds to. A dial that is set is an instruction, even when the reference says otherwise. A
dial left out gets ONE value read from the references and prompt. The values used come back in the response's
`dials`, each with its `source` (`customer` or `reference`) and what it meant for this kit.

| Dial | Range | Controls |
|---|---|---|
| `count` | integer | how many items (default: the item list's length, else the prompt, else 8) |
| `variety` | 0 - 1 | 0 = variations of one object family (matching mugs, jars); 1 = a broad mix that only shares the style |
| `detail` | `low` \| `medium` \| `high` | per-item budget: low <= 20k tris, 1K textures; medium <= 100k, 2K; high <= 200k, 2K. LOD1 is about a quarter |
| `wear` | 0 pristine - 1 decayed | dirt, scratches, chips, fading |
| `wetness` | 0 dry - 1 soaked | water on surfaces |
| `warmth` | -1 cold - 1 warm | palette colour temperature and the presentation light |
| `look` | `clean` \| `cinematic` \| `gritty` \| `vintage` | surface finish and the presentation renders' grade |

### Live progress

The agent streams its progress as `ANYSCENE_STAGE {json}` lines in the request's logs (`with_logs=true` on the
queue's status call). Each event has `t` (seconds), `stage`, `status` (`running` / `done` / `failed`), `message`,
and optionally a `preview_url`. Milestones are `plan`, `references`, `models`, `picks`, `prep`, `fixes`, `lineup`,
`critique`, `finals`, `done`, with retakes allowed; around them come `received`, `log` (one per tool call), `thinking`,
`heartbeat` (every 20 s, with the milestones so far), `error`, and `packaged` last. The prefix is shared with AnyScene
and AnyShot so any of them can relay the others' events.

### Gallery: `POST /kits`, `POST /kit_events`

Kits are read from the app's storage, not the fal queue, so anyone with access to the app sees every kit:

- `/kits` `{"limit": 200}`: every kit, newest first: `{"builds": [{"id", "status", "passed", "prompt",
  "reference_urls", "dials", "name", "critic_rounds", "last_scores", "minutes", "cover", "previews", "package_url",
  ...}], "total", "now"}`.
- `/kit_events` `{"kit_id": "k_...", "after": 0}`: one kit's stage events from byte offset `after` (then the previous
  call's `next`), its summary, and on the last page of a finished kit its response.

### Cost and time

In earlier runs (one style reference, 10 items, effort `medium`) a kit took **about 25-40 minutes and about $25
all-in**: roughly $17-18 of fal generation (a style-conditioned reference plus both Tripo and Meshy per item, about
$1.70 an item) and $3-6 of Claude, plus the GPU worker's time. Use the queue, not a synchronous call.

### curl

```bash
# submit (any image URL works; client.py uploads local files to fal storage)
curl -s https://queue.fal.run/<your-fal-username>/anykit/ref2kit \
  -H "Authorization: Key $FAL_KEY" -H "Content-Type: application/json" \
  -d '{
        "reference_image_urls": ["https://example.com/my_style_reference.jpg"],
        "prompt": "Hong Kong dai pai dong street kitchen and table goods",
        "dials": {"count": 10, "wear": 0.6}
      }'
# -> {"request_id": "...", "status_url": ".../requests/<id>/status", "response_url": ".../requests/<id>"}

curl -s "https://queue.fal.run/<your-fal-username>/anykit/requests/<id>/status?logs=1" -H "Authorization: Key $FAL_KEY"
curl -s "https://queue.fal.run/<your-fal-username>/anykit/requests/<id>" -H "Authorization: Key $FAL_KEY"
```

### Python

```python
import fal_client  # FAL_KEY in the environment

ref = fal_client.upload_file("examples/dai_pai_dong.jpg")
res = fal_client.subscribe(
    "<your-fal-username>/anykit/ref2kit",
    arguments={"reference_image_urls": [ref], "prompt": "Hong Kong dai pai dong street kitchen and table goods",
               "dials": {"count": 10}},
    with_logs=True,
    on_queue_update=lambda u: [print(l["message"]) for l in getattr(u, "logs", None) or []],
    client_timeout=3 * 3600,
)
print(res["lineup_url"], [it["glb_url"] for it in res["items"]])
```

### Command line

`client.py` uploads local images, prints the agent's live log and saves the response, the lineup and the thumbnails:

```bash
pip install fal-client                     # or prefix the commands with: uv run --with fal-client
export FAL_KEY=... ANYKIT_APP=<your-fal-username>/anykit
python client.py kit examples/dai_pai_dong.jpg --prompt "Hong Kong dai pai dong street kitchen and table goods" \
    --dial count=10 --dial wear=0.6 --out kit_out
python client.py kit ref_a.png ref_b.png --items "rotary desk phone,ashtray,ring binder" --dial detail=low
python client.py smoke
```

`--app <owner>/<app>` overrides `ANYKIT_APP`. Results land in `--out` (default `kit_out/`): `kit.json` (the
response), `events.jsonl`, `lineup.png` and `thumb_<id>.png`.

### Run the agent locally

With Blender 5.0+, node 20+ and ffmpeg on your PATH, and `FAL_KEY` plus `OPENROUTER_API_KEY` (or `ANTHROPIC_API_KEY`)
in your environment:

```bash
uv run --python 3.13 --with claude-agent-sdk --with anthropic --with pydantic --with pillow \
    python agent/kit_builder.py examples/dai_pai_dong.jpg --work ../my_kit --prompt "street kitchen goods"
```

## License

MIT, see [LICENSE](LICENSE). `examples/dai_pai_dong.jpg` is an AI-generated image made for this project.
