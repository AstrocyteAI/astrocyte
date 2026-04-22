"""M3 — inbound extraction: normalize → chunking profile resolution → metadata/tags.

Pipeline shape: **Raw → Normalizer → Chunker → Entity extract (optional) → retain**.

See ``docs/_design/product-roadmap-v1.md`` (M3) and ``built-in-pipeline.md``.
"""

from __future__ import annotations

import dataclasses
import json
import re
from dataclasses import dataclass
from functools import lru_cache

import yaml

from astrocyte.config import AstrocyteConfig, ExtractionProfileConfig, SourceConfig
from astrocyte.mip.schema import ChunkerSpec
from astrocyte.types import Metadata, MetadataValue, RetainRequest

_STRATEGY_ALIASES: dict[str, str] = {
    "semantic": "sentence",
    "dialogue": "dialogue",
    "sentence": "sentence",
    "paragraph": "paragraph",
    "fixed": "fixed",
}

# Code defaults; packaged ``extraction_builtin.yaml`` (if present) merges on top, then user config.
BUILTIN_EXTRACTION_PROFILES: dict[str, ExtractionProfileConfig] = {
    "builtin_text": ExtractionProfileConfig(
        content_type="text",
        chunking_strategy="sentence",
    ),
    "builtin_conversation": ExtractionProfileConfig(
        content_type="conversation",
        chunking_strategy="dialogue",
    ),
}


def _extraction_profile_from_mapping(data: dict[str, object]) -> ExtractionProfileConfig:
    valid = {f.name for f in dataclasses.fields(ExtractionProfileConfig)}
    filtered = {k: v for k, v in data.items() if k in valid}
    return ExtractionProfileConfig(**filtered)


@lru_cache(maxsize=1)
def _packaged_yaml_builtin_profiles() -> dict[str, ExtractionProfileConfig]:
    """Load optional ``extraction_builtin.yaml`` next to this module (wheel-safe via importlib.resources)."""
    try:
        from importlib import resources

        path = resources.files("astrocyte.pipeline").joinpath("extraction_builtin.yaml")
        raw = path.read_text(encoding="utf-8")
    except (OSError, TypeError, FileNotFoundError, ValueError, ModuleNotFoundError):
        return {}
    loaded = yaml.safe_load(raw) or {}
    if not isinstance(loaded, dict):
        return {}
    out: dict[str, ExtractionProfileConfig] = {}
    for name, pdata in loaded.items():
        if isinstance(pdata, dict):
            out[str(name)] = _extraction_profile_from_mapping(pdata)
    return out


def _all_builtin_profiles() -> dict[str, ExtractionProfileConfig]:
    """Code builtins, overridden/extended by packaged YAML when shipped."""
    return {**BUILTIN_EXTRACTION_PROFILES, **_packaged_yaml_builtin_profiles()}


def merged_user_and_builtin_profiles(
    user: dict[str, ExtractionProfileConfig] | None,
) -> dict[str, ExtractionProfileConfig]:
    """Merge packaged + code builtins with an optional user table (user wins on name clash)."""
    return {**_all_builtin_profiles(), **(user or {})}


def merged_extraction_profiles(config: AstrocyteConfig) -> dict[str, ExtractionProfileConfig]:
    """Return builtins plus ``config.extraction_profiles`` (user definitions override same-named builtins)."""
    return merged_user_and_builtin_profiles(config.extraction_profiles)


def extraction_profile_for_source(
    source_id: str,
    sources: dict[str, SourceConfig] | None,
) -> str | None:
    """Resolve ``sources.*.extraction_profile`` for ingest callers (full webhook wiring is M4)."""
    if not sources:
        return None
    src = sources.get(source_id)
    if src is None:
        return None
    return src.extraction_profile


def effective_content_type(request_ct: str, profile: ExtractionProfileConfig | None) -> str:
    """Use profile ``content_type`` when set; otherwise ``RetainRequest.content_type``."""
    if profile is not None and profile.content_type:
        return str(profile.content_type).strip().lower()
    return (request_ct or "text").strip().lower() or "text"


def _normalize_chunking_strategy(name: str) -> str:
    key = (name or "").strip().lower()
    if key in _STRATEGY_ALIASES:
        return _STRATEGY_ALIASES[key]
    raise ValueError(f"Unknown chunking strategy: {name!r}")


@dataclass(frozen=True)
class ChunkingDecision:
    """Resolved chunking parameters for one retain call.

    ``overlap`` is ``None`` when no source set it explicitly — callers should
    fall through to ``chunk_text``'s default.
    """

    strategy: str
    max_size: int
    overlap: int | None = None


def resolve_retain_chunking(
    content_type: str,
    *,
    profile: ExtractionProfileConfig | None,
    default_strategy: str,
    default_max_chunk_size: int,
    mip_chunker: ChunkerSpec | None = None,
) -> ChunkingDecision:
    """Pick chunking parameters for :class:`~astrocyte.types.RetainRequest`.

    Precedence (highest to lowest):
    1. ``mip_chunker`` — per-rule overrides from a MIP ``RoutingDecision``
    2. ``profile`` — per-source ``ExtractionProfileConfig``
    3. ``content_type`` — built-in routing for known types
    4. ``default_strategy`` / ``default_max_chunk_size`` — orchestrator defaults

    Within each layer, individual fields are independent — a MIP override that
    only sets ``max_size`` still allows ``profile`` or ``content_type`` to
    determine the strategy.
    """
    # Layer 4: defaults
    strategy: str = default_strategy
    max_size: int = default_max_chunk_size
    overlap: int | None = None

    # Layer 3: content_type
    ct = (content_type or "").strip().lower()
    if ct in ("conversation", "transcript"):
        strategy = "dialogue"
    elif ct in ("document", "email"):
        strategy = "paragraph"
    elif ct == "event":
        strategy = "sentence"

    # Layer 2: profile
    if profile is not None:
        if profile.chunk_size is not None:
            max_size = int(profile.chunk_size)
        if profile.chunking_strategy:
            strategy = _normalize_chunking_strategy(str(profile.chunking_strategy))

    # Layer 1: MIP override (highest precedence)
    if mip_chunker is not None:
        if mip_chunker.strategy is not None:
            strategy = _normalize_chunking_strategy(mip_chunker.strategy)
        if mip_chunker.max_size is not None:
            max_size = int(mip_chunker.max_size)
        if mip_chunker.overlap is not None:
            overlap = int(mip_chunker.overlap)

    return ChunkingDecision(strategy=strategy, max_size=max_size, overlap=overlap)


# --- Line endings & per-type normalizer (conservative heuristics) ---


def _normalize_line_endings(s: str) -> str:
    return s.replace("\r\n", "\n").replace("\r", "\n")


def _looks_like_rfc_headers(block: str) -> bool:
    lines = [ln for ln in block.split("\n") if ln.strip()]
    if len(lines) < 2:
        return False
    headerish = sum(1 for ln in lines if re.match(r"^[A-Za-z][A-Za-z0-9-]*:", ln))
    return headerish >= 2 and headerish >= len(lines) - 1


def _normalize_email_body(s: str) -> str:
    if "\n\n" in s:
        head, rest = s.split("\n\n", 1)
        if _looks_like_rfc_headers(head):
            s = rest
    if "\n-- \n" in s:
        s = s.split("\n-- \n", 1)[0]
    return s.strip()


def _collapse_blank_runs(s: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", s.strip())


def normalize_content(raw: str, content_type: str, *, profile: ExtractionProfileConfig | None = None) -> str:
    """Normalize raw inbound text for chunking/embed (stage before chunker).

    Per-type behavior is intentionally shallow: line-ending cleanup, light email/event handling,
    and transcript/document blank-line collapse. MIME decoding and full mail parsing are **not** done here.
    """
    _ = profile  # reserved for future profile-driven normalizer options
    ct = (content_type or "text").strip().lower()
    text = raw or ""
    text = text.lstrip("\ufeff")
    text = _normalize_line_endings(text)

    if ct == "email":
        text = _normalize_email_body(text)
    elif ct == "document":
        text = _collapse_blank_runs(text)
    elif ct in ("conversation", "transcript"):
        text = _collapse_blank_runs(text)
    elif ct == "event":
        text = text.strip()
    else:
        text = text.strip()
    return text


def _json_path_get(root: object, path: str) -> MetadataValue | None:
    """Minimal ``$.a.b`` style path for JSON objects (leading ``$`` optional)."""
    p = path.strip()
    if p.startswith("$"):
        p = p[1:]
    if p.startswith("."):
        p = p[1:]
    if not p:
        return None
    cur: object = root
    for part in p.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    if isinstance(cur, (str, int, float, bool)) or cur is None:
        return cur
    if isinstance(cur, list):
        return json.dumps(cur)
    return str(cur)


def apply_metadata_mapping(
    content: str,
    profile: ExtractionProfileConfig | None,
) -> Metadata | None:
    """Apply ``ExtractionProfileConfig.metadata_mapping`` (JSON paths → metadata keys)."""
    if profile is None or not profile.metadata_mapping:
        return None
    data: object | None
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        data = None
    if not isinstance(data, (dict, list)):
        return None
    root = data if isinstance(data, dict) else {"$": data}
    out: Metadata = {}
    for meta_key, spec in profile.metadata_mapping.items():
        path = str(spec).strip()
        if not path.startswith("$"):
            # Literal string value (no JSON extraction)
            out[str(meta_key)] = path
            continue
        val = _json_path_get(root, path)
        if val is not None:
            out[str(meta_key)] = val
    return out or None


def apply_tag_rules(content: str, profile: ExtractionProfileConfig | None) -> list[str] | None:
    """Apply ``tag_rules``: ``contains`` / ``match`` substring → ``tags``."""
    if profile is None or not profile.tag_rules:
        return None
    extra: list[str] = []
    for rule in profile.tag_rules:
        if not isinstance(rule, dict):
            continue
        needle = rule.get("contains")
        if needle is None:
            needle = rule.get("match")
        tags = rule.get("tags")
        if needle is None or not tags:
            continue
        if str(needle) in content:
            extra.extend(str(t) for t in tags)
    return extra or None


def should_extract_entities(profile: ExtractionProfileConfig | None, *, graph_store_configured: bool) -> bool:
    """Whether to run LLM entity extraction (requires graph store to persist)."""
    if not graph_store_configured:
        return False
    if profile is None:
        return True
    ee = profile.entity_extraction
    if ee is None:
        return True
    if ee is False or ee == "disabled":
        return False
    return True


def merge_tags(base: list[str] | None, extra: list[str] | None) -> list[str] | None:
    if not base and not extra:
        return None
    ordered: list[str] = []
    for t in (base or []) + (extra or []):
        if t and t not in ordered:
            ordered.append(t)
    return ordered or None


def merge_metadata(
    mapped: Metadata | None,
    request_meta: Metadata | None,
) -> Metadata | None:
    """Merge profile-derived metadata with request metadata; **request wins** on key conflicts."""
    if not mapped and not request_meta:
        return None
    out: Metadata = {**(mapped or {}), **(request_meta or {})}
    return out or None


@dataclass(frozen=True)
class PreparedRetainInput:
    """Result of Raw → normalizer → metadata/tags merge (before chunk/embed in orchestrator)."""

    text: str
    metadata: Metadata | None
    tags: list[str] | None
    extract_entities: bool
    effective_content_type: str
    fact_type: str


def resolve_retain_fact_type(profile: ExtractionProfileConfig | None) -> str:
    """Default fact type for stored chunks; profile ``fact_type`` overrides ``world``."""
    if profile is not None and profile.fact_type and str(profile.fact_type).strip():
        return str(profile.fact_type).strip()
    return "world"


def prepare_retain_input(
    request: RetainRequest,
    profile: ExtractionProfileConfig | None,
    *,
    graph_store_configured: bool,
) -> PreparedRetainInput:
    """Single entrypoint for the pre-chunk extraction chain (normalizer + profile fields)."""
    ect = effective_content_type(request.content_type, profile)
    normalized = normalize_content(request.content, ect, profile=profile)
    mapped = apply_metadata_mapping(normalized, profile)
    merged_meta = merge_metadata(mapped, request.metadata)
    rule_tags = apply_tag_rules(normalized, profile)
    merged_tags = merge_tags(request.tags, rule_tags)
    do_entities = should_extract_entities(profile, graph_store_configured=graph_store_configured)
    return PreparedRetainInput(
        text=normalized,
        metadata=merged_meta,
        tags=merged_tags,
        extract_entities=do_entities,
        effective_content_type=ect,
        fact_type=resolve_retain_fact_type(profile),
    )
