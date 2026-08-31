"""The synchronous Unix invoke surface.

Import discipline matters here more than anywhere else.  This module is loaded
on every invocation, so it imports only stdlib argument/path primitives at
module scope.  Format parsers, renderers, and acquisition code enter only after
the command line has selected a source.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

EXIT_OK = 0
EXIT_ERROR = 1
# argparse reserves 2 for command-line usage errors.  Runtime outcomes must not
# collide with it or shell callers cannot distinguish a typo from a deliberate
# named decline.
EXIT_UNSUPPORTED = 3
EXIT_NOT_FOUND = 4
EXIT_BUDGET = 5

_CORE_OUTPUTS = frozenset({"ansi", "md", "html", "pdf", "json", "jsonl"})
_OUTPUT_SUFFIXES = {
    ".md": "md",
    ".markdown": "md",
    ".html": "html",
    ".htm": "html",
    ".pdf": "pdf",
    ".json": "json",
    ".jsonl": "jsonl",
}


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _selected_output(explicit: str | None, destination: Path | None) -> str:
    """Resolve one output shape without making filenames authoritative.

    An explicit ``--out`` always wins.  Otherwise only the documented core
    suffixes opt into inference; stdout and unfamiliar suffixes retain ANSI.
    """

    if explicit is not None:
        return explicit
    if destination is None:
        return "ansi"
    return _OUTPUT_SUFFIXES.get(destination.suffix.casefold(), "ansi")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autotldr",
        description="Point it at anything. Get back what it means.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""commands:
  setup          configure one active LM Studio model
  doctor         verify config, model conformance, and dependencies
  config         show resolved settings and their source paths
  formats        show runtime-derived input and output support
  watch          keep per-file and folder TLDRs current
  integrations   inspect or install agent integrations
  mcp            serve the root-scoped local MCP tool

examples:
  autotldr setup
  autotldr doctor
  autotldr report.xlsx --detail brief
  autotldr ./handoff --detail deep --out html -o handoff.html
  autotldr report.xlsx --model off --out json
""",
    )
    parser.add_argument(
        "sources",
        nargs="+",
        metavar="SOURCE",
        help=(
            "one or more paths/HTTP(S) URLs, or - for stdin; multiple explicit "
            "sources are fused as one collection"
        ),
    )
    parser.add_argument(
        "--detail",
        choices=("brief", "standard", "deep"),
        help="answer detail: brief, standard (default), or deep",
    )
    parser.add_argument(
        "--out",
        metavar="FORMAT",
        help=(
            "output shape: ansi, md, html, pdf, json, jsonl, or a renderer supplied by "
            "an explicit --extension (default: inferred from -o, otherwise ansi)"
        ),
    )
    parser.add_argument(
        "--budget",
        type=_positive_int,
        metavar="N",
        help="hard complete-output ceiling in portable tokens (UTF-8 bytes)",
    )
    parser.add_argument(
        "--cite",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="show inline source spans in human output (default: on)",
    )
    parser.add_argument(
        "--type",
        dest="input_type",
        metavar="TYPE",
        help=(
            "explicit input type for stdin or a mislabeled path "
            "(for example markdown, json, typescript, rust, docx)"
        ),
    )
    parser.add_argument(
        "--crawl",
        action="store_true",
        help=(
            "treat one HTTP(S) source as a bounded same-origin documentation "
            "site collection"
        ),
    )
    parser.add_argument(
        "--extension",
        action="append",
        default=[],
        metavar="MODULE[:FACTORY]",
        help=(
            "explicitly enable one adapter package; repeat for multiple "
            "packages (there is no ambient plugin discovery)"
        ),
    )
    parser.add_argument(
        "--acquirer",
        metavar="NAME",
        help=(
            "use a named acquisition adapter from an explicit --extension "
            "for the single source"
        ),
    )
    parser.add_argument(
        "--model",
        metavar="MODE",
        help=(
            "model mode; use 'off' for an explicit deterministic evidence map "
            "(ordinary use reads the configured local model)"
        ),
    )
    parser.add_argument(
        "--allow-evidence-fallback",
        action="store_true",
        help="return a clearly labelled evidence map if local synthesis fails",
    )
    parser.add_argument(
        "--no-config",
        action="store_true",
        help="ignore user and project configuration for this invocation",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        metavar="FILE",
        help="write canonical text or PDF bytes to FILE instead of stdout",
    )
    return parser


def build_watch_parser() -> argparse.ArgumentParser:
    """Return the dependency-free Stage 6 watch command parser."""

    parser = argparse.ArgumentParser(
        prog="autotldr watch",
        description="Keep a folder's per-file TLDRs and fused roll-up current.",
    )
    parser.add_argument("source", type=Path, metavar="DIRECTORY")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--once",
        action="store_true",
        help="scan once and exit (useful for CI and foreground demos)",
    )
    mode.add_argument(
        "--status",
        action="store_true",
        help="print durable watch status as JSON without changing anything",
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="include non-hidden files below nested directories",
    )
    parser.add_argument(
        "--debounce",
        type=float,
        default=30.0,
        metavar="SECONDS",
        help="quiet window before a changed batch is processed (default: 30)",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=1.0,
        metavar="SECONDS",
        help="polling fallback interval (default: 1)",
    )
    parser.add_argument(
        "--budget",
        type=_positive_int,
        metavar="N",
        help="hard complete-output ceiling for each generated artifact",
    )
    parser.add_argument(
        "--detail",
        choices=("brief", "standard", "deep"),
        help="answer detail for per-file and folder TLDRs",
    )
    parser.add_argument(
        "--model",
        choices=("off",),
        help="use 'off' for explicit deterministic evidence artifacts",
    )
    parser.add_argument(
        "--allow-evidence-fallback",
        action="store_true",
        help="write labelled evidence artifacts when local synthesis fails",
    )
    parser.add_argument(
        "--no-config",
        action="store_true",
        help="ignore user and project configuration",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    if argv and argv[0] == "watch":
        return _watch_main(argv[1:])
    if argv and argv[0] == "mcp":
        from .mcp import main as mcp_main

        return mcp_main(argv[1:])
    if argv and argv[0] == "doctor":
        return _doctor_main(argv[1:])
    if argv and argv[0] == "setup":
        return _setup_main(argv[1:])
    if argv and argv[0] == "config":
        return _config_main(argv[1:])
    if argv and argv[0] in {"formats", "capabilities"}:
        return _formats_main(argv[1:])
    if argv and argv[0] == "integrations":
        return _integrations_main(argv[1:])

    # Answered before argparse so the version path does not require a source and
    # remains the cheapest possible invocation.
    if argv == ["--version"]:
        from . import __version__

        print(__version__)
        return EXIT_OK

    parser = build_parser()
    args = parser.parse_args(argv)
    args.out = _selected_output(args.out, args.output)

    if len(args.sources) > 1 and args.input_type is not None:
        parser.error("--type is valid only when exactly one source is supplied")
    if args.sources.count("-") > 1:
        parser.error("stdin source '-' may appear at most once")
    if len(set(args.sources)) != len(args.sources):
        parser.error("duplicate sources are not allowed in one collection")
    if args.crawl and (
        len(args.sources) != 1 or not _is_http_url_argument(args.sources[0])
    ):
        parser.error("--crawl requires exactly one HTTP(S) source")
    if args.crawl and args.input_type is not None:
        parser.error("--type cannot be combined with --crawl")
    if args.model is not None and args.model != "off":
        parser.error(
            "direct --model IDs cannot prove ZBook-local routing; configure product "
            "prose with `autotldr setup`, use --model off for evidence, or use "
            "run_local_candidates.py for guarded evaluation"
        )
    from .product import load_product_config

    try:
        product_config = load_product_config(use_config=not args.no_config)
    except ValueError as exc:
        parser.error(str(exc))
    import os

    explicit_model_off = args.model == "off" or os.environ.get(
        "AUTOTLDR_MODEL"
    ) == "off"
    configured_extensions = list(product_config.extensions)
    extension_references = [*configured_extensions, *args.extension]
    if len(set(extension_references)) != len(extension_references):
        parser.error("duplicate --extension references are not allowed")
    if args.acquirer is not None:
        if not extension_references:
            parser.error("--acquirer requires at least one --extension")
        if len(args.sources) != 1 or args.sources[0] == "-":
            parser.error("--acquirer requires exactly one non-stdin source")
        if args.crawl or args.input_type is not None:
            parser.error("--acquirer cannot be combined with --crawl or --type")

    from . import router

    try:
        registry = _load_explicit_extensions(extension_references, router=router)
        _validate_selected_output(parser, args.out, registry)
        _validate_selected_acquirer(parser, args.acquirer, registry)
        if decline := _explicit_input_type_decline(
            args.input_type,
            registry=registry,
            router=router,
        ):
            print(f"autotldr: {decline}", file=sys.stderr)
            return EXIT_UNSUPPORTED
        if not explicit_model_off:
            from .product import require_active_model, require_configured_model

            try:
                require_active_model(require_configured_model(product_config))
            except ValueError as exc:
                print(f"autotldr: {exc}", file=sys.stderr)
                return EXIT_ERROR
        from .api import acquire

        result = acquire(
            args.sources,
            input_type=args.input_type,
            crawl=args.crawl,
            stdin=_read_stdin() if "-" in args.sources else None,
            registry=registry,
            acquirer=args.acquirer,
        )

        # A recognized PDF without a text layer is a named Tier 4 decline,
        # not an empty successful Tier 1 result.  The library extraction still
        # retains the useful absence finding.
        if result.kind == "pdf" and not result.units and any(
            "scanned" in gap.casefold() for gap in result.gaps
        ):
            print(
                f"autotldr: {result.source}: scanned PDF needs OCR (tier 4), "
                "which this text-only stage does not read",
                file=sys.stderr,
            )
            return EXIT_UNSUPPORTED

        if registry is not None:
            result.meta["extensions"] = {
                "schema": "autotldr-explicit-extension-run-v1",
                "requested": list(extension_references),
                "capabilities": registry.capability_manifest(),
            }

        from .api import apply_product_synthesis

        result, _synthesis = apply_product_synthesis(
            result,
            detail=args.detail,
            mode="evidence" if explicit_model_off else "prose",
            allow_evidence_fallback=(
                True if args.allow_evidence_fallback else None
            ),
            product_config=product_config,
        )

        from .render import BudgetTooSmall

        color = (
            args.out == "ansi"
            and args.output is None
            and _stdout_is_tty()
            and not _no_color()
        )
        try:
            if args.out == "pdf":
                from .share import render_pdf

                rendered = render_pdf(
                    result,
                    budget=args.budget,
                    cite=args.cite,
                )
            else:
                from .render import render

                rendered = render(
                    result,
                    output=args.out,
                    budget=args.budget,
                    cite=args.cite,
                    color=color,
                    registry=registry,
                )
        except BudgetTooSmall as exc:
            print(f"autotldr: {exc}", file=sys.stderr)
            return EXIT_BUDGET

        _write(rendered, args.output)
        return EXIT_OK

    except router.UnsupportedFormat as exc:
        print(f"autotldr: {exc}", file=sys.stderr)
        return EXIT_UNSUPPORTED
    except router.UnknownFormat as exc:
        print(f"autotldr: {exc}", file=sys.stderr)
        return EXIT_UNSUPPORTED
    except FileNotFoundError as exc:
        missing = exc.filename or str(exc)
        print(f"autotldr: {missing}: no such file", file=sys.stderr)
        return EXIT_NOT_FOUND
    except BrokenPipeError:
        # A downstream consumer such as ``head`` closing early is a successful
        # Unix pipeline, not a reason to print a traceback.
        return EXIT_OK
    except (ImportError, OSError, UnicodeError, ValueError) as exc:
        print(f"autotldr: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:  # pragma: no cover - final CLI containment boundary
        print(f"autotldr: {exc}", file=sys.stderr)
        return EXIT_ERROR


def _watch_main(argv: list[str]) -> int:
    """Run Stage 6 without changing ordinary invoke-mode behavior."""

    import json
    from dataclasses import asdict

    parser = build_watch_parser()
    args = parser.parse_args(argv)
    if args.debounce < 0:
        parser.error("--debounce must not be negative")
    if args.poll_interval <= 0:
        parser.error("--poll-interval must be positive")

    from .watch import run_once, status, watch

    try:
        if args.status:
            snapshot = status(args.source)
            payload = asdict(snapshot)
            payload["root"] = str(snapshot.root)
            payload["store"] = str(snapshot.store)
            for item in payload["files"]:
                if item["artifact"] is not None:
                    item["artifact"] = str(item["artifact"])
            print(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                )
            )
            return EXIT_OK

        result = (
            run_once(
                args.source,
                recursive=args.recursive,
                budget=args.budget,
                detail=args.detail,
                mode="evidence" if args.model == "off" else None,
                allow_evidence_fallback=(
                    True if args.allow_evidence_fallback else None
                ),
                use_config=not args.no_config,
            )
            if args.once
            else watch(
                args.source,
                recursive=args.recursive,
                budget=args.budget,
                detail=args.detail,
                mode="evidence" if args.model == "off" else None,
                allow_evidence_fallback=(
                    True if args.allow_evidence_fallback else None
                ),
                use_config=not args.no_config,
                debounce=args.debounce,
                poll_interval=args.poll_interval,
            )
        )
        print(
            json.dumps(
                {
                    "root": str(result.root),
                    "run_id": result.run_id,
                    "scanned": result.scanned,
                    "changed": result.changed,
                    "unchanged": result.unchanged,
                    "succeeded": result.succeeded,
                    "failed": result.failed,
                    "removed": result.removed,
                    "rollup_written": result.rollup_written,
                    "artifacts": [str(item) for item in result.artifacts],
                    "errors": [asdict(item) for item in result.errors],
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return EXIT_ERROR if result.failed else EXIT_OK
    except KeyboardInterrupt:
        print("autotldr: watch stopped", file=sys.stderr)
        return EXIT_OK
    except FileNotFoundError as exc:
        missing = exc.filename or str(exc)
        print(f"autotldr: {missing}: no such directory", file=sys.stderr)
        return EXIT_NOT_FOUND
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"autotldr: {exc}", file=sys.stderr)
        return EXIT_ERROR


def _formats_main(argv: list[str]) -> int:
    """Print the inventory the live installation can actually route."""

    parser = argparse.ArgumentParser(
        prog="autotldr formats",
        description=(
            "Show available leaf formats, collection inputs, dependencies, "
            "and scoped declines."
        ),
    )
    parser.add_argument("--json", action="store_true", help="emit canonical JSON")
    args = parser.parse_args(argv)
    from .capabilities import runtime_capabilities

    inventory = runtime_capabilities()
    if args.json:
        import json

        print(json.dumps(inventory, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return EXIT_OK
    print("AutoTLDR runtime formats")
    print()
    for tier in range(4):
        rows = [item for item in inventory["inputs"] if item["tier"] == tier]
        if not rows:
            continue
        print(f"Tier {tier}")
        for item in rows:
            suffixes = ", ".join(item["suffixes"])
            status = item["status"]
            install = f"; install {item['install']}" if item["install"] else ""
            print(f"  {item['kind']:<16} {status:<18} {suffixes}{install}")
        print()
    print("Tier 2 collections")
    for item in inventory["collection_capabilities"]:
        addresses = [
            *item["suffixes"],
            *[f"{scheme}://" for scheme in item["schemes"]],
        ]
        shown = ", ".join(addresses) if addresses else "path"
        print(f"  {item['name']:<16} {item['status']:<18} {shown}")
    print()
    print("Outputs")
    for item in inventory["output_capabilities"]:
        install = f"; install {item['install']}" if item["install"] else ""
        print(f"  {item['name']:<16} {item['status']}{install}")
    print("Detail: " + ", ".join(inventory["detail_levels"]))
    return EXIT_OK


def _setup_main(argv: list[str]) -> int:
    """Discover and persist one explicit model on the certified alpha runtime."""

    from .product import DEFAULT_LOCAL_ENDPOINT

    parser = argparse.ArgumentParser(
        prog="autotldr setup",
        description="Configure one already-active LM Studio model for cited prose TLDRs.",
    )
    parser.add_argument("--model", metavar="ID", help="exact active model ID")
    parser.add_argument(
        "--detail",
        choices=("brief", "standard", "deep"),
        default="standard",
    )
    parser.add_argument("--force", action="store_true", help="replace existing user config")
    args = parser.parse_args(argv)
    from .product import (
        LocalModelUnavailable,
        ModelProfile,
        ProductConfigError,
        discover_served_models,
        write_user_model_config,
    )

    try:
        models = discover_served_models(DEFAULT_LOCAL_ENDPOINT)
        if args.model is None:
            if not models:
                raise LocalModelUnavailable(
                    "LM Studio has no eligible local generation model active; "
                    "load one directly on this machine and retry"
                )
            if len(models) != 1:
                choices = ", ".join(models)
                raise LocalModelUnavailable(
                    f"multiple generation models are active ({choices}); rerun with --model ID"
                )
            selected = models[0]
        else:
            selected = args.model
            if selected not in models:
                raise LocalModelUnavailable(
                    f"model {selected!r} is not active; active: {', '.join(models) or 'none'}"
                )
        path = write_user_model_config(
            ModelProfile(DEFAULT_LOCAL_ENDPOINT, selected),
            detail=args.detail,
            force=args.force,
        )
    except (ProductConfigError, LocalModelUnavailable, OSError) as exc:
        print(f"autotldr setup: {exc}", file=sys.stderr)
        return EXIT_ERROR
    print(f"Configured local model {selected!r} in {path}")
    print("Run `autotldr doctor` to verify the complete installation.")
    return EXIT_OK


def _doctor_main(argv: list[str]) -> int:
    """Diagnose product readiness without acquiring user sources."""

    parser = argparse.ArgumentParser(
        prog="autotldr doctor",
        description="Check configuration, local model service, and format dependencies.",
    )
    parser.add_argument("--json", action="store_true", help="emit canonical JSON")
    parser.add_argument("--no-config", action="store_true", help="ignore persisted config")
    args = parser.parse_args(argv)

    from .capabilities import runtime_capabilities
    from .product import (
        LocalModelUnavailable,
        ProductConfigError,
        discover_local_runtime,
        load_product_config,
        probe_model_profile,
        user_config_path,
    )

    checks: list[dict[str, object]] = []
    ready = True
    try:
        config = load_product_config(use_config=not args.no_config)
    except ProductConfigError as exc:
        config = None
        ready = False
        checks.append({"name": "config", "status": "error", "detail": str(exc)})
    else:
        checks.append(
            {
                "name": "config",
                "status": "ok" if config.sources else "missing",
                "detail": (
                    ", ".join(config.sources)
                    if config.sources
                    else f"run `autotldr setup`; expected {user_config_path()}"
                ),
            }
        )
        if config.model is None:
            ready = False
            checks.append(
                {"name": "local-model", "status": "missing", "detail": "run `autotldr setup`"}
            )
        else:
            try:
                runtime = discover_local_runtime(config.model.endpoint)
            except LocalModelUnavailable as exc:
                ready = False
                checks.append({"name": "local-model", "status": "error", "detail": str(exc)})
            else:
                exact = (
                    runtime.active_state_verified
                    and runtime.provider == config.model.runtime
                    and config.model.model in runtime.active_models
                )
                ready &= exact
                checks.append(
                    {
                        "name": "local-model",
                        "status": "ok" if exact else "error",
                        "detail": (
                            f"{config.model.model} active in {runtime.provider} at "
                            f"{config.model.endpoint}"
                            if exact
                            else f"configured {config.model.model!r}; active: "
                            f"{', '.join(runtime.active_models) or 'unverified/none'}"
                        ),
                    }
                )
                if exact:
                    try:
                        probe = probe_model_profile(config.model)
                    except Exception as exc:
                        ready = False
                        checks.append(
                            {
                                "name": "grounded-prose",
                                "status": "error",
                                "detail": (
                                    f"{type(exc).__name__}: local completion did not "
                                    "pass the grounded response contract"
                                ),
                            }
                        )
                    else:
                        checks.append(
                            {
                                "name": "grounded-prose",
                                "status": "ok",
                                "detail": (
                                    f"accepted bounded diagnostic response from "
                                    f"{probe['model']}"
                                ),
                            }
                        )

    inventory = runtime_capabilities()
    available = sum(item["status"] == "available" for item in inventory["inputs"])
    missing = [item for item in inventory["inputs"] if item["status"] == "missing-dependency"]
    checks.append(
        {
            "name": "formats",
            "status": "ok" if not missing else "partial",
            "detail": f"{available} format families available; {len(missing)} dependency-gated",
        }
    )
    report = {
        "schema": "autotldr-doctor-v1",
        "ready": ready,
        "checks": checks,
        "capabilities": inventory,
    }
    if args.json:
        import json

        print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    else:
        print("AutoTLDR doctor")
        for check in checks:
            print(f"  {str(check['status']).upper():<7} {check['name']}: {check['detail']}")
        print()
        print("Ready for cited local prose." if ready else "Not ready; follow the actions above.")
    return EXIT_OK if ready else EXIT_ERROR


def _config_main(argv: list[str]) -> int:
    """Make layered configuration visible without requiring TOML knowledge."""

    parser = argparse.ArgumentParser(
        prog="autotldr config",
        description="Inspect AutoTLDR configuration and precedence.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    show = subparsers.add_parser("show", help="show the resolved configuration")
    show.add_argument("--json", action="store_true", help="emit canonical JSON")
    subparsers.add_parser("paths", help="show user and project configuration paths")
    args = parser.parse_args(argv)
    from .product import (
        ProductConfigError,
        load_product_config,
        project_config_path,
        user_config_path,
    )

    if args.command == "paths":
        print(f"user: {user_config_path()}")
        print(f"project: {project_config_path()}")
        return EXIT_OK
    try:
        config = load_product_config()
    except ProductConfigError as exc:
        print(f"autotldr config: {exc}", file=sys.stderr)
        return EXIT_ERROR
    manifest = config.as_manifest()
    if args.json:
        import json

        print(json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return EXIT_OK
    print("AutoTLDR resolved configuration")
    print(f"  detail: {config.detail}")
    print(f"  evidence fallback: {'on' if config.allow_evidence_fallback else 'off'}")
    print(
        "  model: "
        + (
            f"{config.model.model} at {config.model.endpoint}"
            if config.model is not None
            else "not configured"
        )
    )
    print(f"  extensions: {', '.join(config.extensions) if config.extensions else 'none'}")
    print(f"  loaded from: {', '.join(config.sources) if config.sources else 'built-in defaults'}")
    return EXIT_OK


def _integrations_main(argv: list[str]) -> int:
    """Expose version-matched agent assets carried by the distribution."""

    parser = argparse.ArgumentParser(
        prog="autotldr integrations",
        description="Inspect or install AutoTLDR's version-matched agent integration.",
    )
    subparsers = parser.add_subparsers(dest="integration", required=True)
    skill = subparsers.add_parser(
        "skill",
        help="show or install the bundled AutoTLDR Agent Skill",
    )
    action = skill.add_mutually_exclusive_group()
    action.add_argument(
        "--install",
        type=Path,
        metavar="SKILLS_DIRECTORY",
        help="copy the skill to SKILLS_DIRECTORY/autotldr",
    )
    action.add_argument(
        "--print",
        dest="print_skill",
        action="store_true",
        help="print the bundled SKILL.md",
    )
    args = parser.parse_args(argv)

    try:
        source = _bundled_agent_skill()
        if args.install is not None:
            import shutil

            parent = args.install.expanduser().resolve()
            target = parent / "autotldr"
            if target.exists():
                raise FileExistsError(
                    f"{target} already exists; remove or rename it before reinstalling"
                )
            parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, target)
            print(f"Installed AutoTLDR Agent Skill in {target}")
        elif args.print_skill:
            sys.stdout.write((source / "SKILL.md").read_text(encoding="utf-8"))
        else:
            print(source)
        return EXIT_OK
    except (FileNotFoundError, OSError) as exc:
        print(f"autotldr integrations: {exc}", file=sys.stderr)
        return EXIT_ERROR


def _bundled_agent_skill() -> Path:
    """Resolve package data, with a source-checkout fallback for editable installs."""

    from importlib.resources import files

    packaged = files("autotldr").joinpath("integrations/skills/autotldr")
    if packaged.is_dir():
        return Path(str(packaged))
    checkout = Path(__file__).resolve().parents[2] / "integrations/skills/autotldr"
    if checkout.is_dir():
        return checkout
    raise FileNotFoundError("the installed distribution does not contain the AutoTLDR skill")


def _validate_selected_output(
    parser: argparse.ArgumentParser,
    name: str,
    registry,
) -> None:
    """Reject an unknown selected renderer before acquiring any source."""

    if name in _CORE_OUTPUTS:
        return
    if registry is not None:
        try:
            registry.get_renderer(name)
        except (LookupError, ValueError):
            pass
        else:
            return
    parser.error(
        f"unknown --out format {name!r}; choose ansi, md, html, pdf, json, "
        "jsonl, or a renderer supplied by --extension"
    )


def _validate_selected_acquirer(
    parser: argparse.ArgumentParser,
    name: str | None,
    registry,
) -> None:
    """Reject an unknown selected acquisition adapter before source I/O."""

    if name is None:
        return
    try:
        registry.get_acquisition(name)
    except (LookupError, ValueError):
        parser.error(
            f"unknown --acquirer name {name!r}; choose an acquisition adapter "
            "supplied by --extension"
        )


def _explicit_input_type_decline(name: str | None, *, registry, router):
    """Return a named format decline for an invalid explicit routing hint."""

    if name is None:
        return None
    normalized = name.casefold().lstrip(".")
    try:
        handler, suffix = router._handler_for_kind(name, registry=registry)
    except ValueError:
        deferred = router._DEFERRED.get(f".{normalized}")
        if deferred is not None:
            return router.UnsupportedFormat(
                f"explicit input type {name!r}",
                deferred[0],
                deferred[1],
            )
        return router.UnknownFormat(f"explicit input type {name!r}")
    if suffix in router._UNAVAILABLE_SOURCE_SUFFIXES:
        return router.UnsupportedFormat(
            f"explicit input type {name!r}",
            f"{router._UNAVAILABLE_SOURCE_NAMES[suffix]} source",
            handler.tier,
        )
    return None


def _load_explicit_extensions(references: list[str], *, router):
    """Build one invocation-scoped registry; never inspect installed packages."""

    if not references:
        return None
    from .extensions import ExtensionRegistry, load_extension

    registry = ExtensionRegistry()
    for reference in references:
        load_extension(reference, registry)

    # Each core surface owns its own reservation table.  Validate the complete
    # registry before any adapter is resolved or any user source is acquired.
    router.validate_extension_registry(registry)
    from .collection import validate_extension_registry as validate_acquisitions
    from .render import validate_extension_registry as validate_renderers

    validate_acquisitions(registry)
    validate_renderers(registry)
    return registry


def _write(rendered: str | bytes, output: Path | None) -> None:
    payload = rendered if isinstance(rendered, bytes) else rendered.encode("utf-8")
    if output is not None:
        output.write_bytes(payload)
        return

    # Counted UTF-8 bytes must be the bytes that hit the pipe regardless of the
    # caller's PYTHONIOENCODING.  StringIO-like embedders have no byte stream and
    # receive the already-canonical text as the compatibility fallback.
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is not None:
        buffer.write(payload)
    else:
        if isinstance(rendered, bytes):
            raise OSError("binary PDF output requires a binary stdout or --output FILE")
        sys.stdout.write(rendered)


def _stdout_is_tty() -> bool:
    try:
        return sys.stdout.isatty()
    except (AttributeError, OSError):
        return False


def _read_stdin() -> str | bytes:
    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is not None:
        return buffer.read()
    return sys.stdin.read()


def _no_color() -> bool:
    import os

    return "NO_COLOR" in os.environ


def _is_http_url_argument(value: str) -> bool:
    return value.casefold().startswith(("http://", "https://"))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
