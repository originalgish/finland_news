from pathlib import Path

from worker.__main__ import STAGES

CRONTAB = Path(__file__).resolve().parents[1] / "worker" / "crontab"


def scheduled_stages():
    """The stage name from each scheduled command, which must be `python -m worker <stage>`."""
    stages = []
    for line in CRONTAB.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        fields = line.split()
        assert fields[5:8] == ["python", "-m", "worker"], line
        stages.append(fields[8])
    return stages


def test_every_scheduled_command_is_a_known_stage():
    assert set(scheduled_stages()) <= set(STAGES)


def test_the_schedule_runs_the_pipeline_the_digest_and_maintenance():
    assert sorted(scheduled_stages()) == ["digest", "maintenance", "run"]
