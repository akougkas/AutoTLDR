#!/usr/bin/env python3
"""Run the Stage 2 local-model pilot with an exclusive LM Studio lifecycle.

This runner owns *model residency*, while :mod:`evaluate` owns benchmark
requests and prediction artifacts.  The separation is intentional: lifecycle
code never receives a labels path and never reads predictions.

The operational contract is deliberately conservative:

* only the LM Studio instance on 127.0.0.1 is used;
* LM Link rows are observed but never loaded or unloaded;
* no local embedding model may be resident;
* exactly one local LLM is loaded for each candidate;
* every load is preceded by a 100% GPU-offload estimate; and
* every estimate, load, and unload is routed through a verified ZBook
  preference transaction; and
* a candidate is unloaded and zero local residents are verified even when the
  evaluator fails.

Use ``--dry-run`` to print the invariant-bearing command plan without running
any command.  The command executor is also injectable for fully offline tests.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence, TypeVar


HERE = Path(__file__).resolve().parent
DEFAULT_ITEMS = HERE / "pilot" / "items.jsonl"
DEFAULT_PROMPT = HERE / "prompt.md"
DEFAULT_POLICY = HERE / "policy.json"
DEFAULT_OUTPUT_DIR = HERE / "pilot" / "predictions"
EVALUATOR = HERE / "evaluate.py"

LOCAL_BASE_URL = "http://127.0.0.1:1234/v1"
AUTOTLDR_PREFIX = "autotldr-"
CONTEXT_LENGTH = 8192
PARALLEL = 4
GPU_MODE = "max"
GPU_OFFLOAD_LINE = "GPU Offload: 100%"
DEFAULT_LMS_EXECUTABLE = os.environ.get("AUTOTLDR_LMS_CLI", "lms")
TIMEOUT_RECORD_SCHEMA = "autotldr-lifecycle-timeout-v1"
ATTESTATION_SCHEMA = "autotldr-zbook-residency-attestation-v1"
ATTESTATION_FAILURE_SCHEMA = "autotldr-zbook-residency-ineligible-v1"
RUNTIME_CONFIG_SCHEMA = "autotldr-incumbent-runtime-config-v1"
CANDIDATE_RECOVERY_SCHEMA = "autotldr-candidate-recovery-v1"
_MAX_DIAGNOSTIC_CHARS = 240
_SECRET_OPTIONS = frozenset(
    {
        "--api-key",
        "--authorization",
        "--password",
        "--secret",
        "--token",
    }
)

JsonObject = dict[str, Any]
ResultT = TypeVar("ResultT")


class LifecycleError(RuntimeError):
    """The requested lifecycle would violate the local-only safety contract."""


class ResidencyAttestationError(LifecycleError):
    """Actual full-GPU residency could not be proven for an exact resident."""

    def __init__(
        self,
        message: str,
        *,
        fingerprint_sha256: str | None = None,
        phase: str | None = None,
    ) -> None:
        detail = _bounded_text(message)
        self.record: JsonObject = {
            "schema": ATTESTATION_FAILURE_SCHEMA,
            "eligible": False,
            "error_class": self.__class__.__name__,
            "phase": _bounded_text(phase) if phase else None,
            "resident_fingerprint_sha256": fingerprint_sha256,
            "detail": detail,
        }
        super().__init__(detail)


class RuntimeConfigurationError(LifecycleError):
    """An incumbent runtime configuration is incomplete or not exactly restorable."""


class ReconciliationError(LifecycleError):
    """A timed-out or failed load left local model state indeterminate."""


def _bounded_text(value: object) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    if not text:
        text = value.__class__.__name__
    return text[:_MAX_DIAGNOSTIC_CHARS]


def _sanitize_action(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LifecycleError("command action must be a non-empty string")
    return _bounded_text(value)


def _sanitize_argv(argv: Sequence[str]) -> tuple[str, ...]:
    """Return a bounded command record with common secret values redacted."""

    sanitized: list[str] = []
    redact_next = False
    for raw in argv:
        value = _bounded_text(raw)
        if redact_next:
            sanitized.append("<redacted>")
            redact_next = False
            continue
        option, separator, _inline = value.partition("=")
        if option.casefold() in _SECRET_OPTIONS:
            sanitized.append(option + ("=<redacted>" if separator else ""))
            redact_next = not separator
            continue
        if value.startswith(("/", "\\\\")) or re.match(
            r"^[A-Za-z]:[\\/]", value
        ):
            digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
            sanitized.append(f"<absolute-path:{digest}>")
            continue
        sanitized.append(value)
    return tuple(sanitized)


@dataclass(frozen=True)
class OperationTimeouts:
    """Hard operation ceilings plus independent recovery budgets."""

    inspect_seconds: float = 15.0
    preference_seconds: float = 15.0
    catalog_seconds: float = 30.0
    estimate_seconds: float = 180.0
    load_seconds: float = 300.0
    evaluator_seconds: float = 360.0
    unload_seconds: float = 120.0
    restore_seconds: float = 300.0
    audit_seconds: float = 30.0
    cleanup_seconds: float = 180.0
    reconciliation_seconds: float = 30.0
    reconciliation_poll_seconds: float = 0.25
    terminate_grace_seconds: float = 2.0

    def __post_init__(self) -> None:
        for name, value in vars(self).items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise LifecycleError(f"operation timeout {name} must be positive")


@dataclass(frozen=True)
class TimeoutRecord:
    schema: str
    action: str
    argv: tuple[str, ...]
    timeout_seconds: float
    terminate_process_group: bool

    @classmethod
    def build(
        cls,
        *,
        action: str,
        argv: Sequence[str],
        timeout_seconds: float,
        terminate_process_group: bool,
    ) -> "TimeoutRecord":
        return cls(
            TIMEOUT_RECORD_SCHEMA,
            _sanitize_action(action),
            _sanitize_argv(argv),
            round(float(timeout_seconds), 6),
            bool(terminate_process_group),
        )

    def as_json(self) -> JsonObject:
        return {
            "schema": self.schema,
            "action": self.action,
            "argv": list(self.argv),
            "timeout_seconds": self.timeout_seconds,
            "terminate_process_group": self.terminate_process_group,
        }


class CommandTimeout(LifecycleError):
    """A typed, sanitized hard-deadline failure at the command boundary."""

    def __init__(self, record: TimeoutRecord) -> None:
        self.record = record
        rendered = shlex.join(record.argv)
        super().__init__(
            f"{record.action} exceeded {record.timeout_seconds:.3f}s deadline: {rendered}"
        )


@dataclass(frozen=True)
class CommandRequest:
    argv: tuple[str, ...]
    action: str
    deadline_ns: int
    timeout_seconds: float
    terminate_grace_seconds: float
    terminate_process_group: bool = False

    def remaining_seconds(self, clock_ns: Callable[[], int]) -> float:
        return max(0.0, (self.deadline_ns - clock_ns()) / 1_000_000_000)


@dataclass(frozen=True)
class CommandResult:
    """Small subprocess result type used by the injectable command boundary."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


CommandRunner = Callable[[CommandRequest], CommandResult]


_VOLATILE_RESIDENT_FIELDS = frozenset(
    {
        "status",
        "state",
        "progress",
        "uptimeMs",
        "uptimeSeconds",
        "lastUsedAt",
        "lastRequestAt",
        "tokensPerSecond",
        "activeRequests",
    }
)
_PROCESS_RESIDENT_FIELDS = frozenset(
    {
        "processId",
        "processStartTimeNs",
        "processStartTime",
    }
)


@dataclass(frozen=True)
class Candidate:
    """A human-readable pilot name and an installed LM Studio model reference."""

    name: str
    model: str

    @property
    def slug(self) -> str:
        value = re.sub(r"[^a-z0-9]+", "-", self.name.lower()).strip("-")
        if not value:
            raise LifecycleError(f"candidate name {self.name!r} has no usable characters")
        return value

    @property
    def identifier(self) -> str:
        return f"{AUTOTLDR_PREFIX}{self.slug}"


@dataclass(frozen=True)
class Resident:
    """A normalized row from ``lms ps --json`` or ``lms ls --json``."""

    identifier: str | None
    model_key: str | None
    path: str | None
    indexed_identifier: str | None
    device_identifier: str | None
    model_type: str | None
    raw: JsonObject

    @property
    def references(self) -> tuple[str, ...]:
        return tuple(
            value
            for value in (self.model_key, self.path, self.indexed_identifier)
            if value
        )

    @property
    def is_linked(self) -> bool:
        return self.device_identifier is not None or any(
            _has_colon_prefix(value) for value in self.references
        )

    @property
    def is_local(self) -> bool:
        return not self.is_linked

    @property
    def is_llm(self) -> bool:
        return (self.model_type or "").lower() == "llm"

    @property
    def is_embedding(self) -> bool:
        return "embed" in (self.model_type or "").lower()


@dataclass(frozen=True)
class ResidentFingerprint:
    """Immutable, exact identity/configuration captured from one local PS row.

    LM Studio's transient activity fields are deliberately excluded; every
    other field from the original row is canonicalized.  A later row is the
    same resident only when this complete projection, its expected model
    reference, and the local LM Link device identity are byte-for-byte equal.
    """

    local_device_identifier: str
    expected_model_ref: str
    identifier: str
    size_bytes: int
    canonical_json: str
    sha256: str
    configuration_json: str
    configuration_sha256: str

    @classmethod
    def capture(
        cls,
        resident: Resident,
        *,
        local_device_identifier: str,
        expected_model_ref: str,
    ) -> "ResidentFingerprint":
        if resident.is_linked or resident.device_identifier is not None:
            raise LifecycleError("cannot fingerprint an LM Link resident as local")
        if not resident.identifier:
            raise LifecycleError("cannot fingerprint a resident without an identifier")
        if expected_model_ref not in resident.references:
            raise LifecycleError(
                f"resident does not carry expected local model reference {expected_model_ref!r}"
            )
        size_bytes = resident.raw.get("sizeBytes")
        if (
            not isinstance(size_bytes, int)
            or isinstance(size_bytes, bool)
            or size_bytes <= 0
        ):
            raise LifecycleError(
                "resident sizeBytes must be a positive integer for GPU attestation"
            )
        stable_raw = {
            key: value
            for key, value in resident.raw.items()
            if key not in _VOLATILE_RESIDENT_FIELDS
        }
        payload = {
            "schema": 1,
            "local_device_identifier": local_device_identifier,
            "expected_model_ref": expected_model_ref,
            "resident": {
                "identifier": resident.identifier,
                "model_key": resident.model_key,
                "path": resident.path,
                "indexed_identifier": resident.indexed_identifier,
                "device_identifier": resident.device_identifier,
                "model_type": resident.model_type,
                "raw": stable_raw,
            },
        }
        try:
            canonical = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise LifecycleError(
                "resident fingerprint contains non-canonical LM Studio values"
            ) from exc
        configuration_payload = dict(payload)
        configuration_payload["resident"] = dict(payload["resident"])
        configuration_payload["resident"]["raw"] = {
            key: value
            for key, value in stable_raw.items()
            if key not in _PROCESS_RESIDENT_FIELDS
        }
        configuration_canonical = json.dumps(
            configuration_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return cls(
            local_device_identifier=local_device_identifier,
            expected_model_ref=expected_model_ref,
            identifier=resident.identifier,
            size_bytes=size_bytes,
            canonical_json=canonical,
            sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            configuration_json=configuration_canonical,
            configuration_sha256=hashlib.sha256(
                configuration_canonical.encode("utf-8")
            ).hexdigest(),
        )

    def matches(self, resident: Resident, *, local_device_identifier: str) -> bool:
        try:
            observed = self.capture(
                resident,
                local_device_identifier=local_device_identifier,
                expected_model_ref=self.expected_model_ref,
            )
        except LifecycleError:
            return False
        return observed.canonical_json == self.canonical_json and observed.sha256 == self.sha256

    def same_configuration(self, other: "ResidentFingerprint") -> bool:
        return (
            self.configuration_json == other.configuration_json
            and self.configuration_sha256 == other.configuration_sha256
        )


@dataclass
class CandidateLifecycleState:
    """Mutable transaction state retained even when routing restoration raises."""

    candidate: Candidate
    model_ref: str
    load_attempted: bool = False
    fingerprint: ResidentFingerprint | None = None
    attestation: "ResidencyAttestation | None" = None
    load_timeout: TimeoutRecord | None = None
    load_command_finished: bool = False


_ATTESTATION_KEYS = frozenset(
    {
        "schema",
        "complete",
        "resident_fingerprint_sha256",
        "local_device_identifier",
        "resident_identifier",
        "expected_model_ref",
        "process_id",
        "process_start_time_ns",
        "process_executable_sha256",
        "gpu_layers_request",
        "cpu_moe_layers",
        "kv_cache_on_gpu",
        "gpu_allocation_bytes",
        "model_size_bytes",
        "offloaded_layers",
        "total_layers",
    }
)


@dataclass(frozen=True)
class ResidencyAttestation:
    """Closed, sanitized proof of actual (not configured) ZBook residency."""

    schema: str
    complete: bool
    resident_fingerprint_sha256: str
    local_device_identifier: str
    resident_identifier: str
    expected_model_ref: str
    process_id: int
    process_start_time_ns: int
    process_executable_sha256: str
    gpu_layers_request: str
    cpu_moe_layers: int
    kv_cache_on_gpu: bool
    gpu_allocation_bytes: int
    model_size_bytes: int
    offloaded_layers: int
    total_layers: int

    @classmethod
    def parse(
        cls,
        value: Mapping[str, Any],
        *,
        fingerprint: ResidentFingerprint,
    ) -> "ResidencyAttestation":
        if not isinstance(value, Mapping):
            raise ResidencyAttestationError("residency attestor returned no object")
        keys = frozenset(value)
        if keys != _ATTESTATION_KEYS:
            missing = sorted(_ATTESTATION_KEYS - keys)
            extra = sorted(keys - _ATTESTATION_KEYS)
            raise ResidencyAttestationError(
                f"residency attestation is not closed; missing={missing}, extra={extra}"
            )
        try:
            attestation = cls(**dict(value))
        except TypeError as exc:
            raise ResidencyAttestationError(
                "residency attestation has invalid fields"
            ) from exc
        exact_strings = {
            "schema": ATTESTATION_SCHEMA,
            "resident_fingerprint_sha256": fingerprint.sha256,
            "local_device_identifier": fingerprint.local_device_identifier,
            "resident_identifier": fingerprint.identifier,
            "expected_model_ref": fingerprint.expected_model_ref,
            "gpu_layers_request": GPU_MODE,
        }
        for field, expected in exact_strings.items():
            if getattr(attestation, field) != expected:
                raise ResidencyAttestationError(
                    f"residency attestation field {field!r} does not bind the exact resident"
                )
        if attestation.complete is not True:
            raise ResidencyAttestationError("residency attestation is partial")
        if (
            not isinstance(attestation.process_id, int)
            or isinstance(attestation.process_id, bool)
            or attestation.process_id <= 0
        ):
            raise ResidencyAttestationError(
                "residency attestation has no positive process_id"
            )
        if (
            not isinstance(attestation.process_start_time_ns, int)
            or isinstance(attestation.process_start_time_ns, bool)
            or attestation.process_start_time_ns <= 0
        ):
            raise ResidencyAttestationError(
                "residency attestation has no positive process_start_time_ns"
            )
        if not isinstance(attestation.process_executable_sha256, str) or not re.fullmatch(
            r"[0-9a-f]{64}", attestation.process_executable_sha256
        ):
            raise ResidencyAttestationError(
                "residency attestation has no sanitized executable fingerprint"
            )
        if type(attestation.cpu_moe_layers) is not int or attestation.cpu_moe_layers != 0:
            raise ResidencyAttestationError(
                "residency attestation does not prove zero CPU MoE layers"
            )
        if attestation.kv_cache_on_gpu is not True:
            raise ResidencyAttestationError(
                "residency attestation does not prove GPU KV cache"
            )
        integer_fields = (
            "gpu_allocation_bytes",
            "model_size_bytes",
            "offloaded_layers",
            "total_layers",
        )
        for field in integer_fields:
            observed = getattr(attestation, field)
            if (
                not isinstance(observed, int)
                or isinstance(observed, bool)
                or observed <= 0
            ):
                raise ResidencyAttestationError(
                    f"residency attestation field {field!r} must be positive"
                )
        if attestation.model_size_bytes != fingerprint.size_bytes:
            raise ResidencyAttestationError(
                "residency attestation model size differs from the fingerprint"
            )
        if attestation.gpu_allocation_bytes < attestation.model_size_bytes:
            raise ResidencyAttestationError(
                "GPU allocation is smaller than the resident model"
            )
        if attestation.offloaded_layers != attestation.total_layers:
            raise ResidencyAttestationError(
                "residency attestation does not prove literal all-layer offload"
            )
        return attestation

    def as_json(self) -> JsonObject:
        return {
            field: getattr(self, field)
            for field in sorted(_ATTESTATION_KEYS)
        }


class ResidencyAttestor(Protocol):
    def __call__(
        self,
        fingerprint: ResidentFingerprint,
        *,
        deadline_ns: int,
    ) -> Mapping[str, Any]: ...


_RUNTIME_CONFIGURATION_KEYS = frozenset(
    {
        "schema",
        "source",
        "complete",
        "exact_restorable",
        "resident_fingerprint_sha256",
        "settings",
        "restore_argv",
    }
)
_SECRET_KEY_FRAGMENTS = (
    "api_key",
    "authorization",
    "password",
    "secret",
    "token",
    "commandline",
    "command_line",
    "argv",
    "environment",
)


def _reject_secret_settings(value: Any, *, path: str = "settings") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise RuntimeConfigurationError(
                    f"runtime configuration {path} contains a non-string key"
                )
            normalized = key.casefold().replace("-", "_")
            if any(fragment in normalized for fragment in _SECRET_KEY_FRAGMENTS):
                raise RuntimeConfigurationError(
                    f"runtime configuration contains prohibited secret field at {path}"
                )
            _reject_secret_settings(child, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_secret_settings(child, path=f"{path}[{index}]")
    elif value is not None and type(value) not in {str, int, float, bool}:
        raise RuntimeConfigurationError(
            f"runtime configuration {path} is not a strict JSON value"
        )


@dataclass(frozen=True)
class RuntimeConfiguration:
    """Complete structured incumbent settings and their exact restore command."""

    schema: str
    source: str
    complete: bool
    exact_restorable: bool
    resident_fingerprint_sha256: str
    settings: Mapping[str, Any]
    restore_argv: tuple[str, ...]
    settings_sha256: str

    @classmethod
    def parse(
        cls,
        value: Mapping[str, Any],
        *,
        fingerprint: ResidentFingerprint,
    ) -> "RuntimeConfiguration":
        if not isinstance(value, Mapping):
            raise RuntimeConfigurationError("runtime configuration probe returned no object")
        keys = frozenset(value)
        if keys != _RUNTIME_CONFIGURATION_KEYS:
            missing = sorted(_RUNTIME_CONFIGURATION_KEYS - keys)
            extra = sorted(keys - _RUNTIME_CONFIGURATION_KEYS)
            raise RuntimeConfigurationError(
                f"runtime configuration is not closed; missing={missing}, extra={extra}"
            )
        if value.get("schema") != RUNTIME_CONFIG_SCHEMA:
            raise RuntimeConfigurationError("runtime configuration schema is not supported")
        if value.get("complete") is not True:
            raise RuntimeConfigurationError("incumbent runtime configuration is incomplete")
        if value.get("exact_restorable") is not True:
            raise RuntimeConfigurationError(
                "incumbent runtime configuration is not exactly restorable"
            )
        source = value.get("source")
        if not isinstance(source, str) or not source.strip():
            raise RuntimeConfigurationError(
                "runtime configuration source must be a non-empty identifier"
            )
        if value.get("resident_fingerprint_sha256") != fingerprint.sha256:
            raise RuntimeConfigurationError(
                "runtime configuration does not bind the exact incumbent fingerprint"
            )
        settings = value.get("settings")
        if not isinstance(settings, Mapping):
            raise RuntimeConfigurationError("runtime configuration settings must be an object")
        _reject_secret_settings(settings)
        try:
            canonical = json.dumps(
                settings,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeConfigurationError(
                "runtime configuration settings are not canonical JSON"
            ) from exc
        raw_restore = value.get("restore_argv")
        if (
            not isinstance(raw_restore, list)
            or not raw_restore
            or not all(isinstance(item, str) and item for item in raw_restore)
        ):
            raise RuntimeConfigurationError(
                "runtime configuration has no exact restore argv"
            )
        restore_argv = tuple(raw_restore)
        if _sanitize_argv(restore_argv) != restore_argv:
            raise RuntimeConfigurationError(
                "runtime restore argv contains a prohibited secret option"
            )
        return cls(
            schema=RUNTIME_CONFIG_SCHEMA,
            source=source,
            complete=True,
            exact_restorable=True,
            resident_fingerprint_sha256=fingerprint.sha256,
            settings=dict(settings),
            restore_argv=restore_argv,
            settings_sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        )

    def matches(self, other: "RuntimeConfiguration") -> bool:
        return (
            self.schema == other.schema
            and self.source == other.source
            and self.complete is other.complete is True
            and self.exact_restorable is other.exact_restorable is True
            and self.settings_sha256 == other.settings_sha256
            and self.settings == other.settings
            and self.restore_argv == other.restore_argv
        )

    def as_json(self) -> JsonObject:
        return {
            "schema": self.schema,
            "source": self.source,
            "complete": self.complete,
            "exact_restorable": self.exact_restorable,
            "resident_fingerprint_sha256": self.resident_fingerprint_sha256,
            "settings": dict(self.settings),
            "settings_sha256": self.settings_sha256,
            "restore_argv": list(self.restore_argv),
        }


class RuntimeConfigurationProbe(Protocol):
    def __call__(
        self,
        fingerprint: ResidentFingerprint,
        *,
        deadline_ns: int,
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class IncumbentSnapshot:
    """Every load-relevant setting exposed by the captured LM Studio row."""

    resident: Resident
    model_ref: str
    identifier: str
    context_length: int
    parallel: int
    ttl_ms: int | None
    fingerprint: ResidentFingerprint
    runtime_configuration: RuntimeConfiguration
    residency_attestation: ResidencyAttestation

    def as_json(self) -> JsonObject:
        return {
            "schema": 2,
            "purpose": "incumbent-recovery-snapshot",
            "raw_lms_ps_row": self.resident.raw,
            "normalized": {
                "model_ref": self.model_ref,
                "identifier": self.identifier,
                "context_length": self.context_length,
                "parallel": self.parallel,
                "ttl_ms": self.ttl_ms,
                "gpu": GPU_MODE,
            },
            "resident_fingerprint": {
                "sha256": self.fingerprint.sha256,
                "canonical": json.loads(self.fingerprint.canonical_json),
            },
            "runtime_configuration": self.runtime_configuration.as_json(),
            "residency_attestation": self.residency_attestation.as_json(),
            "restore_command": list(self.runtime_configuration.restore_argv),
        }


@dataclass(frozen=True)
class LinkSnapshot:
    """The local device and original LM Link routing preference."""

    local_device_identifier: str
    preferred_device_identifier: str
    peer_device_identifiers: tuple[str, ...]
    raw: JsonObject

    def as_json(self, restore_command: Sequence[str]) -> JsonObject:
        return {
            "schema": 1,
            "purpose": "lm-link-preference-recovery-snapshot",
            "local_device_identifier": self.local_device_identifier,
            "preferred_device_identifier": self.preferred_device_identifier,
            "peer_device_identifiers": list(self.peer_device_identifiers),
            "raw_lms_link_status": self.raw,
            "restore_command": list(restore_command),
        }


def _terminate_process(
    process: subprocess.Popen[str],
    *,
    process_group: bool,
    grace_seconds: float,
) -> None:
    """Bounded best-effort termination without ever waiting indefinitely."""

    def signal_group(signum: int) -> None:
        if process_group and os.name == "posix":
            os.killpg(process.pid, signum)
        else:
            if signum == signal.SIGTERM:
                process.terminate()
            else:
                process.kill()

    try:
        signal_group(signal.SIGTERM)
    except (OSError, ProcessLookupError):
        return
    try:
        process.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        signal_group(signal.SIGKILL)
    except (OSError, ProcessLookupError):
        return
    try:
        process.wait(timeout=grace_seconds)
    except (OSError, subprocess.TimeoutExpired):
        # The caller still receives a typed timeout.  There is no unbounded
        # third wait hidden behind error reporting.
        return


def _default_command_runner(request: CommandRequest) -> CommandResult:
    remaining = request.remaining_seconds(time.monotonic_ns)
    if remaining <= 0:
        raise CommandTimeout(
            TimeoutRecord.build(
                action=request.action,
                argv=request.argv,
                timeout_seconds=request.timeout_seconds,
                terminate_process_group=request.terminate_process_group,
            )
        )
    kwargs: dict[str, Any] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
    }
    if request.terminate_process_group:
        if os.name == "posix":
            kwargs["start_new_session"] = True
        elif hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    process = subprocess.Popen(list(request.argv), **kwargs)
    try:
        stdout, stderr = process.communicate(timeout=remaining)
    except subprocess.TimeoutExpired as exc:
        _terminate_process(
            process,
            process_group=request.terminate_process_group,
            grace_seconds=request.terminate_grace_seconds,
        )
        raise CommandTimeout(
            TimeoutRecord.build(
                action=request.action,
                argv=request.argv,
                timeout_seconds=request.timeout_seconds,
                terminate_process_group=request.terminate_process_group,
            )
        ) from exc
    return CommandResult(process.returncode, stdout, stderr)


def _has_colon_prefix(value: str) -> bool:
    """Return whether a path carries LM Link's ``DEVICE:...`` prefix.

    A Windows drive prefix (``C:\\`` or ``C:/``) is the only accepted colon
    form.  Installed model keys do not need URI schemes, so rejecting other
    colon prefixes closes the ambiguity that lets LM Link expose remote models
    through a local catalog.
    """

    stripped = value.strip()
    prefix, separator, suffix = stripped.partition(":")
    if not separator:
        return False
    if len(prefix) == 1 and prefix.isalpha() and suffix.startswith(("/", "\\")):
        return False
    return True


def _require_string(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise LifecycleError(f"LM Studio field {field!r} must be null or a non-empty string")
    return value


def _rows_from_json(text: str, command: str) -> list[JsonObject]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LifecycleError(f"{command} did not return valid JSON: {exc}") from exc
    if isinstance(value, dict):
        for key in ("models", "loadedModels", "data"):
            if isinstance(value.get(key), list):
                value = value[key]
                break
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise LifecycleError(f"{command} JSON must be an array of model objects")
    return [dict(row) for row in value]


def parse_models(text: str, command: str) -> list[Resident]:
    """Normalize LM Studio JSON without collapsing the local/link distinction."""

    residents: list[Resident] = []
    for raw in _rows_from_json(text, command):
        residents.append(
            Resident(
                identifier=_require_string(raw.get("identifier"), "identifier"),
                model_key=_require_string(raw.get("modelKey"), "modelKey"),
                path=_require_string(raw.get("path"), "path"),
                indexed_identifier=_require_string(
                    raw.get("indexedModelIdentifier"), "indexedModelIdentifier"
                ),
                device_identifier=_require_string(
                    raw.get("deviceIdentifier"), "deviceIdentifier"
                ),
                model_type=_require_string(raw.get("type"), "type"),
                raw=raw,
            )
        )
    return residents


def parse_link_status(text: str) -> LinkSnapshot:
    """Parse the exact LM Link identity boundary exposed by the local CLI."""

    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LifecycleError(f"lms link status --json returned invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise LifecycleError("lms link status --json must return one object")
    local_identifier = _require_string(value.get("deviceIdentifier"), "deviceIdentifier")
    preferred_identifier = _require_string(
        value.get("preferredDeviceIdentifier"), "preferredDeviceIdentifier"
    )
    if local_identifier is None or preferred_identifier is None:
        raise LifecycleError(
            "LM Link status must expose both local deviceIdentifier and "
            "preferredDeviceIdentifier"
        )
    peers = value.get("peers")
    if not isinstance(peers, list) or not all(isinstance(peer, dict) for peer in peers):
        raise LifecycleError("LM Link status peers must be an array of objects")
    peer_identifiers: list[str] = []
    for offset, peer in enumerate(peers, start=1):
        identifier = _require_string(
            peer.get("deviceIdentifier"), f"peers[{offset}].deviceIdentifier"
        )
        if identifier is None:
            raise LifecycleError(f"LM Link peer {offset} has no deviceIdentifier")
        peer_identifiers.append(identifier)
    if len(peer_identifiers) != len(set(peer_identifiers)):
        raise LifecycleError("LM Link status contains duplicate peer device identifiers")
    if local_identifier in peer_identifiers:
        raise LifecycleError("local LM Link deviceIdentifier is also listed as a peer")
    known_identifiers = {local_identifier, *peer_identifiers}
    if preferred_identifier not in known_identifiers:
        raise LifecycleError(
            "preferredDeviceIdentifier is neither the local device nor a listed peer"
        )
    return LinkSnapshot(
        local_device_identifier=local_identifier,
        preferred_device_identifier=preferred_identifier,
        peer_device_identifiers=tuple(peer_identifiers),
        raw=dict(value),
    )


def parse_candidate(value: str) -> Candidate:
    name, separator, model = value.partition("=")
    name = name.strip()
    model = model.strip()
    if not separator or not name or not model:
        raise LifecycleError(
            f"candidate {value!r} must use the non-empty NAME=INSTALLED_MODEL form"
        )
    if _has_colon_prefix(model):
        raise LifecycleError(f"candidate model {model!r} has a linked/colon-prefixed path")
    candidate = Candidate(name=name, model=model)
    _ = candidate.slug
    return candidate


class SequentialPilotRunner:
    """Enforce exclusive local residency around label-blind pilot runs."""

    def __init__(
        self,
        *,
        command_runner: CommandRunner = _default_command_runner,
        lms_executable: str = DEFAULT_LMS_EXECUTABLE,
        python_executable: str = sys.executable,
        evaluator: Path = EVALUATOR,
        items_path: Path = DEFAULT_ITEMS,
        prompt_path: Path = DEFAULT_PROMPT,
        policy_path: Path = DEFAULT_POLICY,
        output_dir: Path = DEFAULT_OUTPUT_DIR,
        context_length: int = CONTEXT_LENGTH,
        parallel: int = PARALLEL,
        timeouts: OperationTimeouts = OperationTimeouts(),
        clock_ns: Callable[[], int] = time.monotonic_ns,
        sleep: Callable[[float], None] = time.sleep,
        residency_attestor: ResidencyAttestor | None = None,
        runtime_configuration_probe: RuntimeConfigurationProbe | None = None,
    ) -> None:
        if (
            not isinstance(context_length, int)
            or isinstance(context_length, bool)
            or context_length < 1
        ):
            raise LifecycleError("context_length must be a positive integer")
        if not isinstance(parallel, int) or isinstance(parallel, bool) or parallel < 1:
            raise LifecycleError("parallel must be a positive integer")
        self.command_runner = command_runner
        self.lms_executable = lms_executable
        self.python_executable = python_executable
        self.evaluator = evaluator
        self.items_path = items_path
        self.prompt_path = prompt_path
        self.policy_path = policy_path
        self.output_dir = output_dir
        self.context_length = context_length
        self.parallel = parallel
        self.timeouts = timeouts
        self.clock_ns = clock_ns
        self.sleep = sleep
        self.residency_attestor = residency_attestor
        self.runtime_configuration_probe = runtime_configuration_probe
        self.snapshot_path = output_dir / ".incumbent-snapshot.json"
        self.link_snapshot_path = output_dir / ".link-preference-snapshot.json"
        self.candidate_recovery_path = output_dir / ".candidate-recovery.json"

    def _run(
        self,
        argv: Sequence[str],
        purpose: str,
        timeout_seconds: float,
        *,
        deadline_ns: int | None = None,
        terminate_process_group: bool = False,
    ) -> CommandResult:
        """Execute one command under a mandatory monotonic hard deadline."""

        started_ns = self.clock_ns()
        operation_deadline_ns = started_ns + int(timeout_seconds * 1_000_000_000)
        effective_deadline_ns = (
            operation_deadline_ns
            if deadline_ns is None
            else min(operation_deadline_ns, deadline_ns)
        )
        effective_seconds = max(
            0.0, (effective_deadline_ns - started_ns) / 1_000_000_000
        )
        request = CommandRequest(
            argv=tuple(argv),
            action=_sanitize_action(purpose),
            deadline_ns=effective_deadline_ns,
            timeout_seconds=effective_seconds,
            terminate_grace_seconds=self.timeouts.terminate_grace_seconds,
            terminate_process_group=terminate_process_group,
        )
        if effective_seconds <= 0:
            raise CommandTimeout(
                TimeoutRecord.build(
                    action=purpose,
                    argv=argv,
                    timeout_seconds=effective_seconds,
                    terminate_process_group=terminate_process_group,
                )
            )
        result = self.command_runner(request)
        if self.clock_ns() > effective_deadline_ns:
            raise CommandTimeout(
                TimeoutRecord.build(
                    action=purpose,
                    argv=argv,
                    timeout_seconds=effective_seconds,
                    terminate_process_group=terminate_process_group,
                )
            )
        if result.returncode != 0:
            detail = _bounded_text(
                result.stderr.strip() or result.stdout.strip() or "no command output"
            )
            raise LifecycleError(
                f"{purpose} failed with exit code {result.returncode}: {detail}"
            )
        return result

    def _effective_deadline(
        self, timeout_seconds: float, outer_deadline_ns: int | None = None
    ) -> tuple[int, float]:
        started_ns = self.clock_ns()
        operation_deadline_ns = started_ns + int(timeout_seconds * 1_000_000_000)
        deadline_ns = (
            operation_deadline_ns
            if outer_deadline_ns is None
            else min(operation_deadline_ns, outer_deadline_ns)
        )
        return deadline_ns, max(0.0, (deadline_ns - started_ns) / 1_000_000_000)

    def _probe_timeout(
        self,
        *,
        action: str,
        argv: Sequence[str],
        timeout_seconds: float,
    ) -> CommandTimeout:
        return CommandTimeout(
            TimeoutRecord.build(
                action=action,
                argv=argv,
                timeout_seconds=timeout_seconds,
                terminate_process_group=False,
            )
        )

    def _attest_residency(
        self,
        fingerprint: ResidentFingerprint,
        *,
        phase: str,
        deadline_ns: int | None = None,
    ) -> ResidencyAttestation:
        if self.residency_attestor is None:
            raise ResidencyAttestationError(
                "no ZBook actual-residency attestor is configured; configuration-only "
                "GPU claims are not accepted",
                fingerprint_sha256=fingerprint.sha256,
                phase=phase,
            )
        action = f"attest actual GPU residency {phase}"
        probe_argv = ("<zbook-residency-attestor>", fingerprint.sha256)
        probe_deadline_ns, effective_seconds = self._effective_deadline(
            self.timeouts.audit_seconds, deadline_ns
        )
        if effective_seconds <= 0:
            raise self._probe_timeout(
                action=action,
                argv=probe_argv,
                timeout_seconds=effective_seconds,
            )
        value = self.residency_attestor(
            fingerprint,
            deadline_ns=probe_deadline_ns,
        )
        if self.clock_ns() > probe_deadline_ns:
            raise self._probe_timeout(
                action=action,
                argv=probe_argv,
                timeout_seconds=effective_seconds,
            )
        try:
            return ResidencyAttestation.parse(value, fingerprint=fingerprint)
        except ResidencyAttestationError as exc:
            raise ResidencyAttestationError(
                str(exc),
                fingerprint_sha256=fingerprint.sha256,
                phase=phase,
            ) from exc

    def _capture_runtime_configuration(
        self,
        fingerprint: ResidentFingerprint,
        *,
        phase: str,
        deadline_ns: int | None = None,
    ) -> RuntimeConfiguration:
        if self.runtime_configuration_probe is None:
            raise RuntimeConfigurationError(
                "no complete incumbent runtime-configuration probe is configured"
            )
        action = f"capture incumbent runtime configuration {phase}"
        probe_argv = ("<zbook-runtime-config-probe>", fingerprint.sha256)
        probe_deadline_ns, effective_seconds = self._effective_deadline(
            self.timeouts.audit_seconds, deadline_ns
        )
        if effective_seconds <= 0:
            raise self._probe_timeout(
                action=action,
                argv=probe_argv,
                timeout_seconds=effective_seconds,
            )
        value = self.runtime_configuration_probe(
            fingerprint,
            deadline_ns=probe_deadline_ns,
        )
        if self.clock_ns() > probe_deadline_ns:
            raise self._probe_timeout(
                action=action,
                argv=probe_argv,
                timeout_seconds=effective_seconds,
            )
        configuration = RuntimeConfiguration.parse(value, fingerprint=fingerprint)
        argv = configuration.restore_argv
        if len(argv) < 3 or argv[:2] != (self.lms_executable, "load"):
            raise RuntimeConfigurationError(
                "incumbent restore argv is not a load through the configured local LMS CLI"
            )
        if argv[2] != fingerprint.expected_model_ref:
            raise RuntimeConfigurationError(
                "incumbent restore argv targets a different model reference"
            )
        if "--estimate-only" in argv:
            raise RuntimeConfigurationError("incumbent restore argv is estimate-only")
        try:
            identifier = argv[argv.index("--identifier") + 1]
            gpu_mode = argv[argv.index("--gpu") + 1]
        except (ValueError, IndexError) as exc:
            raise RuntimeConfigurationError(
                "incumbent restore argv lacks exact identifier/GPU settings"
            ) from exc
        if identifier != fingerprint.identifier or gpu_mode != GPU_MODE:
            raise RuntimeConfigurationError(
                "incumbent restore argv does not bind exact identifier and max GPU mode"
            )
        return configuration

    def _ps(
        self,
        *,
        deadline_ns: int | None = None,
        timeout_seconds: float | None = None,
    ) -> list[Resident]:
        result = self._run(
            [self.lms_executable, "ps", "--json"],
            "inspect LM Studio residents",
            timeout_seconds or self.timeouts.inspect_seconds,
            deadline_ns=deadline_ns,
        )
        return parse_models(result.stdout, "lms ps --json")

    def _catalog(self, *, deadline_ns: int | None = None) -> list[Resident]:
        result = self._run(
            [self.lms_executable, "ls", "--json"],
            "inspect LM Studio catalog",
            self.timeouts.catalog_seconds,
            deadline_ns=deadline_ns,
        )
        return parse_models(result.stdout, "lms ls --json")

    def _link_status(
        self,
        *,
        deadline_ns: int | None = None,
        timeout_seconds: float | None = None,
    ) -> LinkSnapshot:
        result = self._run(
            [self.lms_executable, "link", "status", "--json"],
            "inspect LM Link routing",
            timeout_seconds or self.timeouts.inspect_seconds,
            deadline_ns=deadline_ns,
        )
        return parse_link_status(result.stdout)

    def _set_preferred_device(
        self, device_identifier: str, *, deadline_ns: int | None = None
    ) -> None:
        self._run(
            [
                self.lms_executable,
                "link",
                "set-preferred-device",
                device_identifier,
            ],
            f"set LM Link preferred device to {device_identifier!r}",
            self.timeouts.preference_seconds,
            deadline_ns=deadline_ns,
        )

    def _verify_link_identity(
        self,
        snapshot: LinkSnapshot,
        *,
        expected_preferred: str,
        deadline_ns: int | None = None,
    ) -> None:
        observed = self._link_status(deadline_ns=deadline_ns)
        if observed.local_device_identifier != snapshot.local_device_identifier:
            raise LifecycleError(
                "local LM Link device identity changed during the lifecycle: "
                f"expected {snapshot.local_device_identifier!r}, found "
                f"{observed.local_device_identifier!r}"
            )
        if observed.preferred_device_identifier != expected_preferred:
            raise LifecycleError(
                "LM Link preference verification failed: expected "
                f"{expected_preferred!r}, found "
                f"{observed.preferred_device_identifier!r}"
            )

    def _restore_link_preference(
        self, snapshot: LinkSnapshot, *, deadline_ns: int | None = None
    ) -> None:
        self._set_preferred_device(
            snapshot.preferred_device_identifier, deadline_ns=deadline_ns
        )
        self._verify_link_identity(
            snapshot,
            expected_preferred=snapshot.preferred_device_identifier,
            deadline_ns=deadline_ns,
        )

    def _on_zbook(
        self,
        snapshot: LinkSnapshot,
        purpose: str,
        operation: Callable[[], ResultT],
        *,
        deadline_ns: int | None = None,
    ) -> ResultT:
        """Run one operation under local routing, restoring routing on all exits."""

        result: ResultT | None = None
        failure: BaseException | None = None
        try:
            self._set_preferred_device(
                snapshot.local_device_identifier, deadline_ns=deadline_ns
            )
            self._verify_link_identity(
                snapshot,
                expected_preferred=snapshot.local_device_identifier,
                deadline_ns=deadline_ns,
            )
            result = operation()
        except BaseException as exc:
            failure = exc

        try:
            self._restore_link_preference(snapshot, deadline_ns=deadline_ns)
        except BaseException as restore_exc:
            if failure is not None:
                raise LifecycleError(
                    f"{purpose} failed ({failure}); restoring LM Link preference also "
                    f"failed ({restore_exc})"
                ) from restore_exc
            raise LifecycleError(
                f"{purpose} completed, but LM Link preference restoration failed "
                f"({restore_exc})"
            ) from restore_exc

        if failure is not None:
            raise failure
        return result  # type: ignore[return-value]

    @staticmethod
    def _local_llms(rows: Sequence[Resident]) -> list[Resident]:
        local_rows = [row for row in rows if row.is_local]
        embeddings = [row for row in local_rows if row.is_embedding]
        if embeddings:
            names = [row.identifier or row.model_key or row.path for row in embeddings]
            raise LifecycleError(
                f"Stage 2 forbids local embedding residents; found {names}"
            )
        unknown = [row for row in local_rows if not row.is_llm]
        if unknown:
            names = [row.identifier or row.model_key or row.path for row in unknown]
            raise LifecycleError(f"refusing unknown local model types: {names}")
        if len(local_rows) > 1:
            names = [row.identifier or row.model_key or row.path for row in local_rows]
            raise LifecycleError(
                f"exclusive residency violated: {len(local_rows)} local LLMs are loaded: {names}"
            )
        return local_rows

    @staticmethod
    def _linked_state(rows: Sequence[Resident]) -> bytes:
        linked = [row.raw for row in rows if row.is_linked]
        linked.sort(
            key=lambda row: (
                str(row.get("deviceIdentifier") or ""),
                str(row.get("identifier") or ""),
                str(row.get("modelKey") or ""),
                str(row.get("path") or ""),
            )
        )
        return json.dumps(
            linked,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")

    def _require_zero_local(self, *, deadline_ns: int | None = None) -> None:
        local = self._local_llms(self._ps(deadline_ns=deadline_ns))
        if local:
            row = local[0]
            raise LifecycleError(
                "expected zero local LLM residents, found "
                f"{row.identifier or row.model_key or row.path!r}"
            )

    def _resolve_local_catalog_model(self, requested: str) -> str:
        """Resolve an installed ref while refusing any local/link ambiguity."""

        matches = [row for row in self._catalog() if requested in row.references]
        if not matches:
            raise LifecycleError(
                f"candidate {requested!r} is not an exact installed LM Studio catalog reference"
            )
        linked = [row for row in matches if row.is_linked]
        local = [row for row in matches if row.is_local]
        if linked:
            raise LifecycleError(
                f"candidate {requested!r} also resolves to an LM Link row; use its unique "
                "ZBook-local path instead"
            )
        if len(local) != 1:
            raise LifecycleError(
                f"candidate {requested!r} resolves to {len(local)} local catalog rows"
            )
        row = local[0]
        if not row.is_llm:
            raise LifecycleError(
                f"candidate {requested!r} is type {row.model_type!r}, not an LLM"
            )
        # `lms ls` exposes an exact catalog path, but `lms load` accepts the
        # modelKey selector rather than that path on current LM Studio builds.
        # The requested path above still disambiguates local from linked rows;
        # the verified ZBook preference plus `--yes` makes the subsequent key
        # selection non-interactive, and the post-load PS fingerprint proves
        # that the resulting row is local and has the expected identity.
        canonical = row.model_key
        if not canonical or _has_colon_prefix(canonical):
            raise LifecycleError(f"candidate {requested!r} has no safe local model key")
        return canonical

    def _estimate(
        self,
        link_snapshot: LinkSnapshot,
        model_ref: str,
        *,
        context_length: int,
        parallel: int,
    ) -> None:
        def estimate() -> None:
            result = self._run(
                [
                    self.lms_executable,
                    "load",
                    model_ref,
                    "--estimate-only",
                    "--gpu",
                    GPU_MODE,
                    "--context-length",
                    str(context_length),
                    "--parallel",
                    str(parallel),
                    "--yes",
                ],
                f"estimate GPU residency for {model_ref!r}",
                self.timeouts.estimate_seconds,
            )
            # `lms load --estimate-only` prints its report to stderr when stdout
            # is not a terminal, which is always the case here. Reading only
            # stdout turned a satisfied 100% gate into a refusal.
            lines = [
                line.strip()
                for line in f"{result.stdout}\n{result.stderr}".splitlines()
            ]
            if lines.count(GPU_OFFLOAD_LINE) != 1:
                observed = [line for line in lines if line.startswith("GPU Offload:")]
                raise LifecycleError(
                    f"refusing {model_ref!r}: estimate must contain exactly "
                    f"{GPU_OFFLOAD_LINE!r}; observed "
                    f"{observed or 'no GPU Offload line'}"
                )

        self._on_zbook(
            link_snapshot,
            f"local GPU estimate for {model_ref!r}",
            estimate,
        )

    def _load_command(
        self,
        model_ref: str,
        identifier: str,
        *,
        context_length: int,
        parallel: int,
        ttl_ms: int | None = None,
    ) -> list[str]:
        command = [
            self.lms_executable,
            "load",
            model_ref,
            "--identifier",
            identifier,
            "--context-length",
            str(context_length),
            "--parallel",
            str(parallel),
            "--gpu",
            GPU_MODE,
            "--yes",
        ]
        if ttl_ms is not None:
            if ttl_ms <= 0 or ttl_ms % 1000:
                raise LifecycleError(
                    f"incumbent ttlMs {ttl_ms!r} cannot be restored exactly in seconds"
                )
            command.extend(["--ttl", str(ttl_ms // 1000)])
        return command

    def _load_candidate(
        self,
        link_snapshot: LinkSnapshot,
        model_ref: str,
        candidate: Candidate,
        state: CandidateLifecycleState,
    ) -> ResidentFingerprint:
        def load_and_verify() -> ResidentFingerprint:
            state.load_attempted = True
            try:
                self._run(
                    self._load_command(
                        model_ref,
                        candidate.identifier,
                        context_length=self.context_length,
                        parallel=self.parallel,
                    ),
                    f"load candidate {candidate.name!r}",
                    self.timeouts.load_seconds,
                )
            except CommandTimeout as exc:
                state.load_timeout = exc.record
                raise
            else:
                state.load_command_finished = True
            fingerprint = self._verify_candidate(
                candidate, model_ref, link_snapshot=link_snapshot
            )
            state.fingerprint = fingerprint
            state.attestation = self._attest_residency(
                fingerprint, phase="immediately after candidate load"
            )
            return fingerprint

        return self._on_zbook(
            link_snapshot,
            f"local load for candidate {candidate.name!r}",
            load_and_verify,
        )

    def _unload_safe(
        self,
        link_snapshot: LinkSnapshot,
        fingerprint: ResidentFingerprint,
        *,
        deadline_ns: int | None = None,
    ) -> None:
        if fingerprint.local_device_identifier != link_snapshot.local_device_identifier:
            raise LifecycleError(
                "internal safety stop: resident fingerprint belongs to another device"
            )

        def verify_unload_verify() -> None:
            # Never trust the row captured before switching routing.  Resolve
            # the target again while the ZBook is explicitly preferred.
            current = self._local_llms(self._ps(deadline_ns=deadline_ns))
            if len(current) != 1 or not fingerprint.matches(
                current[0],
                local_device_identifier=link_snapshot.local_device_identifier,
            ):
                found = [
                    row.identifier or row.model_key or row.path for row in current
                ]
                raise LifecycleError(
                    f"refusing unload: fresh ZBook PS does not contain the exact "
                    f"fingerprinted local row {fingerprint.identifier!r}; found {found}"
                )
            self._run(
                [self.lms_executable, "unload", fingerprint.identifier],
                f"unload local model {fingerprint.identifier!r}",
                self.timeouts.unload_seconds,
                deadline_ns=deadline_ns,
            )
            remaining = self._local_llms(self._ps(deadline_ns=deadline_ns))
            if remaining:
                found = [
                    row.identifier or row.model_key or row.path for row in remaining
                ]
                raise LifecycleError(
                    f"local unload of {fingerprint.identifier!r} did not leave an empty "
                    f"ZBook; found {found}"
                )

        self._on_zbook(
            link_snapshot,
            f"local unload for {fingerprint.identifier!r}",
            verify_unload_verify,
            deadline_ns=deadline_ns,
        )

    def _verify_candidate(
        self,
        candidate: Candidate,
        model_ref: str,
        *,
        link_snapshot: LinkSnapshot,
        deadline_ns: int | None = None,
    ) -> ResidentFingerprint:
        local = self._local_llms(self._ps(deadline_ns=deadline_ns))
        if len(local) != 1:
            raise LifecycleError(
                f"candidate load must leave exactly one local LLM, found {len(local)}"
            )
        row = local[0]
        if row.identifier != candidate.identifier:
            raise LifecycleError(
                f"loaded identifier is {row.identifier!r}, expected {candidate.identifier!r}"
            )
        if not row.identifier.startswith(AUTOTLDR_PREFIX):
            raise LifecycleError("candidate identifier is not AutoTLDR-owned")
        if model_ref not in row.references:
            raise LifecycleError(
                f"loaded candidate references {row.references!r}, expected {model_ref!r}"
            )
        if row.raw.get("contextLength") != self.context_length:
            raise LifecycleError(
                f"loaded candidate contextLength is {row.raw.get('contextLength')!r}, "
                f"expected {self.context_length}"
            )
        if row.raw.get("parallel") != self.parallel:
            raise LifecycleError(
                f"loaded candidate parallel is {row.raw.get('parallel')!r}, "
                f"expected {self.parallel}"
            )
        return ResidentFingerprint.capture(
            row,
            local_device_identifier=link_snapshot.local_device_identifier,
            expected_model_ref=model_ref,
        )

    def _verify_fingerprint(
        self,
        fingerprint: ResidentFingerprint,
        link_snapshot: LinkSnapshot,
        *,
        phase: str,
        deadline_ns: int | None = None,
    ) -> Resident:
        local = self._local_llms(self._ps(deadline_ns=deadline_ns))
        if len(local) != 1 or not fingerprint.matches(
            local[0], local_device_identifier=link_snapshot.local_device_identifier
        ):
            found = [row.identifier or row.model_key or row.path for row in local]
            raise LifecycleError(
                f"{phase} resident fingerprint changed; expected exact "
                f"{fingerprint.identifier!r}, found {found}"
            )
        return local[0]

    def _evaluate_command(self, candidate: Candidate) -> list[str]:
        return [
            self.python_executable,
            str(self.evaluator),
            "run-model",
            "--arm",
            "local",
            "--items",
            str(self.items_path),
            "--prompt",
            str(self.prompt_path),
            "--policy",
            str(self.policy_path),
            "--base-url",
            LOCAL_BASE_URL,
            "--model",
            candidate.identifier,
            "--output",
            str(self.output_dir / f"{candidate.slug}.jsonl"),
            "--concurrency",
            str(self.parallel),
            "--pilot",
        ]

    def _evaluate(
        self,
        candidate: Candidate,
        link_snapshot: LinkSnapshot,
        fingerprint: ResidentFingerprint,
        attestation: ResidencyAttestation,
    ) -> None:
        # This is intentionally the final command before inference.  A model
        # that loaded locally is insufficient if LM Link routing was not put
        # back exactly as found.
        self._verify_link_identity(
            link_snapshot,
            expected_preferred=link_snapshot.preferred_device_identifier,
        )
        self._verify_fingerprint(
            fingerprint, link_snapshot, phase="before inference"
        )
        before_attestation = self._attest_residency(
            fingerprint, phase="before inference"
        )
        if before_attestation != attestation:
            raise ResidencyAttestationError(
                "candidate process or actual residency changed before inference",
                fingerprint_sha256=fingerprint.sha256,
                phase="before inference",
            )
        self._run(
            self._evaluate_command(candidate),
            f"run label-blind pilot for {candidate.name!r}",
            self.timeouts.evaluator_seconds,
            terminate_process_group=True,
        )
        self._verify_fingerprint(
            fingerprint, link_snapshot, phase="after inference"
        )
        after_attestation = self._attest_residency(
            fingerprint, phase="after inference"
        )
        if after_attestation != attestation:
            raise ResidencyAttestationError(
                "candidate process or actual residency changed during inference",
                fingerprint_sha256=fingerprint.sha256,
                phase="after inference",
            )

    @staticmethod
    def _resident_recovery_evidence(resident: Resident) -> JsonObject:
        """Sanitized identity/config evidence without private model paths."""

        reference_hashes = [
            hashlib.sha256(value.encode("utf-8")).hexdigest()
            for value in resident.references
        ]
        return {
            "identifier": resident.identifier,
            "device_identifier": resident.device_identifier,
            "model_type": resident.model_type,
            "reference_sha256": reference_hashes,
            "context_length": resident.raw.get("contextLength"),
            "parallel": resident.raw.get("parallel"),
            "ttl_ms": resident.raw.get("ttlMs"),
            "size_bytes": resident.raw.get("sizeBytes"),
        }

    def _write_candidate_recovery(
        self,
        state: CandidateLifecycleState,
        *,
        reason: str,
        observations: Sequence[Sequence[Resident]],
        timeout_record: TimeoutRecord | None = None,
    ) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        if self.candidate_recovery_path.exists():
            raise ReconciliationError(
                f"candidate recovery evidence already exists at "
                f"{self.candidate_recovery_path}"
            )
        payload: JsonObject = {
            "schema": CANDIDATE_RECOVERY_SCHEMA,
            "reason": _bounded_text(reason),
            "candidate": {
                "name": state.candidate.name,
                "identifier": state.candidate.identifier,
                "expected_model_ref_sha256": hashlib.sha256(
                    state.model_ref.encode("utf-8")
                ).hexdigest(),
                "context_length": self.context_length,
                "parallel": self.parallel,
            },
            "load": {
                "attempted": state.load_attempted,
                "command_finished": state.load_command_finished,
                "timeout": (
                    state.load_timeout.as_json()
                    if state.load_timeout is not None
                    else None
                ),
            },
            "reconciliation_timeout": (
                timeout_record.as_json() if timeout_record is not None else None
            ),
            "captured_fingerprint_sha256": (
                state.fingerprint.sha256 if state.fingerprint is not None else None
            ),
            "observations": [
                [self._resident_recovery_evidence(row) for row in rows]
                for rows in observations[-32:]
            ],
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ) + "\n"
        temporary: tempfile.NamedTemporaryFile[str] | None = None
        try:
            temporary = tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.output_dir,
                prefix=".candidate-recovery.",
                delete=False,
            )
            with temporary:
                temporary.write(encoded)
            os.replace(temporary.name, self.candidate_recovery_path)
        except OSError as exc:
            if temporary is not None:
                Path(temporary.name).unlink(missing_ok=True)
            raise ReconciliationError(
                f"cannot persist candidate recovery evidence: {_bounded_text(exc)}"
            ) from exc

    def _row_is_expected_candidate(
        self,
        row: Resident,
        state: CandidateLifecycleState,
    ) -> bool:
        return (
            row.is_local
            and row.is_llm
            and row.identifier == state.candidate.identifier
            and state.model_ref in row.references
            and row.raw.get("contextLength") == self.context_length
            and row.raw.get("parallel") == self.parallel
            and row.raw.get("ttlMs") is None
            and isinstance(row.raw.get("sizeBytes"), int)
            and not isinstance(row.raw.get("sizeBytes"), bool)
            and row.raw.get("sizeBytes") > 0
        )

    def _reconcile_candidate_load(
        self,
        link_snapshot: LinkSnapshot,
        state: CandidateLifecycleState,
        *,
        cleanup_deadline_ns: int,
    ) -> ResidentFingerprint | None:
        reconciliation_deadline_ns = min(
            cleanup_deadline_ns,
            self.clock_ns()
            + int(self.timeouts.reconciliation_seconds * 1_000_000_000),
        )
        observations: list[list[Resident]] = []
        while True:
            try:
                rows = self._ps(deadline_ns=reconciliation_deadline_ns)
            except CommandTimeout as exc:
                self._write_candidate_recovery(
                    state,
                    reason="candidate reconciliation PS timed out",
                    observations=observations,
                    timeout_record=exc.record,
                )
                raise ReconciliationError(
                    "candidate load reconciliation timed out; recovery evidence preserved"
                ) from exc
            local = [row for row in rows if row.is_local]
            observations.append(local)
            try:
                validated = self._local_llms(rows)
            except LifecycleError as exc:
                self._write_candidate_recovery(
                    state,
                    reason=f"unsafe residents during reconciliation: {exc}",
                    observations=observations,
                )
                raise ReconciliationError(
                    "candidate load reconciliation found unsafe local state; "
                    "recovery evidence preserved"
                ) from exc
            if validated:
                resident = validated[0]
                if not self._row_is_expected_candidate(resident, state):
                    self._write_candidate_recovery(
                        state,
                        reason="unexpected local resident during candidate reconciliation",
                        observations=observations,
                    )
                    raise ReconciliationError(
                        "candidate load reconciliation found an unexpected resident; "
                        "recovery evidence preserved"
                    )
                fingerprint = ResidentFingerprint.capture(
                    resident,
                    local_device_identifier=link_snapshot.local_device_identifier,
                    expected_model_ref=state.model_ref,
                )
                state.fingerprint = fingerprint
                try:
                    state.attestation = self._attest_residency(
                        fingerprint,
                        phase="late-load reconciliation",
                        deadline_ns=cleanup_deadline_ns,
                    )
                except BaseException as exc:
                    self._write_candidate_recovery(
                        state,
                        reason=f"late candidate could not be attested: {exc}",
                        observations=observations,
                    )
                    raise ReconciliationError(
                        "late candidate residency is indeterminate; recovery evidence preserved"
                    ) from exc
                return fingerprint

            if state.load_timeout is None:
                # A completed/failed foreground CLI and an empty fresh PS form
                # a determinate absent state; no late server action is pending.
                return None
            now_ns = self.clock_ns()
            if now_ns >= reconciliation_deadline_ns:
                self._write_candidate_recovery(
                    state,
                    reason=(
                        "timed-out load remained absent through bounded reconciliation; "
                        "later server completion cannot be excluded"
                    ),
                    observations=observations,
                )
                raise ReconciliationError(
                    "timed-out candidate load remains indeterminate; recovery evidence preserved"
                )
            remaining_seconds = (
                reconciliation_deadline_ns - now_ns
            ) / 1_000_000_000
            self.sleep(
                min(self.timeouts.reconciliation_poll_seconds, remaining_seconds)
            )

    def _cleanup_candidate(
        self,
        link_snapshot: LinkSnapshot,
        state: CandidateLifecycleState,
    ) -> None:
        """Reconcile, unload only exact ownership, and prove a determinate empty ZBook."""

        cleanup_deadline_ns = self.clock_ns() + int(
            self.timeouts.cleanup_seconds * 1_000_000_000
        )
        local = self._local_llms(self._ps(deadline_ns=cleanup_deadline_ns))
        if local:
            if state.fingerprint is None:
                self._reconcile_candidate_load(
                    link_snapshot,
                    state,
                    cleanup_deadline_ns=cleanup_deadline_ns,
                )
            if state.fingerprint is None:
                raise ReconciliationError(
                    "local resident exists but no exact candidate fingerprint was established"
                )
            try:
                self._verify_fingerprint(
                    state.fingerprint,
                    link_snapshot,
                    phase="before unload",
                    deadline_ns=cleanup_deadline_ns,
                )
            except BaseException as exc:
                self._write_candidate_recovery(
                    state,
                    reason=f"candidate fingerprint changed before unload: {exc}",
                    observations=[local],
                )
                raise ReconciliationError(
                    "candidate resident changed before unload; recovery evidence preserved"
                ) from exc
            if state.attestation is None:
                # The load returned and the exact full PS fingerprint (including
                # process fields when LM Studio exposes them) is unchanged.
                # Attestation failure blocks inference, but not deterministic
                # cleanup of the row this transaction just created.
                self._unload_safe(
                    link_snapshot,
                    state.fingerprint,
                    deadline_ns=cleanup_deadline_ns,
                )
            else:
                unload_attestation = self._attest_residency(
                    state.fingerprint,
                    phase="before candidate unload",
                    deadline_ns=cleanup_deadline_ns,
                )
                if unload_attestation != state.attestation:
                    self._write_candidate_recovery(
                        state,
                        reason="candidate process/residency changed before unload",
                        observations=[local],
                    )
                    raise ReconciliationError(
                        "refusing unload because candidate process/residency was replaced; "
                        "recovery evidence preserved"
                    )
                self._unload_safe(
                    link_snapshot,
                    state.fingerprint,
                    deadline_ns=cleanup_deadline_ns,
                )
        elif state.load_attempted and state.fingerprint is None:
            self._reconcile_candidate_load(
                link_snapshot,
                state,
                cleanup_deadline_ns=cleanup_deadline_ns,
            )
            if state.fingerprint is not None:
                self._unload_safe(
                    link_snapshot,
                    state.fingerprint,
                    deadline_ns=cleanup_deadline_ns,
                )
        self._require_zero_local(deadline_ns=cleanup_deadline_ns)

    def _snapshot_from_resident(
        self,
        resident: Resident,
        link_snapshot: LinkSnapshot,
    ) -> IncumbentSnapshot:
        if resident.is_linked or not resident.is_llm:
            raise LifecycleError("only a local LLM can be snapshotted as the incumbent")
        if not resident.identifier:
            raise LifecycleError("incumbent has no restorable identifier")
        # Current `lms load` accepts modelKey, not the exact catalog path.  The
        # restore runs only inside the verified ZBook preference transaction;
        # the complete post-load resident fingerprint (including local path)
        # must then match the snapshot before restoration is accepted.
        model_ref = resident.model_key
        if not model_ref or _has_colon_prefix(model_ref):
            raise LifecycleError("incumbent has no safe restorable local model key")
        context_length = resident.raw.get("contextLength")
        parallel = resident.raw.get("parallel")
        ttl_ms = resident.raw.get("ttlMs")
        if (
            not isinstance(context_length, int)
            or isinstance(context_length, bool)
            or context_length < 1
        ):
            raise LifecycleError("incumbent contextLength is not a positive integer")
        if not isinstance(parallel, int) or isinstance(parallel, bool) or parallel < 1:
            raise LifecycleError("incumbent parallel is not a positive integer")
        if ttl_ms is not None and (
            not isinstance(ttl_ms, int) or isinstance(ttl_ms, bool) or ttl_ms < 1
        ):
            raise LifecycleError("incumbent ttlMs must be null or a positive integer")
        fingerprint = ResidentFingerprint.capture(
            resident,
            local_device_identifier=link_snapshot.local_device_identifier,
            expected_model_ref=model_ref,
        )
        residency_attestation = self._attest_residency(
            fingerprint, phase="before incumbent unload"
        )
        runtime_configuration = self._capture_runtime_configuration(
            fingerprint, phase="before incumbent unload"
        )
        return IncumbentSnapshot(
            resident=resident,
            model_ref=model_ref,
            identifier=resident.identifier,
            context_length=context_length,
            parallel=parallel,
            ttl_ms=ttl_ms,
            fingerprint=fingerprint,
            runtime_configuration=runtime_configuration,
            residency_attestation=residency_attestation,
        )

    def _write_snapshot(self, snapshot: IncumbentSnapshot) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        if self.snapshot_path.exists():
            raise LifecycleError(
                f"refusing to overwrite recovery snapshot {self.snapshot_path}"
            )
        payload = json.dumps(snapshot.as_json(), indent=2) + "\n"
        temporary: tempfile.NamedTemporaryFile[str] | None = None
        try:
            temporary = tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.output_dir,
                prefix=".incumbent-snapshot.",
                delete=False,
            )
            with temporary:
                temporary.write(payload)
            os.replace(temporary.name, self.snapshot_path)
        except OSError as exc:
            if temporary is not None:
                Path(temporary.name).unlink(missing_ok=True)
            raise LifecycleError(f"cannot persist incumbent recovery snapshot: {exc}") from exc

    def _write_link_snapshot(self, snapshot: LinkSnapshot) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        if self.link_snapshot_path.exists():
            raise LifecycleError(
                f"refusing to overwrite LM Link recovery snapshot "
                f"{self.link_snapshot_path}"
            )
        restore_command = [
            self.lms_executable,
            "link",
            "set-preferred-device",
            snapshot.preferred_device_identifier,
        ]
        payload = json.dumps(snapshot.as_json(restore_command), indent=2) + "\n"
        temporary: tempfile.NamedTemporaryFile[str] | None = None
        try:
            temporary = tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.output_dir,
                prefix=".link-preference-snapshot.",
                delete=False,
            )
            with temporary:
                temporary.write(payload)
            os.replace(temporary.name, self.link_snapshot_path)
        except OSError as exc:
            if temporary is not None:
                Path(temporary.name).unlink(missing_ok=True)
            raise LifecycleError(
                f"cannot persist LM Link preference recovery snapshot: {exc}"
            ) from exc

    def _restore_incumbent(
        self, snapshot: IncumbentSnapshot, link_snapshot: LinkSnapshot
    ) -> None:
        # An unload command can fail after LM Studio has already changed state.
        # Make restoration idempotent: if the snapshotted incumbent is still
        # present with every captured setting, there is nothing to reload.
        local = self._local_llms(self._ps())
        if local:
            restored = local[0]
            restored_fingerprint = ResidentFingerprint.capture(
                restored,
                local_device_identifier=link_snapshot.local_device_identifier,
                expected_model_ref=snapshot.model_ref,
            )
            if snapshot.fingerprint.same_configuration(restored_fingerprint):
                self._attest_residency(
                    restored_fingerprint, phase="idempotent incumbent restore"
                )
                observed_configuration = self._capture_runtime_configuration(
                    restored_fingerprint, phase="idempotent incumbent restore"
                )
                if not snapshot.runtime_configuration.matches(observed_configuration):
                    raise RuntimeConfigurationError(
                        "resident incumbent runtime configuration drifted from snapshot"
                    )
                self.snapshot_path.unlink(missing_ok=True)
                return
            raise LifecycleError(
                "cannot restore incumbent while a different local LLM is resident: "
                f"{restored.identifier or restored.model_key or restored.path!r}"
            )
        self._estimate(
            link_snapshot,
            snapshot.model_ref,
            context_length=snapshot.context_length,
            parallel=snapshot.parallel,
        )

        def load_and_verify() -> Resident:
            self._run(
                snapshot.runtime_configuration.restore_argv,
                f"restore incumbent {snapshot.identifier!r}",
                self.timeouts.restore_seconds,
            )
            local = self._local_llms(self._ps())
            if len(local) != 1:
                raise LifecycleError(
                    f"incumbent restore left {len(local)} local LLMs instead of one"
                )
            return local[0]

        restored = self._on_zbook(
            link_snapshot,
            f"local restore for incumbent {snapshot.identifier!r}",
            load_and_verify,
        )
        restored_fingerprint = ResidentFingerprint.capture(
            restored,
            local_device_identifier=link_snapshot.local_device_identifier,
            expected_model_ref=snapshot.model_ref,
        )
        if not snapshot.fingerprint.same_configuration(restored_fingerprint):
            raise LifecycleError(
                "incumbent restore identity/configuration fingerprint differs from snapshot"
            )
        self._attest_residency(
            restored_fingerprint, phase="after incumbent restore"
        )
        observed_configuration = self._capture_runtime_configuration(
            restored_fingerprint, phase="after incumbent restore"
        )
        if not snapshot.runtime_configuration.matches(observed_configuration):
            raise RuntimeConfigurationError(
                "incumbent restore did not reproduce the complete runtime configuration"
            )
        self.snapshot_path.unlink(missing_ok=True)

    def run(
        self,
        candidates: Sequence[Candidate],
        *,
        incumbent_identifier: str | None = None,
    ) -> None:
        if not candidates:
            raise LifecycleError("at least one candidate is required")
        linked_specs = [
            candidate.model
            for candidate in candidates
            if _has_colon_prefix(candidate.model)
        ]
        if linked_specs:
            raise LifecycleError(
                f"candidate models have linked/colon-prefixed paths: {linked_specs}"
            )
        identifiers = [candidate.identifier for candidate in candidates]
        if len(identifiers) != len(set(identifiers)):
            raise LifecycleError("candidate names collapse to duplicate AutoTLDR identifiers")
        if self.candidate_recovery_path.exists():
            raise ReconciliationError(
                f"unresolved candidate recovery evidence exists at "
                f"{self.candidate_recovery_path}; reconcile it before another run"
            )

        link_snapshot = self._link_status()
        initial_rows = self._ps()
        initial_local = self._local_llms(initial_rows)
        initial_linked_state = self._linked_state(initial_rows)
        self._write_link_snapshot(link_snapshot)
        incumbent_snapshot: IncumbentSnapshot | None = None
        failure: BaseException | None = None
        try:
            if initial_local:
                resident = initial_local[0]
                if not incumbent_identifier or resident.identifier != incumbent_identifier:
                    raise LifecycleError(
                        f"pre-existing local identifier {resident.identifier!r} is not "
                        "owned by this transaction; pass its exact identifier with "
                        "--incumbent to authorize snapshot, unload, and restoration"
                    )
                proposed_snapshot = self._snapshot_from_resident(
                    resident, link_snapshot
                )
                self._write_snapshot(proposed_snapshot)
                incumbent_snapshot = proposed_snapshot
                self._unload_safe(link_snapshot, proposed_snapshot.fingerprint)
                self._require_zero_local()

            for candidate in candidates:
                self._require_zero_local()
                # ``lms ls`` is preference-sensitive under LM Link.  Resolve
                # the installed path only while the ZBook is explicitly the
                # preferred device, otherwise a localhost CLI invocation can
                # return a peer's catalog row and make a remote model look
                # locally installed.
                model_ref = self._on_zbook(
                    link_snapshot,
                    f"ZBook-local catalog resolution for {candidate.name!r}",
                    lambda candidate=candidate: self._resolve_local_catalog_model(
                        candidate.model
                    ),
                )
                state = CandidateLifecycleState(candidate, model_ref)
                candidate_failure: BaseException | None = None
                try:
                    self._estimate(
                        link_snapshot,
                        model_ref,
                        context_length=self.context_length,
                        parallel=self.parallel,
                    )
                    fingerprint = self._load_candidate(
                        link_snapshot, model_ref, candidate, state
                    )
                    if state.attestation is None:
                        raise ResidencyAttestationError(
                            "candidate load returned without a residency attestation"
                        )
                    self._evaluate(
                        candidate,
                        link_snapshot,
                        fingerprint,
                        state.attestation,
                    )
                except BaseException as exc:
                    candidate_failure = exc
                try:
                    self._cleanup_candidate(link_snapshot, state)
                except BaseException as cleanup_exc:
                    if candidate_failure is not None:
                        raise LifecycleError(
                            f"candidate {candidate.name!r} failed ({candidate_failure}); "
                            f"independent cleanup also failed ({cleanup_exc})"
                        ) from cleanup_exc
                    raise
                if candidate_failure is not None:
                    raise candidate_failure
        except BaseException as exc:  # restoration must also happen on Ctrl-C
            failure = exc

        if incumbent_snapshot is not None:
            try:
                self._restore_incumbent(incumbent_snapshot, link_snapshot)
            except BaseException as restore_exc:
                if failure is not None:
                    failure = LifecycleError(
                        f"pilot failed ({failure}); incumbent restoration also failed "
                        f"({restore_exc}); recovery snapshot remains at {self.snapshot_path}"
                    )
                else:
                    failure = restore_exc

        try:
            self._restore_link_preference(link_snapshot)
            final_link = self._link_status(
                timeout_seconds=self.timeouts.audit_seconds
            )
            final_linked_state = self._linked_state(
                self._ps(timeout_seconds=self.timeouts.audit_seconds)
            )
            if final_link.raw != link_snapshot.raw:
                raise LifecycleError(
                    "LM Link status did not return to its exact initial state"
                )
            if final_linked_state != initial_linked_state:
                raise LifecycleError(
                    "linked/Dynamo resident rows changed during the lifecycle"
                )
        except BaseException as link_restore_exc:
            if failure is not None:
                failure = LifecycleError(
                    f"pilot lifecycle failed ({failure}); final LM Link preference "
                    f"restoration also failed ({link_restore_exc}); recovery snapshot "
                    f"remains at {self.link_snapshot_path}"
                )
            else:
                failure = link_restore_exc
        else:
            self.link_snapshot_path.unlink(missing_ok=True)

        if failure is not None:
            raise failure

    def dry_run_commands(
        self,
        candidates: Sequence[Candidate],
        *,
        incumbent_identifier: str | None = None,
    ) -> list[str]:
        """Render a no-I/O plan; dynamic checks are explicit pseudo-steps."""

        if not candidates:
            raise LifecycleError("at least one candidate is required")
        lines = [
            shlex.join(
                [self.lms_executable, "link", "status", "--json"]
            ),
            (
                "# persist top-level deviceIdentifier as ZBOOK_DEVICE_ID and "
                "preferredDeviceIdentifier as ORIGINAL_PREFERRED_DEVICE_ID"
            ),
            shlex.join([self.lms_executable, "ps", "--json"]),
            "# assert <=1 ZBook-local LLM and zero ZBook-local embeddings; ignore LM Link rows",
        ]
        if incumbent_identifier:
            lines.extend(
                [
                    f"# if resident identifier is {incumbent_identifier!r}: persist exact snapshot",
                    (
                        "# set/verify ZBOOK_DEVICE_ID, re-read exact local PS row, "
                        "then unload"
                    ),
                    shlex.join([self.lms_executable, "unload", incumbent_identifier]),
                    (
                        "# verify zero ZBook-local residents, then restore/verify "
                        "ORIGINAL_PREFERRED_DEVICE_ID"
                    ),
                ]
            )
        else:
            lines.append("# refuse any pre-existing non-autotldr identifier")
        for candidate in candidates:
            lines.extend(
                [
                    shlex.join(
                        [
                            self.lms_executable,
                            "link",
                            "set-preferred-device",
                            "ZBOOK_DEVICE_ID",
                        ]
                    ),
                    "# verify ZBOOK_DEVICE_ID before catalog inspection",
                    shlex.join([self.lms_executable, "ls", "--json"]),
                    f"# resolve {candidate.model!r} to exactly one non-linked local LLM path",
                    shlex.join(
                        [
                            self.lms_executable,
                            "link",
                            "set-preferred-device",
                            "ORIGINAL_PREFERRED_DEVICE_ID",
                        ]
                    ),
                    "# verify ORIGINAL_PREFERRED_DEVICE_ID after catalog inspection",
                    shlex.join(
                        [
                            self.lms_executable,
                            "link",
                            "set-preferred-device",
                            "ZBOOK_DEVICE_ID",
                        ]
                    ),
                    shlex.join(
                        [
                            self.lms_executable,
                            "load",
                            candidate.model,
                            "--estimate-only",
                            "--gpu",
                            GPU_MODE,
                            "--context-length",
                            str(self.context_length),
                            "--parallel",
                            str(self.parallel),
                            "--yes",
                        ]
                    ),
                    f"# require exactly one line: {GPU_OFFLOAD_LINE}",
                    shlex.join(
                        [
                            self.lms_executable,
                            "link",
                            "set-preferred-device",
                            "ORIGINAL_PREFERRED_DEVICE_ID",
                        ]
                    ),
                    "# verify original preference, then set ZBOOK_DEVICE_ID again for load",
                    shlex.join(
                        [
                            self.lms_executable,
                            "link",
                            "set-preferred-device",
                            "ZBOOK_DEVICE_ID",
                        ]
                    ),
                    shlex.join(
                        self._load_command(
                            candidate.model,
                            candidate.identifier,
                            context_length=self.context_length,
                            parallel=self.parallel,
                        )
                    ),
                    (
                        "# verify this is the only resident with deviceIdentifier:null; "
                        "never unload a linked row"
                    ),
                    shlex.join(
                        [
                            self.lms_executable,
                            "link",
                            "set-preferred-device",
                            "ORIGINAL_PREFERRED_DEVICE_ID",
                        ]
                    ),
                    "# re-read link status and refuse inference unless preference is restored",
                    shlex.join(self._evaluate_command(candidate)),
                    (
                        "# finally: set/verify ZBOOK_DEVICE_ID and re-read the exact "
                        "owned local PS row"
                    ),
                    shlex.join(
                        [self.lms_executable, "unload", candidate.identifier]
                    ),
                    (
                        "# verify zero local residents, restore/verify the original "
                        "preference, then permit the next candidate"
                    ),
                ]
            )
        if incumbent_identifier:
            lines.append("# finally: restore and verify every snapshotted incumbent setting")
        lines.extend(
            [
                shlex.join(
                    [
                        self.lms_executable,
                        "link",
                        "set-preferred-device",
                        "ORIGINAL_PREFERRED_DEVICE_ID",
                    ]
                ),
                "# finally: verify original preference and remove its recovery snapshot",
            ]
        )
        return lines


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--candidate",
        action="append",
        required=True,
        metavar="NAME=INSTALLED_MODEL",
        help="repeat once per sequential local candidate",
    )
    parser.add_argument(
        "--incumbent",
        help=(
            "exact pre-existing non-autotldr identifier to snapshot, temporarily "
            "unload, and restore"
        ),
    )
    parser.add_argument("--items", type=Path, default=DEFAULT_ITEMS)
    parser.add_argument("--prompt", type=Path, default=DEFAULT_PROMPT)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--lms-executable",
        default=DEFAULT_LMS_EXECUTABLE,
        help=(
            "ZBook-local LMS executable (or set AUTOTLDR_LMS_CLI; useful for "
            "the Windows lms.exe path when invoking from WSL)"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the command/check plan without invoking LM Studio or the evaluator",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        candidates = [parse_candidate(value) for value in args.candidate]
        runner = SequentialPilotRunner(
            items_path=args.items,
            prompt_path=args.prompt,
            policy_path=args.policy,
            output_dir=args.output_dir,
            lms_executable=args.lms_executable,
        )
        if args.dry_run:
            print("\n".join(runner.dry_run_commands(candidates, incumbent_identifier=args.incumbent)))
        else:
            runner.run(candidates, incumbent_identifier=args.incumbent)
    except LifecycleError as exc:
        print(f"local candidate lifecycle error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
