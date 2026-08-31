"""First-user product profiles and dependency-free configuration.

This module is intentionally standard-library-only.  The CLI imports it only after a
product command or an ordinary invocation has been parsed, so configuration does not
pull a parser or model runtime into the cold-start import graph.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


CONFIG_VERSION = 1
DEFAULT_LOCAL_ENDPOINT = "http://127.0.0.1:1234"
DETAIL_NAMES = ("brief", "standard", "deep")


class ProductConfigError(ValueError):
    """A persisted or requested product setting is invalid."""


class LocalModelUnavailable(ProductConfigError):
    """The configured local endpoint cannot provide the requested model."""


@dataclass(frozen=True, slots=True)
class DetailProfile:
    """Versioned expansion of one user-facing detail choice."""

    name: str
    evidence_budget_bytes: int
    max_claims: int
    max_output_tokens: int
    timeout_seconds: float
    include_supporting_units: bool
    reasoning_effort: str | None

    def as_manifest(self) -> dict[str, object]:
        return {
            "schema": "autotldr-detail-profile-v1",
            "name": self.name,
            "evidence_budget_bytes": self.evidence_budget_bytes,
            "max_claims": self.max_claims,
            "max_output_tokens": self.max_output_tokens,
            "timeout_seconds": self.timeout_seconds,
            "include_supporting_units": self.include_supporting_units,
            "reasoning_effort": self.reasoning_effort,
        }


DETAIL_PROFILES: Mapping[str, DetailProfile] = {
    "brief": DetailProfile("brief", 8_000, 2, 512, 60.0, False, "none"),
    "standard": DetailProfile("standard", 24_000, 4, 1_024, 90.0, True, "none"),
    "deep": DetailProfile("deep", 48_000, 6, 1_800, 120.0, True, "none"),
}


@dataclass(frozen=True, slots=True)
class ModelProfile:
    """One explicit local model identity used by ordinary invocation."""

    endpoint: str
    model: str
    timeout_seconds: float = 120.0
    runtime: str = "lm-studio"

    def __post_init__(self) -> None:
        endpoint = _local_endpoint(self.endpoint)
        object.__setattr__(self, "endpoint", endpoint)
        if (
            type(self.model) is not str
            or not self.model
            or self.model.strip() != self.model
            or any(ord(character) < 32 for character in self.model)
        ):
            raise ProductConfigError("model must be one non-empty, unpadded identifier")
        try:
            self.model.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise ProductConfigError("model must be valid UTF-8") from exc
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not 0 < float(self.timeout_seconds) <= 300
        ):
            raise ProductConfigError("model timeout_seconds must be in (0, 300]")
        if self.runtime != "lm-studio":
            raise ProductConfigError(
                "alpha runtime must be 'lm-studio'; other local transports are not yet certified"
            )

    def as_manifest(self) -> dict[str, object]:
        return {
            "endpoint": self.endpoint,
            "model": self.model,
            "timeout_seconds": float(self.timeout_seconds),
            "runtime": self.runtime,
        }


@dataclass(frozen=True, slots=True)
class RuntimeDiscovery:
    """Catalog and active-residency facts reported by one local runtime."""

    provider: str
    endpoint: str
    catalog_models: tuple[str, ...]
    active_models: tuple[str, ...]
    active_state_verified: bool

    def as_manifest(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "endpoint": self.endpoint,
            "catalog_models": list(self.catalog_models),
            "active_models": list(self.active_models),
            "active_state_verified": self.active_state_verified,
        }


@dataclass(frozen=True, slots=True)
class ProductConfig:
    """Resolved user and project preferences for one invocation."""

    detail: str = "standard"
    allow_evidence_fallback: bool = False
    model: ModelProfile | None = None
    extensions: tuple[str, ...] = ()
    sources: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.detail not in DETAIL_PROFILES:
            raise ProductConfigError(
                f"detail must be one of {', '.join(DETAIL_NAMES)}"
            )
        if not isinstance(self.allow_evidence_fallback, bool):
            raise ProductConfigError("allow_evidence_fallback must be a boolean")
        if self.model is not None and type(self.model) is not ModelProfile:
            raise ProductConfigError("model profile is invalid")
        if type(self.extensions) is not tuple or any(
            type(item) is not str or not item or item.strip() != item
            for item in self.extensions
        ):
            raise ProductConfigError("extensions must be an immutable list of imports")
        if len(set(self.extensions)) != len(self.extensions):
            raise ProductConfigError("extensions must not contain duplicates")

    @property
    def detail_profile(self) -> DetailProfile:
        return DETAIL_PROFILES[self.detail]

    def as_manifest(self) -> dict[str, object]:
        return {
            "schema": "autotldr-resolved-product-config-v1",
            "detail": self.detail_profile.as_manifest(),
            "allow_evidence_fallback": self.allow_evidence_fallback,
            "model": None if self.model is None else self.model.as_manifest(),
            "extensions": list(self.extensions),
            "sources": list(self.sources),
        }


def user_config_path() -> Path:
    """Return the one explicit user configuration path."""

    override = os.environ.get("AUTOTLDR_CONFIG")
    if override:
        return Path(override).expanduser()
    root = os.environ.get("XDG_CONFIG_HOME")
    base = Path(root).expanduser() if root else Path.home() / ".config"
    return base / "autotldr" / "config.toml"


def project_config_path(directory: Path | None = None) -> Path:
    """Return the project config in the invocation directory.

    Alpha deliberately does not walk arbitrary parents.  The file that affects a run is
    visible in the working directory, and its exact path enters the resolved manifest.
    """

    return (directory or Path.cwd()) / ".autotldr.toml"


def load_product_config(
    *,
    use_config: bool = True,
    directory: Path | None = None,
) -> ProductConfig:
    """Load user then project TOML under a closed, deterministic schema."""

    if not use_config:
        return ProductConfig()
    merged: dict[str, Any] = {}
    sources: list[str] = []
    for path in (user_config_path(), project_config_path(directory)):
        if not path.exists():
            continue
        data = _read_config(path)
        _merge_config(merged, data)
        sources.append(str(path.resolve()))
    return _config_from_mapping(merged, sources=tuple(sources))


def write_user_model_config(
    profile: ModelProfile,
    *,
    detail: str = "standard",
    force: bool = False,
) -> Path:
    """Write the small user configuration produced by ``autotldr setup``."""

    if detail not in DETAIL_PROFILES:
        raise ProductConfigError(
            f"detail must be one of {', '.join(DETAIL_NAMES)}"
        )
    path = user_config_path()
    import json

    if path.exists() and not force:
        raise ProductConfigError(f"configuration already exists at {path}; use --force")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        f"version = {CONFIG_VERSION}\n\n"
        "[defaults]\n"
        f'detail = "{detail}"\n'
        "allow_evidence_fallback = false\n\n"
        "[model]\n"
        f'endpoint = {json.dumps(profile.endpoint)}\n'
        f'name = {json.dumps(profile.model)}\n'
        f"timeout_seconds = {float(profile.timeout_seconds):g}\n"
        f'runtime = {json.dumps(profile.runtime)}\n'
    )
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return path


def discover_local_runtime(
    endpoint: str = DEFAULT_LOCAL_ENDPOINT,
    *,
    timeout_seconds: float = 2.0,
) -> RuntimeDiscovery:
    """Separate catalog availability from actively loaded model instances."""

    import urllib.error

    normalized = _local_endpoint(endpoint)
    catalog_url = normalized.rstrip("/") + (
        "/models" if normalized.endswith("/v1") else "/v1/models"
    )
    document = _read_runtime_json(
        catalog_url,
        timeout_seconds=timeout_seconds,
        maximum_bytes=256 * 1024,
        label="local model catalog",
    )
    if type(document) is not dict or set(document) - {"object", "data"}:
        raise LocalModelUnavailable("local model catalog has an unsupported envelope")
    rows = document.get("data")
    if type(rows) is not list:
        raise LocalModelUnavailable("local model catalog has no data array")
    models: list[str] = []
    for row in rows:
        if type(row) is not dict or type(row.get("id")) is not str:
            raise LocalModelUnavailable("local model catalog contains an invalid row")
        identifier = row["id"]
        if not identifier or identifier.strip() != identifier:
            raise LocalModelUnavailable("local model catalog contains an invalid model ID")
        models.append(identifier)
    if len(models) != len(set(models)):
        raise LocalModelUnavailable("local model catalog contains duplicate model IDs")
    catalog_models = tuple(sorted(models))

    root = normalized[:-3] if normalized.endswith("/v1") else normalized
    lm_studio_url = root.rstrip("/") + "/api/v1/models"
    try:
        runtime_document = _read_runtime_json(
            lm_studio_url,
            timeout_seconds=timeout_seconds,
            maximum_bytes=2 * 1024 * 1024,
            label="LM Studio runtime inventory",
        )
    except LocalModelUnavailable as exc:
        cause = exc.__cause__
        if not isinstance(cause, urllib.error.HTTPError) or cause.code != 404:
            # A valid OpenAI catalog without the LM Studio management surface is a real
            # transport, but it cannot prove that a request will not trigger auto-load.
            return RuntimeDiscovery(
                "openai-compatible-unverified",
                normalized,
                catalog_models,
                (),
                False,
            )
        return RuntimeDiscovery(
            "openai-compatible-unverified",
            normalized,
            catalog_models,
            (),
            False,
        )
    active_models = _lm_studio_active_models(runtime_document)
    unknown_active = sorted(set(active_models) - set(catalog_models))
    if unknown_active:
        raise LocalModelUnavailable(
            f"LM Studio reports active model {unknown_active[0]!r} outside its OpenAI catalog"
        )
    return RuntimeDiscovery(
        "lm-studio",
        normalized,
        catalog_models,
        active_models,
        True,
    )


def discover_served_models(
    endpoint: str = DEFAULT_LOCAL_ENDPOINT,
    *,
    timeout_seconds: float = 2.0,
) -> tuple[str, ...]:
    """Return only model IDs proven active, never the downloaded catalog."""

    discovery = discover_local_runtime(endpoint, timeout_seconds=timeout_seconds)
    if not discovery.active_state_verified:
        raise LocalModelUnavailable(
            "the endpoint does not expose verified active-model state; alpha setup "
            "currently supports LM Studio's local runtime API"
        )
    return discovery.active_models


def require_active_model(profile: ModelProfile) -> RuntimeDiscovery:
    """Fail before source acquisition unless the exact configured model is active."""

    discovery = discover_local_runtime(profile.endpoint)
    if discovery.provider != profile.runtime or not discovery.active_state_verified:
        raise LocalModelUnavailable(
            f"configured runtime {profile.runtime!r} cannot prove active model state at "
            f"{profile.endpoint}"
        )
    if profile.model not in discovery.active_models:
        active = ", ".join(discovery.active_models) or "none"
        raise LocalModelUnavailable(
            f"configured model {profile.model!r} is not active; active models: {active}"
        )
    return discovery


def require_configured_model(config: ProductConfig) -> ModelProfile:
    if config.model is None:
        raise LocalModelUnavailable(
            "no local model is configured; run `autotldr setup` and then retry"
        )
    return config.model


def probe_model_profile(profile: ModelProfile) -> dict[str, object]:
    """Run one bounded, source-free grounding probe against an active model.

    Catalog presence alone does not prove that a runtime accepts AutoTLDR's request or
    returns the strict response envelope.  The probe uses synthetic public text, invokes
    no model lifecycle operation, and returns only conformance metadata.
    """

    require_active_model(profile)
    from .api import assemble_collection
    from .router import extract_stdin
    from .synthesis import EndpointPolicy, SynthesisConfig, synthesize

    leaf = extract_stdin(
        b"# AutoTLDR doctor\n\nThe diagnostic fixture states that the probe is local.\n",
        kind="markdown",
    )
    collection = assemble_collection((leaf,), subject="<autotldr-doctor>")
    result = synthesize(
        collection,
        SynthesisConfig(
            model=profile.model,
            endpoint=profile.endpoint,
            endpoint_policy=EndpointPolicy(),
            evidence_budget_bytes=4_096,
            timeout_seconds=min(float(profile.timeout_seconds), 30.0),
            max_output_tokens=DETAIL_PROFILES["brief"].max_output_tokens,
            max_claims=1,
            reasoning_effort=DETAIL_PROFILES["brief"].reasoning_effort,
            product_detail="brief",
            include_findings=False,
            fallback_on_failure=False,
        ),
    )
    return {
        "model": profile.model,
        "endpoint": profile.endpoint,
        "claims": len(result.extraction.summary_claims),
        "outcome": "accepted",
    }


def _read_config(path: Path) -> dict[str, Any]:
    import tomllib

    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ProductConfigError(f"cannot read configuration {path}: {exc}") from exc
    if len(payload) > 256 * 1024:
        raise ProductConfigError(f"configuration {path} exceeds 256 KiB")
    try:
        data = tomllib.loads(payload.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ProductConfigError(f"invalid configuration {path}: {exc}") from exc
    if type(data) is not dict:
        raise ProductConfigError(f"configuration {path} must be a TOML table")
    return data


def _merge_config(target: dict[str, Any], update: Mapping[str, Any]) -> None:
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge_config(target[key], value)
        else:
            target[key] = value


def _config_from_mapping(
    data: Mapping[str, Any], *, sources: tuple[str, ...]
) -> ProductConfig:
    allowed = {"version", "defaults", "model", "extensions"}
    if extras := sorted(set(data) - allowed):
        raise ProductConfigError(f"unknown configuration key: {extras[0]}")
    version = data.get("version", CONFIG_VERSION)
    if type(version) is not int or version != CONFIG_VERSION:
        raise ProductConfigError(f"configuration version must be {CONFIG_VERSION}")

    defaults = data.get("defaults", {})
    if type(defaults) is not dict:
        raise ProductConfigError("defaults must be a TOML table")
    if extras := sorted(set(defaults) - {"detail", "allow_evidence_fallback"}):
        raise ProductConfigError(f"unknown defaults key: {extras[0]}")
    detail = defaults.get("detail", "standard")
    fallback = defaults.get("allow_evidence_fallback", False)

    model_data = data.get("model")
    model = None
    if model_data is not None:
        if type(model_data) is not dict:
            raise ProductConfigError("model must be a TOML table")
        if extras := sorted(
            set(model_data) - {"endpoint", "name", "timeout_seconds", "runtime"}
        ):
            raise ProductConfigError(f"unknown model key: {extras[0]}")
        if "endpoint" not in model_data or "name" not in model_data:
            raise ProductConfigError("model.endpoint and model.name are required")
        model = ModelProfile(
            endpoint=model_data["endpoint"],
            model=model_data["name"],
            timeout_seconds=model_data.get("timeout_seconds", 120.0),
            runtime=model_data.get("runtime", "lm-studio"),
        )

    extensions_data = data.get("extensions", {})
    if type(extensions_data) is not dict:
        raise ProductConfigError("extensions must be a TOML table")
    if extras := sorted(set(extensions_data) - {"imports"}):
        raise ProductConfigError(f"unknown extensions key: {extras[0]}")
    imports = extensions_data.get("imports", [])
    if type(imports) is not list:
        raise ProductConfigError("extensions.imports must be an array")
    return ProductConfig(
        detail=detail,
        allow_evidence_fallback=fallback,
        model=model,
        extensions=tuple(imports),
        sources=sources,
    )


def _local_endpoint(value: object) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise ProductConfigError("model endpoint must be a non-empty URL")
    if value != DEFAULT_LOCAL_ENDPOINT:
        raise ProductConfigError(
            f"alpha model endpoint must be exactly {DEFAULT_LOCAL_ENDPOINT}; "
            "other local endpoints are not yet certified"
        )
    return value


def _read_runtime_json(
    url: str,
    *,
    timeout_seconds: float,
    maximum_bytes: int,
    label: str,
) -> object:
    import json
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "AutoTLDR/setup"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            if response.status != 200:
                raise LocalModelUnavailable(
                    f"{label} returned HTTP {response.status}"
                )
            payload = response.read(maximum_bytes + 1)
    except (OSError, urllib.error.URLError) as exc:
        raise LocalModelUnavailable(f"{label} did not respond at {url}") from exc
    if len(payload) > maximum_bytes:
        raise LocalModelUnavailable(f"{label} exceeded {maximum_bytes} bytes")
    try:
        return json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LocalModelUnavailable(f"{label} was not valid UTF-8 JSON") from exc


def _lm_studio_active_models(document: object) -> tuple[str, ...]:
    if type(document) is not dict or type(document.get("models")) is not list:
        raise LocalModelUnavailable("LM Studio runtime inventory has an unsupported envelope")
    active: list[str] = []
    for row in document["models"]:
        if type(row) is not dict:
            raise LocalModelUnavailable("LM Studio runtime inventory contains an invalid row")
        key = row.get("key")
        model_type = row.get("type")
        instances = row.get("loaded_instances")
        if type(key) is not str or type(model_type) is not str or type(instances) is not list:
            raise LocalModelUnavailable("LM Studio runtime inventory contains an invalid model")
        if model_type != "llm":
            continue
        for instance in instances:
            if type(instance) is not dict or type(instance.get("id")) is not str:
                raise LocalModelUnavailable(
                    "LM Studio runtime inventory contains an invalid loaded instance"
                )
            identifier = instance["id"]
            if not identifier or identifier.strip() != identifier:
                raise LocalModelUnavailable(
                    "LM Studio runtime inventory contains an invalid loaded model ID"
                )
            active.append(identifier)
    if len(active) != len(set(active)):
        raise LocalModelUnavailable("LM Studio reports duplicate loaded model IDs")
    return tuple(sorted(active))


__all__ = [
    "CONFIG_VERSION",
    "DEFAULT_LOCAL_ENDPOINT",
    "DETAIL_NAMES",
    "DETAIL_PROFILES",
    "DetailProfile",
    "LocalModelUnavailable",
    "ModelProfile",
    "ProductConfig",
    "ProductConfigError",
    "RuntimeDiscovery",
    "discover_local_runtime",
    "discover_served_models",
    "load_product_config",
    "project_config_path",
    "probe_model_profile",
    "require_configured_model",
    "require_active_model",
    "user_config_path",
    "write_user_model_config",
]
