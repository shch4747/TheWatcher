"""Who is who. Exports show the *exporter's* contact names, so the same person
appears as "Aman", "Aman Sharma", "~ Aman" or a phone number in different
exports. `members.csv` maps all of those to one person (and, optionally, to a
wiki member title so the agent sees a *linked* member).

members.csv columns (header row required; all but `name` optional):
    name        canonical display name
    wiki_title  member page title; when set the person is "linked" (like /link)
    wa_identity phone number or jid, if known
    aliases     other names seen in exports, separated by ';'
"""

from __future__ import annotations

import csv
import difflib
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

_PHONE = re.compile(r"[+\d\s()\-.]+")


def norm(name: str) -> str:
    """Comparison key: casefolded name, or the last 10 digits of a phone number."""
    s = name.replace("\u202f", " ").strip().lstrip("~").strip()
    digits = re.sub(r"\D", "", s)
    if len(digits) >= 7 and _PHONE.fullmatch(s):
        return digits[-10:]
    return " ".join(s.split()).casefold()


def _is_phone_key(key: str) -> bool:
    return key.isdigit()


def _jid(identity: str | None, fallback_key: str, prefix: str) -> str:
    if identity:
        identity = identity.strip()
        if "@" in identity:
            return identity
        digits = re.sub(r"\D", "", identity)
        if digits:
            return f"{digits}@s.whatsapp.net"
    return f"{prefix}{hashlib.sha1(fallback_key.encode()).hexdigest()[:10]}@s.whatsapp.net"


@dataclass(frozen=True)
class Person:
    key: str
    display: str | None  # None for a bare phone number (the agent then never shows a number as a name)
    wa_identity: str
    wiki_title: str | None  # set => linked member


class Roster:
    def __init__(self, rows: list[dict[str, str]] | None = None) -> None:
        self.warnings: list[str] = []
        self._by_alias: dict[str, Person] = {}
        self._unknown: dict[str, Person] = {}
        self.people: list[Person] = []
        for row in rows or []:
            name = (row.get("name") or "").strip()
            if not name:
                continue
            title = (row.get("wiki_title") or "").strip() or None
            key = norm(name)
            person = Person(key, name, _jid(row.get("wa_identity"), key, "m"), title)
            self.people.append(person)
            aliases = [name, *(a for a in (row.get("aliases") or "").split(";"))]
            if title:
                aliases.append(title)
            if row.get("wa_identity"):
                aliases.append(row["wa_identity"])
            for alias in aliases:
                k = norm(alias)
                if not k:
                    continue
                if k in self._by_alias and self._by_alias[k] != person:
                    self.warnings.append(
                        f"alias {alias!r} maps to both {self._by_alias[k].display!r} and {name!r}; "
                        "keeping the first"
                    )
                    continue
                self._by_alias[k] = person

    @classmethod
    def from_csv(cls, path: Path | None) -> Roster:
        if path is None or not Path(path).is_file():
            return cls()
        with open(path, newline="", encoding="utf-8-sig") as fh:
            rows = [
                {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
                for r in csv.DictReader(fh)
            ]
        return cls(rows)

    def resolve(self, raw: str, exporter: str | None = None) -> Person:
        n = norm(raw)
        if n == "you" and exporter:
            raw, n = exporter, norm(exporter)
        if n in self._by_alias:
            return self._by_alias[n]
        if n not in self._unknown:
            if _is_phone_key(n):
                self._unknown[n] = Person(n, None, _jid(raw, n, "u"), None)
            else:
                self._unknown[n] = Person(n, raw.strip().lstrip("~").strip(), _jid(None, n, "u"), None)
        return self._unknown[n]

    def is_known(self, person: Person) -> bool:
        return self._by_alias.get(person.key) == person

    def near(self, person: Person) -> list[str]:
        """Roster names that look like an unknown sender (for the `check` report)."""
        names = [p.display for p in self.people if p.display]
        return difflib.get_close_matches(person.display or "", names, n=2, cutoff=0.6)
