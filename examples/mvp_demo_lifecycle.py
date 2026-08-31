#!/usr/bin/env python3
"""Run the MVP demo inside AutoTLDR's guarded ZBook model lifecycle.

This wrapper deliberately contains no second lifecycle implementation.  It
specializes the audited Stage 2 runner with one command: the mixed-collection
MVP demo.  The runner resolves only an exact unprefixed ZBook catalog path,
requires a 100% GPU-offload estimate, loads one ``autotldr-*`` instance, keeps
LM Link routed to the verified ZBook during inference, unloads that exact row,
and restores an explicitly authorized incumbent and the original preference.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "examples" / "mvp_demo.py"
LIFECYCLE = ROOT / "benchmarks" / "roles" / "run_local_candidates.py"


def _load_lifecycle() -> Any:
    specification = importlib.util.spec_from_file_location(
        "autotldr_mvp_lifecycle_base", LIFECYCLE
    )
    if specification is None or specification.loader is None:
        raise RuntimeError(f"cannot load lifecycle module from {LIFECYCLE}")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


base = _load_lifecycle()


class MVPDemoRunner(base.SequentialPilotRunner):
    """Replace the benchmark evaluator with one complete product demo."""

    def __init__(self, *, source: Path, artifact_dir: Path, **kwargs: Any) -> None:
        super().__init__(
            evaluator=DEMO,
            items_path=source,
            prompt_path=ROOT / "benchmarks" / "synthesis" / "policy.json",
            policy_path=ROOT / "benchmarks" / "synthesis" / "policy.json",
            output_dir=artifact_dir / ".lifecycle",
            context_length=16_384,
            parallel=1,
            **kwargs,
        )
        self.source = source
        self.artifact_dir = artifact_dir

    def _evaluate_command(self, candidate: Any) -> list[str]:
        return [
            self.python_executable,
            str(DEMO),
            str(self.source),
            "--model",
            candidate.identifier,
            "--output-dir",
            str(self.artifact_dir),
        ]

    def _evaluate(
        self,
        candidate: Any,
        link_snapshot: Any,
        fingerprint: Any,
        attestation: Any,
    ) -> None:
        """Keep inference pinned to ZBook and attest before and after it."""

        def infer() -> None:
            self._verify_link_identity(
                link_snapshot,
                expected_preferred=link_snapshot.local_device_identifier,
            )
            self._verify_fingerprint(
                fingerprint, link_snapshot, phase="before MVP inference"
            )
            before = self._attest_residency(
                fingerprint, phase="before MVP inference"
            )
            if before != attestation:
                raise base.ResidencyAttestationError(
                    "candidate process or residency changed before MVP inference",
                    fingerprint_sha256=fingerprint.sha256,
                    phase="before MVP inference",
                )
            self._run(
                self._evaluate_command(candidate),
                "run the complete AutoTLDR MVP demo",
                self.timeouts.evaluator_seconds,
                terminate_process_group=True,
            )
            self._verify_link_identity(
                link_snapshot,
                expected_preferred=link_snapshot.local_device_identifier,
            )
            self._verify_fingerprint(
                fingerprint, link_snapshot, phase="after MVP inference"
            )
            after = self._attest_residency(
                fingerprint, phase="after MVP inference"
            )
            if after != attestation:
                raise base.ResidencyAttestationError(
                    "candidate process or residency changed during MVP inference",
                    fingerprint_sha256=fingerprint.sha256,
                    phase="after MVP inference",
                )

        self._on_zbook(link_snapshot, "ZBook-local MVP inference", infer)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="mixed local collection directory")
    parser.add_argument(
        "--model-ref",
        required=True,
        help="exact unprefixed ZBook catalog path; linked/colon paths are refused",
    )
    parser.add_argument(
        "--name",
        default="mvp-ornith",
        help="owned instance suffix (default creates autotldr-mvp-ornith)",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--incumbent",
        help="exact pre-existing local instance ID to snapshot and restore",
    )
    parser.add_argument(
        "--lms-executable",
        default=base.DEFAULT_LMS_EXECUTABLE,
        help="ZBook LMS executable (or set AUTOTLDR_LMS_CLI)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the guarded command/check plan without changing model state",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        candidate = base.Candidate(args.name, args.model_ref)
        runner = MVPDemoRunner(
            source=args.source.resolve(),
            artifact_dir=args.output_dir.resolve(),
            lms_executable=args.lms_executable,
        )
        if args.dry_run:
            print(
                "\n".join(
                    runner.dry_run_commands(
                        [candidate], incumbent_identifier=args.incumbent
                    )
                )
            )
            return 0
        runner.run([candidate], incumbent_identifier=args.incumbent)
    except (base.LifecycleError, RuntimeError) as exc:
        print(f"AutoTLDR MVP lifecycle error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
