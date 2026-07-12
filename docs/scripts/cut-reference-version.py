#!/usr/bin/env python3
"""Cut a versioned snapshot of the API-reference docs for a release.

Usage (from the repo root, as part of the release checklist — see RELEASING.md):

    make docs-version VERSION=0.16.0

What it does (reference pages only — tutorials/design stay latest-only):

1. Snapshots the API-contract pages into ``docs/_reference-archive/v{V}/``:
   - ``memory-api-reference.md``  (gateway + Python usage reference)
   - ``python-api-index.md``      (the generated ``astrocyte.__all__`` index)
   Each gets an archive banner; internal links are rewritten to ``.md``-file
   relative links that ``sync-docs.mjs`` resolves (setup guides are not
   versioned, so archived pages link back to the latest guides).
2. Prepends the version to ``docs/versions.json`` (the archive registry).
3. Regenerates ``docs/_end-user/reference-archive.md`` (the index page).
   The OpenAPI spec is linked as the tag-pinned GitHub blob of the gateway's
   checked-in ``openapi.json`` — immutable and exact by construction.

Run this ON the release commit (before tagging), so the archived reference is
byte-derived from the same tree that builds the PyPI wheels, the GHCR image,
and the live ``openapi.json``.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

DOCS = pathlib.Path(__file__).resolve().parent.parent
PAGES = ("memory-api-reference.md", "python-api-index.md")
REPO_URL = "https://github.com/AstrocyteAI/astrocyte"
OPENAPI_REPO_PATH = "astrocyte-services-py/astrocyte-gateway-py/openapi.json"


def openapi_blob_url(slug: str) -> str:
    return f"{REPO_URL}/blob/{slug}/{OPENAPI_REPO_PATH}"


def banner(version: str, slug: str) -> str:
    return (
        f"> **Archived reference — {slug}.** This snapshot documents the API as\n"
        f"> released in `{version}` (same tag as the PyPI packages, the gateway\n"
        f"> image, and the [OpenAPI spec]({openapi_blob_url(slug)})).\n"
        f"> The latest reference lives [here](../../_end-user/memory-api-reference.md);\n"
        f"> all archived versions are listed in the\n"
        f"> [reference archive](../../_end-user/reference-archive.md).\n"
    )


def rewrite_links(md: str) -> str:
    """Rewrite same-section route links to .md-file links sync-docs can resolve.

    In the live page, ``](authentication-setup/)`` resolves as a sibling route.
    Archived copies live under ``/reference-archive/v{V}/``, so those relative
    routes would break; pointing at ``../../_end-user/<name>.md`` lets
    ``sync-docs.mjs`` emit a correct relative route to the (unversioned) latest
    page from wherever the archive page is published.
    """
    def repl(m: re.Match) -> str:
        name, anchor = m.group(1), m.group(2) or ""
        return f"](../../_end-user/{name}.md{anchor})"

    return re.sub(r"\]\(([a-z0-9-]+)/(#[^)]+)?\)", repl, md)


def snapshot_pages(version: str) -> None:
    slug = f"v{version}"
    dest = DOCS / "_reference-archive" / slug
    dest.mkdir(parents=True, exist_ok=True)
    for name in PAGES:
        src = DOCS / "_end-user" / name
        text = src.read_text(encoding="utf-8")
        lines = text.split("\n")
        # Insert the banner after the H1 so sync-docs still derives the title.
        h1 = next(i for i, line in enumerate(lines) if line.startswith("# "))
        lines[h1] = f"{lines[h1]} ({slug})"
        lines.insert(h1 + 1, "\n" + banner(version, slug).rstrip())
        out = rewrite_links("\n".join(lines))
        (dest / name).write_text(out, encoding="utf-8")
        print(f"snapshotted {name} -> _reference-archive/{slug}/")


def update_registry(version: str) -> list[str]:
    reg_path = DOCS / "versions.json"
    reg = json.loads(reg_path.read_text())
    slug = f"v{version}"
    if slug in [v["slug"] for v in reg["versions"]]:
        print(f"version {slug} already registered — refreshing snapshot only")
    else:
        reg["versions"].insert(0, {"slug": slug, "label": slug})
    reg_path.write_text(json.dumps(reg, indent=2) + "\n", encoding="utf-8")
    return [v["slug"] for v in reg["versions"]]


def regenerate_index(slugs: list[str]) -> None:
    lines = [
        "# API reference archive",
        "",
        "Versioned snapshots of the API reference, cut at each release from the",
        "same tag that built the PyPI packages and the gateway image — so the",
        "archived reference always matches the installed version. The",
        "[Memory API reference](memory-api-reference.md) and",
        "[Python public API index](python-api-index.md) always describe `main`.",
        "",
    ]
    if not slugs:
        lines += [
            "_No archived versions yet — the first snapshot is cut at the next release._",
            "",
        ]
    else:
        lines += [
            "| Version | Memory API reference | Python API index | OpenAPI spec |",
            "|---|---|---|---|",
        ]
        for s in slugs:
            lines.append(
                f"| **{s}** "
                f"| [reference](../_reference-archive/{s}/memory-api-reference.md) "
                f"| [index](../_reference-archive/{s}/python-api-index.md) "
                f"| [openapi.json]({openapi_blob_url(s)}) |"
            )
        lines.append("")
    (DOCS / "_end-user" / "reference-archive.md").write_text("\n".join(lines), encoding="utf-8")
    print("regenerated _end-user/reference-archive.md")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="Release version, e.g. 0.16.0 (no leading v)")
    args = parser.parse_args()
    version = args.version.lstrip("v")
    if not re.fullmatch(r"\d+\.\d+\.\d+([.-].+)?", version):
        print(f"invalid version: {version!r}", file=sys.stderr)
        return 1
    snapshot_pages(version)
    slugs = update_registry(version)
    regenerate_index(slugs)
    print("\nDone. Review + commit:")
    print(f"  git add docs/_reference-archive/v{version} docs/versions.json docs/_end-user/reference-archive.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
