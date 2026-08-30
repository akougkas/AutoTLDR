"""Command line entry point.

Import discipline matters here more than anywhere else. This module is loaded on
every invocation, so it imports only from the stdlib at module scope and defers
everything else until the arguments say it is needed. See tests/test_startup.py,
which fails the build if that slips.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_UNSUPPORTED = 2
EXIT_NOT_FOUND = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autotldr",
        description="Point it at anything. Get back what it means.",
    )
    parser.add_argument("source", type=Path, help="file to read")
    parser.add_argument(
        "--out",
        choices=("json", "jsonl", "summary"),
        default="summary",
        help="output shape (default: summary)",
    )
    parser.add_argument(
        "--version", action="store_true", help="print the version and exit"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    # Answered before argparse so that --version stays on the cheapest path.
    if argv and argv[0] == "--version":
        from . import __version__

        print(__version__)
        return EXIT_OK

    args = build_parser().parse_args(argv)

    if not args.source.exists():
        print(f"autotldr: {args.source}: no such file", file=sys.stderr)
        return EXIT_NOT_FOUND

    from .router import UnknownFormat, UnsupportedFormat, extract

    try:
        result = extract(args.source)
    except UnsupportedFormat as exc:
        print(f"autotldr: {exc}", file=sys.stderr)
        return EXIT_UNSUPPORTED
    except UnknownFormat as exc:
        print(f"autotldr: {exc}", file=sys.stderr)
        return EXIT_UNSUPPORTED
    except ImportError as exc:
        print(f"autotldr: {exc}", file=sys.stderr)
        return EXIT_ERROR

    if args.out == "summary":
        _write_summary(result)
    else:
        from .render import to_json, to_jsonl

        sys.stdout.write(to_json(result) if args.out == "json" else to_jsonl(result))

    return EXIT_OK


def _write_summary(result) -> None:
    """A terse human view, adequate for the representation spike.

    The real renderers land in Stage 3. This exists so a person can eyeball what
    an extractor produced without reading JSON.
    """
    from .unit import Modality

    print(f"{result.source}  [{result.kind}]")
    print(f"  {len(result.units)} units, {len(result.relations)} relations, {result.tokens} tokens")

    counts: dict[str, int] = {}
    for unit in result.units:
        counts[unit.role] = counts.get(unit.role, 0) + 1
    if counts:
        shown = ", ".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))
        print(f"  roles: {shown}")

    for unit in sorted(result.units, key=lambda u: -u.salience)[:8]:
        if unit.modality is Modality.REFERENCE:
            continue
        body = " ".join(unit.content.split())
        if len(body) > 96:
            body = body[:95] + "…"
        print(f"    {unit.origin.ref:<24} {body}")

    if result.gaps:
        print("  gaps:")
        for gap in result.gaps:
            print(f"    - {gap}")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
