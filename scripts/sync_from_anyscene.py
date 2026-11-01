"""Copy the code AnyKit shares with AnyScene from a sibling AnyScene checkout (github.com/apekshik/anyscene).

    python scripts/sync_from_anyscene.py [path/to/anyscene]      (default: ../anyscene next to this repo)

AnyScene is the home of these files; edit them there, then run this. Each copy gets a one-line header naming its
source. A few of them live in AnyShot (github.com/apekshik/anyshot) and reach AnyScene through its own sync; their
header here names both.
"""

import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# anykit path: anyscene path
FILES = {
    "agent/blender_gpu.py": "builder/blender_gpu.py",
    "agent/gallery.py": "builder/gallery.py",
    "generate/fal.mjs": "generate/fal.mjs",
    "pipeline/env.sh": "pipeline/env.sh",
    "pipeline/gpu_run.sh": "pipeline/gpu_run.sh",
}
UPSTREAM = re.compile(r"^(?:#|//|<!--)\s*synced from (github\.com/\S+ \S+?)(?:\s*-->)?\s*$")


def header(src_rel, upstream=None):
    text = f"synced from github.com/apekshik/anyscene {src_rel}" + (f" (home: {upstream})" if upstream else "")
    if src_rel.endswith(".md"):
        return f"<!-- {text} -->"
    return f"// {text}" if src_rel.endswith((".js", ".mjs", ".ts")) else f"# {text}"


def main():
    src_root = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.join(REPO, "..", "anyscene"))
    if not os.path.exists(os.path.join(src_root, "builder", "scene_builder.py")):
        sys.exit(f"not an AnyScene checkout: {src_root}")
    for dst_rel, src_rel in FILES.items():
        lines = open(os.path.join(src_root, src_rel), encoding="utf-8").read().splitlines(keepends=True)
        at = 1 if lines and lines[0].startswith("#!") else 0  # keep a shebang first
        upstream = None
        if len(lines) > at and (m := UPSTREAM.match(lines[at])):  # AnyScene's own copy of an AnyShot file
            upstream = m.group(1)
            del lines[at]
        lines.insert(at, header(src_rel, upstream) + "\n")
        dst = os.path.join(REPO, dst_rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        old = open(dst, encoding="utf-8").read() if os.path.exists(dst) else None
        new = "".join(lines)
        if new != old:
            with open(dst, "w", encoding="utf-8", newline="\n") as f:
                f.write(new)
        print(f"{'updated' if new != old else 'same   '} {dst_rel}  <-  {src_rel}")


if __name__ == "__main__":
    main()
