"""Execute the Sunday golden-probe scorer."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
import tempfile
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


def _codex_probe_command(vault: Path, probes_path: Path, timeout: int, result_file: Path) -> list[str]:
    """Expose one fixed host measurement, not a general retrieval/shell API."""
    prompt = ('Call the run_golden_probe tool exactly once with empty arguments. '
              'Return ONLY its JSON measurement verbatim. Do not grade it, '
              'interpret it, run commands, or call other tools.')
    executor_args = ['-m', 'brain.golden_executor', '--vault', str(vault),
                     '--probes', str(probes_path), '--timeout', str(timeout),
                     '--result-file', str(result_file)]
    argv = ['codex', 'exec', '--ignore-user-config', '--ignore-rules', '--ephemeral',
            '--skip-git-repo-check', '--sandbox', 'read-only', '-C', str(result_file.parent), '--json']
    settings = {'approval_policy': 'never', 'mcp_servers': {}, 'web_search': 'disabled',
                'mcp_servers.brainiac_golden.command': sys.executable,
                'mcp_servers.brainiac_golden.args': executor_args,
                'mcp_servers.brainiac_golden.required': True,
                'mcp_servers.brainiac_golden.tool_timeout_sec': timeout}
    for key, value in settings.items():
        argv.extend(['-c', f'{key}={json.dumps(value)}'])
    for feature in ('plugins', 'apps', 'hooks', 'shell_tool', 'unified_exec', 'code_mode'):
        argv.extend(['--disable', feature])
    return argv + [prompt]


def _try_codex_probe(
    core: Any, probes_path: Path, *, timeout: int, call: RunnerCall,
) -> tuple[dict[str, Any] | None, str | None]:
    """Require the model response to equal the trusted executor measurement."""
    with tempfile.TemporaryDirectory(prefix='brain-golden-') as folder:
        result_file = Path(folder) / 'measurement.json'
        argv = _codex_probe_command(Path(core.vault), probes_path, timeout, result_file)
        return_code, stdout, stderr = call(argv, timeout)
        try:
            measured = json.loads(result_file.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            measured = None
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
    if document.get("disposition") == "transient":
        return None, "Codex scorer reported transient retrieval; retry on host"
    if measured is None or measured != document:
        return None, "Codex response did not match the trusted host measurement"
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
