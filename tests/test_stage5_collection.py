"""Tier 2 acquisition is bounded, deterministic, and fail-soft per member."""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import stat
import tarfile
import threading
import warnings
import zipfile
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from autotldr.collection import (
    CollectionError,
    CollectionLimits,
    DeclineKind,
    acquire_archive,
    acquire_collection,
    acquire_directory,
    crawl_documentation_site,
)


def _assert_exact_manifest(acquisition) -> None:
    manifest = acquisition.manifest
    expected = manifest["sha256"]
    unsigned = {key: value for key, value in manifest.items() if key != "sha256"}
    canonical = json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert hashlib.sha256(canonical).hexdigest() == expected
    assert [item["order"] for item in manifest["members"]] == list(
        range(len(manifest["members"]))
    )
    json.dumps(manifest, ensure_ascii=False)


def _write_project(root: Path) -> None:
    root.mkdir(parents=True)
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (root / ".autotldr").mkdir()
    (root / ".autotldr" / "stale.md").write_text("secret", encoding="utf-8")
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "cache.pyc").write_bytes(b"cache")
    (root / "README.md").write_text(
        "# Trial\n\nThe measurements are in [results.csv](results.csv).\n",
        encoding="utf-8",
    )
    (root / "results.csv").write_text(
        "sample_id,throughput_mbps\na,12.5\nb,13.0\n",
        encoding="utf-8",
    )
    (root / "opaque.bin").write_bytes(b"\x00\x01\x02")
    try:
        (root / "linked.md").symlink_to("README.md")
    except OSError:  # pragma: no cover - platforms without symlink permission
        pass


def test_git_worktree_fans_out_stably_and_keeps_valid_siblings(tmp_path: Path) -> None:
    first = tmp_path / "one" / "project"
    second = tmp_path / "two" / "project"
    _write_project(first)
    _write_project(second)

    left = acquire_collection(first)
    right = acquire_directory(second)

    assert left.kind == right.kind == "git-worktree"
    assert left.source == right.source == "project"
    assert [item.source for item in left.extractions] == [
        "project/README.md",
        "project/results.csv",
    ]
    assert [item.source for item in left.extractions] == [
        item.source for item in right.extractions
    ]
    assert [unit.id for item in left.extractions for unit in item.units] == [
        unit.id for item in right.extractions for unit in item.units
    ]
    assert left.manifest == right.manifest

    by_source = {item.source: item for item in left.declines}
    assert by_source["project/opaque.bin"].kind is DeclineKind.UNSUPPORTED
    if (first / "linked.md").is_symlink():
        assert by_source["project/linked.md"].kind is DeclineKind.UNSAFE
    assert all(item.as_gap().origin == item.origin for item in left.declines)
    assert {item["reason"] for item in left.manifest["members"] if item["status"] == "ignored"} >= {
        "hidden-directory",
        "internal-directory",
    }

    for extraction in left.extractions:
        assert all(unit.source == extraction.source for unit in extraction.units)
        assert all(unit.origin.source == extraction.source for unit in extraction.units)
        assert all(
            endpoint in {unit.id for unit in extraction.units}
            for relation in extraction.relations
            for endpoint in (relation.src, relation.dst)
        )
        assert extraction.meta["inputs"][0]["source"] == extraction.source
    serialized = json.dumps(
        {
            "manifest": left.manifest,
            "member_meta": [item.meta for item in left.extractions],
        },
        ensure_ascii=False,
        default=str,
    )
    assert str(first) not in serialized
    _assert_exact_manifest(left)


def test_directory_limits_are_typed_and_do_not_erase_small_sibling(tmp_path: Path) -> None:
    root = tmp_path / "bounded"
    root.mkdir()
    (root / "a.md").write_text("# A\n", encoding="utf-8")
    (root / "large.txt").write_text("x" * 40, encoding="utf-8")
    (root / "nested").mkdir()
    (root / "nested" / "inside.md").write_text("# hidden by depth\n", encoding="utf-8")

    result = acquire_directory(
        root,
        limits=CollectionLimits(
            max_member_bytes=16,
            max_total_bytes=64,
            max_directory_depth=0,
        ),
    )

    assert [item.source for item in result.extractions] == ["bounded/a.md"]
    limits = {item.source: item for item in result.declines}
    assert limits["bounded/large.txt"].kind is DeclineKind.LIMIT
    assert limits["bounded/nested"].kind is DeclineKind.LIMIT
    assert result.manifest["counts"]["extracted"] == 1
    assert result.manifest["counts"]["declined"] == 2


def _nested_zip() -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("inside.md", "# Inside\n\nNested evidence.\n")
    return payload.getvalue()


def _write_adversarial_zip(path: Path) -> None:
    link = zipfile.ZipInfo("link.md")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("safe.md", "# Safe\n\nAddressable.\n")
            archive.writestr("inner.zip", _nested_zip())
            archive.writestr("../escape.md", "must never escape")
            archive.writestr("dupe.md", "first")
            archive.writestr("dupe.md", "second")
            archive.writestr(link, "safe.md")
            archive.writestr(
                "bomb.txt",
                b"0" * 200_000,
                compress_type=zipfile.ZIP_DEFLATED,
            )


def test_zip_rejects_escape_duplicate_link_and_bomb_but_routes_siblings(
    tmp_path: Path,
) -> None:
    first = tmp_path / "one" / "bundle.zip"
    second = tmp_path / "two" / "bundle.zip"
    first.parent.mkdir()
    second.parent.mkdir()
    _write_adversarial_zip(first)
    shutil.copyfile(first, second)

    left = acquire_archive(first)
    right = acquire_archive(second)

    assert [item.source for item in left.extractions] == [
        "bundle.zip!/inner.zip!/inside.md",
        "bundle.zip!/safe.md",
    ]
    assert [unit.id for item in left.extractions for unit in item.units] == [
        unit.id for item in right.extractions for unit in item.units
    ]
    assert left.manifest == right.manifest
    kinds = {(item.source, item.kind) for item in left.declines}
    assert ("bundle.zip!/../escape.md", DeclineKind.UNSAFE) in kinds
    assert ("bundle.zip!/dupe.md", DeclineKind.DUPLICATE) in kinds
    assert ("bundle.zip!/link.md", DeclineKind.UNSAFE) in kinds
    assert ("bundle.zip!/bomb.txt", DeclineKind.LIMIT) in kinds
    assert not (tmp_path / "escape.md").exists()
    _assert_exact_manifest(left)


def test_nested_archive_depth_is_a_decline_with_outer_sibling_survival(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "depth.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("safe.md", "# Safe\n")
        archive.writestr("inner.zip", _nested_zip())

    result = acquire_archive(
        archive_path,
        limits=CollectionLimits(max_archive_depth=0),
    )

    assert [item.source for item in result.extractions] == ["depth.zip!/safe.md"]
    nested = next(item for item in result.declines if item.source.endswith("inner.zip"))
    assert nested.kind is DeclineKind.LIMIT
    assert nested.details["limit"] == "max_archive_depth"


def _set_zip_encryption_flags(payload: bytes) -> bytes:
    """Mark an otherwise valid one-member ZIP encrypted in both headers."""

    changed = bytearray(payload)
    cursor = 0
    while True:
        offset = changed.find(b"PK\x03\x04", cursor)
        if offset < 0:
            break
        flags = int.from_bytes(changed[offset + 6 : offset + 8], "little") | 1
        changed[offset + 6 : offset + 8] = flags.to_bytes(2, "little")
        cursor = offset + 4
    cursor = 0
    while True:
        offset = changed.find(b"PK\x01\x02", cursor)
        if offset < 0:
            break
        flags = int.from_bytes(changed[offset + 8 : offset + 10], "little") | 1
        changed[offset + 8 : offset + 10] = flags.to_bytes(2, "little")
        cursor = offset + 4
    return bytes(changed)


def test_encrypted_zip_member_is_refused_before_read(tmp_path: Path) -> None:
    plain = io.BytesIO()
    with zipfile.ZipFile(plain, "w") as archive:
        archive.writestr("secret.md", "# Secret\n")
    path = tmp_path / "encrypted.zip"
    path.write_bytes(_set_zip_encryption_flags(plain.getvalue()))

    result = acquire_archive(path)

    assert result.extractions == ()
    assert len(result.declines) == 1
    assert result.declines[0].kind is DeclineKind.ENCRYPTED
    assert result.declines[0].source == "encrypted.zip!/secret.md"


def _add_tar_bytes(archive: tarfile.TarFile, name: str, payload: bytes) -> None:
    member = tarfile.TarInfo(name)
    member.size = len(payload)
    archive.addfile(member, io.BytesIO(payload))


def test_tar_rejects_traversal_links_and_duplicates_without_losing_safe_file(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bundle.tar"
    with tarfile.open(path, "w") as archive:
        _add_tar_bytes(archive, "safe.md", b"# Safe\n")
        _add_tar_bytes(archive, "../escape.md", b"escape")
        _add_tar_bytes(archive, "dupe.md", b"one")
        _add_tar_bytes(archive, "dupe.md", b"two")
        link = tarfile.TarInfo("link.md")
        link.type = tarfile.SYMTYPE
        link.linkname = "safe.md"
        archive.addfile(link)

    result = acquire_archive(path)

    assert [item.source for item in result.extractions] == ["bundle.tar!/safe.md"]
    kinds = {(item.source, item.kind) for item in result.declines}
    assert ("bundle.tar!/../escape.md", DeclineKind.UNSAFE) in kinds
    assert ("bundle.tar!/dupe.md", DeclineKind.DUPLICATE) in kinds
    assert ("bundle.tar!/link.md", DeclineKind.UNSAFE) in kinds
    assert not (tmp_path / "escape.md").exists()


class _RoutesHandler(BaseHTTPRequestHandler):
    routes: dict[str, tuple[int, dict[str, str], bytes]] = {}

    def do_GET(self) -> None:  # noqa: N802 - stdlib callback spelling
        path = urlsplit(self.path).path
        status, headers, payload = self.routes.get(
            path,
            (404, {"Content-Type": "text/plain"}, b"missing"),
        )
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format: str, *_args) -> None:
        return


@contextmanager
def _server(routes: dict[str, tuple[int, dict[str, str], bytes]]):
    handler = type("Handler", (_RoutesHandler,), {"routes": routes})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _html(body: str) -> tuple[int, dict[str, str], bytes]:
    return 200, {"Content-Type": "text/html; charset=utf-8"}, body.encode("utf-8")


def test_doc_site_is_same_origin_bounded_loop_safe_and_fail_soft() -> None:
    outside_routes = {"/outside.html": _html("<html><body><p>outside</p></body></html>")}
    with _server(outside_routes) as outside:
        routes = {
            "/docs/index.html": _html(
                "<html><body><main><h1>Docs</h1>"
                '<p><a href="/docs/a.html">A</a> '
                '<a href="#top">loop</a> '
                '<a href="/docs/big.html">big</a> '
                '<a href="/redirect">redirect</a> '
                '<a href="https://example.invalid/out">external</a></p>'
                "</main></body></html>"
            ),
            "/docs/a.html": _html(
                "<html><body><main><p>A page "
                '<a href="/docs/index.html">back</a> '
                '<a href="/docs/deep.html">deep</a></p></main></body></html>'
            ),
            "/docs/deep.html": _html("<html><body><p>deep</p></body></html>"),
            "/docs/big.html": _html("<html><body><p>" + "x" * 2_000 + "</p></body></html>"),
            "/redirect": (
                302,
                {"Location": f"{outside}/outside.html", "Content-Type": "text/plain"},
                b"",
            ),
        }
        with _server(routes) as base:
            limits = CollectionLimits(
                max_crawl_depth=1,
                max_crawl_pages=10,
                max_crawl_page_bytes=1_024,
                max_crawl_total_bytes=8_192,
            )
            first = crawl_documentation_site(
                f"{base}/docs/index.html#ignored",
                limits=limits,
            )
            second = acquire_collection(f"{base}/docs/index.html", limits=limits)

    assert [item.source for item in first.extractions] == [
        f"{base}/docs/a.html",
        f"{base}/docs/index.html",
    ]
    assert first.manifest == second.manifest
    assert any(item.kind is DeclineKind.CROSS_ORIGIN for item in first.declines)
    assert any(
        item.kind is DeclineKind.CROSS_ORIGIN
        and item.details.get("requested_url") == f"{base}/redirect"
        for item in first.declines
    )
    assert any(
        item.kind is DeclineKind.LIMIT and item.source == f"{base}/docs/big.html"
        for item in first.declines
    )
    assert any(
        item.kind is DeclineKind.LIMIT and item.source == f"{base}/docs/deep.html"
        for item in first.declines
    )
    assert any(
        item["reason"] == "crawl-loop-or-duplicate"
        for item in first.manifest["members"]
        if item["status"] == "ignored"
    )
    assert all(item.source.startswith(base) for item in first.extractions)
    _assert_exact_manifest(first)


def test_doc_site_page_limit_keeps_root_and_names_unvisited_pages() -> None:
    routes = {
        "/index.html": _html(
            "<html><body><p>"
            '<a href="/a.html">a</a><a href="/b.html">b</a>'
            "</p></body></html>"
        ),
        "/a.html": _html("<html><body><p>a</p></body></html>"),
        "/b.html": _html("<html><body><p>b</p></body></html>"),
    }
    with _server(routes) as base:
        result = crawl_documentation_site(
            f"{base}/index.html",
            limits=CollectionLimits(max_crawl_pages=1),
        )

    assert [item.source for item in result.extractions] == [f"{base}/index.html"]
    limited = {item.source for item in result.declines if item.kind is DeclineKind.LIMIT}
    assert limited == {f"{base}/a.html", f"{base}/b.html"}


def test_collection_root_rejects_symlink_and_non_archive_file(tmp_path: Path) -> None:
    directory = tmp_path / "directory"
    directory.mkdir()
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(directory, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(CollectionError, match="must not be a symlink"):
        acquire_collection(linked)

    ordinary = tmp_path / "notes.md"
    ordinary.write_text("# Notes\n", encoding="utf-8")
    with pytest.raises(CollectionError, match="not a supported ZIP or TAR"):
        acquire_collection(ordinary)
