"""Offline mock model ("engine"). Deterministic, no network, no LLM.

Agents never hold a model client: the orchestrator gives the model to ``ToolContext`` and agents reach it only
through ``ctx.complete()``, which is checked by the workload control gate first (see control.py). This mirrors the
"check at the inference layer, not in workload code" idea. Pricing is illustrative only, not any provider's."""
from __future__ import annotations
import re
from dataclasses import dataclass

from .tools import summarize

# USD per 1M tokens (input, output). Illustrative numbers for cost accounting tests and demos, not real prices.
PRICING_USD_PER_MTOK = {"mock-small": (0.50, 1.50), "mock-large": (5.00, 15.00)}
DEFAULT_MODEL = "mock-small"


def count_tokens(text: str) -> int:
    """Rough offline token count: words and punctuation marks. Real backends report usage themselves."""
    return len(re.findall(r"\w+|[^\w\s]", text or ""))


def cost_usd(model: str, tokens_in: int, tokens_out: int) -> float:
    pin, pout = PRICING_USD_PER_MTOK[model]
    return round((tokens_in * pin + tokens_out * pout) / 1_000_000, 8)


@dataclass(frozen=True)
class Completion:
    text: str
    model: str
    tokens_in: int
    tokens_out: int
    cost_usd: float


class MockModel:
    """Extractive 'completion': the same deterministic summary the summarize tool produces."""

    def __init__(self, name: str = DEFAULT_MODEL):
        if name not in PRICING_USD_PER_MTOK:
            raise ValueError(f"unknown mock model {name!r}")
        self.name = name
        self.calls = 0

    def estimate(self, prompt: str, max_tokens: int) -> tuple[int, int, float]:
        """Worst-case usage for a request, used to reserve budget before the call."""
        tin = count_tokens(prompt)
        return tin, max_tokens, cost_usd(self.name, tin, max_tokens)

    def complete(self, prompt: str, *, focus: str = "", max_sentences: int = 2, max_tokens: int = 256) -> Completion:
        self.calls += 1
        text = summarize(prompt, max_sentences, focus)
        words = text.split()
        while words and count_tokens(" ".join(words)) > max_tokens:  # honour the output cap the budget reserved
            words.pop()
        text = " ".join(words) if len(words) < len(text.split()) else text
        tin, tout = count_tokens(prompt), count_tokens(text)
        return Completion(text, self.name, tin, tout, cost_usd(self.name, tin, tout))
