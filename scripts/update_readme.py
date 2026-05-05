"""Auto-regenerate sections of README.md from the actual project tree.

The README contains marker-pairs:

    <!-- AUTO-MODULE-MAP-START -->
    ...generated content...
    <!-- AUTO-MODULE-MAP-END -->

This script re-walks the project, builds a fresh tree-style module map (with
each file annotated with its module docstring's first line), and replaces the
content between the markers. Other parts of the README are left untouched.

Run manually: `python -m cli regen-readme`
Or wire into git pre-commit (see `scripts/install_hooks.sh`).
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Iterable, List, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
README = PROJECT_ROOT / "README.md"

START_MARKER = "<!-- AUTO-MODULE-MAP-START -->"
END_MARKER = "<!-- AUTO-MODULE-MAP-END -->"

# Files included at the project root (manually curated for readability).
ROOT_FILES = [
    "README.md",
    "requirements.txt",
    ".env.example",
    "config.yaml",
    "cli.py",
    "app.py",
]

# Subtrees we walk automatically — each entry is a path under PROJECT_ROOT.
WALK_DIRS = ["bot", "scripts"]

# Files / directories ignored anywhere they appear.
IGNORE_NAMES = {"__pycache__", "__init__.py", ".pytest_cache", ".venv", "venv"}


def _docstring_summary(path: Path, fallback: str = "") -> str:
    """Extract the first line of a Python module's docstring."""
    if path.suffix != ".py":
        return fallback
    try:
        tree = ast.parse(path.read_text())
        ds = ast.get_docstring(tree) or ""
        first = ds.strip().split("\n", 1)[0]
        return first or fallback
    except Exception:
        return fallback


def _root_summary(name: str) -> str:
    return {
        "README.md": "Full usage + warnings + going-live checklist",
        "requirements.txt": "Pinned deps",
        ".env.example": "Copy to .env (broker keys, OpenAI, Redis, SMTP, TOTP)",
        "config.yaml": "Capital, risk limits, watchlist, strategy params",
        "cli.py": "Typer CLI: run / login / backtest / research / dashboard …",
        "app.py": "Streamlit dashboard (P&L, positions, charts, signals)",
    }.get(name, "")


def _entries(directory: Path, prefix: str = "") -> List[Tuple[Path, bool]]:
    """Sorted entries (dirs first, then files), excluding ignored names."""
    items: List[Path] = []
    for p in sorted(directory.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
        if p.name in IGNORE_NAMES or p.name.startswith("."):
            continue
        items.append(p)
    return [(p, p.is_dir()) for p in items]


def _walk(directory: Path, prefix: str = "") -> List[str]:
    """Return list of tree lines for `directory` (recursively)."""
    lines: List[str] = []
    entries = _entries(directory)
    for i, (p, is_dir) in enumerate(entries):
        last = i == len(entries) - 1
        connector = "└── " if last else "├── "
        if is_dir:
            lines.append(f"{prefix}{connector}{p.name}/")
            child_prefix = prefix + ("    " if last else "│   ")
            lines.extend(_walk(p, child_prefix))
        else:
            summary = _docstring_summary(p)
            label = f"{p.name}"
            line = f"{prefix}{connector}{label}"
            if summary:
                line = f"{line:<40} {summary}"
            lines.append(line)
    return lines


def _format_root_block() -> List[str]:
    lines = [f"{name:<40} {_root_summary(name)}" for name in ROOT_FILES]
    return lines


def build_module_map() -> str:
    out: List[str] = ["```text", f"Stock-Market-Bot/"]
    for i, name in enumerate(ROOT_FILES):
        connector = "├── "
        line = f"{connector}{name}"
        summary = _root_summary(name)
        if summary:
            line = f"{line:<40} {summary}"
        out.append(line)

    for j, sub in enumerate(WALK_DIRS):
        path = PROJECT_ROOT / sub
        if not path.exists():
            continue
        last = j == len(WALK_DIRS) - 1
        connector = "└── " if last else "├── "
        out.append(f"{connector}{sub}/")
        prefix = "    " if last else "│   "
        out.extend(_walk(path, prefix))

    out.append("```")
    return "\n".join(out)


def regenerate(readme_path: Path = README) -> bool:
    if not readme_path.exists():
        raise FileNotFoundError(readme_path)
    text = readme_path.read_text()

    if START_MARKER not in text or END_MARKER not in text:
        # First-time install — locate "## Module map" and inject markers around the next code fence.
        m = re.search(r"(## Module map\s*\n)(```text\n[\s\S]*?\n```)", text)
        if not m:
            raise RuntimeError(
                f"README has no {START_MARKER} markers and no '## Module map' code fence to bootstrap from."
            )
        text = (
            text[: m.start(2)]
            + START_MARKER + "\n"
            + m.group(2) + "\n"
            + END_MARKER
            + text[m.end(2):]
        )

    new_block = build_module_map()
    pattern = re.compile(
        re.escape(START_MARKER) + r"[\s\S]*?" + re.escape(END_MARKER),
        re.MULTILINE,
    )
    new_text = pattern.sub(f"{START_MARKER}\n{new_block}\n{END_MARKER}", text)

    if new_text == text:
        return False
    readme_path.write_text(new_text)
    return True


if __name__ == "__main__":
    changed = regenerate()
    print(f"README {'updated' if changed else 'already up-to-date'}.")
