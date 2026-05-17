"""Unit tests for ``astrocyte.pipeline.rerank_boosts`` (Gap 3, M14)."""

from __future__ import annotations

from datetime import datetime, timezone

from astrocyte.pipeline.rerank_boosts import (
    RerankBoostConfig,
    apply_boosts,
    compute_boost_multiplier,
    proof_count_score,
    recency_score,
    temporal_band_score,
)
from astrocyte.pipeline.section_recall import FusedHit
from astrocyte.types import PageIndexSection

UTC = timezone.utc
NOW = datetime(2026, 5, 16, tzinfo=UTC)


def _section(
    *,
    document_id: str = "d",
    line_num: int = 1,
    session_date: datetime | None = None,
    occurred_start: datetime | None = None,
    occurred_end: datetime | None = None,
) -> PageIndexSection:
    return PageIndexSection(
        document_id=document_id,
        line_num=line_num,
        node_id=str(line_num).zfill(4),
        title="t",
        session_date=session_date,
        occurred_start=occurred_start,
        occurred_end=occurred_end,
    )


class TestRecencyScore:
    def test_today_is_one(self):
        assert recency_score(NOW, NOW) == 1.0

    def test_one_year_ago_is_zero(self):
        a_year_ago = datetime(2025, 5, 16, tzinfo=UTC)
        # 365 days back → 1 - 365/365 = 0 → clamped to floor 0.1
        assert recency_score(a_year_ago, NOW) == 0.1

    def test_half_year_ago_is_half(self):
        half_year = datetime(2025, 11, 16, tzinfo=UTC)
        # ~181 days → 1 - 181/365 ≈ 0.50
        score = recency_score(half_year, NOW)
        assert 0.49 <= score <= 0.51

    def test_no_date_is_neutral(self):
        assert recency_score(None, NOW) == 0.5

    def test_naive_datetime_handled(self):
        naive = datetime(2026, 5, 16)  # no tzinfo
        assert recency_score(naive, NOW) == 1.0


class TestTemporalBand:
    def test_no_query_range_is_neutral(self):
        s = _section(session_date=NOW)
        assert temporal_band_score(s, None) == 0.5

    def test_no_section_date_is_neutral(self):
        s = _section()  # no dates
        range_ = (datetime(2026, 5, 1, tzinfo=UTC), datetime(2026, 5, 31, tzinfo=UTC))
        assert temporal_band_score(s, range_) == 0.5

    def test_overlap_returns_one(self):
        s = _section(session_date=NOW)
        range_ = (datetime(2026, 5, 1, tzinfo=UTC), datetime(2026, 5, 31, tzinfo=UTC))
        assert temporal_band_score(s, range_) == 1.0

    def test_disjoint_returns_penalty(self):
        s = _section(session_date=NOW)
        range_ = (datetime(2025, 1, 1, tzinfo=UTC), datetime(2025, 1, 31, tzinfo=UTC))
        assert temporal_band_score(s, range_) == 0.2

    def test_event_range_used_over_session_date(self):
        # session_date is OUTSIDE the query range, but event range is INSIDE.
        s = _section(
            session_date=datetime(2025, 1, 1, tzinfo=UTC),
            occurred_start=datetime(2026, 5, 10, tzinfo=UTC),
            occurred_end=datetime(2026, 5, 12, tzinfo=UTC),
        )
        range_ = (datetime(2026, 5, 1, tzinfo=UTC), datetime(2026, 5, 31, tzinfo=UTC))
        assert temporal_band_score(s, range_) == 1.0


class TestProofCount:
    def test_none_is_neutral(self):
        assert proof_count_score(None) == 0.5

    def test_zero_is_neutral(self):
        assert proof_count_score(0) == 0.5

    def test_one_is_neutral(self):
        assert proof_count_score(1) == 0.5

    def test_growth_is_log(self):
        s1 = proof_count_score(2)
        s10 = proof_count_score(10)
        s100 = proof_count_score(100)
        assert s1 < s10 < s100
        assert s100 <= 1.0


class TestApplyBoosts:
    def test_disabled_passes_through(self):
        h = FusedHit(document_id="d", line_num=1, rrf_score=0.5)
        out = apply_boosts(
            [h],
            {},
            config=RerankBoostConfig(enabled=False),
        )
        assert out == [h]

    def test_empty_input(self):
        assert apply_boosts([], {}, config=RerankBoostConfig()) == []

    def test_multiplicative_composition(self):
        # Recent + overlapping + multi-strategy section beats a stale
        # section with the same CE score.
        s_recent = _section(line_num=1, session_date=NOW)
        s_stale = _section(
            line_num=2,
            session_date=datetime(2022, 1, 1, tzinfo=UTC),
        )
        hits = [
            FusedHit(document_id="d", line_num=1, rrf_score=0.5),
            FusedHit(document_id="d", line_num=2, rrf_score=0.5),
        ]
        sbk = {("d", 1): s_recent, ("d", 2): s_stale}
        out = apply_boosts(hits, sbk, now=NOW)
        # Recent section ranks first after boost.
        assert out[0].line_num == 1
        assert out[1].line_num == 2
        # Recent score > original; stale score < original.
        assert out[0].rrf_score > 0.5
        assert out[1].rrf_score < 0.5

    def test_temporal_band_breaks_tie(self):
        # Two equally-recent sections; only one overlaps the query.
        same_day = datetime(2026, 5, 10, tzinfo=UTC)
        s_in = _section(line_num=1, session_date=same_day)
        s_out = _section(line_num=2, session_date=same_day)
        hits = [
            FusedHit(document_id="d", line_num=1, rrf_score=0.5),
            FusedHit(document_id="d", line_num=2, rrf_score=0.5),
        ]
        sbk = {("d", 1): s_in, ("d", 2): s_out}
        # Tight query range that only intersects s_in (which we put on same_day).
        # Wait: both have same session_date so both overlap. Move s_out off-band:
        s_out_off = _section(
            line_num=2,
            session_date=datetime(2025, 1, 1, tzinfo=UTC),
        )
        sbk[("d", 2)] = s_out_off
        range_ = (datetime(2026, 5, 1, tzinfo=UTC), datetime(2026, 5, 31, tzinfo=UTC))
        out = apply_boosts(hits, sbk, now=NOW, query_range=range_)
        assert out[0].line_num == 1

    def test_preserves_per_strategy_rank(self):
        h = FusedHit(
            document_id="d",
            line_num=1,
            rrf_score=0.5,
            per_strategy_rank={"semantic": 1, "keyword": 3},
        )
        out = apply_boosts(
            [h],
            {("d", 1): _section(session_date=NOW)},
            now=NOW,
        )
        assert out[0].per_strategy_rank == {"semantic": 1, "keyword": 3}

    def test_no_section_no_dates_collapses_to_identity(self):
        # Section with no dates → recency=0.5 + temporal=0.5 + proof=0.5
        # All boosts collapse to 1.0 → final equals CE.
        s = _section(line_num=1)
        h = FusedHit(document_id="d", line_num=1, rrf_score=0.5)
        out = apply_boosts([h], {("d", 1): s}, now=NOW)
        assert abs(out[0].rrf_score - 0.5) < 1e-9


class TestComputeBoostMultiplier:
    """M19 — compute_boost_multiplier helper used by the bench's unified
    fact + section + wiki rerank pool. Extracted from apply_boosts so
    callers outside section_rerank can apply the same Hindsight-parity
    bounded boosts."""

    def test_disabled_returns_unity(self):
        s = _section(line_num=1, session_date=NOW)
        config = RerankBoostConfig(enabled=False)
        m = compute_boost_multiplier(s, now=NOW, config=config)
        assert m == 1.0

    def test_none_section_no_query_range_collapses_to_unity(self):
        """Wiki / sectionless candidates with no query date band get a
        neutral multiplier — they pass through unchanged."""
        m = compute_boost_multiplier(None, query_range=None, now=NOW)
        assert abs(m - 1.0) < 1e-9

    def test_recent_section_gets_positive_boost(self):
        # Section dated today → max recency boost
        s = _section(line_num=1, session_date=NOW)
        m = compute_boost_multiplier(s, now=NOW)
        # recency ≈ 1.0, others neutral 0.5 → only recency boost fires
        # recency_boost = 1 + 0.2 * (1.0 - 0.5) = 1.10
        assert m > 1.05  # well above identity
        assert m < 1.15  # bounded — recency alpha is 0.2

    def test_stale_section_gets_negative_boost(self):
        # 365 days ago → recency floor 0.1
        from datetime import timedelta
        s = _section(line_num=1, session_date=NOW - timedelta(days=365))
        m = compute_boost_multiplier(s, now=NOW)
        # recency ≈ 0.0 → 0.1 floor; boost = 1 + 0.2 * (0.1 - 0.5) = 0.92
        assert m < 0.95
        assert m > 0.85  # bounded

    def test_temporal_band_overlap_lifts_score(self):
        from datetime import timedelta
        s = _section(
            line_num=1,
            session_date=NOW,
            occurred_start=NOW,
            occurred_end=NOW + timedelta(days=1),
        )
        # Query band overlaps the section's range
        q_range = (NOW - timedelta(days=2), NOW + timedelta(days=2))
        m_overlap = compute_boost_multiplier(s, query_range=q_range, now=NOW)

        # Disjoint query band → penalty (0.2 vs 0.5 neutral)
        q_disjoint = (NOW + timedelta(days=30), NOW + timedelta(days=40))
        m_disjoint = compute_boost_multiplier(s, query_range=q_disjoint, now=NOW)

        assert m_overlap > m_disjoint  # overlap wins

    def test_section_date_override_used_for_recency(self):
        """Bench-callsite use: fact-grain candidates pass
        fact.occurred_start as override so recency reflects the FACT's
        date, not the parent section's."""
        from datetime import timedelta
        # Section is stale, but the fact happened today
        s = _section(line_num=1, session_date=NOW - timedelta(days=365))
        fact_date = NOW
        m_override = compute_boost_multiplier(
            s, now=NOW, section_date_override=fact_date,
        )
        m_no_override = compute_boost_multiplier(s, now=NOW)
        # Override should produce a HIGHER recency boost (fact date is fresher)
        assert m_override > m_no_override

    def test_proof_count_boost(self):
        s = _section(line_num=1)
        # No proof count → neutral
        m_no_count = compute_boost_multiplier(s, now=NOW)
        # High proof count → modest positive boost
        m_high_count = compute_boost_multiplier(s, proof_count=100, now=NOW)
        assert m_high_count > m_no_count

    def test_duck_typed_section_without_date_attrs_collapses_to_neutral(self):
        """Defensive: when the bench's unified rerank pool stores a
        ``FusedHit`` (from section_recall — has document_id/line_num/
        rrf_score but NO occurred_start/session_date) in c["section"],
        compute_boost_multiplier must not crash. It should fall back
        to the neutral multiplier (no boost applied).

        Regression test for the M19 C0 wiring bug where the bench's
        section-grain candidates use FusedHit objects, not PageIndexSection.
        """
        from astrocyte.pipeline.section_recall import FusedHit
        fused = FusedHit(document_id="d", line_num=1, rrf_score=0.5)
        # Should not raise AttributeError on occurred_start / session_date
        m = compute_boost_multiplier(fused, now=NOW)  # type: ignore[arg-type]
        # No date info, no query range, no proof count → all signals neutral
        assert m == 1.0

    def test_all_signals_compose_multiplicatively(self):
        """End-to-end: a recent + temporally-matched + well-evidenced
        section gets ALL boosts; the resulting multiplier should be the
        product of the individual boosts, capped by the alpha bounds."""
        from datetime import timedelta
        s = _section(
            line_num=1,
            session_date=NOW,
            occurred_start=NOW,
            occurred_end=NOW,
        )
        q_range = (NOW - timedelta(days=1), NOW + timedelta(days=1))
        m = compute_boost_multiplier(s, query_range=q_range, proof_count=100, now=NOW)
        # All boosts at or near max: (1.10) × (1.10) × (~1.05) ≈ 1.27
        # Bounded; should be in (1.15, 1.30)
        assert 1.15 < m < 1.30
