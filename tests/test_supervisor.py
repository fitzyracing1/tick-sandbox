"""Tests for tick timing and queue handling. Use lit/dark vocabulary only."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest import mock

import pytest

import supervisor


def test_tick_hz_constant() -> None:
    assert supervisor.TICK_HZ == 151
    assert abs(supervisor.TICK_PERIOD - (1.0 / 151)) < 1e-12


def test_tick_timing_roughly_151hz(tmp_path: Path) -> None:
    """A short run of ticks should average near 151 Hz (not slower than ~100 Hz)."""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    n = 50
    t0 = time.perf_counter()
    supervisor.run_loop(
        inbox,
        dry_run=True,
        owner="fitzyracing1",
        repo="tick-sandbox",
        use_stdin=False,
        max_ticks=n,
    )
    elapsed = time.perf_counter() - t0
    # Lower bound: should not be much slower than target (allow OS jitter)
    assert elapsed < n * supervisor.TICK_PERIOD * 3.0
    # Should have spent roughly the expected wall time (not finish instantly)
    assert elapsed >= n * supervisor.TICK_PERIOD * 0.5


def test_scan_inbox_empty(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    assert supervisor.scan_inbox(inbox) == []


def test_scan_inbox_loads_json(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    job_path = inbox / "demo.json"
    job_path.write_text(
        json.dumps({"job_id": "demo-1", "command": "true"}),
        encoding="utf-8",
    )
    jobs = supervisor.scan_inbox(inbox)
    assert len(jobs) == 1
    assert jobs[0]["job_id"] == "demo-1"
    assert jobs[0]["command"] == "true"


def test_scan_inbox_rejects_bad_json(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "bad.json").write_text("{not json", encoding="utf-8")
    jobs = supervisor.scan_inbox(inbox)
    assert jobs == []
    captured = capsys.readouterr().out
    assert "dark" in captured


def test_scan_inbox_rejects_missing_command(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "nocmd.json").write_text(json.dumps({"job_id": "x"}), encoding="utf-8")
    jobs = supervisor.scan_inbox(inbox)
    assert jobs == []
    assert "dark" in capsys.readouterr().out


def test_tick_once_dispatches_and_moves(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "j1.json").write_text(
        json.dumps({"job_id": "j1", "command": "true"}),
        encoding="utf-8",
    )
    with mock.patch.object(supervisor, "dispatch_job") as disp:
        count = supervisor.tick_once(
            inbox, dry_run=True, owner="o", repo="r", use_stdin=False
        )
    assert count == 1
    disp.assert_called_once()
    assert not (inbox / "j1.json").exists()
    assert (inbox / "processed" / "j1.json").exists()


def test_tick_once_continues_on_new_inputs(tmp_path: Path) -> None:
    """Graceful continue: multiple jobs in one tick all get dispatched."""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    for i in range(3):
        (inbox / f"j{i}.json").write_text(
            json.dumps({"job_id": f"j{i}", "command": "true"}),
            encoding="utf-8",
        )
    with mock.patch.object(supervisor, "dispatch_job") as disp:
        count = supervisor.tick_once(
            inbox, dry_run=True, owner="o", repo="r", use_stdin=False
        )
    assert count == 3
    assert disp.call_count == 3


def test_run_local_lit() -> None:
    assert supervisor.run_local("ok", "true") == "lit"


def test_run_local_dark() -> None:
    assert supervisor.run_local("bad", "false") == "dark"


def test_dry_run_logs_lit(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "local.json").write_text(
        json.dumps({"job_id": "local-1", "command": "true"}),
        encoding="utf-8",
    )
    supervisor.run_loop(
        inbox,
        dry_run=True,
        owner="o",
        repo="r",
        use_stdin=False,
        max_ticks=5,
    )
    # Give daemon thread a moment to finish
    time.sleep(0.3)
    out = capsys.readouterr().out
    assert "lit: job_id=local-1" in out or "dispatch (dry-run): job_id=local-1" in out


def test_resolve_repo_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", "fitzyracing1/tick-sandbox")
    assert supervisor.resolve_repo() == ("fitzyracing1", "tick-sandbox")
