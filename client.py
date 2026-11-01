"""Call AnyKit and print its live log.

    export ANYKIT_APP=<your-fal-username>/anykit FAL_KEY=...
    uv run --python 3.13 --with fal-client python client.py kit <style image path or url> [...] \
        --prompt "1970s mission-control desk clutter" [--items "a,b,c"] [--dial count=9 --dial wear=0.4] [--out DIR]
    uv run --python 3.13 --with fal-client python client.py smoke

Local images are uploaded to fal storage first. Every stage event goes to <out>/events.jsonl (for UI replay); the
response, the lineup and the thumbnails are saved to --out. The fal key is FAL_SERVERLESS_KEY if set (a key of the
account the app is deployed under, when that differs from your everyday FAL_KEY), else FAL_KEY.
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

# The fal app id, <owner>/<app>: set ANYKIT_APP=<your-fal-username>/anykit, or pass --app. No default.
APP_ENV = "ANYKIT_APP"
PREFIX = "ANYSCENE_STAGE "


def user_env(name):
    if os.environ.get(name):
        return os.environ[name]
    if sys.platform == "win32":
        parts = subprocess.run(["reg", "query", r"HKCU\Environment", "/v", name], capture_output=True, text=True).stdout.split()
        if name in parts:
            return parts[parts.index(name) + 2]
    return None


def dial(s):
    k, v = s.split("=", 1)
    for cast in (int, float):
        try:
            return k, cast(v)
        except ValueError:
            pass
    return k, v


def main():
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["kit", "smoke"])
    p.add_argument("refs", nargs="*")
    p.add_argument("--prompt", default="")
    p.add_argument("--items", default="")
    p.add_argument("--dial", action="append", default=[])
    p.add_argument("--effort", default="medium")
    p.add_argument("--out", default="kit_out")
    p.add_argument("--app", help="fal app id (default: $ANYKIT_APP)")
    a = p.parse_args()
    app = (a.app or user_env(APP_ENV) or "").strip().strip("/")
    if not app:
        p.error(f"set {APP_ENV}=<your-fal-username>/anykit or pass --app")
    sys.stdout.reconfigure(encoding="utf-8")
    key = user_env("FAL_SERVERLESS_KEY") or user_env("FAL_KEY")
    if not key:
        p.error("set FAL_KEY (or FAL_SERVERLESS_KEY) to a key of the fal account the app is deployed under")
    os.environ["FAL_KEY"] = key
    import fal_client

    os.makedirs(a.out, exist_ok=True)
    if a.cmd == "smoke":
        res = fal_client.subscribe(f"{app}/smoke", arguments={"reference_image_urls": []}, client_timeout=3600)
        json.dump(res, open(os.path.join(a.out, "smoke.json"), "w"), indent=1)
        print(json.dumps(res, indent=1))
        return
    up = lambda s: s if s.startswith("http") else fal_client.upload_file(s)
    payload = {"reference_image_urls": [up(s) for s in a.refs], "prompt": a.prompt, "effort": a.effort,
               "items": [s.strip() for s in a.items.split(",") if s.strip()], "dials": dict(map(dial, a.dial))}
    events = open(os.path.join(a.out, "events.jsonl"), "w", encoding="utf-8")
    t0, seen = time.time(), set()

    def on_update(update):
        for log in getattr(update, "logs", None) or []:
            msg = log.get("message", "")
            if msg in seen:
                continue
            seen.add(msg)
            if PREFIX not in msg:
                if any(k in msg for k in ("Traceback", "Error", "error:", "Exception")):
                    print(f"[worker] {msg[:600]}", flush=True)
                continue
            ev = json.loads(msg.split(PREFIX, 1)[1])
            events.write(json.dumps(ev) + "\n")
            events.flush()
            if ev["stage"] == "heartbeat":
                continue
            line = f"[{(time.time() - t0) / 60:6.1f}m] {ev['stage']:<10} {ev.get('message', '')[:160]}"
            if ev.get("preview_url"):
                line += f"\n{'':20}image: {ev['preview_url']}"
            print(line, flush=True)

    res = fal_client.subscribe(f"{app}/ref2kit", arguments=payload, with_logs=True, on_queue_update=on_update,
                               on_enqueue=lambda rid: print(f"request {rid}", flush=True), client_timeout=8 * 3600)
    with open(os.path.join(a.out, "kit.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, indent=1)
    if res.get("lineup_url"):
        urllib.request.urlretrieve(res["lineup_url"], os.path.join(a.out, "lineup.png"))
    for it in res.get("items", []):
        if it.get("thumb_url"):
            urllib.request.urlretrieve(it["thumb_url"], os.path.join(a.out, f"thumb_{it['id']}.png"))
    print(json.dumps({k: v for k, v in res.items() if k not in ("critiques", "items")}, indent=1)[:3000])


if __name__ == "__main__":
    main()
