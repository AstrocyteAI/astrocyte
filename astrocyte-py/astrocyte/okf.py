"""OKF v0.2 bundle export.

Projects compiled wiki pages into an Open Knowledge Format bundle: a directory
of markdown files with YAML frontmatter, plus ``index.md`` / ``log.md``.

Spec: ``GoogleCloudPlatform/knowledge-catalog/okf/SPEC.md`` (v0.2).

This is a **projection**, not a storage format. Postgres remains the system of
record; nothing here writes back. It sits beside :mod:`astrocyte.portability`
(AMA), which exports raw memories from ``recall()`` results and structurally
cannot see wiki pages.

Fields are emitted only where real data exists. ``type`` is the sole required
frontmatter key (SPEC 4.1, 11), so a sparse bundle is fully conformant, and
consumers MUST tolerate absent optional families. Deliberate omissions:

``verified``
    Astrocyte has no review workflow, so no record written today carries
    verification events and consumers correctly derive the **unverified** tier
    (SPEC 5.3). :func:`verified_for` reads ``metadata["_verified"]`` so the
    export is complete the moment such a workflow exists — but nothing is
    invented in the meantime.
``status``
    No lifecycle column exists on any record. Absent ``status`` already means
    ``stable`` (SPEC 5.4), which is accurate for a compiled page.
``stale_after``
    Omitted for wiki pages, which no TTL governs. **Emitted for memories** when
    lifecycle is enabled, derived from ``delete_after_days`` — see
    :func:`stale_after_for`.
``description``
    ``WikiPage`` has no summary field; ``wiki_revisions.summary`` receives the
    title. Emitting it would duplicate ``title`` rather than describe the page.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from .portability import _safe_resolve

if TYPE_CHECKING:  # pragma: no cover
    from .types import VectorItem, WikiPage

OKF_VERSION = "0.2"

# SPEC 3.1: reserved at every level of the hierarchy.
_RESERVED_STEMS = frozenset({"index", "log"})

# WikiPage.kind -> OKF `type`. Types are not centrally registered (SPEC 4.1);
# these are descriptive and self-explanatory, which is all the spec asks.
_KIND_TO_TYPE = {"topic": "Topic", "entity": "Entity", "concept": "Concept"}


@dataclass
class BundleFile:
    """One rendered file, relative to the bundle root."""

    path: str
    content: str


@dataclass
class ExportResult:
    bank_id: str
    concept_count: int
    files: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def _slug_segment(raw: str) -> str:
    """Lowercase, URL-safe path segment. Never empty, never reserved."""
    seg = re.sub(r"[^a-z0-9._-]+", "-", str(raw).strip().lower()).strip("-.")
    if not seg or set(seg) <= {".", "-"}:
        seg = "concept"
    if seg in _RESERVED_STEMS:
        seg = f"{seg}-concept"
    return seg


def path_for_page_id(page_id: str, *, fallback_kind: str = "concept") -> str:
    """Bundle-relative path for a ``page_id``, ``.md`` included.

    ``page_id`` is already namespaced (``topic:incident-response``,
    ``entity:alice``, ``obs:{doc}:{slug}``). Colons become directory
    separators, which turns the existing namespace into OKF's hierarchy for
    free — the concept ID is then the path minus ``.md`` (SPEC 2).
    """
    parts = [p for p in str(page_id or "").split(":") if p.strip()]
    if not parts:
        parts = [str(fallback_kind or "concept"), "untitled"]
    return "/".join(_slug_segment(p) for p in parts) + ".md"


def concept_path(page: WikiPage) -> str:
    """Bundle-relative path for a page, ``.md`` included."""
    return path_for_page_id(page.page_id, fallback_kind=str(page.kind or "concept"))


def render_related(cross_links: list[str] | None) -> str:
    """A ``## Related`` section carrying ``cross_links`` as concept links.

    OKF expresses the concept graph as markdown links in the body, not as a
    frontmatter field (SPEC 6.1); the bundle-relative (leading ``/``) form is
    recommended because it survives a document moving within its directory.
    Targets are not checked: a link to a page that has not been compiled yet
    is well-formed, and consumers MUST tolerate it.
    """
    targets = [str(c).strip() for c in (cross_links or []) if str(c).strip()]
    if not targets:
        return ""
    lines = ["## Related", ""]
    lines += [f"* [{t}](/{path_for_page_id(t)})" for t in dict.fromkeys(targets)]
    return "\n".join(lines) + "\n"


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _memory_resource(bank_id: str, memory_id: str) -> str:
    """A `resource` for an internal memory row.

    ``resource`` is REQUIRED within a sources entry (SPEC 5.1) but may name
    something the consumer cannot follow. These IDs are meaningless outside
    the owning bank, so the URI carries the bank to stay unambiguous.
    """
    return f"astrocyte://bank/{bank_id}/memory/{memory_id}"


def build_frontmatter(page: WikiPage, *, producer: str) -> dict[str, Any]:
    """Frontmatter for one concept. Only fields with real data are included."""
    fm: dict[str, Any] = {"type": _KIND_TO_TYPE.get(str(page.kind), "Concept")}

    title = (page.title or "").strip()
    if title:
        fm["title"] = title

    tags = [t for t in (page.tags or []) if str(t).strip()]
    if tags:
        fm["tags"] = tags

    sources = [
        {"id": str(sid), "resource": _memory_resource(page.bank_id, str(sid))}
        for sid in (page.source_ids or [])
        if str(sid).strip()
    ]
    if sources:
        fm["sources"] = sources

    # `by` is REQUIRED within `generated` (SPEC 5.2), so the block is all or
    # nothing. Astrocyte's pipeline genuinely is the producer, and
    # `<producer>/<version>` is the actor form for tools (SPEC 7) — this is
    # not a stand-in for the human/model actor we do not persist.
    at = _iso(getattr(page, "revised_at", None))
    fm["generated"] = {"by": producer, "at": at} if at else {"by": producer}

    if page.scope and str(page.scope).strip():
        fm["scope"] = str(page.scope).strip()
    if page.revision:
        fm["revision"] = int(page.revision)

    return fm


def _lifecycle_created_at(item: VectorItem) -> datetime | None:
    """The creation instant lifecycle actually uses.

    ``run_lifecycle`` reads ``metadata["_created_at"]`` rather than the typed
    ``VectorItem.retained_at``. A published ``stale_after`` is only honest if it
    is computed from the same value that will drive the deletion, so this
    deliberately mirrors that read — including its fallback to nothing when the
    key is absent, which is exactly when lifecycle cannot age the row either.
    """
    meta = item.metadata or {}
    raw = meta.get("_created_at")
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None
    return None


def _lifecycle_tags(item: VectorItem) -> list[str]:
    """Tags as lifecycle sees them (``metadata["_tags"]``, comma-joined)."""
    meta = item.metadata or {}
    raw = meta.get("_tags")
    if isinstance(raw, str) and raw.strip():
        return [t.strip() for t in raw.split(",") if t.strip()]
    return []


def stale_after_for(item: VectorItem, lifecycle: Any, *, now: datetime | None = None) -> str | None:
    """Absolute expiry instant for a memory, or ``None`` to omit the field.

    OKF's ``stale_after`` is an absolute instant, not a TTL (SPEC 5.5), which
    maps cleanly onto ``delete_after_days`` because that threshold is measured
    from creation and therefore does not move.

    Three rules keep the published value truthful:

    - Only when lifecycle is **enabled**; otherwise no expiry is in force.
    - From ``delete_after_days``, never ``archive_after_days`` — the latter is
      measured from ``last_recalled_at`` and so shifts on every read, which is
      not something that can be published as a fixed instant.
    - Omitted for ``exempt_tags`` memories, which genuinely never expire.
    """
    del now  # Absolute; independent of when the export runs.
    if lifecycle is None or not getattr(lifecycle, "enabled", False):
        return None
    ttl = getattr(lifecycle, "ttl", None)
    if ttl is None:
        return None

    exempt = getattr(ttl, "exempt_tags", None) or []
    if exempt and set(_lifecycle_tags(item)) & set(exempt):
        return None

    created = _lifecycle_created_at(item)
    if created is None:
        return None

    days = getattr(ttl, "delete_after_days", None)
    if not isinstance(days, int) or days <= 0:
        return None
    return (created + timedelta(days=days)).isoformat()


# Memory layers whose text the pipeline synthesises rather than the caller
# supplying it. `generated.by` records who produced the *content* (SPEC 5.2),
# so these must be attributed to the producer even when a human triggered the
# write — crediting a consolidated observation to `human:calvin` would claim a
# person wrote words the LLM actually wrote.
_DERIVED_LAYERS = frozenset({"observation", "model", "compiled"})

# ActorIdentity.type -> OKF actor prefix (SPEC 7). Consumers key trust off the
# `human:` prefix, so a real person must map to it and a machine must not.
_ACTOR_TYPE_TO_OKF = {"user": "human", "service": "process", "agent": "agent"}


def okf_actor(principal: str | None) -> str | None:
    """Map an Astrocyte principal (``{type}:{id}``) to an OKF actor (SPEC 7).

    ``user:`` becomes ``human:`` because that prefix is what consumers key trust
    tiers off. ``service:`` becomes ``process:``. Anything unparseable returns
    ``None`` so the caller falls back to the producer rather than emitting a
    malformed actor.
    """
    raw = (principal or "").strip()
    if not raw or ":" not in raw:
        return None
    kind, _, ident = raw.partition(":")
    ident = ident.strip()
    if not ident:
        return None
    return f"{_ACTOR_TYPE_TO_OKF.get(kind.strip(), kind.strip())}:{ident}"


def verified_for(item: VectorItem) -> list[dict[str, str]] | None:
    """Verification events for a memory, or ``None``.

    OKF's ``verified`` is a list of ``{by, at}`` events (SPEC 5.2), and the
    trust tier derives from it (SPEC 5.3). **Astrocyte has no review or
    verification workflow**, so this returns ``None`` for every record written
    today, and consumers correctly see the *unverified* tier.

    The reader exists so that the export is complete the moment such a workflow
    does: write ``metadata["_verified"]`` as a list of ``{"by": <principal>,
    "at": <ISO 8601>}`` and it flows through. Entries missing either field, or
    carrying an unmappable actor, are dropped rather than guessed at.
    """
    raw = (item.metadata or {}).get("_verified")
    if isinstance(raw, dict):  # SPEC 5.2 allows a bare mapping for one verifier.
        raw = [raw]
    if not isinstance(raw, list):
        return None
    out: list[dict[str, str]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        by = okf_actor(str(entry.get("by", "")))
        at = _iso(entry.get("at"))
        if by and at:
            out.append({"by": by, "at": at})
    return out or None


def memory_concept_path(item: VectorItem) -> str:
    """Bundle path for a memory concept.

    Memories are flat and id-keyed, so the hierarchy comes from the kind of
    memory rather than from the id, keeping ``memory/`` browsable.
    """
    group = str(item.fact_type or item.memory_layer or "memory").strip() or "memory"
    return f"memory/{_slug_segment(group)}/{_slug_segment(item.id)}.md"


def build_memory_frontmatter(
    item: VectorItem,
    *,
    producer: str,
    stale_after: str | None = None,
) -> dict[str, Any]:
    """Frontmatter for one memory concept."""
    kind = str(item.fact_type or item.memory_layer or "memory").strip() or "memory"
    fm: dict[str, Any] = {"type": kind.replace("_", " ").title()}

    tags = [t for t in (item.tags or []) if str(t).strip()]
    if tags:
        fm["tags"] = tags

    if item.chunk_id:
        fm["sources"] = [
            {
                "id": str(item.chunk_id),
                "resource": f"astrocyte://bank/{item.bank_id}/chunk/{item.chunk_id}",
            }
        ]

    # Attribute to the writing actor only where the caller supplied the text.
    # Derived layers are the pipeline's own words (see _DERIVED_LAYERS).
    actor = None
    if str(item.memory_layer or "").strip() not in _DERIVED_LAYERS:
        actor = okf_actor((item.metadata or {}).get("_actor"))
    by = actor or producer

    at = _iso(item.retained_at)
    fm["generated"] = {"by": by, "at": at} if at else {"by": by}

    verified = verified_for(item)
    if verified:
        fm["verified"] = verified

    if stale_after:
        fm["stale_after"] = stale_after

    # Valid time, distinct from `generated.at` (system time). OKF has no slot
    # for the second axis, and producers MAY add keys (SPEC 4.1), so the
    # bitemporality survives the projection instead of being flattened away.
    occurred = _iso(item.occurred_at)
    if occurred:
        fm["occurred_at"] = occurred
    if item.memory_layer:
        fm["memory_layer"] = str(item.memory_layer)

    return fm


def render_memory(
    item: VectorItem,
    *,
    producer: str,
    stale_after: str | None = None,
) -> str:
    fm = yaml.safe_dump(
        build_memory_frontmatter(item, producer=producer, stale_after=stale_after),
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    ).strip()
    return f"---\n{fm}\n---\n\n{(item.text or '').strip()}\n"


def render_concept(page: WikiPage, *, producer: str) -> str:
    fm = yaml.safe_dump(
        build_frontmatter(page, producer=producer),
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    ).strip()
    body = (page.content or "").strip()
    related = render_related(getattr(page, "cross_links", None))
    if related:
        body = f"{body}\n\n{related}" if body else related
    return f"---\n{fm}\n---\n\n{body}\n"


def render_index(entries: list[tuple[str, str]], *, heading: str, root: bool) -> str:
    """An `index.md` for one directory (SPEC 8).

    Index files carry no frontmatter, except a root index which MAY declare
    ``okf_version``.
    """
    lines: list[str] = []
    if root:
        lines += ["---", f"okf_version: {OKF_VERSION}", "---", ""]
    lines.append(f"# {heading}")
    lines.append("")
    for title, href in sorted(entries, key=lambda e: e[0].lower()):
        lines.append(f"* [{title}]({href})")
    lines.append("")
    return "\n".join(lines)


def render_log(pages: list[WikiPage]) -> str:
    """A root `log.md` grouped by date, newest first (SPEC 9).

    Entry verb follows the revision number: revision 1 is a Creation, any
    later revision an Update. Grouping uses ``revised_at``, so this reflects
    the current state of each page rather than a full revision history —
    prior revisions live in ``astrocyte_wiki_revisions`` and are not loaded
    here.
    """
    by_date: dict[str, list[str]] = {}
    for page in pages:
        stamp = getattr(page, "revised_at", None)
        day = stamp.date().isoformat() if isinstance(stamp, datetime) else "unknown"
        verb = "Creation" if int(page.revision or 1) <= 1 else "Update"
        title = (page.title or page.page_id or "untitled").strip()
        by_date.setdefault(day, []).append(f"* **{verb}**: [{title}]({concept_path(page)})")

    lines = ["# Update Log", ""]
    for day in sorted(by_date, reverse=True):
        lines.append(f"## {day}")
        lines.extend(sorted(by_date[day]))
        lines.append("")
    return "\n".join(lines)


def build_bundle(
    pages: list[WikiPage],
    *,
    bank_id: str,
    producer: str | None = None,
    memories: list[VectorItem] | None = None,
    lifecycle: Any = None,
) -> list[BundleFile]:
    """Render a full bundle in memory. Pure — performs no I/O.

    ``memories`` are exported as concepts under ``memory/``. When ``lifecycle``
    is supplied and enabled, each memory carries a derived ``stale_after``
    (see :func:`stale_after_for`).
    """
    if producer is None:
        producer = _default_producer()

    files: list[BundleFile] = []
    seen: set[str] = set()
    # Directory -> (title, href) for index generation.
    dirs: dict[str, list[tuple[str, str]]] = {}
    exported: list[WikiPage] = []

    for page in pages:
        path = concept_path(page)
        if path in seen:
            # page_id is UNIQUE per bank, so a collision means two IDs
            # slugified together. Skip rather than silently overwrite.
            continue
        seen.add(path)
        exported.append(page)
        files.append(BundleFile(path=path, content=render_concept(page, producer=producer)))

        parent = str(Path(path).parent)
        parent = "" if parent == "." else parent
        title = (page.title or page.page_id or "untitled").strip()
        dirs.setdefault(parent, []).append((title, Path(path).name))

    for item in memories or []:
        path = memory_concept_path(item)
        if path in seen:
            continue
        seen.add(path)
        files.append(
            BundleFile(
                path=path,
                content=render_memory(
                    item,
                    producer=producer,
                    stale_after=stale_after_for(item, lifecycle),
                ),
            )
        )
        parent = str(Path(path).parent)
        title = (item.text or item.id).strip().splitlines()[0][:80] or item.id
        dirs.setdefault(parent, []).append((title, Path(path).name))

    # Subdirectory indexes, plus links from the root index into them.
    root_entries = list(dirs.get("", []))
    for directory in sorted(d for d in dirs if d):
        top = directory.split("/")[0]
        files.append(
            BundleFile(
                path=f"{directory}/index.md",
                content=render_index(dirs[directory], heading=directory, root=False),
            )
        )
        if directory == top:
            root_entries.append((top, f"{top}/"))

    files.append(
        BundleFile(
            path="index.md",
            content=render_index(root_entries, heading=f"Knowledge bundle: {bank_id}", root=True),
        )
    )
    if exported:
        files.append(BundleFile(path="log.md", content=render_log(exported)))
    return files


def _default_producer() -> str:
    """``<producer>/<version>`` per SPEC 7."""
    try:
        import importlib.metadata as md

        return f"astrocyte/{md.version('astrocyte')}"
    except Exception:  # pragma: no cover - version metadata is best-effort
        return "astrocyte/unknown"


def export_wiki_bundle(
    pages: list[WikiPage],
    *,
    bank_id: str,
    path: str | Path,
    producer: str | None = None,
    memories: list[VectorItem] | None = None,
    lifecycle: Any = None,
    allowed_roots: list[str | Path] | None = None,
    allow_uncontained: bool = False,
) -> ExportResult:
    """Write an OKF bundle for ``pages`` under ``path``.

    Path containment is enforced by :func:`astrocyte.portability._safe_resolve`
    on every file, so a hostile ``page_id`` cannot escape the bundle root.
    """
    root = _safe_resolve(
        path,
        allowed_roots=allowed_roots,
        allow_uncontained=allow_uncontained,
    )
    files = build_bundle(
        pages,
        bank_id=bank_id,
        producer=producer,
        memories=memories,
        lifecycle=lifecycle,
    )

    written: list[str] = []
    for item in files:
        target = _safe_resolve(
            root / item.path,
            allowed_roots=[root],
            allow_uncontained=False,
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(item.content, encoding="utf-8")
        written.append(item.path)

    concepts = [f for f in written if Path(f).name not in {"index.md", "log.md"}]
    return ExportResult(bank_id=bank_id, concept_count=len(concepts), files=written)
