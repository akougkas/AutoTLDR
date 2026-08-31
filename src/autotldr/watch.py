"""Polling watch mode for self-summarizing folders.

The watch surface is deliberately a thin orchestration layer over the public
pipeline in :mod:`autotldr.api`.  It owns filesystem stability, content-hash
suppression, durable status, and atomic artifact publication; it does not own
extraction, fusion, synthesis, or rendering semantics.

Importing this module is dependency-free.  Extractors and renderers are loaded
only when a changed source is actually processed.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable


STORE_SCHEMA = "autotldr-watch-store-v1"
ARTIFACT_DIRECTORY = ".autotldr"
STORE_NAME = "store.db"
ROLLUP_NAME = "FOLDER.tldr.md"
FILES_DIRECTORY = "files"


class UnstableSourceError(OSError):
    """A source did not remain unchanged long enough for a safe read."""


@dataclass(frozen=True, slots=True)
class WatchError:
    """One contained watch failure recorded in the durable store."""

    run_id: int
    path: str | None
    message: str
    created_at: float


@dataclass(frozen=True, slots=True)
class WatchFileStatus:
    """Latest durable state for one root-relative source file."""

    path: str
    sha256: str | None
    size: int | None
    mtime_ns: int | None
    status: str
    error: str | None
    artifact: Path | None
    updated_at: float


@dataclass(frozen=True, slots=True)
class WatchRunStatus:
    """Durable summary of one completed scan transaction."""

    run_id: int
    started_at: float
    finished_at: float | None
    status: str
    scanned: int
    changed: int
    unchanged: int
    succeeded: int
    failed: int
    removed: int
    rollup_written: bool


@dataclass(frozen=True, slots=True)
class WatchStatus:
    """Read-only status snapshot suitable for a CLI or agent API."""

    root: Path
    store: Path
    journal_mode: str
    last_run: WatchRunStatus | None
    files: tuple[WatchFileStatus, ...]
    errors: tuple[WatchError, ...]


@dataclass(frozen=True, slots=True)
class WatchRun:
    """Result of a deterministic :func:`run_once` scan."""

    root: Path
    run_id: int
    scanned: int
    changed: int
    unchanged: int
    succeeded: int
    failed: int
    removed: int
    rollup_written: bool
    artifacts: tuple[Path, ...]
    errors: tuple[WatchError, ...]


@dataclass(frozen=True, slots=True)
class _Candidate:
    path: Path
    relative: str
    size: int
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class _FileRecord:
    relative: str
    sha256: str | None
    size: int | None
    mtime_ns: int | None
    status: str
    error: str | None
    artifact: str | None
    updated_at: float


def artifact_path(source: str | Path, relative_path: str | Path) -> Path:
    """Return the stable per-file artifact path for a root-relative source.

    The source hierarchy is mirrored below ``.autotldr/files`` and the source
    filename is retained before the ``.tldr.md`` suffix.  This avoids basename
    collisions and keeps paths stable when an entire watched root is moved.
    """

    root = Path(source).expanduser().resolve()
    relative = _validated_relative(relative_path)
    parts = relative.parts
    return (
        root
        / ARTIFACT_DIRECTORY
        / FILES_DIRECTORY
        / Path(*parts[:-1])
        / f"{parts[-1]}.tldr.md"
    )


def run_once(
    source: str | Path,
    *,
    recursive: bool = False,
    budget: int | None = None,
    settle: bool = False,
    settle_interval: float = 0.25,
    settle_timeout: float = 30.0,
) -> WatchRun:
    """Scan one folder once, processing only content that actually changed.

    This is the deterministic foreground/demo entry point.  ``settle=True`` is
    used by :func:`watch` so size and mtime must remain stable before bytes are
    opened.  A failure is scoped to its file, recorded in SQLite, and does not
    prevent successful siblings or the folder roll-up from being published.

    ``budget`` is applied independently to every complete Markdown artifact by
    the existing exact renderer.  Model synthesis is intentionally never
    enabled here; watch mode does not load or select a model.
    """

    root = _validated_root(source)
    if budget is not None and budget <= 0:
        raise ValueError("budget must be a positive integer")
    if settle_interval <= 0:
        raise ValueError("settle_interval must be positive")
    if settle_timeout <= 0:
        raise ValueError("settle_timeout must be positive")

    artifact_root = root / ARTIFACT_DIRECTORY
    artifact_root.mkdir(parents=True, exist_ok=True)
    connection = _connect_store(artifact_root / STORE_NAME)
    started_at = time.time()
    run_id = _start_run(connection, started_at)

    try:
        candidates = _scan(root, recursive=recursive)
    except Exception as exc:
        message = _safe_error(exc, root)
        _finish_fatal_run(connection, run_id, message)
        connection.close()
        raise

    previous = _load_records(connection)
    stored_root = _meta_value(connection, "root")
    relocated = stored_root is not None and stored_root != str(root)
    outcomes: dict[str, _FileRecord] = {}
    extracted: dict[str, Any] = {}
    errors: list[WatchError] = []
    artifacts: list[Path] = []
    changed = 0
    unchanged = 0
    succeeded = 0
    failed = 0

    for candidate in candidates:
        old = previous.get(candidate.relative)
        try:
            stable_candidate = (
                _wait_until_stable(
                    candidate,
                    interval=settle_interval,
                    timeout=settle_timeout,
                )
                if settle
                else candidate
            )
            digest, size, mtime_ns = _hash_snapshot(stable_candidate.path)
        except Exception as exc:
            changed += 1
            failed += 1
            message = _safe_error(exc, root)
            now = time.time()
            outcomes[candidate.relative] = _FileRecord(
                candidate.relative,
                None,
                candidate.size,
                candidate.mtime_ns,
                "error",
                message,
                old.artifact if old is not None else None,
                now,
            )
            errors.append(WatchError(run_id, candidate.relative, message, now))
            continue

        if old is not None and old.sha256 == digest and not relocated:
            unchanged += 1
            outcomes[candidate.relative] = _FileRecord(
                candidate.relative,
                digest,
                size,
                mtime_ns,
                old.status,
                old.error,
                old.artifact,
                old.updated_at,
            )
            continue

        changed += 1
        target = artifact_path(root, candidate.relative)
        try:
            from .api import summarize

            result = summarize(
                [stable_candidate.path],
                output="md",
                budget=budget,
                cite=True,
                color=False,
            )
            _atomic_write(target, result.rendered)
        except Exception as exc:
            failed += 1
            message = _safe_error(exc, root)
            now = time.time()
            outcomes[candidate.relative] = _FileRecord(
                candidate.relative,
                digest,
                size,
                mtime_ns,
                "error",
                message,
                old.artifact if old is not None else None,
                now,
            )
            errors.append(WatchError(run_id, candidate.relative, message, now))
            continue

        succeeded += 1
        now = time.time()
        artifact_relative = target.relative_to(artifact_root).as_posix()
        outcomes[candidate.relative] = _FileRecord(
            candidate.relative,
            digest,
            size,
            mtime_ns,
            "ok",
            None,
            artifact_relative,
            now,
        )
        extracted[candidate.relative] = result.extraction
        artifacts.append(target)

    current_paths = {candidate.relative for candidate in candidates}
    removed_paths = sorted(set(previous) - current_paths)
    for relative in removed_paths:
        _remove_generated_artifact(artifact_root, previous[relative].artifact)

    rollup = artifact_root / ROLLUP_NAME
    rollup_written = False
    rebuild_rollup = bool(changed or removed_paths or relocated or not rollup.exists())
    if rebuild_rollup:
        rollup_extractions: list[Any] = []
        for candidate in candidates:
            record = outcomes[candidate.relative]
            if record.status != "ok":
                continue
            extraction = extracted.get(candidate.relative)
            if extraction is None:
                try:
                    from .api import acquire

                    extraction = acquire([candidate.path])
                except Exception as exc:
                    failed += 1
                    message = _safe_error(exc, root)
                    now = time.time()
                    outcomes[candidate.relative] = _FileRecord(
                        record.relative,
                        record.sha256,
                        record.size,
                        record.mtime_ns,
                        "error",
                        message,
                        record.artifact,
                        now,
                    )
                    errors.append(
                        WatchError(run_id, candidate.relative, message, now)
                    )
                    continue
            rollup_extractions.append(extraction)

        try:
            from .api import assemble_collection
            from .render import render

            collection = assemble_collection(
                rollup_extractions,
                subject=str(root),
            )
            rendered = render(
                collection,
                output="md",
                budget=budget,
                cite=True,
                color=False,
            )
            _atomic_write(rollup, rendered)
        except Exception as exc:
            failed += 1
            message = _safe_error(exc, root)
            now = time.time()
            errors.append(WatchError(run_id, None, message, now))
        else:
            rollup_written = True
            artifacts.append(rollup)

    active_error = any(record.status == "error" for record in outcomes.values())
    status_name = "partial" if errors or active_error else "ok"
    _finish_run(
        connection,
        run_id=run_id,
        root=root,
        outcomes=outcomes,
        removed_paths=removed_paths,
        errors=errors,
        status_name=status_name,
        scanned=len(candidates),
        changed=changed,
        unchanged=unchanged,
        succeeded=succeeded,
        failed=failed,
        rollup_written=rollup_written,
    )
    connection.close()

    return WatchRun(
        root=root,
        run_id=run_id,
        scanned=len(candidates),
        changed=changed,
        unchanged=unchanged,
        succeeded=succeeded,
        failed=failed,
        removed=len(removed_paths),
        rollup_written=rollup_written,
        artifacts=tuple(artifacts),
        errors=tuple(errors),
    )


def watch(
    source: str | Path,
    *,
    recursive: bool = False,
    budget: int | None = None,
    debounce: float = 30.0,
    poll_interval: float = 1.0,
    settle_interval: float = 0.25,
    settle_timeout: float = 30.0,
    stop_event: Any | None = None,
    on_run: Callable[[WatchRun], None] | None = None,
) -> WatchRun:
    """Continuously poll a folder, debouncing each stable scan transaction.

    Polling is the guaranteed v1 implementation and works on network mounts.
    ``stop_event`` may be any object exposing ``is_set()`` and ``wait(seconds)``
    (for example :class:`threading.Event`).  The last completed run is returned
    when the event is set.  Invoke mode remains entirely independent.
    """

    if debounce < 0:
        raise ValueError("debounce must not be negative")
    if poll_interval <= 0:
        raise ValueError("poll_interval must be positive")
    root = _validated_root(source)
    observed = _scan_signature(root, recursive=recursive)
    last_run = run_once(
        root,
        recursive=recursive,
        budget=budget,
        settle=True,
        settle_interval=settle_interval,
        settle_timeout=settle_timeout,
    )
    if on_run is not None:
        on_run(last_run)

    after_initial_run = _scan_signature(root, recursive=recursive)
    pending_since: float | None = (
        time.monotonic() if after_initial_run != observed else None
    )
    observed = after_initial_run
    while not _stopped(stop_event):
        if _wait(stop_event, poll_interval):
            break
        try:
            current = _scan_signature(root, recursive=recursive)
        except OSError:
            # A temporarily unavailable mount is retried.  Existing invoke
            # mode and the last good artifacts remain usable throughout.
            continue
        now = time.monotonic()
        if current != observed:
            observed = current
            pending_since = now
            continue
        if pending_since is None or now - pending_since < debounce:
            continue
        before_run = observed
        last_run = run_once(
            root,
            recursive=recursive,
            budget=budget,
            settle=True,
            settle_interval=settle_interval,
            settle_timeout=settle_timeout,
        )
        if on_run is not None:
            on_run(last_run)
        after_run = _scan_signature(root, recursive=recursive)
        pending_since = (
            time.monotonic() if after_run != before_run else None
        )
        observed = after_run
    return last_run


def status(source: str | Path, *, error_limit: int = 50) -> WatchStatus:
    """Return a read-only durable watch status snapshot.

    Querying an unwatched directory does not create ``.autotldr`` or a store.
    Paths in file/error records are root-relative, so the status database moves
    cleanly with its watched folder.
    """

    if error_limit < 0:
        raise ValueError("error_limit must not be negative")
    root = Path(source).expanduser().resolve()
    store = root / ARTIFACT_DIRECTORY / STORE_NAME
    if not store.is_file():
        return WatchStatus(root, store, "absent", None, (), ())

    connection = sqlite3.connect(f"{store.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0])
        row = connection.execute(
            """
            SELECT run_id, started_at, finished_at, status, scanned, changed,
                   unchanged, succeeded, failed, removed, rollup_written
              FROM runs
          ORDER BY run_id DESC
             LIMIT 1
            """
        ).fetchone()
        last_run = (
            WatchRunStatus(
                int(row["run_id"]),
                float(row["started_at"]),
                None if row["finished_at"] is None else float(row["finished_at"]),
                str(row["status"]),
                int(row["scanned"]),
                int(row["changed"]),
                int(row["unchanged"]),
                int(row["succeeded"]),
                int(row["failed"]),
                int(row["removed"]),
                bool(row["rollup_written"]),
            )
            if row is not None
            else None
        )
        file_rows = connection.execute(
            """
            SELECT path, sha256, size, mtime_ns, status, error, artifact,
                   updated_at
              FROM files
          ORDER BY path
            """
        ).fetchall()
        files = tuple(
            WatchFileStatus(
                path=str(item["path"]),
                sha256=item["sha256"],
                size=item["size"],
                mtime_ns=item["mtime_ns"],
                status=str(item["status"]),
                error=item["error"],
                artifact=(
                    None
                    if item["artifact"] is None
                    else root / ARTIFACT_DIRECTORY / str(item["artifact"])
                ),
                updated_at=float(item["updated_at"]),
            )
            for item in file_rows
        )
        error_rows = connection.execute(
            """
            SELECT run_id, path, message, created_at
              FROM errors
          ORDER BY error_id DESC
             LIMIT ?
            """,
            (error_limit,),
        ).fetchall()
        errors = tuple(
            WatchError(
                int(item["run_id"]),
                item["path"],
                str(item["message"]),
                float(item["created_at"]),
            )
            for item in error_rows
        )
    finally:
        connection.close()
    return WatchStatus(root, store, journal_mode, last_run, files, errors)


def _validated_root(source: str | Path) -> Path:
    root = Path(source).expanduser().resolve()
    if not root.exists():
        raise FileNotFoundError(root)
    if not root.is_dir():
        raise NotADirectoryError(root)
    return root


def _validated_relative(value: str | Path) -> PurePosixPath:
    raw = str(value).replace(os.sep, "/")
    relative = PurePosixPath(raw)
    if (
        not raw
        or raw.startswith("/")
        or relative.is_absolute()
        or any(part in {"", ".", ".."} for part in relative.parts)
        or "\\" in raw
        or "\x00" in raw
    ):
        raise ValueError("artifact source path must be a safe relative path")
    return relative


def _scan(root: Path, *, recursive: bool) -> tuple[_Candidate, ...]:
    candidates: list[_Candidate] = []
    if recursive:
        for directory, names, filenames in os.walk(root, followlinks=False):
            names[:] = sorted(
                name
                for name in names
                if name != ARTIFACT_DIRECTORY
                and not name.startswith(".")
                and not (Path(directory) / name).is_symlink()
            )
            for name in sorted(filenames):
                if name.startswith("."):
                    continue
                _append_candidate(candidates, root, Path(directory) / name)
    else:
        with os.scandir(root) as entries:
            for entry in sorted(entries, key=lambda item: item.name):
                if entry.name == ARTIFACT_DIRECTORY or entry.name.startswith("."):
                    continue
                _append_candidate(candidates, root, Path(entry.path))
    return tuple(sorted(candidates, key=lambda item: item.relative))


def _append_candidate(
    candidates: list[_Candidate], root: Path, path: Path
) -> None:
    try:
        info = path.lstat()
    except OSError:
        # Preserve an addressable candidate for the per-file error boundary.
        relative = path.relative_to(root).as_posix()
        candidates.append(_Candidate(path, relative, 0, 0))
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return
    relative = path.relative_to(root).as_posix()
    if any(part.startswith(".") for part in PurePosixPath(relative).parts):
        return
    candidates.append(_Candidate(path, relative, info.st_size, info.st_mtime_ns))


def _scan_signature(root: Path, *, recursive: bool) -> tuple[tuple[Any, ...], ...]:
    signature: list[tuple[Any, ...]] = []
    for candidate in _scan(root, recursive=recursive):
        try:
            info = candidate.path.lstat()
            signature.append(
                (
                    candidate.relative,
                    info.st_dev,
                    info.st_ino,
                    info.st_size,
                    info.st_mtime_ns,
                    info.st_ctime_ns,
                )
            )
        except OSError:
            signature.append((candidate.relative, "unavailable"))
    return tuple(signature)


def _wait_until_stable(
    candidate: _Candidate, *, interval: float, timeout: float
) -> _Candidate:
    deadline = time.monotonic() + timeout
    previous = _size_mtime(candidate.path)
    while True:
        if time.monotonic() >= deadline:
            raise UnstableSourceError(
                f"{candidate.path.name}: size and mtime did not settle before timeout"
            )
        time.sleep(interval)
        current = _size_mtime(candidate.path)
        if current == previous:
            return _Candidate(
                candidate.path,
                candidate.relative,
                current[0],
                current[1],
            )
        previous = current


def _size_mtime(path: Path) -> tuple[int, int]:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise OSError(f"{path.name}: source is no longer a regular file")
    return info.st_size, info.st_mtime_ns


def _hash_snapshot(path: Path) -> tuple[str, int, int]:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise OSError(f"{path.name}: source is not a regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        after = os.fstat(descriptor)
        final = path.lstat()
    finally:
        os.close(descriptor)
    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    identity_final = (
        final.st_dev,
        final.st_ino,
        final.st_size,
        final.st_mtime_ns,
    )
    if identity_before != identity_after or identity_after != identity_final:
        raise UnstableSourceError(f"{path.name}: source changed while being read")
    return digest.hexdigest(), before.st_size, before.st_mtime_ns


def _connect_store(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS runs (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at REAL NOT NULL,
            finished_at REAL,
            status TEXT NOT NULL,
            scanned INTEGER NOT NULL DEFAULT 0,
            changed INTEGER NOT NULL DEFAULT 0,
            unchanged INTEGER NOT NULL DEFAULT 0,
            succeeded INTEGER NOT NULL DEFAULT 0,
            failed INTEGER NOT NULL DEFAULT 0,
            removed INTEGER NOT NULL DEFAULT 0,
            rollup_written INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS files (
            path TEXT PRIMARY KEY,
            sha256 TEXT,
            size INTEGER,
            mtime_ns INTEGER,
            status TEXT NOT NULL,
            error TEXT,
            artifact TEXT,
            updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS errors (
            error_id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL REFERENCES runs(run_id),
            path TEXT,
            message TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS errors_run_idx ON errors(run_id);
        """
    )
    with connection:
        connection.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES('schema', ?)",
            (STORE_SCHEMA,),
        )
        connection.execute(
            """
            UPDATE runs
               SET status = 'interrupted', finished_at = ?
             WHERE status = 'running'
            """,
            (time.time(),),
        )
    return connection


def _start_run(connection: sqlite3.Connection, started_at: float) -> int:
    with connection:
        cursor = connection.execute(
            "INSERT INTO runs(started_at, status) VALUES(?, 'running')",
            (started_at,),
        )
    return int(cursor.lastrowid)


def _finish_fatal_run(
    connection: sqlite3.Connection, run_id: int, message: str
) -> None:
    now = time.time()
    with connection:
        connection.execute(
            """
            UPDATE runs SET finished_at = ?, status = 'error', failed = 1
             WHERE run_id = ?
            """,
            (now, run_id),
        )
        connection.execute(
            """
            INSERT INTO errors(run_id, path, message, created_at)
                 VALUES(?, NULL, ?, ?)
            """,
            (run_id, message, now),
        )


def _finish_run(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    root: Path,
    outcomes: dict[str, _FileRecord],
    removed_paths: list[str],
    errors: list[WatchError],
    status_name: str,
    scanned: int,
    changed: int,
    unchanged: int,
    succeeded: int,
    failed: int,
    rollup_written: bool,
) -> None:
    with connection:
        connection.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES('root', ?)",
            (str(root),),
        )
        for relative in removed_paths:
            connection.execute("DELETE FROM files WHERE path = ?", (relative,))
        for record in outcomes.values():
            connection.execute(
                """
                INSERT INTO files(
                    path, sha256, size, mtime_ns, status, error, artifact,
                    updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    sha256 = excluded.sha256,
                    size = excluded.size,
                    mtime_ns = excluded.mtime_ns,
                    status = excluded.status,
                    error = excluded.error,
                    artifact = excluded.artifact,
                    updated_at = excluded.updated_at
                """,
                (
                    record.relative,
                    record.sha256,
                    record.size,
                    record.mtime_ns,
                    record.status,
                    record.error,
                    record.artifact,
                    record.updated_at,
                ),
            )
        for error in errors:
            connection.execute(
                """
                INSERT INTO errors(run_id, path, message, created_at)
                     VALUES(?, ?, ?, ?)
                """,
                (error.run_id, error.path, error.message, error.created_at),
            )
        connection.execute(
            """
            UPDATE runs
               SET finished_at = ?, status = ?, scanned = ?, changed = ?,
                   unchanged = ?, succeeded = ?, failed = ?, removed = ?,
                   rollup_written = ?
             WHERE run_id = ?
            """,
            (
                time.time(),
                status_name,
                scanned,
                changed,
                unchanged,
                succeeded,
                failed,
                len(removed_paths),
                int(rollup_written),
                run_id,
            ),
        )


def _load_records(connection: sqlite3.Connection) -> dict[str, _FileRecord]:
    rows = connection.execute(
        """
        SELECT path, sha256, size, mtime_ns, status, error, artifact, updated_at
          FROM files
      ORDER BY path
        """
    ).fetchall()
    return {
        str(row["path"]): _FileRecord(
            str(row["path"]),
            row["sha256"],
            row["size"],
            row["mtime_ns"],
            str(row["status"]),
            row["error"],
            row["artifact"],
            float(row["updated_at"]),
        )
        for row in rows
    }


def _meta_value(connection: sqlite3.Connection, key: str) -> str | None:
    row = connection.execute(
        "SELECT value FROM metadata WHERE key = ?", (key,)
    ).fetchone()
    return None if row is None else str(row[0])


def _safe_error(exc: Exception, root: Path) -> str:
    message = str(exc).replace(str(root), "<source>")
    message = " ".join(message.split()) or "no detail"
    return f"{type(exc).__name__}: {message}"[:2000]


def _atomic_write(path: Path, content: str) -> None:
    encoded = content.encode("utf-8", errors="strict")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _sync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_generated_artifact(root: Path, stored_relative: str | None) -> None:
    if stored_relative is None:
        return
    try:
        relative = _validated_relative(stored_relative)
    except ValueError:
        return
    target = root / Path(*relative.parts)
    try:
        target.unlink()
    except FileNotFoundError:
        pass


def _stopped(stop_event: Any | None) -> bool:
    return bool(stop_event is not None and stop_event.is_set())


def _wait(stop_event: Any | None, seconds: float) -> bool:
    if stop_event is None:
        time.sleep(seconds)
        return False
    return bool(stop_event.wait(seconds))


__all__ = [
    "ARTIFACT_DIRECTORY",
    "ROLLUP_NAME",
    "STORE_NAME",
    "UnstableSourceError",
    "WatchError",
    "WatchFileStatus",
    "WatchRun",
    "WatchRunStatus",
    "WatchStatus",
    "artifact_path",
    "run_once",
    "status",
    "watch",
]
