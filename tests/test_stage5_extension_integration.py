"""Production integration for explicit Stage 5 adapters and safe boundaries."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import types
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from autotldr import cli as cli_module
from autotldr import collection, render as render_module, router
from autotldr.collection import CollectionLimits, DeclineKind, acquire_directory
from autotldr.extensions import (
    AcquisitionSpec,
    ExtensionCollisionError,
    ExtensionConformanceError,
    ExtensionRegistry,
    ExtractorSpec,
    RendererSpec,
    SignatureProbe,
    load_extension,
)
from autotldr.unit import Extraction, GroundedStatement, Modality, Origin, Unit


def _write_adapter_package(tmp_path: Path, module_name: str) -> str:
    (tmp_path / f"{module_name}.py").write_text(
        '''
import hashlib
import json
from pathlib import Path

from autotldr.collection import CollectionAcquisition
from autotldr.extensions import AcquisitionSpec, ExtractorSpec, RendererSpec, SignatureProbe
from autotldr.unit import Extraction, Modality, Origin, Unit


def extract_weird(source):
    path = Path(source)
    payload = path.read_bytes()
    body = payload[4:] if payload.startswith(b"WFX\\x00") else payload
    text = body.decode("utf-8", errors="strict")
    units = []
    offset = 4 if payload.startswith(b"WFX\\x00") else 0
    for index, part in enumerate(text.split("|"), start=1):
        if not part:
            continue
        units.append(
            Unit(
                source=str(path),
                modality=Modality.RECORD,
                content=part,
                origin=Origin(str(path), f"record:{index}@byte:{offset}"),
                salience=max(0.1, 1.0 - index / 20),
            )
        )
        offset += len(part.encode("utf-8")) + 1
    return Extraction(source=str(path), kind="weird", units=units)


def build_wire(bundle, options):
    return json.dumps(
        {
            "kind": bundle.kind,
            "subject": bundle.subject,
            "units": [
                {
                    "id": unit.id,
                    "content": unit.content,
                    "origin": {
                        "source": unit.origin.source,
                        "ref": unit.origin.ref,
                    },
                }
                for unit in bundle.units
            ],
            "summary_claims": [
                {
                    "id": statement.id,
                    "evidence_unit_ids": list(statement.evidence_unit_ids),
                }
                for statement in bundle.summary_claims
            ],
            "manifest": bundle.manifest,
            "selection": bundle.selection,
            "options": {
                "output": options.output,
                "cite": options.cite,
                "color": options.color,
                "indent": options.indent,
            },
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ) + "\\n"


def acquire_virtual(source):
    payload = b"virtual fact\\n"
    logical = "virtual/item.wfx"
    unit = Unit(
        source=logical,
        modality=Modality.PROSE,
        content="virtual fact",
        origin=Origin(logical, "line:1"),
    )
    leaf = Extraction(
        source=logical,
        kind="weird",
        units=[unit],
        meta={
            "inputs": [
                {
                    "source": logical,
                    "kind": "weird",
                    "tier": 9,
                    "bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            ]
        },
    )
    return CollectionAcquisition(
        source="virtual",
        kind="virtual-collection",
        extractions=(leaf,),
        declines=(),
        manifest={
            "schema": 1,
            "source": "virtual",
            "kind": "virtual-collection",
            "requested": source,
            "members": [logical],
        },
    )


def autotldr_extension():
    return [
        ExtractorSpec(
            name="weird",
            module=__name__,
            callable="extract_weird",
            kinds=("weird",),
            aliases=("wfx",),
            suffixes=(".wfx",),
            media_types=("application/x-weird",),
            signatures=(SignatureProbe(b"WFX\\x00"),),
            tier=9,
        ),
        AcquisitionSpec(
            name="virtual",
            module=__name__,
            callable="acquire_virtual",
            kinds=("virtual-collection",),
            tier=2,
        ),
        RendererSpec(
            name="wire",
            module=__name__,
            callable="build_wire",
            aliases=("wire-json",),
            suffixes=(".wire",),
            media_types=("application/x-autotldr-wire",),
            supports_citations=False,
        ),
    ]
'''.lstrip(),
        encoding="utf-8",
    )
    return module_name


def _registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
) -> tuple[ExtensionRegistry, str]:
    reference = _write_adapter_package(tmp_path, module_name)
    monkeypatch.syspath_prepend(str(tmp_path))
    registry = ExtensionRegistry()
    load_extension(reference, registry)
    router.validate_extension_registry(registry)
    collection.validate_extension_registry(registry)
    render_module.validate_extension_registry(registry)
    return registry, reference


def test_cli_import_does_not_create_or_discover_an_extension_registry() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import autotldr.cli; "
            "print('autotldr.extensions' in sys.modules); "
            "print('autotldr.collection' in sys.modules); "
            "print('autotldr.render' in sys.modules)",
        ],
        cwd=Path(__file__).resolve().parents[1],
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.splitlines() == ["False", "False", "False"]


def test_extension_routes_signature_kind_media_and_nested_members(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, _reference = _registry(
        tmp_path,
        monkeypatch,
        "autotldr_test_runtime_routes",
    )

    signed = tmp_path / "signed.bin"
    signed.write_bytes(b"WFX\x00signature route")
    signed_result = router.extract(signed, registry=registry)
    assert signed_result.kind == "weird"
    assert [unit.content for unit in signed_result.units] == ["signature route"]
    assert signed_result.meta["inputs"][0]["adapter"] == {
        "origin": "explicit-extension",
        "name": "weird",
    }
    assert signed_result.meta["extensions"]["capabilities"]["counts"] == {
        "extractors": 1,
        "acquisitions": 1,
        "renderers": 1,
    }

    hinted = tmp_path / "mislabeled.opaque"
    hinted.write_text("explicit kind", encoding="utf-8")
    hinted_result = router.extract(hinted, kind="wfx", registry=registry)
    assert [unit.content for unit in hinted_result.units] == ["explicit kind"]
    assert hinted_result.source == str(hinted)

    acquired = router._HttpPayload(  # noqa: SLF001 - production routing seam
        b"media route",
        "https://example.test/blob",
        "application/x-weird",
        "utf-8",
    )
    remote, handler = router._extract_http_payload(  # noqa: SLF001
        acquired,
        requested_url=acquired.final_url,
        registry=registry,
    )
    assert handler.extension_name == "weird"
    assert remote.source == acquired.final_url
    assert [unit.content for unit in remote.units] == ["media route"]

    root = tmp_path / "corpus"
    root.mkdir()
    (root / "member.wfx").write_text("nested|member", encoding="utf-8")
    nested = acquire_directory(root, registry=registry)
    assert [item.source for item in nested.extractions] == ["corpus/member.wfx"]
    leaf_manifest = nested.extractions[0].meta["inputs"][0]
    assert leaf_manifest["adapter"]["name"] == "weird"
    assert nested.manifest["extensions"]["counts"] == {
        "extractors": 1,
        "acquisitions": 1,
        "renderers": 1,
    }
    serialized = json.dumps(nested.manifest, sort_keys=True)
    assert "tmp" not in serialized


def test_cli_records_capabilities_and_external_renderer_exact_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _registry(
        tmp_path,
        monkeypatch,
        "autotldr_test_runtime_cli",
    )
    source = tmp_path / "facts.wfx"
    source.write_text("alpha|beta|gamma", encoding="utf-8")

    status = cli_module.main(
        [
            str(source),
            "--extension",
            "autotldr_test_runtime_cli",
            "--out",
            "wire-json",
            "--no-cite",
        ]
    )
    captured = capsys.readouterr()
    envelope = json.loads(captured.out)
    payload = envelope["core"]
    renderer_payload = json.loads(envelope["payload"])

    assert status == cli_module.EXIT_OK
    assert captured.err == ""
    assert captured.out.endswith("\n")
    assert envelope["schema"] == "autotldr-extension-render-envelope-v1"
    assert envelope["format"]["name"] == "wire"
    selection = payload["manifest"]["selection"]
    assert selection["used"] == len(captured.out.encode("utf-8"))
    assert selection["counter"] == "utf8-byte-v1"
    assert renderer_payload["options"]["output"] == "wire"
    extension_run = payload["manifest"]["extensions"]
    assert extension_run["requested"] == ["autotldr_test_runtime_cli"]
    assert extension_run["capabilities"]["counts"] == {
        "extractors": 1,
        "acquisitions": 1,
        "renderers": 1,
    }
    assert payload["manifest"]["extension_renderer"]["name"] == "wire"
    assert payload["manifest"]["inputs"][0]["adapter"]["name"] == "weird"


def test_external_renderer_is_inside_the_budget_and_utf8_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, _reference = _registry(
        tmp_path,
        monkeypatch,
        "autotldr_test_runtime_budget",
    )
    source = tmp_path / "large.wfx"
    source.write_text(
        "|".join(f"record-{index}-" + ("x" * 600) for index in range(8)),
        encoding="utf-8",
    )
    result = router.extract(source, registry=registry)
    unlimited = render_module.render(
        result,
        output="wire",
        cite=False,
        registry=registry,
    )

    bounded = None
    ceiling = None
    for fraction in (0.9, 0.8, 0.7, 0.6, 0.5):
        candidate_ceiling = int(len(unlimited.encode("utf-8")) * fraction)
        try:
            candidate = render_module.render(
                result,
                output="wire",
                cite=False,
                budget=candidate_ceiling,
                registry=registry,
            )
        except render_module.BudgetTooSmall:
            continue
        record = json.loads(candidate)
        selection = record["core"]["manifest"]["selection"]
        if selection["dropped"]["unit_count"]:
            bounded = candidate
            ceiling = candidate_ceiling
            break

    assert bounded is not None and ceiling is not None
    envelope = json.loads(bounded)
    payload = envelope["core"]
    selection = payload["manifest"]["selection"]
    assert len(bounded.encode("utf-8")) <= ceiling
    assert selection["used"] == len(bounded.encode("utf-8"))
    assert selection["dropped"]["reported"]
    assert envelope["format"]["renderer"]["name"] == "wire"


def test_builtin_capability_collisions_are_rejected_but_declines_are_extensible() -> None:
    extractor_collision = ExtensionRegistry(
        (
            ExtractorSpec(
                name="community-markdown",
                module="builtins",
                callable="len",
                kinds=("community-markdown",),
                suffixes=(".md",),
            ),
        )
    )
    with pytest.raises(ExtensionCollisionError, match="core suffix"):
        router.validate_extension_registry(extractor_collision)

    signature_collision = ExtensionRegistry(
        (
            ExtractorSpec(
                name="fake-pdf",
                module="builtins",
                callable="len",
                kinds=("fake-pdf",),
                signatures=(SignatureProbe(b"%PDF-"),),
            ),
        )
    )
    with pytest.raises(ExtensionCollisionError, match="strong signature"):
        router.validate_extension_registry(signature_collision)

    renderer_collision = ExtensionRegistry(
        (
            RendererSpec(
                name="community-wire",
                module="builtins",
                callable="format",
                suffixes=(".json",),
            ),
        )
    )
    with pytest.raises(ExtensionCollisionError, match="core suffix"):
        render_module.validate_extension_registry(renderer_collision)

    acquisition_collision = ExtensionRegistry(
        (
            AcquisitionSpec(
                name="community-walk",
                module="builtins",
                callable="len",
                kinds=("directory",),
            ),
        )
    )
    with pytest.raises(ExtensionCollisionError, match="core capability"):
        collection.validate_extension_registry(acquisition_collision)

    # .arrow is now an implemented Tier 3 core capability; an extension claiming
    # it must be rejected for colliding with core.
    arrow_collision = ExtensionRegistry(
        (
            ExtractorSpec(
                name="arrow-community",
                module="builtins",
                callable="len",
                kinds=("arrow-community",),
                suffixes=(".arrow",),
            ),
        )
    )
    with pytest.raises(ExtensionCollisionError, match="core"):
        router.validate_extension_registry(arrow_collision)

    # Deferred formats (such as .tar or ambiguous .fit) are not implemented capabilities;
    # explicit community support is therefore allowed to replace them.
    deferred_override = ExtensionRegistry(
        (
            ExtractorSpec(
                name="archive-community",
                module="builtins",
                callable="len",
                kinds=("archive-community",),
                suffixes=(".tar", ".fit"),
            ),
        )
    )
    router.validate_extension_registry(deferred_override)
    assert ".tar" in router.supported_suffixes(registry=deferred_override)
    assert ".tar" not in router.declined_suffixes(registry=deferred_override)
    assert ".fit" in router.supported_suffixes(registry=deferred_override)
    assert ".fit" not in router.declined_suffixes(registry=deferred_override)


def test_external_acquirer_is_explicit_validated_and_manifested(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _registry(
        tmp_path,
        monkeypatch,
        "autotldr_test_runtime_acquirer",
    )
    status = cli_module.main(
        [
            "memory:demo",
            "--extension",
            "autotldr_test_runtime_acquirer",
            "--acquirer",
            "virtual",
            "--out",
            "json",
        ]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert status == cli_module.EXIT_OK
    assert captured.err == ""
    assert payload["kind"] == "collection"
    assert payload["subject"] == "virtual"
    assert payload["units"][0]["content"] == "virtual fact"
    assert payload["manifest"]["extension_acquisition"]["name"] == "virtual"
    assert payload["manifest"]["collection_acquisitions"][0]["requested"] == "memory:demo"


def _runtime_renderer_registry(
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
    builder: object,
    *,
    name: str = "payload-wire",
) -> ExtensionRegistry:
    module = types.ModuleType(module_name)
    module.build = builder  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module_name, module)
    return ExtensionRegistry(
        (
            RendererSpec(
                name=name,
                module=module_name,
                callable="build",
                supports_citations=True,
            ),
        )
    )


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (b"not canonical text", "not bytes"),
        ("bad surrogate: \ud800", "UTF-8"),
    ],
)
def test_extension_renderer_runtime_output_is_validated(
    value: object,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def build(_bundle: object, _options: object) -> object:
        return value

    registry = _runtime_renderer_registry(
        monkeypatch,
        "autotldr_test_bad_runtime_renderer",
        build,
        name="badwire",
    )
    result = Extraction(
        source="source",
        kind="text",
        units=[
            Unit(
                source="source",
                modality=Modality.PROSE,
                content="fact",
                origin=Origin("source", "line:1"),
            )
        ],
    )

    with pytest.raises(ExtensionConformanceError, match=message):
        render_module.render(result, output="badwire", registry=registry)


def test_external_renderer_payload_cannot_omit_core_envelope_or_final_newline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def build(bundle: object, _options: object) -> str:
        # Deliberately omits every provenance, manifest, and accounting field,
        # attempts to delete them from its view, and deliberately omits a
        # wire-level final newline.
        bundle.units.clear()  # type: ignore[attr-defined]
        bundle.manifest.clear()  # type: ignore[attr-defined]
        return "cosmetic payload only"

    registry = _runtime_renderer_registry(
        monkeypatch,
        "autotldr_test_omitting_renderer",
        build,
    )
    unit = Unit(
        source="source",
        modality=Modality.PROSE,
        content="grounded fact",
        origin=Origin("source", "line:1"),
    )
    result = Extraction(source="source", kind="text", units=[unit])

    rendered = render_module.render(
        result,
        output="payload-wire",
        cite=False,
        registry=registry,
    )
    envelope = json.loads(rendered)
    core = envelope["core"]
    selection = core["manifest"]["selection"]

    assert rendered.endswith("\n")
    assert envelope["payload"] == "cosmetic payload only"
    assert envelope["format"]["name"] == "payload-wire"
    assert envelope["format"]["renderer"]["name"] == "payload-wire"
    assert core["units"][0]["id"] == unit.id
    assert core["units"][0]["origin"] == {
        "source": "source",
        "ref": "line:1",
    }
    assert selection["used"] == len(rendered.encode("utf-8"))
    assert selection["counter"] == "utf8-byte-v1"
    assert selection["scope"] == "complete-output"
    assert core["manifest"]["extensions"]["capabilities"]["counts"] == {
        "extractors": 0,
        "acquisitions": 0,
        "renderers": 1,
    }


def test_oversized_external_payload_has_named_mandatory_envelope_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def build(_bundle: object, _options: object) -> str:
        return "x" * 20_000

    registry = _runtime_renderer_registry(
        monkeypatch,
        "autotldr_test_oversized_renderer",
        build,
        name="huge-wire",
    )
    unit = Unit(
        source="source",
        modality=Modality.PROSE,
        content="fact",
        origin=Origin("source", "line:1"),
    )
    result = Extraction(source="source", kind="text", units=[unit])

    with pytest.raises(render_module.BudgetTooSmall) as raised:
        render_module.render(
            result,
            output="huge-wire",
            cite=False,
            budget=512,
            registry=registry,
        )

    assert raised.value.limit == 512
    assert raised.value.required > raised.value.limit
    assert raised.value.output == "huge-wire"


def test_nondeterministic_external_payload_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"count": 0}

    def build(_bundle: object, _options: object) -> str:
        calls["count"] += 1
        return "a" if calls["count"] % 2 else "b"

    registry = _runtime_renderer_registry(
        monkeypatch,
        "autotldr_test_nondeterministic_renderer",
        build,
        name="changing-wire",
    )
    unit = Unit(
        source="source",
        modality=Modality.PROSE,
        content="fact",
        origin=Origin("source", "line:1"),
    )
    result = Extraction(source="source", kind="text", units=[unit])

    with pytest.raises(ExtensionConformanceError, match="nondeterministic"):
        render_module.render(
            result,
            output="changing-wire",
            cite=False,
            registry=registry,
        )


def test_external_envelope_preserves_claim_evidence_closure_and_drop_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def build(bundle: object, _options: object) -> str:
        # The transformer sees only a defensive selected projection and cannot
        # become the owner of the core selection report.
        assert not bundle.selection  # type: ignore[attr-defined]
        return "collection view"

    registry = _runtime_renderer_registry(
        monkeypatch,
        "autotldr_test_claim_renderer",
        build,
        name="claim-wire",
    )
    evidence = [
        Unit(
            source="source",
            modality=Modality.PROSE,
            content=f"evidence-{index}-" + ("e" * 2_000),
            origin=Origin("source", f"line:{index}"),
            salience=0.1,
        )
        for index in range(1, 4)
    ]
    distractor = Unit(
        source="source",
        modality=Modality.PROSE,
        content="distractor-" + ("d" * 2_000),
        origin=Origin("source", "line:10"),
        salience=1.0,
    )
    statement = GroundedStatement(
        content="All evidence units jointly ground this claim.",
        origins=tuple(unit.origin for unit in evidence),
        evidence_unit_ids=tuple(unit.id for unit in evidence),
    )
    result = Extraction(
        source="source",
        kind="collection",
        units=[*evidence, distractor],
        summary_claims=[statement],
    )

    unlimited = render_module.render(
        result,
        output="claim-wire",
        cite=False,
        registry=registry,
    )
    unlimited_core = json.loads(unlimited)["core"]
    selected_ids = {unit["id"] for unit in unlimited_core["units"]}
    assert unlimited_core["summary_claims"] == [
        {
            "id": statement.id,
            "content": statement.content,
            "origins": [
                {"source": origin.source, "ref": origin.ref}
                for origin in statement.origins
            ],
            "evidence_unit_ids": list(statement.evidence_unit_ids),
        }
    ]
    assert set(statement.evidence_unit_ids) <= selected_ids

    bounded = None
    ceiling = None
    available = len(unlimited.encode("utf-8"))
    for fraction in (0.9, 0.8, 0.7, 0.6, 0.5, 0.4):
        candidate_ceiling = int(available * fraction)
        try:
            candidate = render_module.render(
                result,
                output="claim-wire",
                cite=False,
                budget=candidate_ceiling,
                registry=registry,
            )
        except render_module.BudgetTooSmall:
            continue
        candidate_core = json.loads(candidate)["core"]
        if not candidate_core["summary_claims"]:
            bounded = candidate
            ceiling = candidate_ceiling
            break

    assert bounded is not None and ceiling is not None
    bounded_core = json.loads(bounded)["core"]
    bounded_selection = bounded_core["manifest"]["selection"]
    bounded_ids = {unit["id"] for unit in bounded_core["units"]}
    assert len(bounded.encode("utf-8")) <= ceiling
    assert bounded_core["summary_claims"] == []
    assert bounded_selection["dropped"]["reported_statements"][0]["id"] == statement.id
    assert bounded_selection["dropped"]["reported_statements"][0][
        "evidence_unit_ids"
    ] == list(statement.evidence_unit_ids)
    assert all(
        set(claim["evidence_unit_ids"]) <= bounded_ids
        for claim in bounded_core["summary_claims"]
    )


def test_tight_budget_never_keeps_a_claim_with_omitted_evidence() -> None:
    evidence = [
        Unit(
            source="source",
            modality=Modality.PROSE,
            content=f"evidence-{index}-" + ("e" * 2_000),
            origin=Origin("source", f"line:{index}"),
            salience=0.1,
        )
        for index in range(1, 4)
    ]
    distractor = Unit(
        source="source",
        modality=Modality.PROSE,
        content="high-salience distractor " + ("d" * 2_000),
        origin=Origin("source", "line:10"),
        salience=1.0,
    )
    statement = GroundedStatement(
        content="All three evidence units jointly ground this claim.",
        origins=tuple(unit.origin for unit in evidence),
        evidence_unit_ids=tuple(unit.id for unit in evidence),
    )
    result = Extraction(
        source="source",
        kind="collection",
        units=[*evidence, distractor],
        summary_claims=[statement],
    )
    options = render_module.RenderOptions("json", True, False, 2)
    available, _ = render_module._settle_available(  # noqa: SLF001
        result,
        set(range(len(result.units))),
        options,
    )
    ceiling = 1
    for _ in range(20):
        candidate = render_module._settle_used(  # noqa: SLF001
            result,
            {0},
            options,
            requested=ceiling,
            available=available,
        )
        measured = len(candidate.encode("utf-8"))
        if measured <= ceiling:
            break
        ceiling = measured
    else:  # pragma: no cover - fixed-point design boundary
        raise AssertionError("one-unit budget did not converge")

    bounded = render_module.render(result, output="json", budget=ceiling)
    payload = json.loads(bounded)
    selected_ids = {unit["id"] for unit in payload["units"]}
    dropped = payload["manifest"]["selection"]["dropped"]

    assert len(bounded.encode("utf-8")) <= ceiling
    assert payload["summary_claims"] == []
    assert evidence[0].id in selected_ids
    assert distractor.id not in selected_ids, "claim evidence is ranked first"
    assert dropped["statement_count"] == 1
    assert dropped["reported_statements"] == [
        {
            "id": statement.id,
            "evidence_unit_ids": list(statement.evidence_unit_ids),
            "missing_evidence_unit_ids": [evidence[1].id, evidence[2].id],
            "origins": [
                {"source": origin.source, "ref": origin.ref}
                for origin in statement.origins
            ],
            "reason": "budget-evidence-omitted",
        }
    ]
    assert all(
        set(claim["evidence_unit_ids"]) <= selected_ids
        for claim in payload["summary_claims"]
    )


@contextmanager
def _http_server(handler_type: type[BaseHTTPRequestHandler]):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_type)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_crawl_rejects_cross_origin_redirect_before_target_request() -> None:
    target_hits = {"count": 0}

    class TargetHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib hook
            target_hits["count"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body>must not be fetched</body></html>")

        def log_message(self, _format: str, *_args: object) -> None:
            return

    with _http_server(TargetHandler) as target:
        class SourceHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - stdlib hook
                if self.path == "/start":
                    self.send_response(302)
                    self.send_header("Location", f"{target}/secret")
                    self.end_headers()
                    return
                self.send_response(404)
                self.end_headers()

            def log_message(self, _format: str, *_args: object) -> None:
                return

        with _http_server(SourceHandler) as source:
            acquisition = collection.crawl_documentation_site(
                f"{source}/start",
                limits=CollectionLimits(max_crawl_pages=2),
            )

    assert target_hits["count"] == 0
    assert not acquisition.extractions
    assert len(acquisition.declines) == 1
    assert acquisition.declines[0].kind is DeclineKind.CROSS_ORIGIN


def test_cli_model_flag_fails_before_synthesis_import_or_http(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = {"synthesis": 0, "http": 0}
    fake_synthesis = types.ModuleType("autotldr.synthesis")

    def synthesize(*_args: object, **_kwargs: object) -> None:
        calls["synthesis"] += 1

    def extract_url(*_args: object, **_kwargs: object) -> None:
        calls["http"] += 1

    fake_synthesis.synthesize = synthesize  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "autotldr.synthesis", fake_synthesis)
    monkeypatch.setattr(router, "extract_url", extract_url)

    with pytest.raises(SystemExit) as raised:
        cli_module.main(
            ["http://127.0.0.1:9/never", "--model", "linked-or-unknown"]
        )
    captured = capsys.readouterr()

    assert raised.value.code == 2
    assert calls == {"synthesis": 0, "http": 0}
    assert "cannot prove ZBook-local routing" in captured.err
    assert "run_local_candidates.py" in captured.err
