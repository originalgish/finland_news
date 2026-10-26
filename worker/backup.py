"""Backup retention: which database dumps to keep.

scripts/backup.sh writes one dump a day, named ft-YYYY-MM-DD.dump. This keeps every dump
from the last DAILY_DAYS days, plus the Sunday dumps from the last WEEKLY_WEEKS weeks.
Anything else is deleted. Files whose names do not parse are never touched.

Standard library only. scripts/backup.sh runs this on the host, outside the containers.
"""

import sys
from datetime import date
from pathlib import Path

DAILY_DAYS = 14
WEEKLY_WEEKS = 8
SUNDAY = 6  # date.weekday() counts Monday as 0


def day_of(path: Path) -> date | None:
    """The date in a dump's name, or None when the name is not ft-YYYY-MM-DD.dump."""
    name = path.name
    if not (name.startswith("ft-") and name.endswith(".dump")):
        return None
    try:
        return date.fromisoformat(name[3:-5])
    except ValueError:
        return None


def should_keep(day: date, today: date) -> bool:
    age = (today - day).days
    if age < DAILY_DAYS:  # includes dates in the future, so a clock error never deletes a dump
        return True
    return day.weekday() == SUNDAY and age < WEEKLY_WEEKS * 7


def prune(directory: Path, today: date) -> list[Path]:
    """Delete the dumps that are no longer kept. Returns the deleted paths."""
    deleted = []
    for path in sorted(directory.glob("ft-*.dump")):
        day = day_of(path)
        if day is None or should_keep(day, today):
            continue
        path.unlink()
        deleted.append(path)
    return deleted


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2 or args[0] != "prune":
        print("usage: python3 -m worker.backup prune DIRECTORY", file=sys.stderr)
        return 2
    deleted = prune(Path(args[1]), date.today())
    print(f"backup retention: deleted {len(deleted)} old dumps")
    return 0


if __name__ == "__main__":
    sys.exit(main())
