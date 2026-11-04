'use strict';

// Binary resolution and subprocess spawning for cli.js (split out to keep
// cli.js under the 500-line file limit; behaviour unchanged). Shipped via the
// package.json "files": ["bin/"] entry.

const { spawnSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

// ---------------------------------------------------------------------------
// binary resolution — pure fs.PATH scan, no subprocess (works in --dry-run
// without executing anything; also sidesteps depending on which/where
// being present at all).
// ---------------------------------------------------------------------------

// A regular file on PATH isn't necessarily runnable — on non-Windows, only
// accept candidates that also carry the executable bit (mirrors what
// which/where actually check), so a non-executable file named e.g. `uv`
// can't short-circuit the install chain before uv/pipx/pip are all tried.
// Windows has no POSIX x-bit; the PATHEXT extension match is the gate there.
function isExecutableFile(candidate, isWin) {
  try {
    if (!fs.statSync(candidate).isFile()) return false;
  } catch {
    return false;
  }
  if (isWin) return true;
  try {
    fs.accessSync(candidate, fs.constants.X_OK);
    return true;
  } catch {
    return false;
  }
}

function resolveBinary(name) {
  const isWin = process.platform === 'win32';
  if (path.isAbsolute(name)) {
    return isExecutableFile(name, isWin) ? name : null;
  }
  const pathEnv = process.env.PATH || process.env.Path || '';
  const dirs = pathEnv.split(path.delimiter).filter(Boolean);
  const exts = isWin ? (process.env.PATHEXT || '.COM;.EXE;.BAT;.CMD').split(';') : [''];
  for (const dir of dirs) {
    for (const ext of exts) {
      const hasExt = ext && name.toLowerCase().endsWith(ext.toLowerCase());
      const candidate = path.join(dir, hasExt ? name : name + ext);
      if (isExecutableFile(candidate, isWin)) return candidate;
    }
  }
  return null;
}

// Windows-only: a resolved binary that is a .cmd/.bat/.ps1 shim (npm.cmd,
// scoop/choco wrappers) cannot be exec'd by spawnSync with shell:false.
// Route ONLY that one call through the shell, with fully static argv (never
// user input). Real .exe binaries (uv, pipx, python, brain) stay shell:false.
function needsShellRoute(resolvedPath) {
  if (process.platform !== 'win32') return false;
  const ext = path.extname(resolvedPath).toLowerCase();
  return ext === '.cmd' || ext === '.bat' || ext === '.ps1';
}

// node's shell:true join ([file, ...args].join(' ')) does NOT quote
// anything itself before handing the string to cmd.exe — a resolved path
// under a directory with a space (e.g. a scoop/choco shim at
// "C:\Users\First Last\scoop\shims\brain.cmd") gets parsed as just
// "C:\Users\First", "is not recognized". Beyond whitespace, cmd.exe treats
// &, ^, (, ), <, > and | as metacharacters — an unquoted arg carrying one of
// these (e.g. a --target path like "C:\proj&test") gets mis-split and can
// run a second command (fix #2). Quote any token containing a space or one
// of these metachars, or an empty token, before it goes into a shell-routed
// command line.
//
// % and " are NOT in that quoted set — double-quoting can't neutralize
// either: cmd.exe still expands %VAR% inside double quotes, and a literal "
// can't be escaped/balanced through the plain [file, ...args].join(' ')
// shell:true route. Rather than silently pass a corrupted/expanded arg to
// cmd.exe, quoteForShell REJECTS (throws) on % or " — see spawnResolved,
// the only caller, which turns that into a clear CLI error + exit(2).
const CMD_METACHAR_RE = /[\s&^()<>|]/;
const CMD_UNSAFE_RE = /[%"]/;
function quoteForShell(s) {
  const bad = CMD_UNSAFE_RE.exec(s);
  if (bad) {
    throw new Error(
      `path contains a character unsupported on Windows shim invocation (${bad[0]}): ${s}. Use a path without % or ".`,
    );
  }
  return (s === '' || CMD_METACHAR_RE.test(s)) ? `"${s}"` : s;
}

// ---------------------------------------------------------------------------
// subprocess runner — argv array, shell:false unless the resolved binary is
// a Windows shim (see needsShellRoute), in which case the executable path
// and every arg are quoted first (see quoteForShell). Every spawnSync call
// in this file routes through here so the Windows quoting fix lives once.
// The shell:false path (the common case) never rejects anything — only the
// Windows shell-route branch can throw, on an unescapable %/" arg.
// ---------------------------------------------------------------------------

function spawnResolved(resolvedBin, args, spawnOpts) {
  const shellRoute = needsShellRoute(resolvedBin);
  if (!shellRoute) {
    return spawnSync(resolvedBin, args, { ...spawnOpts, shell: false });
  }
  let bin, spawnArgs;
  try {
    bin = quoteForShell(resolvedBin);
    spawnArgs = args.map(quoteForShell);
  } catch (err) {
    console.error(`Error: ${err.message}`);
    process.exit(2);
  }
  return spawnSync(bin, spawnArgs, { ...spawnOpts, shell: true });
}

function resolvePython() {
  if (process.platform === 'win32') {
    // python3 does not exist on Windows; probe order is `py -3` then `python`.
    const py = resolveBinary('py');
    if (py) return { resolvedPath: py, label: 'py', argsPrefix: ['-3'] };
    const python = resolveBinary('python');
    if (python) return { resolvedPath: python, label: 'python', argsPrefix: [] };
    return null;
  }
  const python3 = resolveBinary('python3');
  if (python3) return { resolvedPath: python3, label: 'python3', argsPrefix: [] };
  // Some systems only ship the 3.9+ interpreter as `python` (never
  // `python3`) — mirror the Windows py -> python fallback above. Still
  // gated by pythonVersionOk() before it's actually used.
  const python = resolveBinary('python');
  if (python) return { resolvedPath: python, label: 'python', argsPrefix: [] };
  return null;
}

function pythonVersionOk(py) {
  const res = spawnResolved(
    py.resolvedPath,
    [...py.argsPrefix, '-c', 'import sys; print(sys.version_info >= (3, 9))'],
    { encoding: 'utf8' },
  );
  return res.status === 0 && (res.stdout || '').trim() === 'True';
}

module.exports = {
  isExecutableFile, resolveBinary, needsShellRoute, quoteForShell,
  spawnResolved, resolvePython, pythonVersionOk,
};
