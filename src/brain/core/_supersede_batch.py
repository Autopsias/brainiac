"""Many supersessions, one writer lock, one reconcile.

``supersede`` ends with a sync that walks the whole vault. Measured 2026-09-10
on a 5638-note vault, that sync was ~26s of a ~26s call, so a loop of N pairs
paid N full scans. ``reindex=False`` removes the per-pair scan and hands the
index to the caller. This class is that caller: it keeps both obligations the
flag creates in code, so no loop has to re-derive them.
"""
from __future__ import annotations

import contextlib
from typing import Any

from ..lock import vault_writer_lock


class SupersedeBatch:
    """Run ``supersede``/``unsupersede`` under ONE writer lock and reconcile the
    index ONCE, on every exit — success, refusal or Ctrl-C.

    * The lock is held from the first call to :meth:`close`. That is the fact
      ``_require_caller_held_lock`` checks, and it means no other writer or
      reader sees the index between two pairs.
    * :meth:`close` rolls back a pending journal BEFORE it syncs. An interrupt
      between a pair's two signed writes leaves one, and indexing that
      half-chain would hand every reader an unfinished transaction.

    The lock is taken on the FIRST call, not on entry, so a run with nothing to
    retire takes no lock and runs no sync. For the nightly folds that is almost
    every run, so the batch must cost nothing there.

        with SupersedeBatch(core, "auto-version-chains") as batch:
            batch.supersede(old_id, new_id, reason="...")
    """

    def __init__(self, core: Any, verb: str, *, publish: bool = False) -> None:
        self._core = core
        self._verb = verb
        self._publish = publish
        self._stack = contextlib.ExitStack()
        self._held = False
        #: the reconcile's ``sync`` result; None until a call was attempted
        self.synced: dict[str, Any] | None = None

    def __enter__(self) -> SupersedeBatch:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def supersede(self, old_id: str, new_id: str, **kw: Any) -> dict[str, Any]:
        return self._call("supersede", old_id, new_id, kw)

    def unsupersede(self, old_id: str, new_id: str, **kw: Any) -> dict[str, Any]:
        return self._call("unsupersede", old_id, new_id, kw)

    def _call(self, verb: str, old_id: str, new_id: str,
              kw: dict[str, Any]) -> dict[str, Any]:
        if not self._held:
            self._stack.enter_context(
                vault_writer_lock(self._core.vault, verb=self._verb))
            self._held = True
        return getattr(self._core, verb)(old_id, new_id, reindex=False, **kw)

    def close(self) -> None:
        """Reconcile what this batch attempted, then release the lock.

        Runs after a refusal too: a call that raised may still have written
        one side. Idempotent — a second close, or a close with no call, does
        nothing."""
        if not self._held:
            return
        self._held = False
        with self._stack:  # releases the lock LAST, after the reconcile
            self._core._recover_pending_supersede()
            self.synced = self._core.sync(drain=False, publish=self._publish)
