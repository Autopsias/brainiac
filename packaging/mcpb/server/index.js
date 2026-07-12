#!/usr/bin/env node
'use strict';

/**
 * Brainiac .mcpb stdio shim (SUI-03, Route B).
 *
 * Claude Desktop only bundles Node.js on macOS + Windows (no Python), and a
 * python-type .mcpb would have to vendor a full venv including compiled
 * deps (pydantic-core, pulled in by the `mcp` SDK) — not portable across
 * platforms. So this shim does the minimum: locate the HOST-installed
 * `brain-mcp` engine (same binary `pip install brainiac-cli[mcp]` / `uv tool
 * install brainiac-cli[mcp]` puts on PATH) and spawn it, piping stdio
 * through byte-for-byte via {stdio: 'inherit'} — no JSON-RPC framing lives
 * here, so there is only ONE place the MCP protocol is actually
 * implemented (src/brain/mcp_adapter.py), never a second copy to drift.
 *
 * ponytail: zero npm dependencies — child_process/fs/os/path are stdlib,
 * and a 1:1 stdio pipe is the whole job. Add a real JSON-RPC layer only if
 * the shim ever needs to inspect/rewrite messages (it doesn't today).
 */

const os = require('os');
const path = require('path');
const fs = require('fs');
const { spawn } = require('child_process');

const isWin = process.platform === 'win32';
const EXE = isWin ? '.exe' : '';
const BIN_NAME = 'brain-mcp' + EXE;
const BRAIN_BIN_NAME = 'brain' + EXE;

function findOnPath(name) {
  const pathEnv = process.env.PATH || process.env.Path || '';
  const dirs = pathEnv.split(path.delimiter).filter(Boolean);
  for (const dir of dirs) {
    const candidate = path.join(dir, name);
    if (fs.existsSync(candidate)) return candidate;
  }
  return null;
}

// Mirrors the channel list `brain doctor` already knows about
// (src/brain/doctor.py CHANNEL_*): uv tool install, pipx, pip --user, and
// the legacy dev editable checkout. Every channel installs `brain` and
// `brain-mcp` as sibling console_scripts in the SAME bin dir, so PATH +
// "sibling of brain" (below) covers the common case; this list is the
// fallback for a shell that hasn't picked up a freshly-installed PATH yet.
function candidateDirs() {
  const home = os.homedir();
  if (isWin) {
    const appData = process.env.APPDATA || path.join(home, 'AppData', 'Roaming');
    return [
      path.join(home, '.local', 'bin'),
      path.join(appData, 'uv', 'tools', 'brainiac-cli', 'Scripts'),
      path.join(home, 'pipx', 'venvs', 'brainiac-cli', 'Scripts'),
      path.join(home, '.brainiac', 'venv', 'Scripts'),
    ];
  }
  return [
    path.join(home, '.local', 'bin'),
    path.join(home, '.local', 'share', 'uv', 'tools', 'brainiac-cli', 'bin'),
    path.join(home, '.local', 'pipx', 'venvs', 'brainiac-cli', 'bin'),
    path.join(home, '.brainiac', 'venv', 'bin'),
  ];
}

function locateBrainMcp() {
  const onPath = findOnPath(BIN_NAME);
  if (onPath) return onPath;

  // Same bin dir as a PATH-resolved `brain`, whatever channel installed it.
  const brainOnPath = findOnPath(BRAIN_BIN_NAME);
  if (brainOnPath) {
    const sibling = path.join(path.dirname(brainOnPath), BIN_NAME);
    if (fs.existsSync(sibling)) return sibling;
  }

  for (const dir of candidateDirs()) {
    const candidate = path.join(dir, BIN_NAME);
    if (fs.existsSync(candidate)) return candidate;
  }
  return null;
}

const target = locateBrainMcp();
if (!target) {
  process.stderr.write(
    'Brainiac: no host-installed `brain-mcp` engine found on this machine.\n' +
    'Install the engine first: npx brainiac-install\n' +
    '(this extension only spawns the host engine\'s brain-mcp — it never bundles\n' +
    'its own copy. If you just installed it, reopen this extension.)\n'
  );
  process.exit(1);
}

const child = spawn(target, [], { stdio: 'inherit' });

child.on('error', (err) => {
  process.stderr.write(`Brainiac: failed to start ${target}: ${err.message}\n`);
  process.exit(1);
});

child.on('exit', (code, signal) => {
  process.exit(code === null ? 1 : code);
});

for (const sig of ['SIGINT', 'SIGTERM']) {
  process.on(sig, () => {
    try { child.kill(sig); } catch (e) { /* already gone */ }
  });
}
