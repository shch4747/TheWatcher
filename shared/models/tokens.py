"""Token-budget estimation for prompt chunking. A heuristic, not a
tokenizer: the ingestion prompts mix English, Hinglish, emoji and
WhatsApp ids, which tokenize far worse than the usual "4 chars per
token" - 3 is deliberately conservative so a chunk that fits by this
estimate fits for real. Overestimating only costs an extra call;
underestimating truncates a prompt mid-batch.
"""
from __future__ import annotations

import math

CHARS_PER_TOKEN = 3


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)
