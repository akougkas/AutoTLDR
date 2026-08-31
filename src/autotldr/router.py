"""Format detection and lazy extractor dispatch.

Two rules this module exists to enforce:

1. **Native format beats conversion.** An XLSX routed through a markdown
   converter loses its formula graph, which is the thing that carries the
   meaning. Same for DOCX comments and notebook outputs. Each format goes to the
   extractor that understands it natively.

2. **Nothing is imported until it is needed.** The extractor module is resolved
   by name and imported at call time, so ``autotldr notes.md`` never pays for
   pymupdf or openpyxl. This is what keeps the cold-start contract honest.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from .unit import (
    Extraction,
    Gap,
    GroundedStatement,
    Origin,
    Relation,
    Role,
    Unit,
)


class Extractor(Protocol):
    def extract(self, path: Path) -> Extraction: ...


class HttpRequestObserver(Protocol):
    """Observe one HTTP request before any bytes leave the process."""

    def __call__(self, operation: str, url: str) -> bool | None: ...


class HttpRequestLimitExceeded(RuntimeError):
    """A caller-owned HTTP request budget refused the next request."""


@dataclass(frozen=True, slots=True)
class Handler:
    """A format AutoTLDR knows how to read."""

    kind: str
    module: str
    tier: int
    extra: str | None = None
    """The optional-dependency group this handler needs, if any."""
    extension_name: str | None = None
    """Explicit registry name when this handler is community supplied."""


_SNAPSHOT_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class _LocalSnapshot:
    """One private, byte-exact local acquisition kept alive for dispatch."""

    path: Path
    directory: Path
    byte_count: int
    digest: str
    original_identity: tuple[int, int, int, int, int, int]
    database_sidecars: tuple["_SidecarObservation", ...]


@dataclass(frozen=True, slots=True)
class _SidecarObservation:
    """One logical database companion's state at the start of acquisition."""

    kind: str
    suffix: str
    status: str
    size: int | None = None
    errno: int | None = None


class _LocalSnapshotContext:
    """Acquire a regular file once and expose only its private stable copy."""

    __slots__ = ("logical_path", "_temporary")

    def __init__(self, logical_path: Path) -> None:
        self.logical_path = logical_path
        self._temporary = None

    def __enter__(self) -> _LocalSnapshot:
        # These imports stay below local-path acquisition so importing the
        # router remains free of even optional-parser reachability and the
        # cheapest CLI path pays only for facilities it actually uses.
        import hashlib
        import os
        import stat
        import tempfile

        path = self.logical_path
        temporary = tempfile.TemporaryDirectory(prefix="autotldr-snapshot-")
        self._temporary = temporary
        directory = Path(temporary.name)
        source_fd: int | None = None
        snapshot_fd: int | None = None
        try:
            os.chmod(directory, 0o700)
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            # Opening a FIFO read-only can block before fstat has a chance to
            # reject it. O_NONBLOCK is ignored for regular files and lets this
            # boundary remain file-only under a path-replacement race.
            flags |= getattr(os, "O_NONBLOCK", 0)
            source_fd = os.open(path, flags)
            before = os.fstat(source_fd)
            if not stat.S_ISREG(before.st_mode):
                raise ValueError(f"{path.name}: input is not a regular file")

            path_before = path.stat()
            identity = _stat_identity(before)
            if _stat_identity(path_before) != identity:
                raise ValueError(
                    f"{path.name}: source changed while AutoTLDR was opening it; retry"
                )

            # Record both database families before the first source byte is
            # copied. Detection later decides which family, if either, is
            # relevant, so an unrelated `notes.txt-wal` never rejects text.
            database_sidecars = _observe_database_sidecars(path)

            snapshot_path = directory / path.name
            snapshot_fd = os.open(
                snapshot_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
            os.fchmod(snapshot_fd, 0o600)
            digest = hashlib.sha256()
            byte_count = 0
            while chunk := os.read(source_fd, _SNAPSHOT_CHUNK_BYTES):
                digest.update(chunk)
                byte_count += len(chunk)
                _write_all(snapshot_fd, chunk)

            after = os.fstat(source_fd)
            try:
                path_after = path.stat()
            except OSError as exc:
                raise ValueError(
                    f"{path.name}: source changed while AutoTLDR was reading it; retry"
                ) from exc
            if (
                _stat_identity(after) != identity
                or _stat_identity(path_after) != identity
                or byte_count != before.st_size
            ):
                raise ValueError(
                    f"{path.name}: source changed while AutoTLDR was reading it; retry"
                )

            # The writer descriptor has finished. Seal the shared inode before
            # any detector, native library, or extension receives a pathname.
            # This hardens against accidental writes; it is not an in-process
            # sandbox against code intentionally changing its own permissions.
            os.fchmod(snapshot_fd, 0o400)
            os.close(snapshot_fd)
            snapshot_fd = None
            os.close(source_fd)
            source_fd = None
            return _LocalSnapshot(
                path=snapshot_path,
                directory=directory,
                byte_count=byte_count,
                digest=digest.hexdigest(),
                original_identity=identity,
                database_sidecars=database_sidecars,
            )
        except Exception as exc:
            if snapshot_fd is not None:
                os.close(snapshot_fd)
            if source_fd is not None:
                os.close(source_fd)
            try:
                temporary.cleanup()
            except OSError:
                pass
            self._temporary = None
            message = _scrub_materialized_message(
                str(exc),
                source=str(path),
                physical_paths=(str(directory / path.name),),
                private_root=str(directory),
            )
            if isinstance(exc, ValueError):
                raise ValueError(message) from None
            if isinstance(exc, OSError):
                raise _logicalized_os_error(
                    exc,
                    source=str(path),
                    physical_paths=(str(directory / path.name),),
                    private_root=str(directory),
                ) from None
            raise

    def __exit__(self, exc_type, exc, traceback) -> bool:
        temporary = self._temporary
        self._temporary = None
        if temporary is not None:
            try:
                temporary.cleanup()
            except OSError:
                if exc_type is None:
                    raise OSError(
                        f"{self.logical_path}: could not remove private input snapshot"
                    ) from None
        return False


def _local_snapshot(path: Path) -> _LocalSnapshotContext:
    return _LocalSnapshotContext(path)


def _stat_identity(value) -> tuple[int, int, int, int, int, int]:
    """Metadata diagnostics around acquisition; the copy is the integrity proof."""

    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _write_all(fd: int, payload: bytes) -> None:
    import os

    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:  # pragma: no cover - defensive OS contract guard
            raise OSError("snapshot write made no progress")
        view = view[written:]


# Extension to handler. Only what v1 actually implements is wired up; the rest
# of the tiered surface in MATRIX.md gets added here one row at a time.
_BY_SUFFIX: dict[str, Handler] = {
    ".md": Handler("markdown", "autotldr.extract.text", tier=0),
    ".markdown": Handler("markdown", "autotldr.extract.text", tier=0),
    ".txt": Handler("text", "autotldr.extract.text", tier=0),
    ".rst": Handler("rst", "autotldr.extract.rst", tier=0),
    ".json": Handler("json", "autotldr.extract.structured", tier=0),
    ".jsonl": Handler("jsonl", "autotldr.extract.structured", tier=0),
    ".ndjson": Handler("jsonl", "autotldr.extract.structured", tier=0),
    ".yaml": Handler("yaml", "autotldr.extract.structured", tier=0, extra="structured"),
    ".yml": Handler("yaml", "autotldr.extract.structured", tier=0, extra="structured"),
    ".toml": Handler("toml", "autotldr.extract.structured", tier=0),
    ".xml": Handler("xml", "autotldr.extract.structured", tier=0),
    ".csv": Handler("csv", "autotldr.extract.structured", tier=0),
    ".tsv": Handler("tsv", "autotldr.extract.structured", tier=0),
    ".py": Handler("python", "autotldr.extract.code", tier=0),
    ".pyi": Handler("python", "autotldr.extract.code", tier=0),
    ".js": Handler("source", "autotldr.extract.code", tier=0),
    ".jsx": Handler("source", "autotldr.extract.code", tier=0),
    ".ts": Handler("source", "autotldr.extract.code", tier=0),
    ".tsx": Handler("source", "autotldr.extract.code", tier=0),
    ".c": Handler("source", "autotldr.extract.code", tier=0),
    ".h": Handler("source", "autotldr.extract.code", tier=0),
    ".cc": Handler("source", "autotldr.extract.code", tier=0),
    ".cpp": Handler("source", "autotldr.extract.code", tier=0),
    ".cxx": Handler("source", "autotldr.extract.code", tier=0),
    ".hpp": Handler("source", "autotldr.extract.code", tier=0),
    ".java": Handler("source", "autotldr.extract.code", tier=0),
    ".rs": Handler("source", "autotldr.extract.code", tier=0),
    ".go": Handler("source", "autotldr.extract.code", tier=0),
    ".rb": Handler("source", "autotldr.extract.code", tier=0),
    ".php": Handler("source", "autotldr.extract.code", tier=0),
    ".swift": Handler("source", "autotldr.extract.code", tier=0),
    ".kt": Handler("source", "autotldr.extract.code", tier=0),
    ".kts": Handler("source", "autotldr.extract.code", tier=0),
    ".cs": Handler("source", "autotldr.extract.code", tier=0),
    ".sh": Handler("source", "autotldr.extract.code", tier=0),
    ".bash": Handler("source", "autotldr.extract.code", tier=0),
    ".zsh": Handler("source", "autotldr.extract.code", tier=0),
    ".fish": Handler("source", "autotldr.extract.code", tier=0),
    ".sql": Handler("source", "autotldr.extract.code", tier=0),
    ".scala": Handler("source", "autotldr.extract.code", tier=0),
    ".lua": Handler("source", "autotldr.extract.code", tier=0),
    ".pl": Handler("source", "autotldr.extract.code", tier=0),
    ".r": Handler("source", "autotldr.extract.code", tier=0),
    ".dart": Handler("source", "autotldr.extract.code", tier=0),
    ".ex": Handler("source", "autotldr.extract.code", tier=0),
    ".exs": Handler("source", "autotldr.extract.code", tier=0),
    ".clj": Handler("source", "autotldr.extract.code", tier=0),
    ".cljs": Handler("source", "autotldr.extract.code", tier=0),
    ".hs": Handler("source", "autotldr.extract.code", tier=0),
    ".fs": Handler("source", "autotldr.extract.code", tier=0),
    ".fsx": Handler("source", "autotldr.extract.code", tier=0),
    ".vb": Handler("source", "autotldr.extract.code", tier=0),
    ".m": Handler("source", "autotldr.extract.code", tier=0),
    ".mm": Handler("source", "autotldr.extract.code", tier=0),
    ".groovy": Handler("source", "autotldr.extract.code", tier=0),
    ".vue": Handler("source", "autotldr.extract.code", tier=0),
    ".svelte": Handler("source", "autotldr.extract.code", tier=0),
    ".html": Handler("html", "autotldr.extract.html", tier=1),
    ".htm": Handler("html", "autotldr.extract.html", tier=1),
    ".docx": Handler("docx", "autotldr.extract.docx", tier=1),
    ".ipynb": Handler("notebook", "autotldr.extract.notebook", tier=1),
    ".tex": Handler("latex", "autotldr.extract.latex", tier=1),
    ".epub": Handler("epub", "autotldr.extract.epub", tier=1),
    ".xlsx": Handler("xlsx", "autotldr.extract.xlsx", tier=3, extra="office"),
    ".xlsm": Handler("xlsx", "autotldr.extract.xlsx", tier=3, extra="office"),
    ".parquet": Handler("parquet", "autotldr.extract.tier3", tier=3, extra="data"),
    ".pq": Handler("parquet", "autotldr.extract.tier3", tier=3, extra="data"),
    ".sqlite": Handler("sqlite", "autotldr.extract.tier3", tier=3, extra="data"),
    ".sqlite3": Handler("sqlite", "autotldr.extract.tier3", tier=3, extra="data"),
    ".db3": Handler("sqlite", "autotldr.extract.tier3", tier=3, extra="data"),
    ".s3db": Handler("sqlite", "autotldr.extract.tier3", tier=3, extra="data"),
    ".duckdb": Handler("duckdb", "autotldr.extract.tier3", tier=3, extra="data"),
    ".h5": Handler("hdf5", "autotldr.extract.tier3", tier=3, extra="data"),
    ".hdf5": Handler("hdf5", "autotldr.extract.tier3", tier=3, extra="data"),
    ".hdf": Handler("hdf5", "autotldr.extract.tier3", tier=3, extra="data"),
    ".he5": Handler("hdf5", "autotldr.extract.tier3", tier=3, extra="data"),
    ".nc": Handler("netcdf", "autotldr.extract.tier3", tier=3, extra="data"),
    ".nc4": Handler("netcdf", "autotldr.extract.tier3", tier=3, extra="data"),
    ".netcdf": Handler("netcdf", "autotldr.extract.tier3", tier=3, extra="data"),
    ".npy": Handler("numpy", "autotldr.extract.scientific_arrays", tier=3, extra="data"),
    ".npz": Handler("numpy", "autotldr.extract.scientific_arrays", tier=3, extra="data"),
    ".fits": Handler("fits", "autotldr.extract.astronomy", tier=3),
    ".fts": Handler("fits", "autotldr.extract.astronomy", tier=3),
    ".arrow": Handler("arrow", "autotldr.extract.columnar_interchange", tier=3, extra="data"),
    ".arrows": Handler("arrow-stream", "autotldr.extract.columnar_interchange", tier=3, extra="data"),
    ".feather": Handler("feather", "autotldr.extract.columnar_interchange", tier=3, extra="data"),
    ".orc": Handler("orc", "autotldr.extract.columnar_interchange", tier=3, extra="data"),
    ".pdf": Handler("pdf", "autotldr.extract.pdf", tier=1, extra="pdf"),
}

# Formats named in MATRIX.md that v1 deliberately does not handle. Meeting one
# is not a failure: AutoTLDR says which format it was and which tier owns it,
# then moves on. In a folder run, one unsupported file never stalls the fusion.
_DEFERRED: dict[str, tuple[str, int]] = {
    ".zip": ("archive", 2),
    ".tar": ("archive", 2),
    ".tgz": ("archive", 2),
    ".gz": ("archive", 2),
    # .fit is deliberately ambiguous between FITS astronomy and Garmin FIT
    # activity files. Native byte signatures can route FITS; suffix alone cannot.
    ".fit": ("FITS astronomy or Garmin FIT activity data", 3),
    # .db is deliberately ambiguous between SQLite and DuckDB. Native byte
    # signatures can still route it; suffix alone cannot.
    ".db": ("database", 3),
    ".png": ("image", 4),
    ".jpg": ("image", 4),
    ".jpeg": ("image", 4),
    ".gif": ("image", 4),
    ".pptx": ("slides", 4),
    ".wav": ("audio", 5),
    ".mp3": ("audio", 5),
    ".m4a": ("audio", 5),
    ".mp4": ("video", 5),
    ".mov": ("video", 5),
    ".so": ("binary", 6),
    ".bin": ("binary", 6),
    ".exe": ("binary", 6),
}

# Keep this small catalog synchronized with code.py's deliberately unavailable
# native grammar inventory without importing that optional parser on the router
# cold-start path.  These suffixes still route to code.py so callers receive a
# precise named decline; they are not advertised as implemented support.  The
# display values are also the one cold-path authority for explicit type hints.
_UNAVAILABLE_SOURCE_NAMES = {
    ".swift": "Swift",
    ".zsh": "Zsh",
    ".fish": "Fish",
    ".dart": "Dart",
    ".clj": "Clojure",
    ".cljs": "ClojureScript",
    ".fs": "F#",
    ".fsx": "F#",
    ".vb": "Visual Basic",
    ".groovy": "Groovy",
    ".vue": "Vue",
    ".svelte": "Svelte",
    ".lua": "Lua",
    ".mm": "Objective-C++",
}
_UNAVAILABLE_SOURCE_SUFFIXES = frozenset(_UNAVAILABLE_SOURCE_NAMES)

# HTTP routing is deliberately table-driven.  A recognized media identity is
# stronger than a URL suffix, while generic transport types allow a suffix to
# recover the native format.  The suffix selects the existing lazy Handler.
_HTTP_NATIVE_MEDIA: dict[str, str] = {
    "text/plain": ".txt",
    "text/markdown": ".md",
    "application/markdown": ".md",
    "text/x-rst": ".rst",
    "text/prs.fallenstein.rst": ".rst",
    "application/json": ".json",
    "application/ld+json": ".json",
    "application/x-ndjson": ".jsonl",
    "application/ndjson": ".jsonl",
    "application/jsonl": ".jsonl",
    "application/x-jsonlines": ".jsonl",
    "text/x-jsonl": ".jsonl",
    "application/yaml": ".yaml",
    "application/x-yaml": ".yaml",
    "text/yaml": ".yaml",
    "text/x-yaml": ".yaml",
    "application/toml": ".toml",
    "application/x-toml": ".toml",
    "text/toml": ".toml",
    "application/xml": ".xml",
    "text/xml": ".xml",
    "text/csv": ".csv",
    "application/csv": ".csv",
    "text/x-csv": ".csv",
    "text/tab-separated-values": ".tsv",
    "text/tsv": ".tsv",
    "text/x-tex": ".tex",
    "text/latex": ".tex",
    "application/x-tex": ".tex",
    "application/x-latex": ".tex",
    "application/x-ipynb+json": ".ipynb",
    "application/x-jupyter-notebook+json": ".ipynb",
    "application/vnd.jupyter": ".ipynb",
    "text/html": ".html",
    "application/xhtml+xml": ".html",
    "application/pdf": ".pdf",
    "application/epub+zip": ".epub",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-excel.sheet.macroenabled.12": ".xlsm",
    "application/vnd.apache.parquet": ".parquet",
    "application/vnd.sqlite3": ".sqlite",
    "application/x-sqlite3": ".sqlite",
    "application/x-hdf5": ".h5",
    "application/x-netcdf": ".nc",
    # Official IANA registered science media types (https://www.iana.org/assignments/media-types/media-types.xhtml)
    "application/fits": ".fits",
    "image/fits": ".fits",
    "application/vnd.apache.arrow.file": ".arrow",
    "application/vnd.apache.arrow.stream": ".arrows",
    "text/javascript": ".js",
    "application/javascript": ".js",
    "application/x-javascript": ".js",
    "text/jsx": ".jsx",
    "text/typescript": ".ts",
    "application/typescript": ".ts",
    "application/x-typescript": ".ts",
    "text/tsx": ".tsx",
    "text/x-python": ".py",
    "application/x-python": ".py",
    "text/x-c": ".c",
    "text/x-csrc": ".c",
    "text/x-c++": ".cpp",
    "text/x-c++src": ".cpp",
    "text/x-java-source": ".java",
    "text/x-rust": ".rs",
    "text/rust": ".rs",
    "text/x-go": ".go",
    "text/x-ruby": ".rb",
    "application/x-ruby": ".rb",
    "text/x-php": ".php",
    "application/x-php": ".php",
    "text/x-kotlin": ".kt",
    "text/x-csharp": ".cs",
    "text/x-shellscript": ".sh",
    "application/x-sh": ".sh",
    "text/sql": ".sql",
    "application/sql": ".sql",
    "text/x-scala": ".scala",
    "text/x-lua": ".lua",
    "text/x-perl": ".pl",
    "application/x-perl": ".pl",
    "text/x-r-source": ".r",
    "text/x-elixir": ".ex",
    "text/x-haskell": ".hs",
    "text/x-objective-c": ".m",
}

_HTTP_GENERIC_MEDIA = frozenset(
    {
        None,
        "text/plain",
        "application/octet-stream",
        "binary/octet-stream",
        "application/binary",
    }
)

_HTTP_DEFERRED_MEDIA: dict[str, tuple[str, int]] = {
    "application/zip": ("archive", 2),
    "application/x-tar": ("archive", 2),
    "application/gzip": ("archive", 2),
    "application/x-gzip": ("archive", 2),
    "application/x-7z-compressed": ("archive", 2),
    "application/vnd.rar": ("archive", 2),
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": (
        "slides",
        4,
    ),
}

_HTTP_TRANSCODE_SUFFIXES = frozenset(
    {
        ".txt",
        ".md",
        ".markdown",
        ".rst",
        ".yaml",
        ".yml",
        ".csv",
        ".tsv",
        ".tex",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".c",
        ".h",
        ".cc",
        ".cpp",
        ".cxx",
        ".hpp",
        ".java",
        ".rs",
        ".go",
        ".rb",
        ".php",
        ".kt",
        ".kts",
        ".cs",
        ".sh",
        ".bash",
        ".sql",
        ".scala",
        ".lua",
        ".pl",
        ".r",
        ".ex",
        ".exs",
        ".hs",
        ".m",
    }
)


class UnsupportedFormat(Exception):
    """Raised for inputs v1 knowingly declines, with enough detail to be useful."""

    def __init__(self, path: Path | str, kind: str, tier: int) -> None:
        self.path = path
        self.kind = kind
        self.tier = tier
        label = path.name if isinstance(path, Path) else path
        super().__init__(
            f"{label}: {kind} input is tier {tier}, which the current invoke "
            "path does not read"
        )


class UnknownFormat(Exception):
    def __init__(self, path: Path | str) -> None:
        self.path = path
        label = path.name if isinstance(path, Path) else path
        super().__init__(f"{label}: unrecognized format")


_CANONICAL_SUFFIX: dict[str, str] = {
    "text": ".txt",
    "txt": ".txt",
    "markdown": ".md",
    "md": ".md",
    "rst": ".rst",
    "json": ".json",
    "jsonl": ".jsonl",
    "ndjson": ".jsonl",
    "yaml": ".yaml",
    "yml": ".yaml",
    "toml": ".toml",
    "xml": ".xml",
    "csv": ".csv",
    "tsv": ".tsv",
    "html": ".html",
    "htm": ".html",
    "python": ".py",
    "py": ".py",
    "javascript": ".js",
    "js": ".js",
    "jsx": ".jsx",
    "typescript": ".ts",
    "ts": ".ts",
    "tsx": ".tsx",
    "c": ".c",
    "cpp": ".cpp",
    "c++": ".cpp",
    "java": ".java",
    "rust": ".rs",
    "rs": ".rs",
    "go": ".go",
    "ruby": ".rb",
    "rb": ".rb",
    "php": ".php",
    "kotlin": ".kt",
    "kt": ".kt",
    "csharp": ".cs",
    "cs": ".cs",
    "bash": ".sh",
    "sh": ".sh",
    "sql": ".sql",
    "scala": ".scala",
    "lua": ".lua",
    "swift": ".swift",
    "zsh": ".zsh",
    "fish": ".fish",
    "dart": ".dart",
    "clojure": ".clj",
    "clj": ".clj",
    "clojurescript": ".cljs",
    "cljs": ".cljs",
    "fsharp": ".fs",
    "f#": ".fs",
    "fs": ".fs",
    "fsx": ".fsx",
    "visual-basic": ".vb",
    "visualbasic": ".vb",
    "vb": ".vb",
    "groovy": ".groovy",
    "vue": ".vue",
    "svelte": ".svelte",
    "objective-c++": ".mm",
    "objc++": ".mm",
    "mm": ".mm",
    "perl": ".pl",
    "r": ".r",
    "elixir": ".ex",
    "haskell": ".hs",
    "hs": ".hs",
    "objective-c": ".m",
    "objc": ".m",
    "pdf": ".pdf",
    "docx": ".docx",
    "notebook": ".ipynb",
    "ipynb": ".ipynb",
    "latex": ".tex",
    "tex": ".tex",
    "epub": ".epub",
    "xlsx": ".xlsx",
    "parquet": ".parquet",
    "pq": ".parquet",
    "sqlite": ".sqlite",
    "sqlite3": ".sqlite",
    "db3": ".sqlite",
    "s3db": ".sqlite",
    "duckdb": ".duckdb",
    "hdf5": ".h5",
    "hdf": ".h5",
    "h5": ".h5",
    "he5": ".h5",
    "netcdf": ".nc",
    "netcdf4": ".nc",
    "nc": ".nc",
    "nc4": ".nc",
    "numpy": ".npy",
    "npy": ".npy",
    "npz": ".npz",
    "fits": ".fits",
    "fts": ".fits",
    "arrow": ".arrow",
    "arrow-file": ".arrow",
    "arrow-stream": ".arrows",
    "arrows": ".arrows",
    "feather": ".feather",
    "feather-v2": ".feather",
    "orc": ".orc",
}


_BUILTIN_STRONG_SIGNATURES: tuple[tuple[int, bytes, bytes], ...] = tuple(
    (offset, pattern, b"\xff" * len(pattern))
    for offset, pattern in (
        (0, b"%PDF-"),
        (0, b"PK\x03\x04"),
        (0, b"PK\x05\x06"),
        (0, b"PK\x07\x08"),
        (0, b"PAR1"),
        (-4, b"PAR1"),
        (0, b"SQLite format 3\x00"),
        (8, b"DUCK"),
        (0, b"\x89HDF\r\n\x1a\n"),
        (0, b"CDF\x01"),
        (0, b"CDF\x02"),
        (0, b"CDF\x05"),
        (0, b"\x93NUMPY"),
        (0, b"SIMPLE  ="),
        (0, b"ARROW1"),
        (-6, b"ARROW1"),
        (0, b"FEA1"),
        (-4, b"FEA1"),
        (0, b"ORC"),
    )
)


def validate_extension_registry(registry: object) -> None:
    """Reject extension claims that overlap implemented core routing keys.

    Deferred and unavailable formats are deliberately *not* reserved: an
    explicitly enabled community adapter is allowed to replace a named
    decline.  Implemented native routes remain core-owned, including their
    explicit type names, suffixes, media types, and strong byte identities.
    """

    from .extensions import (
        ExtensionCollisionError,
        ExtensionRegistry,
    )

    if not isinstance(registry, ExtensionRegistry):
        raise TypeError("registry must be an ExtensionRegistry")

    builtin_suffixes = frozenset(_BY_SUFFIX) - _UNAVAILABLE_SOURCE_SUFFIXES
    builtin_names = {
        handler.kind
        for suffix, handler in _BY_SUFFIX.items()
        if suffix in builtin_suffixes
    }
    builtin_names.update(
        name
        for name, suffix in _CANONICAL_SUFFIX.items()
        if suffix in builtin_suffixes
    )
    builtin_media = {
        media_type
        for media_type, suffix in _HTTP_NATIVE_MEDIA.items()
        if suffix in builtin_suffixes
    }

    for spec in registry.extractors:
        claimed_names = {spec.name, *spec.kinds, *spec.aliases}
        if overlap := sorted(claimed_names & builtin_names):
            raise ExtensionCollisionError(
                f"extractor {spec.name!r} collides with implemented core "
                f"name/kind {overlap[0]!r}"
            )
        if overlap := sorted(set(spec.suffixes) & builtin_suffixes):
            raise ExtensionCollisionError(
                f"extractor {spec.name!r} collides with implemented core "
                f"suffix {overlap[0]!r}"
            )
        if overlap := sorted(set(spec.media_types) & builtin_media):
            raise ExtensionCollisionError(
                f"extractor {spec.name!r} collides with implemented core "
                f"media type {overlap[0]!r}"
            )
        for probe in spec.signatures:
            if any(
                _signature_claims_overlap(probe.collision_key, builtin)
                for builtin in _BUILTIN_STRONG_SIGNATURES
            ):
                raise ExtensionCollisionError(
                    f"extractor {spec.name!r} collides with an implemented "
                    "core strong signature"
                )


def _signature_claims_overlap(
    left: tuple[int, bytes, bytes],
    right: tuple[int, bytes, bytes],
) -> bool:
    left_offset, left_pattern, left_mask = left
    right_offset, right_pattern, right_mask = right
    if left_offset != right_offset:
        return False
    width = min(len(left_pattern), len(right_pattern))
    return all(
        ((left_pattern[index] ^ right_pattern[index])
         & left_mask[index]
         & right_mask[index])
        == 0
        for index in range(width)
    )


def _extension_handler(spec: object) -> Handler:
    return Handler(
        kind=str(getattr(spec, "name")),
        module=str(getattr(spec, "module")),
        tier=int(getattr(spec, "tier")),
        extra=getattr(spec, "extra"),
        extension_name=str(getattr(spec, "name")),
    )


def input_type_names(*, registry: object | None = None) -> tuple[str, ...]:
    """Accepted routing hints, including hints that yield a named decline.

    Hint acceptance is not an implementation claim; callers that advertise
    native coverage should use :func:`supported_suffixes`.  For example, Lua
    remains accepted so ``--type lua`` can explain its unavailable pinned
    grammar precisely, while ``.lua`` is intentionally not advertised.
    """

    names = set(_CANONICAL_SUFFIX)
    if registry is not None:
        validate_extension_registry(registry)
        for spec in registry.extractors:  # type: ignore[attr-defined]
            names.update((spec.name, *spec.kinds, *spec.aliases))
    return tuple(sorted(names))


def detect(
    path: Path,
    kind: str | None = None,
    *,
    registry: object | None = None,
) -> Handler:
    """Resolve a path to its handler.

    Strong native signatures are authoritative even when a text suffix or an
    explicit hint is misleading.  Hints select a parser only after PDF, ZIP,
    and HTML identity have been ruled out; they never coerce hostile bytes into
    a parser that would make weaker claims.
    """
    if registry is not None:
        validate_extension_registry(registry)
    if path.is_dir():
        raise UnsupportedFormat(path, "directory", 2)

    suffix = path.suffix.lower()
    hinted = (
        _handler_for_kind(kind, registry=registry)[0]
        if kind is not None
        else None
    )
    zip_fallback = hinted or _BY_SUFFIX.get(suffix)
    if (
        zip_fallback is not None
        and zip_fallback.extension_name is None
        and zip_fallback.kind not in {"docx", "epub", "xlsx", "numpy"}
    ):
        zip_fallback = None

    if sniffed := _sniff(
        path,
        strong_only=True,
        zip_fallback=zip_fallback,
        native_hint=hinted,
        registry=registry,
    ):
        return sniffed

    if hinted is not None:
        return hinted

    if (
        (handler := _BY_SUFFIX.get(suffix))
        and suffix not in _UNAVAILABLE_SOURCE_SUFFIXES
    ):
        return handler

    if registry is not None:
        spec = registry.extractor_for_suffix(suffix)  # type: ignore[attr-defined]
        if spec is not None:
            return _extension_handler(spec)

    # Keep the built-in named source-language decline when no explicitly
    # enabled adapter replaces it.
    if handler is not None:
        return handler

    if deferred := _DEFERRED.get(suffix):
        raise UnsupportedFormat(path, *deferred)

    if sniffed := _sniff(
        path,
        strong_only=False,
        zip_fallback=None,
        native_hint=None,
        registry=registry,
    ):
        return sniffed

    raise UnknownFormat(path)


_MAX_NPZ_MEMBERS = 1024
_MAX_NPZ_MEMBER_UNCOMPRESSED = 512 * 1024 * 1024
_MAX_NPZ_TOTAL_UNCOMPRESSED = 1024 * 1024 * 1024
_MAX_NPZ_DECOMPRESSION_RATIO = 100


def _is_safe_zip_member_name(name: str) -> bool:
    if not name or len(name) > 255:
        return False
    if "\x00" in name or "\\" in name:
        return False
    if name.startswith("/") or name.endswith("/") or "//" in name:
        return False
    parts = PurePosixPath(name).parts
    if not parts or any(p in (".", "..") for p in parts):
        return False
    if ":" in parts[0]:
        return False
    return True


def _is_safe_zip_sniff_envelope(infolist: list[Any]) -> bool:
    total_members = len(infolist)
    if total_members == 0 or total_members > _MAX_NPZ_MEMBERS:
        return False

    import stat
    import zipfile

    seen_names: set[str] = set()
    total_uncompressed = 0

    for info in infolist:
        fname = info.filename
        original = getattr(info, "orig_filename", fname)
        if type(original) is not str or original != fname:
            return False
        if not _is_safe_zip_member_name(fname):
            return False
        if info.is_dir() or fname.endswith("/"):
            return False
        mode = (info.external_attr >> 16) & 0o170000
        if mode and not stat.S_ISREG(mode):
            return False
        if info.flag_bits & 0x1:
            return False
        if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            return False
        if fname in seen_names:
            return False
        seen_names.add(fname)

        if info.file_size > _MAX_NPZ_MEMBER_UNCOMPRESSED:
            return False
        total_uncompressed += info.file_size
        if total_uncompressed > _MAX_NPZ_TOTAL_UNCOMPRESSED:
            return False

        # Zero-compressed non-empty bomb
        if info.file_size > 0 and info.compress_size == 0:
            return False

        if info.compress_type == zipfile.ZIP_DEFLATED and info.compress_size > 0:
            ratio = info.file_size / info.compress_size
            if ratio > _MAX_NPZ_DECOMPRESSION_RATIO and info.file_size > 64 * 1024:
                return False

    return True


def _has_zip_entry(archive: Any, name: str) -> bool:
    try:
        archive.getinfo(name)
        return True
    except KeyError:
        return False


def _sniff(
    path: Path,
    *,
    strong_only: bool,
    zip_fallback: Handler | None,
    native_hint: Handler | None,
    registry: object | None,
) -> Handler | None:
    """Identify native signatures, optionally followed by conservative text sniffing."""
    try:
        with path.open("rb") as fh:
            head = fh.read(512)
    except OSError:
        return None

    if head.startswith(b"%PDF-"):
        return _BY_SUFFIX[".pdf"]
    if head.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        # XLSX, DOCX, and EPUB are all ZIP containers. Inspect their native
        # member signatures instead of declaring every extensionless ZIP to be
        # a workbook and letting openpyxl fail mysteriously later.
        import zipfile

        try:
            with zipfile.ZipFile(path) as archive:
                infolist = archive.infolist()
                candidates: list[Handler] = []
                if _has_zip_entry(archive, "xl/workbook.xml"):
                    candidates.append(_BY_SUFFIX[".xlsx"])
                if _has_zip_entry(archive, "word/document.xml"):
                    candidates.append(_BY_SUFFIX[".docx"])
                if _has_zip_entry(archive, "META-INF/container.xml") and _has_zip_entry(archive, "mimetype"):
                    candidates.append(_BY_SUFFIX[".epub"])

                # Check for NPY identity only after validating the complete ZIP
                # envelope.  A hybrid carrying another native container marker
                # remains visible as ambiguous, while a generic ZIP with an
                # incidental NPY member is not promoted to NPZ.
                # An over-count, oversized, high-ratio, encrypted, unsafe-name,
                # duplicate, unsupported-method, or zero-compressed non-empty
                # archive causes zero member opens.
                has_npy = False
                safe_envelope = _is_safe_zip_sniff_envelope(infolist)
                if safe_envelope:
                    for info in infolist:
                        if info.filename.endswith(".npy"):
                            try:
                                with archive.open(info, "r") as member_stream:
                                    if member_stream.read(6) == b"\x93NUMPY":
                                        has_npy = True
                                        break
                            except (
                                zipfile.BadZipFile,
                                zipfile.LargeZipFile,
                                OSError,
                                EOFError,
                                KeyError,
                                RuntimeError,
                            ):
                                continue

                is_presentation = _has_zip_entry(archive, "ppt/presentation.xml")
                all_members_are_npy = bool(infolist) and all(
                    info.filename.endswith(".npy") for info in infolist
                )
                if has_npy and (
                    all_members_are_npy or candidates or is_presentation
                ):
                    candidates.append(_BY_SUFFIX[".npz"])

                if len(candidates) + int(is_presentation) > 1:
                    kinds = [candidate.kind for candidate in candidates]
                    if is_presentation:
                        kinds.append("slides")
                    raise ValueError(
                        f"{path.name}: ZIP container ambiguously matches "
                        + ", ".join(kinds)
                    )
                if candidates:
                    return candidates[0]
                if is_presentation:
                    if zip_fallback is not None and zip_fallback.extension_name:
                        return zip_fallback
                    if registry is not None:
                        spec = registry.extractor_for_suffix(  # type: ignore[attr-defined]
                            path.suffix.casefold()
                        )
                        if spec is not None:
                            return _extension_handler(spec)
                    raise UnsupportedFormat(path, "slides", 4)
        except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile, EOFError):
            if zip_fallback is not None:
                return zip_fallback
        if zip_fallback is not None and (
            zip_fallback.extension_name is not None
            or zip_fallback == _BY_SUFFIX[".npz"]
        ):
            return zip_fallback
        if registry is not None:
            spec = registry.extractor_for_suffix(  # type: ignore[attr-defined]
                path.suffix.casefold()
            )
            if spec is not None:
                return _extension_handler(spec)
        raise UnsupportedFormat(path, "archive", 2)

    if native := _native_tier3_handler_for_path(
        path,
        head,
        preferred=native_hint,
    ):
        return native

    if registry is not None:
        spec = _extension_spec_for_path(path, registry)
        if spec is not None:
            return _extension_handler(spec)

    if deferred := _known_deferred_signature(head):
        raise UnsupportedFormat(path, *deferred)

    stripped = head.lstrip().lower()
    if _looks_like_html_document(head, doctype_only=strong_only):
        return _BY_SUFFIX[".html"]
    if strong_only:
        return None
    if stripped.startswith((b"{", b"[")):
        return _BY_SUFFIX[".json"]
    if stripped.startswith(b"<?xml"):
        return _BY_SUFFIX[".xml"]
    if _looks_like_text(head):
        return _BY_SUFFIX[".txt"]
    return None


def _native_tier3_handler(
    payload: bytes,
    *,
    preferred: Handler | None = None,
) -> Handler | None:
    """Resolve strong native Tier 3 bytes without guessing from prose.

    NetCDF4 is an HDF5 container, so an explicit/native NetCDF identity is the
    only valid discriminator for that shared magic. Other signatures override
    suffixes and hints outright.
    """

    if payload.startswith(b"\x93NUMPY"):
        return _BY_SUFFIX[".npy"]
    if payload.startswith(b"SIMPLE  ="):
        return _BY_SUFFIX[".fits"]
    if len(payload) >= 12 and payload.startswith(b"ARROW1") and payload.endswith(b"ARROW1"):
        if preferred is not None and preferred.kind == "feather":
            return _BY_SUFFIX[".feather"]
        return _BY_SUFFIX[".arrow"]
    if len(payload) >= 8 and payload.startswith(b"FEA1") and payload.endswith(b"FEA1"):
        return _BY_SUFFIX[".feather"]
    if payload.startswith(b"ORC"):
        return _BY_SUFFIX[".orc"]
    if len(payload) >= 8 and payload.startswith(b"PAR1") and payload.endswith(b"PAR1"):
        return _BY_SUFFIX[".parquet"]
    if payload.startswith(b"SQLite format 3\x00"):
        return _BY_SUFFIX[".sqlite"]
    if len(payload) >= 12 and payload[8:12] == b"DUCK":
        return _BY_SUFFIX[".duckdb"]
    if payload.startswith(b"\x89HDF\r\n\x1a\n"):
        if preferred is not None and preferred.kind == "netcdf":
            return _BY_SUFFIX[".nc"]
        return _BY_SUFFIX[".h5"]
    if payload.startswith((b"CDF\x01", b"CDF\x02", b"CDF\x05")):
        return _BY_SUFFIX[".nc"]
    return None


def _native_tier3_handler_for_path(
    path: Path,
    head: bytes,
    *,
    preferred: Handler | None,
) -> Handler | None:
    suffix_handler = _BY_SUFFIX.get(path.suffix.casefold())
    hdf_preference = (
        preferred
        if preferred is not None and preferred.kind == "netcdf"
        else (
            suffix_handler
            if suffix_handler is not None and suffix_handler.kind == "netcdf"
            else None
        )
    )
    feather_preference = (
        preferred
        if preferred is not None and preferred.kind == "feather"
        else (
            suffix_handler
            if suffix_handler is not None and suffix_handler.kind == "feather"
            else None
        )
    )
    if head.startswith(b"PAR1"):
        try:
            with path.open("rb") as stream:
                stream.seek(-4, 2)
                tail = stream.read(4)
        except OSError:
            tail = b""
        if tail == b"PAR1":
            return _BY_SUFFIX[".parquet"]
    if head.startswith(b"ARROW1"):
        try:
            with path.open("rb") as stream:
                stream.seek(-6, 2)
                tail = stream.read(6)
        except OSError:
            tail = b""
        if tail == b"ARROW1":
            if feather_preference is not None:
                return _BY_SUFFIX[".feather"]
            return _BY_SUFFIX[".arrow"]
    if head.startswith(b"FEA1"):
        try:
            with path.open("rb") as stream:
                stream.seek(-4, 2)
                tail = stream.read(4)
        except OSError:
            tail = b""
        if tail == b"FEA1":
            return _BY_SUFFIX[".feather"]
    return _native_tier3_handler(
        head,
        preferred=hdf_preference or feather_preference,
    )


def _extension_spec_for_path(path: Path, registry: object):
    """Match declarative extension probes without reading the whole file."""

    from .extensions import ExtensionCollisionError

    try:
        size = path.stat().st_size
        stream = path.open("rb")
    except OSError:
        return None

    matches: set[object] = set()
    windows: dict[tuple[int, int], bytes] = {}
    with stream:
        for spec in registry.extractors:  # type: ignore[attr-defined]
            for probe in spec.signatures:
                start = probe.offset if probe.offset >= 0 else size + probe.offset
                end = start + len(probe.pattern)
                if start < 0 or end > size:
                    continue
                key = start, len(probe.pattern)
                candidate = windows.get(key)
                if candidate is None:
                    try:
                        stream.seek(start)
                        candidate = stream.read(len(probe.pattern))
                    except OSError:
                        return None
                    windows[key] = candidate
                if _signature_window_matches(candidate, probe.pattern, probe.mask):
                    matches.add(spec)
                    break

    if len(matches) > 1:
        names = ", ".join(sorted(str(getattr(spec, "name")) for spec in matches))
        raise ExtensionCollisionError(
            f"payload matches multiple strong-signature extractors: {names}"
        )
    return next(iter(matches), None)


def _signature_window_matches(
    candidate: bytes,
    pattern: bytes,
    mask: bytes | None,
) -> bool:
    if len(candidate) != len(pattern):
        return False
    if mask is None:
        return candidate == pattern
    return all(
        (candidate_byte & mask_byte) == (pattern_byte & mask_byte)
        for candidate_byte, pattern_byte, mask_byte in zip(
            candidate, pattern, mask, strict=True
        )
    )


def _looks_like_text(head: bytes) -> bool:
    if not head:
        return False
    if b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
    except UnicodeDecodeError:
        # A multi-byte sequence split by the read boundary is not evidence of
        # binary content, so tolerate a truncated tail.
        try:
            head[:-3].decode("utf-8")
        except UnicodeDecodeError:
            return False
    return True


def _looks_like_html_document(
    payload: bytes,
    *,
    doctype_only: bool = False,
) -> bool:
    """Recognize a delimited leading HTML marker, never a source identifier.

    Source code and prose routinely contain strings such as ``"<body>"``.  A
    substring search would let that weak cue override an authoritative source
    suffix or media type and silently discard native declarations.  JSX also
    begins with names such as ``<BodyComponent>`` and can legitimately use
    ``<html>`` as its root.  Therefore only a real, delimited doctype is strong
    enough to override suffix/media identity; root tags are weak inference.
    """

    head = payload[:512]
    if head.startswith(b"\xef\xbb\xbf"):
        head = head[3:]
    stripped = head.lstrip(b" \t\r\n\f").lower()
    html_space = b" \t\r\n\f"

    if stripped.startswith(b"<!doctype"):
        remainder = stripped[len(b"<!doctype") :]
        if remainder and remainder[0] in html_space:
            remainder = remainder.lstrip(html_space)
            if _has_html_name_boundary(remainder, b"html"):
                return True
    if doctype_only:
        return False
    return _has_html_name_boundary(stripped, b"<html") or _has_html_name_boundary(
        stripped, b"<body"
    )


def _has_html_name_boundary(value: bytes, prefix: bytes) -> bool:
    if not value.startswith(prefix) or len(value) == len(prefix):
        return False
    return value[len(prefix)] in b" \t\r\n\f/>"


def _handler_for_kind(
    kind: str,
    *,
    registry: object | None = None,
) -> tuple[Handler, str]:
    normalized = kind.casefold().lstrip(".")
    suffix = _CANONICAL_SUFFIX.get(normalized)
    if suffix is not None and suffix not in _UNAVAILABLE_SOURCE_SUFFIXES:
        return _BY_SUFFIX[suffix], suffix

    if registry is not None:
        try:
            spec = registry.get_extractor(normalized)  # type: ignore[attr-defined]
        except LookupError:
            pass
        else:
            canonical = spec.suffixes[0] if spec.suffixes else ""
            return _extension_handler(spec), canonical

    if suffix is not None:
        return _BY_SUFFIX[suffix], suffix

    available = ", ".join(input_type_names(registry=registry))
    raise ValueError(
        f"unsupported explicit input type {kind!r}; choose one of {available}"
    )


def extract(
    path: Path,
    kind: str | None = None,
    *,
    registry: object | None = None,
) -> Extraction:
    """Acquire one immutable local snapshot, then detect and extract it."""

    import time

    path = Path(path)
    acquisition_started = time.perf_counter()
    if path.is_dir():
        # Preserve the named tier-2 outcome before any file-only acquisition.
        detect(path, kind=kind, registry=registry)
        raise AssertionError("directory detection must decline")  # pragma: no cover
    with _local_snapshot(path) as snapshot:
        physical_paths = [str(snapshot.path)]
        try:
            # Detection—including ZIP-directory and native footer inspection—
            # is deliberately downstream of acquisition. It therefore cannot
            # select a parser from bytes other than those that parser receives.
            handler = detect(snapshot.path, kind=kind, registry=registry)
            _require_clean_database_sidecars(
                path,
                handler,
                initial=snapshot.database_sidecars,
            )
            dispatch_path = _snapshot_dispatch_path(
                snapshot.path,
                logical_path=path,
                handler=handler,
                kind=kind,
                registry=registry,
            )
            if dispatch_path != snapshot.path:
                physical_paths.append(str(dispatch_path))
            acquisition_ms = _elapsed_ms(acquisition_started)
            extraction_started = time.perf_counter()

            result = _dispatch(dispatch_path, handler, registry=registry)
            _require_clean_database_sidecars(path, handler)
            result = _rebase(
                result,
                str(path),
                materialized_paths=tuple(physical_paths),
                private_root=str(snapshot.directory),
            )
            _validate_rebased_closure(result)
            _attach_input_manifest(
                result,
                handler=handler,
                source=str(path),
                byte_count=snapshot.byte_count,
                digest=snapshot.digest,
                acquisition_ms=acquisition_ms,
                extraction_ms=_elapsed_ms(extraction_started),
            )
            _attach_extension_capabilities(result, registry)
            _require_no_materialized_reference(
                result,
                source=str(path),
                physical_paths=tuple(physical_paths),
                private_root=str(snapshot.directory),
            )
            return result
        except Exception as exc:
            _raise_logicalized_error(
                exc,
                source=str(path),
                physical_paths=tuple(physical_paths),
                private_root=str(snapshot.directory),
            )


def _snapshot_dispatch_path(
    snapshot_path: Path,
    *,
    logical_path: Path,
    handler: Handler,
    kind: str | None,
    registry: object | None,
) -> Path:
    """Give suffix-dependent parsers a path alias to the same snapshot inode."""

    suffix = logical_path.suffix.lower()
    canonical = _CANONICAL_SUFFIX.get((kind or handler.kind).casefold())
    suffix_handler = _BY_SUFFIX.get(suffix)
    requires_suffix_hint = handler.module in {
        "autotldr.extract.structured",
        "autotldr.extract.code",
    } or handler.kind in {"markdown", "rst"}
    if handler.extension_name is not None:
        spec = registry.get_extractor(handler.extension_name)  # type: ignore[union-attr]
        requires_suffix_hint = bool(
            kind is not None and suffix not in set(spec.suffixes)
        )
        canonical = spec.suffixes[0] if spec.suffixes else ""
    if not (
        requires_suffix_hint
        and (
            (kind is not None and suffix != canonical)
            or suffix_handler is None
            or suffix_handler.kind != handler.kind
        )
    ):
        return snapshot_path

    materialized_suffix = canonical or (
        "" if handler.extension_name is not None else _CANONICAL_SUFFIX[handler.kind]
    )
    alias = snapshot_path.with_suffix(materialized_suffix)
    if alias == snapshot_path:
        return snapshot_path
    import os

    os.link(snapshot_path, alias)
    return alias


def _observe_database_sidecars(path: Path) -> tuple[_SidecarObservation, ...]:
    """Capture candidate companion state without deciding the source format."""

    observations: list[_SidecarObservation] = []
    for database_kind, suffixes in (
        ("sqlite", ("-wal", "-journal")),
        ("duckdb", (".wal",)),
    ):
        for suffix in suffixes:
            sidecar = path.with_name(path.name + suffix)
            try:
                size = sidecar.stat().st_size
            except FileNotFoundError:
                observations.append(
                    _SidecarObservation(database_kind, suffix, "missing")
                )
            except OSError as exc:
                observations.append(
                    _SidecarObservation(
                        database_kind,
                        suffix,
                        "error",
                        errno=exc.errno,
                    )
                )
            else:
                observations.append(
                    _SidecarObservation(
                        database_kind,
                        suffix,
                        "clean" if size == 0 else "nonempty",
                        size=size,
                    )
                )
    return tuple(observations)


def _require_clean_database_sidecars(
    path: Path,
    handler: Handler,
    *,
    initial: tuple[_SidecarObservation, ...] | None = None,
) -> None:
    """Keep the one-file database manifest honest outside the private copy."""

    if (
        handler.module != "autotldr.extract.tier3"
        or handler.kind not in {"sqlite", "duckdb"}
    ):
        return
    if initial is not None:
        _require_clean_sidecar_observations(path, handler.kind, initial)
    _require_clean_sidecar_observations(
        path,
        handler.kind,
        _observe_database_sidecars(path),
    )


def _require_clean_sidecar_observations(
    path: Path,
    database_kind: str,
    observations: tuple[_SidecarObservation, ...],
) -> None:
    for observation in observations:
        if observation.kind != database_kind:
            continue
        sidecar = path.with_name(path.name + observation.suffix)
        if observation.status == "error":
            detail = (
                ""
                if observation.errno is None
                else f" (errno {observation.errno})"
            )
            raise ValueError(
                f"{path.name}: database sidecar {sidecar.name} could not be "
                f"inspected{detail}"
            )
        if observation.status == "nonempty":
            raise ValueError(
                f"{path.name}: non-empty {sidecar.name} sidecar cannot be represented "
                "by an immutable single-file manifest; checkpoint it first"
            )


def _dispatch(
    path: Path,
    handler: Handler,
    *,
    registry: object | None = None,
) -> Extraction:
    if handler.extension_name is not None:
        if registry is None:  # pragma: no cover - internal construction guard
            raise AssertionError("extension handler requires its registry")
        from .extensions import (
            ExtensionConformanceError,
            validate_extraction_output,
        )

        spec = registry.get_extractor(handler.extension_name)  # type: ignore[attr-defined]
        extractor = registry.resolve_extractor(spec)  # type: ignore[attr-defined]
        try:
            raw_result = extractor(path)
        except Exception:
            raise ExtensionConformanceError(
                f"extension extractor {spec.name!r} failed"
            ) from None
        result = validate_extraction_output(raw_result)
        _validate_extension_extraction(result, path=path, spec=spec)
        result.meta["extension_adapter"] = spec.as_manifest()
        return result

    from .errors import MissingOptionalDependency

    # Only these legacy adapters import their optional parser while their module
    # is initialized.  Every other optional dependency is reported through the
    # explicit MissingOptionalDependency boundary inside the adapter.  A generic
    # ImportError is a broken implementation, not evidence that an extra is
    # absent, and must retain its real diagnosis.
    eager_optional_modules = {
        "autotldr.extract.pdf": frozenset({"pymupdf"}),
        "autotldr.extract.xlsx": frozenset({"openpyxl"}),
    }
    try:
        module = importlib.import_module(handler.module)
    except MissingOptionalDependency as exc:
        raise ImportError(
            f"{path}: {exc.feature} support is not installed ({exc.detail}).\n  install it with: pip install 'autotldr[{exc.extra}]'"
        ) from exc
    except ModuleNotFoundError as exc:
        if exc.name in eager_optional_modules.get(handler.module, ()):
            hint = f"  install it with: pip install 'autotldr[{handler.extra}]'" if handler.extra else ""
            raise ImportError(
                f"{path}: {handler.kind} support is not installed ({exc}).\n{hint}"
            ) from exc
        raise
    try:
        if handler.module in {"autotldr.extract.tier3", "autotldr.extract.columnar_interchange"}:
            return module.extract(path, kind=handler.kind)
        return module.extract(path)
    except MissingOptionalDependency as exc:
        raise ImportError(
            f"{path}: {exc.feature} support is not installed ({exc.detail}).\n  install it with: pip install 'autotldr[{exc.extra}]'"
        ) from exc
    except Exception as exc:
        unsupported = getattr(module, "UnsupportedSourceLanguage", None)
        if unsupported is not None and isinstance(exc, unsupported):
            language = getattr(exc, "language", handler.kind)
            tier = int(getattr(exc, "tier", handler.tier))
            raise UnsupportedFormat(path, f"{language} source", tier) from exc
        unsupported_subtype = getattr(module, "UnsupportedTier3Subtype", None)
        if unsupported_subtype is not None and isinstance(exc, unsupported_subtype):
            tier = int(getattr(exc, "tier", handler.tier))
            kind = str(getattr(exc, "subtype", getattr(exc, "kind", handler.kind)))
            raise UnsupportedFormat(path, kind, tier) from exc
        raise


def _validate_extension_extraction(result: Extraction, *, path: Path, spec: object) -> None:
    """Close the untrusted adapter boundary before provenance can be rebased."""

    from .extensions import ExtensionConformanceError

    if result.source != str(path):
        raise ExtensionConformanceError(
            f"extension extractor {spec.name!r} must preserve the supplied source"
        )
    if result.kind.casefold() not in set(spec.kinds):
        raise ExtensionConformanceError(
            f"extension extractor {spec.name!r} returned undeclared kind "
            f"{result.kind!r}"
        )
    if not result.units and not result.gaps:
        raise ExtensionConformanceError(
            f"extension extractor {spec.name!r} returned an empty success"
        )
    if not isinstance(result.meta, dict):
        raise ExtensionConformanceError(
            f"extension extractor {spec.name!r} metadata must be a dictionary"
        )

    units_by_id: dict[str, Unit] = {}
    for unit in result.units:
        if unit.source != result.source or unit.origin.source != result.source:
            raise ExtensionConformanceError(
                f"extension extractor {spec.name!r} returned cross-source units"
            )
        # Community extractors are deterministic structure backends.  The
        # current extension contract has no scored role-enrichment identity,
        # so D-013 permits only UNKNOWN at this boundary.
        if unit.role is not Role.UNKNOWN:
            raise ExtensionConformanceError(
                f"extension extractor {spec.name!r} returned an unmeasured "
                f"named role {str(unit.role)!r}"
            )
        if unit.id in units_by_id:
            raise ExtensionConformanceError(
                f"extension extractor {spec.name!r} returned duplicate unit ids"
            )
        units_by_id[unit.id] = unit

    for relation in result.relations:
        if relation.src not in units_by_id or relation.dst not in units_by_id:
            raise ExtensionConformanceError(
                f"extension extractor {spec.name!r} returned a dangling relation"
            )
    for gap in result.gaps:
        if gap.origin.source != result.source:
            raise ExtensionConformanceError(
                f"extension extractor {spec.name!r} returned a cross-source gap"
            )

    statement_ids: set[str] = set()
    for statement in result.summary_claims:
        if statement.id in statement_ids:
            raise ExtensionConformanceError(
                f"extension extractor {spec.name!r} returned duplicate statements"
            )
        statement_ids.add(statement.id)
        try:
            evidence = [units_by_id[unit_id] for unit_id in statement.evidence_unit_ids]
        except KeyError:
            raise ExtensionConformanceError(
                f"extension extractor {spec.name!r} returned unknown statement evidence"
            ) from None
        if set(statement.origins) != {unit.origin for unit in evidence}:
            raise ExtensionConformanceError(
                f"extension extractor {spec.name!r} returned statement origins "
                "that do not exactly match its evidence"
            )


def extract_stdin(
    data: str | bytes,
    *,
    kind: str | None = None,
    registry: object | None = None,
) -> Extraction:
    """Extract data received through the explicit ``-`` source.

    Ambiguous stdin defaults to UTF-8 text after conservative magic sniffing.
    Path-only native parsers may use a temporary file internally, after which
    the entire extraction is rebased to ``<stdin>`` so a process-specific path
    can never enter origins, relation endpoints, or content-addressed unit IDs.
    """

    import hashlib
    import time

    if registry is not None:
        validate_extension_registry(registry)
    acquisition_started = time.perf_counter()
    try:
        payload = data.encode("utf-8", errors="strict") if isinstance(data, str) else data
    except UnicodeEncodeError as exc:
        raise ValueError(
            f"<stdin>: input contains an unpaired Unicode surrogate at "
            f"character {exc.start}"
        ) from exc
    digest = hashlib.sha256(payload).hexdigest()
    acquisition_ms = _elapsed_ms(acquisition_started)
    extraction_started = time.perf_counter()
    native_hint = (
        _handler_for_kind(kind, registry=registry)[0]
        if kind is not None
        else None
    )
    strong = _sniff_stdin(
        payload,
        strong_only=True,
        native_hint=native_hint,
        registry=registry,
    )
    chosen = (
        strong
        or kind
        or _sniff_stdin(payload, native_hint=None, registry=registry)
    ).casefold()
    handler, canonical = _handler_for_kind(
        chosen if chosen != "zip" else "text",
        registry=registry,
    )
    if chosen in {"text", "txt", "rst"}:
        if chosen == "rst":
            result = _extract_materialized(
                payload,
                ".rst",
                "<stdin>",
                handler=_BY_SUFFIX[".rst"],
                registry=registry,
            )
            handler = _BY_SUFFIX[".rst"]
        else:
            module = importlib.import_module("autotldr.extract.text")
            text = _decode_utf8(payload, "<stdin>", "text")
            result = module.extract_text(text, source="<stdin>", kind="text")
            handler = _BY_SUFFIX[".txt"]
    elif chosen in {"md", "markdown"}:
        module = importlib.import_module("autotldr.extract.text")
        text = _decode_utf8(payload, "<stdin>", "Markdown")
        result = module.extract_text(text, source="<stdin>", kind="markdown")
        handler = _BY_SUFFIX[".md"]
    elif chosen == "zip":
        if native_hint is not None and native_hint.extension_name is not None:
            spec = registry.get_extractor(native_hint.extension_name)  # type: ignore[union-attr]
            extension_suffix = spec.suffixes[0] if spec.suffixes else ""
            result = _extract_materialized(
                payload,
                extension_suffix,
                "<stdin>",
                handler=native_hint,
                registry=registry,
            )
            handler = native_hint
        else:
            result, handler = _extract_materialized_detected(
                payload,
                "<stdin>",
                registry=registry,
            )
    else:
        handler, canonical = _handler_for_kind(chosen, registry=registry)
        result = _extract_materialized(
            payload,
            canonical,
            "<stdin>",
            handler=handler,
            registry=registry,
        )

    _attach_input_manifest(
        result,
        handler=handler,
        source="<stdin>",
        byte_count=len(payload),
        digest=digest,
        acquisition_ms=acquisition_ms,
        extraction_ms=_elapsed_ms(extraction_started),
    )
    _attach_extension_capabilities(result, registry)
    return result


def _sniff_stdin(
    payload: bytes,
    *,
    strong_only: bool = False,
    native_hint: Handler | None = None,
    registry: object | None = None,
) -> str | None:
    head = payload.lstrip()[:512].lower()
    if payload.startswith(b"%PDF-"):
        return "pdf"
    if payload.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        # Container identity is resolved by the regular router after spooling.
        return "zip"
    if native := _native_tier3_handler(payload, preferred=native_hint):
        return native.kind
    if registry is not None:
        spec = registry.extractor_for_bytes(payload)  # type: ignore[attr-defined]
        if spec is not None:
            return spec.name
    if deferred := _known_deferred_signature(payload):
        raise UnsupportedFormat("<stdin>", *deferred)
    if _looks_like_html_document(payload, doctype_only=strong_only):
        return "html"
    if strong_only:
        return None
    if head.startswith((b"{", b"[")):
        return "json"
    if head.startswith(b"<?xml"):
        return "xml"
    if _looks_like_text(payload[:512]):
        return "text"
    raise UnknownFormat("<stdin>")


def _known_deferred_signature(payload: bytes) -> tuple[str, int] | None:
    """Return a precise named decline for strong, non-native byte signatures."""

    if payload.startswith(
        (
            b"\x89PNG\r\n\x1a\n",
            b"\xff\xd8\xff",
            b"GIF87a",
            b"GIF89a",
            b"II*\x00",
            b"MM\x00*",
        )
    ):
        return ("image", 4)
    if (
        len(payload) >= 14
        and payload.startswith(b"BM")
        and payload[6:10] == b"\x00\x00\x00\x00"
    ):
        return ("image", 4)
    if len(payload) >= 12 and payload.startswith(b"RIFF") and payload[8:12] == b"WEBP":
        return ("image", 4)
    if len(payload) >= 12 and payload.startswith(b"RIFF") and payload[8:12] == b"WAVE":
        return ("audio", 5)
    if (
        len(payload) >= 10
        and payload.startswith(b"ID3")
        and payload[3] in {2, 3, 4}
        and all(byte < 128 for byte in payload[6:10])
    ):
        return ("audio", 5)
    if len(payload) >= 27 and payload.startswith(b"OggS\x00"):
        return ("audio", 5)
    if (
        len(payload) >= 12
        and payload[4:8] == b"ftyp"
        and 8 <= int.from_bytes(payload[:4], "big") <= len(payload)
    ):
        box_end = int.from_bytes(payload[:4], "big")
        brands = {payload[8:12]}
        brands.update(
            payload[offset : offset + 4]
            for offset in range(16, box_end - 3, 4)
        )
        if brands & {
            b"avif",
            b"avis",
            b"heic",
            b"heix",
            b"hevc",
            b"hevx",
            b"heim",
            b"heis",
            b"mif1",
            b"msf1",
        }:
            return ("image", 4)
        return ("video", 5)
    if payload.startswith(b"\x1f\x8b"):
        return ("archive", 2)
    if (
        len(payload) >= 16
        and payload.startswith(b"\x7fELF")
        and payload[4] in {1, 2}
        and payload[5] in {1, 2}
        and payload[6] == 1
    ):
        return ("binary", 6)
    if len(payload) >= 68 and payload.startswith(b"MZ"):
        pe_offset = int.from_bytes(payload[60:64], "little")
        if (
            64 <= pe_offset <= len(payload) - 4
            and payload[pe_offset : pe_offset + 4] == b"PE\x00\x00"
        ):
            return ("binary", 6)
    return None


def _extract_materialized(
    payload: bytes,
    suffix: str,
    source: str,
    *,
    handler: Handler | None = None,
    registry: object | None = None,
) -> Extraction:
    import tempfile

    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
            handle.write(payload)
            temporary = Path(handle.name)
        selected = handler or detect(temporary, registry=registry)
        result = _dispatch(temporary, selected, registry=registry)
    except Exception as exc:
        assert temporary is not None
        message = str(exc).replace(str(temporary), source).replace(
            temporary.name, source
        )
        if isinstance(exc, UnsupportedFormat):
            raise UnsupportedFormat(source, exc.kind, exc.tier) from exc
        if isinstance(exc, UnknownFormat):
            raise UnknownFormat(source) from exc
        if message == str(exc):
            raise
        if isinstance(exc, ImportError):
            raise ImportError(message) from exc
        if isinstance(exc, OSError):
            raise OSError(message) from exc
        raise ValueError(message) from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return _rebase(result, source)


def _extract_materialized_detected(
    payload: bytes,
    source: str,
    *,
    registry: object | None = None,
    suffix: str = "",
) -> tuple[Extraction, Handler]:
    import tempfile

    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
            handle.write(payload)
            temporary = Path(handle.name)
        handler = detect(temporary, registry=registry)
        result = _dispatch(temporary, handler, registry=registry)
    except Exception as exc:
        assert temporary is not None
        message = str(exc).replace(str(temporary), source).replace(
            temporary.name, source
        )
        if isinstance(exc, UnsupportedFormat):
            raise UnsupportedFormat(source, exc.kind, exc.tier) from exc
        if isinstance(exc, UnknownFormat):
            raise UnknownFormat(source) from exc
        if message == str(exc):
            raise
        if isinstance(exc, ImportError):
            raise ImportError(message) from exc
        if isinstance(exc, OSError):
            raise OSError(message) from exc
        raise ValueError(message) from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return _rebase(result, source), handler


def _rebase(
    result: Extraction,
    source: str,
    *,
    materialized_paths: tuple[str, ...] = (),
    private_root: str | None = None,
) -> Extraction:
    """Replace acquisition-only paths and close every ID-bearing reference."""

    paths = materialized_paths or (result.source,)
    replacements = _materialized_replacements(
        source=source,
        physical_paths=paths,
        private_root=private_root,
    )
    name_replacements = _materialized_name_replacements(
        source=source,
        physical_paths=paths,
    )
    ids: dict[str, str] = {}
    provisional: list[Unit] = []
    for unit in result.units:
        content = _replace_materialized_text(unit.content, replacements)
        rebased = Unit(
            source=source,
            modality=unit.modality,
            content=content,
            origin=Origin(
                source,
                _replace_provenance_text(
                    unit.origin.ref,
                    replacements,
                    name_replacements,
                ),
                unit.origin.char_span,
            ),
            role=unit.role,
            structure=tuple(
                _replace_provenance_text(
                    part,
                    replacements,
                    name_replacements,
                )
                for part in unit.structure
            ),
            salience=unit.salience,
            confidence=unit.confidence,
            tokens=unit.tokens if content == unit.content else 0,
            meta=_replace_materialized_value(
                unit.meta,
                replacements,
                name_replacements,
            ),
        )
        provisional.append(rebased)
        ids[unit.id] = rebased.id

    units = [
        Unit(
            source=unit.source,
            modality=unit.modality,
            content=unit.content,
            origin=unit.origin,
            role=unit.role,
            structure=unit.structure,
            salience=unit.salience,
            confidence=unit.confidence,
            tokens=unit.tokens,
            meta=_rewrite_ids(unit.meta, ids),
        )
        for unit in provisional
    ]
    relations = [
        Relation(
            src=ids.get(relation.src, relation.src),
            dst=ids.get(relation.dst, relation.dst),
            kind=relation.kind,
            evidence=_replace_materialized_text(relation.evidence, replacements),
            confidence=relation.confidence,
        )
        for relation in result.relations
    ]
    rebased = Extraction(
        source=source,
        kind=result.kind,
        units=units,
        relations=relations,
        gaps=[
            Gap(
                _replace_materialized_text(str(gap), replacements),
                Origin(
                    source,
                    _replace_provenance_text(
                        gap.origin.ref,
                        replacements,
                        name_replacements,
                    ),
                    gap.origin.char_span,
                ),
                gap.kind,
            )
            for gap in result.gaps
        ],
        meta=_rewrite_ids(
            _replace_materialized_value(
                result.meta,
                replacements,
                name_replacements,
            ),
            ids,
        ),
        summary_claims=[
            GroundedStatement(
                content=_replace_materialized_text(
                    statement.content,
                    replacements,
                ),
                origins=tuple(
                    Origin(
                        source,
                        _replace_provenance_text(
                            origin.ref,
                            replacements,
                            name_replacements,
                        ),
                        origin.char_span,
                    )
                    for origin in statement.origins
                ),
                evidence_unit_ids=tuple(
                    ids.get(unit_id, unit_id)
                    for unit_id in statement.evidence_unit_ids
                ),
            )
            for statement in result.summary_claims
        ],
    )
    changed_ids = {old for old, new in ids.items() if old != new}
    if changed_ids and any(
        _value_contains_exact(item.meta, changed_ids) for item in rebased.units
    ):
        raise ValueError(f"{source}: source rebasing left a stale unit ID in metadata")
    if changed_ids and _value_contains_exact(rebased.meta, changed_ids):
        raise ValueError(f"{source}: source rebasing left a stale unit ID in metadata")
    return rebased


def _logical_name(source: str) -> str:
    if "://" in source:
        tail = source.rsplit("/", 1)[-1].split("?", 1)[0]
        return tail or source
    return Path(source).name or source


def _materialized_replacements(
    *,
    source: str,
    physical_paths: tuple[str, ...],
    private_root: str | None,
) -> tuple[tuple[str, str], ...]:
    pairs: list[tuple[str, str]] = []
    for physical in physical_paths:
        if physical and physical != source:
            pairs.append((physical, source))
    if private_root:
        logical_parent = str(Path(source).parent) if "://" not in source else source
        pairs.append((private_root, logical_parent))

    # Full paths must be rewritten before their directory/name components.
    unique: dict[str, str] = {}
    for old, new in sorted(pairs, key=lambda pair: len(pair[0]), reverse=True):
        unique.setdefault(old, new)
    return tuple(unique.items())


def _materialized_name_replacements(
    *,
    source: str,
    physical_paths: tuple[str, ...],
) -> tuple[tuple[str, str], ...]:
    """Exact basename mappings for fields explicitly shaped as provenance."""

    logical_name = _logical_name(source)
    unique: dict[str, str] = {}
    for physical in physical_paths:
        physical_name = Path(physical).name if physical else ""
        if physical_name and physical_name != logical_name:
            unique.setdefault(physical_name, logical_name)
    return tuple(unique.items())


def _replace_materialized_text(
    value: str,
    replacements: tuple[tuple[str, str], ...],
) -> str:
    for old, new in replacements:
        value = value.replace(old, new)
    return value


def _replace_provenance_text(
    value: str,
    replacements: tuple[tuple[str, str], ...],
    name_replacements: tuple[tuple[str, str], ...],
) -> str:
    value = _replace_materialized_text(value, replacements)
    for old, new in name_replacements:
        if value == old:
            return new
    return value


def _replace_materialized_value(
    value,
    replacements: tuple[tuple[str, str], ...],
    name_replacements: tuple[tuple[str, str], ...] = (),
    *,
    provenance: bool = False,
):
    if isinstance(value, str):
        if provenance:
            return _replace_provenance_text(
                value,
                replacements,
                name_replacements,
            )
        return _replace_materialized_text(value, replacements)
    if isinstance(value, dict):
        rewritten = {}
        for key, item in value.items():
            rewritten_key = _replace_materialized_value(key, replacements)
            child_provenance = provenance or _provenance_metadata_key(key)
            rewritten_item = _replace_materialized_value(
                item,
                replacements,
                name_replacements,
                provenance=child_provenance,
            )
            if rewritten_key in rewritten:
                raise ValueError(
                    "source rebasing created a metadata dictionary-key collision"
                )
            rewritten[rewritten_key] = rewritten_item
        return rewritten
    if isinstance(value, list):
        return [
            _replace_materialized_value(
                item,
                replacements,
                name_replacements,
                provenance=provenance,
            )
            for item in value
        ]
    if isinstance(value, tuple):
        return tuple(
            _replace_materialized_value(
                item,
                replacements,
                name_replacements,
                provenance=provenance,
            )
            for item in value
        )
    if isinstance(value, set):
        return {
            _replace_materialized_value(
                item,
                replacements,
                name_replacements,
                provenance=provenance,
            )
            for item in value
        }
    if isinstance(value, frozenset):
        return frozenset(
            _replace_materialized_value(
                item,
                replacements,
                name_replacements,
                provenance=provenance,
            )
            for item in value
        )
    return value


def _provenance_metadata_key(value) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.casefold().replace("-", "_")
    return normalized in {
        "file",
        "filename",
        "logical_source",
        "path",
        "physical_path",
        "snapshot",
        "snapshot_root",
        "source",
        "source_name",
        "source_path",
    } or normalized.endswith(
        ("_file", "_filename", "_path", "_paths", "_source_path")
    )


def _rewrite_ids(value, ids: dict[str, str]):
    """Recursively update ID-bearing metadata after provenance rebasing."""

    if isinstance(value, str):
        return ids.get(value, value)
    if isinstance(value, dict):
        rewritten = {}
        for key, item in value.items():
            rewritten_key = _rewrite_ids(key, ids)
            if rewritten_key in rewritten:
                raise ValueError(
                    "source rebasing created a metadata dictionary-key collision"
                )
            rewritten[rewritten_key] = _rewrite_ids(item, ids)
        return rewritten
    if isinstance(value, list):
        return [_rewrite_ids(item, ids) for item in value]
    if isinstance(value, tuple):
        return tuple(_rewrite_ids(item, ids) for item in value)
    if isinstance(value, set):
        return {_rewrite_ids(item, ids) for item in value}
    if isinstance(value, frozenset):
        return frozenset(_rewrite_ids(item, ids) for item in value)
    return value


def _value_contains_exact(value, candidates: set[str]) -> bool:
    if isinstance(value, str):
        return value in candidates
    if isinstance(value, dict):
        return any(
            _value_contains_exact(key, candidates)
            or _value_contains_exact(item, candidates)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_value_contains_exact(item, candidates) for item in value)
    return False


def _validate_rebased_closure(result: Extraction) -> None:
    units_by_id: dict[str, Unit] = {}
    for unit in result.units:
        if unit.source != result.source or unit.origin.source != result.source:
            raise ValueError(f"{result.source}: rebased unit has a foreign source")
        if unit.id in units_by_id:
            raise ValueError(f"{result.source}: source rebasing created duplicate unit IDs")
        units_by_id[unit.id] = unit
    for relation in result.relations:
        if relation.src not in units_by_id or relation.dst not in units_by_id:
            raise ValueError(f"{result.source}: source rebasing left a dangling relation")
    for gap in result.gaps:
        if gap.origin.source != result.source:
            raise ValueError(f"{result.source}: rebased gap has a foreign source")

    statement_ids: set[str] = set()
    for statement in result.summary_claims:
        if statement.id in statement_ids:
            raise ValueError(
                f"{result.source}: source rebasing created duplicate statement IDs"
            )
        statement_ids.add(statement.id)
        try:
            evidence = [units_by_id[item] for item in statement.evidence_unit_ids]
        except KeyError:
            raise ValueError(
                f"{result.source}: source rebasing left unknown statement evidence"
            ) from None
        if set(statement.origins) != {unit.origin for unit in evidence}:
            raise ValueError(
                f"{result.source}: statement origins do not match rebased evidence"
            )


def _scrub_materialized_message(
    message: str,
    *,
    source: str,
    physical_paths: tuple[str, ...],
    private_root: str | None,
) -> str:
    replacements = _materialized_replacements(
        source=source,
        physical_paths=physical_paths,
        private_root=private_root,
    )
    message = _replace_materialized_text(message, replacements)
    for old, new in _materialized_name_replacements(
        source=source,
        physical_paths=physical_paths,
    ):
        message = message.replace(old, new)
    return message


def _logicalized_os_error(
    exc: OSError,
    *,
    source: str,
    physical_paths: tuple[str, ...],
    private_root: str | None,
) -> OSError:
    """Preserve filesystem error class/errno while erasing private paths."""

    message = _scrub_materialized_message(
        str(exc),
        source=source,
        physical_paths=physical_paths,
        private_root=private_root,
    )
    filename_value = exc.filename if exc.filename is not None else source
    filename = _scrub_materialized_message(
        str(filename_value),
        source=source,
        physical_paths=physical_paths,
        private_root=private_root,
    )
    strerror = _scrub_materialized_message(
        str(exc.strerror or message),
        source=source,
        physical_paths=physical_paths,
        private_root=private_root,
    )
    if exc.errno is not None:
        try:
            return type(exc)(exc.errno, strerror, filename)
        except (TypeError, ValueError):  # pragma: no cover - unusual OS subtype
            return OSError(exc.errno, strerror, filename)
    try:
        return type(exc)(message)
    except (TypeError, ValueError):  # pragma: no cover - unusual OS subtype
        return OSError(message)


def _raise_logicalized_error(
    exc: Exception,
    *,
    source: str,
    physical_paths: tuple[str, ...],
    private_root: str | None,
):
    if isinstance(exc, UnsupportedFormat):
        raise UnsupportedFormat(Path(source), exc.kind, exc.tier) from None
    if isinstance(exc, UnknownFormat):
        raise UnknownFormat(Path(source)) from None
    if isinstance(exc, OSError):
        # Reconstruct these before touching their structured attributes.  The
        # subtype and errno are part of the public filesystem contract, while
        # every exposed filename must identify the caller's logical source.
        raise _logicalized_os_error(
            exc,
            source=source,
            physical_paths=physical_paths,
            private_root=private_root,
        ) from None

    message = _scrub_materialized_message(
        str(exc),
        source=source,
        physical_paths=physical_paths,
        private_root=private_root,
    )
    replacements = _materialized_replacements(
        source=source,
        physical_paths=physical_paths,
        private_root=private_root,
    )
    name_replacements = _materialized_name_replacements(
        source=source,
        physical_paths=physical_paths,
    )
    for attribute in ("path", "filename", "filename2"):
        try:
            value = getattr(exc, attribute)
        except (AttributeError, TypeError):
            continue
        if value is None:
            continue
        rewritten = _replace_provenance_text(
            str(value),
            replacements,
            name_replacements,
        )
        if rewritten == str(value):
            continue
        try:
            setattr(
                exc,
                attribute,
                Path(rewritten) if isinstance(value, Path) else rewritten,
            )
        except (AttributeError, TypeError):
            pass

    if message == str(exc):
        raise exc
    if isinstance(exc, ImportError):
        raise ImportError(message) from None
    if isinstance(exc, ValueError):
        raise ValueError(message) from None
    raise RuntimeError(message) from None


def _require_no_materialized_reference(
    result: Extraction,
    *,
    source: str,
    physical_paths: tuple[str, ...],
    private_root: str,
) -> None:
    tokens = {private_root, *(path for path in physical_paths if path != source)}
    for value in _extraction_strings(result):
        if any(token and token in value for token in tokens):
            raise ValueError(
                f"{source}: extractor retained private snapshot provenance"
            )


def _extraction_strings(result: Extraction):
    yield result.source
    yield result.kind
    for unit in result.units:
        yield unit.source
        yield unit.content
        yield unit.origin.source
        yield unit.origin.ref
        yield from unit.structure
        yield from _nested_strings(unit.meta)
    for relation in result.relations:
        yield relation.src
        yield relation.dst
        yield relation.evidence
    for gap in result.gaps:
        yield str(gap)
        yield gap.origin.source
        yield gap.origin.ref
    for statement in result.summary_claims:
        yield statement.content
        yield from statement.evidence_unit_ids
        for origin in statement.origins:
            yield origin.source
            yield origin.ref
    yield from _nested_strings(result.meta)


def _nested_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _nested_strings(key)
            yield from _nested_strings(item)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _nested_strings(item)


def _elapsed_ms(started: float) -> float:
    import time

    return round((time.perf_counter() - started) * 1000.0, 3)


def _attach_input_manifest(
    result: Extraction,
    *,
    handler: Handler,
    source: str,
    byte_count: int,
    digest: str,
    acquisition_ms: float,
    extraction_ms: float,
) -> None:
    """Record the one acquired input without importing timing/hash code eagerly."""

    input_record: dict[str, object] = {
        "source": source,
        "kind": result.kind if handler.extension_name is not None else handler.kind,
        "tier": handler.tier,
        "bytes": byte_count,
        "sha256": digest,
    }
    if handler.extension_name is not None:
        input_record["adapter"] = {
            "origin": "explicit-extension",
            "name": handler.extension_name,
        }
    result.meta["inputs"] = [input_record]
    result.meta["timings"] = {
        "acquisition_ms": acquisition_ms,
        "extraction_ms": extraction_ms,
    }


def _attach_extension_capabilities(
    result: Extraction,
    registry: object | None,
) -> None:
    if registry is None or not len(registry):  # type: ignore[arg-type]
        return
    result.meta["extensions"] = {
        "schema": "autotldr-explicit-extension-run-v1",
        "requested": None,
        "capabilities": registry.capability_manifest(),  # type: ignore[attr-defined]
    }


def _decode_utf8(payload: bytes, source: str, kind: str) -> str:
    try:
        return payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"{source}: {kind} input is not strict UTF-8 at byte {exc.start}"
        ) from exc


@dataclass(frozen=True, slots=True)
class _HttpPayload:
    payload: bytes
    final_url: str
    content_type: str | None
    charset: str


def extract_url(
    url: str,
    *,
    timeout: float = 20.0,
    max_bytes: int = 16 * 1024 * 1024,
    registry: object | None = None,
    same_origin_with: str | None = None,
    probe_llms_txt: bool = True,
    request_observer: HttpRequestObserver | None = None,
) -> Extraction:
    """Acquire one HTTP(S) source using response bytes as format authority."""

    import hashlib
    import time
    from http.client import HTTPException

    if registry is not None:
        validate_extension_registry(registry)
    _validate_http_url(url, label="requested URL")
    if same_origin_with is not None:
        _validate_http_url(same_origin_with, label="same-origin root")
        if not _same_origin(url, same_origin_with):
            raise ValueError(
                f"requested URL {url!r} leaves same-origin root "
                f"{same_origin_with!r}"
            )
    acquisition_started = time.perf_counter()

    request_records: list[dict[str, str]] = []

    def observe(operation: str, target: str) -> bool:
        if request_observer is not None and request_observer(operation, target) is False:
            return False
        request_records.append({"operation": operation, "url": target})
        return True

    llms = (
        _probe_llms_txt(
            url,
            timeout=min(timeout, 2.0),
            max_bytes=min(max_bytes, 1024 * 1024),
            same_origin_with=same_origin_with,
            request_observer=observe,
        )
        if probe_llms_txt
        else None
    )
    if llms is not None:
        acquired = llms
    else:
        try:
            acquired = _fetch_http(
                url,
                timeout=timeout,
                max_bytes=max_bytes,
                same_origin_with=same_origin_with,
                request_observer=observe,
                operation="source",
            )
        except HttpRequestLimitExceeded:
            raise
        except (HTTPException, OSError, UnicodeError, ValueError) as exc:
            detail = str(exc).strip() or type(exc).__name__
            raise ValueError(
                f"{url}: failed to fetch requested URL: {detail}"
            ) from None
    acquisition_ms = _elapsed_ms(acquisition_started)
    extraction_started = time.perf_counter()

    result, handler = _extract_http_payload(
        acquired,
        requested_url=url,
        force_markdown=llms is not None,
        registry=registry,
    )
    result.meta.update(
        {
            "requested_url": url,
            "final_url": acquired.final_url,
            "content_type": acquired.content_type,
            "llms_txt": {
                "used": llms is not None,
                "url": acquired.final_url if llms is not None else None,
            },
            "http_requests": {
                "count": len(request_records),
                "discovery": sum(
                    item["operation"] == "llms.txt-discovery"
                    for item in request_records
                ),
                "source": sum(
                    item["operation"] == "source" for item in request_records
                ),
                "operations": request_records,
            },
        }
    )
    _attach_input_manifest(
        result,
        handler=handler,
        source=acquired.final_url,
        byte_count=len(acquired.payload),
        digest=hashlib.sha256(acquired.payload).hexdigest(),
        acquisition_ms=acquisition_ms,
        extraction_ms=_elapsed_ms(extraction_started),
    )
    _attach_extension_capabilities(result, registry)
    return result


def _fetch_http(
    url: str,
    *,
    timeout: float,
    max_bytes: int,
    same_origin_with: str | None = None,
    request_observer: HttpRequestObserver | None = None,
    operation: str = "source",
) -> _HttpPayload:
    # URL modules remain below the URL-only boundary so path invocation keeps
    # the cold-start import graph unchanged.
    from urllib.error import HTTPError
    from urllib.parse import urldefrag
    from urllib.request import HTTPRedirectHandler, Request, build_opener

    class _HttpOnlyRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            # This hook runs before urllib opens the redirect target.  The
            # post-open final-URL validation below remains defense in depth.
            try:
                _validate_http_url(newurl, label="redirect target")
                if same_origin_with is not None and not _same_origin(
                    same_origin_with,
                    newurl,
                ):
                    raise ValueError(
                        f"redirect target {newurl!r} leaves same-origin root "
                        f"{same_origin_with!r}"
                    )
                _observe_http_request(request_observer, operation, newurl)
            except Exception:
                if fp is not None:
                    fp.close()
                raise
            return super().redirect_request(
                req, fp, code, msg, headers, newurl
            )

    request = Request(
        url,
        headers={
            "User-Agent": "AutoTLDR/0.1 (+https://github.com/autotldr)",
            "Accept-Encoding": "identity",
            "Connection": "close",
        },
    )
    opener = build_opener(_HttpOnlyRedirect())
    _observe_http_request(request_observer, operation, url)
    try:
        response = opener.open(request, timeout=timeout)  # noqa: S310 - explicit user URL
    except HTTPError as exc:
        exc.close()
        raise
    with response:
        final_url, _ = urldefrag(response.geturl())
        _validate_http_url(final_url, label="redirect target")
        if same_origin_with is not None and not _same_origin(
            same_origin_with,
            final_url,
        ):
            raise ValueError(
                f"redirect target {final_url!r} leaves same-origin root "
                f"{same_origin_with!r}"
            )
        content_encoding = (response.headers.get("Content-Encoding") or "identity").casefold()
        if content_encoding not in {"identity", ""}:
            raise ValueError(
                f"{final_url}: unsupported HTTP content encoding "
                f"{content_encoding!r}"
            )
        declared = response.headers.get("Content-Length")
        if declared:
            try:
                declared_size = int(declared)
            except ValueError as exc:
                raise ValueError(
                    f"{final_url}: invalid HTTP Content-Length {declared!r}"
                ) from exc
            if declared_size > max_bytes:
                raise ValueError(
                    f"{final_url}: HTTP response is {declared_size} bytes; "
                    f"limit is {max_bytes} bytes"
                )
        payload = response.read(max_bytes + 1)
        if len(payload) > max_bytes:
            raise ValueError(
                f"{final_url}: HTTP response exceeds {max_bytes} bytes"
            )
        raw_content_type = response.headers.get("Content-Type")
        content_type = (
            response.headers.get_content_type()
            if raw_content_type is not None
            else None
        )
        charset = response.headers.get_content_charset() or "utf-8"
    return _HttpPayload(payload, final_url, content_type, charset)


def _probe_llms_txt(
    url: str,
    *,
    timeout: float,
    max_bytes: int,
    same_origin_with: str | None = None,
    request_observer: HttpRequestObserver | None = None,
) -> _HttpPayload | None:
    """Try the origin's advertised LLM view, falling back without inventing it."""

    from urllib.parse import urlsplit, urlunsplit

    parsed = urlsplit(url)
    if parsed.path == "/llms.txt":
        return None
    probe_url = urlunsplit((parsed.scheme, parsed.netloc, "/llms.txt", "", ""))
    try:
        acquired = _fetch_http(
            probe_url,
            timeout=timeout,
            max_bytes=max_bytes,
            same_origin_with=same_origin_with,
            request_observer=request_observer,
            operation="llms.txt-discovery",
        )
        if not _same_origin(probe_url, acquired.final_url):
            return None
        if acquired.content_type not in {
            "text/plain",
            "text/markdown",
            "application/markdown",
        }:
            return None
        # Validate before accepting the alternate source.  The caller decodes
        # it again during extraction, but this prevents an invalid probe from
        # suppressing a valid HTML page.
        _decode_http_text(acquired, kind="llms.txt")
        return acquired
    except HttpRequestLimitExceeded:
        # Discovery is advisory even when its caller-owned sub-budget refuses
        # a redirect.  The requested source still gets its reserved request;
        # a refusal there remains authoritative in ``extract_url``.
        return None
    except Exception:
        # Discovery is advisory. A missing, redirected, oversized, or invalid
        # llms.txt must not prevent acquisition of the explicitly requested URL.
        return None


def _observe_http_request(
    observer: HttpRequestObserver | None,
    operation: str,
    url: str,
) -> None:
    if observer is not None and observer(operation, url) is False:
        raise HttpRequestLimitExceeded(
            f"HTTP request budget refused {operation} request for {url!r}"
        )


def _extract_http_payload(
    acquired: _HttpPayload,
    *,
    requested_url: str,
    force_markdown: bool = False,
    registry: object | None = None,
) -> tuple[Extraction, Handler]:
    payload = acquired.payload
    content_type = acquired.content_type
    head = payload.lstrip()[:512].lower()

    # 1. Strong byte identity.  Every signature branch precedes every media or
    # URL hint, so a ZIP body mislabeled application/pdf cannot reach the PDF
    # parser and a PNG body mislabeled text/csv cannot become schema claims.
    if payload.startswith(b"%PDF-"):
        handler = _BY_SUFFIX[".pdf"]
        return (
            _extract_http_with_handler(
                acquired,
                handler,
                ".pdf",
                registry=registry,
            ),
            handler,
        )

    if payload.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        trusted = _trusted_http_suffix(
            acquired.final_url,
            requested_url,
            registry=registry,
        )
        return _extract_materialized_detected(
            payload,
            acquired.final_url,
            registry=registry,
            suffix=trusted or "",
        )

    native_preference: Handler | None = None
    media_suffix = _HTTP_NATIVE_MEDIA.get(content_type or "")
    if media_suffix is not None:
        media_handler = _BY_SUFFIX[media_suffix]
        if media_handler.kind in {"netcdf", "feather"}:
            native_preference = media_handler
    if native_preference is None:
        url_suffix = _trusted_http_suffix(
            acquired.final_url,
            requested_url,
            registry=registry,
        )
        url_handler = _BY_SUFFIX.get(url_suffix or "")
        if url_handler is not None and url_handler.kind in {"netcdf", "feather"}:
            native_preference = url_handler
    if native := _native_tier3_handler(payload, preferred=native_preference):
        suffix = _CANONICAL_SUFFIX[native.kind]
        return (
            _extract_http_with_handler(
                acquired,
                native,
                suffix,
                registry=registry,
            ),
            native,
        )

    if registry is not None:
        spec = registry.extractor_for_bytes(payload)  # type: ignore[attr-defined]
        if spec is not None:
            handler = _extension_handler(spec)
            suffix = spec.suffixes[0] if spec.suffixes else ""
            return (
                _extract_http_with_handler(
                    acquired,
                    handler,
                    suffix,
                    registry=registry,
                ),
                handler,
            )

    if deferred := _known_deferred_signature(payload):
        raise UnsupportedFormat(acquired.final_url, *deferred)

    if _looks_like_html_document(payload, doctype_only=True):
        handler = _BY_SUFFIX[".html"]
        return (
            _extract_http_html(
                acquired,
                requested_url=requested_url,
            ),
            handler,
        )

    if force_markdown:
        handler = _BY_SUFFIX[".md"]
        module = importlib.import_module("autotldr.extract.text")
        text = _decode_http_text(acquired, kind="llms.txt Markdown")
        return (
            module.extract_text(
                text,
                source=acquired.final_url,
                kind="markdown",
            ),
            handler,
        )

    # 2. A recognized, non-generic media identity.  text/plain and generic
    # binary transport types intentionally fall through to URL suffix recovery.
    if content_type not in _HTTP_GENERIC_MEDIA:
        if suffix := _HTTP_NATIVE_MEDIA.get(content_type or ""):
            if suffix not in _UNAVAILABLE_SOURCE_SUFFIXES:
                handler = _BY_SUFFIX[suffix]
                if handler.kind == "html":
                    return (
                        _extract_http_html(
                            acquired,
                            requested_url=requested_url,
                        ),
                        handler,
                    )
                return (
                    _extract_http_with_handler(
                        acquired,
                        handler,
                        suffix,
                        registry=registry,
                    ),
                    handler,
                )
        if registry is not None:
            spec = registry.extractor_for_media_type(  # type: ignore[attr-defined]
                content_type or ""
            )
            if spec is not None:
                handler = _extension_handler(spec)
                suffix = spec.suffixes[0] if spec.suffixes else ""
                return (
                    _extract_http_with_handler(
                        acquired,
                        handler,
                        suffix,
                        registry=registry,
                    ),
                    handler,
                )
        if suffix := _HTTP_NATIVE_MEDIA.get(content_type or ""):
            # No explicit adapter replaced this built-in named grammar decline.
            handler = _BY_SUFFIX[suffix]
            return (
                _extract_http_with_handler(
                    acquired,
                    handler,
                    suffix,
                    registry=registry,
                ),
                handler,
            )
        if deferred := _deferred_http_media(content_type):
            raise UnsupportedFormat(acquired.final_url, *deferred)

    # 3. Generic/absent media may borrow any known URL identity.  An otherwise
    # unknown text/* subtype may borrow only an implemented textual suffix;
    # it must not turn a textual response into a deferred image/archive solely
    # because of a misleading filename.
    suffix_eligible = content_type in _HTTP_GENERIC_MEDIA or bool(
        content_type and content_type.startswith("text/")
    )
    if suffix_eligible:
        if suffix := _trusted_http_suffix(
            acquired.final_url,
            requested_url,
            registry=registry,
        ):
            if suffix in _BY_SUFFIX and suffix not in _UNAVAILABLE_SOURCE_SUFFIXES:
                handler = _BY_SUFFIX[suffix]
                if handler.kind == "html":
                    return (
                        _extract_http_html(
                            acquired,
                            requested_url=requested_url,
                        ),
                        handler,
                    )
                return (
                    _extract_http_with_handler(
                        acquired,
                        handler,
                        suffix,
                        registry=registry,
                    ),
                    handler,
                )
            if registry is not None:
                spec = registry.extractor_for_suffix(suffix)  # type: ignore[attr-defined]
                if spec is not None:
                    handler = _extension_handler(spec)
                    return (
                        _extract_http_with_handler(
                            acquired,
                            handler,
                            suffix,
                            registry=registry,
                        ),
                        handler,
                    )
            if deferred := _DEFERRED.get(suffix):
                if content_type in _HTTP_GENERIC_MEDIA:
                    raise UnsupportedFormat(acquired.final_url, *deferred)
            elif suffix in _BY_SUFFIX:
                handler = _BY_SUFFIX[suffix]
                return (
                    _extract_http_with_handler(
                        acquired,
                        handler,
                        suffix,
                        registry=registry,
                    ),
                    handler,
                )

    # A generic text media type with no useful suffix is still explicitly text.
    if content_type == "text/plain":
        handler = _BY_SUFFIX[".txt"]
        return (
            _extract_http_with_handler(
                acquired,
                handler,
                ".txt",
                registry=registry,
            ),
            handler,
        )

    # 4. Weak content inference is deliberately behind signature, media, and
    # permitted suffix resolution. JSON and XML retain their own byte-level
    # encoding rules; inferred prose honors the declared/default charset.
    if _looks_like_html_document(payload):
        handler = _BY_SUFFIX[".html"]
        return (
            _extract_http_html(
                acquired,
                requested_url=requested_url,
            ),
            handler,
        )
    if head.startswith((b"{", b"[")):
        handler = _BY_SUFFIX[".json"]
        return (
            _extract_http_with_handler(
                acquired,
                handler,
                ".json",
                registry=registry,
            ),
            handler,
        )
    if head.startswith(b"<?xml"):
        handler = _BY_SUFFIX[".xml"]
        return (
            _extract_http_with_handler(
                acquired,
                handler,
                ".xml",
                registry=registry,
            ),
            handler,
        )
    if _looks_like_text(payload[:512]):
        handler = _BY_SUFFIX[".txt"]
        return (
            _extract_http_with_handler(
                acquired,
                handler,
                ".txt",
                registry=registry,
            ),
            handler,
        )

    # 5. Opaque bytes are a named unsupported outcome, never lossy text.
    raise UnknownFormat(acquired.final_url)


def _extract_http_html(
    acquired: _HttpPayload,
    *,
    requested_url: str,
) -> Extraction:
    handler = _BY_SUFFIX[".html"]
    module = importlib.import_module(handler.module)
    text = _decode_http_text(acquired, kind="HTML")
    return module.extract_html(
        text,
        source=acquired.final_url,
        requested_url=requested_url,
        content_type=acquired.content_type,
    )


def _extract_http_with_handler(
    acquired: _HttpPayload,
    handler: Handler,
    suffix: str,
    *,
    registry: object | None = None,
) -> Extraction:
    payload = acquired.payload
    if suffix in _HTTP_TRANSCODE_SUFFIXES:
        # The transport charset describes these textual formats.  Parsers take
        # a strict UTF-8 acquisition copy, while manifest hashing/counting stays
        # on the original response bytes and spans address decoded characters.
        text = _decode_http_text(acquired, kind=handler.kind)
        payload = text.encode("utf-8", errors="strict")
    return _extract_materialized(
        payload,
        suffix,
        acquired.final_url,
        handler=handler,
        registry=registry,
    )


def _trusted_http_suffix(
    final_url: str,
    requested_url: str,
    *,
    registry: object | None = None,
) -> str | None:
    from urllib.parse import unquote, urlsplit

    for value in (final_url, requested_url):
        path = unquote(urlsplit(value).path)
        name = path.rsplit("/", 1)[-1]
        if not name:
            continue
        suffix = Path(name).suffix.casefold()
        if (
            suffix in _BY_SUFFIX
            or suffix in _DEFERRED
            or (
                registry is not None
                and bool(suffix)
                and registry.extractor_for_suffix(suffix) is not None  # type: ignore[attr-defined]
            )
        ):
            return suffix
    return None


def _deferred_http_media(content_type: str | None) -> tuple[str, int] | None:
    if content_type is None:
        return None
    if deferred := _HTTP_DEFERRED_MEDIA.get(content_type):
        return deferred
    if content_type.startswith("image/"):
        return ("image", 4)
    if content_type.startswith("audio/"):
        return ("audio", 5)
    if content_type.startswith("video/"):
        return ("video", 5)
    return None


def _decode_http_text(acquired: _HttpPayload, *, kind: str) -> str:
    try:
        return acquired.payload.decode(acquired.charset, errors="strict")
    except LookupError as exc:
        raise ValueError(
            f"{acquired.final_url}: unsupported declared charset "
            f"{acquired.charset!r} for {kind}"
        ) from exc
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"{acquired.final_url}: {kind} bytes are invalid for declared "
            f"charset {acquired.charset!r} at byte {exc.start}"
        ) from exc


def _validate_http_url(value: str, *, label: str) -> None:
    from urllib.parse import urlsplit

    parsed = urlsplit(value)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        raise ValueError(
            f"unsupported {label} {value!r}; only HTTP(S) is accepted"
        )
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError(f"invalid {label} {value!r}: {exc}") from exc


def _same_origin(left: str, right: str) -> bool:
    from urllib.parse import urlsplit

    def identity(value: str) -> tuple[str, str | None, int | None]:
        parsed = urlsplit(value)
        scheme = parsed.scheme.casefold()
        port = parsed.port or (443 if scheme == "https" else 80)
        return scheme, parsed.hostname.casefold() if parsed.hostname else None, port

    return identity(left) == identity(right)


def is_url(value: str) -> bool:
    """True only for the two network locator schemes invoke mode accepts."""

    lowered = value.casefold()
    return lowered.startswith("http://") or lowered.startswith("https://")


def supported_suffixes(*, registry: object | None = None) -> frozenset[str]:
    """Suffixes with an implemented native Stage 3 extraction path."""

    suffixes = set(_BY_SUFFIX) - _UNAVAILABLE_SOURCE_SUFFIXES
    if registry is not None:
        validate_extension_registry(registry)
        for spec in registry.extractors:  # type: ignore[attr-defined]
            suffixes.update(spec.suffixes)
    return frozenset(suffixes)


def declined_suffixes(*, registry: object | None = None) -> frozenset[str]:
    """Suffixes routed only to a named deferred/unavailable outcome."""

    declined = set(_DEFERRED) | set(_UNAVAILABLE_SOURCE_SUFFIXES)
    if registry is not None:
        validate_extension_registry(registry)
        for spec in registry.extractors:  # type: ignore[attr-defined]
            declined.difference_update(spec.suffixes)
    return frozenset(declined)
