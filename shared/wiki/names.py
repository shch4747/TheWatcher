"""Turn known member names in agent-written prose into wikilinks.

The model is asked to use a member's title. This pass makes that
deterministic: a WhatsApp display name, the title itself, or the
`Title (jid)` form the prompt showed all become `[[Title]]`. Text
already inside a wikilink is left as it is.
"""
from __future__ import annotations

import re
from collections.abc import Mapping

_WIKILINK_RE = re.compile(r"\[\[[^\]]+\]\]")
_PLACEHOLDER_RE = re.compile("\ue000(\\d+)\ue001")


def apply_member_wikilinks(text: str, aliases: Mapping[str, str]) -> str:
    """Replace each alias with `[[title]]`. Longer aliases win, so
    "Aira J" is linked before a bare "Aira" can eat its prefix."""
    forms = [
        (alias.strip(), title.strip()) for alias, title in aliases.items() if alias.strip() and title.strip()
    ]
    if not text or not forms:
        return text
    forms.sort(key=lambda pair: len(pair[0]), reverse=True)

    protected: list[str] = []

    def hold(match: re.Match[str]) -> str:
        protected.append(match.group(0))
        return f"\ue000{len(protected) - 1}\ue001"

    shielded = _WIKILINK_RE.sub(hold, text)
    lookup: dict[str, str] = {}
    for alias, title in forms:
        lookup.setdefault(alias.lower(), title)
    pattern = re.compile(
        "|".join(rf"(?<!\w){re.escape(alias)}(?!\w)" for alias, _title in forms),
        re.IGNORECASE,
    )

    def replace(match: re.Match[str]) -> str:
        return f"[[{lookup[match.group(0).lower()]}]]"

    linked = pattern.sub(replace, shielded)
    return _PLACEHOLDER_RE.sub(lambda match: protected[int(match.group(1))], linked)
