<#
build_windows.ps1 — build the `brain` one-dir Windows bundle (PKG-01).

Run on a Windows build runner (GitHub Actions windows-latest, or a managed
Windows VM). Produces an UNSIGNED one-dir under dist\brain\. Signing is a
SEPARATE step (sign_windows.ps1) gated on Azure Trusted Signing onboarding (PW-2).

Sequencing gate (HARDENED:codex / codex-verify-r1): do NOT proceed to
sign_windows.ps1 until the S05 frozen-baseline eval is GREEN and S08 security is
GREEN. This script only BUILDS — it never signs — so it is safe to run anytime.
#>
[CmdletBinding()]
param(
  [string]$Repo = (Resolve-Path "$PSScriptRoot\..\.."),
  [switch]$RebuildBootloader   # decision #4: compile a unique bootloader from source
)
$ErrorActionPreference = "Stop"
Set-Location $Repo

Write-Host "== brain Windows one-dir build ==" -ForegroundColor Cyan
python --version
python -m pip install --quiet --upgrade pip

if ($RebuildBootloader) {
  # Decision #4 — custom-compiled bootloader. The stock PyInstaller bootloader
  # ships in every malware corpus; rebuilding from source gives a unique byte
  # signature that does not match those samples. Needs a C toolchain + the
  # PyInstaller source tree (pip download --no-binary). See the runbook.
  Write-Host "Rebuilding PyInstaller bootloader from source (unique signature)..." -ForegroundColor Yellow
  python -m pip install --no-binary pyinstaller pyinstaller
} else {
  python -m pip install --quiet pyinstaller
}

# Install the product with the CORPORATE minimal-dep set (DIST-01): direct-ONNX
# e5-small, NO fastembed/PyTorch. The model is bundled inline (DIST-02).
python -m pip install --quiet -e ".[corporate]"

# pefile is required on Windows to stamp the PE version resource.
python -m pip install --quiet pefile

# DIST-02: stage the e5-small ONNX model inline so the frozen binary is
# offline-first (no HF download at run time). Set BRAIN_SKIP_MODEL_BUNDLE=1
# (CI / hash-embedder path) to skip staging.
if ($env:BRAIN_SKIP_MODEL_BUNDLE -ne "1") {
  python "$PSScriptRoot\stage_model.py" --repo Xenova/multilingual-e5-small `
    --out "$Repo\packaging\model_bundle\e5-small" `
    --patterns "onnx/model.onnx" "tokenizer.json" "tokenizer_config.json" "special_tokens_map.json" "config.json"
}

$env:BRAIN_MODEL_BUNDLE = "$Repo\packaging\model_bundle\e5-small"
pyinstaller --clean --noconfirm `
  --distpath "$Repo\dist" --workpath "$Repo\build" `
  "$PSScriptRoot\brain-windows.spec"

$exe = "$Repo\dist\brain\brain.exe"
if (-not (Test-Path $exe)) { throw "build failed: $exe not produced" }
Write-Host "Built (UNSIGNED): $exe" -ForegroundColor Green
Get-FileHash $exe -Algorithm SHA256 | Format-List
Write-Host "Next: sign_windows.ps1 (PENDING Azure Trusted Signing — PW-2), then intune\package_intunewin.ps1"
