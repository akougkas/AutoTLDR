"""Native, bounded EPUB spine extraction with item-level provenance."""

from __future__ import annotations

import posixpath
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET

from ..unit import Extraction, Origin, Role, Unit

_MAX_MEMBERS = 10_000
_MAX_TOTAL_UNCOMPRESSED = 256 * 1024 * 1024
_MAX_XML_BYTES = 8 * 1024 * 1024
_MAX_CHAPTER_BYTES = 16 * 1024 * 1024
_MAX_SPINE_ITEMS = 4096
_MAX_COMPRESSION_RATIO = 200


class _InvalidEpub(ValueError):
    pass


def extract(path: Path) -> Extraction:
    source = str(path)
    result = Extraction(source=source, kind="epub")
    title = ""
    spine: list[str] = []
    manifest: dict[str, str] = {}
    try:
        with zipfile.ZipFile(path) as archive:
            _validate_archive(archive)
            rootfile = _rootfile(archive)
            package = _parse_xml(
                _read_bounded(archive, rootfile, _MAX_XML_BYTES),
                rootfile,
            )
            base = posixpath.dirname(rootfile)
            manifest, media = _manifest(package)
            spine = _spine(package)
            if len(spine) > _MAX_SPINE_ITEMS:
                raise _InvalidEpub(
                    f"spine has {len(spine)} items; limit is {_MAX_SPINE_ITEMS}"
                )
            title = _metadata_title(package)

            for position, item_id in enumerate(spine, start=1):
                href = manifest.get(item_id)
                spine_ref = f"spine:{position}"
                if not href:
                    result.add_gap(
                        f"EPUB spine item {item_id!r} has no manifest entry",
                        ref=spine_ref,
                    )
                    continue
                if media.get(item_id) not in {
                    "application/xhtml+xml",
                    "text/html",
                }:
                    result.add_gap(
                        f"EPUB spine item {href!r} is not textual and was skipped",
                        ref=f"{spine_ref}#item:{href}",
                    )
                    continue
                member = _resolve_member(base, href)
                try:
                    raw = _read_bounded(archive, member, _MAX_CHAPTER_BYTES)
                except KeyError:
                    result.add_gap(
                        f"EPUB spine item {href!r} is missing",
                        ref=f"{spine_ref}#item:{href}",
                    )
                    continue
                try:
                    text = raw.decode("utf-8", errors="strict")
                except UnicodeDecodeError as exc:
                    raise _InvalidEpub(
                        f"spine item {href!r} is not strict UTF-8 at byte "
                        f"{exc.start}"
                    ) from exc

                # Lazy so a local text invocation never imports HTML parsing.
                from .html import extract_html

                chapter = extract_html(text, source=member)
                for unit in chapter.units:
                    # Spine position is part of the native address.  An EPUB is
                    # allowed to reference one manifest item more than once;
                    # omitting it would produce duplicate semantic IDs.
                    ref = f"item:{href}#{spine_ref}#{unit.origin.ref}"
                    result.units.append(
                        Unit(
                            source=source,
                            modality=unit.modality,
                            content=unit.content,
                            origin=Origin(source, ref, unit.origin.char_span),
                            role=Role.UNKNOWN,
                            structure=(spine_ref,) + unit.structure,
                            salience=unit.salience,
                            confidence=unit.confidence,
                            meta={
                                **unit.meta,
                                "spine": position,
                                "item": href,
                            },
                        )
                    )
                for gap in chapter.gaps:
                    result.add_gap(
                        f"{href}: {gap}",
                        ref=f"{spine_ref}#item:{href}#{gap.origin.ref}",
                    )
    except _InvalidEpub as exc:
        raise ValueError(f"{path.name}: invalid EPUB container ({exc})") from exc
    except (zipfile.BadZipFile, KeyError, ET.ParseError, OSError, RuntimeError) as exc:
        raise ValueError(f"{path.name}: invalid EPUB container ({exc})") from exc

    if not result.units:
        result.gaps.append("EPUB contains no addressable textual spine content")
    result.meta.update(
        {
            "title": title or None,
            "spine_items": len(spine),
            "manifest_items": len(manifest),
        }
    )
    return result


def _validate_archive(archive: zipfile.ZipFile) -> None:
    infos = archive.infolist()
    if len(infos) > _MAX_MEMBERS:
        raise _InvalidEpub(
            f"archive has {len(infos)} members; limit is {_MAX_MEMBERS}"
        )
    names: set[str] = set()
    total = 0
    for info in infos:
        name = _safe_member_name(info.filename)
        if name in names:
            raise _InvalidEpub(f"archive contains duplicate member {name!r}")
        names.add(name)
        if info.flag_bits & 0x1:
            raise _InvalidEpub(f"archive member {name!r} is encrypted")
        total += info.file_size
        if total > _MAX_TOTAL_UNCOMPRESSED:
            raise _InvalidEpub(
                "archive declared uncompressed size exceeds "
                f"{_MAX_TOTAL_UNCOMPRESSED} bytes"
            )
        if (
            info.file_size > 1024 * 1024
            and info.file_size > max(1, info.compress_size) * _MAX_COMPRESSION_RATIO
        ):
            raise _InvalidEpub(
                f"archive member {name!r} exceeds the "
                f"{_MAX_COMPRESSION_RATIO}:1 compression-ratio limit"
            )


def _safe_member_name(name: str) -> str:
    if not name or "\x00" in name or "\\" in name or name.startswith("/"):
        raise _InvalidEpub(f"unsafe archive member path {name!r}")
    normalized = posixpath.normpath(name)
    if normalized in {".", ".."} or normalized.startswith("../"):
        raise _InvalidEpub(f"unsafe archive member path {name!r}")
    return normalized


def _resolve_member(base: str, href: str) -> str:
    parsed = urlsplit(href)
    if parsed.scheme or parsed.netloc:
        raise _InvalidEpub(f"spine href {href!r} is not package-local")
    path = unquote(parsed.path)
    if not path:
        raise _InvalidEpub(f"spine href {href!r} has no member path")
    return _safe_member_name(posixpath.join(base, path))


def _read_bounded(archive: zipfile.ZipFile, name: str, limit: int) -> bytes:
    safe_name = _safe_member_name(name)
    info = archive.getinfo(safe_name)
    if info.file_size > limit:
        raise _InvalidEpub(
            f"archive member {safe_name!r} is {info.file_size} bytes; "
            f"limit is {limit}"
        )
    with archive.open(info) as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise _InvalidEpub(
            f"archive member {safe_name!r} expands beyond {limit} bytes"
        )
    return data


def _parse_xml(data: bytes, member: str) -> ET.Element:
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise _InvalidEpub(f"malformed {member}: {exc}") from exc


def _rootfile(archive: zipfile.ZipFile) -> str:
    try:
        container = _parse_xml(
            _read_bounded(archive, "META-INF/container.xml", _MAX_XML_BYTES),
            "META-INF/container.xml",
        )
    except KeyError as exc:
        raise _InvalidEpub("missing META-INF/container.xml") from exc
    paths: list[str] = []
    for element in container.iter():
        if element.tag.rsplit("}", 1)[-1] == "rootfile":
            path = element.attrib.get("full-path")
            if path:
                paths.append(_safe_member_name(path))
    if not paths:
        raise _InvalidEpub("container has no rootfile")
    return paths[0]


def _manifest(package: ET.Element) -> tuple[dict[str, str], dict[str, str]]:
    hrefs: dict[str, str] = {}
    media: dict[str, str] = {}
    for element in package.iter():
        if element.tag.rsplit("}", 1)[-1] != "item":
            continue
        item_id = element.attrib.get("id")
        href = element.attrib.get("href")
        if item_id and href:
            if item_id in hrefs:
                raise _InvalidEpub(f"duplicate manifest id {item_id!r}")
            hrefs[item_id] = href
            media[item_id] = element.attrib.get("media-type", "")
    return hrefs, media


def _spine(package: ET.Element) -> list[str]:
    return [
        element.attrib["idref"]
        for element in package.iter()
        if element.tag.rsplit("}", 1)[-1] == "itemref"
        and element.attrib.get("idref")
    ]


def _metadata_title(package: ET.Element) -> str:
    for element in package.iter():
        if element.tag.rsplit("}", 1)[-1] == "title" and element.text:
            return element.text.strip()
    return ""
