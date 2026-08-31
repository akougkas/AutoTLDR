#!/usr/bin/env python3
"""Assemble a version-bound, checksummed private-alpha participant bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tarfile
import tempfile
import zipfile
from email.parser import Parser
from pathlib import Path
from typing import Any


SCHEMA = "autotldr-private-alpha-bundle-v1"
PACKAGE = "autotldr"
TEMPLATE_FIELDS = ("{{VERSION}}", "{{SUPPORT}}")
FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)


class BundleError(ValueError):
    """The release artifacts cannot produce a truthful participant bundle."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_support(value: str) -> str:
    if not value or value != value.strip() or len(value) > 240:
        raise BundleError("--support must be a non-empty contact of at most 240 characters")
    if any(ord(character) < 32 for character in value):
        raise BundleError("--support must not contain control characters")
    return value


def _metadata(payload: str, *, source: str) -> tuple[str, str]:
    parsed = Parser().parsestr(payload)
    name = parsed.get("Name")
    version = parsed.get("Version")
    if not name or not version:
        raise BundleError(f"{source} metadata must contain Name and Version")
    return name.casefold(), version


def _wheel_identity(path: Path) -> tuple[str, str]:
    try:
        with zipfile.ZipFile(path) as archive:
            names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
            if len(names) != 1:
                raise BundleError(f"{path.name} must contain exactly one dist-info METADATA")
            payload = archive.read(names[0]).decode("utf-8", errors="strict")
    except (OSError, UnicodeError, zipfile.BadZipFile, KeyError) as exc:
        raise BundleError(f"cannot inspect wheel {path}: {exc}") from exc
    return _metadata(payload, source=path.name)


def _sdist_identity(path: Path) -> tuple[str, str]:
    try:
        with tarfile.open(path, mode="r:gz") as archive:
            members = [item for item in archive.getmembers() if item.name.endswith("/PKG-INFO")]
            if len(members) != 1:
                raise BundleError(f"{path.name} must contain exactly one root PKG-INFO")
            handle = archive.extractfile(members[0])
            if handle is None:
                raise BundleError(f"cannot read PKG-INFO from {path.name}")
            payload = handle.read().decode("utf-8", errors="strict")
    except (OSError, UnicodeError, tarfile.TarError) as exc:
        raise BundleError(f"cannot inspect source distribution {path}: {exc}") from exc
    return _metadata(payload, source=path.name)


def _release_pair(dist: Path) -> tuple[Path, Path, str]:
    if not dist.is_dir():
        raise BundleError(f"distribution directory does not exist: {dist}")
    wheels = sorted(dist.glob("*.whl"))
    sdists = sorted(dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise BundleError("distribution directory must contain exactly one wheel and one sdist")
    wheel_identity = _wheel_identity(wheels[0])
    sdist_identity = _sdist_identity(sdists[0])
    if wheel_identity != sdist_identity:
        raise BundleError(
            f"wheel identity {wheel_identity} does not match sdist identity {sdist_identity}"
        )
    name, version = wheel_identity
    if name != PACKAGE:
        raise BundleError(f"expected package {PACKAGE!r}, found {name!r}")
    return wheels[0], sdists[0], version


def _render_guide(template: Path, *, version: str, support: str) -> str:
    text = template.read_text(encoding="utf-8")
    rendered = text.replace("{{VERSION}}", version).replace("{{SUPPORT}}", support)
    remaining = [field for field in TEMPLATE_FIELDS if field in rendered]
    if remaining:
        raise BundleError(f"participant guide has unresolved fields: {remaining}")
    return rendered


def _file_record(path: Path, *, root: Path) -> dict[str, Any]:
    return {
        "path": path.relative_to(root).as_posix(),
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _write_deterministic_zip(root: Path, destination: Path, *, version: str) -> None:
    archive_root = f"autotldr-{version}-private-alpha"
    with zipfile.ZipFile(destination, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            relative = path.relative_to(root).as_posix()
            info = zipfile.ZipInfo(f"{archive_root}/{relative}", FIXED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())


def build_bundle(
    dist: Path,
    output: Path,
    *,
    support: str,
    repository: Path,
) -> dict[str, Any]:
    support = _safe_support(support)
    dist = dist.resolve()
    output = output.resolve()
    archive_output = output.with_name(output.name + ".zip")
    if output.exists() or archive_output.exists():
        raise BundleError(f"refusing to overwrite {output} or {archive_output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    if output == dist or output.is_relative_to(dist):
        raise BundleError("output must be outside the distribution directory")

    wheel, sdist, version = _release_pair(dist)
    required_sources = {
        "guide": repository / "docs" / "private-alpha-guide.md",
        "changelog": repository / "docs" / "changelog.md",
        "security": repository / "docs" / "security.md",
        "license": repository / "LICENSE",
    }
    missing = [str(path) for path in required_sources.values() if not path.is_file()]
    if missing:
        raise BundleError("missing bundle source files: " + ", ".join(missing))

    temporary = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.", dir=str(output.parent))
    )
    temporary_archive = archive_output.with_name(
        f".{archive_output.name}.{os.getpid()}.tmp"
    )
    try:
        packages = temporary / "packages"
        packages.mkdir()
        shutil.copyfile(wheel, packages / wheel.name)
        shutil.copyfile(sdist, packages / sdist.name)
        (temporary / "README.md").write_text(
            _render_guide(required_sources["guide"], version=version, support=support),
            encoding="utf-8",
            newline="\n",
        )
        shutil.copyfile(required_sources["changelog"], temporary / "CHANGELOG.md")
        shutil.copyfile(required_sources["security"], temporary / "SECURITY.md")
        shutil.copyfile(required_sources["license"], temporary / "LICENSE")

        payload_files = sorted(
            item for item in temporary.rglob("*") if item.is_file()
        )
        manifest = {
            "schema": SCHEMA,
            "package": PACKAGE,
            "version": version,
            "support": support,
            "break_policy": "pre-alpha interfaces may change between builds",
            "files": [_file_record(path, root=temporary) for path in payload_files],
        }
        manifest_path = temporary / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        checksummed = sorted(
            item for item in temporary.rglob("*") if item.is_file()
        )
        (temporary / "SHA256SUMS").write_text(
            "".join(
                f"{_sha256(path)}  {path.relative_to(temporary).as_posix()}\n"
                for path in checksummed
            ),
            encoding="ascii",
            newline="\n",
        )
        temporary.rename(output)
        _write_deterministic_zip(output, temporary_archive, version=version)
        os.replace(temporary_archive, archive_output)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        if temporary_archive.exists():
            temporary_archive.unlink()
        if output.exists() and not archive_output.exists():
            shutil.rmtree(output)
        raise

    return {
        "schema": SCHEMA,
        "version": version,
        "directory": str(output),
        "archive": str(archive_output),
        "archive_bytes": archive_output.stat().st_size,
        "archive_sha256": _sha256(archive_output),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a checksummed AutoTLDR private-alpha participant bundle."
    )
    parser.add_argument("dist", type=Path, metavar="DIST_DIRECTORY")
    parser.add_argument("output", type=Path, metavar="NEW_OUTPUT_DIRECTORY")
    parser.add_argument(
        "--support",
        required=True,
        metavar="CONTACT",
        help="support contact rendered into the participant guide and manifest",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repository = Path(__file__).resolve().parents[1]
    try:
        report = build_bundle(
            args.dist,
            args.output,
            support=args.support,
            repository=repository,
        )
    except (BundleError, OSError, UnicodeError) as exc:
        print(f"autotldr alpha bundle: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
