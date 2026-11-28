"""Pin the vault a drain claimed, and stop acting on it once it changed (F37).

The drain checks the vault path before the claim rename and once more right
after it. The VM owns everything under ``<workspace>`` and can swap
``<workspace>/vault`` for a symlink at ANY later moment, while a child runs for
up to an hour. Every step below those checks acts on the same path string at its
own syscall time, so they authorized nothing but their own instants.

What this module and its callers do:

* ``pin_claim`` records the vault's identity (device + inode of a real
  directory, every parent resolved) at claim time and returns ``still_claimed``.
  It compares at the instants it is called; the first failed comparison latches,
  so a path swapped away and back stays refused.
* In claim mode ``wire_vault`` does not resolve the ``vault`` and ``workspace``
  the drain gives it again (it only NFC-normalises them) and passes ``model_dir``
  on untouched. A wire does not start once ``still_claimed`` is false.
  ``guarded`` does not start an engine or staging call after that, and discards
  the answer of one that returned after it.
* ``watched_run`` stands in for ``subprocess.run`` for the drain's own children.
  Each child leads its own process group, and while it runs the claim is checked
  every ``poll`` seconds. Once the loss is seen the group gets SIGTERM, then
  SIGKILL ``grace`` seconds later, and a failed result comes back for the
  callers above to discard.
* Once the pin sees the loss the drain starts no further wire, engine call or
  staging call (see the model copy below), writes no result file, and ends
  ``skipped-symlink-escape``.
* ``snapshot_strayed`` is a second check, made once before wire 1: when the engine
  default snapshot directory, derived from the vault, resolves outside the claimed
  vault now but did not when ``pin_claim`` looked, no wire runs and all six report
  ``CLAIM_LOST``. It cannot tell a swap from a link planted after the claim, so
  both get that text. If the pin has also seen a loss by then the drain ends
  ``skipped-symlink-escape``; ``init-failed`` is what it ends with when the check
  fires but the pin has seen no loss: a swap undone before the pin looked, or a
  link planted after the claim was pinned.

NOT covered (closing these takes fd-relative operation, which a flow built on
subprocesses cannot do):

* a swap undone before the next poll or check is never seen;
* between a swap and the SIGKILL, a running child keeps writing through the new
  target for up to one poll interval plus the grace; a child that leaves its
  process group (its own ``setsid``) is not killed, and on a host without
  process groups (Windows) only the child itself is signalled;
* an injected ``runner`` (a test fake) is used as given and is not watched;
* each watched child leads its own process group (``start_new_session``), so it
  is no longer in the drain's own group, and launchd's stop kills only the job's
  process group, which the child has left. That is why ``watched_run`` takes
  SIGTERM, the signal launchd sends first, while a child runs: it SIGKILLs the
  child's group and then lets the drain die as it would have (the previous
  disposition is restored and the signal sent again). A SIGKILL of the drain
  (it cannot be caught), a SIGHUP or SIGQUIT (not handled), or a crash still
  leaves the child running. The drain
  enforces the child's timeout, so with the drain gone the child has none: it
  runs until it exits by itself, and an engine call such as ``sync`` keeps
  holding the writer lock meanwhile;
* callees resolve the path themselves, and a swap landing inside one of them is
  followed: ``workspaces.upsert_entry`` resolves the vault and workspace again
  when it writes a registry row, ``register_mcp`` resolves with ``same_path`` when
  an entry of that name already exists and again when it reads the entry back, the
  nightly label and plist name derive from the resolved vault path, and the last
  check before the result file is a check followed by a write by path;
* the launchd plist rendered by ``init --full --apply`` (its ``BRAIN_VAULT``)
  and the Desktop MCP entry keep the plain ``<workspace>/vault`` string, which
  whatever starts from them resolves again at each launch: a pin taken at drain
  time cannot fix that;
* the fallback model copy (``_stage_model``, a ``copytree`` that can take
  seconds) has the check before it only: a swap during the copy can leave some
  model files in ``<target>/.brain/model``;
* ``_find_model_source`` takes ``<vault>/.brain/model``, which the VM can
  plant, as its first model source, so the files the host copies and stages can
  be the VM's own;
* ``model_dir`` is passed on as ``_find_model_source`` returned it, not resolved
  first as ``wire_vault`` does outside claim mode: a model source that is a
  symlink reaches the stager with its symlink spelling;
* a link at ``<vault>/.brain/snapshot`` that leaves the vault and was planted
  BEFORE the claim is followed, and the snapshot is published into the directory
  it names, exactly as before this change: ``pin_claim`` records that layout, and
  only a snapshot directory that was inside at the pin is refused when it leaves.
"""
from __future__ import annotations

import math
import os
import signal
import stat
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Optional

from .pathkey import canonical, nfc

CLAIM_LOST = ("the vault path changed after the drain claimed it, or its snapshot "
              "directory resolved inside it when the claim was pinned and outside it "
              "since (symlink swap, replaced directory or a link planted after the "
              "claim): refused")
# One refusal that every engine/stager seam reads: `_wire_init` and the sync
# fallback test `ok`; the staging wire tests `status` and shows `reason` when it
# has a `model_dir` ("no model source" otherwise).
REFUSED: dict[str, Any] = {"ok": False, "status": "failed", "reason": CLAIM_LOST}
_KILL = getattr(signal, "SIGKILL", signal.SIGTERM)  # no SIGKILL on Windows


def refuse(res: dict[str, Any]) -> dict[str, Any]:
    """End a provisioning through the drain's existing symlink-escape result."""
    res.update({"status": "skipped-symlink-escape", "ok": False})
    return res


_LEFT = "snapshot_left_at_pin"  # attribute of ``still_claimed``, set by ``pin_claim``


def _leaves(vault: Path, snapshot_dir: Any) -> bool:
    """Does ``snapshot_dir`` sit outside ``vault``? The one test the pin and
    ``snapshot_strayed`` share, so a static layout cannot read differently."""
    return not Path(snapshot_dir).is_relative_to(Path(nfc(vault)))


def snapshot_strayed(vault: Path, snapshot_dir: str, stored: Optional[str],
                     still_claimed: Optional[Callable[[], bool]]) -> bool:
    """Did a snapshot dir DERIVED from the vault leave the claimed one since the pin?

    ``stored`` is None only when nothing named the dir and no registry row held
    one, i.e. the engine default ``<vault>/.brain/snapshot`` was computed and
    resolved again from the vault path. The answer reaches the init child's env
    and the staging (the registry rows get None), so a default that resolves
    outside the claimed vault now but did not when ``pin_claim`` looked (the
    fact is on ``still_claimed``; any other callable counts as "was inside") is a
    swap or a link planted after the claim. A default that had already left the
    vault at the pin is the vault's own layout, followed as before. It detects
    only that, not at all when ``BRAIN_RUNTIME_DIR`` is set (the default is then
    ``$BRAIN_RUNTIME_DIR/snapshot``) and not without a pin (``still_claimed``
    None); the other re-resolutions are in the module docstring.
    """
    return (still_claimed is not None and stored is None
            and not os.environ.get("BRAIN_RUNTIME_DIR")
            and _leaves(vault, snapshot_dir)
            and not getattr(still_claimed, _LEFT, False))


def pin_claim(real_vault: Path) -> Callable[[], bool]:
    """``still_claimed()`` for ``real_vault`` (already fully resolved).

    It also carries, as an attribute, one fact taken once here: whether the engine
    default snapshot dir already resolves outside the vault (``snapshot_strayed``).
    """
    from . import config

    def _identity() -> Optional[tuple[int, int]]:
        try:
            st = os.lstat(real_vault)
            brain = os.lstat(real_vault / ".brain")
            if not (stat.S_ISDIR(st.st_mode) and stat.S_ISDIR(brain.st_mode)
                    and Path(os.path.realpath(real_vault)) == real_vault):
                return None
        except OSError:
            return None
        return (st.st_dev, st.st_ino)

    pinned = _identity()
    lost = False

    def still_claimed() -> bool:
        nonlocal lost
        lost = lost or pinned is None or _identity() != pinned
        return not lost

    try:  # a path that will not resolve leaves the fact unset: the strict check
        left = _leaves(real_vault, canonical(config.snapshot_dir(real_vault)))
        setattr(still_claimed, _LEFT, left)
    except (OSError, RuntimeError):
        pass
    return still_claimed


def guarded(fn: Callable[..., Any], still_claimed: Callable[[], bool]
            ) -> Callable[..., Any]:
    """``fn`` that does not start, and whose answer is discarded, once the
    claim is lost: a child that ran against a swapped path must not be recorded."""
    def run(*args: Any, **kw: Any) -> Any:
        if not still_claimed():
            return dict(REFUSED)
        out = fn(*args, **kw)
        return out if still_claimed() else dict(REFUSED)
    return run


def _signal_group(proc: "subprocess.Popen[Any]", sig: int) -> None:
    """``sig`` to the child's whole process group (``start_new_session`` made it
    the leader); where that is not possible, SIGKILL to the child alone. Never
    after the child was reaped: its pid, hence the group id, may belong to
    someone else by then."""
    if proc.returncode is not None:
        return
    killpg = getattr(os, "killpg", None)
    try:
        if killpg is not None:
            killpg(proc.pid, sig)
            return
    except OSError:
        pass
    proc.kill()


def _stop(proc: "subprocess.Popen[Any]", grace: float) -> None:
    """SIGTERM the group, wait ``grace`` seconds, SIGKILL what is left, reap.

    Nothing reaps the child before the SIGKILL has gone out, so the group id
    stays ours until then. ``grace`` 0 skips the SIGTERM and the wait."""
    if grace:
        _signal_group(proc, signal.SIGTERM)
        time.sleep(grace)
    _signal_group(proc, _KILL)
    proc.wait()


def _group_dies_with_drain(proc: "subprocess.Popen[Any]") -> Callable[[], Any]:
    """While ``proc`` runs, a SIGTERM to the drain SIGKILLs its group first, then
    lets the drain end as it would have (previous disposition back, signal sent again).
    The handler takes no lock and does not wait: it may interrupt ``Popen``'s own.
    Returns the undo; installs nothing off the main thread."""
    try:
        before = signal.getsignal(signal.SIGTERM) or signal.SIG_DFL  # None: set from C

        def on_term(signum: int, _frame: Any) -> None:
            _signal_group(proc, _KILL)
            signal.signal(signum, before)
            os.kill(os.getpid(), signum)

        signal.signal(signal.SIGTERM, on_term)
    except (ValueError, OSError):  # not the main thread, or no handler support
        return lambda: None
    return lambda: signal.signal(signal.SIGTERM, before)


def watched_run(still_claimed: Callable[[], bool], *, poll: float = 0.5,
                grace: float = 1.0) -> Callable[..., "subprocess.CompletedProcess[Any]"]:
    """A ``subprocess.run`` for the drain's children that ends one whose claim
    on the vault was lost.

    Same call shape for what the drain uses (``cmd, capture_output, text,
    timeout, env, cwd``; no ``input`` or ``check``) and the same results: a
    ``CompletedProcess``, ``TimeoutExpired`` (child killed) when ``timeout``
    runs out, the exception re-raised (child killed) on anything else. The child
    leads its own session, so a signal reaches it and what it spawned and not
    the drain. When the claim is lost the group is stopped (see ``_stop``) and a
    failed ``CompletedProcess`` comes back, for the caller to discard; once it is
    lost this runner starts no further child.
    """
    def _lost(cmd: Any) -> "subprocess.CompletedProcess[Any]":
        return subprocess.CompletedProcess(cmd, 1, None, None)

    def run(cmd: Any, *, capture_output: bool = False,
            timeout: Optional[float] = None, **popen_kw: Any
            ) -> "subprocess.CompletedProcess[Any]":
        if not still_claimed():
            return _lost(cmd)
        if capture_output:
            popen_kw.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        limit = math.inf if timeout is None else timeout  # no timeout: never reached
        deadline = time.monotonic() + limit
        with subprocess.Popen(cmd, start_new_session=True, **popen_kw) as proc:
            undo = None
            try:
                undo = _group_dies_with_drain(proc)
                while True:
                    try:
                        out, err = proc.communicate(
                            timeout=max(0.0, min(poll, deadline - time.monotonic())))
                        return subprocess.CompletedProcess(cmd, proc.returncode, out, err)
                    except subprocess.TimeoutExpired:
                        pass
                    if not still_claimed():
                        _stop(proc, grace)
                        return _lost(cmd)
                    if time.monotonic() >= deadline:
                        _stop(proc, 0)
                        raise subprocess.TimeoutExpired(cmd, limit)
            except BaseException:
                _stop(proc, 0)
                raise
            finally:
                if undo:
                    undo()

    return run
