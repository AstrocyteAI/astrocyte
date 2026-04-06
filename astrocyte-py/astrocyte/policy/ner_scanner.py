"""NER-based PII scanner — uses spaCy for name, address, organization detection.

spaCy is an optional dependency. Install with: pip install astrocyte[ner]
"""

from __future__ import annotations

import logging

from astrocyte.types import PiiMatch

logger = logging.getLogger("astrocyte.pii")

# Mapping from spaCy entity labels to PII types
_ENTITY_PII_MAP: dict[str, tuple[str, str]] = {
    "PERSON": ("name", "[NAME_REDACTED]"),
    "GPE": ("address", "[ADDRESS_REDACTED]"),
    "LOC": ("address", "[ADDRESS_REDACTED]"),
    "FAC": ("address", "[ADDRESS_REDACTED]"),
}


class NerPiiScanner:
    """spaCy-based NER for names, addresses, and locations.

    Requires spaCy and a language model (e.g. en_core_web_sm).
    Sync — spaCy inference is CPU-bound, no async needed.
    """

    def __init__(self, model: str = "en_core_web_sm") -> None:
        try:
            import spacy

            self._nlp = spacy.load(model)
        except ImportError:
            raise ImportError(
                "NER PII detection requires spaCy. Install with: pip install astrocyte[ner]"
            ) from None
        except OSError:
            raise ImportError(
                f"spaCy model '{model}' not found. Install with: python -m spacy download {model}"
            ) from None

    def scan(self, text: str) -> list[PiiMatch]:
        """Detect PERSON, GPE, LOC, FAC entities as PII."""
        doc = self._nlp(text)
        matches: list[PiiMatch] = []

        for ent in doc.ents:
            if ent.label_ in _ENTITY_PII_MAP:
                pii_type, replacement = _ENTITY_PII_MAP[ent.label_]
                matches.append(
                    PiiMatch(
                        pii_type=pii_type,
                        start=ent.start_char,
                        end=ent.end_char,
                        matched_text=ent.text,
                        replacement=replacement,
                    )
                )

        return matches
