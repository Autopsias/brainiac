#!/usr/bin/env python3
"""Shared workspace registry helper (SF-01 §2) — flock-guarded read-modify-write
over ``~/.brainiac/workspaces.json``.

This is the ONE place any lifecycle skill/script touches the registry. s02
(`/brainiac-install`) writes the host entry through it; s03 (`/brainiac-update`)
and s04 (`/brainiac-cowork-setup`) must import this module rather than
reinvent locking or the schema.

Schema (design note: docs/install/plugin-distribution.md §2):
    {
      "version": 1,
      "entries": [
        {
          "vault_path": "<realpath>",
          "workspace_path": "<realpath>",
          "target": "host" | "cowork-vm",
          "host": "<hostname>",
          "arch": "<platform.machine()>",
          "model_dir": "...",
          "staged_at": "<iso8601>",
          "last_refreshed": "<iso8601>"
        }
      ]
    }

Upsert key = (host, arch, target, realpath(vault_path), realpath(workspace_path)).
Concurrency: exclusive flock on workspaces.lock around the whole
read-modify-write, then write to a .tmp file + atomic rename.
"""
from __future__ import annotations

import json
import os
import platform
import socket
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

REGISTRY_VERSION = 1

BRAINIAC_HOME = Path(os.environ.get("BRAINIAC_HOME", Path.home() / ".brainiac"))
REGISTRY_PATH = BRAINIAC_HOME / "workspaces.json"
LOCK_PATH = BRAINIAC_HOME / "workspaces.lock"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _locked(lock_path: Path) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
    try:
        if sys.platform == "win32":
            # ponytail: no fcntl on Windows; msvcrt.locking is the stdlib
            # equivalent. Retry loop with a short sleep — good enough for a
            # handful of lifecycle-skill invocations, not a high-throughput lock.
            import msvcrt

            while True:
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.1)
            try:
                yield
            finally:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _load(path: Path) -> dict:
    if not path.exists():
        return {"version": REGISTRY_VERSION, "entries": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # Corrupt registry: don't crash the install/update flow over it —
        # start fresh rather than silently losing the lock forever.
        return {"version": REGISTRY_VERSION, "entries": []}
    data.setdefault("version", REGISTRY_VERSION)
    data.setdefault("entries", [])
    migrated = False
    for entry in data["entries"]:
        if "host" not in entry or "arch" not in entry:
            entry["host"] = entry.get("host", socket.gethostname())
            entry["arch"] = entry.get("arch", platform.machine())
            migrated = True
        if "target" not in entry:
            entry["target"] = (
                "host"
                if entry.get("workspace_path") == entry.get("vault_path")
                else "cowork-vm"
            )
            migrated = True
    if migrated:
        data["version"] = REGISTRY_VERSION
    return data


def _write_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _key(entry: dict) -> tuple:
    return (
        entry.get("host"),
        entry.get("arch"),
        entry.get("target"),
        os.path.realpath(entry.get("vault_path", "")),
        os.path.realpath(entry.get("workspace_path", "")),
    )


def upsert_entry(
    vault_path: str,
    workspace_path: Optional[str] = None,
    target: str = "host",
    model_dir: Optional[str] = None,
    registry_path: Path = REGISTRY_PATH,
    lock_path: Path = LOCK_PATH,
) -> dict:
    """Insert or update one registry entry. Returns the written entry.

    ``workspace_path`` defaults to ``vault_path`` (the host case: workspace
    IS the vault). Realpath-normalizes both paths before matching the upsert
    key, so a relative/symlinked path passed twice still lands on one entry.
    """
    vault_real = os.path.realpath(vault_path)
    workspace_real = os.path.realpath(workspace_path or vault_path)
    host = socket.gethostname()
    arch = platform.machine()
    now = _now_iso()

    with _locked(lock_path):
        data = _load(registry_path)
        new_entry = {
            "vault_path": vault_real,
            "workspace_path": workspace_real,
            "target": target,
            "host": host,
            "arch": arch,
            "model_dir": model_dir or "",
            "staged_at": now,
            "last_refreshed": now,
        }
        want_key = _key(new_entry)
        for entry in data["entries"]:
            if _key(entry) == want_key:
                new_entry["staged_at"] = entry.get("staged_at", now)  # preserve original stage time
                entry.update(new_entry)
                _write_atomic(registry_path, data)
                return entry
        data["entries"].append(new_entry)
        _write_atomic(registry_path, data)
        return new_entry


def list_entries(registry_path: Path = REGISTRY_PATH, target: Optional[str] = None) -> list[dict]:
    data = _load(registry_path)
    entries = data["entries"]
    if target is not None:
        entries = [e for e in entries if e.get("target") == target]
    return entries


def touch_refreshed(
    vault_path: str,
    workspace_path: Optional[str] = None,
    target: str = "host",
    registry_path: Path = REGISTRY_PATH,
    lock_path: Path = LOCK_PATH,
) -> Optional[dict]:
    """Stamp ``last_refreshed`` on an existing entry (used by /brainiac-update)."""
    vault_real = os.path.realpath(vault_path)
    workspace_real = os.path.realpath(workspace_path or vault_path)
    want_key = (socket.gethostname(), platform.machine(), target, vault_real, workspace_real)
    with _locked(lock_path):
        data = _load(registry_path)
        for entry in data["entries"]:
            if _key(entry) == want_key:
                entry["last_refreshed"] = _now_iso()
                _write_atomic(registry_path, data)
                return entry
    return None


def _demo() -> None:
    """ponytail self-check: single-writer upsert-is-idempotent smoke test."""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        reg = Path(td) / "workspaces.json"
        lock = Path(td) / "workspaces.lock"
        with tempfile.TemporaryDirectory() as vault_dir:
            e1 = upsert_entry(vault_dir, target="host", registry_path=reg, lock_path=lock)
            assert e1["target"] == "host"
            e2 = upsert_entry(vault_dir, target="host", registry_path=reg, lock_path=lock)
            entries = list_entries(registry_path=reg)
            assert len(entries) == 1, f"expected 1 entry after re-upsert, got {len(entries)}"
            assert e2["vault_path"] == os.path.realpath(vault_dir)
    print("OK: workspace_registry self-check passed")


if __name__ == "__main__":
    _demo()
