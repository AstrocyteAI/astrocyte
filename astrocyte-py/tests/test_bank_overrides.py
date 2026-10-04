"""Per-bank ``homeostasis`` / ``barriers`` / ``signal_quality`` and bank profiles.

A bank's section resolves as top-level section → the bank's ``profile`` → the
bank's own block, and the policy layer enforces the resolved section for that
bank only.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from astrocyte._astrocyte import Astrocyte
from astrocyte.config import load_config
from astrocyte.errors import ConfigError, RateLimited
from astrocyte.testing.in_memory import InMemoryEngineProvider


def _config_path(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "astrocyte.yaml"
    path.write_text("provider: test\n" + text)
    return path


def _brain(tmp_path: Path, text: str) -> Astrocyte:
    brain = Astrocyte.from_config(_config_path(tmp_path, text))
    brain.set_engine_provider(InMemoryEngineProvider())
    return brain


# ── Config resolution ──


def test_bank_profile_supplies_the_banks_sections(tmp_path: Path) -> None:
    config = load_config(_config_path(tmp_path, "banks:\n  helpdesk:\n    profile: support\n  plain: {}\n"))
    assert config.bank_homeostasis("helpdesk").rate_limits.retain_per_minute == 30
    assert config.bank_barriers("helpdesk").pii.mode == "regex"
    assert config.bank_signal_quality("helpdesk").dedup.similarity_threshold == 0.92
    assert config.bank_signal_quality("helpdesk").noisy_bank.action == "throttle"
    # A bank without overrides, an unknown bank and no bank all read top-level.
    for bank_id in ("plain", "unknown", None):
        assert config.bank_homeostasis(bank_id) is config.homeostasis
        assert config.bank_barriers(bank_id) is config.barriers
        assert config.bank_signal_quality(bank_id) is config.signal_quality


def test_bank_layers_resolve_top_level_then_profile_then_own_block(tmp_path: Path) -> None:
    config = load_config(
        _config_path(
            tmp_path,
            "homeostasis:\n  recall_max_tokens: 111\n  reflect_max_tokens: 222\n"
            "  rate_limits:\n    recall_per_minute: 7\n"
            "banks:\n"
            "  b1:\n    profile: support\n"
            "    homeostasis:\n      reflect_max_tokens: 999\n",
        )
    )
    h = config.bank_homeostasis("b1")
    assert h.reflect_max_tokens == 999, "the bank's own block beats its profile"
    assert h.recall_max_tokens == 4096, "the bank's profile beats the top-level section"
    assert h.rate_limits.retain_per_minute == 30, "from the profile"
    assert h.rate_limits.recall_per_minute == 60, "profile beats top-level, key by key"
    assert config.homeostasis.recall_max_tokens == 111, "top-level untouched"


def test_top_level_fills_keys_neither_bank_layer_sets(tmp_path: Path) -> None:
    config = load_config(
        _config_path(
            tmp_path,
            "barriers:\n  pii:\n    mode: regex\n    action: warn\n"
            "banks:\n  b1:\n    barriers:\n      validation:\n        max_content_length: 5\n",
        )
    )
    b = config.bank_barriers("b1")
    assert (b.pii.mode, b.pii.action, b.validation.max_content_length) == ("regex", "warn", 5)


def test_unknown_bank_profile_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="Profile not found"):
        load_config(_config_path(tmp_path, "banks:\n  b1:\n    profile: nonexistent\n"))


@pytest.mark.parametrize(
    "text",
    [
        "signal_quality:\n  dedup:\n    action: drop\n",
        "signal_quality:\n  noisy_bank:\n    action: explode\n",
        "banks:\n  b1:\n    signal_quality:\n      dedup:\n        action: drop\n",
    ],
)
def test_invalid_signal_quality_actions_are_rejected(tmp_path: Path, text: str) -> None:
    with pytest.raises(ConfigError, match="must be one of"):
        load_config(_config_path(tmp_path, text))


@pytest.mark.parametrize("action", ["skip_chunk", "skip", "warn", "update"])
def test_every_dedup_action_loads(tmp_path: Path, action: str) -> None:
    config = load_config(_config_path(tmp_path, f"signal_quality:\n  dedup:\n    action: {action}\n"))
    assert config.signal_quality.dedup.action == action


# ── Enforcement ──


@pytest.mark.asyncio
async def test_pii_barrier_is_per_bank(tmp_path: Path) -> None:
    brain = _brain(
        tmp_path,
        "barriers:\n  pii:\n    mode: disabled\n"
        "banks:\n  hr:\n    barriers:\n      pii:\n        mode: regex\n        action: redact\n",
    )
    seen: list[dict] = []

    async def on_pii(event):
        seen.append(dict(event.data))

    brain.register_hook("on_pii_detected", on_pii)
    await brain.retain("Reach me at user@example.com", bank_id="hr")
    await brain.retain("Reach me at user@example.com", bank_id="eng")
    memories = brain._engine_provider._memories
    assert "user@example.com" not in memories["hr"][0].text
    assert "user@example.com" in memories["eng"][0].text
    assert [e["action"] for e in seen] == ["redact"], "the hr bank's action, and only for hr"


@pytest.mark.asyncio
async def test_content_validation_is_per_bank(tmp_path: Path) -> None:
    brain = _brain(tmp_path, "banks:\n  tiny:\n    barriers:\n      validation:\n        max_content_length: 10\n")
    assert (await brain.retain("well over ten characters", bank_id="tiny")).stored is False
    assert (await brain.retain("well over ten characters", bank_id="other")).stored is True


@pytest.mark.asyncio
async def test_metadata_sanitizer_is_per_bank(tmp_path: Path) -> None:
    brain = _brain(
        tmp_path,
        "banks:\n  locked:\n    barriers:\n      metadata:\n        blocked_keys: [ticket]\n",
    )
    await brain.retain("The deploy is on Tuesday.", bank_id="locked", metadata={"ticket": "T-1"})
    await brain.retain("The deploy is on Tuesday.", bank_id="open", metadata={"ticket": "T-1"})
    memories = brain._engine_provider._memories
    assert "ticket" not in (memories["locked"][0].metadata or {})
    assert (memories["open"][0].metadata or {}).get("ticket") == "T-1"


@pytest.mark.asyncio
async def test_retain_size_cap_is_per_bank(tmp_path: Path) -> None:
    brain = _brain(tmp_path, "banks:\n  small:\n    homeostasis:\n      retain_max_content_bytes: 8\n")
    small = await brain.retain("twelve bytes", bank_id="small")
    assert small.stored is False and "maximum size" in (small.error or "")
    assert (await brain.retain("twelve bytes", bank_id="big")).stored is True


@pytest.mark.asyncio
async def test_rate_limit_is_per_bank(tmp_path: Path) -> None:
    brain = _brain(tmp_path, "banks:\n  slow:\n    homeostasis:\n      rate_limits:\n        retain_per_minute: 1\n")
    await brain.retain("first note here", bank_id="slow")
    with pytest.raises(RateLimited):
        await brain.retain("second note here", bank_id="slow")
    for i in range(3):
        assert (await brain.retain(f"note number {i} here", bank_id="fast")).stored is True


@pytest.mark.asyncio
async def test_quota_is_per_bank(tmp_path: Path) -> None:
    brain = _brain(tmp_path, "banks:\n  capped:\n    homeostasis:\n      quotas:\n        retain_per_day: 1\n")
    await brain.retain("first note here", bank_id="capped")
    with pytest.raises(RateLimited):
        await brain.retain("second note here", bank_id="capped")
    assert (await brain.retain("second note here", bank_id="free")).stored is True


def test_banks_without_policy_overrides_share_the_top_level_policy(tmp_path: Path) -> None:
    brain = _brain(
        tmp_path,
        "banks:\n  a:\n    access: []\n  b:\n    signal_quality:\n      dedup:\n        enabled: false\n"
        "  c:\n    homeostasis:\n      rate_limits:\n        retain_per_minute: 5\n",
    )
    policy = brain._policy
    assert set(policy._bank_policies) == {"c"}
    assert policy._for("a") is policy._default_policy
    assert policy._for(None) is policy._default_policy
    assert policy._for("c").rate_limiters["retain"]._max_per_minute == 5


def _spy_recall_budgets(brain: Astrocyte) -> list[int | None]:
    budgets: list[int | None] = []
    engine = brain._engine_provider
    original = engine.recall

    async def recall(request):
        budgets.append(request.max_tokens)
        return await original(request)

    engine.recall = recall
    return budgets


@pytest.mark.asyncio
async def test_recall_budget_is_per_bank_and_the_strictest_across_banks(tmp_path: Path) -> None:
    brain = _brain(
        tmp_path,
        "homeostasis:\n  recall_max_tokens: 1000\nbanks:\n  tight:\n    homeostasis:\n      recall_max_tokens: 50\n",
    )
    budgets = _spy_recall_budgets(brain)
    await brain.recall("anything", bank_id="tight")
    await brain.recall("anything", bank_id="other")
    await brain.recall("anything", bank_id="tight", max_tokens=500)
    assert budgets == [50, 1000, 500], "an explicit max_tokens still wins"

    # Multi-bank recall trims the merged result once, after fusion.
    merged_budgets: list[int | None] = []
    original = brain._multi_bank.recall

    async def multi_recall(query, bank_ids, max_results, max_tokens, *rest):
        merged_budgets.append(max_tokens)
        return await original(query, bank_ids, max_results, max_tokens, *rest)

    brain._multi_bank.recall = multi_recall
    await brain.recall("anything", banks=["other", "tight"])
    assert merged_budgets == [50], "the strictest budget of the banks recalled"


def test_token_budget_is_none_when_no_bank_sets_one(tmp_path: Path) -> None:
    brain = _brain(tmp_path, "banks:\n  b1:\n    homeostasis:\n      reflect_max_tokens: 64\n")
    assert brain._policy.token_budget(["b2"], "recall") is None
    assert brain._policy.token_budget(["b1", "b2"], "reflect") == 64


@pytest.mark.asyncio
async def test_reflect_uses_the_banks_budget(tmp_path: Path) -> None:
    brain = _brain(tmp_path, "banks:\n  b1:\n    homeostasis:\n      reflect_max_tokens: 64\n")
    seen: list[int | None] = []
    engine = brain._engine_provider
    original = engine.reflect

    async def reflect(request):
        seen.append(request.max_tokens)
        return await original(request)

    engine.reflect = reflect
    await brain.reflect("what happened?", bank_id="b1")
    assert seen == [64]


@pytest.mark.asyncio
async def test_llm_pii_mode_is_resolved_per_bank(tmp_path: Path) -> None:
    """A bank whose barriers use an LLM PII mode takes the async scan path;
    a bank on regex stays on the sync one."""
    brain = _brain(
        tmp_path,
        "barriers:\n  pii:\n    mode: regex\n"
        "banks:\n  vip:\n    barriers:\n      pii:\n        mode: rules_then_llm\n",
    )
    calls: list[str] = []
    vip_scanner = brain._policy._for("vip").pii_scanner

    async def apply_async(content):
        calls.append("async")
        return content, []

    vip_scanner.apply_async = apply_async
    await brain.retain("The deploy is on Tuesday.", bank_id="vip")
    await brain.retain("The deploy is on Tuesday.", bank_id="other")
    assert calls == ["async"]
