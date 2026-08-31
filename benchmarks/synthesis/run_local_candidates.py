#!/usr/bin/env python3
"""Run the exact Stage 5 synthesis candidates under the guarded ZBook lifecycle.

The residency implementation is inherited from Stage 2's audited
``SequentialPilotRunner``.  Stage 5 changes only the benchmark command and one
important routing condition: the verified ZBook device remains preferred for
the complete localhost HTTP inference call.  The inherited transaction then
restores the original LM Link preference, unloads only the exact AutoTLDR-owned
row, verifies zero local residents, and eventually restores an explicitly
authorized incumbent.

No arbitrary ``--candidate`` option exists.  Candidate order, installed model
references, context length, and parallelism come from the frozen policy.
"""

from __future__ import annotations

import argparse
import importlib.util
import shlex
import sys
from pathlib import Path
from typing import Any, Sequence


HERE = Path(__file__).resolve().parent
POLICY_PATH = HERE / "policy.json"
EVALUATOR_PATH = HERE / "evaluate.py"
DEFAULT_FREEZE_PATH = HERE / "freeze.json"
DEFAULT_OUTPUT_DIR = HERE / "outputs"
BASE_RUNNER_PATH = HERE.parent / "roles" / "run_local_candidates.py"


def _load_module(name: str, path: Path) -> Any:
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        raise RuntimeError(f"cannot load Python module from {path}")
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


base = _load_module("autotldr_stage5_lifecycle_base", BASE_RUNNER_PATH)
evaluator = _load_module("autotldr_stage5_synthesis_evaluator", EVALUATOR_PATH)


def exact_candidates(policy_path: Path = POLICY_PATH) -> tuple[Any, ...]:
    """Return the four frozen candidates in their preregistered order."""

    policy, _ = evaluator.load_policy(policy_path)
    return tuple(
        base.Candidate(item["name"], item["installed_model"])
        for item in evaluator.policy_candidates(policy)
    )


def verify_full_freeze(path: Path) -> None:
    """Recompute and verify the complete immutable Stage 5 freeze record."""

    frozen = evaluator.read_freeze(path)
    evaluator.verify_freeze_record(frozen)


class Stage5CandidateRunner(base.SequentialPilotRunner):
    """Specialize the safe lifecycle for grounded synthesis evaluation."""

    def __init__(
        self,
        *,
        freeze_path: Path = DEFAULT_FREEZE_PATH,
        policy_path: Path = POLICY_PATH,
        output_dir: Path = DEFAULT_OUTPUT_DIR,
        freeze_verifier: Any = verify_full_freeze,
        **kwargs: Any,
    ) -> None:
        policy, _ = evaluator.load_policy(policy_path)
        generation = policy["generation"]
        super().__init__(
            evaluator=EVALUATOR_PATH,
            # The base class stores these paths, but Stage 5's command uses
            # only its explicit freeze and policy-derived configuration.
            items_path=HERE / "hero" / "borealis",
            prompt_path=freeze_path,
            policy_path=policy_path,
            output_dir=output_dir,
            context_length=generation["context_length"],
            parallel=generation["parallel"],
            **kwargs,
        )
        self.freeze_path = freeze_path
        self.freeze_verifier = freeze_verifier

    @staticmethod
    def _linked_state(rows: Sequence[Any]) -> bytes:
        linked = [row.raw for row in rows if row.is_linked]
        linked.sort(
            key=lambda row: (
                str(row.get("deviceIdentifier") or ""),
                str(row.get("identifier") or ""),
                str(row.get("modelKey") or ""),
                str(row.get("path") or ""),
            )
        )
        return evaluator._canonical_bytes(linked)

    def run(
        self,
        candidates: Sequence[Any],
        *,
        incumbent_identifier: str | None = None,
    ) -> None:
        """Run only the exact policy set and audit linked peers before/after."""

        expected = exact_candidates(self.policy_path)
        observed_identity = [(item.name, item.model) for item in candidates]
        expected_identity = [(item.name, item.model) for item in expected]
        if observed_identity != expected_identity:
            raise base.LifecycleError(
                "Stage 5 runner requires the exact four policy candidates in order"
            )

        # Candidate artifacts are write-once evidence.  Refuse a partial or
        # repeated run before even inspecting or changing LM Link state; the
        # per-candidate evaluator guard would otherwise discover the conflict
        # only after an incumbent had been unloaded and a candidate loaded.
        existing_outputs = [
            self.output_dir / f"{candidate.slug}.json"
            for candidate in candidates
            if (self.output_dir / f"{candidate.slug}.json").exists()
        ]
        if existing_outputs:
            names = ", ".join(path.name for path in existing_outputs)
            raise base.LifecycleError(
                f"Stage 5 candidate outputs are write-once; refusing existing: {names}"
            )

        # This is intentionally the final preflight before any LM Studio or
        # LM Link observation can lead to a preference/model mutation.
        self.freeze_verifier(self.freeze_path)

        before_link = self._link_status(timeout_seconds=self.timeouts.audit_seconds)
        before_linked = self._linked_state(
            self._ps(timeout_seconds=self.timeouts.audit_seconds)
        )
        failure: BaseException | None = None
        try:
            super().run(
                candidates,
                incumbent_identifier=incumbent_identifier,
            )
        except BaseException as exc:
            failure = exc

        audit_failure: BaseException | None = None
        try:
            after_link = self._link_status(
                timeout_seconds=self.timeouts.audit_seconds
            )
            after_linked = self._linked_state(
                self._ps(timeout_seconds=self.timeouts.audit_seconds)
            )
            if after_link.raw != before_link.raw:
                raise base.LifecycleError(
                    "LM Link status was not restored state-for-state after Stage 5"
                )
            if after_linked != before_linked:
                raise base.LifecycleError(
                    "linked/Dynamo model rows changed during the Stage 5 lifecycle"
                )
        except BaseException as exc:
            audit_failure = exc

        if failure is not None and audit_failure is not None:
            raise base.LifecycleError(
                f"Stage 5 lifecycle failed ({failure}); linked-peer restoration "
                f"audit also failed ({audit_failure})"
            ) from audit_failure
        if audit_failure is not None:
            raise audit_failure
        if failure is not None:
            raise failure

    def _evaluate_command(self, candidate: Any) -> list[str]:
        return [
            self.python_executable,
            str(self.evaluator),
            "run-model",
            "--freeze",
            str(self.freeze_path),
            "--model",
            candidate.identifier,
            "--output",
            str(self.output_dir / f"{candidate.slug}.json"),
        ]

    def _verify_inference_resident(
        self,
        candidate: Any,
        fingerprint: Any,
        link_snapshot: Any,
    ) -> None:
        if fingerprint.identifier != candidate.identifier:
            raise base.LifecycleError("candidate and fingerprint identifiers differ")
        self._verify_fingerprint(
            fingerprint,
            link_snapshot,
            phase="Stage 5 inference",
        )

    def _evaluate(
        self,
        candidate: Any,
        link_snapshot: Any,
        fingerprint: Any,
        attestation: Any,
    ) -> None:
        def infer_under_verified_local_routing() -> None:
            self._verify_link_identity(
                link_snapshot,
                expected_preferred=link_snapshot.local_device_identifier,
            )
            self._verify_inference_resident(candidate, fingerprint, link_snapshot)
            before_attestation = self._attest_residency(
                fingerprint, phase="before Stage 5 inference"
            )
            if before_attestation != attestation:
                raise base.ResidencyAttestationError(
                    "candidate process or actual residency changed before Stage 5 inference",
                    fingerprint_sha256=fingerprint.sha256,
                    phase="before Stage 5 inference",
                )
            self._run(
                self._evaluate_command(candidate),
                f"run grounded synthesis benchmark for {candidate.name!r}",
                self.timeouts.evaluator_seconds,
                terminate_process_group=True,
            )
            # Detect a routing/residency change that occurred during the HTTP
            # request before the preference transaction is allowed to restore.
            self._verify_link_identity(
                link_snapshot,
                expected_preferred=link_snapshot.local_device_identifier,
            )
            self._verify_inference_resident(candidate, fingerprint, link_snapshot)
            after_attestation = self._attest_residency(
                fingerprint, phase="after Stage 5 inference"
            )
            if after_attestation != attestation:
                raise base.ResidencyAttestationError(
                    "candidate process or actual residency changed during Stage 5 inference",
                    fingerprint_sha256=fingerprint.sha256,
                    phase="after Stage 5 inference",
                )

        self._on_zbook(
            link_snapshot,
            f"ZBook-local inference for {candidate.name!r}",
            infer_under_verified_local_routing,
        )

    def dry_run_commands(
        self,
        candidates: Sequence[Any],
        *,
        incumbent_identifier: str | None = None,
    ) -> list[str]:
        """Render the inherited plan with Stage 5's local inference transaction."""

        lines = super().dry_run_commands(
            candidates,
            incumbent_identifier=incumbent_identifier,
        )
        original_command = shlex.join(
            [
                self.lms_executable,
                "link",
                "set-preferred-device",
                "ORIGINAL_PREFERRED_DEVICE_ID",
            ]
        )
        local_command = shlex.join(
            [
                self.lms_executable,
                "link",
                "set-preferred-device",
                "ZBOOK_DEVICE_ID",
            ]
        )
        old_comment = "# re-read link status and refuse inference unless preference is restored"
        rewritten: list[str] = []
        for line in lines:
            if line == old_comment:
                if rewritten and rewritten[-1] == original_command:
                    rewritten.pop()
                rewritten.extend(
                    [
                        local_command,
                        (
                            "# verify ZBOOK_DEVICE_ID remains preferred and the exact "
                            "AutoTLDR-owned local row is the only local resident"
                        ),
                    ]
                )
                continue
            rewritten.append(line)
            if line.startswith(shlex.quote(self.python_executable) + " ") and " run-model " in line:
                rewritten.extend(
                    [
                        (
                            "# verify preference and exact local resident again after "
                            "localhost inference"
                        ),
                        original_command,
                        "# verify ORIGINAL_PREFERRED_DEVICE_ID is restored",
                    ]
                )
        return [
            "# snapshot exact LM Link status and every linked/Dynamo PS row",
            *rewritten,
            "# after restoration, require exact LM Link status and linked rows unchanged",
        ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--incumbent",
        help=(
            "exact pre-existing ZBook-local identifier to snapshot, temporarily "
            "unload, and restore"
        ),
    )
    parser.add_argument("--freeze", type=Path, default=DEFAULT_FREEZE_PATH)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--report",
        type=Path,
        help="report path after all four runs (default: OUTPUT_DIR/report.json)",
    )
    parser.add_argument(
        "--lms-executable",
        default=base.DEFAULT_LMS_EXECUTABLE,
        help="ZBook-local LMS executable (or set AUTOTLDR_LMS_CLI)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print every command/check without touching LM Studio or the endpoint",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        candidates = exact_candidates()
        runner = Stage5CandidateRunner(
            freeze_path=args.freeze,
            output_dir=args.output_dir,
            lms_executable=args.lms_executable,
        )
        report_path = args.report or (args.output_dir / "report.json")
        if args.dry_run:
            lines = runner.dry_run_commands(
                candidates,
                incumbent_identifier=args.incumbent,
            )
            lines.extend(
                [
                    "# after all four owned candidates are absent and state is restored:",
                    shlex.join(
                        [
                            runner.python_executable,
                            str(EVALUATOR_PATH),
                            "score",
                            "--freeze",
                            str(args.freeze),
                            "--output-dir",
                            str(args.output_dir),
                            "--report",
                            str(report_path),
                        ]
                    ),
                ]
            )
            print("\n".join(lines))
            return 0

        runner.run(candidates, incumbent_identifier=args.incumbent)
        runner._run(
            [
                runner.python_executable,
                str(EVALUATOR_PATH),
                "score",
                "--freeze",
                str(args.freeze),
                "--output-dir",
                str(args.output_dir),
                "--report",
                str(report_path),
            ],
            "score the complete Stage 5 synthesis candidate set",
            runner.timeouts.evaluator_seconds,
            terminate_process_group=True,
        )
    except (base.LifecycleError, evaluator.EvaluationError, RuntimeError) as exc:
        print(f"Stage 5 local candidate lifecycle error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
