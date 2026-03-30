param(
    [string]$RepoUrl = $(if ($env:GHOST_REPO_URL) { $env:GHOST_REPO_URL } else { "https://github.com/denisalbertofx/ghostllm.git" }),
    [string]$InstallDir = $(if ($env:GHOST_INSTALL_DIR) { $env:GHOST_INSTALL_DIR } else { Join-Path $HOME ".ghostllm" }),
    [string]$Branch = $(if ($env:GHOST_BRANCH) { $env:GHOST_BRANCH } else { "master" })
)

$ErrorActionPreference = "Stop"

function Require-Command([string]$Name) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Missing required command: $Name"
    }
}

Require-Command git
Require-Command python
Require-Command uv

function Test-SafeInstallDir([string]$PathValue) {
    if (-not $PathValue) { return $false }
    $trimmed = $PathValue.Trim()
    if ($trimmed -in @('', '.', '~', '/', '\')) { return $false }
    $full = [System.IO.Path]::GetFullPath($PathValue)
    $root = [System.IO.Path]::GetPathRoot($full)
    $homeFull = [System.IO.Path]::GetFullPath($HOME)
    if ($full -eq $root -or $full -eq $homeFull) { return $false }
    return $full.StartsWith($homeFull, [System.StringComparison]::OrdinalIgnoreCase)
}

$InstallDir = [System.IO.Path]::GetFullPath($InstallDir)
if (-not (Test-SafeInstallDir $InstallDir)) {
    throw "Refusing unsafe install dir: $InstallDir"
}

$binDir = Join-Path $HOME ".local\\bin"
New-Item -ItemType Directory -Force -Path $binDir | Out-Null

if ((Test-Path $InstallDir) -and (Test-Path (Join-Path $InstallDir ".git"))) {
    $currentRemote = (git -C $InstallDir remote get-url origin 2>$null)
    if ($LASTEXITCODE -ne 0 -or $currentRemote.Trim() -ne $RepoUrl) {
        throw "Refusing to update unrelated git checkout in $InstallDir"
    }
    Write-Host "Updating GhostLLM in $InstallDir"
    git -C $InstallDir fetch --all --tags
    git -C $InstallDir checkout $Branch
    git -C $InstallDir pull --ff-only
} else {
    if (Test-Path $InstallDir) {
        $item = Get-Item -LiteralPath $InstallDir
        if (-not $item.PSIsContainer) {
            throw "Install path exists and is not a directory: $InstallDir"
        }
        if ((Get-ChildItem -LiteralPath $InstallDir -Force | Select-Object -First 1) -ne $null) {
            throw "Install path exists and is not an approved GhostLLM checkout: $InstallDir"
        }
        Remove-Item $InstallDir -Force
    }
    Write-Host "Cloning GhostLLM into $InstallDir"
    git clone --branch $Branch $RepoUrl $InstallDir
}

Push-Location $InstallDir
uv sync
Pop-Location

$shimPath = Join-Path $binDir "ghost.ps1"
@" 
Set-Location "$InstallDir"
uv run python apps/cli/main.py @args
"@ | Set-Content -Path $shimPath -Encoding UTF8

$cmdShimPath = Join-Path $binDir "ghost.cmd"
@"
@echo off
cd /d "$InstallDir"
uv run python apps/cli/main.py %*
"@ | Set-Content -Path $cmdShimPath -Encoding ASCII

Write-Host ""
Write-Host "GhostLLM installed."
Write-Host "Shims: $shimPath and $cmdShimPath"
Write-Host ""
Write-Host "Next steps:"
Write-Host "  ghost init"
Write-Host "  ghost start"
Write-Host "  ghost doctor"
Write-Host "  ghost codex"
Write-Host ""
Write-Host "For updates:"
Write-Host "  ghost update"
