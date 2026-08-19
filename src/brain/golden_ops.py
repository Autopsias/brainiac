"""Execute the Sunday golden-probe scorer."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from . import maintenance


RunnerCall = Callable[[list[str], int], tuple[int, str, str]]


def _default_runner_call(argv: list[str], timeout: int) -> tuple[int, str, str]:
    """Run a golden-probe subprocess with timeout and launch failures captured."""
    try:
        process = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return process.returncode, process.stdout, process.stderr
    except subprocess.TimeoutExpired as exc:
        return -1, "", f"timeout after {timeout}s: {exc}"
    except OSError as exc:
        return -1, "", f"{type(exc).__name__}: {exc}"


def _probe_result(
    document: dict[str, Any], *, runner: str, degraded: bool
) -> dict[str, Any]:
    """Project a validated scorer document onto the maintenance result shape."""
    return {
        "score": document.get("score"),
        "disposition": document.get("disposition"),
        "exit_code": document.get("exit_code"),
        "runner": runner,
        "degraded": degraded,
    }


def _try_codex_probe(
    core: Any,
    probes_path: Path,
    *,
    timeout: int,
    call: RunnerCall,
) -> tuple[dict[str, Any] | None, str | None]:
    """Run and strictly validate the cross-family Codex execution leg."""
    prompt = maintenance.build_codex_golden_prompt(
        probes_path, Path(core.vault), sys.executable
    )
    argv = [
        "codex",
        "exec",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "-C",
        str(core.vault),
        "--json",
        prompt,
    ]
    return_code, stdout, stderr = call(argv, timeout)
    if return_code != 0:
        error = (stderr or stdout or "").strip()[:300]
        return None, f"codex exec exited {return_code}: {error}"
    final_text = maintenance.parse_codex_final_message(stdout)
    if final_text is None:
        return None, "no agent_message event in codex --json stream"
    try:
        document: Any = json.loads(final_text)
    except ValueError as exc:
        return None, f"final message is not JSON: {exc}"
    shape_error = maintenance.validate_golden_probe_doc(document)
    if shape_error:
        return None, f"invalid golden-probe doc: {shape_error}"
    return _probe_result(document, runner="codex", degraded=False), None


def _run_self_probe(
    core: Any,
    probes_path: Path,
    *,
    timeout: int,
    call: RunnerCall,
    brain_command: str,
    codex_error: str | None,
) -> dict[str, Any]:
    """Run the deterministic self fallback and validate its JSON document."""
    argv = [
        sys.executable,
        "-m",
        "brain.golden_probe",
        str(probes_path),
        "--vault",
        str(core.vault),
        "--brain-cmd",
        brain_command,
    ]
    return_code, stdout, stderr = call(argv, timeout)
    try:
        document: Any = json.loads(stdout)
    except ValueError:
        document = None
    shape_error = (
        maintenance.validate_golden_probe_doc(document)
        if document is not None
        else f"non-JSON self-run output (rc={return_code}): "
        f"{(stderr or stdout or '').strip()[:300]}"
    )
    if shape_error:
        return {
            "score": None,
            "disposition": "transient",
            "exit_code": maintenance.GOLDEN_EXIT_TRANSIENT,
            "runner": "self",
            "degraded": True,
            "error": f"self-run also failed: {shape_error} (codex: {codex_error})",
        }
    result = _probe_result(document, runner="self", degraded=True)
    result["codex_error"] = codex_error
    return result


class GoldenOpsMixin:
    """Provide BrainCore's cross-family golden-probe operation."""

    def _run_golden_probe(
        self,
        *,
        probes_path: Path,
        timeout_seconds: int | None = None,
        codex_call: Any = None,
        self_call: Any = None,
    ) -> dict[str, Any]:
        """Execute WD-03 through Codex with a validated self-run fallback.

        Both legs call the same deterministic scorer. Codex is an execution
        boundary, never an independent grader; malformed output is discarded.
        """
        timeout = (
            timeout_seconds
            if timeout_seconds is not None
            else maintenance.golden_codex_timeout_seconds()
        )
        call_codex: RunnerCall = codex_call or _default_runner_call
        call_self: RunnerCall = self_call or _default_runner_call
        brain_command = shlex.join([sys.executable, "-m", "brain.cli"])
        result, codex_error = _try_codex_probe(
            self,
            probes_path,
            timeout=timeout,
            call=call_codex,
        )
        if result is not None:
            return result
        return _run_self_probe(
            self,
            probes_path,
            timeout=timeout,
            call=call_self,
            brain_command=brain_command,
            codex_error=codex_error,
        )
