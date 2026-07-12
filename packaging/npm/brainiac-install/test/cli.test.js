'use strict';

// Lightweight self-check (node:test, stdlib only — no framework) for the
// platform-conditional branches fixed in the S09 rework: Windows shell-quote
// escaping, non-Windows executable-bit gating on PATH candidates, and the
// python3 -> python fallback. These are exactly the branches the tarball
// smoke test (_evidence/install-plan/s09-npx-smoke.txt) can't observe when
// run on a single OS. Run with: npm test (or: node --test)
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const cli = require('../bin/cli.js');

const CLI_PATH = path.join(__dirname, '..', 'bin', 'cli.js');

function withPlatform(value, fn) {
  const orig = Object.getOwnPropertyDescriptor(process, 'platform');
  Object.defineProperty(process, 'platform', { value, configurable: true });
  try {
    fn();
  } finally {
    Object.defineProperty(process, 'platform', orig);
  }
}

function withPath(dirs, fn) {
  const orig = process.env.PATH;
  process.env.PATH = dirs.join(path.delimiter);
  try {
    fn();
  } finally {
    process.env.PATH = orig;
  }
}

test('quoteForShell wraps only tokens containing whitespace', () => {
  assert.equal(cli.quoteForShell('C:\\Users\\First Last\\brain.cmd'), '"C:\\Users\\First Last\\brain.cmd"');
  assert.equal(cli.quoteForShell('C:\\Users\\bob\\brain.cmd'), 'C:\\Users\\bob\\brain.cmd');
  assert.equal(cli.quoteForShell('--client'), '--client');
});

test('quoteForShell quotes cmd.exe metacharacters even without whitespace (S09-3 fix #2)', () => {
  assert.equal(cli.quoteForShell('C:\\proj&test'), '"C:\\proj&test"');
  assert.equal(cli.quoteForShell('C:\\a^b'), '"C:\\a^b"');
  assert.equal(cli.quoteForShell('C:\\a(b)'), '"C:\\a(b)"');
  assert.equal(cli.quoteForShell('C:\\a<b>c'), '"C:\\a<b>c"');
  assert.equal(cli.quoteForShell('C:\\a|b'), '"C:\\a|b"');
  assert.equal(cli.quoteForShell(''), '""');
  // no metachar, no whitespace -> untouched
  assert.equal(cli.quoteForShell('C:\\Users\\bob\\brain.cmd'), 'C:\\Users\\bob\\brain.cmd');
});

test('quoteForShell rejects % and " instead of mis-quoting them (S09 final)', () => {
  // cmd.exe still expands %VAR% inside double quotes, and a literal " can't
  // be escaped/balanced through the shell:true join — neither is safely
  // quotable, so quoteForShell throws rather than emit a corrupted arg.
  assert.throws(() => cli.quoteForShell('C:\\a%b%'), /unsupported on Windows shim invocation/);
  assert.throws(() => cli.quoteForShell('C:\\a"b'), /unsupported on Windows shim invocation/);
});

test('needsShellRoute is Windows-only and extension-gated', () => {
  withPlatform('win32', () => {
    assert.equal(cli.needsShellRoute('C:\\a\\brain.cmd'), true);
    assert.equal(cli.needsShellRoute('C:\\a\\brain.exe'), false);
  });
  withPlatform('darwin', () => {
    assert.equal(cli.needsShellRoute('/usr/local/bin/brain.cmd'), false);
  });
});

test('resolveBinary requires the executable bit on non-Windows (fix #6)', (t) => {
  if (process.platform === 'win32') { t.skip('POSIX x-bit test, not applicable on win32'); return; }
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'brainiac-install-test-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const nonExec = path.join(dir, 'notrunnable');
  const exec = path.join(dir, 'runnable');
  fs.writeFileSync(nonExec, '#!/bin/sh\n', { mode: 0o644 });
  fs.writeFileSync(exec, '#!/bin/sh\n', { mode: 0o755 });
  withPath([dir], () => {
    assert.equal(cli.resolveBinary('notrunnable'), null,
      'a non-executable file on PATH must not be accepted as a binary');
    assert.equal(cli.resolveBinary('runnable'), exec);
  });
});

test('resolvePython falls back to `python` when `python3` is absent (fix #7)', (t) => {
  if (process.platform === 'win32') { t.skip('non-Windows fallback, not applicable on win32'); return; }
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'brainiac-install-test-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const python = path.join(dir, 'python');
  fs.writeFileSync(python, '#!/bin/sh\n', { mode: 0o755 });
  withPath([dir], () => {
    const py = cli.resolvePython();
    assert.ok(py, 'expected resolvePython() to find the `python` fallback');
    assert.equal(py.resolvedPath, python);
    assert.equal(py.label, 'python');
  });
});

test('CLIENTS stays in sync with the documented client set', () => {
  assert.deepEqual(cli.CLIENTS, ['claude-code', 'claude-desktop', 'codex', 'gemini']);
});

// --- S09 rework 2 additions ---------------------------------------------

test('expandHome/resolveWorkspace expand bare `~` and `~/x` (fix #2)', () => {
  const home = os.homedir();
  assert.equal(cli.expandHome('~'), home);
  assert.equal(cli.expandHome('~/my-brain'), path.join(home, 'my-brain'));
  // must NOT expand ~user
  assert.equal(cli.expandHome('~bob/my-brain'), '~bob/my-brain');
  // a relative, non-tilde path is untouched by expandHome (resolveWorkspace
  // does the path.resolve() on top)
  assert.equal(cli.resolveWorkspace('~/my-brain'), path.resolve(path.join(home, 'my-brain')));
  assert.equal(cli.resolveWorkspace('~'), path.resolve(home));
});

test('buildConnectPlan passes --target matching the workspace root and BRAIN_VAULT (fix #1)', () => {
  const plan = cli.buildConnectPlan(
    { client: 'codex', noVault: false },
    { workspaceRoot: '/tmp/some-workspace', vaultDir: '/tmp/some-workspace/vault', stepFailed: false },
  );
  assert.equal(plan.target, '/tmp/some-workspace');
  assert.deepEqual(plan.args, ['connect', '--client', 'codex', '--target', '/tmp/some-workspace', '--yes']);
  assert.equal(plan.env.BRAIN_VAULT, '/tmp/some-workspace/vault');
});

test('buildConnectPlan falls back to cwd for --target when no workspace is known, never omits it', () => {
  const plan = cli.buildConnectPlan(
    { client: 'gemini', noVault: false },
    { workspaceRoot: null, vaultDir: null, stepFailed: false },
  );
  assert.equal(plan.target, process.cwd());
  assert.ok(plan.args.includes('--target'));
});

test('buildConnectPlan skips connect when Step 3 vault-init failed (fix #3)', () => {
  const plan = cli.buildConnectPlan(
    { client: 'codex', noVault: false },
    { workspaceRoot: '/tmp/ws', vaultDir: '/tmp/ws/vault', stepFailed: true },
  );
  assert.equal(plan.skip, 'init-failed');
});

test('buildConnectPlan refuses claude-desktop with no known vault instead of a silent CWD/vault fallback (fix #5)', () => {
  const plan = cli.buildConnectPlan(
    { client: 'claude-desktop', noVault: true },
    { workspaceRoot: null, vaultDir: null, stepFailed: false },
  );
  assert.equal(plan.skip, 'desktop-no-vault');
});

test('buildConnectPlan wires claude-desktop under --no-vault with an explicit (uninitialized) vault + warning flag (fix #5)', () => {
  const plan = cli.buildConnectPlan(
    { client: 'claude-desktop', noVault: true },
    { workspaceRoot: '/tmp/ws', vaultDir: '/tmp/ws/vault', stepFailed: false },
  );
  assert.equal(plan.skip, undefined);
  assert.equal(plan.warnUninitialized, true);
  assert.equal(plan.env.BRAIN_VAULT, '/tmp/ws/vault');
});

test('buildConnectPlan does not warn for marked-block clients under --no-vault', () => {
  const plan = cli.buildConnectPlan(
    { client: 'codex', noVault: true },
    { workspaceRoot: '/tmp/ws', vaultDir: '/tmp/ws/vault', stepFailed: false },
  );
  assert.equal(plan.warnUninitialized, false);
});

test('--client= (empty) and --vault= (empty) exit 2, same as the missing space-form token (fix #4)', () => {
  const r1 = spawnSync(process.execPath, [CLI_PATH, '--dry-run', '--client='], { encoding: 'utf8' });
  assert.equal(r1.status, 2, r1.stderr);
  assert.match(r1.stderr, /--client requires a value/);

  const r2 = spawnSync(process.execPath, [CLI_PATH, '--dry-run', '--vault='], { encoding: 'utf8' });
  assert.equal(r2.status, 2, r2.stderr);
  assert.match(r2.stderr, /--vault requires a value/);
});

test('--client (empty space-form token) exits 2, same as the missing token (S09-3 fix #1)', () => {
  const r1 = spawnSync(process.execPath, [CLI_PATH, '--dry-run', '--client', ''], { encoding: 'utf8' });
  assert.equal(r1.status, 2, r1.stderr);
  assert.match(r1.stderr, /--client requires a value/);

  const r2 = spawnSync(process.execPath, [CLI_PATH, '--dry-run', '--vault', ''], { encoding: 'utf8' });
  assert.equal(r2.status, 2, r2.stderr);
  assert.match(r2.stderr, /--vault requires a value/);
});

test('buildConnectPlan sets BRAIN_VAULT for a marked-block client with no known vault (S09-3 fix #3)', () => {
  const plan = cli.buildConnectPlan(
    { client: 'codex', noVault: true },
    { workspaceRoot: '/tmp/ws-no-vault', vaultDir: null, stepFailed: false },
  );
  assert.equal(plan.skip, undefined);
  assert.equal(plan.env.BRAIN_VAULT, path.join('/tmp/ws-no-vault', 'vault'));
});

test('--dry-run --vault ~/... shows tilde-expanded --target and BRAIN_VAULT in the printed plan', () => {
  const r = spawnSync(
    process.execPath,
    [CLI_PATH, '--dry-run', '--vault', '~/brainiac-s09-test-ws', '--client', 'codex'],
    { encoding: 'utf8' },
  );
  assert.equal(r.status, 0, r.stderr);
  const expectedRoot = path.join(os.homedir(), 'brainiac-s09-test-ws');
  assert.ok(r.stdout.includes(expectedRoot), r.stdout);
  assert.ok(r.stdout.includes(`--target ${expectedRoot}`), r.stdout);
  assert.ok(r.stdout.includes(`BRAIN_VAULT=${path.join(expectedRoot, 'vault')}`), r.stdout);
});
