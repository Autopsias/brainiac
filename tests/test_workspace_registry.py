"""Concurrent-writer test for tools/workspace_registry.py (SF-01 helper, s02).

Spawns several processes upserting concurrently to prove the flock +
atomic-rename write never drops or duplicates an entry.
"""
from __future__ import annotations

import multiprocessing
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import workspace_registry as wr  # noqa: E402


def _worker(args):
    vault_dir, registry_path, lock_path, target = args
    wr.upsert_entry(vault_dir, target=target, registry_path=Path(registry_path), lock_path=Path(lock_path))


def test_concurrent_upserts_same_key_collapse_to_one_entry():
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as vault_dir:
        reg = Path(td) / "workspaces.json"
        lock = Path(td) / "workspaces.lock"
        args = [(vault_dir, str(reg), str(lock), "host")] * 8
        with multiprocessing.Pool(8) as pool:
            pool.map(_worker, args)
        entries = wr.list_entries(registry_path=reg)
        assert len(entries) == 1
        assert entries[0]["vault_path"] == str(Path(vault_dir).resolve())


def test_concurrent_upserts_distinct_vaults_all_survive():
    with tempfile.TemporaryDirectory() as td:
        reg = Path(td) / "workspaces.json"
        lock = Path(td) / "workspaces.lock"
        vault_dirs = [tempfile.mkdtemp() for _ in range(5)]
        args = [(v, str(reg), str(lock), "host") for v in vault_dirs]
        with multiprocessing.Pool(5) as pool:
            pool.map(_worker, args)
        entries = wr.list_entries(registry_path=reg)
        assert len(entries) == 5


def test_upsert_is_idempotent_and_touch_refreshed_updates_timestamp():
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as vault_dir:
        reg = Path(td) / "workspaces.json"
        lock = Path(td) / "workspaces.lock"
        e1 = wr.upsert_entry(vault_dir, target="host", registry_path=reg, lock_path=lock)
        e2 = wr.touch_refreshed(vault_dir, target="host", registry_path=reg, lock_path=lock)
        assert e2 is not None
        assert e2["staged_at"] == e1["staged_at"]
        assert e2["last_refreshed"] >= e1["last_refreshed"]
        assert len(wr.list_entries(registry_path=reg)) == 1


if __name__ == "__main__":
    test_concurrent_upserts_same_key_collapse_to_one_entry()
    test_concurrent_upserts_distinct_vaults_all_survive()
    test_upsert_is_idempotent_and_touch_refreshed_updates_timestamp()
    print("OK: all workspace_registry tests passed")
