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
        RetainCase(content="Calvin prefers dark mode in all applications", tags=["preference"]),
        RetainCase(content="The deployment pipeline uses GitHub Actions", tags=["technical"]),
        RetainCase(content="Our team follows trunk-based development", tags=["process"]),
        RetainCase(content="Calvin's favorite language is Python", tags=["preference"]),
        RetainCase(content="The database is PostgreSQL 16 with pgvector", tags=["technical"]),
        RetainCase(content="Weekly standup is Monday at 9am Pacific", tags=["meeting"]),
        RetainCase(content="API rate limit is 100 requests per minute", tags=["technical"]),
        RetainCase(content="Calvin joined in January 2025", tags=["personal"]),
        RetainCase(content="Production runs on AWS us-east-1", tags=["infra"]),
        RetainCase(content="Code reviews need two approvals", tags=["process"]),
        RetainCase(content="The frontend uses React 18 with TypeScript", tags=["technical"]),
        RetainCase(content="Monitoring uses Grafana dashboards with Prometheus metrics", tags=["technical"]),
        RetainCase(content="The CI pipeline runs tests in parallel with 8 workers", tags=["technical"]),
        RetainCase(content="Feature flags are managed through LaunchDarkly", tags=["technical"]),
        RetainCase(content="The team uses Slack for communication and GitHub Issues for tracking", tags=["process"]),
    ],
    recalls=[
        # Exact match queries
        RecallCase(query="What UI theme does Calvin prefer?", expected_contains=["dark mode"]),
        RecallCase(query="What CI/CD tool is used for deployment?", expected_contains=["GitHub Actions"]),
        RecallCase(query="What database technology is used?", expected_contains=["PostgreSQL"]),
        # Semantic similarity queries (paraphrased)
        RecallCase(query="Tell me about Calvin's display preferences", expected_contains=["dark mode"]),
        RecallCase(query="How do we ship code to production?", expected_contains=["GitHub Actions"]),
        RecallCase(query="What data store does the application use?", expected_contains=["PostgreSQL"]),
        # Entity-based queries
        RecallCase(query="What do we know about Calvin?", expected_contains=["dark mode"]),
        RecallCase(query="What runs on AWS?", expected_contains=["production", "us-east-1"]),
        # Negative queries (should still return something tangentially related)
        RecallCase(query="What is the company's vacation policy?", expected_contains=[]),
        RecallCase(query="How many employees are there?", expected_contains=[]),
        # Multi-fact queries
        RecallCase(query="Describe the development workflow", expected_contains=["trunk-based"]),
        RecallCase(query="What monitoring tools are used?", expected_contains=["Grafana", "Prometheus"]),
        RecallCase(query="How are feature flags managed?", expected_contains=["LaunchDarkly"]),
        RecallCase(query="What frontend technologies are used?", expected_contains=["React", "TypeScript"]),
        RecallCase(query="How does the team communicate?", expected_contains=["Slack"]),
    ],
    reflects=[
        ReflectCase(query="Summarize the technical stack", expected_topics=["PostgreSQL", "React", "AWS"]),
        ReflectCase(query="What are Calvin's preferences?", expected_topics=["dark mode", "Python"]),
        ReflectCase(query="Describe the team's development process", expected_topics=["trunk-based", "code review"]),
    ],
)


_BUILTIN_SUITES: dict[str, EvalSuite] = {
    "basic": _BASIC_SUITE,
    "accuracy": _ACCURACY_SUITE,
}
