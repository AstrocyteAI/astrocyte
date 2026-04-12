"""DLP output scanner — scans recall/reflect results for PII."""

from __future__ import annotations

from astrocyte.config import AstrocyteConfig
from astrocyte.policy.barriers import PiiScanner
from astrocyte.policy.observability import StructuredLogger
from astrocyte.types import MemoryHit, RecallResult, ReflectResult


class OutputScanner:
    """Scans recall/reflect output for PII and applies redact/reject/warn actions."""

    def __init__(self, config: AstrocyteConfig, logger: StructuredLogger) -> None:
        self._config = config
        self._logger = logger
        self._scanner: PiiScanner | None = None
        if config.dlp.scan_recall_output or config.dlp.scan_reflect_output:
            self._scanner = PiiScanner(mode="regex", action=config.dlp.output_pii_action)

    @property
    def has_scanner(self) -> bool:
        return self._scanner is not None

    def scan_recall(self, result: RecallResult) -> RecallResult:
        """Scan recall hits for PII. Redact/warn/reject per DLP config."""
        if not self._scanner:
            return result
        action = self._config.dlp.output_pii_action
        scanned_hits: list[MemoryHit] = []
        for hit in result.hits:
            matches = self._scanner.scan(hit.text)
            if not matches:
                scanned_hits.append(hit)
                continue
            if action == "reject":
                continue  # Drop hit silently
            if action == "redact":
                redacted, _ = self._scanner.apply(hit.text)
                scanned_hits.append(
                    MemoryHit(
                        text=redacted,
                        score=hit.score,
                        fact_type=hit.fact_type,
                        metadata=hit.metadata,
                        tags=hit.tags,
                        occurred_at=hit.occurred_at,
                        source=hit.source,
                        memory_id=hit.memory_id,
                        bank_id=hit.bank_id,
                        memory_layer=hit.memory_layer,
                        utility_score=hit.utility_score,
                    )
                )
            else:
                # warn — pass through with logging
                self._logger.log(
                    "astrocyte.dlp.recall_pii_detected",
                    bank_id=hit.bank_id or "",
                    operation="recall",
                    data={"pii_types": ",".join(m.pii_type for m in matches), "memory_id": hit.memory_id or ""},
                )
                scanned_hits.append(hit)

        return RecallResult(
            hits=scanned_hits,
            total_available=result.total_available,
            truncated=result.truncated,
            trace=result.trace,
        )

    def scan_reflect(self, result: ReflectResult) -> ReflectResult:
        """Scan reflect answer for PII. Redact/warn/reject per DLP config."""
        if not self._scanner:
            return result
        matches = self._scanner.scan(result.answer)
        if not matches:
            return result

        action = self._config.dlp.output_pii_action
        if action == "reject":
            return ReflectResult(
                answer="",
                confidence=None,
                sources=result.sources,
                observations=["Reflect output blocked by DLP policy: PII detected"],
            )
        if action == "redact":
            redacted, _ = self._scanner.apply(result.answer)
            return ReflectResult(
                answer=redacted,
                confidence=result.confidence,
                sources=result.sources,
                observations=result.observations,
            )
        # warn
        self._logger.log(
            "astrocyte.dlp.reflect_pii_detected",
            operation="reflect",
            data={"pii_types": ",".join(m.pii_type for m in matches)},
        )
        return result
