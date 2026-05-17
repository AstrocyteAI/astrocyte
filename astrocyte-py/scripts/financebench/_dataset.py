"""Load the FinanceBench open-source dataset.

Dataset layout (patronus-ai/financebench):
    data/financebench_open_source.jsonl  — Q&A pairs
    pdfs/<doc_name>.pdf                  — source 10-K filings
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class FinanceBenchEntry:
    financebench_id: str
    question: str
    answer: str
    doc_name: str
    page_number: int | None
    evidence: str
    question_type: str
    domain: str


def load_dataset(dataset_dir: Path, *, max_questions: int = 0) -> list[FinanceBenchEntry]:
    """Load entries from financebench_open_source.jsonl.

    Args:
        dataset_dir: root of the cloned patronus-ai/financebench repo.
        max_questions: cap on entries returned (0 = all).
    """
    jsonl_path = dataset_dir / "data" / "financebench_open_source.jsonl"
    if not jsonl_path.exists():
        raise FileNotFoundError(
            f"FinanceBench dataset not found at {jsonl_path}\n"
            "Run:  make fetch-financebench"
        )
    entries: list[FinanceBenchEntry] = []
    with jsonl_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            raw = json.loads(line)
            entries.append(
                FinanceBenchEntry(
                    financebench_id=raw["financebench_id"],
                    question=raw["question"],
                    answer=raw["answer"],
                    doc_name=raw["doc_name"],
                    page_number=raw.get("page_number"),
                    evidence=raw.get("evidence", ""),
                    question_type=raw.get("question_type", ""),
                    domain=raw.get("domain", ""),
                )
            )
    if max_questions > 0:
        entries = entries[:max_questions]
    return entries


def pdf_path(dataset_dir: Path, doc_name: str) -> Path:
    """Absolute path to the PDF for a given doc_name."""
    return dataset_dir / "pdfs" / f"{doc_name}.pdf"


def unique_docs(entries: list[FinanceBenchEntry]) -> list[str]:
    """Deduplicated doc_names in order of first appearance."""
    seen: set[str] = set()
    docs: list[str] = []
    for e in entries:
        if e.doc_name not in seen:
            seen.add(e.doc_name)
            docs.append(e.doc_name)
    return docs
