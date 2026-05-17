"""Fix 4 (conv-run-4) — episodic event index.

Detects facts of the form "User experienced / attended / met X at /
during Y" and indexes them separately from the generic 'experience'
fact_type. The store-level schema has a CHECK constraint on
``fact_type`` (migration 020), so we don't introduce a new SQL value;
instead we mark each detected fact by appending a namespaced marker
``episodic:event`` to its ``entities`` array. The existing
``search_facts_by_entity`` SPI then becomes the read path: querying for
the marker returns just episodic facts.

Why a separate index: the Brandon-Flowers-encounter LME failure shape
is "user mentions a location/event in passing, then asks about who they
met there". The generic semantic / keyword strategies miss it because
the encounter is buried in a long session and not entity-typed. By
tagging episodic facts at retain and explicitly querying for them when
the question carries a location / event cue, we give the answerer a
direct path to the episodic memory.

Detection is regex-based today — the cost of an LLM classifier per fact
would dominate ingest. The regex is intentionally narrow (verb stems
+ ≥1 noun-phrase content) so false positives stay low; missing an
edge case is preferable to spraying the marker.

Detection rule (conjunction):
1. The fact text matches an EPISODIC_VERB pattern: ``attended``,
   ``visited``, ``met``, ``went to``, ``saw``, ``encountered``,
   ``ran into``, ``bumped into``, ``experienced``.
2. The fact has at least one entity (so there's something to anchor
   the encounter to) AND the entity is not purely a role-marker
   (``role:doctor``-style).

The marker is appended in-place to ``fact.entities`` so the caller's
existing ``save_facts`` call writes the updated array — no extra DB
round-trip.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from astrocyte.types import PageIndexFact

_logger = logging.getLogger("astrocyte.pipeline.episodic_extract")


EPISODIC_MARKER = "episodic:event"

# Verb stems that signal an episodic encounter. Word-boundary anchored
# and case-insensitive; matches both base and -ed/-s/-ing forms via
# `(?:ed|s|ing)?`.
_EPISODIC_VERBS = (
    "attend",
    "visit",
    "meet",
    "met",  # irregular past
    "saw",  # irregular past
    "see",
    "encounter",
    "experienc",
    "went to",
    "go to",
    "going to",
    "ran into",
    "run into",
    "running into",
    "bumped into",
    "bump into",
    "watched",
    "watch",
)

_EPISODIC_RE = re.compile(
    r"\b(" + "|".join(re.escape(v) for v in _EPISODIC_VERBS) + r")(?:ed|s|ing)?\b",
    re.IGNORECASE,
)

# Question-side cues that should trigger episodic recall. Used by the
# query-time matcher; the indexing path doesn't read this.
_LOCATION_CUE_RE = re.compile(
    r"\b(at|in|during|when (?:I|we) (?:were|was)|where|while at)\b",
    re.IGNORECASE,
)
_EVENT_CUE_RE = re.compile(
    r"\b("
    r"concert|show|gig|festival|game|match|conference|meetup|party|"
    r"event|trip|vacation|wedding|funeral|reunion|ceremony|"
    r"premiere|tournament|exhibit|exhibition|expo|tour"
    r")\b",
    re.IGNORECASE,
)


def is_episodic_fact_text(text: str) -> bool:
    """Return True when the fact text reads like an episodic encounter.

    Exposed as a module-level helper so callers can probe a candidate
    without mutating it (used by tests).
    """
    if not text:
        return False
    return bool(_EPISODIC_RE.search(text))


def tag_episodic_facts(facts: list[PageIndexFact]) -> int:
    """Mutate ``facts`` in-place, appending ``episodic:event`` to the
    ``entities`` array of every fact whose text matches the episodic
    pattern AND that carries at least one non-role entity. Returns the
    number of facts tagged.

    Idempotent: re-running on already-tagged facts is a no-op (the
    marker is only appended when absent).
    """
    if not facts:
        return 0
    n_tagged = 0
    for f in facts:
        if not is_episodic_fact_text(f.text or ""):
            continue
        ents = list(f.entities or [])
        # Require at least one non-marker entity so we don't tag a bare
        # "user attended" with no anchor.
        non_marker_entities = [e for e in ents if e and not e.startswith("role:") and not e.startswith("episodic:")]
        if not non_marker_entities:
            continue
        if EPISODIC_MARKER in ents:
            continue
        ents.append(EPISODIC_MARKER)
        f.entities = ents
        n_tagged += 1
    if n_tagged:
        _logger.info(
            "episodic_extract: tagged %d/%d facts as episodic",
            n_tagged,
            len(facts),
        )
    return n_tagged


def question_has_episodic_cue(question: str) -> bool:
    """Return True when the question carries a location or event cue
    that should trigger episodic recall.

    Used by the query path (``astrocyte_client.search``) to decide
    whether to add an episodic-marker fact query alongside the
    semantic/temporal fact strategies. False is the safe default — when
    in doubt we don't inject episodic candidates, since duplicate facts
    in the answerer prompt can cause cross-category regression.
    """
    if not question:
        return False
    if _LOCATION_CUE_RE.search(question):
        return True
    if _EVENT_CUE_RE.search(question):
        return True
    return False
