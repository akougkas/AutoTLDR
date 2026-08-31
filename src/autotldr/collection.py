"""Bounded Tier 2 collection acquisition.

This module is the fan-out boundary between one collection-shaped source and
the existing per-file router.  It deliberately does not fuse or render.  A
caller receives already-routed :class:`~autotldr.unit.Extraction` leaves, an
exact deterministic acquisition manifest, and typed addressable declines that
can be appended to the eventual fused collection as ordinary extraction gaps.

The module is not imported by the current CLI.  When Stage 5 integrates it,
that import must remain below directory/archive/doc-site detection so the Tier
0 cold-start path does not pay for collection handling.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Sequence

from .unit import (
    Extraction,
    Gap,
    GapKind,
    GroundedStatement,
    Origin,
    Relation,
    Unit,
)


MANIFEST_SCHEMA = 1

# Child entries with a leading dot are not acquired.  This avoids silently
# reading secrets and keeps implementation state such as .git and .autotldr out
# of the semantic corpus.  An explicitly named hidden root remains valid.
HIDDEN_ENTRY_POLICY = "skip-child-name-leading-dot"
INTERNAL_DIRECTORY_NAMES = frozenset(
    {"__pycache__", "node_modules", "venv"}
)

_ARCHIVE_SUFFIXES = (
    ".zip",
    ".tar",
    ".tar.gz",
    ".tgz",
)


class CollectionError(ValueError):
    """The explicitly named collection itself cannot be acquired safely."""


class DeclineKind(StrEnum):
    """Why one member was not admitted to the routed collection."""

    UNSUPPORTED = "unsupported"
    EXTRACTION = "extraction-error"
    UNSAFE = "unsafe-member"
    LIMIT = "limit"
    ENCRYPTED = "encrypted-member"
    DUPLICATE = "duplicate-member"
    CROSS_ORIGIN = "cross-origin"


@dataclass(frozen=True, slots=True)
class CollectionLimits:
    """Hard acquisition limits with conservative desktop defaults.

    ``max_total_bytes`` counts bytes admitted from ordinary files and both the
    compressed container and expanded members of archives.  Counting both is
    intentionally conservative: nested compression must consume budget at
    every expansion boundary.
    """

    max_members: int = 2_000
    max_member_bytes: int = 16 * 1024 * 1024
    max_total_bytes: int = 256 * 1024 * 1024
    max_directory_depth: int = 24
    max_archive_members: int = 2_000
    max_archive_depth: int = 3
    max_archive_path_depth: int = 32
    max_archive_container_bytes: int = 64 * 1024 * 1024
    max_compression_ratio: float = 200.0
    max_crawl_pages: int = 64
    max_crawl_depth: int = 3
    max_crawl_page_bytes: int = 8 * 1024 * 1024
    max_crawl_total_bytes: int = 64 * 1024 * 1024
    crawl_timeout_seconds: float = 20.0

    def __post_init__(self) -> None:
        integer_fields = (
            "max_members",
            "max_member_bytes",
            "max_total_bytes",
            "max_archive_members",
            "max_archive_path_depth",
            "max_archive_container_bytes",
            "max_crawl_pages",
            "max_crawl_page_bytes",
            "max_crawl_total_bytes",
        )
        for name in integer_fields:
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        for name in (
            "max_directory_depth",
            "max_archive_depth",
            "max_crawl_depth",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if (
            not isinstance(self.max_compression_ratio, (int, float))
            or isinstance(self.max_compression_ratio, bool)
            or self.max_compression_ratio <= 1
        ):
            raise ValueError("max_compression_ratio must be greater than one")
        if (
            not isinstance(self.crawl_timeout_seconds, (int, float))
            or isinstance(self.crawl_timeout_seconds, bool)
            or self.crawl_timeout_seconds <= 0
        ):
            raise ValueError("crawl_timeout_seconds must be positive")


@dataclass(frozen=True, slots=True)
class MemberDecline:
    """One failed or refused member with an address and machine-readable type."""

    kind: DeclineKind
    content: str
    origin: Origin
    detected_kind: str | None = None
    tier: int | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.content.strip():
            raise ValueError("collection decline content must not be empty")
        if self.tier is not None and (
            not isinstance(self.tier, int)
            or isinstance(self.tier, bool)
            or self.tier < 0
        ):
            raise ValueError("collection decline tier must be a non-negative integer")
        object.__setattr__(self, "details", _json_copy(self.details))

    @property
    def source(self) -> str:
        return self.origin.source

    def as_gap(self) -> Gap:
        """Project the typed decline into the existing absence representation."""

        return Gap(self.content, self.origin, GapKind.EXTRACTION)

    def as_manifest(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "status": "declined",
            "source": self.origin.source,
            "origin": _origin_record(self.origin),
            "decline_kind": str(self.kind),
            "content": self.content,
        }
        if self.detected_kind is not None:
            record["detected_kind"] = self.detected_kind
        if self.tier is not None:
            record["tier"] = self.tier
        if self.details:
            record["details"] = _json_copy(self.details)
        return record


@dataclass(frozen=True, slots=True)
class CollectionAcquisition:
    """A collection expanded into routed semantic leaves.

    Integration is intentionally explicit: pass ``extractions`` to
    :func:`autotldr.fusion.fuse`, append ``gaps`` to that fused result, and copy
    ``manifest`` under a collection-acquisition key in its manifest.  This
    prevents the acquisition layer from depending on fusion or rendering.
    """

    source: str
    kind: str
    extractions: tuple[Extraction, ...]
    declines: tuple[MemberDecline, ...]
    manifest: dict[str, Any]

    def __post_init__(self) -> None:
        sources = [item.source for item in self.extractions]
        if sources != sorted(sources):
            raise ValueError("collection extractions must be in source order")
        if len(sources) != len(set(sources)):
            raise ValueError("collection extraction sources must be unique")
        object.__setattr__(self, "manifest", _json_copy(self.manifest))

    @property
    def members(self) -> tuple[Extraction, ...]:
        """Compatibility spelling for callers that think in collection members."""

        return self.extractions

    @property
    def gaps(self) -> tuple[Gap, ...]:
        return tuple(item.as_gap() for item in self.declines)


@dataclass(slots=True)
class _AcquisitionState:
    limits: CollectionLimits
    registry: object | None = None
    extractions: list[Extraction] = field(default_factory=list)
    declines: list[MemberDecline] = field(default_factory=list)
    records: list[dict[str, Any]] = field(default_factory=list)
    ignored: list[dict[str, Any]] = field(default_factory=list)
    admitted_bytes: int = 0
    encountered_members: int = 0

    def decline(
        self,
        source: str,
        kind: DeclineKind,
        content: str,
        *,
        ref: str = "source",
        detected_kind: str | None = None,
        tier: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> MemberDecline:
        item = MemberDecline(
            kind=kind,
            content=content,
            origin=Origin(source, ref),
            detected_kind=detected_kind,
            tier=tier,
            details=details or {},
        )
        self.declines.append(item)
        return item

    def encounter(self, source: str) -> bool:
        self.encountered_members += 1
        if self.encountered_members <= self.limits.max_members:
            return True
        self.decline(
            source,
            DeclineKind.LIMIT,
            f"Member {source!r} exceeds the collection member limit "
            f"of {self.limits.max_members}.",
            details={"limit": "max_members", "maximum": self.limits.max_members},
        )
        return False

    def charge(self, source: str, size: int, *, maximum: int | None = None) -> bool:
        member_maximum = maximum or self.limits.max_member_bytes
        if size > member_maximum:
            self.decline(
                source,
                DeclineKind.LIMIT,
                f"Member {source!r} is {size} bytes; the limit is "
                f"{member_maximum} bytes.",
                details={
                    "limit": "member_bytes",
                    "bytes": size,
                    "maximum": member_maximum,
                },
            )
            return False
        if self.admitted_bytes + size > self.limits.max_total_bytes:
            self.decline(
                source,
                DeclineKind.LIMIT,
                f"Member {source!r} would exceed the collection byte limit "
                f"of {self.limits.max_total_bytes}.",
                details={
                    "limit": "max_total_bytes",
                    "bytes": size,
                    "admitted_before": self.admitted_bytes,
                    "maximum": self.limits.max_total_bytes,
                },
            )
            return False
        self.admitted_bytes += size
        return True


@dataclass(frozen=True, slots=True)
class _RouteFailure(Exception):
    kind: DeclineKind
    content: str
    detected_kind: str | None = None
    tier: int | None = None


def validate_extension_registry(registry: object) -> None:
    """Reserve the names of core Tier 2 acquisition capabilities."""

    from .extensions import ExtensionCollisionError, ExtensionRegistry

    if not isinstance(registry, ExtensionRegistry):
        raise TypeError("registry must be an ExtensionRegistry")
    core_names = {
        "archive",
        "crawl",
        "directory",
        "doc-site",
        "documentation-site",
        "git",
        "git-bare",
        "git-worktree",
        "repository",
        "tar",
        "tar-gzip",
        "zip",
    }
    for spec in registry.acquisitions:
        claimed = {spec.name, *spec.kinds, *spec.aliases}
        if overlap := sorted(claimed & core_names):
            raise ExtensionCollisionError(
                f"acquisition adapter {spec.name!r} collides with implemented "
                f"core capability {overlap[0]!r}"
            )


def acquire_collection(
    source: str | Path,
    *,
    limits: CollectionLimits | None = None,
    logical_source: str | None = None,
    registry: object | None = None,
) -> CollectionAcquisition:
    """Acquire a directory, git worktree, archive, or documentation site.

    URLs are documentation-site roots and remain URL-addressed.  Local roots
    default to their basename as the logical container identity, so relocating
    an unchanged folder or archive beneath another parent does not rewrite
    every unit ID.  A caller can supply a different stable logical identity
    explicitly when a project has one.
    """

    if registry is not None:
        validate_extension_registry(registry)
    selected_limits = limits or CollectionLimits()
    if isinstance(source, str) and _looks_like_http_url(source):
        if logical_source is not None:
            raise ValueError("logical_source is not accepted for documentation sites")
        return crawl_documentation_site(
            source,
            limits=selected_limits,
            registry=registry,
        )

    path = Path(source)
    try:
        root_stat = path.lstat()
    except FileNotFoundError as exc:
        raise CollectionError(f"{path}: no such collection") from exc
    except OSError as exc:
        raise CollectionError(f"{path}: cannot inspect collection: {exc}") from exc

    import stat

    if stat.S_ISLNK(root_stat.st_mode):
        raise CollectionError(f"{path}: collection root must not be a symlink")
    logical = _logical_root(path, logical_source)
    if stat.S_ISDIR(root_stat.st_mode):
        return acquire_directory(
            path,
            limits=selected_limits,
            logical_source=logical,
            registry=registry,
        )
    if stat.S_ISREG(root_stat.st_mode):
        return acquire_archive(
            path,
            limits=selected_limits,
            logical_source=logical,
            registry=registry,
        )
    raise CollectionError(f"{path}: collection root is not a directory or archive file")


def acquire_directory(
    root: Path,
    *,
    limits: CollectionLimits | None = None,
    logical_source: str | None = None,
    registry: object | None = None,
) -> CollectionAcquisition:
    """Expand one local directory or git worktree without following links."""

    if registry is not None:
        validate_extension_registry(registry)
    selected_limits = limits or CollectionLimits()
    logical = _logical_root(root, logical_source)
    try:
        root_stat = root.lstat()
    except OSError as exc:
        raise CollectionError(f"{root}: cannot inspect directory: {exc}") from exc

    import stat

    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise CollectionError(f"{root}: directory collection must be a real directory")

    repository_kind = _repository_kind(root)
    state = _AcquisitionState(selected_limits, registry=registry)
    if repository_kind == "git-bare":
        state.decline(
            logical,
            DeclineKind.UNSUPPORTED,
            f"Bare git repository {logical!r} has no worktree to acquire.",
            detected_kind="git-bare",
            tier=2,
        )
    else:
        _walk_directory(root, logical, state, depth=0)

    kind = repository_kind or "directory"
    container = {
        "source": logical,
        "kind": kind,
        "repository": repository_kind is not None,
    }
    return _finish(logical, kind, state, container=container)


def acquire_archive(
    path: Path,
    *,
    limits: CollectionLimits | None = None,
    logical_source: str | None = None,
    registry: object | None = None,
) -> CollectionAcquisition:
    """Expand a ZIP or TAR-family archive from one stable byte snapshot."""

    if registry is not None:
        validate_extension_registry(registry)
    selected_limits = limits or CollectionLimits()
    logical = _logical_root(path, logical_source)
    state = _AcquisitionState(selected_limits, registry=registry)

    try:
        payload, digest = _read_stable_path(
            path,
            maximum=selected_limits.max_archive_container_bytes,
        )
    except CollectionError:
        raise
    except OSError as exc:  # pragma: no cover - normalized by _read_stable_path
        raise CollectionError(f"{path}: cannot read archive: {exc}") from exc

    if not state.charge(
        logical,
        len(payload),
        maximum=selected_limits.max_archive_container_bytes,
    ):
        container = {
            "source": logical,
            "kind": "archive",
            "bytes": len(payload),
            "sha256": digest,
        }
        return _finish(logical, "archive", state, container=container)

    archive_kind = _archive_kind(payload, path.name)
    if archive_kind is None:
        raise CollectionError(f"{path}: bytes are not a supported ZIP or TAR archive")
    state.records.append(
        {
            "status": "container",
            "source": logical,
            "kind": archive_kind,
            "bytes": len(payload),
            "sha256": digest,
        }
    )
    _walk_archive(payload, logical, archive_kind, state, archive_depth=0)
    container = {
        "source": logical,
        "kind": archive_kind,
        "bytes": len(payload),
        "sha256": digest,
    }
    return _finish(logical, archive_kind, state, container=container)


def crawl_documentation_site(
    start_url: str,
    *,
    limits: CollectionLimits | None = None,
    registry: object | None = None,
) -> CollectionAcquisition:
    """Crawl a bounded same-origin documentation site in breadth-first order."""

    if registry is not None:
        validate_extension_registry(registry)
    selected_limits = limits or CollectionLimits()
    root = _canonical_http_url(start_url)
    root_origin = _url_origin(root)
    state = _AcquisitionState(selected_limits, registry=registry)
    frontier: list[tuple[int, str]] = [(0, root)]
    scheduled = {root}
    acquired_sources: set[str] = set()
    requests = 0
    crawl_bytes = 0

    while frontier:
        frontier.sort(key=lambda item: (item[0], item[1]))
        depth, requested = frontier.pop(0)
        if requests >= selected_limits.max_crawl_pages:
            state.decline(
                requested,
                DeclineKind.LIMIT,
                f"Documentation page {requested!r} exceeds the crawl page limit "
                f"of {selected_limits.max_crawl_pages}.",
                details={
                    "limit": "max_crawl_pages",
                    "maximum": selected_limits.max_crawl_pages,
                    "depth": depth,
                },
            )
            for queued_depth, queued in frontier:
                state.decline(
                    queued,
                    DeclineKind.LIMIT,
                    f"Documentation page {queued!r} was not acquired because the "
                    f"crawl page limit is {selected_limits.max_crawl_pages}.",
                    details={
                        "limit": "max_crawl_pages",
                        "maximum": selected_limits.max_crawl_pages,
                        "depth": queued_depth,
                    },
                )
            break
        requests += 1

        try:
            result = _route_url(
                requested,
                timeout=selected_limits.crawl_timeout_seconds,
                max_bytes=selected_limits.max_crawl_page_bytes,
                registry=registry,
                same_origin_with=root,
            )
        except _RouteFailure as failure:
            state.decline(
                requested,
                failure.kind,
                failure.content,
                detected_kind=failure.detected_kind,
                tier=failure.tier,
                details={"depth": depth, "requested_url": requested},
            )
            continue

        actual = _canonical_http_url(result.source)
        if _url_origin(actual) != root_origin:
            state.decline(
                actual,
                DeclineKind.CROSS_ORIGIN,
                f"Documentation request {requested!r} resolved outside the "
                f"starting origin to {actual!r}.",
                details={"requested_url": requested, "depth": depth},
            )
            continue

        input_record = _one_input_record(result)
        size = input_record.get("bytes")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            state.decline(
                actual,
                DeclineKind.EXTRACTION,
                f"Documentation page {actual!r} has no exact acquired byte count.",
                details={"requested_url": requested, "depth": depth},
            )
            continue
        if crawl_bytes + size > selected_limits.max_crawl_total_bytes:
            state.decline(
                actual,
                DeclineKind.LIMIT,
                f"Documentation page {actual!r} would exceed the crawl byte "
                f"limit of {selected_limits.max_crawl_total_bytes}.",
                details={
                    "limit": "max_crawl_total_bytes",
                    "bytes": size,
                    "admitted_before": crawl_bytes,
                    "maximum": selected_limits.max_crawl_total_bytes,
                    "depth": depth,
                },
            )
            continue
        if actual in acquired_sources:
            state.ignored.append(
                {
                    "status": "ignored",
                    "source": requested,
                    "reason": "duplicate-acquired-source",
                    "acquired_source": actual,
                }
            )
            continue

        crawl_bytes += size
        state.admitted_bytes += size
        acquired_sources.add(actual)
        result = _rebase_extraction(
            result,
            actual,
            old_source=result.source,
            byte_count=size,
            digest=str(input_record.get("sha256") or ""),
            input_kind=str(input_record.get("kind") or result.kind),
            tier=_optional_int(input_record.get("tier")),
        )
        result.meta["collection_member"] = {
            "collection": root,
            "kind": "documentation-site",
            "requested_url": requested,
            "depth": depth,
        }
        state.extractions.append(result)
        state.records.append(
            _extraction_record(
                result,
                details={"requested_url": requested, "depth": depth},
            )
        )

        for target, referrer in _crawl_targets(result):
            try:
                canonical = _canonical_http_url(target)
            except CollectionError:
                state.decline(
                    target,
                    DeclineKind.UNSAFE,
                    f"Documentation link {target!r} is not a valid HTTP(S) URL.",
                    details={"referrer": referrer, "depth": depth + 1},
                )
                continue
            if _url_origin(canonical) != root_origin:
                state.decline(
                    canonical,
                    DeclineKind.CROSS_ORIGIN,
                    f"Documentation link {canonical!r} leaves the starting origin.",
                    details={"referrer": referrer, "depth": depth + 1},
                )
                continue
            if canonical in scheduled or canonical in acquired_sources:
                state.ignored.append(
                    {
                        "status": "ignored",
                        "source": canonical,
                        "reason": "crawl-loop-or-duplicate",
                        "referrer": referrer,
                    }
                )
                continue
            if depth >= selected_limits.max_crawl_depth:
                state.decline(
                    canonical,
                    DeclineKind.LIMIT,
                    f"Documentation link {canonical!r} exceeds the crawl depth "
                    f"limit of {selected_limits.max_crawl_depth}.",
                    details={
                        "limit": "max_crawl_depth",
                        "maximum": selected_limits.max_crawl_depth,
                        "referrer": referrer,
                        "depth": depth + 1,
                    },
                )
                continue
            scheduled.add(canonical)
            frontier.append((depth + 1, canonical))

    container = {
        "source": root,
        "kind": "documentation-site",
        "origin": list(root_origin),
        "requests": requests,
        "bytes": crawl_bytes,
    }
    return _finish(root, "documentation-site", state, container=container)


def _walk_directory(
    physical: Path,
    logical: str,
    state: _AcquisitionState,
    *,
    depth: int,
) -> None:
    try:
        children = sorted(physical.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        state.decline(
            logical,
            DeclineKind.EXTRACTION,
            f"Directory {logical!r} could not be listed: {exc}.",
        )
        return

    import stat

    for child in children:
        shown_name = _utf8_display(child.name)
        child_logical = f"{logical}/{shown_name}"
        try:
            info = child.lstat()
        except OSError as exc:
            state.decline(
                child_logical,
                DeclineKind.EXTRACTION,
                f"Member {child_logical!r} could not be inspected: {exc}.",
            )
            continue

        is_directory = stat.S_ISDIR(info.st_mode)
        ignored_reason = _ignored_child_reason(child.name, is_directory=is_directory)
        if ignored_reason is not None:
            state.ignored.append(
                {
                    "status": "ignored",
                    "source": child_logical,
                    "reason": ignored_reason,
                }
            )
            continue
        if stat.S_ISLNK(info.st_mode):
            if state.encounter(child_logical):
                state.decline(
                    child_logical,
                    DeclineKind.UNSAFE,
                    f"Member {child_logical!r} is a symlink; collection traversal "
                    "never follows symlinks.",
                )
            continue
        if is_directory:
            if depth >= state.limits.max_directory_depth:
                if state.encounter(child_logical):
                    state.decline(
                        child_logical,
                        DeclineKind.LIMIT,
                        f"Directory {child_logical!r} exceeds the directory depth "
                        f"limit of {state.limits.max_directory_depth}.",
                        details={
                            "limit": "max_directory_depth",
                            "maximum": state.limits.max_directory_depth,
                        },
                    )
                continue
            _walk_directory(child, child_logical, state, depth=depth + 1)
            continue
        if not stat.S_ISREG(info.st_mode):
            if state.encounter(child_logical):
                state.decline(
                    child_logical,
                    DeclineKind.UNSAFE,
                    f"Member {child_logical!r} is not a regular file.",
                )
            continue
        if not state.encounter(child_logical):
            continue

        maximum = (
            state.limits.max_archive_container_bytes
            if _archive_name(child.name)
            else state.limits.max_member_bytes
        )
        if info.st_size > maximum:
            state.decline(
                child_logical,
                DeclineKind.LIMIT,
                f"Member {child_logical!r} is {info.st_size} bytes; the limit "
                f"is {maximum} bytes.",
                details={
                    "limit": "member_bytes",
                    "bytes": info.st_size,
                    "maximum": maximum,
                },
            )
            continue
        try:
            payload, digest = _read_stable_path(child, maximum=maximum)
        except (CollectionError, OSError) as exc:
            state.decline(
                child_logical,
                DeclineKind.EXTRACTION,
                _scrub_message(str(exc), str(child), child_logical),
            )
            continue
        if not state.charge(child_logical, len(payload), maximum=maximum):
            continue

        archive_kind = _archive_kind(payload, child.name) if _archive_name(child.name) else None
        if archive_kind is not None:
            state.records.append(
                {
                    "status": "container",
                    "source": child_logical,
                    "kind": archive_kind,
                    "bytes": len(payload),
                    "sha256": digest,
                }
            )
            _walk_archive(
                payload,
                child_logical,
                archive_kind,
                state,
                archive_depth=0,
            )
            continue
        if _archive_name(child.name):
            state.decline(
                child_logical,
                DeclineKind.EXTRACTION,
                f"Archive member {child_logical!r} is not a valid ZIP or TAR archive.",
                detected_kind="archive",
                tier=2,
                details={"bytes": len(payload), "sha256": digest},
            )
            continue
        _admit_payload(
            payload,
            shown_name,
            child_logical,
            state,
            container=logical,
            digest=digest,
        )


def _walk_archive(
    payload: bytes,
    container_source: str,
    archive_kind: str,
    state: _AcquisitionState,
    *,
    archive_depth: int,
) -> None:
    if archive_kind == "zip":
        _walk_zip(payload, container_source, state, archive_depth=archive_depth)
        return
    _walk_tar(payload, container_source, state, archive_depth=archive_depth)


def _walk_zip(
    payload: bytes,
    container_source: str,
    state: _AcquisitionState,
    *,
    archive_depth: int,
) -> None:
    import io
    import stat
    import zipfile

    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            infos = archive.infolist()
            if len(infos) > state.limits.max_archive_members:
                state.decline(
                    container_source,
                    DeclineKind.LIMIT,
                    f"Archive {container_source!r} has {len(infos)} entries; "
                    f"the limit is {state.limits.max_archive_members}.",
                    details={
                        "limit": "max_archive_members",
                        "entries": len(infos),
                        "maximum": state.limits.max_archive_members,
                    },
                )
                infos = infos[: state.limits.max_archive_members]
            counts = _name_counts(info.filename for info in infos if not info.is_dir())
            for info in sorted(infos, key=lambda item: item.filename):
                if info.is_dir():
                    state.ignored.append(
                        {
                            "status": "ignored",
                            "source": _archive_member_source(container_source, info.filename),
                            "reason": "archive-directory-entry",
                        }
                    )
                    continue
                member_source = _archive_member_source(container_source, info.filename)
                if not state.encounter(member_source):
                    continue
                unsafe = _unsafe_archive_name(info.filename, state.limits)
                if unsafe is not None:
                    state.decline(
                        member_source,
                        DeclineKind.UNSAFE,
                        f"Archive member {info.filename!r} is unsafe: {unsafe}.",
                        details={"container": container_source, "member": info.filename},
                    )
                    continue
                if counts.get(info.filename, 0) > 1:
                    state.decline(
                        member_source,
                        DeclineKind.DUPLICATE,
                        f"Archive {container_source!r} contains duplicate member "
                        f"name {info.filename!r}; no copy was selected.",
                        details={"container": container_source, "member": info.filename},
                    )
                    continue
                mode = (info.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(mode):
                    state.decline(
                        member_source,
                        DeclineKind.UNSAFE,
                        f"Archive member {info.filename!r} is a symlink.",
                        details={"container": container_source, "member": info.filename},
                    )
                    continue
                if info.flag_bits & 0x1:
                    state.decline(
                        member_source,
                        DeclineKind.ENCRYPTED,
                        f"Archive member {info.filename!r} is encrypted.",
                        details={"container": container_source, "member": info.filename},
                    )
                    continue
                if _compression_ratio_exceeded(
                    info.file_size,
                    info.compress_size,
                    state.limits.max_compression_ratio,
                ):
                    state.decline(
                        member_source,
                        DeclineKind.LIMIT,
                        f"Archive member {info.filename!r} exceeds the compression "
                        f"ratio limit of {state.limits.max_compression_ratio:g}.",
                        details={
                            "limit": "max_compression_ratio",
                            "bytes": info.file_size,
                            "compressed_bytes": info.compress_size,
                            "maximum": state.limits.max_compression_ratio,
                        },
                    )
                    continue
                if not state.charge(member_source, info.file_size):
                    continue
                try:
                    member_payload = archive.read(info)
                except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                    state.decline(
                        member_source,
                        DeclineKind.EXTRACTION,
                        f"Archive member {info.filename!r} could not be read: "
                        f"{type(exc).__name__}.",
                        details={"container": container_source, "member": info.filename},
                    )
                    continue
                if len(member_payload) != info.file_size:
                    state.decline(
                        member_source,
                        DeclineKind.EXTRACTION,
                        f"Archive member {info.filename!r} declared {info.file_size} "
                        f"bytes but yielded {len(member_payload)}.",
                    )
                    continue
                _admit_archive_payload(
                    member_payload,
                    info.filename,
                    member_source,
                    state,
                    container=container_source,
                    archive_depth=archive_depth,
                )
    except zipfile.BadZipFile as exc:
        state.decline(
            container_source,
            DeclineKind.EXTRACTION,
            f"Archive {container_source!r} is an invalid ZIP: {exc}.",
            detected_kind="archive",
            tier=2,
        )


def _walk_tar(
    payload: bytes,
    container_source: str,
    state: _AcquisitionState,
    *,
    archive_depth: int,
) -> None:
    import io
    import tarfile

    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*") as archive:
            members: list[Any] = []
            for offset, member in enumerate(archive):
                if offset >= state.limits.max_archive_members:
                    state.decline(
                        container_source,
                        DeclineKind.LIMIT,
                        f"Archive {container_source!r} exceeds the entry limit "
                        f"of {state.limits.max_archive_members}.",
                        details={
                            "limit": "max_archive_members",
                            "maximum": state.limits.max_archive_members,
                        },
                    )
                    break
                members.append(member)
            declared_regular_bytes = sum(
                member.size for member in members if member.isfile()
            )
            if _compression_ratio_exceeded(
                declared_regular_bytes,
                len(payload),
                state.limits.max_compression_ratio,
            ):
                state.decline(
                    container_source,
                    DeclineKind.LIMIT,
                    f"Archive {container_source!r} exceeds the compression ratio "
                    f"limit of {state.limits.max_compression_ratio:g}.",
                    details={
                        "limit": "max_compression_ratio",
                        "bytes": declared_regular_bytes,
                        "compressed_bytes": len(payload),
                        "maximum": state.limits.max_compression_ratio,
                    },
                )
                return
            counts = _name_counts(member.name for member in members if member.isfile())
            for member in sorted(members, key=lambda item: item.name):
                member_source = _archive_member_source(container_source, member.name)
                if member.isdir():
                    state.ignored.append(
                        {
                            "status": "ignored",
                            "source": member_source,
                            "reason": "archive-directory-entry",
                        }
                    )
                    continue
                if not state.encounter(member_source):
                    continue
                unsafe = _unsafe_archive_name(member.name, state.limits)
                if unsafe is not None:
                    state.decline(
                        member_source,
                        DeclineKind.UNSAFE,
                        f"Archive member {member.name!r} is unsafe: {unsafe}.",
                        details={"container": container_source, "member": member.name},
                    )
                    continue
                if member.issym() or member.islnk():
                    state.decline(
                        member_source,
                        DeclineKind.UNSAFE,
                        f"Archive member {member.name!r} is a link.",
                        details={"container": container_source, "member": member.name},
                    )
                    continue
                if not member.isfile():
                    state.decline(
                        member_source,
                        DeclineKind.UNSAFE,
                        f"Archive member {member.name!r} is not a regular file.",
                        details={"container": container_source, "member": member.name},
                    )
                    continue
                if counts.get(member.name, 0) > 1:
                    state.decline(
                        member_source,
                        DeclineKind.DUPLICATE,
                        f"Archive {container_source!r} contains duplicate member "
                        f"name {member.name!r}; no copy was selected.",
                        details={"container": container_source, "member": member.name},
                    )
                    continue
                if not state.charge(member_source, member.size):
                    continue
                stream = archive.extractfile(member)
                if stream is None:
                    state.decline(
                        member_source,
                        DeclineKind.EXTRACTION,
                        f"Archive member {member.name!r} has no readable payload.",
                    )
                    continue
                try:
                    member_payload = stream.read(state.limits.max_member_bytes + 1)
                finally:
                    stream.close()
                if len(member_payload) != member.size:
                    state.decline(
                        member_source,
                        DeclineKind.EXTRACTION,
                        f"Archive member {member.name!r} declared {member.size} "
                        f"bytes but yielded {len(member_payload)}.",
                    )
                    continue
                _admit_archive_payload(
                    member_payload,
                    member.name,
                    member_source,
                    state,
                    container=container_source,
                    archive_depth=archive_depth,
                )
    except (tarfile.ReadError, OSError) as exc:
        state.decline(
            container_source,
            DeclineKind.EXTRACTION,
            f"Archive {container_source!r} is an invalid TAR: {exc}.",
            detected_kind="archive",
            tier=2,
        )


def _admit_archive_payload(
    payload: bytes,
    name: str,
    member_source: str,
    state: _AcquisitionState,
    *,
    container: str,
    archive_depth: int,
) -> None:
    nested_kind = _archive_kind(payload, name) if _archive_name(name) else None
    if nested_kind is not None:
        if archive_depth >= state.limits.max_archive_depth:
            state.decline(
                member_source,
                DeclineKind.LIMIT,
                f"Nested archive {member_source!r} exceeds the archive depth "
                f"limit of {state.limits.max_archive_depth}.",
                details={
                    "limit": "max_archive_depth",
                    "maximum": state.limits.max_archive_depth,
                },
            )
            return
        state.records.append(
            {
                "status": "container",
                "source": member_source,
                "kind": nested_kind,
                "bytes": len(payload),
                "sha256": _sha256(payload),
                "container": container,
            }
        )
        _walk_archive(
            payload,
            member_source,
            nested_kind,
            state,
            archive_depth=archive_depth + 1,
        )
        return
    if _archive_name(name):
        state.decline(
            member_source,
            DeclineKind.EXTRACTION,
            f"Nested archive {member_source!r} is not a valid ZIP or TAR archive.",
            detected_kind="archive",
            tier=2,
        )
        return
    _admit_payload(
        payload,
        name,
        member_source,
        state,
        container=container,
        digest=_sha256(payload),
    )


def _admit_payload(
    payload: bytes,
    display_name: str,
    logical_source: str,
    state: _AcquisitionState,
    *,
    container: str,
    digest: str,
) -> None:
    try:
        extraction = _route_payload(
            payload,
            display_name,
            logical_source,
            registry=state.registry,
        )
    except _RouteFailure as failure:
        state.decline(
            logical_source,
            failure.kind,
            failure.content,
            detected_kind=failure.detected_kind,
            tier=failure.tier,
            details={
                "container": container,
                "bytes": len(payload),
                "sha256": digest,
            },
        )
        return
    extraction.meta["collection_member"] = {
        "collection": container,
        "bytes": len(payload),
        "sha256": digest,
    }
    state.extractions.append(extraction)
    state.records.append(_extraction_record(extraction, details={"container": container}))


def _route_payload(
    payload: bytes,
    display_name: str,
    logical_source: str,
    *,
    registry: object | None = None,
) -> Extraction:
    """Run the production router on bytes, then erase the materialization path."""

    import tempfile

    # The complete suffix, not merely Path.suffix, preserves language routing
    # for names such as component.test.ts. Archive suffixes never reach here.
    suffix = Path(display_name).suffix
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
            handle.write(payload)
            temporary = Path(handle.name)
        from . import router

        result = router.extract(temporary, registry=registry)
    except Exception as exc:
        from . import router

        physical = str(temporary) if temporary is not None else display_name
        message = _scrub_message(str(exc), physical, logical_source)
        if isinstance(exc, router.UnsupportedFormat):
            raise _RouteFailure(
                DeclineKind.UNSUPPORTED,
                message,
                detected_kind=exc.kind,
                tier=exc.tier,
            ) from exc
        if isinstance(exc, router.UnknownFormat):
            raise _RouteFailure(
                DeclineKind.UNSUPPORTED,
                message,
                detected_kind="unknown",
            ) from exc
        if isinstance(exc, (ImportError, OSError, UnicodeError, ValueError)):
            raise _RouteFailure(DeclineKind.EXTRACTION, message) from exc
        raise
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

    input_record = _one_input_record(result)
    return _rebase_extraction(
        result,
        logical_source,
        old_source=str(temporary),
        byte_count=len(payload),
        digest=_sha256(payload),
        input_kind=str(input_record.get("kind") or result.kind),
        tier=_optional_int(input_record.get("tier")),
    )


def _route_url(
    url: str,
    *,
    timeout: float,
    max_bytes: int,
    registry: object | None = None,
    same_origin_with: str | None = None,
) -> Extraction:
    from . import router

    try:
        return router.extract_url(
            url,
            timeout=timeout,
            max_bytes=max_bytes,
            registry=registry,
            same_origin_with=same_origin_with,
        )
    except router.UnsupportedFormat as exc:
        raise _RouteFailure(
            DeclineKind.UNSUPPORTED,
            str(exc),
            detected_kind=exc.kind,
            tier=exc.tier,
        ) from exc
    except router.UnknownFormat as exc:
        raise _RouteFailure(
            DeclineKind.UNSUPPORTED,
            str(exc),
            detected_kind="unknown",
        ) from exc
    except (ImportError, OSError, UnicodeError, ValueError) as exc:
        message = str(exc)
        lowered = message.casefold()
        if "same-origin root" in lowered:
            kind = DeclineKind.CROSS_ORIGIN
        elif "limit" in lowered or "exceed" in lowered:
            kind = DeclineKind.LIMIT
        else:
            kind = DeclineKind.EXTRACTION
        raise _RouteFailure(kind, message) from exc


def _rebase_extraction(
    result: Extraction,
    logical_source: str,
    *,
    old_source: str,
    byte_count: int,
    digest: str,
    input_kind: str,
    tier: int | None,
) -> Extraction:
    """Give a routed snapshot a stable logical source without dangling IDs."""

    original_inputs = result.meta.get("inputs")
    original_adapter = (
        original_inputs[0].get("adapter")
        if isinstance(original_inputs, list)
        and len(original_inputs) == 1
        and isinstance(original_inputs[0], dict)
        else None
    )
    ids: dict[str, str] = {}
    provisional: list[Unit] = []
    for unit in result.units:
        rebased = Unit(
            source=logical_source,
            modality=unit.modality,
            content=unit.content,
            origin=Origin(logical_source, unit.origin.ref, unit.origin.char_span),
            role=unit.role,
            structure=unit.structure,
            salience=unit.salience,
            confidence=unit.confidence,
            tokens=unit.tokens,
            meta=_replace_source(unit.meta, old_source, logical_source),
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
            evidence=_scrub_message(relation.evidence, old_source, logical_source),
            confidence=relation.confidence,
        )
        for relation in result.relations
    ]
    gaps = [
        Gap(
            _scrub_message(str(gap), old_source, logical_source),
            Origin(logical_source, gap.origin.ref, gap.origin.char_span),
            gap.kind,
        )
        for gap in result.gaps
    ]
    statements = [
        GroundedStatement(
            content=_scrub_message(statement.content, old_source, logical_source),
            origins=tuple(
                Origin(logical_source, origin.ref, origin.char_span)
                for origin in statement.origins
            ),
            evidence_unit_ids=tuple(
                ids.get(unit_id, unit_id) for unit_id in statement.evidence_unit_ids
            ),
        )
        for statement in result.summary_claims
    ]
    meta = _rewrite_ids(
        _replace_source(result.meta, old_source, logical_source),
        ids,
    )
    input_record: dict[str, Any] = {
        "source": logical_source,
        "kind": input_kind,
        "bytes": byte_count,
        "sha256": digest,
    }
    if tier is not None:
        input_record["tier"] = tier
    if original_adapter is not None:
        input_record["adapter"] = _json_copy(original_adapter)
    meta["inputs"] = [input_record]
    return Extraction(
        source=logical_source,
        kind=result.kind,
        units=units,
        relations=relations,
        gaps=gaps,
        meta=meta,
        summary_claims=statements,
    )


def _finish(
    source: str,
    kind: str,
    state: _AcquisitionState,
    *,
    container: dict[str, Any],
) -> CollectionAcquisition:
    extractions = tuple(sorted(state.extractions, key=lambda item: item.source))
    declines = tuple(
        sorted(
            state.declines,
            key=lambda item: (item.source, str(item.kind), item.content),
        )
    )
    records = [*state.records, *(item.as_manifest() for item in declines), *state.ignored]
    records.sort(
        key=lambda item: (
            str(item.get("source") or ""),
            str(item.get("status") or ""),
            str(item.get("decline_kind") or item.get("reason") or ""),
        )
    )
    for index, record in enumerate(records):
        record["order"] = index
    manifest: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "source": source,
        "kind": kind,
        "container": _json_copy(container),
        "policy": {
            "hidden_entries": HIDDEN_ENTRY_POLICY,
            "internal_directories": sorted(INTERNAL_DIRECTORY_NAMES),
            "symlinks": "decline-never-follow",
            "member_failures": "typed-decline-keep-siblings",
            "ordering": "logical-source-codepoint-v1",
            "logical_source": "root-basename-or-explicit/member-posix-v1",
        },
        "limits": asdict(state.limits),
        "members": records,
        "counts": {
            "extracted": len(extractions),
            "declined": len(declines),
            "ignored": len(state.ignored),
            "records": len(records),
        },
        "admitted_bytes": state.admitted_bytes,
    }
    if state.registry is not None and len(state.registry):  # type: ignore[arg-type]
        manifest["extensions"] = state.registry.capability_manifest()  # type: ignore[attr-defined]
    manifest["sha256"] = _manifest_digest(manifest)
    return CollectionAcquisition(source, kind, extractions, declines, manifest)


def _extraction_record(
    extraction: Extraction, *, details: dict[str, Any] | None = None
) -> dict[str, Any]:
    input_record = _one_input_record(extraction)
    record: dict[str, Any] = {
        "status": "extracted",
        "source": extraction.source,
        "kind": extraction.kind,
        "units": len(extraction.units),
        "relations": len(extraction.relations),
        "gaps": len(extraction.gaps),
        "bytes": input_record.get("bytes"),
        "sha256": input_record.get("sha256"),
    }
    if "tier" in input_record:
        record["tier"] = input_record["tier"]
    if details:
        record["details"] = _json_copy(details)
    return record


def _one_input_record(result: Extraction) -> dict[str, Any]:
    inputs = result.meta.get("inputs")
    if not isinstance(inputs, list) or len(inputs) != 1 or not isinstance(inputs[0], dict):
        raise _RouteFailure(
            DeclineKind.EXTRACTION,
            f"Routed member {result.source!r} has no exact single-input manifest.",
        )
    return dict(inputs[0])


def _read_stable_path(path: Path, *, maximum: int) -> tuple[bytes, str]:
    import stat

    try:
        before = path.lstat()
    except OSError as exc:
        raise CollectionError(f"{path}: cannot inspect member: {exc}") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise CollectionError(f"{path}: member is not a stable regular file")
    if before.st_size > maximum:
        raise CollectionError(
            f"{path}: member is {before.st_size} bytes; limit is {maximum}"
        )
    try:
        with path.open("rb") as stream:
            payload = stream.read(maximum + 1)
    except OSError as exc:
        raise CollectionError(f"{path}: cannot read member: {exc}") from exc
    if len(payload) > maximum:
        raise CollectionError(f"{path}: member exceeds {maximum} bytes")
    try:
        after = path.lstat()
    except OSError as exc:
        raise CollectionError(f"{path}: member changed while being read") from exc
    identity = lambda value: (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
    )
    if identity(before) != identity(after) or len(payload) != before.st_size:
        raise CollectionError(f"{path}: member changed while being read")
    return payload, _sha256(payload)


def _repository_kind(root: Path) -> str | None:
    git_marker = root / ".git"
    if git_marker.exists() or git_marker.is_symlink():
        return "git-worktree"
    if (
        (root / "HEAD").is_file()
        and (root / "objects").is_dir()
        and (root / "refs").is_dir()
    ):
        return "git-bare"
    return None


def _ignored_child_reason(name: str, *, is_directory: bool) -> str | None:
    if name.startswith("."):
        return "hidden-directory" if is_directory else "hidden-file"
    if is_directory and name in INTERNAL_DIRECTORY_NAMES:
        return "internal-directory"
    return None


def _logical_root(path: Path, explicit: str | None) -> str:
    value = explicit if explicit is not None else path.name
    value = value.strip().replace("\\", "/")
    if not value or value in {".", ".."} or value.startswith("/"):
        raise ValueError("logical collection source must be a non-empty relative name")
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError("logical collection source contains an unsafe path segment")
    value.encode("utf-8", errors="strict")
    return value.rstrip("/")


def _archive_name(name: str) -> bool:
    lowered = name.casefold()
    return any(lowered.endswith(suffix) for suffix in _ARCHIVE_SUFFIXES)


def _archive_kind(payload: bytes, name: str) -> str | None:
    import io
    import tarfile
    import zipfile

    stream = io.BytesIO(payload)
    if zipfile.is_zipfile(stream):
        return "zip"
    if not _archive_name(name):
        return None
    stream.seek(0)
    try:
        with tarfile.open(fileobj=stream, mode="r:*"):
            return "tar"
    except (tarfile.ReadError, OSError):
        return None


def _archive_member_source(container: str, name: str) -> str:
    shown = _utf8_display(name).replace("\\", "/").lstrip("/") or "<empty>"
    return f"{container}!/{shown}"


def _unsafe_archive_name(name: str, limits: CollectionLimits) -> str | None:
    if not name or "\x00" in name:
        return "empty name or NUL byte"
    try:
        name.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return "name is not canonical UTF-8"
    if "\\" in name:
        return "backslash path separator is ambiguous"
    if name.startswith("/"):
        return "absolute path"
    if len(name) >= 2 and name[0].isalpha() and name[1] == ":":
        return "drive-qualified path"
    raw_parts = name.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        return "path traversal segment"
    path = PurePosixPath(name)
    if len(path.parts) > limits.max_archive_path_depth:
        return f"path depth exceeds {limits.max_archive_path_depth}"
    return None


def _name_counts(names: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for name in names:
        counts[name] = counts.get(name, 0) + 1
    return counts


def _compression_ratio_exceeded(size: int, compressed: int, maximum: float) -> bool:
    if size <= 1024:
        return False
    return size / max(compressed, 1) > maximum


def _crawl_targets(result: Extraction) -> tuple[tuple[str, str], ...]:
    targets: set[tuple[str, str]] = set()
    for unit in result.units:
        target = unit.meta.get("target")
        if unit.meta.get("ref_kind") != "url" or not isinstance(target, str):
            continue
        if not _looks_like_http_url(target):
            continue
        targets.add((target, str(unit.origin)))
    return tuple(sorted(targets))


def _looks_like_http_url(value: str) -> bool:
    lowered = value.casefold()
    return lowered.startswith("http://") or lowered.startswith("https://")


def _canonical_http_url(value: str) -> str:
    from urllib.parse import urldefrag, urlsplit, urlunsplit

    without_fragment, _fragment = urldefrag(value)
    parsed = urlsplit(without_fragment)
    scheme = parsed.scheme.casefold()
    if scheme not in {"http", "https"} or not parsed.hostname:
        raise CollectionError(f"invalid documentation URL {value!r}")
    if parsed.username is not None or parsed.password is not None:
        raise CollectionError("documentation URLs must not contain credentials")
    try:
        port = parsed.port
    except ValueError as exc:
        raise CollectionError(f"invalid documentation URL {value!r}: {exc}") from exc
    default = 443 if scheme == "https" else 80
    host = parsed.hostname.casefold()
    shown_host = f"[{host}]" if ":" in host else host
    netloc = shown_host if port in {None, default} else f"{shown_host}:{port}"
    path = parsed.path or "/"
    return urlunsplit((scheme, netloc, path, parsed.query, ""))


def _url_origin(value: str) -> tuple[str, str, int]:
    from urllib.parse import urlsplit

    parsed = urlsplit(value)
    scheme = parsed.scheme.casefold()
    return (
        scheme,
        (parsed.hostname or "").casefold(),
        parsed.port or (443 if scheme == "https" else 80),
    )


def _replace_source(value: Any, old: str, new: str) -> Any:
    if old == new:
        return value
    if isinstance(value, str):
        return value.replace(old, new).replace(Path(old).name, new)
    if isinstance(value, dict):
        return {
            _replace_source(key, old, new): _replace_source(item, old, new)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_replace_source(item, old, new) for item in value]
    if isinstance(value, tuple):
        return tuple(_replace_source(item, old, new) for item in value)
    if isinstance(value, set):
        return {_replace_source(item, old, new) for item in value}
    if isinstance(value, frozenset):
        return frozenset(_replace_source(item, old, new) for item in value)
    return value


def _rewrite_ids(value: Any, ids: dict[str, str]) -> Any:
    if isinstance(value, str):
        return ids.get(value, value)
    if isinstance(value, dict):
        return {
            _rewrite_ids(key, ids): _rewrite_ids(item, ids)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_rewrite_ids(item, ids) for item in value]
    if isinstance(value, tuple):
        return tuple(_rewrite_ids(item, ids) for item in value)
    if isinstance(value, set):
        return {_rewrite_ids(item, ids) for item in value}
    if isinstance(value, frozenset):
        return frozenset(_rewrite_ids(item, ids) for item in value)
    return value


def _scrub_message(message: str, physical: str, logical: str) -> str:
    if physical == logical:
        return message
    return message.replace(physical, logical).replace(Path(physical).name, logical)


def _utf8_display(value: str) -> str:
    """Return a stable printable spelling even for surrogateescaped OS names."""

    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        import os
        from urllib.parse import quote_from_bytes

        return quote_from_bytes(os.fsencode(value), safe="-._~/")
    return value


def _sha256(payload: bytes) -> str:
    import hashlib

    return hashlib.sha256(payload).hexdigest()


def _manifest_digest(manifest: dict[str, Any]) -> str:
    import json

    canonical = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256(canonical)


def _json_copy(value: Any) -> Any:
    import json

    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _origin_record(origin: Origin) -> dict[str, Any]:
    record: dict[str, Any] = {"source": origin.source, "ref": origin.ref}
    if origin.char_span is not None:
        record["char_span"] = list(origin.char_span)
    return record


def _optional_int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


__all__ = [
    "CollectionAcquisition",
    "CollectionError",
    "CollectionLimits",
    "DeclineKind",
    "HIDDEN_ENTRY_POLICY",
    "INTERNAL_DIRECTORY_NAMES",
    "MANIFEST_SCHEMA",
    "MemberDecline",
    "acquire_archive",
    "acquire_collection",
    "acquire_directory",
    "crawl_documentation_site",
    "validate_extension_registry",
]
