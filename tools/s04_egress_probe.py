"""Build and query the synthetic S04 classification-egress fixture."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
QUERY = "synthetic egress boundary"


def _environment(root: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "BRAIN_INDEX_DIR": str(root / "index"),
            "BRAIN_EMBEDDER": "hash",
            "BRAIN_QUERY_CAPTURE_ENABLED": "0",
            "PYTHONPATH": str(REPO_ROOT / "src"),
        }
    )
    return env


def _brain(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "brain", "--vault", str(root / "vault"), *args],
        cwd=REPO_ROOT,
        env=_environment(root),
        capture_output=True,
        text=True,
        check=False,
    )


def _note(note_id: str, classification: str, number: int) -> str:
    return f"""---
id: {note_id}
title: "Synthetic egress boundary {classification.lower()} {number}"
type: note
classification: {classification}
created: 2026-08-15
updated: 2026-08-15
tags: []
---

# Synthetic egress boundary {number}

This authored fixture note repeats the synthetic egress boundary query and
contains no real or user-derived content. It exists only to exercise the
classification filter in Session S04.
"""


def setup(root_arg: Path | None) -> dict[str, Any]:
    root = root_arg or Path(tempfile.mkdtemp(prefix="s04-egress-fixture."))
    (root / "index").mkdir(parents=True, exist_ok=True)
    init = _brain(
        root,
        "init",
        "--full",
        "--no-register-tasks",
        "--no-seed-vault",
        "--json",
    )
    if init.returncode != 0:
        raise RuntimeError(f"brain init failed: {init.stderr or init.stdout}")
    notes_dir = root / "vault" / "brain" / "resources"
    notes_dir.mkdir(parents=True, exist_ok=True)
    public_ids = [f"s04-public-{number}" for number in range(1, 6)]
    restricted_ids = [f"s04-restricted-{number}" for number in range(1, 6)]
    for number, note_id in enumerate(public_ids, start=1):
        (notes_dir / f"{note_id}.md").write_text(
            _note(note_id, "Public", number), encoding="utf-8"
        )
    for number, note_id in enumerate(restricted_ids, start=1):
        (notes_dir / f"{note_id}.md").write_text(
            _note(note_id, "Restricted", number), encoding="utf-8"
        )
    rebuilt = _brain(root, "rebuild", "--json")
    if rebuilt.returncode != 0:
        raise RuntimeError(f"brain rebuild failed: {rebuilt.stderr or rebuilt.stdout}")
    metadata = {
        "fixture_root": str(root),
        "vault": str(root / "vault"),
        "index_dir": str(root / "index"),
        "python": sys.executable,
        "init_command": [
            sys.executable,
            "-m",
            "brain",
            "--vault",
            str(root / "vault"),
            "init",
            "--full",
            "--no-register-tasks",
            "--no-seed-vault",
            "--json",
        ],
        "init_returncode": init.returncode,
        "note_count": len(public_ids) + len(restricted_ids),
        "public_ids": public_ids,
        "restricted_ids": restricted_ids,
        "query": QUERY,
        "rebuild": json.loads(rebuilt.stdout),
    }
    (root / "fixture.json").write_text(
        json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
    )
    return metadata


def query(root: Path) -> dict[str, Any]:
    result = _brain(
        root,
        "search",
        QUERY,
        "--no-rerank",
        "--json",
        "-k",
        "20",
        "--max-tier",
        "Internal",
    )
    if result.returncode != 0:
        raise RuntimeError(f"brain search failed: {result.stderr or result.stdout}")
    return json.loads(result.stdout)


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    setup_parser = subparsers.add_parser("setup")
    setup_parser.add_argument("--root", type=Path)
    query_parser = subparsers.add_parser("query")
    query_parser.add_argument("root", type=Path)
    args = parser.parse_args()
    payload = setup(args.root) if args.command == "setup" else query(args.root)
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
