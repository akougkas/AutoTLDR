"""Explicit, dependency-free extension metadata for AutoTLDR adapters.

The extension layer is deliberately smaller than a plugin framework.  It
records *what* an adapter can handle and *where* its callable lives, while
leaving acquisition, extraction, budget selection, and rendering in the core.
Third-party code is imported only when a caller explicitly resolves a
registered adapter or calls :func:`load_extension`.

There is intentionally no entry-point scan, environment-variable scan, or
directory discovery here.  Installing a package must never make untrusted code
run during ``autotldr`` startup.  A community package is enabled by an explicit
``module[:factory]`` reference; its zero-argument factory returns immutable
specifications, not a callback that mutates the registry.

This module imports only the Python standard library.  Core result types are
imported lazily by the optional output-conformance helpers.
"""

from __future__ import annotations

import importlib
import re
from dataclasses import dataclass
from threading import RLock
from typing import Callable, Iterable, TypeAlias


EXTENSION_API_VERSION = 1
"""Version of the specification/factory contract implemented by this module."""

CAPABILITY_MANIFEST_SCHEMA = "autotldr-extension-capabilities-v1"
EXTRACTOR_CALL_CONTRACT = "source-to-extraction-v1"
ACQUISITION_CALL_CONTRACT = "source-to-collection-acquisition-v1"
RENDERER_BUILDER_CONTRACT = "bundle-options-to-text-v1"
DEFAULT_EXTENSION_FACTORY = "autotldr_extension"


class ExtensionError(ValueError):
    """Base class for extension declaration and loading failures."""


class ExtensionValidationError(ExtensionError):
    """An immutable extension specification is malformed."""


class ExtensionCollisionError(ExtensionError):
    """Two specifications claim the same deterministic routing key."""


class ExtensionLoadError(ExtensionError):
    """Explicitly requested third-party code could not be loaded safely."""


class ExtensionConformanceError(ExtensionError):
    """A declared callable or its result does not satisfy the core contract."""


class UnknownExtensionError(ExtensionError, LookupError):
    """No registered extension owns a requested capability key."""


_SLUG_RE = re.compile(r"[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*\Z")
_DOTTED_RE = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\Z"
)
_MEDIA_RE = re.compile(
    r"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+\Z"
)


def _slug(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ExtensionValidationError(f"{label} must be a string")
    normalized = value.strip().lower()
    if not normalized or not _SLUG_RE.fullmatch(normalized):
        raise ExtensionValidationError(
            f"{label} must be a lowercase ASCII capability name"
        )
    return normalized


def _dotted(value: object, label: str) -> str:
    if not isinstance(value, str) or not _DOTTED_RE.fullmatch(value):
        raise ExtensionValidationError(
            f"{label} must be an absolute dotted Python name"
        )
    return value


def _string_values(values: object, label: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes, bytearray)):
        raise ExtensionValidationError(f"{label} must be a sequence of strings")
    try:
        raw = tuple(values)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ExtensionValidationError(
            f"{label} must be a sequence of strings"
        ) from exc
    normalized = tuple(_slug(value, f"{label} item") for value in raw)
    if len(normalized) != len(set(normalized)):
        raise ExtensionValidationError(f"{label} must not contain duplicates")
    return tuple(sorted(normalized))


def _suffixes(values: object) -> tuple[str, ...]:
    if isinstance(values, (str, bytes, bytearray)):
        raise ExtensionValidationError("suffixes must be a sequence of strings")
    try:
        raw = tuple(values)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ExtensionValidationError(
            "suffixes must be a sequence of strings"
        ) from exc

    normalized: list[str] = []
    for value in raw:
        if not isinstance(value, str):
            raise ExtensionValidationError("suffix item must be a string")
        suffix = value.strip().lower()
        if (
            not suffix.startswith(".")
            or suffix == "."
            or "/" in suffix
            or "\\" in suffix
            or any(character.isspace() for character in suffix)
        ):
            raise ExtensionValidationError(
                "suffix item must start with '.' and contain no path separator"
            )
        try:
            suffix.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ExtensionValidationError(
                "suffix item must contain ASCII characters only"
            ) from exc
        normalized.append(suffix)

    if len(normalized) != len(set(normalized)):
        raise ExtensionValidationError("suffixes must not contain duplicates")
    return tuple(sorted(normalized))


def _media_types(values: object) -> tuple[str, ...]:
    if isinstance(values, (str, bytes, bytearray)):
        raise ExtensionValidationError("media_types must be a sequence of strings")
    try:
        raw = tuple(values)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ExtensionValidationError(
            "media_types must be a sequence of strings"
        ) from exc

    normalized: list[str] = []
    for value in raw:
        if not isinstance(value, str):
            raise ExtensionValidationError("media type must be a string")
        media_type = value.strip().lower()
        if not _MEDIA_RE.fullmatch(media_type):
            raise ExtensionValidationError(
                "media type must be a parameter-free lowercase type/subtype"
            )
        normalized.append(media_type)
    if len(normalized) != len(set(normalized)):
        raise ExtensionValidationError("media_types must not contain duplicates")
    return tuple(sorted(normalized))


def _optional_extra(value: object) -> str | None:
    if value is None:
        return None
    return _slug(value, "optional dependency extra")


def _tier(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ExtensionValidationError("tier must be a non-negative integer")
    return value


def _contract_version(value: object) -> int:
    if type(value) is not int or value != EXTENSION_API_VERSION:
        raise ExtensionValidationError(
            f"extension contract_version must be {EXTENSION_API_VERSION}"
        )
    return EXTENSION_API_VERSION


@dataclass(frozen=True, slots=True)
class SignatureProbe:
    """One declarative strong byte signature.

    ``offset`` is measured from the beginning when non-negative and from the
    end when negative, so formats such as Parquet can declare both ``0`` and
    ``-4`` probes.  A mask is optional; mask bits set to zero are ignored.
    Probe alternatives are ORed within one extractor specification.
    """

    pattern: bytes
    offset: int = 0
    mask: bytes | None = None

    def __post_init__(self) -> None:
        if type(self) is not SignatureProbe:
            raise ExtensionValidationError(
                "signature probe must be the concrete core class"
            )
        if type(self.pattern) is not bytes or not self.pattern:
            raise ExtensionValidationError(
                "signature pattern must be non-empty immutable bytes"
            )
        if len(self.pattern) > 4_096:
            raise ExtensionValidationError(
                "signature pattern must not exceed 4096 bytes"
            )
        if type(self.offset) is not int:
            raise ExtensionValidationError("signature offset must be an integer")
        if self.mask is not None:
            if type(self.mask) is not bytes:
                raise ExtensionValidationError(
                    "signature mask must be immutable bytes"
                )
            if len(self.mask) != len(self.pattern):
                raise ExtensionValidationError(
                    "signature mask must have the same length as its pattern"
                )
            if not any(self.mask):
                raise ExtensionValidationError(
                    "signature mask must inspect at least one information bit"
                )

    @property
    def collision_key(self) -> tuple[int, bytes, bytes]:
        mask = self.mask or (b"\xff" * len(self.pattern))
        normalized_pattern = bytes(
            pattern_byte & mask_byte
            for pattern_byte, mask_byte in zip(self.pattern, mask, strict=True)
        )
        return self.offset, normalized_pattern, mask

    def matches(self, payload: bytes | bytearray | memoryview) -> bool:
        """Return whether this probe matches already-acquired bytes."""

        view = memoryview(payload)
        start = self.offset if self.offset >= 0 else len(view) + self.offset
        end = start + len(self.pattern)
        if start < 0 or end > len(view):
            return False
        candidate = view[start:end]
        if self.mask is None:
            return candidate == self.pattern
        return all(
            (candidate_byte & mask_byte) == (pattern_byte & mask_byte)
            for candidate_byte, pattern_byte, mask_byte in zip(
                candidate, self.pattern, self.mask, strict=True
            )
        )

    def as_manifest(self) -> dict[str, object]:
        return {
            "offset": self.offset,
            "pattern_hex": self.pattern.hex(),
            "mask_hex": None if self.mask is None else self.mask.hex(),
        }


def _signature_probes(values: object) -> tuple[SignatureProbe, ...]:
    if isinstance(values, (str, bytes, bytearray)):
        raise ExtensionValidationError(
            "signatures must be a sequence of SignatureProbe objects"
        )
    try:
        probes = tuple(values)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ExtensionValidationError(
            "signatures must be a sequence of SignatureProbe objects"
        ) from exc
    if any(type(probe) is not SignatureProbe for probe in probes):
        raise ExtensionValidationError(
            "signatures must contain only SignatureProbe objects"
        )
    keys = [probe.collision_key for probe in probes]
    if len(keys) != len(set(keys)):
        raise ExtensionValidationError("signatures must not contain duplicates")
    return tuple(
        sorted(
            probes,
            key=lambda probe: (
                probe.offset,
                probe.pattern,
                b"" if probe.mask is None else probe.mask,
            ),
        )
    )


def _base_spec(spec: object) -> None:
    object.__setattr__(spec, "name", _slug(getattr(spec, "name"), "name"))
    object.__setattr__(
        spec, "module", _dotted(getattr(spec, "module"), "module")
    )
    object.__setattr__(
        spec, "callable", _dotted(getattr(spec, "callable"), "callable")
    )
    object.__setattr__(
        spec,
        "contract_version",
        _contract_version(getattr(spec, "contract_version")),
    )


@dataclass(frozen=True, slots=True)
class ExtractorSpec:
    """Immutable routing metadata for one path-to-Extraction adapter."""

    name: str
    module: str
    callable: str
    kinds: tuple[str, ...]
    aliases: tuple[str, ...] = ()
    suffixes: tuple[str, ...] = ()
    media_types: tuple[str, ...] = ()
    signatures: tuple[SignatureProbe, ...] = ()
    tier: int = 0
    extra: str | None = None
    contract_version: int = EXTENSION_API_VERSION

    def __post_init__(self) -> None:
        if type(self) is not ExtractorSpec:
            raise ExtensionValidationError(
                "extractor specification must be the concrete core class"
            )
        _base_spec(self)
        object.__setattr__(self, "kinds", _string_values(self.kinds, "kinds"))
        if not self.kinds:
            raise ExtensionValidationError("extractor kinds must not be empty")
        object.__setattr__(
            self, "aliases", _string_values(self.aliases, "aliases")
        )
        object.__setattr__(self, "suffixes", _suffixes(self.suffixes))
        object.__setattr__(
            self, "media_types", _media_types(self.media_types)
        )
        object.__setattr__(
            self, "signatures", _signature_probes(self.signatures)
        )
        object.__setattr__(self, "tier", _tier(self.tier))
        object.__setattr__(self, "extra", _optional_extra(self.extra))

    @property
    def call_contract(self) -> str:
        return EXTRACTOR_CALL_CONTRACT

    def as_manifest(self) -> dict[str, object]:
        return {
            "name": self.name,
            "module": self.module,
            "callable": self.callable,
            "call_contract": self.call_contract,
            "kinds": list(self.kinds),
            "aliases": list(self.aliases),
            "suffixes": list(self.suffixes),
            "media_types": list(self.media_types),
            "signatures": [probe.as_manifest() for probe in self.signatures],
            "tier": self.tier,
            "extra": self.extra,
            "contract_version": self.contract_version,
        }


@dataclass(frozen=True, slots=True)
class AcquisitionSpec:
    """Immutable metadata for one source-to-collection acquisition adapter."""

    name: str
    module: str
    callable: str
    kinds: tuple[str, ...]
    aliases: tuple[str, ...] = ()
    tier: int = 2
    extra: str | None = None
    contract_version: int = EXTENSION_API_VERSION

    def __post_init__(self) -> None:
        if type(self) is not AcquisitionSpec:
            raise ExtensionValidationError(
                "acquisition specification must be the concrete core class"
            )
        _base_spec(self)
        object.__setattr__(self, "kinds", _string_values(self.kinds, "kinds"))
        if not self.kinds:
            raise ExtensionValidationError("acquisition kinds must not be empty")
        object.__setattr__(
            self, "aliases", _string_values(self.aliases, "aliases")
        )
        object.__setattr__(self, "tier", _tier(self.tier))
        object.__setattr__(self, "extra", _optional_extra(self.extra))

    @property
    def call_contract(self) -> str:
        return ACQUISITION_CALL_CONTRACT

    def as_manifest(self) -> dict[str, object]:
        return {
            "name": self.name,
            "module": self.module,
            "callable": self.callable,
            "call_contract": self.call_contract,
            "kinds": list(self.kinds),
            "aliases": list(self.aliases),
            "tier": self.tier,
            "extra": self.extra,
            "contract_version": self.contract_version,
        }


@dataclass(frozen=True, slots=True)
class RendererSpec:
    """Metadata for a core-budget-compatible ``(bundle, options) -> str`` builder."""

    name: str
    module: str
    callable: str
    aliases: tuple[str, ...] = ()
    suffixes: tuple[str, ...] = ()
    media_types: tuple[str, ...] = ()
    extra: str | None = None
    supports_citations: bool = True
    supports_color: bool = False
    builder_contract: str = RENDERER_BUILDER_CONTRACT
    contract_version: int = EXTENSION_API_VERSION

    def __post_init__(self) -> None:
        if type(self) is not RendererSpec:
            raise ExtensionValidationError(
                "renderer specification must be the concrete core class"
            )
        _base_spec(self)
        object.__setattr__(
            self, "aliases", _string_values(self.aliases, "aliases")
        )
        object.__setattr__(self, "suffixes", _suffixes(self.suffixes))
        object.__setattr__(
            self, "media_types", _media_types(self.media_types)
        )
        object.__setattr__(self, "extra", _optional_extra(self.extra))
        if not isinstance(self.supports_citations, bool):
            raise ExtensionValidationError("supports_citations must be boolean")
        if not isinstance(self.supports_color, bool):
            raise ExtensionValidationError("supports_color must be boolean")
        if self.builder_contract != RENDERER_BUILDER_CONTRACT:
            raise ExtensionValidationError(
                "renderer builder_contract is not supported by this core"
            )

    def as_manifest(self) -> dict[str, object]:
        return {
            "name": self.name,
            "module": self.module,
            "callable": self.callable,
            "builder_contract": self.builder_contract,
            "aliases": list(self.aliases),
            "suffixes": list(self.suffixes),
            "media_types": list(self.media_types),
            "extra": self.extra,
            "supports_citations": self.supports_citations,
            "supports_color": self.supports_color,
            "contract_version": self.contract_version,
        }


ExtensionSpec: TypeAlias = ExtractorSpec | AcquisitionSpec | RendererSpec


def _spec_category(spec: ExtensionSpec) -> str:
    if type(spec) is ExtractorSpec:
        return "extractor"
    if type(spec) is AcquisitionSpec:
        return "acquisition"
    if type(spec) is RendererSpec:
        return "renderer"
    raise ExtensionValidationError("registry accepts extension specifications only")


@dataclass(frozen=True, slots=True)
class RegistrySnapshot:
    """An immutable, deterministically ordered view of one registry."""

    extractors: tuple[ExtractorSpec, ...]
    acquisitions: tuple[AcquisitionSpec, ...]
    renderers: tuple[RendererSpec, ...]
    api_contract_version: int = EXTENSION_API_VERSION

    def __post_init__(self) -> None:
        if type(self.extractors) is not tuple or any(
            type(spec) is not ExtractorSpec for spec in self.extractors
        ):
            raise ExtensionValidationError(
                "registry snapshot extractors must be concrete specifications"
            )
        if type(self.acquisitions) is not tuple or any(
            type(spec) is not AcquisitionSpec for spec in self.acquisitions
        ):
            raise ExtensionValidationError(
                "registry snapshot acquisitions must be concrete specifications"
            )
        if type(self.renderers) is not tuple or any(
            type(spec) is not RendererSpec for spec in self.renderers
        ):
            raise ExtensionValidationError(
                "registry snapshot renderers must be concrete specifications"
            )
        _contract_version(self.api_contract_version)

    def capability_manifest(self) -> dict[str, object]:
        return {
            "schema": CAPABILITY_MANIFEST_SCHEMA,
            "api_contract_version": self.api_contract_version,
            "counts": {
                "extractors": len(self.extractors),
                "acquisitions": len(self.acquisitions),
                "renderers": len(self.renderers),
            },
            "extractors": [spec.as_manifest() for spec in self.extractors],
            "acquisitions": [spec.as_manifest() for spec in self.acquisitions],
            "renderers": [spec.as_manifest() for spec in self.renderers],
        }


def _claim(
    index: dict[object, ExtensionSpec],
    key: object,
    spec: ExtensionSpec,
    namespace: str,
) -> None:
    previous = index.get(key)
    if previous is not None and previous != spec:
        displayed_key = (
            "<binary signature>"
            if _contains_binary_key(key)
            else repr(key)
        )
        raise ExtensionCollisionError(
            f"{namespace} capability {displayed_key} is claimed by both "
            f"{previous.name!r} and {spec.name!r}"
        )
    index[key] = spec


def _contains_binary_key(value: object) -> bool:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return True
    if isinstance(value, tuple):
        return any(_contains_binary_key(item) for item in value)
    return False


class ExtensionRegistry:
    """An explicit, collision-rejecting registry of lazy adapter metadata.

    Registration is atomic: if any item in :meth:`register_many` conflicts,
    none of that call's items become visible.  Every public listing and
    snapshot is sorted by canonical name rather than registration order.
    """

    def __init__(self, specs: Iterable[ExtensionSpec] = ()) -> None:
        self._lock = RLock()
        self._extractors: dict[str, ExtractorSpec] = {}
        self._acquisitions: dict[str, AcquisitionSpec] = {}
        self._renderers: dict[str, RendererSpec] = {}
        self._extractor_names: dict[str, ExtractorSpec] = {}
        self._extractor_suffixes: dict[str, ExtractorSpec] = {}
        self._extractor_media: dict[str, ExtractorSpec] = {}
        self._extractor_signatures: dict[
            tuple[int, bytes, bytes], ExtractorSpec
        ] = {}
        self._acquisition_names: dict[str, AcquisitionSpec] = {}
        self._renderer_names: dict[str, RendererSpec] = {}
        self._renderer_suffixes: dict[str, RendererSpec] = {}
        self._renderer_media: dict[str, RendererSpec] = {}
        self._resolved: dict[tuple[str, str], Callable[..., object]] = {}
        self.register_many(specs)

    def __len__(self) -> int:
        with self._lock:
            return (
                len(self._extractors)
                + len(self._acquisitions)
                + len(self._renderers)
            )

    @property
    def extractors(self) -> tuple[ExtractorSpec, ...]:
        with self._lock:
            return tuple(
                sorted(self._extractors.values(), key=lambda spec: spec.name)
            )

    @property
    def acquisitions(self) -> tuple[AcquisitionSpec, ...]:
        with self._lock:
            return tuple(
                sorted(self._acquisitions.values(), key=lambda spec: spec.name)
            )

    @property
    def renderers(self) -> tuple[RendererSpec, ...]:
        with self._lock:
            return tuple(
                sorted(self._renderers.values(), key=lambda spec: spec.name)
            )

    def register(self, spec: ExtensionSpec) -> None:
        self.register_many((spec,))

    def register_many(self, specs: Iterable[ExtensionSpec]) -> None:
        try:
            pending = tuple(specs)
        except TypeError as exc:
            raise ExtensionValidationError(
                "register_many expects extension specifications"
            ) from exc
        for spec in pending:
            _spec_category(spec)

        with self._lock:
            extractors = dict(self._extractors)
            acquisitions = dict(self._acquisitions)
            renderers = dict(self._renderers)
            extractor_names = dict(self._extractor_names)
            extractor_suffixes = dict(self._extractor_suffixes)
            extractor_media = dict(self._extractor_media)
            extractor_signatures = dict(self._extractor_signatures)
            acquisition_names = dict(self._acquisition_names)
            renderer_names = dict(self._renderer_names)
            renderer_suffixes = dict(self._renderer_suffixes)
            renderer_media = dict(self._renderer_media)

            for spec in pending:
                if type(spec) is ExtractorSpec:
                    if spec.name in extractors:
                        raise ExtensionCollisionError(
                            f"extractor name {spec.name!r} is already registered"
                        )
                    extractors[spec.name] = spec
                    for key in (spec.name, *spec.kinds, *spec.aliases):
                        _claim(extractor_names, key, spec, "extractor name/kind")
                    for key in spec.suffixes:
                        _claim(extractor_suffixes, key, spec, "extractor suffix")
                    for key in spec.media_types:
                        _claim(extractor_media, key, spec, "extractor media type")
                    for probe in spec.signatures:
                        _claim(
                            extractor_signatures,
                            probe.collision_key,
                            spec,
                            "extractor strong signature",
                        )
                elif type(spec) is AcquisitionSpec:
                    if spec.name in acquisitions:
                        raise ExtensionCollisionError(
                            f"acquisition name {spec.name!r} is already registered"
                        )
                    acquisitions[spec.name] = spec
                    for key in (spec.name, *spec.kinds, *spec.aliases):
                        _claim(
                            acquisition_names,
                            key,
                            spec,
                            "acquisition name/kind",
                        )
                else:
                    assert type(spec) is RendererSpec
                    if spec.name in renderers:
                        raise ExtensionCollisionError(
                            f"renderer name {spec.name!r} is already registered"
                        )
                    renderers[spec.name] = spec
                    for key in (spec.name, *spec.aliases):
                        _claim(renderer_names, key, spec, "renderer name")
                    for key in spec.suffixes:
                        _claim(renderer_suffixes, key, spec, "renderer suffix")
                    for key in spec.media_types:
                        _claim(renderer_media, key, spec, "renderer media type")

            self._extractors = extractors
            self._acquisitions = acquisitions
            self._renderers = renderers
            self._extractor_names = extractor_names
            self._extractor_suffixes = extractor_suffixes
            self._extractor_media = extractor_media
            self._extractor_signatures = extractor_signatures
            self._acquisition_names = acquisition_names
            self._renderer_names = renderer_names
            self._renderer_suffixes = renderer_suffixes
            self._renderer_media = renderer_media

    def get_extractor(self, name_or_kind: str) -> ExtractorSpec:
        key = _slug(name_or_kind, "extractor name or kind")
        with self._lock:
            spec = self._extractor_names.get(key)
        if spec is None:
            raise UnknownExtensionError(f"no extractor owns {key!r}")
        return spec

    def extractor_for_suffix(self, suffix: str) -> ExtractorSpec | None:
        key = _lookup_suffix(suffix)
        with self._lock:
            return self._extractor_suffixes.get(key)

    def extractor_for_media_type(self, media_type: str) -> ExtractorSpec | None:
        key = _lookup_media_type(media_type)
        with self._lock:
            return self._extractor_media.get(key)

    def extractor_for_bytes(
        self, payload: bytes | bytearray | memoryview
    ) -> ExtractorSpec | None:
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            raise TypeError("payload must be bytes-like")
        matches = {
            spec
            for spec in self.extractors
            if any(probe.matches(payload) for probe in spec.signatures)
        }
        if len(matches) > 1:
            names = ", ".join(sorted(spec.name for spec in matches))
            raise ExtensionCollisionError(
                f"payload matches multiple strong-signature extractors: {names}"
            )
        return next(iter(matches), None)

    def get_acquisition(self, name_or_kind: str) -> AcquisitionSpec:
        key = _slug(name_or_kind, "acquisition name or kind")
        with self._lock:
            spec = self._acquisition_names.get(key)
        if spec is None:
            raise UnknownExtensionError(f"no acquisition adapter owns {key!r}")
        return spec

    def get_renderer(self, name_or_alias: str) -> RendererSpec:
        key = _slug(name_or_alias, "renderer name or alias")
        with self._lock:
            spec = self._renderer_names.get(key)
        if spec is None:
            raise UnknownExtensionError(f"no renderer owns {key!r}")
        return spec

    def renderer_for_suffix(self, suffix: str) -> RendererSpec | None:
        key = _lookup_suffix(suffix)
        with self._lock:
            return self._renderer_suffixes.get(key)

    def renderer_for_media_type(self, media_type: str) -> RendererSpec | None:
        key = _lookup_media_type(media_type)
        with self._lock:
            return self._renderer_media.get(key)

    def resolve_extractor(
        self, spec_or_name: ExtractorSpec | str
    ) -> Callable[..., object]:
        spec = self._registered_extractor(spec_or_name)
        return self._resolve(spec, "extractor", validate_extractor_callable)

    def resolve_acquisition(
        self, spec_or_name: AcquisitionSpec | str
    ) -> Callable[..., object]:
        spec = self._registered_acquisition(spec_or_name)
        return self._resolve(spec, "acquisition", validate_acquisition_callable)

    def resolve_renderer(
        self, spec_or_name: RendererSpec | str
    ) -> Callable[..., object]:
        spec = self._registered_renderer(spec_or_name)
        return self._resolve(spec, "renderer", validate_renderer_callable)

    def _registered_extractor(self, value: ExtractorSpec | str) -> ExtractorSpec:
        if isinstance(value, str):
            return self.get_extractor(value)
        if type(value) is not ExtractorSpec:
            raise ExtensionValidationError("expected an ExtractorSpec or name")
        with self._lock:
            registered = self._extractors.get(value.name)
        if registered != value:
            raise UnknownExtensionError(
                f"extractor {value.name!r} is not registered in this registry"
            )
        return registered

    def _registered_acquisition(
        self, value: AcquisitionSpec | str
    ) -> AcquisitionSpec:
        if isinstance(value, str):
            return self.get_acquisition(value)
        if type(value) is not AcquisitionSpec:
            raise ExtensionValidationError("expected an AcquisitionSpec or name")
        with self._lock:
            registered = self._acquisitions.get(value.name)
        if registered != value:
            raise UnknownExtensionError(
                f"acquisition {value.name!r} is not registered in this registry"
            )
        return registered

    def _registered_renderer(self, value: RendererSpec | str) -> RendererSpec:
        if isinstance(value, str):
            return self.get_renderer(value)
        if type(value) is not RendererSpec:
            raise ExtensionValidationError("expected a RendererSpec or name")
        with self._lock:
            registered = self._renderers.get(value.name)
        if registered != value:
            raise UnknownExtensionError(
                f"renderer {value.name!r} is not registered in this registry"
            )
        return registered

    def _resolve(
        self,
        spec: ExtensionSpec,
        category: str,
        validator: Callable[[object], Callable[..., object]],
    ) -> Callable[..., object]:
        cache_key = category, spec.name
        with self._lock:
            cached = self._resolved.get(cache_key)
        if cached is not None:
            return cached

        loaded = _load_declared_callable(spec, category)
        try:
            validated = validator(loaded)
        except ExtensionConformanceError:
            raise
        except Exception:
            raise ExtensionConformanceError(
                f"declared {category} callable could not be inspected"
            ) from None

        with self._lock:
            existing = self._resolved.setdefault(cache_key, validated)
        return existing

    def snapshot(self) -> RegistrySnapshot:
        return RegistrySnapshot(
            extractors=self.extractors,
            acquisitions=self.acquisitions,
            renderers=self.renderers,
        )

    def capability_manifest(self) -> dict[str, object]:
        return self.snapshot().capability_manifest()


def _lookup_suffix(value: object) -> str:
    if not isinstance(value, str):
        raise ExtensionValidationError("suffix lookup must be a string")
    suffix = value.strip().lower()
    if suffix and not suffix.startswith("."):
        suffix = f".{suffix}"
    # Reuse declaration validation so path separators and unusual values fail
    # consistently, while returning the normalized single value.
    return _suffixes((suffix,))[0]


def _lookup_media_type(value: object) -> str:
    if not isinstance(value, str):
        raise ExtensionValidationError("media type lookup must be a string")
    media_type = value.partition(";")[0].strip().lower()
    return _media_types((media_type,))[0]


def _load_declared_callable(
    spec: ExtensionSpec, category: str
) -> Callable[..., object]:
    try:
        module = importlib.import_module(spec.module)
    except Exception:
        hint = "" if spec.extra is None else f"; install the {spec.extra!r} extra"
        raise ExtensionLoadError(
            f"could not import module for {category} {spec.name!r}{hint}"
        ) from None

    value: object = module
    try:
        for component in spec.callable.split("."):
            value = getattr(value, component)
    except Exception:
        raise ExtensionLoadError(
            f"module for {category} {spec.name!r} does not expose its "
            "declared callable"
        ) from None
    if not callable(value):
        raise ExtensionLoadError(
            f"declared object for {category} {spec.name!r} is not callable"
        )
    return value


def _validate_callable_arity(
    value: object, *, positional: int, label: str
) -> Callable[..., object]:
    if not callable(value):
        raise ExtensionConformanceError(f"{label} must be callable")
    # ``inspect`` pulls in AST/tokenization machinery.  Keep it below the
    # explicit resolution boundary so registering declarative metadata cannot
    # tax the Tier 0 cold-start path.
    import inspect

    try:
        signature = inspect.signature(value)
    except (TypeError, ValueError):
        # Some extension callables implemented in C expose no signature.  The
        # presence check is the strongest safe static check available; output
        # validation still closes the result boundary after invocation.
        return value
    except Exception:
        raise ExtensionConformanceError(f"{label} signature inspection failed") from None
    try:
        signature.bind(*(object() for _ in range(positional)))
    except TypeError:
        raise ExtensionConformanceError(
            f"{label} must accept exactly the core call shape"
        ) from None
    return value


def validate_extractor_callable(value: object) -> Callable[..., object]:
    """Require a callable invocable with one acquired source argument."""

    return _validate_callable_arity(value, positional=1, label="extractor")


def validate_acquisition_callable(value: object) -> Callable[..., object]:
    """Require a callable invocable with one collection source argument."""

    return _validate_callable_arity(value, positional=1, label="acquisition adapter")


def validate_renderer_callable(value: object) -> Callable[..., object]:
    """Require a builder invocable as ``builder(bundle, render_options)``."""

    return _validate_callable_arity(value, positional=2, label="renderer builder")


_MAX_CONFORMANCE_JSON_DEPTH = 64
_MAX_CONFORMANCE_JSON_NODES = 100_000


def _conformance_text(
    value: object,
    label: str,
    *,
    nonempty: bool = False,
    nonblank: bool = False,
) -> str:
    """Validate one concrete UTF-8 string without invoking plugin coercions."""

    if type(value) is not str:
        raise ExtensionConformanceError(f"{label} must be a concrete string")
    if nonempty and not value:
        raise ExtensionConformanceError(f"{label} must not be empty")
    if nonblank and not value.strip():
        raise ExtensionConformanceError(f"{label} must not be blank")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise ExtensionConformanceError(f"{label} must be valid UTF-8 text") from None
    return value


def _conformance_number(
    value: object,
    label: str,
    *,
    minimum: float,
    maximum: float,
) -> None:
    """Validate a finite concrete JSON number in an inclusive range."""

    import math

    if type(value) not in (int, float) or not math.isfinite(value):
        raise ExtensionConformanceError(
            f"{label} must be a finite concrete number"
        )
    if value < minimum or value > maximum:
        raise ExtensionConformanceError(
            f"{label} must be between {minimum:g} and {maximum:g}"
        )


def _validate_json_object(value: object, label: str) -> None:
    """Require an exact, bounded JSON tree and a canonical UTF-8 encoding.

    ``json.dumps`` accepts several lossy conveniences: integer dictionary keys,
    tuples, enum/string subclasses, and non-finite floats.  None is suitable at
    an untrusted adapter boundary because the object inspected by the core
    would differ from the object committed to the wire.  The structural walk
    rejects those coercions before the standard encoder is asked to confirm
    that the remaining tree is serializable.
    """

    import json
    import math

    seen: set[int] = set()
    nodes = 0

    def visit(item: object, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > _MAX_CONFORMANCE_JSON_NODES:
            raise ExtensionConformanceError(
                f"{label} exceeds the conformance node limit"
            )

        item_type = type(item)
        if item is None or item_type is bool:
            return
        if item_type is str:
            _conformance_text(item, f"{label} string")
            return
        if item_type is int:
            return
        if item_type is float:
            if not math.isfinite(item):
                raise ExtensionConformanceError(
                    f"{label} contains NaN or Infinity"
                )
            return
        if item_type not in (dict, list):
            raise ExtensionConformanceError(
                f"{label} contains a non-canonical JSON value"
            )
        if depth >= _MAX_CONFORMANCE_JSON_DEPTH:
            raise ExtensionConformanceError(
                f"{label} exceeds the conformance nesting limit"
            )

        identity = id(item)
        if identity in seen:
            raise ExtensionConformanceError(f"{label} contains a cycle")
        seen.add(identity)
        try:
            if item_type is list:
                for child in item:
                    visit(child, depth + 1)
            else:
                keys = list(item)
                if any(type(key) is not str for key in keys):
                    raise ExtensionConformanceError(
                        f"{label} contains a non-string dictionary key"
                    )
                for key in sorted(keys):
                    _conformance_text(key, f"{label} dictionary key")
                    visit(item[key], depth + 1)
        finally:
            seen.remove(identity)

    visit(value, 0)
    try:
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8", errors="strict")
    except (OverflowError, TypeError, UnicodeError, ValueError):
        raise ExtensionConformanceError(
            f"{label} has no canonical JSON encoding"
        ) from None


def _validate_origin(value: object, *, label: str, Origin: type) -> None:
    if type(value) is not Origin:
        raise ExtensionConformanceError(f"{label} must be a concrete Origin")
    _conformance_text(value.source, f"{label} source", nonempty=True)
    _conformance_text(value.ref, f"{label} ref", nonblank=True)
    span = value.char_span
    if span is None:
        return
    if type(span) is not tuple or len(span) != 2:
        raise ExtensionConformanceError(
            f"{label} char span must be a concrete two-item tuple"
        )
    start, end = span
    if (
        type(start) is not int
        or type(end) is not int
        or start < 0
        or end < start
    ):
        raise ExtensionConformanceError(
            f"{label} char span must be a non-negative half-open integer span"
        )


def _validate_input_manifest_claims(result: object) -> None:
    """Validate optional input/count claims without requiring their presence."""

    import re

    counts = result.meta.get("counts")
    if counts is not None:
        if type(counts) is not dict:
            raise ExtensionConformanceError(
                "extraction counts claim must be a concrete dictionary"
            )
        expected_counts = {
            "units": len(result.units),
            "relations": len(result.relations),
            "gaps": len(result.gaps),
            "summary_claims": len(result.summary_claims),
        }
        for key, expected in expected_counts.items():
            if key in counts and (
                type(counts[key]) is not int or counts[key] != expected
            ):
                raise ExtensionConformanceError(
                    "extraction counts claim does not match the result"
                )

    if "inputs" not in result.meta:
        return
    inputs = result.meta["inputs"]
    if type(inputs) is not list:
        raise ExtensionConformanceError(
            "extraction inputs claim must be a concrete list"
        )
    if result.kind != "collection" and len(inputs) != 1:
        raise ExtensionConformanceError(
            "non-collection extraction inputs claim must contain one record"
        )

    seen_sources: set[str] = set()
    for record in inputs:
        if type(record) is not dict:
            raise ExtensionConformanceError(
                "extraction input manifest item must be a concrete dictionary"
            )
        source = record.get("source")
        if type(source) is not str:
            raise ExtensionConformanceError(
                "extraction input manifest item must name a source"
            )
        _conformance_text(source, "extraction input source", nonempty=True)
        if source in seen_sources:
            raise ExtensionConformanceError(
                "extraction input manifest sources must be unique"
            )
        seen_sources.add(source)
        if result.kind != "collection" and source != result.source:
            raise ExtensionConformanceError(
                "extraction input manifest source must match the result"
            )
        if "kind" in record:
            _conformance_text(
                record["kind"], "extraction input kind", nonblank=True
            )
        if "bytes" in record and (
            type(record["bytes"]) is not int or record["bytes"] < 0
        ):
            raise ExtensionConformanceError(
                "extraction input byte count must be a non-negative integer"
            )
        if "tier" in record and (
            type(record["tier"]) is not int or record["tier"] < 0
        ):
            raise ExtensionConformanceError(
                "extraction input tier must be a non-negative integer"
            )
        if "sha256" in record and (
            type(record["sha256"]) is not str
            or re.fullmatch(r"[0-9a-f]{64}", record["sha256"]) is None
        ):
            raise ExtensionConformanceError(
                "extraction input SHA-256 must be 64 lowercase hex characters"
            )


def _validate_extraction(
    value: object,
    *,
    Extraction: type,
    Gap: type,
    GapKind: type,
    GroundedStatement: type,
    Modality: type,
    Origin: type,
    Relation: type,
    RelationKind: type,
    Role: type,
    Unit: type,
    _GapList: type,
):
    if type(value) is not Extraction:
        raise ExtensionConformanceError(
            "extractor result must be a concrete autotldr.unit.Extraction"
        )

    source = _conformance_text(
        value.source, "extraction source", nonempty=True
    )
    kind = _conformance_text(value.kind, "extraction kind", nonblank=True)
    if type(value.units) is not list:
        raise ExtensionConformanceError("extraction units must be a concrete list")
    if type(value.relations) is not list:
        raise ExtensionConformanceError(
            "extraction relations must be a concrete list"
        )
    if type(value.gaps) is not _GapList or type(value.gaps.source) is not str:
        raise ExtensionConformanceError(
            "extraction gaps must be the core addressable gap list"
        )
    if value.gaps.source != source:
        raise ExtensionConformanceError(
            "extraction gap-list source must match the result"
        )
    if type(value.meta) is not dict:
        raise ExtensionConformanceError(
            "extraction metadata must be a concrete dictionary"
        )
    if type(value.summary_claims) is not list:
        raise ExtensionConformanceError(
            "extraction summary claims must be a concrete list"
        )
    _validate_json_object(value.meta, "extraction metadata")

    units_by_id: dict[str, object] = {}
    for unit in value.units:
        if type(unit) is not Unit:
            raise ExtensionConformanceError(
                "extraction unit must be a concrete Unit"
            )
        unit_source = _conformance_text(
            unit.source, "unit source", nonempty=True
        )
        if type(unit.modality) is not Modality:
            raise ExtensionConformanceError("unit modality must be a concrete enum")
        _conformance_text(unit.content, "unit content", nonblank=True)
        _validate_origin(unit.origin, label="unit origin", Origin=Origin)
        if unit_source != unit.origin.source:
            raise ExtensionConformanceError(
                "unit source must match its origin source"
            )
        if kind != "collection" and unit_source != source:
            raise ExtensionConformanceError(
                "non-collection unit source must match the extraction source"
            )
        if type(unit.role) is not Role:
            raise ExtensionConformanceError("unit role must be a concrete enum")
        if unit.role is not Role.UNKNOWN:
            raise ExtensionConformanceError(
                "community extractor unit role must be unknown"
            )
        if type(unit.structure) is not tuple:
            raise ExtensionConformanceError(
                "unit structure must be a concrete tuple"
            )
        for part in unit.structure:
            _conformance_text(part, "unit structure item")
        _conformance_number(
            unit.salience, "unit salience", minimum=0.0, maximum=1.0
        )
        _conformance_number(
            unit.confidence, "unit confidence", minimum=0.0, maximum=1.0
        )
        if type(unit.tokens) is not int or unit.tokens < 1:
            raise ExtensionConformanceError(
                "unit token estimate must be a positive concrete integer"
            )
        if type(unit.meta) is not dict:
            raise ExtensionConformanceError(
                "unit metadata must be a concrete dictionary"
            )
        _validate_json_object(unit.meta, "unit metadata")

        unit_id = unit.id
        if unit_id in units_by_id:
            raise ExtensionConformanceError(
                "extraction unit ids must be unique"
            )
        units_by_id[unit_id] = unit

    relation_keys: set[tuple[object, ...]] = set()
    for relation in value.relations:
        if type(relation) is not Relation:
            raise ExtensionConformanceError(
                "extraction relation must be a concrete Relation"
            )
        relation_source = _conformance_text(
            relation.src, "relation source endpoint", nonblank=True
        )
        relation_destination = _conformance_text(
            relation.dst, "relation destination endpoint", nonblank=True
        )
        if type(relation.kind) is not RelationKind:
            raise ExtensionConformanceError(
                "relation kind must be a concrete enum"
            )
        _conformance_text(relation.evidence, "relation evidence")
        _conformance_number(
            relation.confidence,
            "relation confidence",
            minimum=0.0,
            maximum=1.0,
        )
        relation_key = (
            relation_source,
            relation_destination,
            relation.kind,
            relation.evidence,
            relation.confidence,
        )
        if relation_key in relation_keys:
            raise ExtensionConformanceError(
                "extraction relations must not contain exact duplicates"
            )
        relation_keys.add(relation_key)
        if (
            relation_source not in units_by_id
            or relation_destination not in units_by_id
        ):
            raise ExtensionConformanceError(
                "extraction relation has a dangling endpoint"
            )

    gap_keys: set[tuple[object, ...]] = set()
    for gap in value.gaps:
        if type(gap) is not Gap:
            raise ExtensionConformanceError("extraction gap must be a concrete Gap")
        _conformance_text(gap.content, "gap content", nonblank=True)
        _validate_origin(gap.origin, label="gap origin", Origin=Origin)
        if type(gap.kind) is not GapKind:
            raise ExtensionConformanceError("gap kind must be a concrete enum")
        if kind != "collection" and gap.origin.source != source:
            raise ExtensionConformanceError(
                "non-collection gap source must match the extraction source"
            )
        gap_key = (gap.content, gap.origin, gap.kind)
        if gap_key in gap_keys:
            raise ExtensionConformanceError(
                "extraction gaps must not contain exact duplicates"
            )
        gap_keys.add(gap_key)

    statement_ids: set[str] = set()
    for statement in value.summary_claims:
        if type(statement) is not GroundedStatement:
            raise ExtensionConformanceError(
                "summary claim must be a concrete GroundedStatement"
            )
        _conformance_text(
            statement.content, "grounded statement content", nonblank=True
        )
        if type(statement.origins) is not tuple or not statement.origins:
            raise ExtensionConformanceError(
                "grounded statement origins must be a non-empty concrete tuple"
            )
        for origin in statement.origins:
            _validate_origin(
                origin, label="grounded statement origin", Origin=Origin
            )
        if len(set(statement.origins)) != len(statement.origins):
            raise ExtensionConformanceError(
                "grounded statement origins must be unique"
            )
        if (
            type(statement.evidence_unit_ids) is not tuple
            or not statement.evidence_unit_ids
        ):
            raise ExtensionConformanceError(
                "grounded statement evidence ids must be a non-empty concrete tuple"
            )
        evidence_ids: list[str] = []
        for evidence_id in statement.evidence_unit_ids:
            evidence_ids.append(
                _conformance_text(
                    evidence_id,
                    "grounded statement evidence id",
                    nonblank=True,
                )
            )
        if len(set(evidence_ids)) != len(evidence_ids):
            raise ExtensionConformanceError(
                "grounded statement evidence ids must be unique"
            )
        if any(evidence_id not in units_by_id for evidence_id in evidence_ids):
            raise ExtensionConformanceError(
                "grounded statement has unknown evidence"
            )
        evidence_origins: list[object] = []
        seen_evidence_origins: set[object] = set()
        for evidence_id in evidence_ids:
            evidence_origin = units_by_id[evidence_id].origin
            if evidence_origin not in seen_evidence_origins:
                seen_evidence_origins.add(evidence_origin)
                evidence_origins.append(evidence_origin)
        if statement.origins != tuple(evidence_origins):
            raise ExtensionConformanceError(
                "grounded statement origins must exactly match canonical evidence order"
            )
        statement_id = statement.id
        if statement_id in statement_ids:
            raise ExtensionConformanceError(
                "grounded statement ids must be unique"
            )
        statement_ids.add(statement_id)

    if not value.units and not value.gaps:
        raise ExtensionConformanceError(
            "extension extraction must not return an empty success"
        )
    _validate_input_manifest_claims(value)
    return value


def validate_extraction_output(value: object):
    """Validate the complete untrusted IR without core imports at startup."""

    from .unit import (
        Extraction,
        Gap,
        GapKind,
        GroundedStatement,
        Modality,
        Origin,
        Relation,
        RelationKind,
        Role,
        Unit,
        _GapList,
    )

    try:
        return _validate_extraction(
            value,
            Extraction=Extraction,
            Gap=Gap,
            GapKind=GapKind,
            GroundedStatement=GroundedStatement,
            Modality=Modality,
            Origin=Origin,
            Relation=Relation,
            RelationKind=RelationKind,
            Role=Role,
            Unit=Unit,
            _GapList=_GapList,
        )
    except ExtensionConformanceError:
        raise
    except Exception:
        raise ExtensionConformanceError(
            "extractor result could not be validated safely"
        ) from None


def _manifest_origin_record(origin: object) -> dict[str, object]:
    record: dict[str, object] = {
        "source": origin.source,
        "ref": origin.ref,
    }
    if origin.char_span is not None:
        record["char_span"] = list(origin.char_span)
    return record


def _canonical_json_text(value: object) -> str:
    """Serialize an already validated JSON tree for deterministic comparison."""

    import json

    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _decline_canonical_key(decline: object) -> tuple[object, ...]:
    span = decline.origin.char_span
    span_key = (0, -1, -1) if span is None else (1, span[0], span[1])
    detected_key = (
        (0, "")
        if decline.detected_kind is None
        else (1, decline.detected_kind)
    )
    tier_key = (0, -1) if decline.tier is None else (1, decline.tier)
    return (
        decline.source,
        str(decline.kind),
        decline.content,
        decline.origin.ref,
        span_key,
        detected_key,
        tier_key,
        _canonical_json_text(decline.details),
    )


def _validate_acquisition_manifest(
    manifest: dict[str, object],
    *,
    source: str,
    kind: str,
    extractions: tuple[object, ...],
    declines: tuple[object, ...],
) -> None:
    """Validate optional recognizable manifest claims as exact commitments."""

    import hashlib
    import json
    import re

    _validate_json_object(manifest, "collection acquisition manifest")
    if "source" in manifest and manifest["source"] != source:
        raise ExtensionConformanceError(
            "acquisition manifest source does not match the result"
        )
    if "kind" in manifest and manifest["kind"] != kind:
        raise ExtensionConformanceError(
            "acquisition manifest kind does not match the result"
        )
    container = manifest.get("container")
    if container is not None:
        if type(container) is not dict:
            raise ExtensionConformanceError(
                "acquisition container claim must be a concrete dictionary"
            )
        if "source" in container and container["source"] != source:
            raise ExtensionConformanceError(
                "acquisition container source does not match the result"
            )
        if "kind" in container and container["kind"] != kind:
            raise ExtensionConformanceError(
                "acquisition container kind does not match the result"
            )
    if "admitted_bytes" in manifest and (
        type(manifest["admitted_bytes"]) is not int
        or manifest["admitted_bytes"] < 0
    ):
        raise ExtensionConformanceError(
            "acquisition admitted byte count must be a non-negative integer"
        )

    members = manifest.get("members")
    ignored_records = 0
    if members is not None:
        if type(members) is not list:
            raise ExtensionConformanceError(
                "acquisition members claim must be a concrete list"
            )
        if members and all(type(item) is str for item in members):
            member_sources = [
                _conformance_text(
                    item, "acquisition member source", nonempty=True
                )
                for item in members
            ]
            if len(set(member_sources)) != len(member_sources):
                raise ExtensionConformanceError(
                    "acquisition member source claims must be unique"
                )
            known_sources = {
                *(item.source for item in extractions),
                *(item.source for item in declines),
            }
            if (
                len(member_sources) != len(extractions) + len(declines)
                or set(member_sources) != known_sources
            ):
                raise ExtensionConformanceError(
                    "acquisition manifest member sources do not match the result"
                )
        elif all(type(item) is dict for item in members):
            extracted_by_source = {item.source: item for item in extractions}
            seen_extracted: set[str] = set()
            unmatched_declines = list(declines)
            seen_records: set[str] = set()
            ordered = any("order" in record for record in members)
            for index, record in enumerate(members):
                if ordered and (
                    type(record.get("order")) is not int
                    or record["order"] != index
                ):
                    raise ExtensionConformanceError(
                        "acquisition manifest record order is not canonical"
                    )
                status = record.get("status")
                if type(status) is not str or status not in {
                    "container",
                    "declined",
                    "extracted",
                    "ignored",
                }:
                    raise ExtensionConformanceError(
                        "acquisition manifest record has an unknown or missing status"
                    )
                semantic_record = {
                    key: item for key, item in record.items() if key != "order"
                }
                record_key = _canonical_json_text(semantic_record)
                if record_key in seen_records:
                    raise ExtensionConformanceError(
                        "acquisition manifest records must not contain exact duplicates"
                    )
                seen_records.add(record_key)
                if status == "ignored":
                    _conformance_text(
                        record.get("source"),
                        "ignored acquisition member source",
                        nonempty=True,
                    )
                    _conformance_text(
                        record.get("reason"),
                        "ignored acquisition member reason",
                        nonblank=True,
                    )
                    for optional_source in ("acquired_source", "referrer"):
                        if optional_source in record:
                            _conformance_text(
                                record[optional_source],
                                "ignored acquisition member reference",
                                nonempty=True,
                            )
                    ignored_records += 1
                    continue
                if status == "container":
                    _conformance_text(
                        record.get("source"),
                        "acquisition container record source",
                        nonempty=True,
                    )
                    _conformance_text(
                        record.get("kind"),
                        "acquisition container record kind",
                        nonblank=True,
                    )
                    if "bytes" in record and (
                        type(record["bytes"]) is not int or record["bytes"] < 0
                    ):
                        raise ExtensionConformanceError(
                            "acquisition container byte count must be non-negative"
                        )
                    if "sha256" in record and (
                        type(record["sha256"]) is not str
                        or re.fullmatch(r"[0-9a-f]{64}", record["sha256"])
                        is None
                    ):
                        raise ExtensionConformanceError(
                            "acquisition container SHA-256 must be lowercase hex"
                        )
                    continue
                if status == "extracted":
                    record_source = record.get("source")
                    if (
                        type(record_source) is not str
                        or record_source not in extracted_by_source
                        or record_source in seen_extracted
                    ):
                        raise ExtensionConformanceError(
                            "acquisition manifest has an invalid extracted record"
                        )
                    extraction = extracted_by_source[record_source]
                    seen_extracted.add(record_source)
                    claims = {
                        "kind": extraction.kind,
                        "units": len(extraction.units),
                        "relations": len(extraction.relations),
                        "gaps": len(extraction.gaps),
                    }
                    if any(
                        key in record and record[key] != expected
                        for key, expected in claims.items()
                    ):
                        raise ExtensionConformanceError(
                            "acquisition extracted record does not match its leaf"
                        )
                    continue
                if status == "declined":
                    matched_index: int | None = None
                    for candidate_index, decline in enumerate(unmatched_declines):
                        claims = {
                            "source": decline.source,
                            "origin": _manifest_origin_record(decline.origin),
                            "decline_kind": str(decline.kind),
                            "content": decline.content,
                            "detected_kind": decline.detected_kind,
                            "tier": decline.tier,
                            "details": decline.details,
                        }
                        required = (
                            "source",
                            "origin",
                            "decline_kind",
                            "content",
                        )
                        if any(record.get(key) != claims[key] for key in required):
                            continue
                        if any(
                            key in record and record[key] != expected
                            for key, expected in claims.items()
                            if key not in required
                        ):
                            continue
                        matched_index = candidate_index
                        break
                    if matched_index is None:
                        raise ExtensionConformanceError(
                            "acquisition manifest has an invalid declined record"
                        )
                    unmatched_declines.pop(matched_index)
            if seen_extracted != set(extracted_by_source):
                raise ExtensionConformanceError(
                    "acquisition manifest omits an extracted leaf record"
                )
            if unmatched_declines:
                raise ExtensionConformanceError(
                    "acquisition manifest omits a declined member record"
                )
        elif members:
            raise ExtensionConformanceError(
                "acquisition members must use one concrete record shape"
            )

    counts = manifest.get("counts")
    if counts is not None:
        if type(counts) is not dict:
            raise ExtensionConformanceError(
                "acquisition counts claim must be a concrete dictionary"
            )
        expected_counts: dict[str, int] = {
            "extracted": len(extractions),
            "declined": len(declines),
        }
        if members is not None:
            expected_counts["records"] = len(members)
            expected_counts["ignored"] = ignored_records
        for count_name, expected in expected_counts.items():
            if count_name in counts and (
                type(counts[count_name]) is not int
                or counts[count_name] != expected
            ):
                raise ExtensionConformanceError(
                    "acquisition counts claim does not match the result"
                )

    digest = manifest.get("sha256")
    if digest is not None:
        if (
            type(digest) is not str
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            raise ExtensionConformanceError(
                "acquisition manifest SHA-256 must be 64 lowercase hex characters"
            )
        unsigned = dict(manifest)
        unsigned.pop("sha256")
        canonical = json.dumps(
            unsigned,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8", errors="strict")
        if hashlib.sha256(canonical).hexdigest() != digest:
            raise ExtensionConformanceError(
                "acquisition manifest SHA-256 does not match its records"
            )



def validate_acquisition_output(value: object):
    """Validate a complete community collection result through lazy imports."""

    from .collection import (
        CollectionAcquisition,
        DeclineKind,
        MemberDecline,
    )
    from .unit import (
        Extraction,
        Gap,
        GapKind,
        GroundedStatement,
        Modality,
        Origin,
        Relation,
        RelationKind,
        Role,
        Unit,
        _GapList,
    )

    try:
        if type(value) is not CollectionAcquisition:
            raise ExtensionConformanceError(
                "acquisition result must be a concrete "
                "autotldr.collection.CollectionAcquisition"
            )
        source = _conformance_text(
            value.source, "collection acquisition source", nonempty=True
        )
        kind = _conformance_text(
            value.kind, "collection acquisition kind", nonblank=True
        )
        if type(value.extractions) is not tuple:
            raise ExtensionConformanceError(
                "collection extractions must be a concrete tuple"
            )
        if type(value.declines) is not tuple:
            raise ExtensionConformanceError(
                "collection declines must be a concrete tuple"
            )
        if type(value.manifest) is not dict:
            raise ExtensionConformanceError(
                "collection manifest must be a concrete dictionary"
            )

        extraction_sources: list[str] = []
        for extraction in value.extractions:
            checked = _validate_extraction(
                extraction,
                Extraction=Extraction,
                Gap=Gap,
                GapKind=GapKind,
                GroundedStatement=GroundedStatement,
                Modality=Modality,
                Origin=Origin,
                Relation=Relation,
                RelationKind=RelationKind,
                Role=Role,
                Unit=Unit,
                _GapList=_GapList,
            )
            extraction_sources.append(checked.source)
        if extraction_sources != sorted(extraction_sources):
            raise ExtensionConformanceError(
                "collection extraction sources must be sorted"
            )
        if len(extraction_sources) != len(set(extraction_sources)):
            raise ExtensionConformanceError(
                "collection extraction sources must be unique"
            )

        decline_keys: list[tuple[object, ...]] = []
        for decline in value.declines:
            if type(decline) is not MemberDecline:
                raise ExtensionConformanceError(
                    "collection decline must be a concrete MemberDecline"
                )
            if type(decline.kind) is not DeclineKind:
                raise ExtensionConformanceError(
                    "collection decline kind must be a concrete enum"
                )
            _conformance_text(
                decline.content, "collection decline content", nonblank=True
            )
            _validate_origin(
                decline.origin, label="collection decline origin", Origin=Origin
            )
            if decline.detected_kind is not None:
                _conformance_text(
                    decline.detected_kind,
                    "collection decline detected kind",
                    nonblank=True,
                )
            if decline.tier is not None and (
                type(decline.tier) is not int or decline.tier < 0
            ):
                raise ExtensionConformanceError(
                    "collection decline tier must be a non-negative integer"
                )
            if type(decline.details) is not dict:
                raise ExtensionConformanceError(
                    "collection decline details must be a concrete dictionary"
                )
            _validate_json_object(decline.details, "collection decline details")
            decline_keys.append(_decline_canonical_key(decline))

        if len(decline_keys) != len(set(decline_keys)):
            raise ExtensionConformanceError(
                "collection declines must not contain exact duplicates"
            )
        if decline_keys != sorted(decline_keys):
            raise ExtensionConformanceError(
                "collection declines must be in deterministic canonical order"
            )

        if not value.extractions and not value.declines:
            raise ExtensionConformanceError(
                "extension acquisition must not return an empty success"
            )
        _validate_acquisition_manifest(
            value.manifest,
            source=source,
            kind=kind,
            extractions=value.extractions,
            declines=value.declines,
        )
        return value
    except ExtensionConformanceError:
        raise
    except Exception:
        raise ExtensionConformanceError(
            "acquisition result could not be validated safely"
        ) from None


def validate_renderer_output(value: object) -> str:
    """Require canonical renderer text so the core can count UTF-8 exactly."""

    if type(value) is not str:
        raise ExtensionConformanceError(
            "renderer builder result must be canonical text, not bytes"
        )
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise ExtensionConformanceError(
            "renderer builder result must be valid canonical UTF-8 text"
        ) from None
    return value


def load_extension(
    reference: str, registry: ExtensionRegistry
) -> tuple[ExtensionSpec, ...]:
    """Explicitly load and atomically register ``module[:factory]`` metadata.

    The factory receives no arguments and returns either one specification or
    a non-empty list/tuple of specifications.  Import and factory exception
    details are intentionally scrubbed: third-party exception strings may
    contain source contents, credentials, or local paths and do not belong on
    the CLI error channel.
    """

    if type(registry) is not ExtensionRegistry:
        raise ExtensionValidationError("registry must be an ExtensionRegistry")
    module_name, factory_name = _parse_extension_reference(reference)

    try:
        module = importlib.import_module(module_name)
    except Exception:
        raise ExtensionLoadError(
            f"could not import explicitly requested extension {module_name!r}"
        ) from None
    try:
        factory: object = module
        for component in factory_name.split("."):
            factory = getattr(factory, component)
    except Exception:
        raise ExtensionLoadError(
            f"extension {module_name!r} does not expose factory {factory_name!r}"
        ) from None

    try:
        checked_factory = _validate_callable_arity(
            factory, positional=0, label="extension factory"
        )
        raw_specs = checked_factory()
    except Exception:
        raise ExtensionLoadError(
            f"extension factory {module_name!r}:{factory_name} failed"
        ) from None

    try:
        if type(raw_specs) in (ExtractorSpec, AcquisitionSpec, RendererSpec):
            specs: tuple[ExtensionSpec, ...] = (raw_specs,)
        elif type(raw_specs) in (list, tuple) and raw_specs:
            specs = tuple(raw_specs)
        else:
            raise ExtensionLoadError(
                f"extension factory {module_name!r}:{factory_name} returned an "
                "unsupported specification collection"
            )
    except ExtensionLoadError:
        raise
    except Exception:
        raise ExtensionLoadError(
            f"extension factory {module_name!r}:{factory_name} returned an "
            "unreadable specification collection"
        ) from None
    if any(
        type(spec) not in (ExtractorSpec, AcquisitionSpec, RendererSpec)
        for spec in specs
    ):
        raise ExtensionLoadError(
            f"extension factory {module_name!r}:{factory_name} returned an "
            "unsupported specification item"
        )

    # register_many stages all indexes before publishing them, so a conflict in
    # the last factory item cannot leave the first item partially registered.
    registry.register_many(specs)
    return tuple(sorted(specs, key=lambda spec: (_spec_category(spec), spec.name)))


def _parse_extension_reference(reference: object) -> tuple[str, str]:
    if not isinstance(reference, str) or not reference or reference.count(":") > 1:
        raise ExtensionValidationError(
            "extension reference must be module[:factory]"
        )
    module_name, separator, factory_name = reference.partition(":")
    module_name = _dotted(module_name, "extension module")
    factory_name = _dotted(
        factory_name if separator else DEFAULT_EXTENSION_FACTORY,
        "extension factory",
    )
    return module_name, factory_name


__all__ = [
    "ACQUISITION_CALL_CONTRACT",
    "CAPABILITY_MANIFEST_SCHEMA",
    "DEFAULT_EXTENSION_FACTORY",
    "EXTENSION_API_VERSION",
    "EXTRACTOR_CALL_CONTRACT",
    "RENDERER_BUILDER_CONTRACT",
    "AcquisitionSpec",
    "ExtensionCollisionError",
    "ExtensionConformanceError",
    "ExtensionError",
    "ExtensionLoadError",
    "ExtensionRegistry",
    "ExtensionSpec",
    "ExtensionValidationError",
    "ExtractorSpec",
    "RegistrySnapshot",
    "RendererSpec",
    "SignatureProbe",
    "UnknownExtensionError",
    "load_extension",
    "validate_acquisition_callable",
    "validate_acquisition_output",
    "validate_extraction_output",
    "validate_extractor_callable",
    "validate_renderer_callable",
    "validate_renderer_output",
]
