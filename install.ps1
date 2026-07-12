<#
.SYNOPSIS
    Brainiac one-command installer (Windows / PowerShell).

.DESCRIPTION
    Windows counterpart to install.sh (macOS/Linux) — same contract, same
    post-cold-start behaviour (no blocking model download, no OCR install).

    PyPI-first by default (PYP-04): tries, in order, `uv tool install` ->
    `pipx install` -> `pip install --user`, first success wins, each attempt
    visibly reported. Pass -Dev for the contributor/offline path — an
    editable install from THIS checkout into a private venv under
    `%USERPROFILE%\.brainiac\venv` (or `$env:BRAINIAC_HOME\venv`), the
    pre-PyPI behavior. Every channel installs the `[mcp]` extra so
    `brain-mcp` works out of the box.

    Unlike install.sh's `--with-ocr` flag, this script never invokes a
    package manager on your behalf (winget/choco availability and elevation
    vary too much across Windows machines to assume one is safe to run
    unattended) — it only prints the manual command. See the OCR section
    below.

.PARAMETER Dev
    Contributor/offline path: editable install from this checkout instead of
    PyPI. Also builds the checkout's own lexical-only sample-vault index.

.EXAMPLE
    .\install.ps1
    PyPI-first install. Run from anywhere (a bare downloaded install.ps1
    works — no clone required).

.EXAMPLE
    .\install.ps1 -Dev
    Editable install from a cloned checkout, for contributors/offline use.
#>
[CmdletBinding()]
param(
    [switch]$Dev
)

# Unknown/extra parameters are rejected automatically by PowerShell's own
# parameter binder (there is nothing declared above to accept them) — this
# is the .ps1 equivalent of install.sh's "unknown option" guard.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
# PowerShell 7.3+ defaults $PSNativeCommandUseErrorActionPreference to $true,
# which makes a non-zero exit code from a native exe (py/pip/brain.exe) throw
# a generic terminating error immediately under ErrorActionPreference='Stop'
# -- pre-empting our own `if ($LASTEXITCODE -ne 0) { Fail ... }` checks below
# and their friendlier messages. Pin it off so every version of PowerShell
# (5.1 doesn't know this variable at all -- setting it is a harmless no-op
# there) behaves the same: native failures flow through our own checks.
$PSNativeCommandUseErrorActionPreference = $false

function Say {
    param([string]$Message)
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Fail {
    param([string]$Message)
    # -ErrorAction Continue here overrides the script-wide 'Stop' just for
    # this call, so the message prints instead of throwing a stack trace;
    # the explicit exit is what actually stops the script.
    Write-Error "ERROR: $Message" -ErrorAction Continue
    exit 1
}

# ---------------------------------------------------------------------------
# 1. Python check (>=3.9).
#    Windows machines commonly have the `py` launcher (py.exe) rather than a
#    bare `python3` on PATH; prefer it (py -3 unambiguously picks Python 3)
#    and fall back to `python` (python.org / winget installs both put this
#    on PATH; the launcher is what's sometimes missing, not `python`).
# ---------------------------------------------------------------------------
$PyExe = $null
$PyBaseArgs = @()
if (Get-Command py -ErrorAction SilentlyContinue) {
    $PyExe = 'py'
    $PyBaseArgs = @('-3')
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $PyExe = 'python'
} else {
    Fail "Python not found. Install Python 3.9+ from https://python.org (check 'Add python.exe to PATH' in the installer) or 'winget install Python.Python.3.12', then re-run this script."
}

function Invoke-Py {
    param([string[]]$PyArgs)
    & $PyExe @PyBaseArgs @PyArgs
}

Invoke-Py -PyArgs @('-c', 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)')
if ($LASTEXITCODE -ne 0) {
    $verOutput = (Invoke-Py -PyArgs @('--version')) 2>&1
    Fail "Python 3.9+ required; found $verOutput."
}

# ---------------------------------------------------------------------------
# 1a. venv module preflight (mirrors install.sh's CS-03 check), only
#     load-bearing for -Dev (the editable venv). Most Windows Python
#     distributions ship venv/ensurepip in the box, but a minimal or
#     corrupted install can still be missing them — catching it here gives a
#     clear fix instead of a confusing failure deep inside step 2.
# ---------------------------------------------------------------------------
if ($Dev) {
    Invoke-Py -PyArgs @('-c', 'import venv, ensurepip') *>$null
    if ($LASTEXITCODE -ne 0) {
        Fail "Python's venv/ensurepip modules are missing. Reinstall Python from https://python.org with the default components selected (or 'winget install Python.Python.3.12'), then re-run this script."
    }
}

# ---------------------------------------------------------------------------
# 1b. OCR toolchain — NEVER installed by this script (deviation from
#     install.sh's --with-ocr, which can shell out to brew/apt). `brain
#     ingest` only needs ocrmypdf + tesseract for image-only PDFs; without
#     them a scanned PDF just quarantines as `pdf_no_text_layer` instead of
#     failing anything, so skipping this is safe by default.
# ---------------------------------------------------------------------------
Say "OCR toolchain: not installed by this script. Scanned PDFs quarantine (pdf_no_text_layer) until you add it manually:"
Write-Host "    winget install --id UB-Mannheim.TesseractOCR -e"
Write-Host "    (or) choco install tesseract"
Write-Host "    then: <venv>\Scripts\pip.exe install ocrmypdf"

# ---------------------------------------------------------------------------
# 2. Engine install — PyPI-first by default (PYP-04): uv tool install ->
#    pipx install -> pip install --user, first success wins, each attempt
#    visibly reported. -Dev keeps the editable-checkout path (contributors /
#    offline / no PyPI access) — private venv, `pip install -e .[mcp]`,
#    unchanged from the pre-PyPI behavior. Every channel carries the [mcp]
#    extra so `brain-mcp` works out of the box (the console script is
#    defined unconditionally, so without the extra it would exist but crash
#    on a missing `mcp` import).
# ---------------------------------------------------------------------------
$RepoDir = $PSScriptRoot
$BrainiacHome = if ($env:BRAINIAC_HOME) { $env:BRAINIAC_HOME } else { Join-Path $env:USERPROFILE '.brainiac' }
$VenvDir = Join-Path $BrainiacHome 'venv'
$VenvScripts = Join-Path $VenvDir 'Scripts'
$InstalledChannel = $null
$PathWasAdded = $false

if ($Dev) {
    Say "Installing Brainiac into $VenvDir (-Dev: editable install from this checkout)"
    Invoke-Py -PyArgs @('-m', 'venv', $VenvDir)
    if ($LASTEXITCODE -ne 0) { Fail "venv creation failed at $VenvDir." }

    $VenvPython = Join-Path $VenvScripts 'python.exe'
    $VenvPip = Join-Path $VenvScripts 'pip.exe'
    # `python -m pip`, NOT pip.exe directly -- pip.exe upgrading ITSELF means
    # the running exe tries to overwrite its own file, which Windows keeps
    # locked while it's executing ([WinError 5] Access is denied). Running
    # pip as a module under python.exe avoids that lock.
    & $VenvPython -m pip install --quiet --upgrade pip
    if ($LASTEXITCODE -ne 0) { Fail "pip self-upgrade failed inside $VenvDir." }
    # NOTE: must be "${RepoDir}[mcp]", NOT "$RepoDir[mcp]" -- inside a
    # double-quoted string, "$var[x]" is parsed as PowerShell array/string
    # INDEXING ($var['x']), not literal text appended after the variable. The
    # curly-brace form stops the parser at the variable name so `[mcp]` stays
    # literal (pip's extras syntax), matching install.sh's `$REPO_DIR[mcp]`.
    & $VenvPip install --quiet -e "${RepoDir}[mcp]"
    if ($LASTEXITCODE -ne 0) { Fail "'pip install -e `"${RepoDir}[mcp]`"' failed. Check the error above." }
    $InstalledChannel = 'editable-checkout'

    # PATH wiring — additive, idempotent (safe to re-run). Windows has no
    # per-shell PATH like ~/.bashrc; the durable equivalent is the User
    # environment variable (registry-backed, picked up by new processes).
    #
    # DELIBERATELY NOT using [Environment]::GetEnvironmentVariable('Path',
    # 'User') / SetEnvironmentVariable(...,'User') here. Get/SetEnvironmentVariable
    # always round-trips through the EXPANDED string -- if the user's existing
    # User PATH has a REG_EXPAND_SZ entry like "%JAVA_HOME%\bin", Get returns it
    # already expanded to a literal path, and writing that back persists it as
    # plain REG_SZ: a "dynamic" PATH entry silently becomes a permanently fixed
    # one. That breaks this script's own "additive -- never clobbers" guarantee.
    # Reading the RAW registry value (DoNotExpandEnvironmentNames) and writing
    # it back with its ORIGINAL RegistryValueKind keeps %VAR% tokens intact.
    # Do NOT "simplify" this back to SetEnvironmentVariable.
    $EnvKey = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey('Environment')
    if ($EnvKey.GetValueNames() -contains 'Path') {
        $RawUserPath = $EnvKey.GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
        $ExistingPathKind = $EnvKey.GetValueKind('Path')
    } else {
        # No User Path value exists yet (fresh account) -- REG_EXPAND_SZ is
        # what Windows itself creates the first time a User PATH entry is added.
        $RawUserPath = ''
        $ExistingPathKind = [Microsoft.Win32.RegistryValueKind]::ExpandString
    }
    if ($null -eq $RawUserPath) { $RawUserPath = '' }
    $UserPathEntries = $RawUserPath -split ';' | Where-Object { $_ -ne '' }

    if ($UserPathEntries -notcontains $VenvScripts) {
        # -notcontains is case-insensitive by default in PowerShell (Windows
        # PATH matching is case-insensitive too), so this is already a safe
        # re-run check.
        $NewUserPath = if ($RawUserPath -and -not $RawUserPath.EndsWith(';')) { "$RawUserPath;$VenvScripts" } else { "$RawUserPath$VenvScripts" }
        $EnvKey.SetValue('Path', $NewUserPath, $ExistingPathKind)
        $PathWasAdded = $true
        Say "Added $VenvScripts to your User PATH (registry) — new terminal windows will see it."
    } else {
        Say "$VenvScripts is already on your User PATH."
    }
    $EnvKey.Close()

    # Also extend *this* process's PATH so the rest of this script — and any
    # commands you paste from the "Try it" block below in the SAME window —
    # can find `brain` right away, without waiting for a new session.
    if (($env:Path -split ';') -notcontains $VenvScripts) {
        $env:Path = "$env:Path;$VenvScripts"
    }
} else {
    Say "Installing brainiac-cli from PyPI — trying uv tool install, then pipx, then pip --user"

    if (Get-Command uv -ErrorAction SilentlyContinue) {
        Say "Attempt 1/3: uv tool install 'brainiac-cli[mcp]'"
        & uv tool install 'brainiac-cli[mcp]'
        if ($LASTEXITCODE -eq 0) { $InstalledChannel = 'uv tool' } else { Say "uv tool install failed — falling back to pipx" }
    } else {
        Say "Attempt 1/3: uv not found on PATH — skipping (install from https://docs.astral.sh/uv/ for the fastest channel)"
    }

    if (-not $InstalledChannel) {
        if (Get-Command pipx -ErrorAction SilentlyContinue) {
            Say "Attempt 2/3: pipx install 'brainiac-cli[mcp]'"
            & pipx install 'brainiac-cli[mcp]'
            if ($LASTEXITCODE -eq 0) { $InstalledChannel = 'pipx' } else { Say "pipx install failed — falling back to pip --user" }
        } else {
            Say "Attempt 2/3: pipx not found on PATH — skipping (https://pipx.pypa.io/)"
        }
    }

    if (-not $InstalledChannel) {
        Say "Attempt 3/3: python -m pip install --user 'brainiac-cli[mcp]'"
        Invoke-Py -PyArgs @('-m', 'pip', 'install', '--user', '--quiet', 'brainiac-cli[mcp]')
        if ($LASTEXITCODE -eq 0) {
            $InstalledChannel = 'pip --user'
        } else {
            Fail "Every install channel failed (uv tool install / pipx install / pip install --user).
  Fixes to try:
    - Check network access to pypi.org (brainiac-cli may also not be published yet -- see README).
    - Install uv (https://docs.astral.sh/uv/) or pipx (https://pipx.pypa.io/) for a more isolated install.
    - Contributors/offline: re-run as '.\install.ps1 -Dev' to install editable from this checkout instead."
        }
    }

    Say "Installed via: $InstalledChannel"

    if ($InstalledChannel -eq 'pip --user' -and -not (Get-Command brain -ErrorAction SilentlyContinue)) {
        $UserBase = (Invoke-Py -PyArgs @('-m', 'site', '--user-base')) | Select-Object -First 1
        Say "NOTE: 'brain' isn't on PATH yet. Add this to your User PATH (or open a new terminal after a User install):"
        Write-Host "    $UserBase\Scripts"
    }
}

# ---------------------------------------------------------------------------
# 3. Verify + next steps. The first-index-build against the checkout's own
#    sample vault only makes sense in -Dev mode (that's the only mode with a
#    local checkout to build from) — lexical-only, no network.
#    BRAIN_EMBEDDER=hash forces the offline deterministic embedder so
#    grep/bases-query/FTS work immediately; `brain search`'s dense leg
#    self-detects this placeholder and degrades to FTS-only with a notice
#    until a real rebuild applies the real model (see `brain warmup` below).
# ---------------------------------------------------------------------------
if ($Dev) {
    $BrainExe = Join-Path $VenvScripts 'brain.exe'
    if (-not (Test-Path $BrainExe)) {
        Fail "brain.exe not found at $BrainExe after install — the pip install above may have failed silently."
    }

    Say "Building a lexical-only index for the sample vault (no model download)"
    Push-Location $RepoDir
    # Save whatever the caller already had (including "unset") so `finally`
    # restores it instead of unconditionally deleting a value the user set
    # themselves before running this script.
    $PriorBrainEmbedder = $env:BRAIN_EMBEDDER
    try {
        $env:BRAIN_EMBEDDER = 'hash'
        & $BrainExe rebuild
        if ($LASTEXITCODE -ne 0) { Fail "'brain rebuild' failed. Check the error above." }
    } finally {
        if ($null -eq $PriorBrainEmbedder) {
            Remove-Item Env:\BRAIN_EMBEDDER -ErrorAction SilentlyContinue
        } else {
            $env:BRAIN_EMBEDDER = $PriorBrainEmbedder
        }
        Pop-Location
    }

    Say "Done. Try it:"
    Write-Host "    cd $RepoDir"
    Write-Host "    brain grep `"arctic-embed`""
    Write-Host "    brain --help"
} else {
    Say "Done. Verify it:"
    Write-Host "    brain --version"
    Write-Host "    brain --help"
    Write-Host ""
    Write-Host "Next: point it at your vault (creates <workspace>\vault if it doesn't exist yet):"
    Write-Host "    `$env:BRAIN_VAULT = '<workspace>\vault'; brain init --full --apply"
}

Write-Host ""
Write-Host "Semantic search downloads its model (multilingual-e5-small, ~465 MB) on first real use, with a"
Write-Host "progress line on stderr -- or run 'brain warmup' now, then 'brain sync'"
Write-Host "to apply it to the index ('brain status' shows embedder: ready|pending)."
if ($PathWasAdded) {
    Write-Host ""
    Write-Host "NOTE: open a NEW PowerShell window for 'brain' to resolve there too --"
    Write-Host "this window already has it (this script extended its own session PATH)."
}
Write-Host ""
Write-Host "Want the nightly maintenance task (drain/sign/reindex/brief) registered as a"
Write-Host "Windows Scheduled Task? Run: .\scripts\install-brief-windows.ps1 -VaultPath <path-to-vault>"
Write-Host ""
Write-Host "Already installed and want the latest? Re-run this script, or in Claude Code: /brainiac-update"
