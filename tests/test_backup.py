from datetime import date, timedelta

import pytest

from worker.backup import day_of, main, prune, should_keep

TODAY = date(2026, 10, 26)  # a Monday


def last_sunday_before(today, weeks_ago=0):
    """The Sunday weeks_ago weeks before today's most recent Sunday (today itself is not one)."""
    days_back = (today.weekday() - 6) % 7 or 7
    return today - timedelta(days=days_back + 7 * weeks_ago)


def test_every_dump_from_the_last_fourteen_days_is_kept():
    for age in range(14):
        assert should_keep(TODAY - timedelta(days=age), TODAY)


def test_an_older_weekday_dump_is_deleted():
    wednesday = TODAY - timedelta(days=20)

    assert wednesday.weekday() != 6
    assert not should_keep(wednesday, TODAY)


def test_sunday_dumps_are_kept_for_eight_weeks():
    assert should_keep(last_sunday_before(TODAY, weeks_ago=0), TODAY)
    assert should_keep(last_sunday_before(TODAY, weeks_ago=7), TODAY)
    assert not should_keep(last_sunday_before(TODAY, weeks_ago=8), TODAY)


def test_a_future_dated_dump_is_kept():
    assert should_keep(TODAY + timedelta(days=3), TODAY)


def test_prune_deletes_only_dumps_that_fall_outside_the_rules(tmp_path):
    old_weekday = TODAY - timedelta(days=20)
    while old_weekday.weekday() == 6:
        old_weekday -= timedelta(days=1)
    kept_sunday = last_sunday_before(TODAY, weeks_ago=2)
    names = {
        f"ft-{TODAY.isoformat()}.dump": True,
        f"ft-{old_weekday.isoformat()}.dump": False,
        f"ft-{kept_sunday.isoformat()}.dump": True,
    }
    for name in names:
        (tmp_path / name).write_bytes(b"dump")
    (tmp_path / "notes.txt").write_text("keep me")
    (tmp_path / "ft-not-a-date.dump").write_bytes(b"keep me")
    (tmp_path / "ft-2026-13-40.dump").write_bytes(b"keep me")

    deleted = prune(tmp_path, TODAY)

    assert [path.name for path in deleted] == [f"ft-{old_weekday.isoformat()}.dump"]
    remaining = sorted(path.name for path in tmp_path.iterdir())
    assert f"ft-{old_weekday.isoformat()}.dump" not in remaining
    assert "notes.txt" in remaining
    assert "ft-not-a-date.dump" in remaining
    assert "ft-2026-13-40.dump" in remaining


def test_day_of_reads_only_well_formed_names(tmp_path):
    assert day_of(tmp_path / "ft-2026-10-26.dump") == date(2026, 10, 26)
    assert day_of(tmp_path / "ft-2026-10-26.dump.partial") is None
    assert day_of(tmp_path / "other-2026-10-26.dump") is None


def test_main_needs_the_prune_command_and_a_directory(capsys):
    assert main(["prune"]) == 2
    assert "usage" in capsys.readouterr().err


@pytest.mark.parametrize("bad", [["list", "dir"], []])
def test_main_rejects_other_arguments(bad):
    assert main(bad) == 2
