#!/usr/bin/env node
'use strict';

/*
 * brainiac-install — thin npx bootstrap for the Brainiac `brain` CLI engine.
 *
 * What it does: detects an existing `brain` install, otherwise installs the
 * `brainiac-cli[mcp]` distribution from PyPI via uv -> pipx -> pip --user
 * (first available wins), verifies it, offers to initialize a vault
 * (`brain init --full --apply`), and optionally wires one client
 * (`brain connect --client <name>`).
 *
 * What it never does: no telemetry, no piping a remote script into a shell,
 * no runtime dependencies. Every subprocess is spawned with an explicit argv
 * array (spawnSync, shell:false) against a binary this script resolved to an
 * absolute path itself — see resolveBinary()/needsShellRoute() below for the
 * one narrow exception (Windows .cmd/.ps1 shims can't be exec'd without a
 * shell; when that happens only THAT one call is shell-routed, with fully
 * static args).
 */

const { spawnSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const PKG_SPEC = "brainiac-cli[mcp]";
// Mirrors src/brain/connect.py CLIENTS. Kept as a local, no-subprocess copy
// on purpose: --dry-run must validate --client without ever invoking `brain`.
// If connect.py grows a 5th client, add it here too (or drop this check and
// let `brain connect` be the sole validator — but that breaks dry-run
// validation, so the duplication is the deliberate tradeoff).
const CLIENTS = ['claude-code', 'claude-desktop', 'codex', 'gemini'];

// ---------------------------------------------------------------------------
// argv parsing
// ---------------------------------------------------------------------------

// A missing token (undefined — off the end of argv) and a present-but-empty
// token (`--client ''` / `--client=`) both fail the same way: exit 2 with an
// actionable message. One helper, four call sites (space-form + `=`-form for
// each of --client/--vault) instead of the guard copy-pasted four times.
function requireValue(flagName, rawValue) {
  if (rawValue === undefined || rawValue === '') {
    console.error(`--${flagName} requires a value.\nRun with --help for usage.`);
    process.exit(2);
  }
  return rawValue;
}

function parseArgs(argv) {
  const opts = { dryRun: false, client: null, vault: null, noVault: false, help: false };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--dry-run') opts.dryRun = true;
    else if (a === '--client') {
      requireValue('client', argv[i + 1]);
      opts.client = argv[++i];
    }
    else if (a.startsWith('--client=')) {
      const v = a.slice('--client='.length);
      requireValue('client', v);
      opts.client = v;
    }
    else if (a === '--vault') {
      requireValue('vault', argv[i + 1]);
      opts.vault = argv[++i];
    }
    else if (a.startsWith('--vault=')) {
      const v = a.slice('--vault='.length);
      requireValue('vault', v);
      opts.vault = v;
    }
    else if (a === '--no-vault') opts.noVault = true;
    else if (a === '--help' || a === '-h') opts.help = true;
    else {
      console.error(`Unknown argument: ${a}\nRun with --help for usage.`);
      process.exit(2);
    }
  }
  if (opts.client && !CLIENTS.includes(opts.client)) {
    console.error(`--client must be one of: ${CLIENTS.join(', ')}`);
    process.exit(2);
  }
  return opts;
}

function printHelp() {
  console.log(`brainiac-install — install the Brainiac 'brain' CLI engine and set up a vault.

Usage: npx brainiac-install [options]

Options:
  --dry-run           print the command plan; execute nothing (no network, no writes)
  --vault <path>       workspace directory; vault is created at <path>/vault
  --no-vault           skip the vault-init step (prints the command to run yourself)
  --client <name>       wire one client after install: ${CLIENTS.join(', ')}
  -h, --help           show this help

This tool never pipes a remote script into a shell. The engine ships on PyPI
as 'brainiac-cli' (Python >=3.9 required); see README.md for the full
install chain (uv -> pipx -> pip --user) and what happens with none present.`);
}

// ---------------------------------------------------------------------------
// workspace path resolution — the SINGLE point that feeds BRAIN_VAULT,
// `connect --target`, and the printed "try it:" line. path.resolve() alone
// does not expand a leading `~`, so `~/my-brain` from the interactive prompt
// (or --vault ~/my-brain) would otherwise become `<cwd>/~/my-brain` (fix #2).
// ---------------------------------------------------------------------------

// Expand a bare `~` or a `~/...` (`~\...` on Windows) prefix to the home
// directory. Deliberately does NOT expand `~user` — no such lookup here.
function expandHome(p) {
  if (p === '~') return os.homedir();
  if (p.startsWith('~/') || p.startsWith('~\\')) return path.join(os.homedir(), p.slice(2));
  return p;
}

function resolveWorkspace(raw) {
  return path.resolve(expandHome(raw));
}

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

function runCommand(resolvedBin, args, { dryRun, env } = {}) {
  const display = [resolvedBin, ...args].join(' ');
  if (dryRun) {
    console.log(`  [dry-run] would run: ${display}`);
    return { ok: true, dryRun: true };
  }
  console.log(`  $ ${display}`);
  const res = spawnResolved(resolvedBin, args, {
    stdio: 'inherit',
    env: env ? { ...process.env, ...env } : process.env,
  });
  if (res.error) {
    console.error(`  error: ${res.error.message}`);
    return { ok: false, error: res.error };
  }
  return { ok: res.status === 0, status: res.status };
}

function printNoPythonInstructions() {
  console.log('No uv, pipx, or Python >=3.9 found on PATH.');
  console.log('Install one of these yourself, then re-run this command:');
  console.log('');
  if (process.platform === 'win32') {
    console.log('  uv (standalone):  powershell -ExecutionPolicy Bypass -c "irm https://astral.sh/uv/install.ps1 | iex"');
  } else {
    console.log('  uv (standalone):  curl -LsSf https://astral.sh/uv/install.sh | sh');
  }
  console.log('  Python:           https://www.python.org/downloads/');
}

function printPathHint() {
  const home = os.homedir();
  console.log('brain installed but not found on PATH in this shell.');
  console.log('Common shim locations (add to PATH, then open a new shell):');
  console.log(`  uv tool:    ${path.join(home, '.local', 'bin')}`);
  console.log(`  pipx:       ${path.join(home, '.local', 'bin')}`);
  if (process.platform === 'win32') {
    console.log(`  pip --user: ${path.join(home, 'AppData', 'Roaming', 'Python', 'Scripts')}`);
  } else if (process.platform === 'darwin') {
    // macOS framework/python.org Python installs --user console scripts
    // under ~/Library/Python/<X.Y>/bin, NOT ~/.local/bin (fix #5). The exact
    // <X.Y> depends on which interpreter ran pip, so list the pattern
    // alongside the Linux-style path rather than resolving one version.
    console.log(`  pip --user: ${path.join(home, '.local', 'bin')}`);
    console.log(`              ${path.join(home, 'Library', 'Python', '<X.Y>', 'bin')} (framework/python.org Python)`);
  } else {
    console.log(`  pip --user: ${path.join(home, '.local', 'bin')}`);
  }
}

// Pure — computes the Step 4 client-connect plan (or the reason to skip it)
// from state Step 3 already resolved. Side-effect-free (no console/spawn) so
// test/cli.test.js can exercise the --target/BRAIN_VAULT gating logic
// directly without spawning a real `brain` process.
function buildConnectPlan(o, state) {
  if (!o.client) return { skip: 'no-client' };
  if (state.stepFailed) return { skip: 'init-failed' };
  const isDesktop = o.client === 'claude-desktop';
  if (isDesktop && !state.vaultDir) return { skip: 'desktop-no-vault' };
  const target = state.workspaceRoot || process.cwd();
  // Marked-block clients (codex/claude-code/gemini) don't need a vault to
  // exist, but Step 4 still spawns `brain connect` — with no BRAIN_VAULT in
  // env, the engine's config.vault_root(None) falls through to its
  // CWD/vault default and prints a multi-line stderr warning that's
  // irrelevant here. Point BRAIN_VAULT at the (possibly not-yet-created)
  // <target>/vault so vault_root() takes the explicit-env branch and stays
  // quiet (fix #3). claude-desktop already requires a real vaultDir
  // (refused above when absent), so state.vaultDir wins whenever it's set.
  const vaultForEnv = state.vaultDir || path.join(target, 'vault');
  return {
    target,
    args: ['connect', '--client', o.client, '--target', target, '--yes'],
    env: { BRAIN_VAULT: vaultForEnv },
    warnUninitialized: isDesktop && o.noVault === true,
  };
}

// ---------------------------------------------------------------------------
// main
// ---------------------------------------------------------------------------

async function main() {
  const opts = parseArgs(process.argv.slice(2));
  if (opts.help) {
    printHelp();
    return 0;
  }
  const dryRun = opts.dryRun;

  console.log(`Brainiac bootstrap${dryRun ? ' — DRY RUN (executes nothing, no network)' : ''}`);
  console.log(`platform: ${process.platform}`);
  console.log('');

  // -- Step 1: engine ---------------------------------------------------
  console.log('== Step 1/4: engine (brainiac-cli on PyPI) ==');
  let brainBin = resolveBinary('brain');
  let installOk = true;

  if (brainBin) {
    console.log(`already installed: ${brainBin}`);
    console.log('to upgrade later, run: brain update');
  } else {
    const uv = resolveBinary('uv');
    const pipx = resolveBinary('pipx');

    if (uv) {
      installOk = runCommand(uv, ['tool', 'install', PKG_SPEC], { dryRun }).ok;
    } else if (pipx) {
      installOk = runCommand(pipx, ['install', PKG_SPEC], { dryRun }).ok;
    } else {
      // Only scan PATH for a Python interpreter once uv AND pipx are both
      // absent — resolvePython() does its own PATH walk(s), wasted work when
      // either of the above already won (fix #6).
      const py = resolvePython();
      if (py && (dryRun || pythonVersionOk(py))) {
        installOk = runCommand(
          py.resolvedPath,
          [...py.argsPrefix, '-m', 'pip', 'install', '--user', PKG_SPEC],
          { dryRun },
        ).ok;
      } else {
        printNoPythonInstructions();
        if (!dryRun) {
          process.exitCode = 2;
          return 2;
        }
        installOk = false;
      }
    }

    if (!dryRun && !installOk) {
      console.log('Engine install failed — see output above.');
      process.exitCode = 1;
      return 1;
    }
  }

  // -- Step 2: verify -----------------------------------------------------
  console.log('');
  console.log('== Step 2/4: verify ==');
  if (dryRun) {
    console.log('  [dry-run] would run: brain --version');
  } else {
    brainBin = resolveBinary('brain');
    if (!brainBin) {
      printPathHint();
      process.exitCode = 2;
      return 2;
    }
    const res = spawnResolved(brainBin, ['--version'], { encoding: 'utf8' });
    if (res.status !== 0) {
      console.log(res.error ? `❌ brain --version failed: ${res.error.message}` : '❌ brain --version failed');
      process.exitCode = 2;
      return 2;
    }
    console.log(`✅ ${(res.stdout || '').trim()}`);
  }

  // stepFailed tracks Step 3/4 failures so main() can exit non-zero even
  // though those steps (unlike Steps 1/2) don't return early — a failed
  // vault init or client connect must still fail `npx brainiac-install … && next`.
  let stepFailed = false;
  // workspaceRoot is the PROJECT ROOT (where CLAUDE.md/AGENTS.md/
  // .gemini/settings.json live — see `brain connect --target`, DEFAULT ".").
  // It is SEPARATE from vaultDir (workspaceRoot/vault, only relevant to
  // BRAIN_VAULT). Both are resolved once here and reused by both Step 3 and
  // Step 4 so they can never drift apart.
  let workspaceRoot = null;
  let vaultDir = null;
  let initVault = false;

  // -- Step 3: vault init ---------------------------------------------------
  // Bootstrap ends at a usable vault, not a bare CLI — the ≤2-step promise is
  // measured to first search, not to `brain --version`.
  console.log('');
  console.log('== Step 3/4: vault ==');

  if (opts.noVault) {
    if (opts.vault) {
      // --no-vault skips *init*, but --vault still names the project root a
      // marked-block client (codex/claude-code/gemini) should be wired
      // into, and the (uninitialized) vault claude-desktop's stanza would
      // point at — resolve it now so Step 4 has an explicit target (fix #5).
      workspaceRoot = resolveWorkspace(opts.vault);
      vaultDir = path.join(workspaceRoot, 'vault');
      console.log(`--no-vault set; skipping vault init (workspace resolved for Step 4: ${workspaceRoot}).`);
    } else {
      console.log('--no-vault set; skipping vault init.');
    }
  } else {
    let workspaceRaw = opts.vault;
    if (!workspaceRaw) {
      if (dryRun) {
        // no interactive prompt in dry-run; fall through to the placeholder
        // branch below so the full plan (incl. Step 4's --target) still
        // prints deterministically with zero subprocess/stdin use.
      } else if (process.stdin.isTTY) {
        const readline = require('node:readline/promises');
        const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
        const answer = (await rl.question(
          'Workspace directory for your vault (blank to skip; vault -> <path>/vault): ',
        )).trim();
        rl.close();
        workspaceRaw = answer || null;
      }
    }
    if (workspaceRaw) {
      workspaceRoot = resolveWorkspace(workspaceRaw);
      vaultDir = path.join(workspaceRoot, 'vault');
      initVault = true;
    } else if (dryRun) {
      workspaceRoot = '<workspace>';
      vaultDir = '<workspace>/vault';
      initVault = true;
    }
  }

  if (initVault) {
    const cmdLine = `BRAIN_VAULT=${vaultDir} brain init --full --apply`;
    console.log(`  $ ${cmdLine}`);
    if (dryRun) {
      console.log('  [dry-run] would run the above');
    } else {
      const res = spawnResolved(brainBin, ['init', '--full', '--apply'], {
        stdio: 'inherit',
        env: { ...process.env, BRAIN_VAULT: vaultDir },
      });
      if (res.error) {
        console.log(`❌ vault init: ${res.error.message}`);
        stepFailed = true;
      } else if (res.status === 0) {
        console.log('✅ vault init');
        console.log(`  try it: BRAIN_VAULT=${vaultDir} brain search "<query>" --json`);
      } else {
        console.log('❌ vault init');
        stepFailed = true;
      }
    }
  } else if (!opts.noVault) {
    console.log('Skipped — run this yourself when ready:');
    console.log('  BRAIN_VAULT=<workspace>/vault brain init --full --apply');
  }

  // -- Step 4: client -------------------------------------------------------
  // --yes: this is a non-interactive installer, so `brain connect` (which
  // otherwise refuses to proceed without a TTY) must get the non-interactive
  // opt-in explicitly. --target: `connect` writes CLAUDE.md/AGENTS.md/
  // .gemini/settings.json under --target (default "." — the CWD `brain`
  // itself would use, NOT the vault) — always pass it explicitly
  // (workspaceRoot when known, else cwd) so wiring never silently lands
  // wherever `npx` happened to be invoked from (fix #1). BRAIN_VAULT only
  // matters to claude-desktop's stanza (codex/claude-code/gemini are
  // pointer-only, no vault dependency); when no vault is known at all,
  // claude-desktop is refused rather than silently pointing at brain's own
  // $CWD/vault default (fix #5).
  console.log('');
  console.log('== Step 4/4: client ==');
  if (opts.client) {
    const plan = buildConnectPlan(opts, { workspaceRoot, vaultDir, stepFailed });
    if (plan.skip === 'init-failed') {
      console.log('Skipping client wiring: vault init failed.');
    } else if (plan.skip === 'desktop-no-vault') {
      console.log('⚠️  claude-desktop needs an explicit vault to point at; none is known');
      console.log('   (pass --vault <path>, or drop --no-vault). Refusing to wire a silent');
      console.log('   $CWD/vault default into the claude-desktop MCP stanza.');
      // Same dry-run exemption as Step 1's no-Python case: a dry-run always
      // previews the full plan and exits 0 (barring upfront arg-parse
      // errors) — only a REAL run that truly cannot proceed fails.
      if (!dryRun) stepFailed = true;
    } else {
      if (plan.warnUninitialized) {
        console.log(`⚠️  --no-vault set: the claude-desktop stanza will point at ${vaultDir},`);
        console.log('   which has not been initialized yet. Run the init command above first.');
      }
      const envLine = plan.env.BRAIN_VAULT ? `BRAIN_VAULT=${plan.env.BRAIN_VAULT} ` : '';
      console.log(`  $ ${envLine}brain ${plan.args.join(' ')}`);
      if (dryRun) {
        console.log('  [dry-run] would run the above');
      } else {
        const res = spawnResolved(brainBin, plan.args, {
          stdio: 'inherit',
          env: plan.env.BRAIN_VAULT ? { ...process.env, ...plan.env } : process.env,
        });
        if (res.error) {
          console.log(`❌ client connect failed: ${res.error.message}`);
          stepFailed = true;
        } else if (res.status === 0) {
          console.log('✅ client connected');
        } else {
          console.log('❌ client connect failed');
          stepFailed = true;
        }
      }
    }
  } else {
    console.log(`No --client given. Available: ${CLIENTS.join(', ')}`);
    console.log('Re-run with --client <name> to wire one, e.g.: npx brainiac-install --client claude-code');
  }

  if (stepFailed) {
    process.exitCode = 1;
    return 1;
  }
  return 0;
}

// Exported for test/cli.test.js (node:test, stdlib only) to exercise the
// platform-conditional branches (Windows quoting, non-Windows executable-bit
// gating, python3->python fallback) that this repo's OS can't observe just
// by running the CLI end-to-end. require.main guard below keeps `require()`
// side-effect-free for that — main() only runs when this file is the entry.
module.exports = {
  parseArgs, resolveBinary, isExecutableFile, needsShellRoute, quoteForShell,
  resolvePython, CLIENTS, expandHome, resolveWorkspace, buildConnectPlan,
};

if (require.main === module) {
  main()
    .then((code) => { process.exitCode = code || 0; })
    .catch((err) => {
      console.error(err && err.stack ? err.stack : String(err));
      process.exitCode = 1;
    });
}
