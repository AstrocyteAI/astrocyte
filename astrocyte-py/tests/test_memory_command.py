"""``astrocyte memory`` — inspect and remove captured memories per project."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from argparse import Namespace
from pathlib import Path

import pytest

pytest.importorskip("astrocyte_sqlite")

from astrocyte.cli import main as cli_main  # noqa: E402
from astrocyte.harness.memories import open_local  # noqa: E402
from astrocyte.harness.project import project_bank  # noqa: E402


@pytest.fixture
def local(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    cfg = home / ".config" / "astrocyte" / "astrocyte.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        "provider_tier: storage\nvector_store: sqlite\n"
        f"vector_store_config:\n  path: {tmp_path / 'mem.db'}\n"
        "llm_provider: mock\nbarriers:\n  pii:\n    mode: disabled\n"
    )
    repo, other = tmp_path / "payments", tmp_path / "web"
    for d in (repo, other):
        d.mkdir()
        subprocess.run(["git", "-C", str(d), "init", "-q"], check=True)
    monkeypatch.chdir(repo)
    return Namespace(cfg=cfg, repo=repo, other=other, bank=project_bank(str(repo)))


def retain(cfg: Path, bank: str, *texts: str) -> None:
    async def go():
        pipeline, brain = open_local(cfg)
        for t in texts:
            await brain.retain(t, bank_id=bank, metadata={"source": "codex"})
        await pipeline.vector_store.close()

    asyncio.run(go())


def run(capsys, *argv: str) -> tuple[int, str, str]:
    code = cli_main(["memory", *argv])
    out = capsys.readouterr()
    return code, out.out, out.err


def ids(capsys) -> list[str]:
    _, out, _ = run(capsys, "--json")
    return [m["id"] for m in json.loads(out)]


def test_lists_this_projects_memories_only(local, capsys):
    retain(local.cfg, local.bank, "Deploys happen on Tuesdays.")
    retain(local.cfg, project_bank(str(local.other)), "The web app uses Svelte.")
    code, out, _ = run(capsys)
    assert code == 0 and local.bank in out and "Tuesdays" in out and "Svelte" not in out
    assert "codex" in out, "the capturing agent is shown"
    code, out, _ = run(capsys, "list", "--project", str(local.other))
    assert "Svelte" in out and "Tuesdays" not in out


def test_search_finds_by_meaning_of_the_query(local, capsys):
    retain(local.cfg, local.bank, "Deploys happen on Tuesdays.", "The cache TTL is five minutes.")
    code, out, _ = run(capsys, "search", "cache TTL", "--json")
    hits = json.loads(out)
    assert code == 0 and hits and "cache TTL" in hits[0]["text"]


def test_forget_by_unique_prefix(local, capsys):
    retain(local.cfg, local.bank, "Deploys happen on Tuesdays.", "The cache TTL is five minutes.")
    before = ids(capsys)
    code, out, _ = run(capsys, "forget", before[0][:8])
    assert code == 0 and "Removed 1 memory" in out
    assert ids(capsys) == before[1:]


@pytest.mark.parametrize("prefix,code,message", [("ab", 2, "at least 4"), ("zzzzzzzz", 1, "no memory")])
def test_forget_refuses_what_it_cannot_pin_down(local, capsys, prefix, code, message):
    retain(local.cfg, local.bank, "Deploys happen on Tuesdays.")
    got, _, err = run(capsys, "forget", prefix)
    assert got == code and message in err
    assert len(ids(capsys)) == 1


def test_forget_all_needs_yes(local, capsys):
    retain(local.cfg, local.bank, "Deploys happen on Tuesdays.", "The cache TTL is five minutes.")
    code, _, err = run(capsys, "forget", "--all")
    assert code == 1 and "--yes" in err and len(ids(capsys)) == 2
    code, out, _ = run(capsys, "forget", "--all", "--yes")
    assert code == 0 and ids(capsys) == []


def test_banks_marks_the_current_project(local, capsys):
    retain(local.cfg, local.bank, "Deploys happen on Tuesdays.")
    retain(local.cfg, project_bank(str(local.other)), "The web app uses Svelte.")
    code, out, _ = run(capsys, "banks")
    assert code == 0
    marked = [line for line in out.splitlines() if line.strip().startswith("→")]
    assert len(marked) == 1 and local.bank in marked[0]


def test_not_set_up_is_a_clear_message(local, capsys):
    local.cfg.unlink()
    code, _, err = run(capsys)
    assert code == 2 and "astrocyte setup" in err


def test_forget_erases_the_text_from_the_database_files(local, capsys, tmp_path):
    """Core forget is a soft delete; a removed key must not stay on disk."""
    secret = "the staging password is Kestrel-Opal-Fjord"
    retain(local.cfg, local.bank, secret, "Deploys happen on Tuesdays.")
    # Lowercased: the full-text index holds the words as lowercase tokens.
    on_disk = lambda: any(w in b"".join(f.read_bytes() for f in tmp_path.glob("mem.db*")).lower()  # noqa: E731
                          for w in (b"kestrel", b"fjord"))
    assert on_disk()
    target = next(i for i in json.loads(run(capsys, "--json")[1]) if "Kestrel" in i["text"])
    code, out, _ = run(capsys, "forget", target["id"][:8])
    assert code == 0 and "Erased from disk" in out
    assert not on_disk(), "no copy in the db, its WAL, or the FTS index"
    assert len(ids(capsys)) == 1


def test_empty_project_and_search_misses_are_plain_messages(local, capsys):
    assert "No memories in" in run(capsys)[1]
    assert "Nothing in" in run(capsys, "search", "anything at all")[1]
    assert run(capsys, "search", "anything", "--json")[1].strip() == "[]"
    code, out, _ = run(capsys, "forget", "--all")
    assert code == 0 and "No memories in" in out


def test_human_search_output_shows_id_and_text(local, capsys):
    retain(local.cfg, local.bank, "The cache TTL is five minutes.")
    code, out, _ = run(capsys, "search", "cache TTL")
    assert code == 0 and "cache TTL" in out and ids(capsys)[0][:8] in out


def test_list_limit_says_more_may_exist(local, capsys):
    retain(local.cfg, local.bank, "one fact here", "two fact here", "three fact here")
    code, out, _ = run(capsys, "list", "-n", "2")
    assert "2 most recent" in out and len([ln for ln in out.splitlines() if ln.startswith("  ")]) == 2


def test_forget_needs_ids_or_all(local, capsys):
    code, _, err = run(capsys, "forget")
    assert code == 2 and "--all" in err


def test_forget_refuses_an_ambiguous_prefix(local, capsys, monkeypatch):
    from astrocyte.harness import memories

    retain(local.cfg, local.bank, "one fact here", "two fact here")
    real = memories._all_items

    async def twins(store, bank):  # two ids sharing a prefix, as UUIDs can
        items = await real(store, bank)
        for n, item in enumerate(items):
            item.id = f"dup0{n}{item.id}"
        return items

    monkeypatch.setattr(memories, "_all_items", twins)
    code, _, err = run(capsys, "forget", "dup0")
    assert code == 1 and "matches 2 memories" in err
    assert len(ids(capsys)) == 2


def test_banks_json_and_empty(local, capsys):
    assert "No memories yet" in run(capsys, "banks")[1]
    retain(local.cfg, local.bank, "Deploys happen on Tuesdays.")
    [row] = json.loads(run(capsys, "banks", "--json")[1])
    assert row["bank"] == local.bank and row["memories"] == 1 and row["newest"]


def test_stores_without_enumeration_or_purge_say_what_they_can_do(local, capsys):
    """A server-backed store (no list_banks / purge) still lists and forgets;
    it says that forgotten memories are kept for history."""
    local.cfg.write_text("provider_tier: storage\nvector_store: in_memory\nllm_provider: mock\n"
                         "barriers:\n  pii:\n    mode: disabled\n")
    code, _, err = run(capsys, "banks")
    assert code == 2 and "can't enumerate banks" in err
    # in_memory doesn't persist across commands, so forget within one brain:
    import asyncio as aio
    from argparse import Namespace as NS

    from astrocyte.harness import memories

    async def go():
        pipeline, brain = memories.open_local(local.cfg)
        await brain.retain("Deploys happen on Tuesdays.", bank_id=local.bank)
        listed = await pipeline.vector_store.list_vectors(local.bank)
        args = NS(bank=local.bank, project=None, ids=[listed[0].id[:8]], all=False, yes=False)
        return await memories._forget(args, pipeline, brain)

    assert aio.run(go()) == 0
    assert "keeps forgotten memories for history" in capsys.readouterr().out


# ── import / export ──────────────────────────────────────────────────────

from astrocyte.harness.memories import import_files, sections  # noqa: E402

GUIDE = """# Project guide

Intro line.

## Testing

Run `uv run pytest` from astrocyte-py.

```bash
# not a heading: inside a fence
uv run pytest -x
```

## Deploys

Deploys happen on Tuesdays.
"""


def test_sections_split_at_headings_but_not_inside_code_fences():
    parts = sections(GUIDE)
    assert [p.splitlines()[0] for p in parts] == ["# Project guide", "## Testing", "## Deploys"]
    assert "# not a heading: inside a fence" in parts[1]


def test_long_sections_are_split_at_paragraphs():
    long = "## Big\n\n" + "\n\n".join("para " + "x" * 900 for _ in range(6))
    parts = sections(long)
    assert len(parts) > 1 and all(len(p) <= 3_000 for p in parts)


def test_directories_yield_docs_but_skip_hidden_vendored_and_huge(tmp_path):
    (tmp_path / "docs" / "sub").mkdir(parents=True)
    (tmp_path / "docs" / "a.md").write_text("# A")
    (tmp_path / "docs" / "sub" / "b.txt").write_text("B")
    (tmp_path / "docs" / "c.py").write_text("print()")
    for skipped in (".git", "node_modules", ".venv"):
        (tmp_path / "docs" / skipped).mkdir()
        (tmp_path / "docs" / skipped / "x.md").write_text("# hidden")
    (tmp_path / "docs" / "huge.md").write_text("x" * (600 * 1024))
    names = sorted(f.name for f in import_files([str(tmp_path / "docs")]))
    assert names == ["a.md", "b.txt"]


def _memories(local) -> list[dict]:
    async def go():
        pipeline, _ = open_local(local.cfg)
        items = await pipeline.vector_store.list_vectors(local.bank)
        await pipeline.vector_store.close()
        return [{"text": i.text, **(i.metadata or {})} for i in items]

    return asyncio.run(go())


def test_import_is_a_sync_of_each_file(local, capsys):
    guide = local.repo / "CLAUDE.md"
    guide.write_text(GUIDE)
    code, out, _ = run(capsys, "import", "CLAUDE.md")
    assert code == 0 and "3 added" in out
    mems = _memories(local)
    assert len(mems) == 3 and {m["import_path"] for m in mems} == {"CLAUDE.md"}
    assert all(m["source"] == "import" for m in mems)

    code, out, _ = run(capsys, "import", "CLAUDE.md")
    assert "0 added, 0 removed, 3 unchanged" in out, "re-importing an unchanged file is a no-op"

    guide.write_text(GUIDE.replace("Tuesdays", "Thursdays").replace("Intro line.", "Intro line, revised."))
    code, out, _ = run(capsys, "import", "CLAUDE.md")
    assert "2 added, 2 removed, 1 unchanged" in out
    texts = " ".join(m["text"] for m in _memories(local))
    assert "Thursdays" in texts and "Tuesdays" not in texts


def test_import_dry_run_changes_nothing(local, capsys):
    (local.repo / "AGENTS.md").write_text(GUIDE)
    code, out, _ = run(capsys, "import", "AGENTS.md", "--dry-run")
    assert code == 0 and "3 to add" in out and _memories(local) == []


def test_import_of_a_missing_path_is_an_error(local, capsys):
    code, _, err = run(capsys, "import", "nope.md")
    assert code == 2 and "nope.md" in err


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_export_then_import_round_trips_through_an_archive(local, capsys, tmp_path):
    retain(local.cfg, local.bank, "Deploys happen on Tuesdays.", "The cache TTL is five minutes.")
    archive = tmp_path / "proj.ama.jsonl"
    code, out, _ = run(capsys, "export", str(archive))
    assert code == 0 and "Exported 2 memories" in out
    assert oct(archive.stat().st_mode & 0o777) == "0o600", "an archive holds conversations"
    header, *records = [json.loads(line) for line in archive.read_text().splitlines()]
    assert header["memory_count"] == 2 and {r["text"] for r in records} >= {"Deploys happen on Tuesdays."}
    code, out, _ = run(capsys, "import", str(archive), "--bank", "elsewhere")
    assert code == 0 and "Imported 2" in out
    code, out, _ = run(capsys, "list", "--bank", "elsewhere")
    assert "Tuesdays" in out


def test_a_duplicated_section_is_skipped_the_same_way_every_run(local, capsys):
    """The pipeline's own duplicate check only sees one process's retains;
    without checking the store, the next import stored the duplicate."""
    (local.repo / "a.md").write_text("## Deploys\n\nDeploys happen on Tuesdays.")
    (local.repo / "b.md").write_text("## Deploys\n\nDeploys happen on Tuesdays.")
    code, out, _ = run(capsys, "import", "a.md", "b.md")
    assert "Added 1" in out and "skipped 1 near-duplicates" in out
    code, out, _ = run(capsys, "import", "a.md", "b.md")
    assert "Added 0" in out and len(_memories(local)) == 1


def test_the_id_an_agent_gets_from_recall_is_what_forget_takes(local, capsys):
    """The MCP server tells agents to hand users a memory_id from
    memory_recall for `astrocyte memory forget <id> --bank <bank>`."""
    retain(local.cfg, local.bank, "The staging password is in the team vault.")

    async def recall_id():
        pipeline, brain = open_local(local.cfg)
        hits = (await brain.recall("staging password", bank_id=local.bank)).hits
        await pipeline.vector_store.close()
        return hits[0].memory_id

    memory_id = asyncio.run(recall_id())
    code, out, _ = run(capsys, "forget", memory_id, "--bank", local.bank)
    assert code == 0 and "Removed 1 memory" in out
    assert ids(capsys) == []
