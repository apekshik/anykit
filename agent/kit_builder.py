"""AnyKit: one Claude agent builds a cohesive kit of static 3D props from style reference image(s).

The agent is Claude Code driven through the Claude Agent SDK (Opus 5.5, fast mode; OpenRouter or the Anthropic API).
It works in a fresh workspace holding its instructions (.builder/PROMPT.md, KIT.md) and the kit tools (kit/,
generate/, pipeline/). On top of its own tools (Bash, Read, Write, Edit...) it gets:
  report(stage, message, image)   a stage event for the live log (ANYSCENE_STAGE lines, out/events.jsonl)
  critique(images)                an independent kit critic: style reference vs lineup + thumbnails, scores and fixes
Every tool call is also streamed as a short log event (PostToolUse hook), with a heartbeat every 20 s. A Stop hook
keeps the agent working until the deliverables exist and the critic passes (or its rounds run out, or three rounds in
a row bring no improvement); a cap guard stops it before fal's 2-hour request limit, and a broken session is resumed.
Done means out/kit.json, per-item GLB + LOD1 + thumbnail, out/kit.blend, out/renders/lineup.png, out/dials.json and a
passing critique.

The harness is the one AnyScene's scene builder uses (github.com/apekshik/anyscene builder/scene_builder.py), with
the kit's tools, critic and definition of done. Run it locally (Blender, node and ffmpeg on PATH):

    uv run --python 3.13 --with claude-agent-sdk --with anthropic --with pydantic --with pillow \
        python agent/kit_builder.py <reference.png> [...] --work <dir> [--prompt "..."] [--items "a,b,c"]
"""

import argparse
import asyncio
import base64
import io
import json
import os
import shutil
import subprocess
import sys
import time

from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, HookMatcher, ResultMessage,
                              TextBlock, create_sdk_mcp_server, tool)

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import llm  # noqa: E402  (the critic's Claude client; module level so fal bundles it)

PREFIX = "ANYSCENE_STAGE "  # shared with AnyScene and AnyShot, so either can relay the other's events
# fal ends a request at 7200 s; warn the agent, then stop it with time left to package and upload
CAP_WARN_S = 95 * 60
CAP_STOP_S = 110 * 60
# most critic rounds a kit gets; after the last one the agent finishes its deliverables and delivers
MAX_CRITIC_ROUNDS = 8
MODEL = "anthropic/claude-opus-5.5"  # through OpenRouter (OPENROUTER_API_KEY)
DIRECT_MODEL = "claude-opus-5-5"  # the same model on the Anthropic API (ANTHROPIC_API_KEY)
DIALS = ("count", "variety", "detail", "wear", "wetness", "warmth", "look")


def user_env(name):
    if os.environ.get(name):
        return os.environ[name]
    if sys.platform == "win32":
        parts = subprocess.run(["reg", "query", r"HKCU\Environment", "/v", name], capture_output=True, text=True).stdout.split()
        if name in parts:
            return parts[parts.index(name) + 2]
    return None


# ---------------------------------------------------------------- workspace
# repo path -> workspace path: everything the agent's workspace starts with
TOOLKIT = {
    "agent/PROMPT.md": ".builder/PROMPT.md",
    "agent/KIT.md": "KIT.md",
    "kit/gen.mjs": "kit/gen.mjs",
    "generate/fal.mjs": "generate/fal.mjs",
    "pipeline/env.sh": "pipeline/env.sh",
    "pipeline/gpu_run.sh": "pipeline/gpu_run.sh",
}


def toolkit_files():
    """Everything the agent's workspace starts with, as {workspace path: text}: its instructions, the kit tools, the fal
    client and the GPU queue. Read at deploy time so the fal app carries it."""
    files = {dst: open(os.path.join(REPO, src), encoding="utf-8").read() for src, dst in TOOLKIT.items()}
    for n in sorted(os.listdir(os.path.join(REPO, "kit", "tools"))):
        if n.endswith(".py"):
            files[f"kit/tools/{n}"] = open(os.path.join(REPO, "kit", "tools", n), encoding="utf-8").read()
    return files


def write_toolkit(work, files):
    for rel, text in files.items():
        path = os.path.join(work, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        if rel.endswith(".sh"):
            os.chmod(path, 0o755)


def make_workspace(work, refs, prompt, files, dials=None, items=None, ref_urls=None):
    os.makedirs(work, exist_ok=True)
    write_toolkit(work, files)
    inp = os.path.join(work, "input")
    os.makedirs(inp, exist_ok=True)
    for i, r in enumerate(refs):
        shutil.copy(r, os.path.join(inp, f"reference_{i + 1}.png"))
    for name, obj in (("dials.json", dials or {}), ("items.json", items or []), ("style_urls.json", ref_urls or [])):
        with open(os.path.join(inp, name), "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=1)
    with open(os.path.join(inp, "brief.md"), "w", encoding="utf-8") as f:
        f.write(prompt or "(no prompt: match the reference)\n")
    for d in ("scenes/kit/scripts", "out/critique", "out/items", "out/renders/thumbs", "refs/kit"):
        os.makedirs(os.path.join(work, d), exist_ok=True)
    open(os.path.join(work, "generate", "spend.jsonl"), "a").close()


def _jpeg(path, size=1600):
    from PIL import Image
    img = Image.open(path).convert("RGB")
    img.thumbnail((size, size))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88)
    return base64.standard_b64encode(buf.getvalue()).decode()


# ---------------------------------------------------------------- critic
CRITIC = """You review 3D prop kits for a service that turns style reference images into production-ready asset kits.
The first {n_refs} image(s) are the customer's STYLE REFERENCE. They asked for: {brief}

The remaining images are the delivered kit: first a lineup render (every item side by side on a neutral ground, with
a scale bar; its label under each item), then per-item thumbnails. The kit's manifest:
{manifest}

The reference is a starting point for the world, not a target to copy: the kit must clearly belong to the same world
(era, materials, palette, design language), but which items were chosen, their exact designs and the presentation
are the builder's own choices and must NOT be marked down for differing from the reference.

The bar is a production-ready base: a kit a studio would accept as the starting point its artists then refine. Judge
it as a whole, from normal viewing distance. Do NOT nitpick: no points off for a creative choice that differs from
the reference, a surface that is a little CG on close inspection, or anything an artist would tweak in ten minutes.

Score each category 1-10 (7 = a solid, usable production kit; 9 = exceptional):
- cohesion: the items read as one set: shared palette, material language, era, wear and level of detail;
- reference_fidelity: the kit belongs in the reference's world;
- brief: the items are what the customer asked for (prompt, item list, count), nothing off-topic;
- mesh_quality: clean recognisable shapes; nothing broken, blobby or melted;
- texture_quality: believable textured surfaces; nothing untextured, smeared or flat grey;
- assembly: parts that belong together are physically connected and seated: cords and cables plugged in at
  both ends, handsets on their cradles, lamp heads on their arms, lids on their hinges, nothing hovering or
  intersecting;
- scale: each item's real-world size (manifest dims, scale bar) is right, alone and relative to the others;
- labels: no real brands; no garbled text on hero labels.

Then list at most FIVE fixes, most damaging first: only problems that would stop a studio from using the kit as a
base (an item broken, melted or with disconnected parts, garbled hero labels, wrong world, untextured, badly wrong
scale). If the kit is already a good base, say so and list fewer or none. Pass when the kit is a good production
base: no category below 6 and an average of 7 or more."""


def critic_schema():
    from pydantic import BaseModel, Field

    class Scores(BaseModel):
        cohesion: int
        reference_fidelity: int
        brief: int
        mesh_quality: int
        texture_quality: int
        assembly: int
        scale: int
        labels: int

    class Critique(BaseModel):
        scores: Scores
        fixes: list[str] = Field(description="At most five, most damaging first; may be empty.")
        summary: str
    return Critique


def manifest_text(work):
    try:
        kit = json.load(open(os.path.join(work, "out", "kit.json"), encoding="utf-8"))
    except Exception:
        return "(out/kit.json not written yet)"
    rows = []
    for it in kit.get("items", []):
        d = it.get("dims_m") or []
        dims = " x ".join(f"{v * 100:.0f}" for v in d) + " cm (w x d x h)" if len(d) == 3 else "?"
        rows.append(f"- {it.get('id')}: {it.get('label', '')}, {dims}, {it.get('tris', '?')} tris")
    return "\n".join(rows) or "(no items)"


def run_critic(refs, images, brief, manifest):
    Critique = critic_schema()
    client, model, extra = llm.client()
    content = []
    for p in list(refs) + list(images):
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                    "data": _jpeg(p)}})
    content.append({"type": "text", "text": CRITIC.format(n_refs=len(refs), brief=brief, manifest=manifest)})
    with client.beta.messages.stream(model=model, max_tokens=16000, thinking={"type": "adaptive"},
                                     output_config={"effort": "high", "format": {"type": "json_schema",
                                                                                 "schema": llm.strict_schema(Critique)}},
                                     messages=[{"role": "user", "content": content}], **extra) as s:
        msg = s.get_final_message()
    c = Critique.model_validate_json(next(b.text for b in msg.content if b.type == "text"))
    d = c.model_dump()
    v = list(d["scores"].values())
    d["passed"] = min(v) >= 6 and sum(v) / len(v) >= 7  # a good production base
    return d


# ---------------------------------------------------------------- run
class KitBuilder:
    """One kit build: the agent session, its tools, hooks, heartbeat and cap guard, the critic and what "done" means."""

    ASK = "Build the kit from the style reference in input/. Start by reading KIT.md."

    def __init__(self, work, refs, uploader=None, emit=None, effort="medium"):
        self.effort = effort
        self.work, self.refs = work, refs
        self.out = os.path.join(work, "out")
        os.makedirs(os.path.join(self.out, "critique"), exist_ok=True)
        self.t0 = time.time()
        self.upload = uploader or (lambda p: p)
        self.events = open(os.path.join(self.out, "events.jsonl"), "a", encoding="utf-8")
        self.log = open(os.path.join(self.out, "builder.log"), "a", encoding="utf-8")
        self.critiques = []
        self.milestones = []  # every milestone so far, repeated in each heartbeat so a late watcher can backfill
        self.working = None  # (description, started) of the tool call in progress
        self.cap_warned = False
        self.capped = False  # stopped at the platform's 2-hour limit: let it end without a pass
        self._emit_hook = emit

    def elapsed(self):
        return time.time() - self.t0

    def emit(self, stage, status="running", message="", image=None, **extra):
        ev = {"t": round(self.elapsed(), 1), "stage": stage, "status": status, "message": message, **extra}
        if image:
            path = image if os.path.isabs(image) else os.path.join(self.work, image)
            if os.path.exists(path):
                ev["preview_url"] = self.upload(path)
        if stage not in ("log", "thinking", "heartbeat", "received", "error", "critique", "done", "packaged"):
            self.milestones.append({k: ev[k] for k in ("t", "stage", "status", "message", "preview_url") if k in ev})
        print(PREFIX + json.dumps(ev), flush=True)
        self.events.write(json.dumps(ev) + "\n")
        self.events.flush()
        if self._emit_hook:
            self._emit_hook(ev)

    def note(self, text):
        self.log.write(f"[{self.elapsed() / 60:6.1f}m] {text}\n")
        self.log.flush()

    def brief(self):
        txt = open(os.path.join(self.work, "input", "brief.md"), encoding="utf-8").read().strip()
        items = json.load(open(os.path.join(self.work, "input", "items.json"), encoding="utf-8"))
        dials = json.load(open(os.path.join(self.work, "input", "dials.json"), encoding="utf-8"))
        return (txt + (f"\nItems requested: {', '.join(items)}" if items else "")
                + (f"\nDials: {json.dumps(dials)}" if dials else ""))

    # ---- MCP tools
    def tools(self):
        d = self

        @tool("report", "Post a stage event to the customer's live log, optionally with a rendered image.",
              {"stage": str, "message": str, "image": str})
        async def report(args):
            d.emit(args["stage"], "running", args.get("message", ""), args.get("image") or None)
            return {"content": [{"type": "text", "text": "reported"}]}

        @tool("critique", "Independent kit critic: compares the style reference with your lineup and thumbnails "
                          "(paths relative to the workspace or absolute; the lineup FIRST, then up to 10 thumbnails). "
                          "It also reads out/kit.json for dims. Returns scores 1-10 per category, up to five fixes that "
                          "matter, and whether it passes (no category below 6, average 7+). At most "
                          f"{MAX_CRITIC_ROUNDS} rounds.",
              {"type": "object", "properties": {"images": {"type": "array", "items": {"type": "string"}}},
               "required": ["images"]})
        async def critique(args):
            if len(d.critiques) >= MAX_CRITIC_ROUNDS:
                return {"content": [{"type": "text", "text": (
                    f"The critic has had its {MAX_CRITIC_ROUNDS} rounds; no more reviews. Stop iterating on the look: "
                    "finish every remaining deliverable (items/<id>/<id>.glb and _lod1.glb, renders/lineup.png, "
                    "renders/thumbs/, kit.json, kit.blend, dials.json, NOTES.md) and deliver.")}]}
            paths = [p if os.path.isabs(p) else os.path.join(d.work, p) for p in args["images"]]
            missing = [p for p in paths if not os.path.exists(p)]
            if missing:
                return {"content": [{"type": "text", "text": f"missing: {missing}"}], "is_error": True}
            d.emit("critique", "running", f"critic is looking at the lineup and {len(paths) - 1} item renders")
            res = await asyncio.to_thread(run_critic, d.refs, paths[:11], d.brief(), manifest_text(d.work))
            res["round"] = len(d.critiques) + 1
            res["stills"] = paths
            d.critiques.append(res)
            json.dump(res, open(os.path.join(d.work, "out", "critique", f"round_{res['round']}.json"), "w"), indent=1)
            sc = res["scores"]
            d.emit("critique", "done" if res["passed"] else "running",
                   f"round {res['round']}: {'PASS' if res['passed'] else 'not yet'} - "
                   + ", ".join(f"{k} {v}" for k, v in sc.items()), paths[0], critique=res)
            return {"content": [{"type": "text", "text": json.dumps(res, indent=1)}]}

        return [report, critique]

    # ---- hooks
    @staticmethod
    def describe(name, ti, work):
        if name == "Bash":
            return ti.get("description") or ti.get("command", "")[:120]
        if name in ("Write", "Edit"):
            return f"{'writing' if name == 'Write' else 'editing'} {os.path.relpath(ti.get('file_path', ''), work)}"
        if name == "Read":
            p = ti.get("file_path", "")
            return f"{'looking at' if p.lower().endswith(('.png', '.jpg', '.jpeg')) else 'reading'} {os.path.relpath(p, work)}"
        return name

    async def pre_tool(self, inp, tool_use_id, ctx):
        self.working = (self.describe(inp.get("tool_name", ""), inp.get("tool_input") or {}, self.work), time.time())
        if self.elapsed() > CAP_WARN_S and not self.cap_warned:
            self.cap_warned = True
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": (
                "The platform ends this request at 120 minutes and the kit is now past 95. Before anything else, make "
                "sure out/ holds your best current version of every deliverable (kit.json, items/<id>/<id>.glb and "
                "_lod1.glb, renders/lineup.png, renders/thumbs/, kit.blend, dials.json), then keep improving.")}}
        return {}

    async def post_tool(self, inp, tool_use_id, ctx):
        self.working = None
        name = inp.get("tool_name", "")
        ti = inp.get("tool_input") or {}
        quiet = name.startswith("mcp__kit__") or name in ("ToolSearch", "TodoWrite")
        msg = None if quiet else self.describe(name, ti, self.work)
        if msg:
            # an image the agent opens is shown to the customer too (its sheets, compares, lineups), so the live view
            # follows what it is looking at; the customer's own reference is left out
            p = ti.get("file_path", "") if name == "Read" else ""
            image = p if p.lower().endswith((".png", ".jpg", ".jpeg")) and "/input/" not in p.replace("\\", "/") else None
            self.emit("log", "running", msg, image if image and os.path.exists(image) else None, tool=name)
        self.note(f"{name}: {json.dumps(ti)[:400]}")
        return {}

    async def heartbeat(self):
        """Every 20 s: what is running and for how long, plus the milestones so far. Long renders otherwise look idle,
        and a page that attaches late (fal's status call returns only the recent log) can fill in its schedule."""
        while True:
            await asyncio.sleep(20)
            w = self.working
            self.emit("heartbeat", "running", f"{self.elapsed() / 60:.0f} min in",
                      working=w[0] if w else None, working_s=round(time.time() - w[1]) if w else None,
                      milestones=self.milestones[-20:], critique_rounds=len(self.critiques))

    # ---- definition of done
    def kit(self):
        try:
            return json.load(open(os.path.join(self.work, "out", "kit.json"), encoding="utf-8"))
        except Exception:
            return None

    def missing(self):
        out = os.path.join(self.work, "out")
        miss = []
        kit = self.kit()
        if not kit or not kit.get("items"):
            miss.append("out/kit.json with an items list")
        else:
            for it in kit["items"]:
                i = it.get("id", "?")
                for rel in (f"items/{i}/{i}.glb", f"items/{i}/{i}_lod1.glb", f"renders/thumbs/{i}.png"):
                    if not os.path.exists(os.path.join(out, rel)):
                        miss.append(f"out/{rel}")
                for k in ("label", "dims_m", "tris", "generator", "why"):
                    if k not in it:
                        miss.append(f"kit.json item {i} has no {k}")
        for f in ("kit.blend", "renders/lineup.png", "dials.json"):
            if not os.path.exists(os.path.join(out, f)):
                miss.append(f"out/{f}")
        if not self.critiques:
            miss.append("no critique yet")
        elif not self.critiques[-1]["passed"] and len(self.critiques) < MAX_CRITIC_ROUNDS:
            miss.append("the last critique did not pass")
        return miss

    def stalled(self):
        """Three critic rounds in a row without beating the best total so far: the builder is going in circles."""
        totals = [sum(c["scores"].values()) for c in self.critiques]
        return len(totals) >= 4 and max(totals[-3:]) <= max(totals[:-3])

    async def on_stop(self, inp, tool_use_id, ctx):
        miss = self.missing()
        if miss and not self.stalled() and not self.capped:
            self.note(f"STOP blocked: {miss}")
            return {"decision": "block", "reason": "Not done: " + "; ".join(miss) + ". Keep going."}
        return {}

    async def run(self, env):
        prompt = open(os.path.join(self.work, ".builder", "PROMPT.md"), encoding="utf-8").read()
        server = create_sdk_mcp_server("kit", tools=self.tools())
        beat = asyncio.create_task(self.heartbeat())
        self.emit("received", "running", "kit builder starting")
        result, session_id, attempt = None, None, 0
        ask = self.ASK
        while True:
            opts = ClaudeAgentOptions(
                model=env.get("ANTHROPIC_MODEL") or MODEL, effort=self.effort, permission_mode="bypassPermissions",
                cwd=self.work,
                system_prompt={"type": "preset", "preset": "claude_code", "append": prompt},
                mcp_servers={"kit": server}, setting_sources=[], settings='{"fastMode": true}',
                hooks={"PreToolUse": [HookMatcher(hooks=[self.pre_tool])],
                       "PostToolUse": [HookMatcher(hooks=[self.post_tool])],
                       "Stop": [HookMatcher(hooks=[self.on_stop])]},
                # tool results come back as single JSON lines; a full-size render read as an image is several MB, and
                # the default 1 MB buffer kills the session
                max_buffer_size=256 * 1024 * 1024, resume=session_id,
                env=env, stderr=lambda l: self.note("STDERR " + l[:300]))
            try:
                async with ClaudeSDKClient(opts) as client:
                    async def cap_guard():
                        # fal kills the request at 2 h with nothing returned; stop the agent first so the endpoint can
                        # still package and return what is in out/
                        while self.elapsed() < CAP_STOP_S:
                            await asyncio.sleep(15)
                        self.capped = True
                        self.emit("error", "running", "stopping the kit builder before the platform's 2-hour limit; "
                                  "packaging what it has")
                        await client.interrupt()
                    guard = asyncio.create_task(cap_guard())
                    try:
                        await client.query(ask)
                        async for m in client.receive_response():
                            session_id = getattr(m, "session_id", None) \
                                or (getattr(m, "data", None) or {}).get("session_id") or session_id
                            if isinstance(m, AssistantMessage):
                                for b in m.content:
                                    if isinstance(b, TextBlock) and b.text.strip():
                                        self.note("SAY " + b.text.strip()[:2000])
                                        self.emit("thinking", "running", b.text.strip()[:400])
                            elif isinstance(m, ResultMessage):
                                result = m
                    finally:
                        guard.cancel()
                break
            except Exception as e:  # the session broke (SDK or CLI error): pick the same session back up
                import traceback
                if self.capped:
                    break
                attempt += 1
                self.note("SESSION ERROR " + traceback.format_exc()[-3000:])
                self.emit("error", "running", f"agent session error ({type(e).__name__}: {str(e)[:200]}); "
                          f"{'resuming' if attempt <= 3 and session_id else 'giving up'}")
                if attempt > 3 or not session_id:
                    raise
                ask = ("The previous turn was cut off by a harness error, not by you. Continue the kit from where "
                       "you were; check the state of the workspace first.")
        beat.cancel()
        cost = getattr(result, "total_cost_usd", None)
        self.emit("done", "done", f"kit builder finished after {self.elapsed() / 60:.0f} min"
                  + (f", agent cost ${cost:.2f}" if cost else ""), missing=self.missing(),
                  critiques=len(self.critiques))
        return result


def agent_env():
    """The agent's environment. Keys come only from the environment (fal secrets on a worker): OPENROUTER_API_KEY
    (Claude through OpenRouter, the default) or, without it, ANTHROPIC_API_KEY (the Anthropic API directly). FAL_KEY is
    passed through for the generate/ and kit/ tools. ANTHROPIC_MODEL in the result is the model id for that provider."""
    key = user_env("OPENROUTER_API_KEY")
    direct = None if key else user_env("ANTHROPIC_API_KEY")
    if not key and not direct:
        raise RuntimeError("set OPENROUTER_API_KEY or ANTHROPIC_API_KEY (a fal secret on the app)")
    fal = user_env("FAL_KEY") or ""
    common = {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "BASH_MAX_TIMEOUT_MS": "600000", "FAL_KEY": fal,
              "IS_SANDBOX": "1"}  # the fal worker runs as root in its own container; Claude Code needs this to skip prompts
    if direct:
        os.environ["ANTHROPIC_API_KEY"] = direct  # the critic (llm.client) then talks to Anthropic directly
        return {**common, "ANTHROPIC_BASE_URL": "https://api.anthropic.com", "ANTHROPIC_API_KEY": direct,
                "ANTHROPIC_MODEL": DIRECT_MODEL}
    env = {**common, "ANTHROPIC_BASE_URL": "https://openrouter.ai/api", "ANTHROPIC_AUTH_TOKEN": key, "ANTHROPIC_API_KEY": "",
           "ANTHROPIC_MODEL": MODEL, "ANTHROPIC_DEFAULT_OPUS_MODEL": MODEL,
           "ANTHROPIC_DEFAULT_SONNET_MODEL": "anthropic/claude-sonnet-5.5",
           "ANTHROPIC_DEFAULT_HAIKU_MODEL": "anthropic/claude-haiku-5.5", "OPENROUTER_API_KEY": key}
    os.environ["OPENROUTER_API_KEY"] = key  # the critic (llm.client) reads it
    os.environ.pop("ANTHROPIC_API_KEY", None)
    return env


def generation_spend(work):
    try:
        rows = [json.loads(l) for l in open(os.path.join(work, "generate", "spend.jsonl"), encoding="utf-8") if l.strip()]
    except OSError:
        return 0.0
    return round(sum(r.get("est_usd", 0) for r in rows), 2)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("refs", nargs="+")
    p.add_argument("--work", required=True)
    p.add_argument("--prompt", default="")
    p.add_argument("--items", default="")
    p.add_argument("--effort", default="medium")
    a = p.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    work = os.path.abspath(a.work)
    if not os.path.exists(os.path.join(work, "KIT.md")):
        make_workspace(work, a.refs, a.prompt, toolkit_files(), items=[s for s in a.items.split(",") if s])
    refs = sorted(os.path.join(work, "input", f) for f in os.listdir(os.path.join(work, "input")) if f.startswith("reference_")
                  and f.endswith(".png"))
    asyncio.run(KitBuilder(work, refs, effort=a.effort).run(agent_env()))


if __name__ == "__main__":
    main()
