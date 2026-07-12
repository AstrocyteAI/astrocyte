#!/usr/bin/env python3
"""Docs-coverage gate: docs must mention every public API surface element.

Two checks, same philosophy as the snapshot/oasdiff/griffe gates — drift
between implementation and documentation is a CI failure, not a hope:

1. Every path in the gateway's checked-in ``openapi.json`` must be mentioned
   somewhere in the docs authoring tree (path parameters compared by position,
   so ``{model_id}`` in code matches ``{id}`` in prose).
2. Every name in ``astrocyte.__all__`` must be mentioned somewhere in the docs
   tree (the generated ``python-api-index.md`` page guarantees a floor; run
   ``docs/scripts/generate-api-index.py`` after changing the public surface).

Run from the repo root inside the astrocyte-py environment:

    (cd astrocyte-py && uv run python ../tooling/check_docs_coverage.py)
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
# NOTE: docs/_reference-archive is deliberately EXCLUDED — archived snapshots
# mention every historical endpoint/name forever, which would mask missing
# documentation for the CURRENT surface.
DOCS_DIRS = ("docs/_design", "docs/_end-user", "docs/_plugins", "docs/_tutorials")
OPENAPI = REPO / "astrocyte-services-py/astrocyte-gateway-py/openapi.json"


def docs_corpus() -> str:
    chunks = []
    for d in DOCS_DIRS:
        for f in (REPO / d).rglob("*.md*"):
            chunks.append(f.read_text(errors="ignore"))
    # Prose escapes MDX-hostile braces as \{...\} (CommonMark backslash
    # escapes); normalize them away so path templates still match.
    return "\n".join(chunks).replace("\\{", "{").replace("\\}", "}")


def norm(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{}", path)


def check_endpoints(corpus: str) -> list[str]:
    spec = json.loads(OPENAPI.read_text())
    documented = {norm(m) for m in re.findall(r"/(?:v1/|health|live|openapi)[a-z0-9/{}_.-]*", corpus)}
    return sorted(p for p in spec["paths"] if norm(p) not in documented)


def check_library(corpus: str) -> list[str]:
    import astrocyte

    return sorted(
        n for n in set(astrocyte.__all__) if not re.search(rf"\b{re.escape(n)}\b", corpus)
    )


def main() -> int:
    corpus = docs_corpus()
    failures = 0

    missing_paths = check_endpoints(corpus)
    if missing_paths:
        failures += 1
        print(f"✗ {len(missing_paths)} gateway path(s) not mentioned anywhere in docs/:")
        for p in missing_paths:
            print(f"    {p}")
        print("  → document them (e.g. docs/_end-user/memory-api-reference.md)")
    else:
        print("✓ all gateway OpenAPI paths are mentioned in docs/")

    missing_names = check_library(corpus)
    if missing_names:
        failures += 1
        print(f"✗ {len(missing_names)} public astrocyte name(s) not mentioned anywhere in docs/:")
        for n in missing_names:
            print(f"    {n}")
        print("  → regenerate the index: (cd astrocyte-py && uv run python ../docs/scripts/generate-api-index.py)")
    else:
        print("✓ all astrocyte.__all__ names are mentioned in docs/")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
