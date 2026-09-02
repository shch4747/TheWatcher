"""
The only place the Project Agent makes LLM calls. Two jobs:

1. Draft a post-mortem from a member's stated reason + activity timeline.
2. Judge, for project pairs above the embedding-similarity threshold,
   whether the overlap is real (topic/goal/papers) or coincidental phrasing.

Kept separate from healthbar.py (which is deliberately deterministic /
LLM-free) so the expensive, judgment-heavy calls are easy to find, log,
and rate-limit independently of the cheap scoring path.
"""

from __future__ import annotations

import json

from anthropic import Anthropic

from config import ANTHROPIC_API_KEY, REASONER_MODEL

_client = Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None


def draft_post_mortem(
    project_name: str,
    member_reason: str,
    activity_timeline: str,
) -> str:
    """
    activity_timeline: a short, pre-formatted string summarizing the
    healthbar decline / last commits / last WA messages -- built by the
    caller from GitHub + Lapis data, not re-fetched here.
    """
    if _client is None:
        raise RuntimeError("ANTHROPIC_API_KEY not configured")

    prompt = f"""You are drafting a short project post-mortem for an internal
ARIES (IIT Delhi AI/ML club) wiki page. Be factual and concise, not dramatic.

Project: {project_name}

Reason given by the project's last active member:
\"\"\"{member_reason}\"\"\"

Activity timeline leading up to this:
{activity_timeline}

Write a 3-5 sentence post-mortem for the "## Post-mortem" section of the
project's wiki page. State plainly why the project stopped, grounded in
the member's stated reason, with the activity timeline as supporting
context. Do not moralize or speculate beyond what's given. This is a
draft -- it will be shown to the member for confirmation/edits before
being finalized, so flag it as a draft in your first line."""

    resp = _client.messages.create(
        model=REASONER_MODEL,
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text


def judge_similarity(
    project_a: dict,
    project_b: dict,
) -> dict:
    """
    project_a / project_b: dicts with keys like {"name", "goal", "papers"}
    (papers optional/empty at the first-pass stage).

    Returns {"is_similar": bool, "confidence": float, "reasoning": str}
    """
    if _client is None:
        raise RuntimeError("ANTHROPIC_API_KEY not configured")

    prompt = f"""Two ARIES club projects came back above an embedding
similarity threshold when comparing their titles and goals. Judge whether
this is a genuine overlap worth flagging to humans for a possible merge,
or coincidental phrasing (e.g. both mention "computer vision" but solve
unrelated problems).

Project A: {project_a.get('name')}
Goal A: {project_a.get('goal', '')}
Papers A: {', '.join(project_a.get('papers', [])) or 'none listed'}

Project B: {project_b.get('name')}
Goal B: {project_b.get('goal', '')}
Papers B: {', '.join(project_b.get('papers', [])) or 'none listed'}

Respond with ONLY a JSON object, no other text, in this exact shape:
{{"is_similar": true or false, "confidence": 0.0-1.0, "reasoning": "one sentence"}}"""

    resp = _client.messages.create(
        model=REASONER_MODEL,
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )
    text = resp.content[0].text.strip()
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(text)
