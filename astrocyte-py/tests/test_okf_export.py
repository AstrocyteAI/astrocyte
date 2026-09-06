"""OKF v0.2 bundle export.

Conformance points asserted here come from
``GoogleCloudPlatform/knowledge-catalog/okf/SPEC.md`` v0.2, cited per test.
The negative assertions matter as much as the positive ones: we deliberately
omit ``verified`` / ``status`` / ``stale_after`` because Astrocyte persists no
such data, and emitting them would fabricate trust and lifecycle signals.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import yaml

from astrocyte.okf import (
    OKF_VERSION,
    build_bundle,
    build_frontmatter,
    concept_path,
    export_wiki_bundle,
    render_concept,
)
from astrocyte.types import WikiPage


def make_page(
    page_id="topic:incident-response",
    *,
    kind="topic",
    title="Incident Response",
    content="# Incident Response\n\nEscalate within 15 minutes.",
    tags=None,
    source_ids=None,
    revision=1,
    bank_id="bank-1",
    scope="ops",
    revised_at=datetime(2026, 9, 6, 12, 30, tzinfo=UTC),
) -> WikiPage:
    return WikiPage(
        page_id=page_id,
        bank_id=bank_id,
        kind=kind,
        title=title,
        content=content,
        scope=scope,
        source_ids=list(source_ids or []),
        cross_links=[],
        revision=revision,
        revised_at=revised_at,
        tags=tags,
    )


def split_frontmatter(text: str) -> tuple[dict, str]:
    assert text.startswith("---\n"), "concept must open with a frontmatter fence"
    _, fm, body = text.split("---\n", 2)
    return yaml.safe_load(fm), body


class TestConceptPath:
    def test_namespaced_id_becomes_hierarchy(self):
        assert concept_path(make_page("topic:incident-response")) == "topic/incident-response.md"
        assert concept_path(make_page("entity:alice")) == "entity/alice.md"

    def test_multi_segment_id_nests(self):
        assert concept_path(make_page("obs:doc123:my-slug")) == "obs/doc123/my-slug.md"

    def test_unnamespaced_id_sits_at_root(self):
        assert concept_path(make_page("standalone")) == "standalone.md"

    @pytest.mark.parametrize("hostile", ["../../etc/passwd", "..", "../..", "/etc/passwd"])
    def test_traversal_is_neutralised(self, hostile):
        path = concept_path(make_page(hostile))
        assert ".." not in Path_parts(path)
        assert not path.startswith("/")

    @pytest.mark.parametrize("reserved", ["index", "log"])
    def test_reserved_filenames_are_avoided(self, reserved):
        # SPEC 3.1: index.md and log.md MUST NOT be concept documents.
        assert concept_path(make_page(reserved)) != f"{reserved}.md"

    def test_empty_id_still_yields_a_path(self):
        assert concept_path(make_page("")).endswith(".md")


def Path_parts(path: str) -> list[str]:
    return path.split("/")


class TestFrontmatter:
    def test_type_is_always_present(self):
        # SPEC 4.1: `type` is the only required key.
        for kind, expected in [("topic", "Topic"), ("entity", "Entity"), ("concept", "Concept")]:
            fm = build_frontmatter(make_page(kind=kind), producer="astrocyte/0.1")
            assert fm["type"] == expected

    def test_unknown_kind_falls_back_to_concept(self):
        fm = build_frontmatter(make_page(kind="wat"), producer="astrocyte/0.1")
        assert fm["type"] == "Concept"

    def test_generated_carries_producer_and_timestamp(self):
        # SPEC 5.2: `by` is REQUIRED within `generated`; SPEC 7: <producer>/<version>.
        fm = build_frontmatter(make_page(), producer="astrocyte/0.15.1")
        assert fm["generated"]["by"] == "astrocyte/0.15.1"
        assert fm["generated"]["at"].startswith("2026-09-06T12:30")

    def test_generated_omits_at_when_unknown_but_keeps_by(self):
        # `generated` without `by` would be invalid, so `by` must survive alone.
        fm = build_frontmatter(make_page(revised_at=None), producer="astrocyte/0.1")
        assert fm["generated"] == {"by": "astrocyte/0.1"}

    def test_sources_get_a_required_resource(self):
        # SPEC 5.1: `resource` is REQUIRED within each sources entry.
        fm = build_frontmatter(make_page(source_ids=["mem1", "mem2"]), producer="p/1")
        assert [s["id"] for s in fm["sources"]] == ["mem1", "mem2"]
        for entry in fm["sources"]:
            assert entry["resource"].startswith("astrocyte://bank/bank-1/memory/")

    def test_sources_omitted_when_empty(self):
        assert "sources" not in build_frontmatter(make_page(), producer="p/1")

    def test_tags_emitted_only_when_present(self):
        assert "tags" not in build_frontmatter(make_page(tags=None), producer="p/1")
        fm = build_frontmatter(make_page(tags=["ops", "runbook"]), producer="p/1")
        assert fm["tags"] == ["ops", "runbook"]

    @pytest.mark.parametrize("absent", ["verified", "status", "stale_after", "description"])
    def test_unbacked_fields_are_never_invented(self, absent):
        # Astrocyte persists no verification, lifecycle, or summary data.
        # Absent `verified` => unverified tier (SPEC 5.3); absent `status`
        # => stable (SPEC 5.4). Emitting placeholders would fabricate trust.
        fm = build_frontmatter(make_page(tags=["x"], source_ids=["m1"]), producer="p/1")
        assert absent not in fm


class TestRenderConcept:
    def test_roundtrips_as_yaml_plus_body(self):
        text = render_concept(make_page(), producer="astrocyte/0.1")
        fm, body = split_frontmatter(text)
        assert fm["title"] == "Incident Response"
        assert "Escalate within 15 minutes." in body

    def test_yaml_special_characters_are_escaped(self):
        page = make_page(title='Title: with "quotes" and #hash', content="body")
        fm, _ = split_frontmatter(render_concept(page, producer="p/1"))
        assert fm["title"] == 'Title: with "quotes" and #hash'


class TestBundle:
    def test_root_index_declares_okf_version(self):
        # SPEC 8: only a root index may carry frontmatter, and only okf_version.
        files = {f.path: f.content for f in build_bundle([make_page()], bank_id="bank-1")}
        assert f"okf_version: {OKF_VERSION}" in files["index.md"]

    def test_subdirectory_index_has_no_frontmatter(self):
        files = {f.path: f.content for f in build_bundle([make_page()], bank_id="bank-1")}
        assert not files["topic/index.md"].startswith("---")

    def test_log_groups_by_iso_date_and_marks_creation(self):
        # SPEC 9: date headings MUST be ISO 8601 YYYY-MM-DD.
        pages = [
            make_page("topic:a", revision=1),
            make_page("topic:b", revision=3, revised_at=datetime(2026, 9, 4, tzinfo=UTC)),
        ]
        log = {f.path: f.content for f in build_bundle(pages, bank_id="b")}["log.md"]
        assert "## 2026-09-06" in log and "## 2026-09-04" in log
        assert log.index("2026-09-06") < log.index("2026-09-04"), "newest first"
        assert "**Creation**" in log and "**Update**" in log

    def test_colliding_slugs_do_not_overwrite(self):
        pages = [make_page("topic:Alice", title="A"), make_page("topic:alice", title="B")]
        files = build_bundle(pages, bank_id="b")
        concepts = [f for f in files if f.path == "topic/alice.md"]
        assert len(concepts) == 1
        assert "title: A" in concepts[0].content, "first page wins; second is skipped"

    def test_build_bundle_performs_no_io(self, tmp_path):
        build_bundle([make_page()], bank_id="b")
        assert list(tmp_path.iterdir()) == []


class TestExport:
    def test_writes_a_containable_bundle(self, tmp_path):
        pages = [make_page("topic:incident-response"), make_page("entity:alice", kind="entity")]
        result = export_wiki_bundle(pages, bank_id="bank-1", path=tmp_path / "bundle", allowed_roots=[tmp_path])
        assert result.concept_count == 2
        root = tmp_path / "bundle"
        assert (root / "topic/incident-response.md").exists()
        assert (root / "entity/alice.md").exists()
        assert (root / "index.md").exists()
        assert (root / "log.md").exists()

    def test_refuses_to_escape_allowed_roots(self, tmp_path):
        with pytest.raises(ValueError):
            export_wiki_bundle(
                [make_page()],
                bank_id="b",
                path=tmp_path / ".." / "escape",
                allowed_roots=[tmp_path],
            )

    def test_hostile_page_id_stays_inside_the_bundle(self, tmp_path):
        root = tmp_path / "bundle"
        export_wiki_bundle(
            [make_page("../../../../etc/passwd")],
            bank_id="b",
            path=root,
            allowed_roots=[tmp_path],
        )
        written = [p for p in root.rglob("*.md")]
        assert written, "expected the concept to be written somewhere inside the bundle"
        for p in written:
            assert p.resolve().is_relative_to(root.resolve())

    def test_requires_containment_by_default(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ASTROCYTE_PORTABILITY_ROOTS", raising=False)
        with pytest.raises(ValueError):
            export_wiki_bundle([make_page()], bank_id="b", path=tmp_path / "x")

    def test_empty_bundle_still_emits_an_index(self, tmp_path):
        result = export_wiki_bundle([], bank_id="empty", path=tmp_path / "b", allowed_roots=[tmp_path])
        assert result.concept_count == 0
        assert (tmp_path / "b" / "index.md").exists()
        assert not (tmp_path / "b" / "log.md").exists()


class TestCrossLinks:
    """SPEC 6.1: the concept graph is markdown links in the body."""

    def test_cross_links_render_as_bundle_relative_links(self):
        page = make_page("topic:a")
        page.cross_links = ["entity:alice", "topic:incident-response"]
        text = render_concept(page, producer="p/1")
        assert "## Related" in text
        assert "(/entity/alice.md)" in text
        assert "(/topic/incident-response.md)" in text

    def test_no_related_section_without_links(self):
        assert "## Related" not in render_concept(make_page(), producer="p/1")

    def test_duplicate_targets_collapse(self):
        page = make_page("topic:a")
        page.cross_links = ["entity:alice", "entity:alice"]
        assert render_concept(page, producer="p/1").count("(/entity/alice.md)") == 1

    def test_body_is_preserved_alongside_links(self):
        page = make_page("topic:a", content="Original body text.")
        page.cross_links = ["entity:alice"]
        text = render_concept(page, producer="p/1")
        assert "Original body text." in text and "## Related" in text


class TestBrainExportOkfBundle:
    """`Astrocyte.export_okf_bundle` — the wiring that makes the exporter usable."""

    def _brain(self):
        from astrocyte import Astrocyte
        from astrocyte.testing.in_memory import InMemoryWikiStore

        brain = Astrocyte.from_config_dict({"banks": {"eng": {}}})
        store = InMemoryWikiStore()
        brain.set_wiki_store(store)
        return brain, store

    async def test_requires_a_wiki_store(self, tmp_path):
        from astrocyte import Astrocyte
        from astrocyte.errors import ConfigError

        brain = Astrocyte.from_config_dict({"banks": {"eng": {}}})
        with pytest.raises(ConfigError, match="WikiStore"):
            await brain.export_okf_bundle("eng", str(tmp_path / "b"), allowed_roots=[str(tmp_path)])

    async def test_exports_pages_from_the_bank(self, tmp_path):
        brain, store = self._brain()
        await store.upsert_page(make_page("topic:incident-response"), "eng")
        await store.upsert_page(make_page("entity:alice", kind="entity", title="Alice"), "eng")

        result = await brain.export_okf_bundle("eng", str(tmp_path / "b"), allowed_roots=[str(tmp_path)])

        assert result.concept_count == 2
        root = tmp_path / "b"
        assert (root / "topic/incident-response.md").exists()
        assert (root / "entity/alice.md").exists()
        assert (root / "index.md").exists()

    async def test_kind_filter_narrows_the_bundle(self, tmp_path):
        brain, store = self._brain()
        await store.upsert_page(make_page("topic:a"), "eng")
        await store.upsert_page(make_page("entity:alice", kind="entity"), "eng")

        result = await brain.export_okf_bundle("eng", str(tmp_path / "b"), kind="entity", allowed_roots=[str(tmp_path)])

        assert result.concept_count == 1
        assert (tmp_path / "b" / "entity/alice.md").exists()
        assert not (tmp_path / "b" / "topic/a.md").exists()

    async def test_other_banks_are_not_exported(self, tmp_path):
        brain, store = self._brain()
        await store.upsert_page(make_page("topic:mine"), "eng")
        await store.upsert_page(make_page("topic:theirs"), "other")

        result = await brain.export_okf_bundle("eng", str(tmp_path / "b"), allowed_roots=[str(tmp_path)])

        assert result.concept_count == 1
        assert (tmp_path / "b" / "topic/mine.md").exists()
        assert not (tmp_path / "b" / "topic/theirs.md").exists()

    async def test_containment_is_enforced_through_the_brain(self, tmp_path):
        brain, store = self._brain()
        await store.upsert_page(make_page(), "eng")
        with pytest.raises(ValueError):
            await brain.export_okf_bundle("eng", str(tmp_path / ".." / "escape"), allowed_roots=[str(tmp_path)])

    async def test_empty_bank_exports_an_empty_bundle(self, tmp_path):
        brain, _ = self._brain()
        result = await brain.export_okf_bundle("eng", str(tmp_path / "b"), allowed_roots=[str(tmp_path)])
        assert result.concept_count == 0
        assert (tmp_path / "b" / "index.md").exists()


def make_item(
    mid="m1",
    *,
    text="Server went down at 2am.",
    bank_id="bank-1",
    fact_type="experience",
    tags=None,
    created_at="2026-01-01T00:00:00+00:00",
    lifecycle_tags=None,
    retained_at=datetime(2026, 9, 6, tzinfo=UTC),
    occurred_at=None,
    chunk_id=None,
    memory_layer="fact",
):
    from astrocyte.types import VectorItem

    meta = {}
    if created_at:
        meta["_created_at"] = created_at
    if lifecycle_tags:
        meta["_tags"] = ",".join(lifecycle_tags)
    return VectorItem(
        id=mid,
        bank_id=bank_id,
        vector=[0.0],
        text=text,
        metadata=meta or None,
        tags=tags,
        fact_type=fact_type,
        occurred_at=occurred_at,
        memory_layer=memory_layer,
        retained_at=retained_at,
        chunk_id=chunk_id,
    )


class _Ttl:
    def __init__(self, delete_after_days=365, exempt_tags=None):
        self.delete_after_days = delete_after_days
        self.archive_after_days = 90
        self.exempt_tags = exempt_tags


class _Lifecycle:
    def __init__(self, enabled=True, **kw):
        self.enabled = enabled
        self.ttl = _Ttl(**kw)


class TestStaleAfter:
    """SPEC 5.5: stale_after is an absolute instant, not a TTL."""

    def test_derived_from_delete_threshold(self):
        from astrocyte.okf import stale_after_for

        got = stale_after_for(make_item(), _Lifecycle())
        assert got is not None and got.startswith("2027-01-01")

    def test_omitted_when_lifecycle_disabled(self):
        from astrocyte.okf import stale_after_for

        assert stale_after_for(make_item(), _Lifecycle(enabled=False)) is None

    def test_omitted_for_exempt_tags(self):
        from astrocyte.okf import stale_after_for

        item = make_item(lifecycle_tags=["legal", "keep"])
        assert stale_after_for(item, _Lifecycle(exempt_tags=["legal"])) is None

    def test_omitted_when_creation_unknown(self):
        from astrocyte.okf import stale_after_for

        assert stale_after_for(make_item(created_at=None), _Lifecycle()) is None

    def test_uses_lifecycle_created_at_not_retained_at(self):
        # run_lifecycle keys off metadata["_created_at"]; publishing an expiry
        # from retained_at would advertise a date we would not delete on.
        from astrocyte.okf import stale_after_for

        item = make_item(created_at="2020-06-15T00:00:00+00:00", retained_at=datetime(2026, 9, 6, tzinfo=UTC))
        assert stale_after_for(item, _Lifecycle()).startswith("2021-06-15")

    def test_malformed_created_at_is_not_fatal(self):
        from astrocyte.okf import stale_after_for

        assert stale_after_for(make_item(created_at="not-a-date"), _Lifecycle()) is None

    def test_ignores_archive_threshold(self):
        # archive_after_days moves with last_recalled_at, so it must not drive
        # a published absolute instant.
        from astrocyte.okf import stale_after_for

        got = stale_after_for(make_item(), _Lifecycle(delete_after_days=365))
        assert got.startswith("2027-01-01"), "must use delete (365d), not archive (90d)"


class TestMemoryConcepts:
    def test_memory_path_groups_by_fact_type(self):
        from astrocyte.okf import memory_concept_path

        assert memory_concept_path(make_item("m1", fact_type="experience")) == "memory/experience/m1.md"

    def test_memory_path_falls_back_to_layer(self):
        from astrocyte.okf import memory_concept_path

        item = make_item("m2", fact_type=None, memory_layer="observation")
        assert memory_concept_path(item) == "memory/observation/m2.md"

    def test_type_derives_from_fact_type(self):
        from astrocyte.okf import build_memory_frontmatter

        fm = build_memory_frontmatter(make_item(fact_type="assistant_statement"), producer="p/1")
        assert fm["type"] == "Assistant Statement"

    def test_occurred_at_is_preserved_as_an_extension(self):
        # Bitemporality: OKF has no second time axis, and producers MAY add keys.
        from astrocyte.okf import build_memory_frontmatter

        item = make_item(occurred_at=datetime(2026, 1, 2, tzinfo=UTC))
        fm = build_memory_frontmatter(item, producer="p/1")
        assert fm["occurred_at"].startswith("2026-01-02")
        assert fm["generated"]["at"].startswith("2026-09-06"), "system time stays distinct"

    def test_chunk_id_becomes_a_source(self):
        from astrocyte.okf import build_memory_frontmatter

        fm = build_memory_frontmatter(make_item(chunk_id="c9"), producer="p/1")
        assert fm["sources"][0]["id"] == "c9"
        assert fm["sources"][0]["resource"].endswith("/chunk/c9")

    def test_memories_land_in_the_bundle_with_expiry(self):
        from astrocyte.okf import build_bundle

        files = {
            f.path: f.content for f in build_bundle([], bank_id="b", memories=[make_item()], lifecycle=_Lifecycle())
        }
        assert "memory/experience/m1.md" in files
        assert "stale_after: '2027-01-01" in files["memory/experience/m1.md"]

    def test_no_expiry_emitted_without_lifecycle(self):
        from astrocyte.okf import build_bundle

        files = {f.path: f.content for f in build_bundle([], bank_id="b", memories=[make_item()])}
        assert "stale_after" not in files["memory/experience/m1.md"]


class TestActorMapping:
    """SPEC 7: actor convention; SPEC 5.2/5.3: generated.by and trust tiers."""

    @pytest.mark.parametrize(
        ("principal", "expected"),
        [
            ("user:calvin", "human:calvin"),
            ("service:nightly", "process:nightly"),
            ("agent:support-bot-1", "agent:support-bot-1"),
        ],
    )
    def test_principal_maps_to_okf_actor(self, principal, expected):
        from astrocyte.okf import okf_actor

        assert okf_actor(principal) == expected

    @pytest.mark.parametrize("bad", [None, "", "   ", "noprefix", "user:", "  :  "])
    def test_unmappable_principal_yields_none(self, bad):
        # None means "fall back to the producer" rather than emit a bad actor.
        from astrocyte.okf import okf_actor

        assert okf_actor(bad) is None

    def test_human_prefix_is_used_for_users(self):
        # Consumers key trust off `human:` (SPEC 5.3), so a person must map to it.
        from astrocyte.okf import okf_actor

        assert okf_actor("user:calvin").startswith("human:")

    def test_generated_by_credits_the_writing_actor(self):
        from astrocyte.okf import build_memory_frontmatter

        item = make_item()
        item.metadata = {**(item.metadata or {}), "_actor": "user:calvin"}
        fm = build_memory_frontmatter(item, producer="astrocyte/1.0")
        assert fm["generated"]["by"] == "human:calvin"

    def test_producer_is_used_when_no_actor_recorded(self):
        from astrocyte.okf import build_memory_frontmatter

        fm = build_memory_frontmatter(make_item(), producer="astrocyte/1.0")
        assert fm["generated"]["by"] == "astrocyte/1.0"

    @pytest.mark.parametrize("layer", ["observation", "model", "compiled"])
    def test_derived_layers_are_credited_to_the_producer_not_the_human(self, layer):
        # The pipeline wrote these words, not the caller who triggered the write.
        from astrocyte.okf import build_memory_frontmatter

        item = make_item(memory_layer=layer)
        item.metadata = {**(item.metadata or {}), "_actor": "user:calvin"}
        fm = build_memory_frontmatter(item, producer="astrocyte/1.0")
        assert fm["generated"]["by"] == "astrocyte/1.0"

    def test_raw_fact_layer_keeps_the_human(self):
        from astrocyte.okf import build_memory_frontmatter

        item = make_item(memory_layer="fact")
        item.metadata = {**(item.metadata or {}), "_actor": "user:calvin"}
        fm = build_memory_frontmatter(item, producer="astrocyte/1.0")
        assert fm["generated"]["by"] == "human:calvin"


class TestVerified:
    """SPEC 5.2/5.3 — supported, but never fabricated."""

    def test_absent_for_records_written_today(self):
        from astrocyte.okf import build_memory_frontmatter, verified_for

        assert verified_for(make_item()) is None
        assert "verified" not in build_memory_frontmatter(make_item(), producer="p/1")

    def test_events_flow_through_when_present(self):
        from astrocyte.okf import build_memory_frontmatter

        item = make_item()
        item.metadata = {
            **(item.metadata or {}),
            "_verified": [{"by": "user:calvin", "at": "2026-09-07T10:00:00+00:00"}],
        }
        fm = build_memory_frontmatter(item, producer="p/1")
        assert fm["verified"] == [{"by": "human:calvin", "at": "2026-09-07T10:00:00+00:00"}]

    def test_bare_mapping_is_accepted_as_one_event(self):
        # SPEC 5.2 permits a single verifier without the list dash.
        from astrocyte.okf import verified_for

        item = make_item()
        item.metadata = {**(item.metadata or {}), "_verified": {"by": "user:x", "at": "2026-09-07T00:00:00+00:00"}}
        assert verified_for(item) == [{"by": "human:x", "at": "2026-09-07T00:00:00+00:00"}]

    @pytest.mark.parametrize(
        "bad",
        [
            [{"by": "user:calvin"}],
            [{"at": "2026-09-07T00:00:00+00:00"}],
            [{"by": "malformed", "at": "2026-09-07T00:00:00+00:00"}],
            ["not-a-dict"],
            "garbage",
        ],
    )
    def test_incomplete_events_are_dropped_not_guessed(self, bad):
        from astrocyte.okf import verified_for

        item = make_item()
        item.metadata = {**(item.metadata or {}), "_verified": bad}
        assert verified_for(item) is None


class TestActorEndToEnd:
    """retain(context=...) -> persisted metadata -> OKF `generated.by`."""

    async def _brain_with_vectors(self):
        from astrocyte import Astrocyte
        from astrocyte.pipeline.orchestrator import PipelineOrchestrator
        from astrocyte.testing.in_memory import (
            InMemoryVectorStore,
            InMemoryWikiStore,
            MockLLMProvider,
        )

        vs = InMemoryVectorStore()
        brain = Astrocyte.from_config_dict({"banks": {"eng": {}}})
        brain.set_pipeline(PipelineOrchestrator(vector_store=vs, llm_provider=MockLLMProvider()))
        brain.set_wiki_store(InMemoryWikiStore())
        return brain, vs

    async def test_actor_is_persisted_on_retain(self):
        from astrocyte.types import AstrocyteContext

        brain, vs = await self._brain_with_vectors()
        await brain.retain("Alice owns payments.", bank_id="eng", context=AstrocyteContext(principal="user:calvin"))
        items = await vs.list_vectors("eng", offset=0, limit=10)
        assert any((i.metadata or {}).get("_actor") == "user:calvin" for i in items)

    async def test_no_actor_recorded_without_context(self):
        brain, vs = await self._brain_with_vectors()
        await brain.retain("No context here.", bank_id="eng")
        items = await vs.list_vectors("eng", offset=0, limit=10)
        assert all((i.metadata or {}).get("_actor") is None for i in items)

    async def test_actor_reaches_the_exported_bundle(self, tmp_path):
        from astrocyte.types import AstrocyteContext

        brain, _ = await self._brain_with_vectors()
        await brain.retain("Alice owns payments.", bank_id="eng", context=AstrocyteContext(principal="user:calvin"))
        await brain.export_okf_bundle("eng", str(tmp_path / "b"), include_memories=True, allowed_roots=[str(tmp_path)])
        written = list((tmp_path / "b" / "memory").rglob("*.md"))
        assert written, "expected at least one memory concept"
        assert any("by: human:calvin" in p.read_text() for p in written)

    async def test_caller_supplied_actor_is_not_overwritten(self):
        # An explicit _actor in metadata is the caller's assertion; setdefault
        # must not clobber it with the request context.
        from astrocyte.types import AstrocyteContext

        brain, vs = await self._brain_with_vectors()
        await brain.retain(
            "x",
            bank_id="eng",
            metadata={"_actor": "service:importer"},
            context=AstrocyteContext(principal="user:calvin"),
        )
        items = await vs.list_vectors("eng", offset=0, limit=10)
        assert any((i.metadata or {}).get("_actor") == "service:importer" for i in items)
