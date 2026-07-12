#!/usr/bin/env python3
"""Regenerate docs/_end-user/python-api-index.md from astrocyte.__all__.

Run from the repo root (needs the astrocyte-py venv):

    (cd astrocyte-py && uv run python ../docs/scripts/generate-api-index.py)

The docs-coverage CI gate fails when a public name is absent from the docs
tree; regenerating this page after adding a public export satisfies it.
"""

import inspect
import pathlib

import astrocyte

OUT = pathlib.Path(__file__).resolve().parent.parent / "_end-user" / "python-api-index.md"

HEADER = """# Python public API index

Every name exported from `astrocyte` (`astrocyte.__all__`) — the import surface
covered by the stability policy. Import from the package root, not submodules:

```python
from astrocyte import Astrocyte, RecallRequest, AccessDenied
```

This page is **generated** by `docs/scripts/generate-api-index.py`; regenerate
after changing the public surface. CI fails if a public name is missing from
the docs, and the surface itself is pinned by
`astrocyte-py/tests/test_public_api_surface.py` plus a griffe breaking-change
gate. For usage-oriented documentation see the
[Memory API reference](memory-api-reference/).
"""


def one_liner(obj) -> str:
    doc = inspect.getdoc(obj) or ""
    line = doc.split("\n")[0].strip().rstrip(".")
    # Escape MDX-hostile characters: braces parse as expressions and raw pipes
    # break the table. Backslash-escapes are valid CommonMark, so plain
    # Starlight rendering is unaffected.
    line = line.replace("{", "\\{").replace("}", "\\}")
    return line or "—"


def main() -> None:
    groups: dict[str, list[tuple[str, str]]] = {
        "Classes & types": [],
        "Functions": [],
        "Exceptions": [],
        "Constants & aliases": [],
    }
    for n in sorted(set(astrocyte.__all__), key=str.lower):
        obj = getattr(astrocyte, n)
        if inspect.isclass(obj):
            key = "Exceptions" if issubclass(obj, BaseException) else "Classes & types"
        elif callable(obj):
            key = "Functions"
        else:
            key = "Constants & aliases"
        groups[key].append((n, one_liner(obj)))

    parts = [HEADER]
    for title, rows in groups.items():
        if not rows:
            continue
        parts.append(f"## {title} ({len(rows)})\n")
        parts.append("| Name | Summary |")
        parts.append("|---|---|")
        for n, d in rows:
            parts.append(f"| `{n}` | {d.replace('|', chr(92) + '|')} |")
        parts.append("")
    OUT.write_text("\n".join(parts) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
