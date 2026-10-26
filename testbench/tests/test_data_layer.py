from datetime import UTC, datetime
from pathlib import Path

import pytest

from testbench.dataset import date_bounds, load_dataset
from testbench.groups import DataError, load_groups
from testbench.parse_export import parse_export

SAMPLE = Path(__file__).resolve().parents[1] / "sample_data"
IST = lambda *a: datetime(*a, tzinfo=UTC)  # noqa: E731


def test_android_24h_basic_multiline_and_system_dropped():
    text = (SAMPLE / "android_drone.txt").read_text()
    msgs, rep = parse_export(text, source="a")
    assert rep.date_order == "dmy" and rep.date_order_basis == "unambiguous"
    assert rep.system_dropped == 3  # encryption, created group, added Karan
    assert rep.deleted_dropped == 1
    first = msgs[0]
    assert first.sender == "Aman Sharma"
    assert first.text == "Team, flight controller board arrived\nWho is testing the PID tuning tomorrow?"
    # 10:15 IST == 04:45 UTC
    assert first.sent_at == IST(2025, 10, 13, 4, 45)
    assert not first.has_seconds


def test_edited_suffix_and_media_and_phone_sender():
    msgs, rep = parse_export((SAMPLE / "android_drone.txt").read_text())
    by_text = {m.text: m for m in msgs}
    assert "Uploaded the tuning notes" in by_text  # <This message was edited> stripped
    media = [m for m in msgs if m.media_kind]
    assert len(media) == 1 and media[0].text == "" and rep.media_only == 1
    assert any(m.sender == "+91 98765 43210" for m in msgs)


def test_iphone_brackets_lrm_and_seconds():
    msgs, rep = parse_export((SAMPLE / "iphone_drone.txt").read_text())
    assert rep.system_dropped == 3  # encryption notice "from" the group, "created group", "added Karan"
    assert msgs[0].text.startswith("Team, flight controller")
    assert msgs[0].has_seconds and msgs[0].sent_at == IST(2025, 10, 13, 4, 45, 42)
    assert any(m.media_kind == "image" for m in msgs)  # <attached: ...jpg>
    assert all(not m.sender.startswith("\u200e") for m in msgs)


def test_us_locale_12h_narrow_space_month_first():
    msgs, rep = parse_export((SAMPLE / "android_coordis_us.txt").read_text())
    assert rep.date_order == "mdy" and rep.date_order_basis == "unambiguous"
    assert msgs[0].sent_at == IST(2025, 10, 13, 10, 35)  # 4:05 PM IST
    assert msgs[2].sent_at == IST(2025, 10, 14, 4, 0)  # 9:30 AM IST


def test_ios_media_placeholder_from_lrm_sender_is_not_a_system_event():
    text = (
        "[13/10/2025, 10:21:00] Aman: \u200eimage omitted\n[13/10/2025, 10:22:00] Aman: \u200eadded Karan\n"
    )
    msgs, rep = parse_export(text)
    assert len(msgs) == 1 and msgs[0].media_kind == "image" and rep.system_dropped == 1


def test_a_person_saying_added_is_not_a_system_event():
    msgs, _ = parse_export("[13/10/2025, 10:21:00] Aman: added the file to drive\n")
    assert len(msgs) == 1


def test_twelve_am_pm_edge():
    text = "1/2/25, 12:05 AM - A: x\n1/2/25, 12:05 PM - A: y\n"
    msgs, _ = parse_export(text, tz="UTC", dayfirst=True)
    assert [m.sent_at.hour for m in msgs] == [0, 12]


def test_ambiguous_dates_resolved_by_chronology():
    # 03/04, 05/04, 02/05: as dd/mm: Apr3, Apr5, May2 (forward). as mm/dd: Mar4, May4, Feb5 (backwards)
    text = "03/04/2025, 10:00 - A: a\n05/04/2025, 10:00 - A: b\n02/05/2025, 10:00 - A: c\n"
    _, rep = parse_export(text)
    assert (rep.date_order, rep.date_order_basis) == ("dmy", "chronology")


def test_truly_ambiguous_uses_default_and_warns():
    text = "01/02/2025, 10:00 - A: a\n02/02/2025, 10:00 - A: b\n"
    _, rep = parse_export(text, default_dayfirst=True)
    assert rep.date_order_basis == "default" and rep.warnings
    _, rep2 = parse_export(text, default_dayfirst=False)
    assert rep2.date_order == "mdy"


def test_subject_change_with_colon_is_system():
    text = '12/10/2025, 10:00 - Aman changed the subject from "A: B" to "C"\n12/10/2025, 10:01 - Riya: hi\n'
    msgs, rep = parse_export(text)
    assert [m.sender for m in msgs] == ["Riya"] and rep.system_dropped == 1


def test_continuation_after_system_line_is_not_attached():
    text = "12/10/2025, 10:00 - Riya: hi\n12/10/2025, 10:01 - Aman added Karan\nstray line\n"
    msgs, _ = parse_export(text)
    assert msgs[0].text == "hi"


def test_merge_android_and_iphone_dedupes_keeps_repeats_and_resolves_you():
    ds = load_dataset(SAMPLE, groups=["Drone Project"])
    (gd,) = ds.groups
    texts = [m.text for m in gd.messages]
    # "ok" sent twice in the same minute by Riya in BOTH exports -> exactly two, not four
    assert texts.count("ok") == 2
    # board message appears once although both exports have it
    assert sum(t.startswith("Team, flight controller") for t in texts) == 1
    # messages only the iPhone export has are present; "You" became Aman Sharma
    assert "Karan, can you own the ESC review? Deadline Friday" in texts
    up = next(m for m in gd.messages if m.text == "Uploaded the tuning notes")
    assert up.person.display == "Aman Sharma" and up.person.wiki_title == "Aman Sharma"
    # the copy with seconds wins
    board = next(m for m in gd.messages if m.text.startswith("Team, flight"))
    assert board.sent_at.second == 42
    # sorted chronologically
    assert [m.sent_at for m in gd.messages] == sorted(m.sent_at for m in gd.messages)
    # media-only dropped by default
    assert gd.dropped_media_only >= 1


def test_aliases_phone_and_unknown_senders():
    ds = load_dataset(SAMPLE, groups=["Drone Project"])
    gd = ds.groups[0]
    phones = [p for p in gd.unknown_senders if p.display is None]
    assert len(phones) == 1 and phones[0].wa_identity.endswith("@s.whatsapp.net")
    assert not any(p.display == "Aman" for p in gd.unknown_senders)  # alias resolved
    riya = next(m for m in gd.messages if m.person.display == "Riya Verma")
    assert riya.person.wiki_title == "Riya Verma"


def test_include_media_keeps_media_only():
    ds = load_dataset(SAMPLE, groups=["Drone Project"], include_media=True)
    assert any(m.media_kind and not m.text for m in ds.groups[0].messages)


def test_date_range_is_local_and_inclusive():
    ds = load_dataset(SAMPLE, groups=["Drone Project"], date_from="2025-10-14", date_to="2025-10-15")
    texts = [m.text for m in ds.groups[0].messages]
    assert "Uploaded the tuning notes" in texts and "Sharing the ESC datasheet" in " ".join(texts)
    assert not any("flight controller" in t for t in texts)  # 13 Oct excluded
    assert not any("own the ESC review" in t for t in texts)  # 16 Oct excluded
    start, end = date_bounds("2025-10-14", "2025-10-14", "Asia/Kolkata")
    assert start == IST(2025, 10, 13, 18, 30) and end == IST(2025, 10, 14, 18, 30)


def test_disabled_group_and_its_exports_skipped():
    ds = load_dataset(SAMPLE)
    assert {g.group.title for g in ds.groups} == {"Drone Project", "Coordis"}


def test_group_channel_title_and_jid():
    groups = {g.title: g for g in load_groups(SAMPLE / "groups.csv")}
    assert groups["Drone Project"].channel_title == "Autonomous Drone"  # like /setup project <Title>
    assert groups["Drone Project"].jid == "autonomous-drone@g.us"
    assert groups["Coordis"].channel_title == "Coordis" and groups["Coordis"].initiative is None


@pytest.mark.parametrize(
    "body,needle",
    [
        ("title,kind\nA,logs\n", "logs"),
        ("title,kind\nA,wizard\n", "not one of"),
        ("title,kind\nA,coordis\nB,coordis\n", "only one"),
        ("title,kind\nA,other\nA,other\n", "duplicate"),
        ("title,kind,initiative\nA,project,X\nB,project,X\n", "share the wiki folder"),
        ("title,kind\n", "no groups"),
    ],
)
def test_groups_csv_errors(tmp_path, body, needle):
    p = tmp_path / "groups.csv"
    p.write_text(body)
    with pytest.raises(DataError, match=needle):
        load_groups(p)


def test_unknown_group_filter_errors():
    with pytest.raises(DataError, match="unknown group"):
        load_dataset(SAMPLE, groups=["Nope"])
