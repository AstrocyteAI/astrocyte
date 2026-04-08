"""Evaluation suite definition and loading.

Suites can be:
- Built-in names: "basic", "accuracy"
- YAML file paths: "./my-suite.yaml"
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# Suite data model
# ---------------------------------------------------------------------------


@dataclass
class RetainCase:
    """A single memory to retain during evaluation."""

    content: str
    tags: list[str] | None = None
    fact_type: str | None = None
    metadata: dict[str, str] | None = None


@dataclass
class RecallCase:
    """A single recall query with expected results."""

    query: str
    expected_contains: list[str]  # Keywords that should appear in at least one hit
    tags: list[str] | None = None
    max_results: int = 10


@dataclass
class ReflectCase:
    """A single reflect query with expected topics."""

    query: str
    expected_topics: list[str]  # Keywords that should appear in the answer


@dataclass
class EvalSuite:
    """Complete evaluation suite definition."""

    name: str
    retains: list[RetainCase]
    recalls: list[RecallCase]
    reflects: list[ReflectCase] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_suite(suite_ref: str) -> EvalSuite:
    """Load a suite by name or YAML path.

    Built-in suites: "basic", "accuracy"
    Custom suites: any path ending in .yaml or .yml
    """
    if suite_ref.endswith((".yaml", ".yml")):
        return _load_yaml_suite(Path(suite_ref))

    builtin = _BUILTIN_SUITES.get(suite_ref)
    if builtin is None:
        available = ", ".join(sorted(_BUILTIN_SUITES.keys()))
        raise ValueError(f"Unknown suite '{suite_ref}'. Available: {available}")
    return builtin


def _load_yaml_suite(path: Path) -> EvalSuite:
    """Load a suite from a YAML file."""
    with open(path) as f:
        data = yaml.safe_load(f) or {}

    name = data.get("name", path.stem)
    retains = [
        RetainCase(
            content=r["content"],
            tags=r.get("tags"),
            fact_type=r.get("fact_type"),
            metadata=r.get("metadata"),
        )
        for r in data.get("retain", [])
    ]
    recalls = [
        RecallCase(
            query=r["query"],
            expected_contains=r.get("expected_contains", []),
            tags=r.get("tags"),
            max_results=r.get("max_results", 10),
        )
        for r in data.get("recall", [])
    ]
    reflects = [
        ReflectCase(
            query=r["query"],
            expected_topics=r.get("expected_topics", []),
        )
        for r in data.get("reflect", [])
    ]

    return EvalSuite(name=name, retains=retains, recalls=recalls, reflects=reflects)


# ---------------------------------------------------------------------------
# Built-in suites
# ---------------------------------------------------------------------------


_BASIC_SUITE = EvalSuite(
    name="basic",
    retains=[
        RetainCase(content="Calvin prefers dark mode in all applications", tags=["preference", "ui"]),
        RetainCase(content="The deployment pipeline uses GitHub Actions with a 10-minute timeout", tags=["technical"]),
        RetainCase(
            content="Our team follows trunk-based development with feature flags", tags=["technical", "process"]
        ),
        RetainCase(content="Calvin's favorite programming language is Python 3.11", tags=["preference"]),
        RetainCase(content="The database uses PostgreSQL 16 with pgvector extension", tags=["technical", "database"]),
        RetainCase(content="Weekly standup is every Monday at 9am Pacific time", tags=["process", "meeting"]),
        RetainCase(content="The API rate limit is 100 requests per minute per user", tags=["technical", "api"]),
        RetainCase(content="Calvin started at the company in January 2025", tags=["personal"]),
        RetainCase(content="The production environment runs on AWS us-east-1", tags=["technical", "infra"]),
        RetainCase(content="Code reviews require at least two approvals before merge", tags=["process"]),
    ],
    recalls=[
        RecallCase(query="What are Calvin's preferences?", expected_contains=["dark mode", "Python"]),
        RecallCase(query="How does deployment work?", expected_contains=["GitHub Actions"]),
        RecallCase(query="What database do we use?", expected_contains=["PostgreSQL", "pgvector"]),
        RecallCase(query="When is the standup?", expected_contains=["Monday", "9am"]),
        RecallCase(query="What is the API rate limit?", expected_contains=["100 requests"]),
        RecallCase(query="Where does production run?", expected_contains=["AWS", "us-east-1"]),
        RecallCase(query="What is the code review policy?", expected_contains=["two approvals"]),
        RecallCase(query="What development methodology does the team follow?", expected_contains=["trunk-based"]),
        RecallCase(query="When did Calvin join?", expected_contains=["January 2025"]),
        RecallCase(query="Does Calvin like light mode?", expected_contains=["dark mode"]),
    ],
    reflects=[
        ReflectCase(
            query="Summarize what we know about Calvin", expected_topics=["dark mode", "Python", "January 2025"]
        ),
        ReflectCase(query="Describe the technical stack", expected_topics=["PostgreSQL", "GitHub Actions", "AWS"]),
    ],
)


_ACCURACY_SUITE = EvalSuite(
    name="accuracy",
    retains=[
        # -- Baseline facts --
        RetainCase(content="Calvin prefers dark mode in all applications", tags=["preference"]),
        RetainCase(content="The primary database is PostgreSQL 16 with pgvector extension", tags=["technical"]),
        RetainCase(content="Production runs on AWS us-east-1 with a failover in us-west-2", tags=["infra"]),
        RetainCase(content="The API rate limit is 100 requests per minute per user", tags=["technical", "api"]),
        RetainCase(content="Calvin joined the company in January 2025 as a senior engineer", tags=["personal"]),
        # -- Contradicting / superseding facts (tests temporal update handling) --
        RetainCase(content="The API rate limit was increased to 500 requests per minute per user", tags=["technical", "api"]),
        RetainCase(content="Calvin was promoted to staff engineer in March 2025", tags=["personal"]),
        # -- Overlapping facts with different specificity --
        RetainCase(content="We use PostgreSQL for the main application and Redis for caching and rate limiting", tags=["technical"]),
        RetainCase(content="The Redis cluster runs 3 nodes with 64GB RAM each in production", tags=["technical", "infra"]),
        # -- Dense multi-entity facts --
        RetainCase(
            content="The Q1 2025 incident was caused by a pgvector index rebuild that locked the memories table "
            "for 47 minutes, affecting 12,000 users across us-east-1",
            tags=["incident"],
        ),
        # -- Subtly related facts requiring disambiguation --
        RetainCase(content="The team uses OpenAI text-embedding-3-small for vector embeddings", tags=["technical", "ai"]),
        RetainCase(content="The LLM gateway routes completions through LiteLLM to OpenAI and Anthropic", tags=["technical", "ai"]),
        # -- Facts with numeric precision --
        RetainCase(content="Average recall latency in production is 23ms at p50 and 89ms at p95", tags=["performance"]),
        RetainCase(content="The vector index contains 2.4 million embeddings across 180 banks", tags=["scale"]),
        # -- Negation / absence facts --
        RetainCase(content="We evaluated Pinecone but chose not to use it due to vendor lock-in concerns", tags=["decision"]),
    ],
    recalls=[
        # -- Semantic paraphrase (very different wording) --
        RecallCase(query="Which cloud region hosts our services?", expected_contains=["AWS", "us-east-1"]),
        RecallCase(query="What is Calvin's display theme preference?", expected_contains=["dark mode"]),
        # -- Temporal / superseding facts (should surface the latest info) --
        RecallCase(query="What is the current API rate limit?", expected_contains=["500"]),
        RecallCase(query="What is Calvin's current role?", expected_contains=["staff engineer"]),
        # -- Multi-hop: connecting facts across retained memories --
        RecallCase(
            query="What databases and caching layers make up our data tier?",
            expected_contains=["PostgreSQL", "Redis"],
        ),
        RecallCase(
            query="Which AI providers do we use and for what?",
            expected_contains=["OpenAI", "embedding"],
        ),
        # -- Numeric precision retrieval --
        RecallCase(query="What is our p95 recall latency?", expected_contains=["89ms"]),
        RecallCase(query="How many embeddings are in the index?", expected_contains=["2.4 million"]),
        # -- Specificity: broad query should pull multiple related facts --
        RecallCase(
            query="Tell me everything about our infrastructure",
            expected_contains=["AWS"],
        ),
        # -- Negation understanding --
        RecallCase(query="Do we use Pinecone?", expected_contains=["not"]),
        # -- Incident detail retrieval --
        RecallCase(query="What happened in the Q1 2025 incident?", expected_contains=["pgvector", "index"]),
        RecallCase(query="How many users were affected by the outage?", expected_contains=["12,000"]),
        # -- Entity disambiguation (OpenAI for embeddings vs completions) --
        RecallCase(query="What model do we use for embeddings?", expected_contains=["text-embedding-3-small"]),
        # -- Out-of-scope queries (nothing relevant retained) --
        RecallCase(query="What is our revenue forecast for next quarter?", expected_contains=[]),
        RecallCase(query="Who is the CEO?", expected_contains=[]),
    ],
    reflects=[
        # -- Multi-source synthesis: requires combining 3+ facts --
        ReflectCase(
            query="Describe our complete data infrastructure including databases, caching, and vector storage",
            expected_topics=["PostgreSQL", "pgvector", "Redis", "2.4 million"],
        ),
        # -- Temporal reasoning: should prefer the updated fact --
        ReflectCase(
            query="Summarize Calvin's history at the company",
            expected_topics=["January 2025", "staff engineer"],
        ),
        # -- Analytical synthesis: requires judgment, not just retrieval --
        ReflectCase(
            query="What are the key risks and past incidents in our infrastructure?",
            expected_topics=["pgvector", "index", "12,000", "failover"],
        ),
        # -- Broad synthesis across many facts --
        ReflectCase(
            query="Give a technical overview of our AI and memory stack",
            expected_topics=["OpenAI", "embedding", "LiteLLM", "pgvector"],
        ),
    ],
)


_BUILTIN_SUITES: dict[str, EvalSuite] = {
    "basic": _BASIC_SUITE,
    "accuracy": _ACCURACY_SUITE,
}
