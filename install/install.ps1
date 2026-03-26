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

$binDir = Join-Path $HOME ".local\\bin"
New-Item -ItemType Directory -Force -Path $binDir | Out-Null

if (Test-Path (Join-Path $InstallDir ".git")) {
    Write-Host "Updating GhostLLM in $InstallDir"
    git -C $InstallDir fetch --all --tags
    git -C $InstallDir checkout $Branch
    git -C $InstallDir pull --ff-only
} else {
    if (Test-Path $InstallDir) {
        Remove-Item $InstallDir -Recurse -Force
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
