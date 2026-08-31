from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from autotldr.cli import main
from autotldr.watch import (
    ROLLUP_NAME,
    artifact_path,
    run_once,
    status,
    watch,
)


_GENEROUS_BUDGET = 250_000


def _write_markdown(path: Path, title: str, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# {title}\n\n{body}\n", encoding="utf-8")


def test_watch_import_does_not_load_heavy_dependencies() -> None:
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
import autotldr.watch
heavy = ('numpy', 'pyarrow', 'openpyxl', 'fitz', 'pymupdf', 'h5py', 'netCDF4')
print(','.join(name for name in heavy if name in sys.modules))
""",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert probe.stdout.strip() == ""


def test_identical_resave_is_a_content_hash_noop(tmp_path: Path) -> None:
    source = tmp_path / "inbox"
    source.mkdir()
    document = source / "notes.md"
    _write_markdown(document, "Notes", "The deployment window is Friday.")

    first = run_once(source, budget=_GENEROUS_BUDGET)
    per_file = artifact_path(source, "notes.md")
    rollup = source / ".autotldr" / ROLLUP_NAME
    first_file_stat = per_file.stat()
    first_rollup_stat = rollup.stat()

    os.utime(document, None)
    second = run_once(source, budget=_GENEROUS_BUDGET)

    assert first.changed == first.succeeded == 1
    assert second.changed == 0
    assert second.unchanged == 1
    assert not second.rollup_written
    assert per_file.stat().st_mtime_ns == first_file_stat.st_mtime_ns
    assert rollup.stat().st_mtime_ns == first_rollup_stat.st_mtime_ns


def test_changed_file_updates_its_artifact_and_rollup(tmp_path: Path) -> None:
    source = tmp_path / "inbox"
    source.mkdir()
    document = source / "plan.md"
    _write_markdown(document, "Plan", "First version.")
    run_once(source, budget=_GENEROUS_BUDGET)

    _write_markdown(document, "Plan", "Second version with a new decision.")
    result = run_once(source, budget=_GENEROUS_BUDGET)

    per_file = artifact_path(source, "plan.md")
    rollup = source / ".autotldr" / ROLLUP_NAME
    assert result.changed == result.succeeded == 1
    assert result.rollup_written
    assert "Second version with a new decision." in per_file.read_text("utf-8")
    assert "Second version with a new decision." in rollup.read_text("utf-8")
    assert len(per_file.read_bytes()) <= _GENEROUS_BUDGET
    assert len(rollup.read_bytes()) <= _GENEROUS_BUDGET


def test_one_bad_file_does_not_stall_a_good_sibling(tmp_path: Path) -> None:
    source = tmp_path / "inbox"
    source.mkdir()
    _write_markdown(source / "good.md", "Good", "Addressable source material.")
    (source / "bad.png").write_bytes(b"\x89PNG\r\n\x1a\nnot-a-supported-image")

    result = run_once(source, budget=_GENEROUS_BUDGET)
    snapshot = status(source)

    assert result.scanned == 2
    assert result.succeeded == 1
    assert result.failed == 1
    assert artifact_path(source, "good.md").is_file()
    assert not artifact_path(source, "bad.png").exists()
    assert (source / ".autotldr" / ROLLUP_NAME).is_file()
    by_path = {item.path: item for item in snapshot.files}
    assert by_path["good.md"].status == "ok"
    assert by_path["bad.png"].status == "error"
    assert "UnsupportedFormat" in (by_path["bad.png"].error or "")
    assert snapshot.last_run is not None
    assert snapshot.last_run.status == "partial"


def test_store_uses_wal_and_status_is_read_only(tmp_path: Path) -> None:
    unwatched = tmp_path / "unwatched"
    unwatched.mkdir()
    empty = status(unwatched)
    assert empty.journal_mode == "absent"
    assert not (unwatched / ".autotldr").exists()

    source = tmp_path / "inbox"
    source.mkdir()
    _write_markdown(source / "one.md", "One", "One source.")
    result = run_once(source, budget=_GENEROUS_BUDGET)
    snapshot = status(source)

    assert snapshot.journal_mode.casefold() == "wal"
    assert snapshot.last_run is not None
    assert snapshot.last_run.run_id == result.run_id
    assert snapshot.last_run.status == "ok"
    with sqlite3.connect(snapshot.store) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute(
            "SELECT value FROM metadata WHERE key = 'schema'"
        ).fetchone()[0] == "autotldr-watch-store-v1"


def test_recursive_policy_and_hidden_entries_have_stable_artifacts(
    tmp_path: Path,
) -> None:
    source = tmp_path / "inbox"
    source.mkdir()
    _write_markdown(source / "top.md", "Top", "Visible top-level input.")
    _write_markdown(source / "nested" / "deep.md", "Deep", "Visible nested input.")
    _write_markdown(source / ".hidden.md", "Hidden", "Must be ignored.")
    _write_markdown(source / ".git" / "config.md", "Git", "Must be ignored.")
    generated = source / ".autotldr" / "injected.md"
    _write_markdown(generated, "Generated", "Must never re-enter acquisition.")

    nonrecursive = run_once(source, recursive=False, budget=_GENEROUS_BUDGET)
    assert nonrecursive.scanned == 1
    assert artifact_path(source, "top.md").is_file()
    assert not artifact_path(source, "nested/deep.md").exists()

    recursive = run_once(source, recursive=True, budget=_GENEROUS_BUDGET)
    assert recursive.scanned == 2
    assert artifact_path(source, "nested/deep.md").is_file()
    assert not artifact_path(source, ".hidden.md").exists()
    assert not artifact_path(source, ".git/config.md").exists()
    assert not artifact_path(source, ".autotldr/injected.md").exists()
    temporary_artifacts = list((source / ".autotldr").rglob("*.tmp"))
    assert temporary_artifacts == []


def test_source_removal_removes_only_its_generated_artifact(tmp_path: Path) -> None:
    source = tmp_path / "inbox"
    source.mkdir()
    document = source / "obsolete.md"
    _write_markdown(document, "Obsolete", "This source will be removed.")
    run_once(source, budget=_GENEROUS_BUDGET)
    generated = artifact_path(source, "obsolete.md")
    assert generated.is_file()

    document.unlink()
    result = run_once(source, budget=_GENEROUS_BUDGET)

    assert result.removed == 1
    assert result.rollup_written
    assert not generated.exists()
    assert status(source).files == ()


def test_moving_the_root_reuses_relative_keys_and_artifact_paths(
    tmp_path: Path,
) -> None:
    original = tmp_path / "original"
    original.mkdir()
    _write_markdown(original / "nested" / "guide.md", "Guide", "Move-safe input.")
    run_once(original, recursive=True, budget=_GENEROUS_BUDGET)
    original_artifact_relative = artifact_path(
        original, "nested/guide.md"
    ).relative_to(original)

    relocated = tmp_path / "relocated"
    shutil.move(original, relocated)
    rerun = run_once(relocated, recursive=True, budget=_GENEROUS_BUDGET)
    snapshot = status(relocated)

    assert rerun.changed == rerun.succeeded == 1
    assert [item.path for item in snapshot.files] == ["nested/guide.md"]
    relocated_artifact = relocated / original_artifact_relative
    assert relocated_artifact.is_file()
    rendered = relocated_artifact.read_text("utf-8")
    assert str(relocated) in rendered
    assert str(original) not in rendered


def test_continuous_mode_settle_path_waits_before_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "inbox"
    source.mkdir()
    _write_markdown(source / "stable.md", "Stable", "Complete bytes.")
    sleeps: list[float] = []

    import autotldr.watch as watch_module

    real_sleep = watch_module.time.sleep

    def recorded_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        real_sleep(0)

    monkeypatch.setattr(watch_module.time, "sleep", recorded_sleep)
    result = run_once(
        source,
        budget=_GENEROUS_BUDGET,
        settle=True,
        settle_interval=0.001,
        settle_timeout=1.0,
    )

    assert result.succeeded == 1
    assert sleeps == [0.001]


def test_watch_continuous_entry_point_stops_cleanly(tmp_path: Path) -> None:
    source = tmp_path / "inbox"
    source.mkdir()
    _write_markdown(source / "queued.md", "Queued", "Initial watch transaction.")
    stop = threading.Event()
    stop.set()

    result = watch(
        source,
        budget=_GENEROUS_BUDGET,
        poll_interval=0.001,
        settle_interval=0.001,
        settle_timeout=1.0,
        stop_event=stop,
    )

    assert result.succeeded == 1
    assert artifact_path(source, "queued.md").is_file()


def test_artifact_path_rejects_escapes(tmp_path: Path) -> None:
    source = tmp_path / "inbox"
    source.mkdir()
    with pytest.raises(ValueError, match="safe relative path"):
        artifact_path(source, "../outside.md")
    with pytest.raises(ValueError, match="safe relative path"):
        artifact_path(source, "/absolute.md")


def test_watch_cli_once_and_read_only_status(tmp_path: Path, capsys) -> None:
    source = tmp_path / "inbox"
    source.mkdir()
    _write_markdown(source / "guide.md", "Guide", "CLI watch input.")

    assert main(["watch", str(source), "--once", "--budget", "250000"]) == 0
    run_payload = __import__("json").loads(capsys.readouterr().out)
    assert run_payload["succeeded"] == 1
    assert run_payload["rollup_written"] is True

    assert main(["watch", str(source), "--status"]) == 0
    status_payload = __import__("json").loads(capsys.readouterr().out)
    assert status_payload["journal_mode"].casefold() == "wal"
    assert status_payload["last_run"]["status"] == "ok"
    assert status_payload["files"][0]["path"] == "guide.md"
