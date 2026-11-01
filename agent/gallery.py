# synced from github.com/apekshik/anyscene builder/gallery.py
"""The shared builds gallery: every build anyone ran, read from the app's persistent storage (not the fal queue, which
only the submitter's key can read). Used by builder/app.py (/builds, /build_events) and by AnyKit's app.py (/kits,
/kit_events; github.com/apekshik/anykit keeps a synced copy). Stdlib only.

A build lives at <root>/<id>/ (b_... scenes, k_... kits). out/events.jsonl is its stage stream (scene_builder.emit),
out/.alive is touched every 20 s while an attempt runs, and <root>/_index/<fingerprint>.json (builds since the retry
fix) holds the finished response. Older builds have no index, so everything is derived from events.jsonl.

Summaries are folded incrementally (only the bytes added since the last look) and cached in memory and on disk at
<root>/_gallery/<id>.json, so a new runner does not re-read every finished build. Image URLs come from the events
(already on the fal CDN). A finished build without an index response gets its stills/previews/panoramas uploaded
once, on first open, and the URLs are cached in <root>/_gallery/<id>.files.json.
"""

import json
import os
import re
import threading
import time

ID_RE = re.compile(r"^[bk]_[0-9a-zA-Z]{4,40}$")
ALIVE_S = 90  # an attempt touches out/.alive every 20 s; heartbeats land every 20 s
CAP_RE = re.compile(r"2-hour limit", re.I)
SUMMARY_VERSION = 3
MAX_LIST_IMAGES = 16  # previews/stills per build in the light listing record
NON_MILESTONES = {"received", "log", "thinking", "critique", "done", "packaged", "heartbeat", "error", "previews"}

_lock = threading.Lock()
_mem: dict = {}  # (root, id) -> summary state
_index_cache: dict = {}  # root -> (signature, {id: record})
_list_cache: dict = {}  # (root, limit) -> (time, result)


# ---------------------------------------------------------------- helpers
def _mtime(p):
    try:
        return os.path.getmtime(p)
    except OSError:
        return None


def _gallery_dir(root):
    d = os.path.join(root, "_gallery")
    os.makedirs(d, exist_ok=True)
    return d


def _write_json(path, obj):
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def _index(root):
    """{id: index record} from <root>/_index/*.json, re-read only when a file there changed."""
    d = os.path.join(root, "_index")
    try:
        ents = [e for e in os.scandir(d) if e.name.endswith(".json")]
    except OSError:
        return {}
    sig = tuple(sorted((e.name, e.stat().st_mtime, e.stat().st_size) for e in ents))
    hit = _index_cache.get(root)
    if hit and hit[0] == sig:
        return hit[1]
    out = {}
    for e in ents:
        try:
            rec = json.load(open(e.path, encoding="utf-8"))
        except Exception:
            continue
        bid = rec.get("build_id") or rec.get("kit_id")
        if bid and (bid not in out or (rec.get("started") or 0) >= (out[bid].get("started") or 0)):
            out[bid] = rec
    _index_cache[root] = (sig, out)
    return out


def _started(work, rec):
    if rec and rec.get("started"):
        return rec["started"]
    cands = [_mtime(os.path.join(work, p)) for p in ("input/brief.md", "input/dials.json", "ref_1.png", "input")]
    cands = [c for c in cands if c]
    return min(cands) if cands else (_mtime(work) or 0)


# ---------------------------------------------------------------- fold events into a summary
def _empty_state():
    return {"v": SUMMARY_VERSION, "offset": 0, "given": None, "rounds": 0, "last_critique": None, "critiques": [],
            "previews": [], "frames": 0, "cover": None, "cover_rank": 0, "last_milestone": None, "packaged": False,
            "package_url": None, "capped": False, "failed": None, "agent_done": False, "events": 0,
            "milestones": [], "last_t": 0, "attempts": 0}


# what makes a good cover: a finals render beats a critic still beats any milestone beats any frame
_RANK = {"finals": 5, "critique": 4, "previews": 2}


def _fold(st, ev):
    st["events"] += 1
    stage = ev.get("stage", "")
    if isinstance(ev.get("t"), (int, float)):
        st["last_t"] = ev["t"]
    if stage == "received":
        if ev.get("reference_urls") is not None and st["given"] is None:
            st["given"] = {k: ev.get(k) for k in ("reference_urls", "prompt", "dials", "effort", "floor_plan_url", "items")
                           if k in ev}
        if re.match(r"(build|resuming) [bk]_", ev.get("message", "")):
            st["attempts"] += 1
    url = ev.get("preview_url")
    if url:
        st["frames"] += 1
        rank = _RANK.get(stage, 3 if stage not in NON_MILESTONES else 1)
        if rank >= st["cover_rank"]:
            st["cover"], st["cover_rank"] = url, rank
    if stage == "previews" and url and not any(p["url"] == url for p in st["previews"]):
        st["previews"].append({"url": url, "message": (ev.get("message") or "")[:200]})
    if stage == "critique" and ev.get("critique"):
        c = ev["critique"]
        slim = {k: c.get(k) for k in ("round", "scores", "passed", "summary") if k in c}
        st["critiques"] = [x for x in st["critiques"] if x.get("round") != slim.get("round")] + [slim]
        st["rounds"] = max(st["rounds"], int(c.get("round") or 0))
        st["last_critique"] = slim
    if stage not in NON_MILESTONES and stage:
        st["last_milestone"] = {"stage": stage, "message": (ev.get("message") or "")[:300], "t": ev.get("t")}
        if stage not in st["milestones"]:
            st["milestones"].append(stage)
    if stage == "done":
        st["agent_done"] = True
    if stage == "packaged":
        st["packaged"] = True
        st["package_url"] = ev.get("package_url") or st["package_url"]
    if stage == "error" and CAP_RE.search(ev.get("message", "")):
        st["capped"] = True
    if ev.get("status") == "failed":
        st["failed"] = (ev.get("message") or "")[:300]


def _state(root, bid):
    """The folded summary state of one build, updated with whatever was appended since the last look."""
    ev_path = os.path.join(root, bid, "out", "events.jsonl")
    try:
        size = os.path.getsize(ev_path)
    except OSError:
        size = 0
    key = (root, bid)
    st = _mem.get(key)
    disk = os.path.join(_gallery_dir(root), bid + ".json")
    if st is None and os.path.exists(disk):
        try:
            st = json.load(open(disk, encoding="utf-8"))
            if st.get("v") != SUMMARY_VERSION:
                st = None
        except Exception:
            st = None
    if st is None or size < st["offset"]:
        st = _empty_state()
    if size > st["offset"]:
        with open(ev_path, "rb") as f:
            f.seek(st["offset"])
            chunk = f.read(size - st["offset"])
        end = chunk.rfind(b"\n") + 1  # only whole lines; a half-written last line waits for the next look
        for line in chunk[:end].splitlines():
            if line.strip():
                try:
                    _fold(st, json.loads(line))
                except Exception:
                    pass
        if end:
            st["offset"] += end
            try:
                _write_json(disk, st)
            except OSError:
                pass
    _mem[key] = st
    return st


def _status(root, bid, st, rec):
    out = os.path.join(root, bid, "out")
    fresh = max(_mtime(os.path.join(out, ".alive")) or 0, _mtime(os.path.join(out, "events.jsonl")) or 0)
    if time.time() - fresh < ALIVE_S and not (rec and rec.get("response")):
        return "running"
    if st["capped"]:
        return "capped"
    if (rec and rec.get("response")) or st["packaged"]:
        return "done"
    # cut off mid-work right at a platform request timeout (30 min before it was raised, then 2 h): capped, not crashed
    if 1770 <= st["last_t"] <= 1830 or st["last_t"] >= 7080:
        return "capped"
    return "stopped"


def summary(root, bid, kind="scene"):
    rec = _index(root).get(bid)
    st = _state(root, bid)
    work = os.path.join(root, bid)
    resp = (rec or {}).get("response") or None
    started = _started(work, rec)
    updated = _mtime(os.path.join(work, "out", "events.jsonl")) or started
    status = _status(root, bid, st, rec)
    given = st["given"] or {}
    last = st["last_critique"]
    if resp:
        crit = resp.get("critiques") or []
        if crit:
            c = crit[-1]
            last = {k: c.get(k) for k in ("round", "scores", "passed", "summary")}
    rounds = max(st["rounds"], len((resp or {}).get("critiques") or []))
    if kind == "kit":
        stills = [u for u in [(resp or {}).get("lineup_url")] if u]
        previews = [{"url": it["thumb_url"], "message": it.get("label") or it.get("id")}
                    for it in (resp or {}).get("items", []) if it.get("thumb_url")] or st["previews"]
        cover = (resp or {}).get("lineup_url") or st["cover"]
    else:
        stills = list((resp or {}).get("stills") or [])
        previews = [{"url": p["url"], "message": p.get("name", "")} for p in (resp or {}).get("previews") or []] \
            or st["previews"]
        cover = (stills[0] if stills else None) or st["cover"]
    minutes = (resp or {}).get("minutes")
    if minutes is None:
        minutes = round(((time.time() if status == "running" else updated) - started) / 60, 1)
    return {
        "id": bid, "kind": kind, "started": started, "updated": updated, "status": status,
        "passed": bool(last and last.get("passed")),
        "reference_urls": given.get("reference_urls") or [], "prompt": given.get("prompt") or "",
        "dials": given.get("dials") or {}, "effort": given.get("effort"), "floor_plan_url": given.get("floor_plan_url"),
        "items": given.get("items"), "name": (resp or {}).get("name"),
        "critic_rounds": rounds, "last_scores": (last or {}).get("scores"), "last_summary": (last or {}).get("summary"),
        "minutes": minutes, "cover": cover,
        "stills": stills[:MAX_LIST_IMAGES], "previews": previews[:MAX_LIST_IMAGES],
        "n_stills": len(stills), "n_previews": len(previews), "frames": st["frames"],
        "milestone": st["last_milestone"], "attempts": st["attempts"], "has_response": bool(resp),
        "agent_cost_usd": (resp or {}).get("agent_cost_usd"), "error": st["failed"],
        "package_url": (resp or {}).get("package_url") or st["package_url"],
    }


def list_builds(root, kind="scene", limit=200, max_age_s=8.0):
    """Light records of every build under root, newest first. The whole listing is reused for a few seconds."""
    hit = _list_cache.get((root, limit))
    if hit and time.time() - hit[0] < max_age_s:
        return hit[1]
    prefix = "k_" if kind == "kit" else "b_"
    with _lock:
        try:
            ids = [e.name for e in os.scandir(root) if e.is_dir() and e.name.startswith(prefix) and ID_RE.match(e.name)]
        except OSError:
            ids = []
        rows = []
        for bid in ids:
            try:
                rows.append(summary(root, bid, kind))
            except Exception as e:  # one broken workspace must not hide the rest
                rows.append({"id": bid, "kind": kind, "status": "stopped", "started": _mtime(os.path.join(root, bid)) or 0,
                             "error": f"{type(e).__name__}: {e}"[:200]})
    rows.sort(key=lambda r: r.get("started") or 0, reverse=True)
    res = {"builds": rows[:limit], "total": len(rows), "now": time.time()}
    _list_cache[(root, limit)] = (time.time(), res)
    return res


# ---------------------------------------------------------------- one build: events + response
def _files_response(root, bid, kind, uploader):
    """A response for a finished build with no index record, built from the files in out/. Each file is uploaded once;
    the URLs are cached next to the summary (keyed by size + mtime, so a re-render is uploaded again)."""
    out = os.path.join(root, bid, "out")
    cache_path = os.path.join(_gallery_dir(root), bid + ".files.json")
    try:
        cache = json.load(open(cache_path, encoding="utf-8"))
    except Exception:
        cache = {}
    changed = False

    def lst(sub, exts):
        d = os.path.join(out, sub)
        return [os.path.join(sub, f) for f in sorted(os.listdir(d)) if f.lower().endswith(exts)] if os.path.isdir(d) else []

    if kind == "kit":
        wanted = lst("renders", (".png", ".jpg")) + lst("renders/thumbs", (".png", ".jpg"))
        wanted = [w for w in wanted if w.replace("\\", "/") in ("renders/lineup.png",) or "/thumbs/" in w.replace("\\", "/")]
    else:
        wanted = lst("stills", (".png", ".jpg")) + lst("previews", (".png", ".jpg")) + lst("panoramas", (".png", ".jpg", ".exr", ".hdr"))
        if os.path.exists(os.path.join(out, "scene.glb")):
            wanted.append("scene.glb")
    todo = []
    for rel in wanted:
        p = os.path.join(out, rel)
        try:
            s = os.stat(p)
        except OSError:
            continue
        k = rel.replace("\\", "/")
        sig = [s.st_size, s.st_mtime]
        if not (cache.get(k) and cache[k].get("sig") == sig and cache[k].get("url")):
            todo.append((k, p, sig))
    if todo and uploader:
        from concurrent.futures import ThreadPoolExecutor

        def up(item):
            k, p, sig = item
            try:
                return k, {"sig": sig, "url": uploader(p)}
            except Exception as e:
                return k, {"sig": sig, "url": None, "error": str(e)[:200]}
        with ThreadPoolExecutor(8) as ex:
            for k, v in ex.map(up, todo):
                cache[k] = v
                changed = True
    if changed:
        try:
            _write_json(cache_path, cache)
        except OSError:
            pass
    url = lambda k: (cache.get(k) or {}).get("url")
    pick = lambda sub: [(k, url(k)) for k in sorted(cache) if k.startswith(sub + "/") and url(k)
                        and os.path.exists(os.path.join(out, k))]
    if kind == "kit":
        kit = {}
        try:
            kit = json.load(open(os.path.join(out, "kit.json"), encoding="utf-8"))
        except Exception:
            pass
        labels = {it.get("id"): it.get("label") for it in kit.get("items", []) if isinstance(it, dict)}
        items = [{"id": os.path.splitext(os.path.basename(k))[0], "label": labels.get(os.path.splitext(os.path.basename(k))[0], ""),
                  "thumb_url": u} for k, u in pick("renders/thumbs")]
        return {"kit_id": bid, "name": kit.get("name"), "style": kit.get("style"), "items": items,
                "lineup_url": url("renders/lineup.png"), "kit_blend_url": None, "kit_json_url": None, "package_url": None,
                "critiques": [], "dials": {}, "missing": [], "agent_cost_usd": None, "generation_cost_usd": None,
                "minutes": None, "from_storage": True}
    dials = {}
    try:
        dials = json.load(open(os.path.join(out, "dials.json"), encoding="utf-8"))
    except Exception:
        pass
    return {"build_id": bid, "stills": [u for _, u in pick("stills")],
            "previews": [{"name": os.path.splitext(os.path.basename(k))[0], "url": u} for k, u in pick("previews")],
            "panorama_urls": [u for _, u in pick("panoramas")], "scene_glb_url": url("scene.glb"),
            "scene_blend_url": None, "package_url": None, "critiques": [], "dials": dials, "missing": [],
            "agent_cost_usd": None, "minutes": None, "from_storage": True}


def build_events(root, bid, after=0, kind="scene", heartbeats=False, max_bytes=8 << 20, uploader=None):
    """Events of one build from byte offset `after` (whole lines only), plus its summary. Heartbeats are dropped except
    the newest one in the page (they repeat the milestones, which the full stream already has). The last page of a
    build that is not running also carries `response`: the index's, else one built from out/."""
    if not ID_RE.match(bid or ""):
        raise ValueError("bad id")
    ev_path = os.path.join(root, bid, "out", "events.jsonl")
    if not os.path.isdir(os.path.join(root, bid)):
        raise ValueError(f"no such build: {bid}")
    after = max(0, int(after or 0))
    events, nxt, size = [], after, 0
    try:
        size = os.path.getsize(ev_path)
    except OSError:
        pass
    if size > after:
        with open(ev_path, "rb") as f:
            f.seek(after)
            chunk = f.read(min(size - after, max_bytes))
        end = chunk.rfind(b"\n") + 1
        last_beat = None
        for line in chunk[:end].splitlines():
            if not line.strip():
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if ev.get("stage") == "heartbeat" and not heartbeats:
                last_beat = ev
                continue
            events.append(ev)
        if last_beat is not None:
            events.append(last_beat)
        nxt = after + end
    s = summary(root, bid, kind)
    # more: this page stopped at max_bytes, so ask again right away (otherwise poll at leisure)
    res = {"id": bid, "events": events, "next": nxt, "size": size, "more": size - after > max_bytes, "summary": s}
    if s["status"] != "running" and not res["more"]:
        rec = _index(root).get(bid) or {}
        res["response"] = rec.get("response") or _files_response(root, bid, kind, uploader)
        if not res["response"].get("package_url") and s.get("package_url"):  # the "packaged" event had it
            res["response"]["package_url"] = s["package_url"]
    return res
