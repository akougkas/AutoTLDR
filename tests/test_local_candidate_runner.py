"""Offline tests for exclusive ZBook-local LM Studio candidate residency."""

from __future__ import annotations

import importlib.util
import json
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest


RUNNER_PATH = (
    Path(__file__).resolve().parents[1]
    / "benchmarks"
    / "roles"
    / "run_local_candidates.py"
)
SPEC = importlib.util.spec_from_file_location("autotldr_local_candidates", RUNNER_PATH)
assert SPEC and SPEC.loader
local_candidates = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = local_candidates
SPEC.loader.exec_module(local_candidates)


def _model_row(
    model: str,
    *,
    identifier: str | None = None,
    device_identifier: str | None = None,
    model_type: str = "llm",
    context_length: int = 8192,
    parallel: int = 4,
    ttl_ms: int | None = None,
    path: str | None = None,
    size_bytes: int = 1_000_000,
    process_id: int = 101,
):
    return {
        "type": model_type,
        "modelKey": model,
        "path": path or model,
        "indexedModelIdentifier": path or model,
        "deviceIdentifier": device_identifier,
        "identifier": identifier,
        "contextLength": context_length,
        "parallel": parallel,
        "ttlMs": ttl_ms,
        "sizeBytes": size_bytes,
        "processId": process_id,
        "status": "idle",
    }


class FakeCommandRunner:
    """A tiny in-memory LM Studio; no subprocess or endpoint is touched."""

    def __init__(
        self,
        *,
        catalog,
        local=None,
        linked=(),
        offload=None,
        evaluator_returncode=0,
        local_device_identifier="zbook-device",
        preferred_device_identifier="dynamo-device",
        fail_preference_set_calls=(),
        after_evaluator=None,
        load_timeout=False,
        late_load_after_ps=None,
        additional_local=(),
        evaluator_exception=None,
        late_load_overrides=None,
        clock=None,
        advance_evaluator_timeout=False,
    ):
        self.catalog = list(catalog)
        self.local = dict(local) if local else None
        self.linked = [dict(row) for row in linked]
        self.offload = offload or {}
        self.evaluator_returncode = evaluator_returncode
        self.local_device_identifier = local_device_identifier
        self.preferred_device_identifier = preferred_device_identifier
        self.original_preferred_device_identifier = preferred_device_identifier
        self.fail_preference_set_calls = set(fail_preference_set_calls)
        self.preference_set_count = 0
        self.calls: list[tuple[str, ...]] = []
        self.preference_at_evaluation: list[str] = []
        self.preference_at_estimate: list[str] = []
        self.preference_at_unload: list[str] = []
        self.preference_at_catalog: list[str] = []
        self.requests: list[local_candidates.CommandRequest] = []
        self.after_evaluator = after_evaluator
        self.load_timeout = load_timeout
        self.late_load_after_ps = late_load_after_ps
        self.additional_local = [dict(row) for row in additional_local]
        self.evaluator_exception = evaluator_exception
        self.late_load_overrides = dict(late_load_overrides or {})
        self.pending_load = None
        self.pending_ps_count = 0
        self.clock = clock
        self.advance_evaluator_timeout = advance_evaluator_timeout

    @staticmethod
    def _option(argv, name, default=None):
        if name not in argv:
            return default
        return argv[argv.index(name) + 1]

    def __call__(self, request):
        assert isinstance(request, local_candidates.CommandRequest)
        assert request.deadline_ns > 0
        assert request.timeout_seconds > 0
        self.requests.append(request)
        argv = request.argv
        self.calls.append(argv)

        if argv[:4] == ("lms-test", "link", "status", "--json"):
            peers = []
            if self.original_preferred_device_identifier != self.local_device_identifier:
                peers.append(
                    {
                        "deviceIdentifier": self.original_preferred_device_identifier,
                        "deviceName": "remote-peer",
                        "status": "connected",
                        "loadedModels": [],
                    }
                )
            status = {
                "status": "online",
                "issues": [],
                "peers": peers,
                "deviceIdentifier": self.local_device_identifier,
                "deviceName": "zbook",
                "preferredDeviceIdentifier": self.preferred_device_identifier,
            }
            return local_candidates.CommandResult(0, json.dumps(status), "")

        if argv[:3] == ("lms-test", "link", "set-preferred-device"):
            self.preference_set_count += 1
            if self.preference_set_count in self.fail_preference_set_calls:
                return local_candidates.CommandResult(
                    9, "", "fake preference restoration failure"
                )
            requested = argv[3]
            known = {
                self.local_device_identifier,
                self.original_preferred_device_identifier,
            }
            if requested not in known:
                return local_candidates.CommandResult(2, "", "unknown fake device")
            self.preferred_device_identifier = requested
            return local_candidates.CommandResult(0, "Preferred device updated\n", "")

        if argv[:3] == ("lms-test", "ps", "--json"):
            rows = [*self.linked]
            if self.pending_load is not None:
                self.pending_ps_count += 1
                if (
                    self.late_load_after_ps is not None
                    and self.pending_ps_count >= self.late_load_after_ps
                ):
                    self.local = dict(self.pending_load, **self.late_load_overrides)
                    self.pending_load = None
            if self.local is not None:
                rows.append(self.local)
            rows.extend(self.additional_local)
            return local_candidates.CommandResult(0, json.dumps(rows), "")

        if argv[:3] == ("lms-test", "ls", "--json"):
            self.preference_at_catalog.append(self.preferred_device_identifier)
            return local_candidates.CommandResult(0, json.dumps(self.catalog), "")

        if argv[:2] == ("lms-test", "load"):
            model = argv[2]
            if "--estimate-only" in argv:
                self.preference_at_estimate.append(self.preferred_device_identifier)
                if self.preferred_device_identifier != self.local_device_identifier:
                    return local_candidates.CommandResult(
                        8, "", "fake estimate was routed away from ZBook"
                    )
                percent = self.offload.get(model, 100)
                return local_candidates.CommandResult(
                    0,
                    f"GPU Offload: {percent}%\nEstimated Total Memory: test\n",
                    "",
                )
            if self.local is not None:
                return local_candidates.CommandResult(
                    2, "", "fake LM Studio refuses a second local resident"
                )
            identifier = self._option(argv, "--identifier")
            context_length = int(self._option(argv, "--context-length"))
            parallel = int(self._option(argv, "--parallel"))
            raw_ttl = self._option(argv, "--ttl")
            ttl_ms = int(raw_ttl) * 1000 if raw_ttl is not None else None
            if self.preferred_device_identifier != self.local_device_identifier:
                return local_candidates.CommandResult(
                    8, "", "fake load was routed away from ZBook"
                )
            loaded = _model_row(
                model,
                identifier=identifier,
                context_length=context_length,
                parallel=parallel,
                ttl_ms=ttl_ms,
            )
            if self.load_timeout:
                self.pending_load = loaded
                raise local_candidates.CommandTimeout(
                    local_candidates.TimeoutRecord.build(
                        action=request.action,
                        argv=request.argv,
                        timeout_seconds=request.timeout_seconds,
                        terminate_process_group=request.terminate_process_group,
                    )
                )
            self.local = loaded
            return local_candidates.CommandResult(0, f'Model "{identifier}" loaded\n', "")

        if argv[:2] == ("lms-test", "unload"):
            identifier = argv[2]
            self.preference_at_unload.append(self.preferred_device_identifier)
            if self.preferred_device_identifier != self.local_device_identifier:
                return local_candidates.CommandResult(
                    8, "", "fake unload was routed away from ZBook"
                )
            if self.local is None or self.local["identifier"] != identifier:
                return local_candidates.CommandResult(
                    2, "", f"fake model {identifier!r} is not a local resident"
                )
            self.local = None
            return local_candidates.CommandResult(
                0, f'Model "{identifier}" unloaded\n', ""
            )

        if argv and argv[0] == "python-test":
            self.preference_at_evaluation.append(self.preferred_device_identifier)
            if self.advance_evaluator_timeout:
                assert self.clock is not None
                self.clock.now_ns = request.deadline_ns + 1
            if self.evaluator_exception is not None:
                raise self.evaluator_exception
            if self.after_evaluator is not None:
                self.after_evaluator(self)
            return local_candidates.CommandResult(
                self.evaluator_returncode,
                "{}\n" if self.evaluator_returncode == 0 else "",
                "fake evaluator failure" if self.evaluator_returncode else "",
            )

        raise AssertionError(f"unexpected command: {argv!r}")


class FakeResidencyAttestor:
    """Closed fake actual-residency probe bound to the in-memory process."""

    def __init__(self, fake, **overrides):
        self.fake = fake
        self.overrides = overrides
        self.calls = []

    def __call__(self, fingerprint, *, deadline_ns):
        assert deadline_ns > 0
        assert self.fake.local is not None
        self.calls.append(fingerprint.sha256)
        record = {
            "schema": local_candidates.ATTESTATION_SCHEMA,
            "complete": True,
            "resident_fingerprint_sha256": fingerprint.sha256,
            "local_device_identifier": fingerprint.local_device_identifier,
            "resident_identifier": fingerprint.identifier,
            "expected_model_ref": fingerprint.expected_model_ref,
            "process_id": self.fake.local["processId"],
            "process_start_time_ns": self.fake.local["processId"] * 1_000_000,
            "process_executable_sha256": "a" * 64,
            "gpu_layers_request": "max",
            "cpu_moe_layers": 0,
            "kv_cache_on_gpu": True,
            "gpu_allocation_bytes": fingerprint.size_bytes + 64_000,
            "model_size_bytes": fingerprint.size_bytes,
            "offloaded_layers": 41,
            "total_layers": 41,
        }
        overrides = dict(self.overrides)
        drop_key = overrides.pop("__drop_key__", None)
        record.update(overrides)
        if drop_key is not None:
            record.pop(drop_key)
        return record


class FakeRuntimeConfigurationProbe:
    """Declares and reproduces every runtime setting exposed by the fake."""

    def __init__(
        self,
        lms_executable="lms-test",
        *,
        complete=True,
        exact_restorable=True,
        drift_after_first=False,
        secret_setting=False,
    ):
        self.lms_executable = lms_executable
        self.complete = complete
        self.exact_restorable = exact_restorable
        self.drift_after_first = drift_after_first
        self.secret_setting = secret_setting
        self.calls = []

    def __call__(self, fingerprint, *, deadline_ns):
        assert deadline_ns > 0
        payload = json.loads(fingerprint.canonical_json)
        raw = payload["resident"]["raw"]
        settings = {
            "context_length": raw["contextLength"],
            "parallel": raw["parallel"],
            "ttl_ms": raw["ttlMs"],
            "gpu": "max",
            "flash_attention": raw.get("flashAttention"),
            "kv_cache_on_gpu": raw.get("offloadKVCacheToGpu", True),
        }
        if self.drift_after_first and self.calls:
            settings["flash_attention"] = "drifted"
        if self.secret_setting:
            settings["api_key"] = "never-store"
        argv = [
            self.lms_executable,
            "load",
            fingerprint.expected_model_ref,
            "--identifier",
            fingerprint.identifier,
            "--context-length",
            str(raw["contextLength"]),
            "--parallel",
            str(raw["parallel"]),
            "--gpu",
            "max",
        ]
        if raw["ttlMs"] is not None:
            argv.extend(["--ttl", str(raw["ttlMs"] // 1000)])
        self.calls.append(fingerprint.sha256)
        return {
            "schema": local_candidates.RUNTIME_CONFIG_SCHEMA,
            "source": "fake-complete-runtime-v1",
            "complete": self.complete,
            "exact_restorable": self.exact_restorable,
            "resident_fingerprint_sha256": fingerprint.sha256,
            "settings": settings,
            "restore_argv": argv,
        }


def _runner(tmp_path, fake, **kwargs):
    return local_candidates.SequentialPilotRunner(
        command_runner=fake,
        lms_executable="lms-test",
        python_executable="python-test",
        evaluator=Path("/repo/evaluate.py"),
        items_path=Path("/repo/pilot/items.jsonl"),
        prompt_path=Path("/repo/prompt.md"),
        policy_path=Path("/repo/policy.json"),
        output_dir=tmp_path / "predictions",
        residency_attestor=FakeResidencyAttestor(fake),
        runtime_configuration_probe=FakeRuntimeConfigurationProbe(),
        **kwargs,
    )


def _loads(fake):
    return [
        call
        for call in fake.calls
        if call[:2] == ("lms-test", "load") and "--estimate-only" not in call
    ]


def _estimates(fake):
    return [call for call in fake.calls if "--estimate-only" in call]


def _evaluations(fake):
    return [call for call in fake.calls if call and call[0] == "python-test"]


def test_runs_two_candidates_sequentially_and_never_acts_on_linked_rows(tmp_path):
    first = "ibm/granite-4.2-3b-q8.gguf"
    second = "openbmb/MiniCPM-V-4_6-F16.gguf"
    linked = _model_row(
        "qwen-on-dynamo",
        identifier="qwen3.8-27b-dynamo",
        device_identifier="3cb326b0-linked-device",
        path="3cb326b0-linked-device:qwen-on-dynamo",
    )
    fake = FakeCommandRunner(
        catalog=[_model_row(first), _model_row(second), linked],
        linked=[linked],
    )
    runner = _runner(tmp_path, fake)

    runner.run(
        [
            local_candidates.Candidate("Granite 4.2 3B", first),
            local_candidates.Candidate("MiniCPM V 4.6", second),
        ]
    )

    assert fake.local is None
    assert fake.preferred_device_identifier == "dynamo-device"
    assert fake.preference_at_evaluation == ["dynamo-device", "dynamo-device"]
    assert fake.preference_at_estimate == ["zbook-device", "zbook-device"]
    assert fake.preference_at_unload == ["zbook-device", "zbook-device"]
    assert fake.preference_at_catalog == ["zbook-device", "zbook-device"]
    assert not runner.link_snapshot_path.exists()
    loads = _loads(fake)
    assert [call[2] for call in loads] == [first, second]
    assert all(call[call.index("--gpu") + 1] == "max" for call in loads)
    assert all(call[call.index("--context-length") + 1] == "8192" for call in loads)
    assert all(call[call.index("--parallel") + 1] == "4" for call in loads)
    assert [call[2] for call in fake.calls if call[:2] == ("lms-test", "unload")] == [
        "autotldr-granite-4-2-3b",
        "autotldr-minicpm-v-4-6",
    ]
    assert not any("qwen3.8-27b-dynamo" in call for call in fake.calls if call[:2] in {
        ("lms-test", "load"),
        ("lms-test", "unload"),
    })

    evaluations = _evaluations(fake)
    assert len(evaluations) == 2
    for command, identifier in zip(
        evaluations,
        ("autotldr-granite-4-2-3b", "autotldr-minicpm-v-4-6"),
        strict=True,
    ):
        assert command[command.index("--base-url") + 1] == "http://127.0.0.1:1234/v1"
        assert command[command.index("--model") + 1] == identifier
        assert "--pilot" in command
        assert "--labels" not in command

    # Each estimate is made for the actual load shape and must precede its load.
    assert len(_estimates(fake)) == 2
    for estimate, load in zip(_estimates(fake), loads, strict=True):
        assert fake.calls.index(estimate) < fake.calls.index(load)
        assert estimate[estimate.index("--gpu") + 1] == "max"


def test_refuses_any_estimate_below_exactly_100_percent_and_does_not_load(tmp_path):
    model = "ibm/granite-4.2-8b-q4km.gguf"
    fake = FakeCommandRunner(
        catalog=[_model_row(model)],
        offload={model: 99},
    )

    with pytest.raises(local_candidates.LifecycleError, match="GPU Offload: 100%"):
        _runner(tmp_path, fake).run([local_candidates.Candidate("Granite 8B", model)])

    assert fake.local is None
    assert not _loads(fake)
    assert not _evaluations(fake)
    assert fake.preferred_device_identifier == "dynamo-device"
    assert not [call for call in fake.calls if call[:2] == ("lms-test", "unload")]


class _StderrEstimateRunner(FakeCommandRunner):
    """The real `lms load --estimate-only` reports on stderr, not stdout."""

    def __call__(self, request):
        result = super().__call__(request)
        argv = tuple(request.argv)
        if argv[:2] == ("lms-test", "load") and "--estimate-only" in argv:
            return local_candidates.CommandResult(
                result.returncode, "", result.stdout + result.stderr
            )
        return result


def test_offload_gate_accepts_an_estimate_reported_only_on_stderr(tmp_path):
    """Reading stdout alone turned a satisfied 100% gate into a refusal."""

    model = "ibm/granite-4.2-8b-q4km.gguf"
    fake = _StderrEstimateRunner(catalog=[_model_row(model)])

    _runner(tmp_path, fake).run([local_candidates.Candidate("Granite 8B", model)])

    assert len(_loads(fake)) == 1
    assert _loads(fake)[0][_loads(fake)[0].index("--gpu") + 1] == "max"
    assert _evaluations(fake)


def test_offload_gate_still_refuses_a_partial_estimate_reported_on_stderr(tmp_path):
    model = "ibm/granite-4.2-8b-q4km.gguf"
    fake = _StderrEstimateRunner(catalog=[_model_row(model)], offload={model: 99})

    with pytest.raises(local_candidates.LifecycleError, match="GPU Offload: 100%"):
        _runner(tmp_path, fake).run([local_candidates.Candidate("Granite 8B", model)])

    assert not _loads(fake)


def test_refuses_a_linked_catalog_match_before_estimate_or_load(tmp_path):
    model = "shared/catalog-name.gguf"
    linked = _model_row(
        model,
        device_identifier="3cb326b0-linked-device",
        path="3cb326b0-linked-device:shared/catalog-name.gguf",
    )
    fake = FakeCommandRunner(catalog=[linked], linked=[linked])

    with pytest.raises(local_candidates.LifecycleError, match="LM Link row"):
        _runner(tmp_path, fake).run([local_candidates.Candidate("Remote", model)])

    assert not _estimates(fake)
    assert not _loads(fake)
    assert fake.preferred_device_identifier == "dynamo-device"
    assert not [call for call in fake.calls if call[:2] == ("lms-test", "unload")]


def test_refuses_an_ambiguous_model_key_shared_by_local_and_linked_catalog_rows(
    tmp_path,
):
    model = "shared/catalog-name.gguf"
    linked = _model_row(
        model,
        device_identifier="remote-device",
        path="remote-device:shared/catalog-name.gguf",
    )
    fake = FakeCommandRunner(
        catalog=[_model_row(model), linked],
        linked=[linked],
    )

    with pytest.raises(local_candidates.LifecycleError, match="also resolves"):
        _runner(tmp_path, fake).run([local_candidates.Candidate("Ambiguous", model)])

    assert not _estimates(fake)
    assert not _loads(fake)
    assert not _evaluations(fake)
    assert not [call for call in fake.calls if call[:2] == ("lms-test", "unload")]


def test_refuses_to_unload_a_preexisting_non_owned_model_without_authority(tmp_path):
    incumbent = _model_row(
        "ornith/model.gguf",
        identifier="ornith-1.5-35b-a3b",
        context_length=262144,
        parallel=4,
        ttl_ms=7_200_000,
    )
    fake = FakeCommandRunner(catalog=[], local=incumbent)

    with pytest.raises(local_candidates.LifecycleError, match="--incumbent"):
        _runner(tmp_path, fake).run(
            [local_candidates.Candidate("Granite", "granite/model.gguf")]
        )

    assert fake.local == incumbent
    assert not [call for call in fake.calls if call[:2] == ("lms-test", "unload")]


def test_explicit_incumbent_is_restored_exactly_even_if_evaluation_fails(tmp_path):
    candidate_model = "ibm/granite/model.gguf"
    incumbent = _model_row(
        "ornith/model.gguf",
        identifier="ornith-1.5-35b-a3b",
        context_length=262144,
        parallel=4,
        ttl_ms=7_200_000,
    )
    fake = FakeCommandRunner(
        catalog=[_model_row(candidate_model)],
        local=incumbent,
        evaluator_returncode=7,
    )
    runner = _runner(tmp_path, fake)

    with pytest.raises(local_candidates.LifecycleError, match="fake evaluator failure"):
        runner.run(
            [local_candidates.Candidate("Granite", candidate_model)],
            incumbent_identifier="ornith-1.5-35b-a3b",
        )

    assert fake.local is not None
    assert fake.local["identifier"] == "ornith-1.5-35b-a3b"
    assert fake.local["modelKey"] == "ornith/model.gguf"
    assert fake.local["contextLength"] == 262144
    assert fake.local["parallel"] == 4
    assert fake.local["ttlMs"] == 7_200_000
    assert fake.preferred_device_identifier == "dynamo-device"
    assert fake.preference_at_evaluation == ["dynamo-device"]
    assert fake.preference_at_unload == ["zbook-device", "zbook-device"]
    assert not runner.snapshot_path.exists()
    assert not runner.link_snapshot_path.exists()

    unloads = [call[2] for call in fake.calls if call[:2] == ("lms-test", "unload")]
    assert unloads == ["ornith-1.5-35b-a3b", "autotldr-granite"]
    loads = _loads(fake)
    assert [call[2] for call in loads] == [candidate_model, "ornith/model.gguf"]
    restore = loads[-1]
    assert restore[restore.index("--identifier") + 1] == "ornith-1.5-35b-a3b"
    assert restore[restore.index("--context-length") + 1] == "262144"
    assert restore[restore.index("--parallel") + 1] == "4"
    assert restore[restore.index("--ttl") + 1] == "7200"
    # Restore is a load too, so it receives its own 100%-offload gate.
    assert [estimate[2] for estimate in _estimates(fake)] == [
        candidate_model,
        "ornith/model.gguf",
    ]


def test_incumbent_recovery_uses_cli_model_key_and_keeps_path_in_fingerprint(tmp_path) -> None:
    local_path = (
        "ornith-ai/Ornith-1.5-35B-A3B-GGUF/"
        "Ornith-1.5-35B-Q4_K_M.gguf"
    )
    row = _model_row(
        "ornith-1.5-35b-a3b",
        identifier="ornith-1.5-35b-a3b",
        path=local_path,
        context_length=262144,
        parallel=4,
    )
    fake = FakeCommandRunner(catalog=[], local=row)
    runner = _runner(tmp_path, fake)
    resident = local_candidates.parse_models(json.dumps([row]), "lms ps --json")[0]
    snapshot = runner._snapshot_from_resident(resident, runner._link_status())

    assert snapshot.model_ref == row["modelKey"]
    assert local_path in snapshot.fingerprint.canonical_json


def test_exact_local_catalog_path_loads_via_noninteractive_model_key(tmp_path) -> None:
    local_path = "ibm-granite/granite-4.2-8b-GGUF/granite-4.2-8b-Q4_K_M.gguf"
    row = _model_row("granite-4.2-8b", path=local_path)
    fake = FakeCommandRunner(catalog=[row])

    _runner(tmp_path, fake).run(
        [local_candidates.Candidate("Granite", local_path)]
    )

    loads = _loads(fake)
    assert loads[0][2] == "granite-4.2-8b"
    assert "--yes" in loads[0]
    assert _estimates(fake)[0][2] == "granite-4.2-8b"
    assert "--yes" in _estimates(fake)[0]


def test_failed_post_load_preference_restoration_prevents_inference_and_cleans_up(
    tmp_path,
):
    model = "ibm/granite/model.gguf"
    # Preference set calls are: local/original for catalog resolution (1/2),
    # estimate (3/4), then load (5/6).  Fail restoration after the load.  The
    # outer finally must retry restoration after unloading the owned candidate.
    fake = FakeCommandRunner(
        catalog=[_model_row(model)],
        fail_preference_set_calls={6},
    )
    runner = _runner(tmp_path, fake)

    with pytest.raises(
        local_candidates.LifecycleError,
        match="preference restoration failed",
    ):
        runner.run([local_candidates.Candidate("Granite", model)])

    assert fake.local is None
    assert fake.preferred_device_identifier == "dynamo-device"
    assert not _evaluations(fake)
    assert [call[2] for call in fake.calls if call[:2] == ("lms-test", "unload")] == [
        "autotldr-granite"
    ]
    assert not runner.link_snapshot_path.exists()


def test_failed_unload_preference_restoration_stops_before_the_next_candidate(
    tmp_path,
):
    first = "ibm/granite/model.gguf"
    second = "openbmb/minicpm/model.gguf"
    # First candidate: catalog set/restore (1/2), estimate (3/4), load (5/6),
    # unload (7/8).  Fail restoration after the local unload.  The outer
    # finally retries it, but the second candidate must never start.
    fake = FakeCommandRunner(
        catalog=[_model_row(first), _model_row(second)],
        fail_preference_set_calls={8},
    )
    runner = _runner(tmp_path, fake)

    with pytest.raises(
        local_candidates.LifecycleError,
        match="local unload.*preference restoration failed",
    ):
        runner.run(
            [
                local_candidates.Candidate("Granite", first),
                local_candidates.Candidate("MiniCPM", second),
            ]
        )

    assert fake.local is None
    assert fake.preferred_device_identifier == "dynamo-device"
    assert fake.preference_at_unload == ["zbook-device"]
    assert len(_evaluations(fake)) == 1
    assert _evaluations(fake)[0][
        _evaluations(fake)[0].index("--model") + 1
    ] == "autotldr-granite"
    assert [call[2] for call in _loads(fake)] == [first]
    assert not runner.link_snapshot_path.exists()


def test_local_embedding_resident_is_a_hard_stop_and_is_not_unloaded(tmp_path):
    embedding = _model_row(
        "embedding/model.gguf",
        identifier="embedding-model",
        model_type="embedding",
    )
    fake = FakeCommandRunner(catalog=[], local=embedding)

    with pytest.raises(local_candidates.LifecycleError, match="forbids local embedding"):
        _runner(tmp_path, fake).run(
            [local_candidates.Candidate("Granite", "granite/model.gguf")]
        )

    assert fake.local == embedding
    assert not [call for call in fake.calls if call[:2] == ("lms-test", "unload")]


def test_dry_run_is_no_io_and_names_every_fixed_runtime_setting(tmp_path):
    def forbidden(_argv):  # pragma: no cover - a call fails the test immediately
        raise AssertionError("dry-run must not invoke its command runner")

    runner = _runner(tmp_path, forbidden)
    lines = runner.dry_run_commands(
        [local_candidates.Candidate("Granite 3B", "ibm/granite.gguf")],
        incumbent_identifier="ornith",
    )
    rendered = "\n".join(lines)

    assert "http://127.0.0.1:1234/v1" in rendered
    assert "link status --json" in rendered
    assert "link set-preferred-device ZBOOK_DEVICE_ID" in rendered
    assert "link set-preferred-device ORIGINAL_PREFERRED_DEVICE_ID" in rendered
    assert "--estimate-only --gpu max --context-length 8192 --parallel 4" in rendered
    assert "GPU Offload: 100%" in rendered
    assert "--identifier autotldr-granite-3b" in rendered
    assert "--pilot" in rendered
    assert "refuse inference unless preference is restored" in rendered
    assert "restore and verify every snapshotted incumbent setting" in rendered


def test_every_command_has_a_deadline_and_evaluator_owns_a_process_group(tmp_path):
    model = "ibm/granite/model.gguf"
    fake = FakeCommandRunner(catalog=[_model_row(model)])

    _runner(tmp_path, fake).run([local_candidates.Candidate("Granite", model)])

    assert fake.requests
    assert all(request.deadline_ns > 0 for request in fake.requests)
    assert all(request.timeout_seconds > 0 for request in fake.requests)
    evaluator_requests = [
        request for request in fake.requests if request.argv[0] == "python-test"
    ]
    assert len(evaluator_requests) == 1
    assert evaluator_requests[0].terminate_process_group is True
    assert all(
        not request.terminate_process_group
        for request in fake.requests
        if request.argv[0] != "python-test"
    )


def test_command_boundary_timeout_is_typed_bounded_and_redacts_secrets(tmp_path):
    now = [1_000_000_000]

    def clock():
        return now[0]

    def late_success(request):
        now[0] = request.deadline_ns + 1
        return local_candidates.CommandResult(0, "ignored", "")

    runner = local_candidates.SequentialPilotRunner(
        command_runner=late_success,
        output_dir=tmp_path,
        clock_ns=clock,
    )
    with pytest.raises(local_candidates.CommandTimeout) as raised:
        runner._run(
            ["worker", "--api-key", "do-not-log-me"],
            "fake evaluator",
            1.0,
            terminate_process_group=True,
        )

    record = raised.value.record
    assert record.schema == local_candidates.TIMEOUT_RECORD_SCHEMA
    assert record.action == "fake evaluator"
    assert record.argv == ("worker", "--api-key", "<redacted>")
    assert record.timeout_seconds == 1.0
    assert record.terminate_process_group is True
    assert "do-not-log-me" not in str(raised.value)
    assert "do-not-log-me" not in json.dumps(record.as_json())


def test_default_runner_terminates_evaluator_process_group_on_timeout(monkeypatch):
    signals = []

    class FakeProcess:
        pid = 4242
        returncode = None

        def __init__(self):
            self.waits = 0

        def communicate(self, *, timeout):
            assert timeout > 0
            raise subprocess.TimeoutExpired(["worker"], timeout)

        def wait(self, *, timeout):
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired(["worker"], timeout)
            self.returncode = -signal.SIGKILL
            return self.returncode

    process = FakeProcess()

    def fake_popen(argv, **kwargs):
        assert argv == ["worker"]
        if local_candidates.os.name == "posix":
            assert kwargs["start_new_session"] is True
        return process

    monkeypatch.setattr(local_candidates.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        local_candidates.os,
        "killpg",
        lambda pid, signum: signals.append((pid, signum)),
    )
    request = local_candidates.CommandRequest(
        argv=("worker",),
        action="evaluator",
        deadline_ns=time.monotonic_ns() + 1_000_000_000,
        timeout_seconds=1.0,
        terminate_grace_seconds=0.01,
        terminate_process_group=True,
    )

    with pytest.raises(local_candidates.CommandTimeout):
        local_candidates._default_command_runner(request)

    if local_candidates.os.name == "posix":
        assert signals == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]


def test_same_identifier_replacement_is_never_adopted_for_cleanup(tmp_path):
    model = "ibm/granite/model.gguf"

    def replace_process(fake):
        assert fake.local is not None
        fake.local = dict(fake.local, processId=999)

    fake = FakeCommandRunner(
        catalog=[_model_row(model)],
        after_evaluator=replace_process,
    )

    with pytest.raises(
        local_candidates.LifecycleError,
        match="process or actual residency changed|fingerprint changed|process/residency was replaced",
    ):
        _runner(tmp_path, fake).run([local_candidates.Candidate("Granite", model)])

    assert fake.local is not None
    assert fake.local["identifier"] == "autotldr-granite"
    assert fake.local["processId"] == 999
    assert not [call for call in fake.calls if call[:2] == ("lms-test", "unload")]
    assert (_runner(tmp_path, fake).candidate_recovery_path).exists()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"complete": False}, "partial"),
        ({"__drop_key__": "offloaded_layers"}, "not closed"),
        ({"cpu_moe_layers": 1}, "zero CPU MoE"),
        ({"cpu_moe_layers": 0.0}, "zero CPU MoE"),
        ({"kv_cache_on_gpu": False}, "GPU KV cache"),
        ({"gpu_allocation_bytes": 1}, "smaller"),
        ({"offloaded_layers": 40}, "all-layer"),
        ({"total_layers": 0}, "positive"),
        ({"model_size_bytes": 999}, "model size"),
        ({"process_executable_sha256": None}, "executable fingerprint"),
        ({"gpu_layers_request": "999999"}, "exact resident"),
        ({"resident_fingerprint_sha256": "0" * 64}, "exact resident"),
        ({"raw_commandline": "--api-key never-log"}, "not closed"),
    ],
)
def test_incomplete_or_contradictory_attestation_blocks_inference_but_cleans_exact_row(
    tmp_path,
    overrides,
    message,
):
    model = "ibm/granite/model.gguf"
    fake = FakeCommandRunner(catalog=[_model_row(model)])
    runner = local_candidates.SequentialPilotRunner(
        command_runner=fake,
        lms_executable="lms-test",
        python_executable="python-test",
        evaluator=Path("/repo/evaluate.py"),
        items_path=Path("/repo/items.jsonl"),
        prompt_path=Path("/repo/prompt.md"),
        policy_path=Path("/repo/policy.json"),
        output_dir=tmp_path / "predictions",
        residency_attestor=FakeResidencyAttestor(fake, **overrides),
        runtime_configuration_probe=FakeRuntimeConfigurationProbe(),
    )

    with pytest.raises(
        local_candidates.ResidencyAttestationError, match=message
    ) as raised:
        runner.run([local_candidates.Candidate("Granite", model)])

    assert raised.value.record["schema"] == local_candidates.ATTESTATION_FAILURE_SCHEMA
    assert raised.value.record["eligible"] is False
    assert raised.value.record["resident_fingerprint_sha256"]
    assert "never-log" not in json.dumps(raised.value.record)
    assert fake.local is None
    assert not _evaluations(fake)
    assert not runner.candidate_recovery_path.exists()


def test_missing_attestor_fails_closed_after_load_and_still_cleans_exact_row(tmp_path):
    model = "ibm/granite/model.gguf"
    fake = FakeCommandRunner(catalog=[_model_row(model)])
    runner = local_candidates.SequentialPilotRunner(
        command_runner=fake,
        lms_executable="lms-test",
        python_executable="python-test",
        output_dir=tmp_path / "predictions",
    )

    with pytest.raises(local_candidates.ResidencyAttestationError, match="no ZBook"):
        runner.run([local_candidates.Candidate("Granite", model)])

    assert fake.local is None
    assert not _evaluations(fake)


def test_timed_out_load_that_appears_late_is_reconciled_and_unloaded(tmp_path):
    model = "ibm/granite/model.gguf"
    fake = FakeCommandRunner(
        catalog=[_model_row(model)],
        load_timeout=True,
        late_load_after_ps=2,
    )
    runner = _runner(tmp_path, fake)

    with pytest.raises(local_candidates.CommandTimeout, match="load candidate"):
        runner.run([local_candidates.Candidate("Granite", model)])

    assert fake.local is None
    assert [call[2] for call in fake.calls if call[:2] == ("lms-test", "unload")] == [
        "autotldr-granite"
    ]
    assert not runner.candidate_recovery_path.exists()
    assert not _evaluations(fake)


class FakeClock:
    def __init__(self):
        self.now_ns = 1_000_000_000

    def __call__(self):
        return self.now_ns

    def sleep(self, seconds):
        self.now_ns += int(seconds * 1_000_000_000)


def test_timed_out_load_absence_remains_indeterminate_with_sanitized_recovery(tmp_path):
    model = "/private/user/models/granite.gguf"
    fake = FakeCommandRunner(
        catalog=[_model_row(model)],
        load_timeout=True,
    )
    clock = FakeClock()
    runner = _runner(
        tmp_path,
        fake,
        clock_ns=clock,
        sleep=clock.sleep,
        timeouts=local_candidates.OperationTimeouts(
            reconciliation_seconds=0.5,
            reconciliation_poll_seconds=0.25,
        ),
    )

    with pytest.raises(local_candidates.LifecycleError, match="indeterminate|reconciliation"):
        runner.run([local_candidates.Candidate("Granite", model)])

    assert fake.local is None
    assert runner.candidate_recovery_path.exists()
    recovery = runner.candidate_recovery_path.read_text(encoding="utf-8")
    assert local_candidates.CANDIDATE_RECOVERY_SCHEMA in recovery
    assert "/private/user" not in recovery
    assert model not in recovery
    assert "timeout" in recovery
    assert not _evaluations(fake)


def test_stale_candidate_recovery_refuses_before_any_lm_studio_io(tmp_path):
    output_dir = tmp_path / "predictions"
    output_dir.mkdir()
    (output_dir / ".candidate-recovery.json").write_text(
        '{"schema":"autotldr-candidate-recovery-v1"}\n',
        encoding="utf-8",
    )

    def forbidden(_request):  # pragma: no cover - any call fails immediately
        raise AssertionError("stale recovery must precede all LM Studio I/O")

    runner = local_candidates.SequentialPilotRunner(
        command_runner=forbidden,
        output_dir=output_dir,
    )
    with pytest.raises(local_candidates.ReconciliationError, match="recovery evidence"):
        runner.run([local_candidates.Candidate("Granite", "granite.gguf")])


def test_evaluator_deadline_uses_a_fresh_independent_cleanup_budget(tmp_path):
    model = "ibm/granite/model.gguf"
    clock = FakeClock()
    fake = FakeCommandRunner(
        catalog=[_model_row(model)],
        clock=clock,
        advance_evaluator_timeout=True,
    )
    runner = _runner(tmp_path, fake, clock_ns=clock, sleep=clock.sleep)

    with pytest.raises(local_candidates.CommandTimeout, match="label-blind pilot"):
        runner.run([local_candidates.Candidate("Granite", model)])

    assert fake.local is None
    unload_requests = [
        request
        for request in fake.requests
        if request.argv[:2] == ("lms-test", "unload")
    ]
    assert len(unload_requests) == 1
    assert unload_requests[0].timeout_seconds > 0
    assert not runner.candidate_recovery_path.exists()


def test_timed_out_load_with_unexpected_late_row_is_preserved_and_never_unloaded(
    tmp_path,
):
    model = "ibm/granite/model.gguf"
    fake = FakeCommandRunner(
        catalog=[_model_row(model)],
        load_timeout=True,
        late_load_after_ps=2,
        late_load_overrides={
            "modelKey": "other/model.gguf",
            "path": "other/model.gguf",
            "indexedModelIdentifier": "other/model.gguf",
        },
    )
    runner = _runner(tmp_path, fake)

    with pytest.raises(local_candidates.LifecycleError, match="unexpected resident"):
        runner.run([local_candidates.Candidate("Granite", model)])

    assert fake.local is not None
    assert fake.local["path"] == "other/model.gguf"
    assert runner.candidate_recovery_path.exists()
    assert not [call for call in fake.calls if call[:2] == ("lms-test", "unload")]
    assert not _evaluations(fake)


def test_two_initial_local_llms_refuse_before_any_preference_or_model_mutation(tmp_path):
    first = _model_row("one.gguf", identifier="one")
    second = _model_row("two.gguf", identifier="two", process_id=202)
    fake = FakeCommandRunner(catalog=[], local=first, additional_local=[second])

    with pytest.raises(local_candidates.LifecycleError, match="exclusive residency"):
        _runner(tmp_path, fake).run(
            [local_candidates.Candidate("Granite", "granite.gguf")]
        )

    mutating = [
        call
        for call in fake.calls
        if call[:3] == ("lms-test", "link", "set-preferred-device")
        or call[:2] in {("lms-test", "load"), ("lms-test", "unload")}
    ]
    assert mutating == []


def test_baseexception_from_evaluator_still_unloads_and_restores_link(tmp_path):
    model = "ibm/granite/model.gguf"
    fake = FakeCommandRunner(
        catalog=[_model_row(model)],
        evaluator_exception=KeyboardInterrupt(),
    )

    with pytest.raises(KeyboardInterrupt):
        _runner(tmp_path, fake).run([local_candidates.Candidate("Granite", model)])

    assert fake.local is None
    assert fake.preferred_device_identifier == fake.original_preferred_device_identifier
    assert not [path for path in (tmp_path / "predictions").glob(".*snapshot.json")]


def test_incomplete_incumbent_runtime_capture_refuses_before_unload(tmp_path):
    incumbent = _model_row(
        "ornith/model.gguf",
        identifier="ornith",
        context_length=32_768,
        ttl_ms=3_600_000,
    )
    fake = FakeCommandRunner(catalog=[], local=incumbent)
    runner = local_candidates.SequentialPilotRunner(
        command_runner=fake,
        lms_executable="lms-test",
        python_executable="python-test",
        output_dir=tmp_path / "predictions",
        residency_attestor=FakeResidencyAttestor(fake),
        runtime_configuration_probe=FakeRuntimeConfigurationProbe(complete=False),
    )

    with pytest.raises(
        local_candidates.RuntimeConfigurationError,
        match="incomplete",
    ):
        runner.run(
            [local_candidates.Candidate("Granite", "granite.gguf")],
            incumbent_identifier="ornith",
        )

    assert fake.local == incumbent
    assert not [call for call in fake.calls if call[:2] == ("lms-test", "unload")]


@pytest.mark.parametrize(
    ("probe", "message"),
    [
        (FakeRuntimeConfigurationProbe(exact_restorable=False), "not exactly restorable"),
        (FakeRuntimeConfigurationProbe(secret_setting=True), "prohibited secret"),
    ],
)
def test_unsafe_incumbent_runtime_records_refuse_before_unload(
    tmp_path,
    probe,
    message,
):
    incumbent = _model_row("ornith/model.gguf", identifier="ornith")
    fake = FakeCommandRunner(catalog=[], local=incumbent)
    runner = local_candidates.SequentialPilotRunner(
        command_runner=fake,
        lms_executable="lms-test",
        output_dir=tmp_path / "predictions",
        residency_attestor=FakeResidencyAttestor(fake),
        runtime_configuration_probe=probe,
    )

    with pytest.raises(local_candidates.RuntimeConfigurationError, match=message):
        runner.run(
            [local_candidates.Candidate("Granite", "granite.gguf")],
            incumbent_identifier="ornith",
        )

    assert fake.local == incumbent
    assert not [call for call in fake.calls if call[:2] == ("lms-test", "unload")]


def test_incumbent_restore_attests_and_rejects_complete_runtime_drift(tmp_path):
    candidate_model = "ibm/granite/model.gguf"
    incumbent = _model_row(
        "ornith/model.gguf",
        identifier="ornith",
        context_length=32_768,
        ttl_ms=3_600_000,
    )
    fake = FakeCommandRunner(catalog=[_model_row(candidate_model)], local=incumbent)
    attestor = FakeResidencyAttestor(fake)
    runtime_probe = FakeRuntimeConfigurationProbe(drift_after_first=True)
    runner = local_candidates.SequentialPilotRunner(
        command_runner=fake,
        lms_executable="lms-test",
        python_executable="python-test",
        output_dir=tmp_path / "predictions",
        residency_attestor=attestor,
        runtime_configuration_probe=runtime_probe,
    )

    with pytest.raises(
        local_candidates.RuntimeConfigurationError,
        match="complete runtime configuration",
    ):
        runner.run(
            [local_candidates.Candidate("Granite", candidate_model)],
            incumbent_identifier="ornith",
        )

    assert fake.local is not None
    assert fake.local["identifier"] == "ornith"
    assert runner.snapshot_path.exists()
    # Original incumbent, candidate post-load/pre/post/pre-unload, then the
    # restored incumbent are all independently attested.
    assert len(attestor.calls) >= 6
