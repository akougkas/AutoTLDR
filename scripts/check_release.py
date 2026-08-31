#!/usr/bin/env python3
"""Fail when built distributions contain accidental or missing product files."""

from __future__ import annotations

import sys
import tarfile
import zipfile
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        raise SystemExit("usage: check_release.py DIST_DIRECTORY")
    root = Path(argv[0])
    sdists = sorted(root.glob("*.tar.gz"))
    wheels = sorted(root.glob("*.whl"))
    if len(sdists) != 1 or len(wheels) != 1:
        raise SystemExit("expected exactly one sdist and one wheel")

    with tarfile.open(sdists[0]) as archive:
        sdist_names = archive.getnames()
    forbidden = ("/benchmarks/", "/tests/", "/docs/census/", "/.well-known/")
    for marker in forbidden:
        leaked = [name for name in sdist_names if marker in f"/{name}/"]
        if leaked:
            raise SystemExit(f"sdist contains forbidden {marker}: {leaked[0]}")

    with zipfile.ZipFile(wheels[0]) as archive:
        wheel_names = archive.namelist()
    required = (
        "autotldr/integrations/skills/autotldr/SKILL.md",
        "autotldr/integrations/skills/autotldr/agents/openai.yaml",
    )
    for suffix in required:
        if not any(name.endswith(suffix) for name in wheel_names):
            raise SystemExit(f"wheel is missing {suffix}")

    print(
        f"release audit passed: {len(sdist_names)} sdist entries, "
        f"{len(wheel_names)} wheel entries"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
