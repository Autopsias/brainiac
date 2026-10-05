"""Invocation-scoped host executor for one fixed golden measurement.

Not a vault retrieval API: the sole tool returns scorer metadata, never note
bodies. The native host CLI retains classification gating and SEC-06 records.
No model-supplied command, vault, probe path or arguments are accepted.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shlex
import subprocess
import sys

from .maintenance import validate_golden_probe_doc


def measure(vault: Path, probes: Path, timeout: int) -> dict:
    """Run the fixed native scorer outside the model's filesystem sandbox."""
    command = [sys.executable, '-m', 'brain.golden_probe', str(probes),
               '--vault', str(vault), '--brain-cmd',
               shlex.join([sys.executable, '-m', 'brain.cli'])]
    result = subprocess.run(command, capture_output=True, text=True,
                            timeout=timeout, stdin=subprocess.DEVNULL)
    document = json.loads(result.stdout)
    error = validate_golden_probe_doc(document)
    if error or result.returncode != document.get('exit_code'):
        raise ValueError(error or 'Scorer process/document exit-code mismatch')
    return document


class Executor:
    """Minimal bounded stdio protocol; one fixed empty-argument tool."""

    def __init__(self, vault: Path, probes: Path, timeout: int, result_file: Path | None = None):
        self.vault, self.probes, self.timeout = vault, probes, timeout
        self.cached = None
        self.result_file = result_file

    def dispatch(self, method: str, params: dict) -> dict:
        if method == 'initialize':
            return {'protocolVersion': params.get('protocolVersion', '2025-06-18'),
                    'capabilities': {'tools': {}},
                    'serverInfo': {'name': 'brainiac-golden-executor', 'version': '1'}}
        if method == 'ping':
            return {}
        if method == 'tools/list':
            return {'tools': [{'name': 'run_golden_probe',
                'description': 'Run the fixed audited host golden scorer once; return its JSON measurement.',
                'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
                'annotations': {'readOnlyHint': True, 'destructiveHint': False,
                                'idempotentHint': True, 'openWorldHint': False}}]}
        if method != 'tools/call':
            raise ValueError('Unknown protocol method')
        if params.get('name') != 'run_golden_probe' or params.get('arguments', {}) != {}:
            raise ValueError('Only run_golden_probe with empty arguments is allowed')
        if self.cached is None:
            self.cached = measure(self.vault, self.probes, self.timeout)
            if self.result_file is not None:
                # Private parent-created artifact; no path comes from the tool.
                self.result_file.write_text(json.dumps(self.cached), encoding='utf-8')
        return {'content': [{'type': 'text', 'text': json.dumps(self.cached)}],
                'structuredContent': self.cached, 'isError': False}


def serve(executor: Executor, incoming, outgoing) -> None:
    """Answer requests only; stdout is exclusively JSON-RPC protocol data."""
    while True:
        line = incoming.readline(131073)
        if not line:
            return
        message = json.loads(line) if len(line) <= 131072 else {}
        if not isinstance(message, dict) or 'id' not in message:
            continue
        response = {'jsonrpc': '2.0', 'id': message['id']}
        try:
            response['result'] = executor.dispatch(message.get('method'), message.get('params') or {})
        except Exception as exc:
            response['error'] = {'code': -32602, 'message': f'{type(exc).__name__}: {exc}'}
        outgoing.write(json.dumps(response) + '\n')
        outgoing.flush()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vault', type=Path, required=True)
    parser.add_argument('--probes', type=Path, required=True)
    parser.add_argument('--timeout', type=int, default=600)
    parser.add_argument('--result-file', type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.timeout <= 600:
        parser.error('timeout must be between 1 and 600 seconds')
    serve(Executor(args.vault.resolve(), args.probes.resolve(), args.timeout,
                   args.result_file.resolve()), sys.stdin, sys.stdout)


if __name__ == '__main__':
    main()
