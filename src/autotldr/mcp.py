"""Local-only MCP stdio surface over :mod:`autotldr.api`.

Official protocol sources, retrieved 2026-08-30:
https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/stdio
https://github.com/modelcontextprotocol/ext-tasks/blob/main/specification/2026-07-28/tasks.md

Only the standard library is imported until a tool executes. This module does
not load, unload, or select a model and exposes no network listener. Sources are
confined to roots chosen when the server starts; prose uses the same configured
local-model policy as the CLI.
Modern requests use MCP 2026-07-28 per-request metadata; a narrow synchronous
2025-11-25 ``initialize`` fallback supports hosts from the prior protocol era.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

from ._version import __version__

PROTOCOL_VERSION = "2026-07-28"
LEGACY_PROTOCOL_VERSION = "2025-11-25"
TASKS_EXTENSION = "io.modelcontextprotocol/tasks"
TOOL_NAME = "autotldr_summarize"

_SERVER_INFO = {"name": "autotldr", "version": __version__}
_SERVER_META = {"io.modelcontextprotocol/serverInfo": _SERVER_INFO}
_TASK_TTL_MS = 86_400_000
_TASK_POLL_MS = 250
_MAX_MESSAGE_BYTES = 1_048_576
_MAX_SOURCES = 128
_MAX_SOURCE_CHARS = 4096
_MAX_BUDGET = 10_000_000
_DEFAULT_OUTPUT = "md"
_DEFAULT_BUDGET = 65_536
_STORE: _TaskStore | None = None
_STORE_LOCK = threading.Lock()

_TOOL_INPUT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "sources": {
            "type": "array",
            "items": {
                "type": "string",
                "minLength": 1,
                "maxLength": _MAX_SOURCE_CHARS,
            },
            "minItems": 1,
            "maxItems": _MAX_SOURCES,
            "uniqueItems": True,
            "description": "Local file or directory paths. URLs and stdin are refused.",
        },
        "output": {
            "type": "string",
            "enum": ["ansi", "md", "json", "jsonl"],
            "default": _DEFAULT_OUTPUT,
        },
        "budget": {
            "type": "integer",
            "minimum": 1,
            "maximum": _MAX_BUDGET,
            "default": _DEFAULT_BUDGET,
            "description": "Hard utf8-byte-v1 ceiling for rendered AutoTLDR output.",
        },
        "detail": {
            "type": "string",
            "enum": ["brief", "standard", "deep"],
            "description": "Answer detail; omitted means the configured default.",
        },
        "mode": {
            "type": "string",
            "enum": ["prose", "evidence"],
            "default": "prose",
            "description": "Configured local-model prose, or explicit model-off evidence.",
        },
        "allowEvidenceFallback": {
            "type": "boolean",
            "default": False,
            "description": "Return a labelled evidence map if prose synthesis fails.",
        },
    },
    "required": ["sources"],
    "additionalProperties": False,
}

_TOOL = {
    "name": TOOL_NAME,
    "title": "Summarize local sources with AutoTLDR",
    "description": (
        "Extract, fuse, and summarize root-scoped local files or collections with "
        "exact origins, configured local-model prose, and a hard output budget."
    ),
    "inputSchema": _TOOL_INPUT_SCHEMA,
    "annotations": {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
}


class _InvalidParams(ValueError):
    pass


class _InvalidRequest(ValueError):
    pass


class _MethodNotFound(LookupError):
    pass


class _MissingCapability(RuntimeError):
    pass


class _UnsupportedVersion(RuntimeError):
    def __init__(self, requested: str) -> None:
        self.requested = requested


class _TaskStore:
    """Owner-private, atomic task records that survive stdio reconnects."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._lock = threading.RLock()

    def create(self, record: dict[str, Any]) -> None:
        with self._locked():
            path = self._path(record["taskId"])
            if path.exists():  # pragma: no cover - cryptographically improbable
                raise RuntimeError("task identifier collision")
            self._write(path, record)

    def get(self, task_id: str) -> dict[str, Any]:
        _validate_task_id(task_id)
        with self._locked():
            return self._read(task_id)

    def update(self, task_id: str, **changes: Any) -> dict[str, Any]:
        _validate_task_id(task_id)
        with self._locked():
            record = self._read(task_id)
            record.update(changes)
            record["lastUpdatedAt"] = _timestamp()
            self._write(self._path(task_id), record)
            return record

    def _read(self, task_id: str) -> dict[str, Any]:
        path = self._path(task_id)
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
            raise KeyError(task_id) from exc
        if _now_ms() >= record["createdUnixMs"] + record["ttlMs"]:
            try:
                path.unlink()
            except OSError:
                pass
            raise KeyError(task_id)
        return record

    @contextmanager
    def _locked(self):
        """Serialize record replacement across detached worker processes."""

        import fcntl

        with self._lock:
            self._ensure_root()
            lock_fd = os.open(
                self.root / ".lock",
                os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
            try:
                os.fchmod(lock_fd, 0o600)
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                yield
            finally:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                finally:
                    os.close(lock_fd)

    def _ensure_root(self) -> None:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            self.root.chmod(0o700)
        except OSError:
            pass

    def _path(self, task_id: str) -> Path:
        return self.root / f"{task_id}.json"

    def _write(self, path: Path, record: dict[str, Any]) -> None:
        payload = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        fd, temporary = tempfile.mkstemp(prefix=".task-", dir=self.root)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise


class MCPServer:
    """Small JSON-RPC dispatcher with modern and legacy MCP eras."""

    def __init__(self, roots: tuple[Path, ...] | None = None) -> None:
        self._legacy_initialized = False
        self._roots = _configured_roots() if roots is None else _validate_roots(roots)

    def handle(self, message: Any) -> dict[str, Any] | None:
        request_id: Any = None
        try:
            request_id, method, params, notification = _validate_envelope(message)
            if notification:
                if method == "notifications/initialized":
                    self._legacy_initialized = True
                return None
            if method == "initialize":
                return self._initialize(request_id, params)

            if _has_modern_meta(params):
                meta = _validate_modern_meta(params)
                result = self._dispatch_modern(method, params, meta)
            elif self._legacy_initialized:
                result = self._dispatch_legacy(method, params)
            else:
                raise _InvalidParams(
                    "params._meta must include the MCP protocol version and "
                    "client capabilities"
                )
            return _success(request_id, result)
        except _MethodNotFound as exc:
            return _error(
                request_id, -32601, "Method not found", "METHOD_NOT_FOUND", str(exc)
            )
        except _MissingCapability:
            return _missing_capability_error(request_id)
        except _UnsupportedVersion as exc:
            return _unsupported_version_error(request_id, exc.requested)
        except _InvalidRequest as exc:
            return _error(
                request_id,
                -32600,
                "Invalid Request",
                "INVALID_REQUEST",
                str(exc),
            )
        except KeyError:
            return _error(
                request_id,
                -32602,
                "Invalid params",
                "TASK_NOT_FOUND",
                "The task does not exist or has expired.",
            )
        except _InvalidParams as exc:
            return _error(
                request_id, -32602, "Invalid params", "INVALID_PARAMS", str(exc)
            )
        except Exception:
            return _error(
                request_id,
                -32603,
                "Internal error",
                "INTERNAL_ERROR",
                "The MCP server could not complete the protocol request.",
            )

    def _initialize(self, request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        _require_keys(
            params,
            {"protocolVersion", "capabilities", "clientInfo", "_meta"},
            {"protocolVersion", "capabilities", "clientInfo"},
        )
        if not isinstance(params["protocolVersion"], str):
            raise _InvalidParams("protocolVersion must be a string")
        if not isinstance(params["capabilities"], dict) or not isinstance(
            params["clientInfo"], dict
        ):
            raise _InvalidParams("capabilities and clientInfo must be objects")
        self._legacy_initialized = True
        return _success(
            request_id,
            {
                "protocolVersion": LEGACY_PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": _SERVER_INFO,
                "instructions": (
                    "Use autotldr_summarize for configured-root local sources; "
                    "URLs are refused. Prose is the default; evidence mode is explicit."
                ),
            },
        )

    def _dispatch_modern(
        self, method: str, params: dict[str, Any], meta: dict[str, Any]
    ) -> dict[str, Any]:
        if method == "server/discover":
            _require_keys(params, {"_meta"}, {"_meta"})
            return {
                "resultType": "complete",
                "supportedVersions": [PROTOCOL_VERSION],
                "capabilities": {
                    "tools": {"listChanged": False},
                    "extensions": {TASKS_EXTENSION: {}},
                },
                "instructions": (
                    "Summarize paths within configured roots only. No remote source "
                    "or model endpoint is allowed."
                ),
                "ttlMs": 3_600_000,
                "cacheScope": "public",
                "_meta": _SERVER_META,
            }
        if method == "ping":
            _require_keys(params, {"_meta"}, {"_meta"})
            return _complete()
        if method == "tools/list":
            _require_keys(params, {"_meta", "cursor"}, {"_meta"})
            if "cursor" in params and not isinstance(params["cursor"], str):
                raise _InvalidParams("cursor must be a string")
            return {
                "resultType": "complete",
                "tools": [_TOOL],
                "_meta": _SERVER_META,
            }
        if method == "tools/call":
            _require_keys(
                params, {"_meta", "name", "arguments"}, {"_meta", "name"}
            )
            if params["name"] != TOOL_NAME:
                raise _MethodNotFound(f"unknown tool {params['name']!r}")
            arguments = _validate_arguments(params.get("arguments", {}), self._roots)
            if _should_defer(arguments["sources"]) and _supports_tasks(meta):
                return _create_task(arguments)
            return _execute_tool(arguments)
        if method == "tasks/get":
            _require_task_capability(meta)
            return _detailed_task(_task_store().get(_task_request(params)))
        if method == "tasks/cancel":
            _require_task_capability(meta)
            task_id = _task_request(params)
            record = _task_store().get(task_id)
            if record["status"] == "working":
                changes: dict[str, Any] = {"cancelRequested": True}
                if record.get("phase") == "queued":
                    changes.update(
                        status="cancelled", statusMessage="Cancelled before execution."
                    )
                else:
                    changes["statusMessage"] = (
                        "Cancellation requested; parser work may finish before "
                        "it is observed."
                    )
                _task_store().update(task_id, **changes)
            return _complete()
        if method == "tasks/update":
            _require_task_capability(meta)
            _require_keys(
                params,
                {"_meta", "taskId", "inputResponses"},
                {"_meta", "taskId", "inputResponses"},
            )
            task_id = _checked_task_id(params["taskId"])
            if not isinstance(params["inputResponses"], dict):
                raise _InvalidParams("inputResponses must be an object")
            _task_store().get(task_id)
            return _complete()
        raise _MethodNotFound(method)

    def _dispatch_legacy(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "ping":
            _require_keys(params, {"_meta"}, set())
            return {}
        if method == "tools/list":
            _require_keys(params, {"cursor", "_meta"}, set())
            return {"tools": [_TOOL]}
        if method == "tools/call":
            _require_keys(params, {"name", "arguments", "_meta"}, {"name"})
            if params["name"] != TOOL_NAME:
                raise _MethodNotFound(f"unknown tool {params['name']!r}")
            result = _execute_tool(
                _validate_arguments(params.get("arguments", {}), self._roots)
            )
            return _legacy_tool_result(result)
        raise _MethodNotFound(method)


def _validate_envelope(message: Any) -> tuple[Any, str, dict[str, Any], bool]:
    if not isinstance(message, dict):
        raise _InvalidRequest("JSON-RPC message must be an object")
    allowed = {"jsonrpc", "id", "method", "params"}
    missing = {"jsonrpc", "method"} - message.keys()
    extra = message.keys() - allowed
    if missing:
        raise _InvalidRequest(f"missing required field: {sorted(missing)[0]}")
    if extra:
        raise _InvalidRequest(f"unexpected field: {sorted(extra)[0]}")
    if message["jsonrpc"] != "2.0" or not isinstance(message["method"], str):
        raise _InvalidRequest("jsonrpc must be '2.0' and method must be a string")
    notification = "id" not in message
    request_id = message.get("id")
    if not notification and (
        isinstance(request_id, bool) or not isinstance(request_id, (str, int))
    ):
        raise _InvalidRequest("request id must be a string or integer")
    params = message.get("params", {})
    if not isinstance(params, dict):
        raise _InvalidParams("params must be an object")
    return request_id, message["method"], params, notification


def _validate_modern_meta(params: dict[str, Any]) -> dict[str, Any]:
    meta = params.get("_meta")
    if not isinstance(meta, dict):
        raise _InvalidParams("params._meta must be an object")
    version = meta.get("io.modelcontextprotocol/protocolVersion")
    capabilities = meta.get("io.modelcontextprotocol/clientCapabilities")
    if not isinstance(version, str) or not isinstance(capabilities, dict):
        raise _InvalidParams(
            "modern requests require protocolVersion and clientCapabilities metadata"
        )
    if version != PROTOCOL_VERSION:
        raise _UnsupportedVersion(version)
    return meta


def _has_modern_meta(params: dict[str, Any]) -> bool:
    meta = params.get("_meta")
    return isinstance(meta, dict) and "io.modelcontextprotocol/protocolVersion" in meta


def _supports_tasks(meta: dict[str, Any]) -> bool:
    capabilities = meta["io.modelcontextprotocol/clientCapabilities"]
    extensions = capabilities.get("extensions")
    return isinstance(extensions, dict) and isinstance(
        extensions.get(TASKS_EXTENSION), dict
    )


def _require_task_capability(meta: dict[str, Any]) -> None:
    if not _supports_tasks(meta):
        raise _MissingCapability


def _validate_arguments(
    arguments: Any, roots: tuple[Path, ...]
) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise _InvalidParams("tool arguments must be an object")
    _require_keys(
        arguments,
        {
            "sources",
            "output",
            "budget",
            "detail",
            "mode",
            "allowEvidenceFallback",
        },
        {"sources"},
    )
    if not roots:
        raise _InvalidParams(
            "the MCP server has no authorized source root; restart it with --root"
        )
    sources = arguments["sources"]
    if not isinstance(sources, list) or not 1 <= len(sources) <= _MAX_SOURCES:
        raise _InvalidParams(
            f"sources must contain between 1 and {_MAX_SOURCES} local paths"
        )
    if any(not isinstance(source, str) for source in sources):
        raise _InvalidParams("each source must be a string")
    if len(set(sources)) != len(sources):
        raise _InvalidParams("sources must not contain duplicates")
    for source in sources:
        if not source or len(source) > _MAX_SOURCE_CHARS:
            raise _InvalidParams("each source must be a non-empty bounded string")
        if "\x00" in source or source == "-":
            raise _InvalidParams("stdin and NUL-containing paths are not accepted")
        if "://" in source or source.startswith(("//", "\\\\")):
            raise _InvalidParams(
                "remote sources are not accepted by the local MCP surface"
            )
    sources = _authorize_sources(tuple(sources), roots)
    output = arguments.get("output", _DEFAULT_OUTPUT)
    if output not in {"ansi", "md", "json", "jsonl"}:
        raise _InvalidParams("output must be one of ansi, md, json, or jsonl")
    budget = arguments.get("budget", _DEFAULT_BUDGET)
    if (
        isinstance(budget, bool)
        or not isinstance(budget, int)
        or not 1 <= budget <= _MAX_BUDGET
    ):
        raise _InvalidParams(
            f"budget must be an integer between 1 and {_MAX_BUDGET}"
        )
    detail = arguments.get("detail")
    if detail is not None and detail not in {"brief", "standard", "deep"}:
        raise _InvalidParams("detail must be brief, standard, or deep")
    mode = arguments.get("mode", "prose")
    if mode not in {"prose", "evidence"}:
        raise _InvalidParams("mode must be prose or evidence")
    fallback = arguments.get("allowEvidenceFallback", False)
    if not isinstance(fallback, bool):
        raise _InvalidParams("allowEvidenceFallback must be a boolean")
    return {
        "sources": sources,
        "output": output,
        "budget": budget,
        "detail": detail,
        "mode": mode,
        "allowEvidenceFallback": fallback,
    }


def _validate_roots(values: tuple[Path, ...]) -> tuple[Path, ...]:
    roots: list[Path] = []
    for value in values:
        try:
            root = Path(value).expanduser().resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"MCP source root is unavailable: {value}") from exc
        if not root.is_dir():
            raise ValueError(f"MCP source root is not a directory: {root}")
        if root not in roots:
            roots.append(root)
    return tuple(roots)


def _configured_roots() -> tuple[Path, ...]:
    encoded = os.environ.get("AUTOTLDR_MCP_ROOTS")
    if not encoded:
        return ()
    return _validate_roots(tuple(Path(item) for item in encoded.split(os.pathsep) if item))


def _authorize_sources(
    sources: tuple[str, ...], roots: tuple[Path, ...]
) -> tuple[str, ...]:
    authorized: list[str] = []
    for source in sources:
        candidate = Path(source).expanduser()
        if candidate.is_absolute():
            resolved = candidate.resolve(strict=False)
        elif len(roots) == 1:
            resolved = (roots[0] / candidate).resolve(strict=False)
        else:
            matches = [
                resolved_candidate
                for root in roots
                if (resolved_candidate := (root / candidate).resolve(strict=False)).exists()
            ]
            if len(matches) != 1:
                raise _InvalidParams(
                    "a relative source must resolve in exactly one configured root; "
                    "use an absolute path when roots are ambiguous"
                )
            resolved = matches[0]
        if not any(resolved == root or resolved.is_relative_to(root) for root in roots):
            raise _InvalidParams("a source resolves outside the configured MCP roots")
        authorized.append(str(resolved))
    if len(set(authorized)) != len(authorized):
        raise _InvalidParams("sources resolve to duplicate paths")
    return tuple(authorized)


def _execute_tool(arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        from .api import summarize_product

        result = summarize_product(
            arguments["sources"],
            detail=arguments["detail"],
            mode=arguments["mode"],
            allow_evidence_fallback=arguments["allowEvidenceFallback"],
            output=arguments["output"],
            budget=arguments["budget"],
            cite=True,
            color=False,
        )
        rendered = result.rendered
        structured: dict[str, Any] = {
            "format": arguments["output"],
            "byteLength": len(rendered.encode("utf-8")),
            "budget": arguments["budget"],
            "estimator": "utf8-byte-v1",
            "mode": arguments["mode"],
            "detail": arguments["detail"],
        }
        if arguments["output"] == "json":
            structured["artifact"] = json.loads(rendered)
        return {
            "resultType": "complete",
            "content": [{"type": "text", "text": rendered}],
            "structuredContent": structured,
            "isError": False,
            "_meta": _SERVER_META,
        }
    except Exception as exc:
        code, message = _named_tool_error(exc)
        return {
            "resultType": "complete",
            "content": [{"type": "text", "text": f"{code}: {message}"}],
            "structuredContent": {"error": {"code": code, "message": message}},
            "isError": True,
            "_meta": _SERVER_META,
        }


def _named_tool_error(exc: Exception) -> tuple[str, str]:
    name = type(exc).__name__
    if isinstance(exc, FileNotFoundError):
        return "SOURCE_NOT_FOUND", "A requested local source was not found."
    if isinstance(exc, PermissionError):
        return "SOURCE_UNREADABLE", "A requested local source could not be read."
    if name == "UnsupportedFormat":
        kind = getattr(exc, "kind", "input")
        tier = getattr(exc, "tier", "unknown")
        return (
            "UNSUPPORTED_FORMAT",
            f"The {kind} format is a named Tier {tier} decline.",
        )
    if name == "UnknownFormat":
        return "UNKNOWN_FORMAT", "A source format could not be identified."
    if name == "BudgetTooSmall":
        return "BUDGET_TOO_SMALL", (
            f"Budget {getattr(exc, 'limit', '?')} cannot hold the minimum valid "
            f"{getattr(exc, 'output', 'requested')} envelope; retry with at least "
            f"{getattr(exc, 'required', '?')} bytes."
        )
    if name == "MissingOptionalDependency":
        dependency = getattr(exc, "dependency", "an optional parser")
        extra = getattr(exc, "extra", "all")
        return (
            "MISSING_OPTIONAL_DEPENDENCY",
            f"{dependency} is unavailable; install autotldr[{extra}].",
        )
    if name == "LocalModelUnavailable":
        return (
            "LOCAL_MODEL_UNAVAILABLE",
            "No configured loopback model is ready; run autotldr setup and doctor.",
        )
    if name in {
        "SynthesisError",
        "SynthesisTimeoutError",
        "InvalidModelResponse",
    }:
        return (
            "SYNTHESIS_FAILED",
            "The local model did not produce an acceptable grounded response.",
        )
    return (
        "PROCESSING_FAILED",
        "AutoTLDR could not process the local source safely.",
    )


def _should_defer(sources: tuple[str, ...]) -> bool:
    if len(sources) > 1:
        return True
    path = Path(sources[0])
    try:
        if path.is_dir():
            return True
    except OSError:
        pass
    suffixes = tuple(suffix.casefold() for suffix in path.suffixes)
    return suffixes[-1:] in {(".zip",), (".tar",)} or suffixes[-2:] in {
        (".tar", ".gz"),
        (".tar", ".bz2"),
        (".tar", ".xz"),
    }


def _create_task(arguments: dict[str, Any]) -> dict[str, Any]:
    task_id = secrets.token_urlsafe(24)
    now = _timestamp()
    record = {
        "taskId": task_id,
        "status": "working",
        "statusMessage": "Queued for local collection extraction.",
        "createdAt": now,
        "lastUpdatedAt": now,
        "createdUnixMs": _now_ms(),
        "ttlMs": _TASK_TTL_MS,
        "pollIntervalMs": _TASK_POLL_MS,
        "phase": "queued",
        "cancelRequested": False,
        "arguments": arguments,
    }
    _task_store().create(record)
    seed = _task_view(record)
    seed.update(resultType="task", _meta=_SERVER_META)
    try:
        worker_pid = _start_task_worker(task_id)
    except OSError:
        failed = _task_store().update(
            task_id,
            status="failed",
            statusMessage="The local task worker could not be started.",
            error={"code": -32603, "message": "Task worker could not start"},
        )
        seed = _task_view(failed)
        seed.update(resultType="task", _meta=_SERVER_META)
    else:
        _task_store().update(task_id, workerPid=worker_pid)
    return seed


def _start_task_worker(task_id: str) -> int:
    """Detach local parser work so stdio EOF does not destroy the task."""

    import subprocess

    environment = os.environ.copy()
    environment["AUTOTLDR_MCP_TASK_DIR"] = str(_task_store().root)
    process = subprocess.Popen(
        [sys.executable, "-m", "autotldr.mcp", "--run-task", task_id],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
        env=environment,
    )
    return process.pid


def _run_task(task_id: str) -> None:
    try:
        record = _task_store().get(task_id)
        if record["status"] == "cancelled" or record["cancelRequested"]:
            _task_store().update(
                task_id,
                status="cancelled",
                statusMessage="Cancelled before execution.",
            )
            return
        _task_store().update(
            task_id,
            phase="running",
            statusMessage="Extracting local collection sources.",
        )
        result = _execute_tool(record["arguments"])
        record = _task_store().get(task_id)
        if record["cancelRequested"]:
            _task_store().update(
                task_id,
                status="cancelled",
                statusMessage="Cancellation observed after local parser cleanup.",
            )
        else:
            _task_store().update(
                task_id,
                status="completed",
                statusMessage="Local AutoTLDR processing completed.",
                result=result,
            )
    except Exception:
        try:
            _task_store().update(
                task_id,
                status="failed",
                statusMessage="The task worker failed at the protocol boundary.",
                error={"code": -32603, "message": "Task worker failed"},
            )
        except Exception:
            pass


def _task_store() -> _TaskStore:
    global _STORE
    if _STORE is None:
        with _STORE_LOCK:
            if _STORE is None:
                configured = os.environ.get("AUTOTLDR_MCP_TASK_DIR")
                if configured:
                    root = Path(configured)
                else:
                    state = os.environ.get("XDG_STATE_HOME")
                    root = (
                        Path(state) if state else Path.home() / ".local" / "state"
                    ) / "autotldr" / "mcp-tasks"
                _STORE = _TaskStore(root)
    return _STORE


def _task_request(params: dict[str, Any]) -> str:
    _require_keys(params, {"_meta", "taskId"}, {"_meta", "taskId"})
    return _checked_task_id(params["taskId"])


def _checked_task_id(value: Any) -> str:
    if not isinstance(value, str):
        raise _InvalidParams("taskId must be a string")
    _validate_task_id(value)
    return value


def _validate_task_id(value: str) -> None:
    allowed = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    if not 20 <= len(value) <= 128 or any(char not in allowed for char in value):
        raise _InvalidParams("taskId is malformed")


def _detailed_task(record: dict[str, Any]) -> dict[str, Any]:
    result = _task_view(record)
    result.update(resultType="complete", _meta=_SERVER_META)
    if record["status"] == "completed":
        result["result"] = record["result"]
    elif record["status"] == "failed":
        result["error"] = record["error"]
    return result


def _task_view(record: dict[str, Any]) -> dict[str, Any]:
    return {
        key: record[key]
        for key in (
            "taskId",
            "status",
            "statusMessage",
            "createdAt",
            "lastUpdatedAt",
            "ttlMs",
            "pollIntervalMs",
        )
    }


def _complete() -> dict[str, Any]:
    return {"resultType": "complete", "_meta": _SERVER_META}


def _legacy_tool_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in result.items()
        if key not in {"resultType", "_meta"}
    }


def _require_keys(
    value: dict[str, Any], allowed: set[str], required: set[str]
) -> None:
    missing = required - value.keys()
    extra = value.keys() - allowed
    if missing:
        raise _InvalidParams(f"missing required field: {sorted(missing)[0]}")
    if extra:
        raise _InvalidParams(f"unexpected field: {sorted(extra)[0]}")


def _success(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(
    request_id: Any, number: int, message: str, name: str, detail: str
) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {
            "code": number,
            "message": message,
            "data": {"code": name, "detail": detail},
        },
    }


def _missing_capability_error(request_id: Any) -> dict[str, Any]:
    response = _error(
        request_id,
        -32021,
        "Missing required client capability",
        "MISSING_REQUIRED_CLIENT_CAPABILITY",
        "The Tasks extension must be declared on this request.",
    )
    response["error"]["data"]["requiredCapabilities"] = {
        "extensions": {TASKS_EXTENSION: {}}
    }
    return response


def _unsupported_version_error(
    request_id: Any, requested: str
) -> dict[str, Any]:
    response = _error(
        request_id,
        -32022,
        "Unsupported protocol version",
        "UNSUPPORTED_PROTOCOL_VERSION",
        "Use one of the advertised MCP protocol versions.",
    )
    response["error"]["data"].update(
        supported=[PROTOCOL_VERSION], requested=requested
    )
    return response


def _timestamp() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def serve(
    stdin: BinaryIO, stdout: BinaryIO, *, roots: tuple[Path, ...] = ()
) -> None:
    server = MCPServer(roots=roots)
    while True:
        line = stdin.readline(_MAX_MESSAGE_BYTES + 1)
        if not line:
            return
        if len(line) > _MAX_MESSAGE_BYTES:
            while line and not line.endswith(b"\n"):
                line = stdin.readline(_MAX_MESSAGE_BYTES + 1)
            response = _error(
                None,
                -32600,
                "Invalid Request",
                "MESSAGE_TOO_LARGE",
                "MCP message exceeds 1 MiB.",
            )
        else:
            try:
                message = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                response = _error(
                    None,
                    -32700,
                    "Parse error",
                    "PARSE_ERROR",
                    "Input is not one complete UTF-8 JSON value.",
                )
            else:
                response = server.handle(message)
        if response is not None:
            payload = (
                json.dumps(
                    response, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
                + b"\n"
            )
            stdout.write(payload)
            stdout.flush()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) == 2 and argv[0] == "--run-task":
        try:
            _validate_task_id(argv[1])
        except _InvalidParams:
            return 2
        _run_task(argv[1])
        return 0

    import argparse

    parser = argparse.ArgumentParser(
        prog="autotldr mcp",
        description="Serve root-scoped AutoTLDR tools over local stdio MCP.",
    )
    parser.add_argument(
        "--root",
        action="append",
        required=True,
        type=Path,
        metavar="DIRECTORY",
        help="authorize one local source tree; repeat for additional roots",
    )
    args = parser.parse_args(argv)
    try:
        roots = _validate_roots(tuple(args.root))
    except ValueError as exc:
        parser.error(str(exc))
    serve(sys.stdin.buffer, sys.stdout.buffer, roots=roots)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
