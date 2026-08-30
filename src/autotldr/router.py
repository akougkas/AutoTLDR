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
from pathlib import Path
from typing import Protocol

from .unit import Extraction


class Extractor(Protocol):
    def extract(self, path: Path) -> Extraction: ...


@dataclass(frozen=True, slots=True)
class Handler:
    """A format AutoTLDR knows how to read."""

    kind: str
    module: str
    tier: int
    extra: str | None = None
    """The optional-dependency group this handler needs, if any."""


# Extension to handler. Only what v1 actually implements is wired up; the rest
# of the tiered surface in MATRIX.md gets added here one row at a time.
_BY_SUFFIX: dict[str, Handler] = {
    ".md": Handler("markdown", "autotldr.extract.text", tier=0),
    ".markdown": Handler("markdown", "autotldr.extract.text", tier=0),
    ".txt": Handler("text", "autotldr.extract.text", tier=0),
    ".rst": Handler("text", "autotldr.extract.text", tier=0),
    ".xlsx": Handler("xlsx", "autotldr.extract.xlsx", tier=3, extra="office"),
    ".xlsm": Handler("xlsx", "autotldr.extract.xlsx", tier=3, extra="office"),
    ".pdf": Handler("pdf", "autotldr.extract.pdf", tier=1, extra="pdf"),
}

# Formats named in MATRIX.md that v1 deliberately does not handle. Meeting one
# is not a failure: AutoTLDR says which format it was and which tier owns it,
# then moves on. In a folder run, one unsupported file never stalls the fusion.
_DEFERRED: dict[str, tuple[str, int]] = {
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


class UnsupportedFormat(Exception):
    """Raised for inputs v1 knowingly declines, with enough detail to be useful."""

    def __init__(self, path: Path, kind: str, tier: int) -> None:
        self.path = path
        self.kind = kind
        self.tier = tier
        super().__init__(
            f"{path.name}: {kind} input is tier {tier}, which v1 does not read. "
            f"v1 covers tiers 0 through 3, text-derivable formats only."
        )


class UnknownFormat(Exception):
    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(f"{path.name}: unrecognized format")


def detect(path: Path) -> Handler:
    """Resolve a path to its handler.

    Extension first because it is free and correct almost always. Content
    sniffing is reserved for the extensionless and the mislabeled, and is only
    reached when the cheap path has already failed.
    """
    suffix = path.suffix.lower()

    if handler := _BY_SUFFIX.get(suffix):
        return handler

    if deferred := _DEFERRED.get(suffix):
        raise UnsupportedFormat(path, *deferred)

    if sniffed := _sniff(path):
        return sniffed

    raise UnknownFormat(path)


def _sniff(path: Path) -> Handler | None:
    """Identify by leading bytes when the extension is absent or lying."""
    try:
        with path.open("rb") as fh:
            head = fh.read(8)
    except OSError:
        return None

    if head.startswith(b"%PDF-"):
        return _BY_SUFFIX[".pdf"]
    # XLSX is a zip. So are many things, so this only fires when the extension
    # gave us nothing, and the extractor still validates the workbook itself.
    if head.startswith(b"PK\x03\x04") and path.suffix == "":
        return _BY_SUFFIX[".xlsx"]
    if _looks_like_text(head):
        return _BY_SUFFIX[".txt"]
    return None


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


def extract(path: Path) -> Extraction:
    """Detect, import the right extractor, and run it."""
    handler = detect(path)
    try:
        module = importlib.import_module(handler.module)
    except ImportError as exc:  # pragma: no cover - depends on install extras
        hint = f"  install it with: pip install 'autotldr[{handler.extra}]'" if handler.extra else ""
        raise ImportError(
            f"{handler.kind} support is not installed ({exc}).\n{hint}"
        ) from exc
    return module.extract(path)


def supported_suffixes() -> frozenset[str]:
    return frozenset(_BY_SUFFIX)
