"""CSV + exports -> one clean, merged, filtered message list per group.

Several people export the same group, so exports overlap. Messages are
de-duplicated across files on (minute, person, normalised text) while keeping
genuine repeats: if one export has "ok" twice in a minute and another has it
once, the merged result has it twice (the max count over files, never the sum).
Android exports have no seconds; when the same message also appears in an
iPhone export the copy with seconds wins.

Pure standard library: no `shared/` imports.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from testbench.groups import DataError, ExportSpec, Group, disabled_titles, load_exports, load_groups
from testbench.parse_export import DEFAULT_TZ, ParsedMessage, ParseReport, parse_export_file
from testbench.people import Person, Roster


@dataclass
class Msg:
    sent_at: datetime  # aware UTC
    person: Person
    text: str
    media_kind: str | None
    sources: tuple[str, ...]


@dataclass
class GroupData:
    group: Group
    messages: list[Msg] = field(default_factory=list)
    reports: list[ParseReport] = field(default_factory=list)
    dropped_by_filter: int = 0
    dropped_media_only: int = 0
    unknown_senders: dict[Person, int] = field(default_factory=dict)


@dataclass
class Dataset:
    groups: list[GroupData]
    roster: Roster
    warnings: list[str] = field(default_factory=list)


def _canon(text: str) -> str:
    return " ".join(text.split()).casefold()


def date_bounds(
    date_from: str | None, date_to: str | None, tz: str
) -> tuple[datetime | None, datetime | None]:
    """Inclusive local dates -> [start, end) as aware UTC datetimes."""
    zone = ZoneInfo(tz)

    def parse(v: str) -> date:
        try:
            return date.fromisoformat(v)
        except ValueError:
            raise DataError(f"bad date {v!r}: use YYYY-MM-DD") from None

    start = datetime.combine(parse(date_from), time.min, zone).astimezone(UTC) if date_from else None
    end = (
        datetime.combine(parse(date_to) + timedelta(days=1), time.min, zone).astimezone(UTC)
        if date_to
        else None
    )
    return start, end


def load_dataset(
    data_dir: Path,
    *,
    groups: list[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    tz: str = DEFAULT_TZ,
    include_media: bool = False,
    default_dayfirst: bool = True,
) -> Dataset:
    data_dir = Path(data_dir)
    gpath = data_dir / "groups.csv"
    all_groups = load_groups(gpath)
    specs = load_exports(data_dir / "exports.csv", all_groups, skip=disabled_titles(gpath))
    roster = Roster.from_csv(data_dir / "members.csv")
    warnings = list(roster.warnings)

    wanted = {g.casefold() for g in groups} if groups else None
    if wanted:
        unknown = wanted - {g.title.casefold() for g in all_groups}
        if unknown:
            raise DataError(f"unknown group(s) in experiment config: {', '.join(sorted(unknown))}")
    start, end = date_bounds(date_from, date_to, tz)

    out: list[GroupData] = []
    for group in all_groups:
        if wanted is not None and group.title.casefold() not in wanted:
            continue
        gd = GroupData(group)
        files: list[tuple[ExportSpec, list[ParsedMessage]]] = []
        for spec in (s for s in specs if s.group == group.title):
            msgs, report = parse_export_file(
                spec.path, tz=spec.timezone or tz, dayfirst=spec.dayfirst, default_dayfirst=default_dayfirst
            )
            gd.reports.append(report)
            warnings += [f"{report.source}: {w}" for w in report.warnings]
            files.append((spec, msgs))
        if not files:
            warnings.append(f"group {group.title!r} has no export files in exports.csv")

        # key -> {file index -> [(file idx, msg, person)]}
        by_key: dict[tuple, dict[int, list[tuple[ParsedMessage, Person]]]] = defaultdict(
            lambda: defaultdict(list)
        )
        for i, (spec, msgs) in enumerate(files):
            for m in msgs:
                person = roster.resolve(m.sender, spec.exporter)
                key = (
                    m.sent_at.replace(second=0, microsecond=0),
                    person.key,
                    _canon(m.text),
                    m.media_kind or "",
                )
                by_key[key][i].append((m, person))

        merged: list[tuple[int, Msg, int]] = []
        for per_file in by_key.values():
            fi, best = max(per_file.items(), key=lambda kv: (len(kv[1]), kv[1][0][0].has_seconds, -kv[0]))
            sources = tuple(sorted({files[i][0].path.name for i in per_file}))
            for m, person in best:
                merged.append((fi, Msg(m.sent_at, person, m.text, m.media_kind, sources), m.line_no))
        merged.sort(key=lambda t: (t[1].sent_at, t[0], t[2]))

        for _, msg, _ in merged:
            if (start and msg.sent_at < start) or (end and msg.sent_at >= end):
                gd.dropped_by_filter += 1
                continue
            if not msg.text.strip() and not include_media:
                gd.dropped_media_only += 1
                continue
            gd.messages.append(msg)
            if not roster.is_known(msg.person):
                gd.unknown_senders[msg.person] = gd.unknown_senders.get(msg.person, 0) + 1
        out.append(gd)
    return Dataset(out, roster, warnings)
