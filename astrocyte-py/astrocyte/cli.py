"""Astrocyte CLI — operator tooling for MIP configs.

Commands:
    astrocyte mip lint <path>            Validate a mip.yaml, printing errors and warnings.
    astrocyte mip explain <path> ...     Show which rule fires for a hypothetical input.

The CLI is intentionally minimal: argparse-based, no external dependencies, and
reuses the same loader / router code that runtime uses. It is the recommended
way to verify pipeline overrides (chunker, dedup, rerank, reflect) before
deploying a rule change.
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
import warnings
from pathlib import Path
from typing import Sequence

from astrocyte.errors import ConfigError
from astrocyte.mip import MipRouter, load_mip_config
from astrocyte.mip.rule_engine import RuleEngineInput, evaluate_rules
from astrocyte.mip.schema import PipelineSpec
from astrocyte.types import MetadataValue

# ---------------------------------------------------------------------------
# astrocyte mip lint
# ---------------------------------------------------------------------------


def _cmd_mip_lint(args: argparse.Namespace) -> int:
    """Load and validate a mip.yaml. Returns 0 if clean, 1 on any error."""
    path = Path(args.path)
    print(f"Linting MIP config: {path}")

    captured: list[warnings.WarningMessage] = []
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            config = load_mip_config(path)
            captured = list(caught)
    except ConfigError as exc:
        print(f"  error: {exc}", file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(f"  error: {exc}", file=sys.stderr)
        return 1

    rule_count = len(config.rules or [])
    bank_count = len(config.banks or [])
    print(f"  ok: {rule_count} rule(s), {bank_count} bank(s)")

    if captured:
        print(f"  {len(captured)} warning(s):")
        for w in captured:
            print(f"    - {w.message}")
    return 0


# ---------------------------------------------------------------------------
# astrocyte mip explain
# ---------------------------------------------------------------------------


def _parse_kv(items: Sequence[str] | None) -> dict[str, MetadataValue]:
    """Parse a list of ``key=value`` strings into a metadata dict.

    Numeric and boolean values are coerced; everything else stays as a string.
    """
    out: dict[str, MetadataValue] = {}
    if not items:
        return out
    for raw in items:
        if "=" not in raw:
            raise SystemExit(f"Invalid --metadata entry (expected KEY=VALUE): {raw!r}")
        key, _, val = raw.partition("=")
        key = key.strip()
        val = val.strip()
        coerced: MetadataValue
        if val.lower() in ("true", "false"):
            coerced = val.lower() == "true"
        else:
            try:
                coerced = int(val)
            except ValueError:
                try:
                    coerced = float(val)
                except ValueError:
                    coerced = val
        out[key] = coerced
    return out


def _format_pipeline(pipeline: PipelineSpec | None) -> list[str]:
    """Render a PipelineSpec as a list of indented printable lines."""
    if pipeline is None:
        return []
    lines: list[str] = ["  pipeline:"]
    if pipeline.version is not None:
        lines.append(f"    version: {pipeline.version}")
    for field in ("chunker", "dedup", "rerank", "reflect"):
        spec = getattr(pipeline, field)
        if spec is None:
            continue
        # Show only fields that are set
        set_fields = {f.name: getattr(spec, f.name) for f in dataclasses.fields(spec) if getattr(spec, f.name) is not None}
        if set_fields:
            lines.append(f"    {field}: {set_fields}")
    return lines


def _cmd_mip_explain(args: argparse.Namespace) -> int:
    """Show which rule(s) match a hypothetical input and the resulting decision."""
    path = Path(args.path)
    try:
        config = load_mip_config(path)
    except (ConfigError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    metadata = _parse_kv(args.metadata)
    rule_input = RuleEngineInput(
        content=args.content or "",
        content_type=args.content_type,
        metadata=metadata or None,
        tags=list(args.tag) if args.tag else None,
        pii_detected=args.pii_detected,
        source=args.source,
    )

    print("Input:")
    print(f"  content_type: {rule_input.content_type!r}")
    print(f"  metadata: {rule_input.metadata}")
    print(f"  tags: {rule_input.tags}")
    print(f"  pii_detected: {rule_input.pii_detected}")
    print(f"  source: {rule_input.source!r}")

    # 1. Show every matching rule (mechanical eval, before escalation policy)
    sorted_rules = sorted(config.rules or [], key=lambda r: r.priority)
    matches = evaluate_rules(sorted_rules, rule_input)
    print(f"\nMatched rules ({len(matches)}):")
    if not matches:
        print("  (none)")
    for m in matches:
        print(f"  - {m.rule.name} (priority={m.rule.priority}, override={m.rule.override}, confidence={m.confidence})")

    # 2. Show what the synchronous router would actually return
    router = MipRouter(config)
    decision = router.route_sync(rule_input)
    print("\nDecision (sync routing):")
    if decision is None:
        print("  → escalation required (would call intent layer at runtime)")
        return 0

    print(f"  resolved_by: {decision.resolved_by}")
    print(f"  rule_name:   {decision.rule_name}")
    print(f"  bank_id:     {decision.bank_id}")
    print(f"  tags:        {decision.tags}")
    print(f"  retain_policy: {decision.retain_policy}")
    print(f"  confidence:  {decision.confidence}")
    for line in _format_pipeline(decision.pipeline):
        print(line)
    return 0


# ---------------------------------------------------------------------------
# argparse plumbing
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="astrocyte", description="Astrocyte operator CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    mip = sub.add_parser("mip", help="MIP (Memory Intent Protocol) tools")
    mip_sub = mip.add_subparsers(dest="mip_command", required=True)

    lint = mip_sub.add_parser("lint", help="Validate a mip.yaml file")
    lint.add_argument("path", help="Path to mip.yaml")
    lint.set_defaults(func=_cmd_mip_lint)

    explain = mip_sub.add_parser(
        "explain", help="Show which rule fires for a hypothetical retain input",
    )
    explain.add_argument("path", help="Path to mip.yaml")
    explain.add_argument("--content", default="", help="Inbound content (text body)")
    explain.add_argument("--content-type", default=None, help="Content type, e.g. text|conversation|document")
    explain.add_argument("--metadata", action="append", default=[], help="Metadata KEY=VALUE (repeatable)")
    explain.add_argument("--tag", action="append", default=[], help="Tag (repeatable)")
    explain.add_argument("--source", default=None, help="Source identifier")
    explain.add_argument("--pii-detected", action="store_true", help="Mark PII as already detected")
    explain.set_defaults(func=_cmd_mip_explain)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
