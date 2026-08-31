"""Adversarial regressions for byte-exact local-path acquisition."""

from __future__ import annotations

import errno
import hashlib
import io
import os
import sqlite3
import stat
import sys
import tempfile
import types
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

from autotldr import cli as cli_module
from autotldr import router
from autotldr.extensions import ExtensionRegistry, ExtractorSpec
from autotldr.unit import (
    Extraction,
    Gap,
    GroundedStatement,
    Modality,
    Origin,
    Relation,
    RelationKind,
    Unit,
)


def _restore_times(path: Path, times: os.stat_result) -> None:
    os.utime(path, ns=(times.st_atime_ns, times.st_mtime_ns))


def _assert_closed(result: Extraction, source: Path) -> None:
    assert result.source == str(source)
    units = {unit.id: unit for unit in result.units}
    assert units
    assert all(
        unit.source == str(source) and unit.origin.source == str(source)
        for unit in units.values()
    )
    assert all(
        relation.src in units and relation.dst in units
        for relation in result.relations
    )
    assert all(gap.origin.source == str(source) for gap in result.gaps)
    for statement in result.summary_claims:
        evidence = [units[unit_id] for unit_id in statement.evidence_unit_ids]
        assert set(statement.origins) == {unit.origin for unit in evidence}


def test_same_size_restored_mtime_after_capture_cannot_split_hash_and_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "fact.md"
    captured = b"# Relay\n\nThe mode is old.\n"
    replacement = b"# Relay\n\nThe mode is new.\n"
    assert len(captured) == len(replacement)
    source.write_bytes(captured)
    original_stat = source.stat()
    real_dispatch = router._dispatch
    snapshots: list[Path] = []

    def mutate_then_dispatch(path, handler, *, registry=None):
        snapshot = Path(path)
        snapshots.append(snapshot)
        assert snapshot != source
        assert snapshot.name == source.name
        assert stat.S_IMODE(snapshot.stat().st_mode) == 0o400
        assert stat.S_IMODE(snapshot.parent.stat().st_mode) == 0o700
        source.write_bytes(replacement)
        _restore_times(source, original_stat)
        return real_dispatch(snapshot, handler, registry=registry)

    monkeypatch.setattr(router, "_dispatch", mutate_then_dispatch)
    result = router.extract(source)

    assert any("mode is old" in unit.content for unit in result.units)
    assert not any("mode is new" in unit.content for unit in result.units)
    assert source.read_bytes() == replacement
    assert source.stat().st_size == original_stat.st_size
    assert source.stat().st_mtime_ns == original_stat.st_mtime_ns
    manifest = result.meta["inputs"][0]
    assert manifest["bytes"] == len(captured)
    assert manifest["sha256"] == hashlib.sha256(captured).hexdigest()
    _assert_closed(result, source)
    assert "autotldr-snapshot-" not in repr(result)
    assert snapshots and all(not path.exists() for path in snapshots)
    assert all(not path.parent.exists() for path in snapshots)


def test_extension_aba_mutation_rebases_ids_claims_relations_and_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "facts.snapx"
    captured = b"alpha|omega"
    transient = b"bravo|sigma"
    assert len(captured) == len(transient)
    source.write_bytes(captured)
    original_stat = source.stat()
    seen: list[Path] = []

    module_name = "autotldr_test_local_snapshot_aba"
    extension_module = types.ModuleType(module_name)

    def extract_snapshot(acquired):
        physical = Path(acquired)
        seen.append(physical)
        source.write_bytes(transient)
        _restore_times(source, original_stat)
        text = physical.read_text(encoding="utf-8")
        source.write_bytes(captured)
        _restore_times(source, original_stat)

        left_text, right_text = text.split("|")
        left = Unit(
            source=str(physical),
            modality=Modality.RECORD,
            content=left_text,
            origin=Origin(str(physical), "record:1"),
        )
        right = Unit(
            source=str(physical),
            modality=Modality.RECORD,
            content=right_text,
            origin=Origin(str(physical), "record:2"),
            meta={"anchor": left.id, "snapshot": str(physical)},
        )
        relation = Relation(
            left.id,
            right.id,
            RelationKind.DESCRIBES,
            evidence=f"records were read from {physical}",
        )
        statement = GroundedStatement(
            f"The snapshot {physical} contains two records.",
            (left.origin, right.origin),
            (left.id, right.id),
        )
        return Extraction(
            source=str(physical),
            kind="snapshot-extension",
            units=[left, right],
            relations=[relation],
            gaps=[
                Gap(
                    f"No third record exists in {physical}",
                    Origin(str(physical), "source"),
                )
            ],
            meta={
                "anchor": left.id,
                "snapshot": str(physical),
                "snapshot_root": str(physical.parent),
            },
            summary_claims=[statement],
        )

    extension_module.extract_snapshot = extract_snapshot
    monkeypatch.setitem(sys.modules, module_name, extension_module)
    registry = ExtensionRegistry(
        (
            ExtractorSpec(
                name="snapshot-extension",
                module=module_name,
                callable="extract_snapshot",
                kinds=("snapshot-extension",),
                suffixes=(".snapx",),
            ),
        )
    )

    result = router.extract(source, registry=registry)

    assert [unit.content for unit in result.units] == ["alpha", "omega"]
    assert source.read_bytes() == captured
    assert result.meta["inputs"][0]["sha256"] == hashlib.sha256(captured).hexdigest()
    assert result.meta["anchor"] == result.units[0].id
    assert result.units[1].meta["anchor"] == result.units[0].id
    assert result.meta["snapshot"] == str(source)
    assert result.meta["snapshot_root"] == str(source.parent)
    assert str(source) in result.relations[0].evidence
    assert str(source) in result.summary_claims[0].content
    _assert_closed(result, source)
    assert "autotldr-snapshot-" not in repr(result)
    assert seen and all(not path.exists() for path in seen)


def _docx_bytes() -> bytes:
    namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    document = (
        f'<w:document xmlns:w="{namespace}"><w:body><w:p>'
        "<w:r><w:t>Stable ZIP fact.</w:t></w:r>"
        "</w:p></w:body></w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()


def test_zip_detector_and_extractor_reopen_only_the_private_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "document.docx"
    source.write_bytes(_docx_bytes())
    real_zip_file = zipfile.ZipFile
    opened: list[Path] = []

    def recording_zip_file(file, *args, **kwargs):
        if isinstance(file, (str, os.PathLike)):
            opened.append(Path(file))
        return real_zip_file(file, *args, **kwargs)

    monkeypatch.setattr(zipfile, "ZipFile", recording_zip_file)
    result = router.extract(source)

    assert any(unit.content == "Stable ZIP fact." for unit in result.units)
    assert len(opened) >= 2
    assert all(path != source for path in opened)
    assert len({str(path) for path in opened}) == 1
    assert all(not path.exists() for path in opened)
    _assert_closed(result, source)


def _sqlite_file(path: Path) -> bytes:
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE facts (id INTEGER PRIMARY KEY, value TEXT)")
    connection.execute("INSERT INTO facts VALUES (1, 'row payload')")
    connection.commit()
    connection.close()
    return path.read_bytes()


def test_tier3_path_only_connection_reopens_snapshot_and_manifest_is_exact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "facts.sqlite"
    payload = _sqlite_file(source)
    real_connect = sqlite3.connect
    opened: list[str] = []

    def recording_connect(database, *args, **kwargs):
        opened.append(str(database))
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", recording_connect)
    result = router.extract(source)

    database_uri = next(value for value in opened if value.startswith("file:"))
    physical = Path(unquote(urlsplit(database_uri).path))
    assert physical != source
    assert "autotldr-snapshot-" in str(physical)
    assert not physical.exists()
    assert not physical.parent.exists()
    assert result.meta["inputs"][0]["bytes"] == len(payload)
    assert result.meta["inputs"][0]["sha256"] == hashlib.sha256(payload).hexdigest()
    _assert_closed(result, source)
    assert "autotldr-snapshot-" not in repr(result)


def test_explicit_suffix_coercion_never_whole_file_reads_the_original(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "mislabeled.bin"
    # This is also the canonical alias basename. Rebasing provenance must not
    # rewrite an exact semantic source slice that happens to contain it.
    payload = b"mislabeled.md\n"
    source.write_bytes(payload)
    real_read_bytes = Path.read_bytes
    reads: list[Path] = []

    def guarded_read_bytes(path):
        candidate = Path(path)
        reads.append(candidate)
        if candidate == source:
            raise AssertionError("router reread the whole logical source")
        return real_read_bytes(candidate)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)
    result = router.extract(source, kind="markdown")

    assert result.kind == "markdown"
    assert any(unit.content == "mislabeled.md" for unit in result.units)
    assert reads and all(path != source for path in reads)
    assert any(path.suffix == ".md" for path in reads)
    assert result.meta["inputs"][0]["sha256"] == hashlib.sha256(payload).hexdigest()
    _assert_closed(result, source)


def test_alias_is_read_only_and_basename_rebasing_is_provenance_shaped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "mislabeled.bin"
    source.write_text("snapshot payload\n", encoding="utf-8")
    seen: list[Path] = []
    detected_inode: list[tuple[int, int]] = []
    real_detect = router.detect

    def read_only_detect(path, kind=None, *, registry=None):
        physical = Path(path)
        identity = physical.stat()
        assert stat.S_IMODE(identity.st_mode) == 0o400
        detected_inode.append((identity.st_dev, identity.st_ino))
        return real_detect(path, kind=kind, registry=registry)

    def semantic_dispatch(path, handler, *, registry=None):
        physical = Path(path)
        seen.append(physical)
        alias_name = physical.name
        assert alias_name == "mislabeled.md"
        identity = physical.stat()
        assert stat.S_IMODE(identity.st_mode) == 0o400
        assert detected_inode == [(identity.st_dev, identity.st_ino)]

        left = Unit(
            source=str(physical),
            modality=Modality.PROSE,
            content=alias_name,
            origin=Origin(str(physical), alias_name),
            structure=(alias_name,),
            meta={
                "semantic_value": alias_name,
                "semantic_sentence": f"the word {alias_name} is meaningful",
                "semantic_full_path": str(physical),
                "filename": alias_name,
                "physical_path": str(physical),
            },
        )
        right = Unit(
            source=str(physical),
            modality=Modality.PROSE,
            content="second unit",
            origin=Origin(str(physical), "line:2"),
        )
        return Extraction(
            source=str(physical),
            kind=handler.kind,
            units=[left, right],
            relations=[
                Relation(
                    left.id,
                    right.id,
                    RelationKind.DESCRIBES,
                    evidence=f"relation names {alias_name}",
                )
            ],
            gaps=[
                Gap(
                    f"gap names {alias_name}",
                    Origin(str(physical), alias_name),
                )
            ],
            meta={
                "semantic_value": alias_name,
                "semantic_sentence": f"the word {alias_name} is meaningful",
                "filename": alias_name,
                "physical_path": str(physical),
                "snapshot_root": str(physical.parent),
            },
            summary_claims=[
                GroundedStatement(
                    f"statement names {alias_name}",
                    (left.origin, right.origin),
                    (left.id, right.id),
                )
            ],
        )

    monkeypatch.setattr(router, "detect", read_only_detect)
    monkeypatch.setattr(router, "_dispatch", semantic_dispatch)
    result = router.extract(source, kind="markdown")

    alias_name = "mislabeled.md"
    left = result.units[0]
    assert left.content == alias_name
    assert left.origin.ref == source.name
    assert left.structure == (source.name,)
    assert left.meta["semantic_value"] == alias_name
    assert left.meta["semantic_sentence"] == f"the word {alias_name} is meaningful"
    assert left.meta["semantic_full_path"] == str(source)
    assert left.meta["filename"] == source.name
    assert left.meta["physical_path"] == str(source)
    assert result.relations[0].evidence == f"relation names {alias_name}"
    assert result.gaps[0].content == f"gap names {alias_name}"
    assert result.gaps[0].origin.ref == source.name
    assert result.summary_claims[0].content == f"statement names {alias_name}"
    assert result.meta["semantic_value"] == alias_name
    assert result.meta["semantic_sentence"] == f"the word {alias_name} is meaningful"
    assert result.meta["filename"] == source.name
    assert result.meta["physical_path"] == str(source)
    assert result.meta["snapshot_root"] == str(source.parent)
    _assert_closed(result, source)
    assert "autotldr-snapshot-" not in repr(result)
    assert seen and all(not path.exists() for path in seen)
    assert all(not path.parent.exists() for path in seen)


def test_rebasing_refuses_metadata_dictionary_key_collisions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "collision.md"
    source.write_text("collision\n", encoding="utf-8")
    seen: list[Path] = []

    def colliding_dispatch(path, handler, *, registry=None):
        physical = Path(path)
        seen.append(physical)
        unit = Unit(
            source=str(physical),
            modality=Modality.PROSE,
            content="collision",
            origin=Origin(str(physical), "line:1"),
            meta={str(physical): "private", str(source): "logical"},
        )
        return Extraction(str(physical), handler.kind, units=[unit])

    monkeypatch.setattr(router, "_dispatch", colliding_dispatch)
    with pytest.raises(ValueError, match="dictionary-key collision") as raised:
        router.extract(source)

    assert "autotldr-snapshot-" not in str(raised.value)
    assert seen and all(not path.exists() for path in seen)
    assert all(not path.parent.exists() for path in seen)


def test_dispatch_error_is_scrubbed_and_snapshot_is_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "broken.md"
    source.write_text("# Broken\n", encoding="utf-8")
    seen: list[Path] = []

    def fail(path, _handler, *, registry=None):
        physical = Path(path)
        seen.append(physical)
        raise ValueError(f"{physical}: failed inside {physical.parent}")

    monkeypatch.setattr(router, "_dispatch", fail)
    with pytest.raises(ValueError) as raised:
        router.extract(source)

    message = str(raised.value)
    assert str(source) in message
    assert "autotldr-snapshot-" not in message
    assert seen and all(not path.exists() for path in seen)
    assert all(not path.parent.exists() for path in seen)


def test_read_only_symlink_retarget_after_capture_cannot_change_extraction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first_payload = b"# First\n\nCaptured target.\n"
    second_payload = b"# Second\n\nRetargeted source.\n"
    first.write_bytes(first_payload)
    second.write_bytes(second_payload)
    first.chmod(0o400)
    logical = tmp_path / "current.md"
    logical.symlink_to(first.name)
    real_dispatch = router._dispatch

    def retarget_then_dispatch(path, handler, *, registry=None):
        logical.unlink()
        logical.symlink_to(second.name)
        return real_dispatch(path, handler, registry=registry)

    monkeypatch.setattr(router, "_dispatch", retarget_then_dispatch)
    result = router.extract(logical)

    assert any("Captured target" in unit.content for unit in result.units)
    assert not any("Retargeted source" in unit.content for unit in result.units)
    assert logical.resolve() == second.resolve()
    assert result.meta["inputs"][0]["sha256"] == hashlib.sha256(first_payload).hexdigest()
    _assert_closed(result, logical)


def test_snapshot_copy_uses_bounded_chunks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "bounded.txt"
    payload = ("bounded semantic line\n" * 20).encode()
    source.write_bytes(payload)
    writes: list[int] = []
    real_write_all = router._write_all

    def recording_write(fd, chunk):
        writes.append(len(chunk))
        return real_write_all(fd, chunk)

    monkeypatch.setattr(router, "_SNAPSHOT_CHUNK_BYTES", 7)
    monkeypatch.setattr(router, "_write_all", recording_write)
    result = router.extract(source)

    assert len(writes) > 1
    assert max(writes) <= 7
    assert sum(writes) == len(payload)
    assert result.meta["inputs"][0]["bytes"] == len(payload)
    assert result.meta["inputs"][0]["sha256"] == hashlib.sha256(payload).hexdigest()


def test_original_path_mutation_during_copy_is_diagnosed_even_if_mtime_is_restored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "moving.txt"
    captured = b"abcdefgh-abcdefgh-abcdefgh\n"
    replacement = b"ABCDEFGH-ABCDEFGH-ABCDEFGH\n"
    assert len(captured) == len(replacement)
    source.write_bytes(captured)
    original_stat = source.stat()
    real_write_all = router._write_all
    writes = 0

    def mutate_during_first_write(fd, chunk):
        nonlocal writes
        writes += 1
        if writes == 1:
            source.write_bytes(replacement)
            _restore_times(source, original_stat)
        return real_write_all(fd, chunk)

    monkeypatch.setattr(router, "_SNAPSHOT_CHUNK_BYTES", 8)
    monkeypatch.setattr(router, "_write_all", mutate_during_first_write)

    with pytest.raises(ValueError) as raised:
        router.extract(source)

    assert writes > 1
    assert "source changed" in str(raised.value)
    assert source.name in str(raised.value)
    assert "autotldr-snapshot-" not in str(raised.value)


def test_detection_failure_scrubs_and_cleans_the_private_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "detect.txt"
    source.write_text("detect me\n", encoding="utf-8")
    seen: list[Path] = []

    def fail_detect(path, kind=None, *, registry=None):
        physical = Path(path)
        seen.append(physical)
        raise ValueError(f"cannot detect {physical} beneath {physical.parent}")

    monkeypatch.setattr(router, "detect", fail_detect)
    with pytest.raises(ValueError) as raised:
        router.extract(source)

    assert str(source) in str(raised.value)
    assert "autotldr-snapshot-" not in str(raised.value)
    assert seen and all(not path.exists() for path in seen)
    assert all(not path.parent.exists() for path in seen)


def test_database_sidecars_are_rejected_before_and_after_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "sidecars.sqlite"
    _sqlite_file(source)
    sidecar = source.with_name(source.name + "-wal")
    sidecar.write_bytes(b"uncheckpointed")

    with pytest.raises(ValueError, match="checkpoint"):
        router.extract(source)

    sidecar.unlink()
    real_dispatch = router._dispatch

    def create_sidecar_after_dispatch(path, handler, *, registry=None):
        result = real_dispatch(path, handler, registry=registry)
        sidecar.write_bytes(b"appeared during extraction")
        return result

    monkeypatch.setattr(router, "_dispatch", create_sidecar_after_dispatch)
    with pytest.raises(ValueError) as raised:
        router.extract(source)

    assert "checkpoint" in str(raised.value)
    assert source.name in str(raised.value)
    assert "autotldr-snapshot-" not in str(raised.value)


def test_database_sidecar_removed_during_copy_is_still_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "vanishing.sqlite"
    _sqlite_file(source)
    sidecar = source.with_name(source.name + "-wal")
    sidecar.write_bytes(b"uncheckpointed before acquisition")
    real_write_all = router._write_all
    writes = 0
    detected: list[Path] = []
    real_detect = router.detect

    def remove_sidecar_on_first_copy(fd, chunk):
        nonlocal writes
        writes += 1
        if writes == 1:
            sidecar.unlink()
        return real_write_all(fd, chunk)

    def recording_detect(path, kind=None, *, registry=None):
        detected.append(Path(path))
        return real_detect(path, kind=kind, registry=registry)

    monkeypatch.setattr(router, "_write_all", remove_sidecar_on_first_copy)
    monkeypatch.setattr(router, "detect", recording_detect)
    with pytest.raises(ValueError, match="checkpoint") as raised:
        router.extract(source)

    assert writes >= 1
    assert not sidecar.exists()
    assert source.name in str(raised.value)
    assert "autotldr-snapshot-" not in str(raised.value)
    assert detected and all(not path.exists() for path in detected)
    assert all(not path.parent.exists() for path in detected)


def test_non_database_coincidental_sidecars_do_not_reject_text(
    tmp_path: Path,
) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("ordinary text\n", encoding="utf-8")
    sidecars = [
        source.with_name(source.name + "-wal"),
        source.with_name(source.name + "-journal"),
        source.with_name(source.name + ".wal"),
    ]
    for sidecar in sidecars:
        sidecar.write_bytes(b"coincidental non-database file")

    result = router.extract(source)

    assert any("ordinary text" in unit.content for unit in result.units)
    assert all(sidecar.exists() for sidecar in sidecars)
    _assert_closed(result, source)


def test_missing_source_preserves_oserror_contract_cli_exit_and_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "absent.md"
    real_temporary_directory = tempfile.TemporaryDirectory
    created: list[Path] = []

    def recording_temporary_directory(*args, **kwargs):
        temporary = real_temporary_directory(*args, **kwargs)
        created.append(Path(temporary.name))
        return temporary

    monkeypatch.setattr(tempfile, "TemporaryDirectory", recording_temporary_directory)
    with pytest.raises(FileNotFoundError) as raised:
        router.extract(source)

    error = raised.value
    assert error.errno == errno.ENOENT
    assert error.filename == str(source)
    assert str(source) in str(error)
    assert "autotldr-snapshot-" not in str(error)
    assert created and all(not path.exists() for path in created)

    status = cli_module.main([str(source)])
    captured = capsys.readouterr()
    assert status == cli_module.EXIT_NOT_FOUND
    assert str(source) in captured.err


def test_permission_source_preserves_subclass_errno_filename_and_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "denied.md"
    source.write_text("denied\n", encoding="utf-8")
    real_open = os.open
    real_temporary_directory = tempfile.TemporaryDirectory
    created: list[Path] = []

    def recording_temporary_directory(*args, **kwargs):
        temporary = real_temporary_directory(*args, **kwargs)
        created.append(Path(temporary.name))
        return temporary

    def deny_source(path, flags, *args, **kwargs):
        if Path(path) == source:
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(source))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(tempfile, "TemporaryDirectory", recording_temporary_directory)
    monkeypatch.setattr(os, "open", deny_source)
    with pytest.raises(PermissionError) as raised:
        router.extract(source)

    error = raised.value
    assert error.errno == errno.EACCES
    assert error.filename == str(source)
    assert str(source) in str(error)
    assert "autotldr-snapshot-" not in str(error)
    assert created and all(not path.exists() for path in created)
