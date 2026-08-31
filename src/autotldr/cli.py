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


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autotldr",
        description="Point it at anything. Get back what it means.",
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
        "--out",
        default="ansi",
        metavar="FORMAT",
        help=(
            "output shape: ansi, md, html, pdf, json, jsonl, or a renderer supplied by "
            "an explicit --extension (default: ansi)"
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
        metavar="LOADED_MODEL_ID",
        help=(
            "reserved for the guarded ZBook-local lifecycle runner; direct "
            "invoke synthesis is currently disabled"
        ),
    )
    parser.add_argument(
        "--evidence-budget",
        type=_positive_int,
        default=12_000,
        metavar="N",
        help="hard canonical UTF-8 byte ceiling for model evidence (default: 12000)",
    )
    parser.add_argument(
        "--require-synthesis",
        action="store_true",
        help="fail instead of using the grounded deterministic fallback if synthesis fails",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        metavar="FILE",
        help="write canonical text or PDF bytes to FILE instead of stdout",
    )
    parser.add_argument(
        "--version", action="store_true", help="print the version and exit"
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
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    if argv and argv[0] == "watch":
        return _watch_main(argv[1:])
    if argv and argv[0] == "mcp":
        from .mcp import main as mcp_main

        return mcp_main(argv[1:])

    # Answered before argparse so the version path does not require a source and
    # remains the cheapest possible invocation.
    if "--version" in argv:
        from . import __version__

        print(__version__)
        return EXIT_OK

    parser = build_parser()
    args = parser.parse_args(argv)

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
    if args.require_synthesis and args.model is None:
        parser.error("--require-synthesis requires --model")
    if args.model is not None:
        parser.error(
            "--model is disabled because invoke mode cannot prove ZBook-local "
            "routing or deviceIdentifier:null; use the guarded Stage 5 runner "
            "at benchmarks/synthesis/run_local_candidates.py"
        )
    if len(set(args.extension)) != len(args.extension):
        parser.error("duplicate --extension references are not allowed")
    if args.acquirer is not None:
        if not args.extension:
            parser.error("--acquirer requires at least one --extension")
        if len(args.sources) != 1 or args.sources[0] == "-":
            parser.error("--acquirer requires exactly one non-stdin source")
        if args.crawl or args.input_type is not None:
            parser.error("--acquirer cannot be combined with --crawl or --type")

    from . import router

    try:
        registry = _load_explicit_extensions(args.extension, router=router)
        if args.acquirer is not None:
            acquisition, acquisition_spec = _run_extension_acquirer(
                args.sources[0],
                args.acquirer,
                registry,
            )
            from .api import assemble_collection

            result = assemble_collection(
                acquisition.extractions,
                subject=acquisition.source,
                acquisitions=(acquisition,),
            )
        else:
            from .api import acquire

            result = acquire(
                args.sources,
                input_type=args.input_type,
                crawl=args.crawl,
                stdin=_read_stdin() if "-" in args.sources else None,
                registry=registry,
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
                "requested": list(args.extension),
                "capabilities": registry.capability_manifest(),
            }
            if args.acquirer is not None:
                result.meta["extension_acquisition"] = acquisition_spec.as_manifest()

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
            )
            if args.once
            else watch(
                args.source,
                recursive=args.recursive,
                budget=args.budget,
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


def _run_extension_acquirer(source: str, name: str, registry):
    """Invoke one explicitly selected collection adapter behind a closed seam."""

    from .extensions import (
        ExtensionConformanceError,
        validate_acquisition_output,
    )

    try:
        spec = registry.get_acquisition(name)
    except LookupError:
        raise ValueError(f"unknown extension acquisition adapter: {name}") from None
    adapter = registry.resolve_acquisition(spec)
    try:
        raw = adapter(source)
    except Exception:
        raise ExtensionConformanceError(
            f"extension acquisition adapter {spec.name!r} failed"
        ) from None
    acquisition = validate_acquisition_output(raw)
    _validate_extension_acquisition(acquisition, spec=spec)
    return acquisition, spec


def _validate_extension_acquisition(acquisition, *, spec) -> None:
    """Require exact routed leaves before an extension collection may fuse."""

    import json
    import re

    from .extensions import ExtensionConformanceError
    from .render import _validate_result

    if not acquisition.extractions and not acquisition.declines:
        raise ExtensionConformanceError(
            f"extension acquisition adapter {spec.name!r} returned an empty success"
        )
    try:
        json.dumps(
            acquisition.manifest,
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError):
        raise ExtensionConformanceError(
            f"extension acquisition adapter {spec.name!r} returned a non-canonical manifest"
        ) from None

    for extraction in acquisition.extractions:
        try:
            _validate_result(extraction)
        except (TypeError, ValueError):
            raise ExtensionConformanceError(
                f"extension acquisition adapter {spec.name!r} returned an invalid routed leaf"
            ) from None
        inputs = extraction.meta.get("inputs")
        if (
            not isinstance(inputs, list)
            or len(inputs) != 1
            or not isinstance(inputs[0], dict)
        ):
            raise ExtensionConformanceError(
                f"extension acquisition adapter {spec.name!r} returned a leaf "
                "without one exact input manifest"
            )
        record = inputs[0]
        digest = record.get("sha256")
        byte_count = record.get("bytes")
        if (
            record.get("source") != extraction.source
            or not isinstance(byte_count, int)
            or isinstance(byte_count, bool)
            or byte_count < 0
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            raise ExtensionConformanceError(
                f"extension acquisition adapter {spec.name!r} returned an "
                "inexact leaf input manifest"
            )


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
