"""
DETECTEDLANGUAGE REMOVAL AUDIT (docs/PENDING_WORK.md A2, Phase 6).

Read-only. Lists every place the legacy `detectedLanguage` /
`detected_language` is referenced, grouped by layer, so the eventual
removal is a checklist and not an archaeology exercise -- and so the
scope in the docs cannot silently go stale again (the first version of
the plan missed the trace pipeline and the verification service).

Does NOT remove anything and does NOT decide when removal is safe: that
is language_tag_readiness.py's job (READY on real traffic).

    python -m app.language.detected_language_audit          # inventory
    python -m app.language.detected_language_audit --check-doc
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
PATTERN = re.compile(r"detectedLanguage|detected_language")
SKIP_DIRS = {"node_modules", ".git", "__pycache__", "dist", "build"}
# The tools that talk ABOUT the field are not consumers of it.
SELF_FILES = {"detected_language_audit.py", "language_tag_readiness.py"}
EXTS = {".py", ".ts", ".tsx", ".js", ".sql", ".md", ".json"}

# Comment-only references (deprecation notes, history) are not code to
# change; they are listed separately so they are not mistaken for it.
_COMMENT = re.compile(r"^\s*(#|--|//|\*|/\*|\"\"\")")


def _layer(rel: str) -> str:
    if rel.startswith("db/"):
        return "db-migration"
    if "/tests/" in rel or rel.startswith("tests/"):
        return "test"
    if rel.endswith(".md"):
        return "doc"
    if rel.startswith("node-api/"):
        return "node-api"
    if rel.startswith("python-api/app/"):
        return "python-app"
    return "other"


def scan(repo: Path = REPO) -> list[dict]:
    hits: list[dict] = []
    if not repo.exists():
        return hits
    for p in sorted(repo.rglob("*")):
        if not p.is_file() or p.suffix not in EXTS or SKIP_DIRS & set(p.parts):
            continue
        rel = p.relative_to(repo).as_posix()
        if p.name in SELF_FILES:
            continue
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(lines, 1):
            if PATTERN.search(line):
                hits.append({"file": rel, "line": n, "layer": _layer(rel),
                             "comment_only": bool(_COMMENT.match(line)) or _layer(rel) == "doc",
                             "text": line.strip()[:110]})
    return hits


def summarize(hits: list[dict]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for h in hits:
        if h["comment_only"]:
            continue
        out.setdefault(h["layer"], {}).setdefault(h["file"], 0)
        out[h["layer"]][h["file"]] += 1
    return out


def render(hits: list[dict]) -> str:
    if not hits:
        return "no references found (or repo root not mounted: " + str(REPO) + ")"
    lines = ["CODE references (must change at removal):"]
    for layer, files in sorted(summarize(hits).items()):
        lines.append(f"  [{layer}]")
        for f, n in sorted(files.items()):
            lines.append(f"    {f}  x{n}")
    nodes = [h for h in hits if h["layer"] == "node-api"]
    lines.append(f"\nnode-api readers: {len(nodes)}" + ("  <-- CHECK BEFORE REMOVING" if nodes else " (none found)"))
    lines.append(f"comment/doc-only references: {sum(1 for h in hits if h['comment_only'])} (update wording, no behaviour)")
    return "\n".join(lines)


def _main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check-doc", action="store_true",
                    help="fail if a code file with references is missing from docs/PENDING_WORK.md A2")
    a = ap.parse_args()
    hits = scan()
    print(render(hits))
    if a.check_doc:
        doc = (REPO / "docs" / "PENDING_WORK.md").read_text(encoding="utf-8")
        missing = [f for layer, fs in summarize(hits).items() if layer in ("python-app", "db-migration", "node-api")
                   for f in fs if Path(f).name not in doc]
        if missing:
            print("\nNOT MENTIONED in PENDING_WORK.md A2:", missing)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
