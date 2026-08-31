"""First-user product contract: config, discovery, detail, and default prose."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from autotldr.cli import main
from autotldr.product import (
    DETAIL_PROFILES,
    ModelProfile,
    ProductConfigError,
    RuntimeDiscovery,
    discover_local_runtime,
    discover_served_models,
    load_product_config,
    probe_model_profile,
    write_user_model_config,
)
from autotldr.unit import Extraction, GroundedStatement


def test_detail_profiles_expand_one_simple_user_choice():
    assert tuple(DETAIL_PROFILES) == ("brief", "standard", "deep")
    assert [item.max_claims for item in DETAIL_PROFILES.values()] == [2, 4, 6]
    assert [item.evidence_budget_bytes for item in DETAIL_PROFILES.values()] == [
        8_000,
        24_000,
        48_000,
    ]
    assert [item.max_output_tokens for item in DETAIL_PROFILES.values()] == [
        512,
        1_024,
        1_800,
    ]
    assert [item.timeout_seconds for item in DETAIL_PROFILES.values()] == [
        60.0,
        90.0,
        120.0,
    ]


def test_user_then_project_config_is_strict_and_project_wins(tmp_path, monkeypatch):
    user = tmp_path / "user" / "config.toml"
    monkeypatch.setenv("AUTOTLDR_CONFIG", str(user))
    write_user_model_config(
        ModelProfile("http://127.0.0.1:1234", "served-local"),
        detail="brief",
    )
    project = tmp_path / "project"
    project.mkdir()
    (project / ".autotldr.toml").write_text(
        'version = 1\n[defaults]\ndetail = "deep"\n', encoding="utf-8"
    )

    config = load_product_config(directory=project)

    assert config.detail == "deep"
    assert config.model == ModelProfile("http://127.0.0.1:1234", "served-local")
    assert config.sources == (str(user.resolve()), str((project / ".autotldr.toml").resolve()))
    assert user.stat().st_mode & 0o777 == 0o600


def test_unknown_config_key_fails_closed(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text("version = 1\ninvented = true\n", encoding="utf-8")
    monkeypatch.setenv("AUTOTLDR_CONFIG", str(config))
    with pytest.raises(ProductConfigError, match="unknown configuration key"):
        load_product_config(directory=tmp_path)


@pytest.mark.parametrize(
    "endpoint",
    (
        "http://localhost:1234",
        "http://127.0.0.1:1234/v1",
        "http://127.0.0.1:4321",
        "https://127.0.0.1:1234",
    ),
)
def test_alpha_model_profile_accepts_only_the_certified_runtime_endpoint(endpoint):
    with pytest.raises(ProductConfigError, match="must be exactly"):
        ModelProfile(endpoint, "served-local")


def test_grounded_probe_uses_strict_transport_and_a_real_brief_completion_budget(
    monkeypatch,
):
    import autotldr.product
    import autotldr.synthesis

    profile = ModelProfile("http://127.0.0.1:1234", "served-local")
    observed = {}

    monkeypatch.setattr(
        autotldr.product,
        "require_active_model",
        lambda candidate: RuntimeDiscovery(
            "lm-studio",
            candidate.endpoint,
            (candidate.model,),
            (candidate.model,),
            True,
        ),
    )

    def fake_synthesize(extraction, config):
        observed["config"] = config
        unit = extraction.units[0]
        completed = Extraction(
            source=extraction.source,
            kind=extraction.kind,
            units=list(extraction.units),
            relations=list(extraction.relations),
            gaps=list(extraction.gaps),
            meta=dict(extraction.meta),
            summary_claims=[
                GroundedStatement("The probe is local.", (unit.origin,), (unit.id,))
            ],
        )
        return SimpleNamespace(extraction=completed)

    monkeypatch.setattr(autotldr.synthesis, "synthesize", fake_synthesize)

    report = probe_model_profile(profile)

    assert report["outcome"] == "accepted"
    assert observed["config"].max_output_tokens == DETAIL_PROFILES["brief"].max_output_tokens
    assert observed["config"].timeout_seconds == 30.0
    assert observed["config"].endpoint_policy.strict_zbook_local is True
    assert observed["config"].reasoning_effort == "none"
    assert observed["config"].product_detail == "brief"
    assert observed["config"].include_findings is False


def test_formats_json_is_derived_from_live_router(capsys):
    assert main(["formats", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "autotldr-runtime-capabilities-v1"
    assert payload["detail_levels"] == ["brief", "standard", "deep"]
    assert any(item["kind"] == "xlsx" and item["tier"] == 3 for item in payload["inputs"])
    assert any(item["suffix"] == ".pptx" and item["tier"] == 4 for item in payload["declined"])
    assert {item["name"] for item in payload["output_capabilities"]} == set(
        payload["outputs"]
    )


def test_default_cli_uses_configured_local_prose_and_detail(
    tmp_path, monkeypatch, capsys
):
    source = tmp_path / "notes.md"
    source.write_text("# Cooling policy\n\nKeep pressure below 240 kPa.\n", encoding="utf-8")
    config_path = tmp_path / "config.toml"
    monkeypatch.setenv("AUTOTLDR_CONFIG", str(config_path))
    monkeypatch.delenv("AUTOTLDR_MODEL", raising=False)
    write_user_model_config(
        ModelProfile("http://127.0.0.1:1234", "served-local"),
        force=True,
    )
    observed = {}

    def fake_synthesize(extraction, config):
        observed["kind"] = extraction.kind
        observed["config"] = config
        unit = extraction.units[0]
        statement = GroundedStatement(
            "The source documents a cooling policy.",
            (unit.origin,),
            (unit.id,),
        )
        result = Extraction(
            source=extraction.source,
            kind=extraction.kind,
            units=list(extraction.units),
            relations=list(extraction.relations),
            gaps=list(extraction.gaps),
            meta=dict(extraction.meta),
            summary_claims=[statement],
        )
        return SimpleNamespace(extraction=result, used_fallback=False)

    import autotldr.synthesis
    import autotldr.product

    monkeypatch.setattr(autotldr.synthesis, "synthesize", fake_synthesize)
    monkeypatch.setattr(
        autotldr.product,
        "require_active_model",
        lambda profile: RuntimeDiscovery(
            "lm-studio", profile.endpoint, (profile.model,), (profile.model,), True
        ),
    )

    assert main([str(source), "--detail", "deep", "--out", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert observed["kind"] == "collection"
    assert observed["config"].model == "served-local"
    assert observed["config"].max_claims == 6
    assert observed["config"].evidence_budget_bytes == 48_000
    assert observed["config"].max_output_tokens == 1_800
    assert observed["config"].timeout_seconds == 120.0
    assert observed["config"].endpoint_policy.strict_zbook_local is True
    assert observed["config"].reasoning_effort == "none"
    assert observed["config"].product_detail == "deep"
    assert observed["config"].include_findings is False
    assert payload["summary_claims"][0]["content"] == "The source documents a cooling policy."
    assert payload["kind"] == "markdown"
    assert payload["manifest"]["product"]["mode"] == "prose"
    assert payload["manifest"]["product"]["detail"]["name"] == "deep"


def test_explicit_model_off_is_labelled_evidence_mode(tmp_path, monkeypatch, capsys):
    source = tmp_path / "notes.md"
    source.write_text("# Notes\n\nEvidence only.\n", encoding="utf-8")
    monkeypatch.setenv("AUTOTLDR_CONFIG", str(tmp_path / "missing.toml"))
    monkeypatch.delenv("AUTOTLDR_MODEL", raising=False)

    assert main([str(source), "--model", "off", "--out", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["manifest"]["product"]["mode"] == "evidence"
    assert payload["summary_claims"] == []


def test_environment_model_off_is_labelled_evidence_mode(
    tmp_path, monkeypatch, capsys
):
    source = tmp_path / "notes.md"
    source.write_text("# Notes\n\nEvidence only.\n", encoding="utf-8")
    monkeypatch.setenv("AUTOTLDR_CONFIG", str(tmp_path / "missing.toml"))
    monkeypatch.setenv("AUTOTLDR_MODEL", "off")

    def unexpected_model_check(*args, **kwargs):
        raise AssertionError("model-off must not inspect local model residency")

    monkeypatch.setattr(
        "autotldr.product.require_active_model", unexpected_model_check
    )

    assert main([str(source), "--out", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["manifest"]["product"]["mode"] == "evidence"
    assert payload["summary_claims"] == []


def test_missing_model_fails_before_source_acquisition(tmp_path, monkeypatch, capsys):
    missing_source = tmp_path / "does-not-exist.md"
    monkeypatch.setenv("AUTOTLDR_CONFIG", str(tmp_path / "missing.toml"))
    monkeypatch.delenv("AUTOTLDR_MODEL", raising=False)

    assert main([str(missing_source)]) == 1

    error = capsys.readouterr().err
    assert "no local model is configured" in error
    assert "no such file" not in error


def test_source_named_version_is_not_stolen_after_option_separator(
    tmp_path, monkeypatch, capsys
):
    source = tmp_path / "--version"
    source.write_text("Meaning.\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AUTOTLDR_MODEL", raising=False)

    assert main(["--out", "json", "--model", "off", "--", "--version"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["subject"] == "--version"


def test_all_checked_in_product_surfaces_use_package_version():
    from autotldr import __version__
    from autotldr import mcp

    assert mcp._SERVER_INFO["version"] == __version__
    assert not Path(".well-known/agent-card.json").exists()


def test_human_detail_reports_presentation_without_hiding_machine_evidence(
    tmp_path, monkeypatch, capsys
):
    source = tmp_path / "many.md"
    source.write_text(
        "\n\n".join(f"## Topic {index}\n\nEvidence {index}." for index in range(12)),
        encoding="utf-8",
    )
    monkeypatch.setenv("AUTOTLDR_CONFIG", str(tmp_path / "missing.toml"))
    monkeypatch.delenv("AUTOTLDR_MODEL", raising=False)

    assert main([str(source), "--model", "off", "--detail", "brief", "--out", "md"]) == 0
    brief = capsys.readouterr().out
    assert "Presenting 6 of 24 budget-selected units" in brief

    assert main([str(source), "--model", "off", "--detail", "deep", "--out", "md"]) == 0
    deep = capsys.readouterr().out
    assert "Presenting 24 of 24 budget-selected units" in deep

    assert main([str(source), "--model", "off", "--detail", "brief", "--out", "json"]) == 0
    machine = json.loads(capsys.readouterr().out)
    assert len(machine["units"]) == 24


def test_human_sources_name_extracted_and_declined_collection_members(
    tmp_path, monkeypatch, capsys
):
    root = tmp_path / "handoff"
    root.mkdir()
    (root / "notes.md").write_text("# Notes\n\nSupported.\n", encoding="utf-8")
    (root / "unknown.zzz").write_bytes(b"\x00\x01")
    monkeypatch.setenv("AUTOTLDR_CONFIG", str(tmp_path / "missing.toml"))
    monkeypatch.delenv("AUTOTLDR_MODEL", raising=False)

    assert main([str(root), "--model", "off", "--out", "md"]) == 0
    rendered = capsys.readouterr().out

    assert "## Sources" in rendered
    assert "notes.md" in rendered and "extracted" in rendered
    assert "unknown.zzz" in rendered and "declined" in rendered
    assert "## Gaps" in rendered


def test_bundled_agent_skill_can_be_installed_without_a_source_checkout(
    tmp_path, capsys
):
    destination = tmp_path / "skills"

    assert main(["integrations", "skill", "--install", str(destination)]) == 0

    installed = destination / "autotldr"
    assert (installed / "SKILL.md").is_file()
    assert (installed / "agents" / "openai.yaml").is_file()
    assert "Installed AutoTLDR Agent Skill" in capsys.readouterr().out
    assert main(["integrations", "skill", "--install", str(destination)]) == 1
    assert "already exists" in capsys.readouterr().err


def test_watch_uses_configured_prose_for_files_and_rollup(
    tmp_path, monkeypatch
):
    from autotldr.watch import ROLLUP_NAME, artifact_path, run_once

    root = tmp_path / "inbox"
    root.mkdir()
    source = root / "notes.md"
    source.write_text("# Policy\n\nKeep pressure below 240 kPa.\n", encoding="utf-8")
    config_path = tmp_path / "config.toml"
    monkeypatch.setenv("AUTOTLDR_CONFIG", str(config_path))
    monkeypatch.delenv("AUTOTLDR_MODEL", raising=False)
    write_user_model_config(
        ModelProfile("http://127.0.0.1:1234", "served-local"),
        force=True,
    )
    observed = []

    def fake_synthesize(extraction, config):
        observed.append((extraction.kind, config.max_claims))
        unit = extraction.units[0]
        completed = Extraction(
            source=extraction.source,
            kind=extraction.kind,
            units=list(extraction.units),
            relations=list(extraction.relations),
            gaps=list(extraction.gaps),
            meta=dict(extraction.meta),
            summary_claims=[
                GroundedStatement(
                    "The source defines a pressure policy.",
                    (unit.origin,),
                    (unit.id,),
                )
            ],
        )
        return SimpleNamespace(extraction=completed, used_fallback=False)

    import autotldr.synthesis
    import autotldr.product

    monkeypatch.setattr(autotldr.synthesis, "synthesize", fake_synthesize)
    monkeypatch.setattr(
        autotldr.product,
        "require_active_model",
        lambda profile: RuntimeDiscovery(
            "lm-studio", profile.endpoint, (profile.model,), (profile.model,), True
        ),
    )

    result = run_once(root, detail="brief", budget=250_000)

    assert result.succeeded == 1
    assert observed == [("collection", 2), ("collection", 2)]
    assert "Cited local-model prose" in artifact_path(root, "notes.md").read_text()
    assert "Cited local-model prose" in (root / ".autotldr" / ROLLUP_NAME).read_text()


def test_doctor_requires_catalog_identity_and_grounded_completion_probe(
    tmp_path, monkeypatch, capsys
):
    config_path = tmp_path / "config.toml"
    monkeypatch.setenv("AUTOTLDR_CONFIG", str(config_path))
    profile = ModelProfile("http://127.0.0.1:1234", "served-local")
    write_user_model_config(profile, force=True)
    import autotldr.product

    monkeypatch.setattr(
        autotldr.product,
        "discover_local_runtime",
        lambda endpoint: RuntimeDiscovery(
            "lm-studio", endpoint, ("served-local",), ("served-local",), True
        ),
    )
    observed = []

    def successful_probe(candidate):
        observed.append(candidate)
        return {
            "model": candidate.model,
            "endpoint": candidate.endpoint,
            "claims": 1,
            "outcome": "accepted",
        }

    monkeypatch.setattr(autotldr.product, "probe_model_profile", successful_probe)

    assert main(["doctor", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ready"] is True
    assert observed == [profile]
    assert next(
        item for item in report["checks"] if item["name"] == "grounded-prose"
    )["status"] == "ok"

    monkeypatch.setattr(
        autotldr.product,
        "probe_model_profile",
        lambda _profile: (_ for _ in ()).throw(ValueError("bad response")),
    )
    assert main(["doctor", "--json"]) == 1
    failed = json.loads(capsys.readouterr().out)
    assert failed["ready"] is False
    assert next(
        item for item in failed["checks"] if item["name"] == "grounded-prose"
    )["status"] == "error"


def test_root_help_and_config_introspection_are_discoverable(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("AUTOTLDR_CONFIG", str(tmp_path / "missing.toml"))

    with pytest.raises(SystemExit) as help_exit:
        main(["--help"])
    assert help_exit.value.code == 0
    help_text = capsys.readouterr().out
    for command in ("setup", "doctor", "config", "formats", "watch", "integrations", "mcp"):
        assert command in help_text

    assert main(["config", "show", "--json"]) == 0
    resolved = json.loads(capsys.readouterr().out)
    assert resolved["detail"]["name"] == "standard"
    assert resolved["model"] is None

    assert main(["config", "paths"]) == 0
    paths = capsys.readouterr().out
    assert str(tmp_path / "missing.toml") in paths
    assert ".autotldr.toml" in paths


def test_setup_has_no_fake_endpoint_choice_and_persists_one_active_model(
    tmp_path, monkeypatch, capsys
):
    import autotldr.product

    config_path = tmp_path / "config.toml"
    monkeypatch.setenv("AUTOTLDR_CONFIG", str(config_path))
    observed = []

    def active_models(endpoint):
        observed.append(endpoint)
        return ("active-local",)

    monkeypatch.setattr(autotldr.product, "discover_served_models", active_models)

    with pytest.raises(SystemExit) as help_exit:
        main(["setup", "--help"])
    assert help_exit.value.code == 0
    setup_help = capsys.readouterr().out
    assert "--endpoint" not in setup_help

    assert main(["setup", "--detail", "brief"]) == 0
    assert observed == ["http://127.0.0.1:1234"]
    config = load_product_config(directory=tmp_path)
    assert config.detail == "brief"
    assert config.model == ModelProfile(
        "http://127.0.0.1:1234", "active-local"
    )
    assert "Run `autotldr doctor`" in capsys.readouterr().out


def test_setup_requires_explicit_model_when_multiple_are_active(
    tmp_path, monkeypatch, capsys
):
    import autotldr.product

    config_path = tmp_path / "config.toml"
    monkeypatch.setenv("AUTOTLDR_CONFIG", str(config_path))
    monkeypatch.setattr(
        autotldr.product,
        "discover_served_models",
        lambda _endpoint: ("first", "second"),
    )

    assert main(["setup"]) == 1
    error = capsys.readouterr().err
    assert "multiple generation models are active" in error
    assert "--model ID" in error
    assert not config_path.exists()


def test_runtime_discovery_never_confuses_catalog_with_active_models(monkeypatch):
    import autotldr.product as product

    catalog = {
        "object": "list",
        "data": [
            {"id": "loaded-model"},
            {"id": "downloaded-only"},
            {"id": "embedding-model"},
        ],
    }
    runtime = {
        "models": [
            {
                "key": "loaded-model",
                "type": "llm",
                "loaded_instances": [{"id": "loaded-model"}],
            },
            {
                "key": "downloaded-only",
                "type": "llm",
                "loaded_instances": [],
            },
            {
                "key": "embedding-model",
                "type": "embedding",
                "loaded_instances": [{"id": "embedding-model"}],
            },
        ]
    }

    def fake_read(url, **_kwargs):
        return runtime if "/api/" in url else catalog

    monkeypatch.setattr(product, "_read_runtime_json", fake_read)

    discovered = discover_local_runtime("http://127.0.0.1:1234")

    assert discovered.provider == "lm-studio"
    assert discovered.active_state_verified is True
    assert discovered.catalog_models == (
        "downloaded-only",
        "embedding-model",
        "loaded-model",
    )
    assert discovered.active_models == ("loaded-model",)
    assert discover_served_models("http://127.0.0.1:1234") == ("loaded-model",)
