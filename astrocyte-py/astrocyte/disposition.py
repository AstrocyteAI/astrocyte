"""Bank disposition + background — per-bank prompt-shaping configuration.

Adopted from Hindsight (``hindsight-api-slim/hindsight_api/engine/search/think_utils.py``
+ ``models.py`` Bank.disposition / Bank.background). Each bank has:

  - **disposition**: three traits on 1-5 scales
      * skepticism (1=very trusting → 5=highly skeptical)
      * literalism (1=interprets flexibly → 5=very literal)
      * empathy (1=facts-only → 5=highly emotional-context-aware)
  - **background**: free-form text about who/what the bank serves
      (e.g., "a software engineer's professional context",
       "a customer-support agent for healthcare clinics")

These are **reflect-time** shapers — they affect how retrieved evidence
is woven into an answer, NOT what gets retrieved. Recall stays
disposition-blind so retrieval ranking is reproducible across bank
configurations.

Public API:

    BankDisposition(skepticism=3, literalism=3, empathy=3)
        # validates 1-5 ranges; raises ValueError on out-of-range

    BankProfile(disposition, background="")
        # combined shape; convenient bundle for prompt formatting

    format_disposition_block(disposition) -> str
        # renders three-line "Your disposition traits:" block for
        # injection into a system prompt

    format_profile_block(profile) -> str
        # renders disposition + background as a complete bank-profile
        # block for system prompts

Storage shape (per migration 024):
  astrocyte_banks.disposition JSONB  ('{"skepticism": 3, "literalism": 3, "empathy": 3}')
  astrocyte_banks.background  TEXT   ('')
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

VALID_TRAIT_RANGE = range(1, 6)  # 1-5 inclusive

DEFAULT_TRAIT = 3  # "balanced"


# ─── trait descriptors ────────────────────────────────────────────────


_SKEPTICISM_DESCRIPTIONS = {
    1: "You are very trusting and tend to take information at face value.",
    2: "You tend to trust information but may question obvious inconsistencies.",
    3: "You have a balanced approach to information, neither too trusting nor too skeptical.",
    4: "You are somewhat skeptical and often question the reliability of information.",
    5: "You are highly skeptical and critically examine all information for accuracy and hidden motives.",
}

_LITERALISM_DESCRIPTIONS = {
    1: "You interpret information very flexibly, reading between the lines and inferring intent.",
    2: "You tend to consider context and implied meaning alongside literal statements.",
    3: "You balance literal interpretation with contextual understanding.",
    4: "You prefer to interpret information more literally and precisely.",
    5: "You interpret information very literally and focus on exact wording and commitments.",
}

_EMPATHY_DESCRIPTIONS = {
    1: "You focus primarily on facts and data, setting aside emotional context.",
    2: "You consider facts first but acknowledge emotional factors exist.",
    3: "You balance factual analysis with emotional understanding.",
    4: "You give significant weight to emotional context and human factors.",
    5: "You strongly consider the emotional state and circumstances of others when forming memories.",
}

_LEVEL_NAMES = {1: "very low", 2: "low", 3: "moderate", 4: "high", 5: "very high"}


def describe_trait_level(value: int) -> str:
    """Map a trait value (1-5) to a level name. Out-of-range → 'moderate'."""
    return _LEVEL_NAMES.get(value, "moderate")


# ─── data shapes ──────────────────────────────────────────────────────


@dataclass
class BankDisposition:
    """Three personality traits shaping the bank's answer style."""

    skepticism: int = DEFAULT_TRAIT
    literalism: int = DEFAULT_TRAIT
    empathy: int = DEFAULT_TRAIT

    def __post_init__(self) -> None:
        for name, value in (
            ("skepticism", self.skepticism),
            ("literalism", self.literalism),
            ("empathy", self.empathy),
        ):
            if not isinstance(value, int):
                raise TypeError(f"{name} must be int, got {type(value).__name__}")
            if value not in VALID_TRAIT_RANGE:
                raise ValueError(f"{name} must be in 1..5, got {value}")

    def to_dict(self) -> dict[str, int]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BankDisposition:
        """Construct from a JSON-shaped dict; extras are ignored, missing → default."""
        return cls(
            skepticism=int(data.get("skepticism", DEFAULT_TRAIT)),
            literalism=int(data.get("literalism", DEFAULT_TRAIT)),
            empathy=int(data.get("empathy", DEFAULT_TRAIT)),
        )

    @classmethod
    def balanced(cls) -> BankDisposition:
        """The default 'no special bias' disposition (3/3/3)."""
        return cls()


@dataclass
class BankProfile:
    """A bank's prompt-shaping configuration: disposition + background."""

    disposition: BankDisposition = field(default_factory=BankDisposition.balanced)
    background: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "disposition": self.disposition.to_dict(),
            "background": self.background,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BankProfile:
        return cls(
            disposition=BankDisposition.from_dict(data.get("disposition") or {}),
            background=str(data.get("background", "") or ""),
        )


# ─── prompt formatters ────────────────────────────────────────────────


def format_disposition_block(disposition: BankDisposition) -> str:
    """Render the three traits as a system-prompt block.

    Mirrors Hindsight's ``build_disposition_description`` output shape.
    """
    return (
        "Your disposition traits:\n"
        f"- Skepticism ({describe_trait_level(disposition.skepticism)}): "
        f"{_SKEPTICISM_DESCRIPTIONS[disposition.skepticism]}\n"
        f"- Literalism ({describe_trait_level(disposition.literalism)}): "
        f"{_LITERALISM_DESCRIPTIONS[disposition.literalism]}\n"
        f"- Empathy ({describe_trait_level(disposition.empathy)}): "
        f"{_EMPATHY_DESCRIPTIONS[disposition.empathy]}"
    )


def format_background_block(background: str) -> str:
    """Render the background text as a system-prompt block.

    Returns empty string if background is empty — caller can join blocks
    with newlines without worrying about blank lines.
    """
    bg = (background or "").strip()
    if not bg:
        return ""
    return f"Background:\n{bg}"


def format_profile_block(profile: BankProfile) -> str:
    """Render disposition + background as a combined system-prompt block.

    Skips the background section if empty. Always renders disposition
    (even if all-3-defaults, callers can choose to omit by checking
    ``is_balanced()`` first if they prefer).
    """
    parts = [format_disposition_block(profile.disposition)]
    bg_block = format_background_block(profile.background)
    if bg_block:
        parts.append(bg_block)
    return "\n\n".join(parts)


def is_balanced(disposition: BankDisposition) -> bool:
    """True if all traits are at the default (3)."""
    return (
        disposition.skepticism == DEFAULT_TRAIT
        and disposition.literalism == DEFAULT_TRAIT
        and disposition.empathy == DEFAULT_TRAIT
    )
