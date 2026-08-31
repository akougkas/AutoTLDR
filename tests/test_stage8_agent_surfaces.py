"""Stage 8's thin, local agent surfaces.

Normative sources checked 2026-08-30:
- MCP 2026-07-28 stdio and Tasks:
  https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/stdio
  https://github.com/modelcontextprotocol/ext-tasks/blob/main/specification/2026-07-28/tasks.md
- A2A v1.0.1 AgentCard and discovery:
  https://github.com/a2aproject/A2A/blob/v1.0.1/specification/a2a.proto
  https://a2a-protocol.org/v1.0.1/specification/#8-agent-discovery-the-agent-card
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from autotldr import mcp

ROOT = Path(__file__).resolve().parents[1]


def _meta(*, tasks: bool = False, version: str = mcp.PROTOCOL_VERSION):
    capabilities = (
        {"extensions": {mcp.TASKS_EXTENSION: {}}} if tasks else {}
    )
    return {
        "io.modelcontextprotocol/protocolVersion": version,
        "io.modelcontextprotocol/clientInfo": {
            "name": "autotldr-tests",
            "version": "1",
        },
        "io.modelcontextprotocol/clientCapabilities": capabilities,
    }


def _request(server, method, params, request_id=1):
    return server.handle(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
    )


def _tool_call(server, arguments, *, tasks=False):
    arguments = {"mode": "evidence", **arguments}
    return _request(
        server,
        "tools/call",
        {
            "name": mcp.TOOL_NAME,
            "arguments": arguments,
            "_meta": _meta(tasks=tasks),
        },
    )


def _poll(server, task_id, expected):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        response = _request(
            server,
            "tasks/get",
            {"taskId": task_id, "_meta": _meta(tasks=True)},
        )
        if response["result"]["status"] == expected:
            return response["result"]
        time.sleep(0.01)
    raise AssertionError(f"task {task_id} did not reach {expected}")


def test_mcp_import_is_stdlib_only_and_keeps_public_pipeline_lazy():
    script = """
import sys
import autotldr.mcp
blocked = {
    'autotldr.api', 'numpy', 'pyarrow', 'openpyxl', 'fitz', 'duckdb',
    'h5py', 'netCDF4'
}
print(','.join(sorted(name for name in blocked if name in sys.modules)))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert result.stdout == "\n"


def test_modern_discovery_and_tool_schema_are_closed_and_task_capable():
    server = mcp.MCPServer()
    discovered = _request(
        server, "server/discover", {"_meta": _meta(tasks=True)}
    )["result"]

    assert discovered["resultType"] == "complete"
    assert discovered["supportedVersions"] == ["2026-07-28"]
    assert discovered["capabilities"]["extensions"] == {
        "io.modelcontextprotocol/tasks": {}
    }
    assert discovered["capabilities"]["tools"] == {"listChanged": False}

    listed = _request(server, "tools/list", {"_meta": _meta()})["result"]
    assert listed["resultType"] == "complete"
    assert [tool["name"] for tool in listed["tools"]] == [
        "autotldr_summarize"
    ]
    schema = listed["tools"][0]["inputSchema"]
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["sources"]
    assert schema["properties"]["output"]["enum"] == [
        "ansi",
        "md",
        "json",
        "jsonl",
    ]
    assert schema["properties"]["output"]["default"] == "md"
    assert schema["properties"]["budget"]["default"] == 65_536
    assert schema["properties"]["mode"]["default"] == "prose"
    assert schema["properties"]["detail"]["enum"] == [
        "brief",
        "standard",
        "deep",
    ]
    assert listed["tools"][0]["annotations"]["openWorldHint"] is False


def test_sync_tool_reuses_api_and_honors_the_rendered_budget(tmp_path):
    source = tmp_path / "notes.md"
    source.write_text("# Purpose\n\nThe worker validates signed jobs.\n")

    result = _tool_call(
        mcp.MCPServer(),
        {"sources": [str(source)], "output": "json", "budget": 12_000},
    )["result"]

    assert result["resultType"] == "complete"
    assert result["isError"] is False
    structured = result["structuredContent"]
    assert structured["byteLength"] <= structured["budget"] == 12_000
    assert structured["mode"] == "evidence"
    rendered = json.loads(result["content"][0]["text"])
    assert structured["artifact"] == rendered
    assert rendered["units"]
    assert all(unit["origin"]["source"] == str(source) for unit in rendered["units"])


@pytest.mark.parametrize(
    "arguments",
    [
        {"sources": ["notes.md"], "surprise": True},
        {"sources": ["notes.md"], "output": "html"},
        {"sources": ["notes.md"], "budget": True},
        {"sources": ["https://example.com/private"]},
        {"sources": ["//server/share/private.md"]},
        {"sources": ["\\\\server\\share\\private.md"]},
        {"sources": ["-"]},
        {"sources": ["notes.md", "notes.md"]},
    ],
)
def test_tool_validation_fails_closed_before_acquisition(arguments):
    response = _tool_call(mcp.MCPServer(), arguments)
    assert response["error"]["code"] == -32602
    assert response["error"]["data"]["code"] == "INVALID_PARAMS"


def test_named_tool_error_does_not_leak_an_absolute_path(tmp_path):
    missing = tmp_path / "private" / "does-not-exist.md"
    response = _tool_call(
        mcp.MCPServer(), {"sources": [str(missing)], "output": "json"}
    )
    result = response["result"]
    serialized = json.dumps(result)

    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "SOURCE_NOT_FOUND"
    assert str(missing) not in serialized
    assert str(tmp_path) not in serialized


def test_mcp_requires_roots_and_rejects_escape_and_symlink_escape(tmp_path):
    root = tmp_path / "authorized"
    root.mkdir()
    inside = root / "inside.md"
    inside.write_text("# Inside\n\nAuthorized evidence.\n")
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n\nPrivate evidence.\n")
    (root / "escape.md").symlink_to(outside)

    unconfigured = _tool_call(
        mcp.MCPServer(roots=()), {"sources": [str(inside)], "output": "json"}
    )
    assert unconfigured["error"]["data"]["code"] == "INVALID_PARAMS"
    assert "no authorized source root" in unconfigured["error"]["data"]["detail"]

    server = mcp.MCPServer(roots=(root,))
    accepted = _tool_call(
        server, {"sources": ["inside.md"], "output": "json", "budget": 12_000}
    )["result"]
    assert accepted["isError"] is False

    for source in (str(outside), "../outside.md", "escape.md"):
        rejected = _tool_call(server, {"sources": [source], "output": "json"})
        assert rejected["error"]["data"]["code"] == "INVALID_PARAMS"
        assert "outside the configured MCP roots" in rejected["error"]["data"]["detail"]


def test_mcp_default_prose_fails_actionably_without_a_configured_model(
    tmp_path, monkeypatch
):
    source = tmp_path / "notes.md"
    source.write_text("# Notes\n\nGrounded source.\n")
    monkeypatch.setenv("AUTOTLDR_CONFIG", str(tmp_path / "missing.toml"))

    response = _request(
        mcp.MCPServer(roots=(tmp_path,)),
        "tools/call",
        {
            "name": mcp.TOOL_NAME,
            "arguments": {"sources": ["notes.md"], "output": "json"},
            "_meta": _meta(),
        },
    )["result"]

    assert response["isError"] is True
    assert response["structuredContent"]["error"]["code"] == "LOCAL_MODEL_UNAVAILABLE"


def test_tasks_are_durable_pollable_results_for_long_collections(
    tmp_path, monkeypatch
):
    import autotldr.api as api

    entered = threading.Event()
    release = threading.Event()

    def fake_summarize(sources, **kwargs):
        entered.set()
        assert release.wait(2)
        assert tuple(sources) == ("/left.md", "/right.json")
        assert kwargs == {
            "detail": None,
            "mode": "evidence",
            "allow_evidence_fallback": False,
            "output": "json",
            "budget": 65_536,
            "cite": True,
            "color": False,
        }
        return SimpleNamespace(rendered='{"grounded":true}\n')

    store_root = tmp_path / "tasks"

    def inline_worker(task_id):
        threading.Thread(target=mcp._run_task, args=(task_id,), daemon=True).start()
        return 1

    monkeypatch.setattr(api, "summarize_product", fake_summarize)
    monkeypatch.setattr(mcp, "_STORE", mcp._TaskStore(store_root))
    monkeypatch.setattr(mcp, "_start_task_worker", inline_worker)
    server = mcp.MCPServer()

    created = _tool_call(
        server,
        {"sources": ["left.md", "right.json"], "output": "json"},
        tasks=True,
    )["result"]
    assert created["resultType"] == "task"
    assert created["status"] == "working"
    assert entered.wait(1)

    working = _request(
        server,
        "tasks/get",
        {"taskId": created["taskId"], "_meta": _meta(tasks=True)},
    )["result"]
    assert working["resultType"] == "complete"
    assert working["status"] == "working"
    assert "result" not in working

    task_file = store_root / f"{created['taskId']}.json"
    assert stat.S_IMODE(store_root.stat().st_mode) == 0o700
    assert stat.S_IMODE(task_file.stat().st_mode) == 0o600

    release.set()
    completed = _poll(server, created["taskId"], "completed")
    assert completed["result"]["isError"] is False
    assert completed["result"]["content"][0]["text"] == '{"grounded":true}\n'
    assert "output" not in completed["result"]["structuredContent"]


def test_task_cancellation_is_acknowledged_and_observed(tmp_path, monkeypatch):
    import autotldr.api as api

    entered = threading.Event()
    release = threading.Event()

    def slow_summarize(*_args, **_kwargs):
        entered.set()
        assert release.wait(2)
        return SimpleNamespace(rendered="done\n")

    monkeypatch.setattr(api, "summarize_product", slow_summarize)
    monkeypatch.setattr(mcp, "_STORE", mcp._TaskStore(tmp_path / "cancel"))

    def inline_worker(task_id):
        threading.Thread(target=mcp._run_task, args=(task_id,), daemon=True).start()
        return 1

    monkeypatch.setattr(mcp, "_start_task_worker", inline_worker)
    server = mcp.MCPServer()
    created = _tool_call(
        server, {"sources": ["a.md", "b.md"]}, tasks=True
    )["result"]
    assert entered.wait(1)

    cancelled = _request(
        server,
        "tasks/cancel",
        {"taskId": created["taskId"], "_meta": _meta(tasks=True)},
    )["result"]
    assert cancelled["resultType"] == "complete"
    release.set()
    final = _poll(server, created["taskId"], "cancelled")
    assert "result" not in final


def test_task_methods_require_per_request_capability_and_use_current_methods():
    server = mcp.MCPServer()
    response = _request(
        server,
        "tasks/get",
        {"taskId": "A" * 32, "_meta": _meta(tasks=False)},
    )
    assert response["error"]["code"] == -32021
    assert response["error"]["data"]["requiredCapabilities"] == {
        "extensions": {mcp.TASKS_EXTENSION: {}}
    }

    # MCP Tasks 2026-07-28 inlines the final result in tasks/get; there is no
    # tasks/result method from earlier experimental designs.
    obsolete = _request(
        server, "tasks/result", {"taskId": "A" * 32, "_meta": _meta(tasks=True)}
    )
    assert obsolete["error"]["code"] == -32601


def test_unsupported_modern_version_is_a_named_mcp_error():
    response = _request(
        mcp.MCPServer(),
        "server/discover",
        {"_meta": _meta(version="2099-01-01")},
    )
    assert response["error"]["code"] == -32022
    assert response["error"]["data"]["supported"] == ["2026-07-28"]
    assert response["error"]["data"]["requested"] == "2099-01-01"


def test_json_rpc_envelope_and_params_errors_remain_distinct():
    invalid_request = mcp.MCPServer().handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "ping",
            "params": {"_meta": _meta()},
            "unexpected": True,
        }
    )
    assert invalid_request["error"]["code"] == -32600
    assert invalid_request["error"]["data"]["code"] == "INVALID_REQUEST"

    invalid_params = _request(mcp.MCPServer(), "ping", ["not", "an", "object"])
    assert invalid_params["error"]["code"] == -32602


def test_legacy_initialize_fallback_is_synchronous_and_task_free():
    server = mcp.MCPServer()
    initialized = _request(
        server,
        "initialize",
        {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "legacy-test", "version": "1"},
        },
    )
    assert initialized["result"]["protocolVersion"] == "2025-11-25"
    listed = _request(server, "tools/list", {})["result"]
    assert "resultType" not in listed
    assert [tool["name"] for tool in listed["tools"]] == [mcp.TOOL_NAME]


def test_stdio_is_one_json_rpc_message_per_line():
    discover = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": "d1",
            "method": "server/discover",
            "params": {"_meta": _meta(tasks=True)},
        },
        separators=(",", ":"),
    )
    result = subprocess.run(
        [sys.executable, "-m", "autotldr.mcp", "--root", str(ROOT)],
        cwd=ROOT,
        input=f"not-json\n{discover}\n",
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 0
    assert result.stderr == ""
    messages = [json.loads(line) for line in result.stdout.splitlines()]
    assert messages[0]["error"]["code"] == -32700
    assert messages[1]["id"] == "d1"
    assert messages[1]["result"]["supportedVersions"] == ["2026-07-28"]


def test_detached_task_result_survives_stdio_reconnect(tmp_path):
    left = tmp_path / "left.md"
    right = tmp_path / "right.json"
    left.write_text("# Worker\n\nSee right.json for worker_limit.\n")
    right.write_text('{"worker_limit": 8}\n')
    task_dir = tmp_path / "durable-tasks"
    environment = os.environ.copy()
    environment["AUTOTLDR_MCP_TASK_DIR"] = str(task_dir)

    create = {
        "jsonrpc": "2.0",
        "id": "create",
        "method": "tools/call",
        "params": {
            "name": mcp.TOOL_NAME,
            "arguments": {
                "sources": [str(left), str(right)],
                "output": "json",
                "budget": 20_000,
                "mode": "evidence",
            },
            "_meta": _meta(tasks=True),
        },
    }
    launched = subprocess.run(
        [sys.executable, "-m", "autotldr.mcp", "--root", str(tmp_path)],
        cwd=ROOT,
        env=environment,
        input=json.dumps(create) + "\n",
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )
    assert launched.returncode == 0, launched.stderr
    created = json.loads(launched.stdout)["result"]
    assert created["resultType"] == "task"

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        get_task = {
            "jsonrpc": "2.0",
            "id": "poll",
            "method": "tasks/get",
            "params": {
                "taskId": created["taskId"],
                "_meta": _meta(tasks=True),
            },
        }
        polled = subprocess.run(
            [sys.executable, "-m", "autotldr.mcp", "--root", str(tmp_path)],
            cwd=ROOT,
            env=environment,
            input=json.dumps(get_task) + "\n",
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )
        assert polled.returncode == 0, polled.stderr
        result = json.loads(polled.stdout)["result"]
        if result["status"] == "completed":
            break
        time.sleep(0.02)
    else:
        raise AssertionError("detached task did not survive the original stdio process")

    assert result["result"]["isError"] is False
    rendered = json.loads(result["result"]["content"][0]["text"])
    assert len(rendered["manifest"]["inputs"]) == 2


def test_a2a_is_not_advertised_without_a_real_server():
    assert not (ROOT / ".well-known" / "agent-card.json").exists()


def test_skill_is_concise_instruction_only_and_has_required_ui_metadata():
    root = ROOT / "integrations" / "skills" / "autotldr"
    files = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
    }
    assert files == {"SKILL.md", "agents/openai.yaml"}
    skill = (root / "SKILL.md").read_text()
    assert len(skill.splitlines()) < 150
    assert "name: autotldr" in skill
    assert "TODO" not in skill
    metadata = (root / "agents" / "openai.yaml").read_text()
    assert 'display_name: "AutoTLDR"' in metadata
    assert (
        'short_description: "Grounded local summaries for files and folders"'
        in metadata
    )
    assert "$autotldr" in metadata
