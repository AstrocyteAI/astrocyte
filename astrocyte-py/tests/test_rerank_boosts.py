"""Unit tests for ``astrocyte.pipeline.rerank_boosts`` (Gap 3, M14)."""

from __future__ import annotations

from datetime import datetime, timezone

from astrocyte.pipeline.rerank_boosts import (
    RerankBoostConfig,
    apply_boosts,
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
