"""groups.csv and exports.csv -> typed rows. Nothing about groups is hardcoded.

groups.csv  (one row per WhatsApp group)
    title       the group's name (the key used by exports.csv)            required
    kind        project | event | coordis | exes | research | all | other required
    initiative  project/event page title (defaults to `title`)
    lead        member title of the lead (project/event; default "Unassigned")
    brief       one-line brief for the initiative page
    enabled     false/0/no to skip the group (default true)

exports.csv  (one row per exported .txt file; several files per group is normal)
    file        path, relative to the CSV's folder or absolute              required
    group       a title from groups.csv                                     required
    exporter    name of the person who exported it (resolves "You")
    timezone    IANA tz of the exporter's phone (default: experiment tz)
    dayfirst    dmy | mdy | blank = auto-detect
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

KINDS = ("project", "event", "coordis", "exes", "research", "all", "other")
SINGLETON_KINDS = ("coordis", "exes", "research", "all")
INITIATIVE_KINDS = ("project", "event")
_FALSE = {"false", "0", "no", "n", "off"}


class DataError(ValueError):
    """A problem in the user's CSV/export data, worded for the user."""


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")[:80].rstrip("-") or "untitled"


@dataclass(frozen=True)
class Group:
    title: str
    kind: str
    initiative: str | None
    lead: str
    brief: str
    channel_title: str  # what the wiki calls the channel (initiative for project/event, like /setup)
    jid: str  # synthetic, deterministic


@dataclass(frozen=True)
class ExportSpec:
    path: Path
    group: str  # group title
    exporter: str | None
    timezone: str | None
    dayfirst: bool | None


def _rows(path: Path) -> list[dict[str, str]]:
    if not Path(path).is_file():
        raise DataError(f"{path} not found")
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        rows = [{(k or "").strip().lower(): (v or "").strip() for k, v in r.items()} for r in reader]
    return [r for r in rows if any(r.values())]


def load_groups(path: Path) -> list[Group]:
    groups: list[Group] = []
    seen_titles: set[str] = set()
    seen_channels: dict[str, str] = {}
    singles: dict[str, str] = {}
    for n, row in enumerate(_rows(path), 2):
        where = f"{Path(path).name} line {n}"
        title, kind = row.get("title", ""), row.get("kind", "").lower()
        if not title:
            raise DataError(f"{where}: `title` is empty")
        if kind == "logs":
            raise DataError(
                f"{where}: kind 'logs' is not supported in the testbench "
                "(the agent would try to send WhatsApp messages)"
            )
        if kind not in KINDS:
            raise DataError(f"{where}: kind {kind!r} is not one of {', '.join(KINDS)}")
        if row.get("enabled", "").lower() in _FALSE:
            continue
        if title.casefold() in seen_titles:
            raise DataError(f"{where}: duplicate group title {title!r}")
        seen_titles.add(title.casefold())
        if kind in SINGLETON_KINDS:
            if kind in singles:
                raise DataError(
                    f"{where}: only one '{kind}' group is allowed ({singles[kind]!r} is already one)"
                )
            singles[kind] = title
        initiative = (row.get("initiative") or title) if kind in INITIATIVE_KINDS else None
        channel_title = initiative or title
        if slug(channel_title) in seen_channels:
            raise DataError(
                f"{where}: {title!r} and {seen_channels[slug(channel_title)]!r} would share the wiki folder "
                f"'{slug(channel_title)}' - give them different initiatives"
            )
        seen_channels[slug(channel_title)] = title
        groups.append(
            Group(
                title,
                kind,
                initiative,
                row.get("lead") or "Unassigned",
                row.get("brief", ""),
                channel_title,
                f"{slug(channel_title)}@g.us",
            )
        )
    if not groups:
        raise DataError(f"{path}: no groups")
    return groups


def disabled_titles(path: Path) -> set[str]:
    return {r.get("title", "").casefold() for r in _rows(path) if r.get("enabled", "").lower() in _FALSE}


def load_exports(path: Path, groups: list[Group], skip: set[str] | None = None) -> list[ExportSpec]:
    skip = skip or set()
    by_title = {g.title.casefold(): g for g in groups}
    base = Path(path).parent
    specs: list[ExportSpec] = []
    for n, row in enumerate(_rows(path), 2):
        where = f"{Path(path).name} line {n}"
        file, group = row.get("file", ""), row.get("group", "")
        if not file or not group:
            raise DataError(f"{where}: `file` and `group` are required")
        if group.casefold() in skip:
            continue
        if group.casefold() not in by_title:
            raise DataError(f"{where}: group {group!r} is not in groups.csv")
        p = Path(file)
        p = p if p.is_absolute() else base / p
        if not p.is_file():
            raise DataError(f"{where}: file {p} not found")
        order = row.get("dayfirst", "").lower()
        if order not in ("", "dmy", "mdy"):
            raise DataError(f"{where}: dayfirst must be dmy, mdy or blank")
        specs.append(
            ExportSpec(
                p,
                by_title[group.casefold()].title,
                row.get("exporter") or None,
                row.get("timezone") or None,
                None if not order else order == "dmy",
            )
        )
    return specs
