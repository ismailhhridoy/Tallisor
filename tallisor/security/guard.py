"""Guards around the language model.

1. Spotlighting (Hines et al., Microsoft, 2024): untrusted text from uploaded
   files is datamarked and fenced before it reaches a model, and the model is
   told that fenced text is data, never instructions.
2. Injection screening: suspicious phrases in untrusted fields are flagged and
   logged. This is a tripwire only. Safety does not depend on it, because no
   decision is taken from free text: categories and amounts come from code.
3. Numeric grounding: every money amount a model writes must equal an amount
   present in the case evidence. Otherwise the output is rejected and the
   deterministic explanation is used instead (hallucination control, OWASP LLM09).
"""
from __future__ import annotations

import re

from ..money import to_cents

MARK = "ˆ"  # datamarking character placed between words of untrusted text
FENCE_OPEN = "<<UNTRUSTED_DATA>>"
FENCE_CLOSE = "<</UNTRUSTED_DATA>>"

_INJECTION_PATTERNS = [
    r"ignore (all |any |the )?(previous|prior|above) (instructions|rules)",
    r"disregard (the |all )?(instructions|rules|system)",
    r"\bsystem prompt\b",
    r"\byou are now\b",
    r"\b(approve|reconcile|mark) (all|every|everything)\b",
    r"\b(transfer|send|wire|pay out|move)\b.{0,40}\b(\$|usd|money|funds|account)\b",
    r"\bnew instructions?\b",
    r"\bact as\b",
    r"</?(system|assistant|instruction)>",
]
_INJECTION_RE = re.compile("|".join(_INJECTION_PATTERNS), re.IGNORECASE)

# Money with exactly two decimals, optional sign and $: 1,234.56  -$12.00  −$5.00
# A trailing full stop ends a sentence and must not hide the amount before it,
# so only a following digit or ".digit" disqualifies a match.
_MONEY_RE = re.compile(r"(?<![\w.])([-−+]?\$?\d{1,3}(?:,\d{3})*\.\d{2}|[-−+]?\$?\d+\.\d{2})(?!\d)(?!\.\d)")


def datamark(text: str) -> str:
    cleaned = (text or "").replace(FENCE_OPEN, "").replace(FENCE_CLOSE, "")
    return f"{FENCE_OPEN}{MARK.join(cleaned.split())}{FENCE_CLOSE}"


def screen_injection(text: str) -> list[str]:
    if not text:
        return []
    return sorted({m.group(0).lower() for m in _INJECTION_RE.finditer(text)})


def money_mentions(text: str) -> list[int]:
    values = []
    for raw in _MONEY_RE.findall(text or ""):
        try:
            values.append(abs(to_cents(raw)))
        except Exception:
            continue
    return values


def verify_numbers(text: str, allowed: set[int]) -> tuple[bool, list[int], int]:
    """Return (ok, unsupported_amounts, amounts_checked)."""
    found = money_mentions(text)
    allowed_abs = {abs(a) for a in allowed}
    bad = [v for v in found if v not in allowed_abs]
    return (not bad), bad, len(found)
