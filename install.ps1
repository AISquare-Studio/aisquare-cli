# aisquare installer for Windows — a shim into WSL2, and nothing more.
#
#   irm https://raw.githubusercontent.com/AISquare-Studio/aisquare-cli/main/install.ps1 | iex
#
# WHY THIS IS A SHIM AND NOT AN INSTALLER (docs/plans/one-line-install.md §2).
# The fleet UI runs every agent in a real tmux pane, and there is no tmux on
# Windows. So there is no Windows-native install to do: the honest thing is to
# put the user inside WSL2 and run the POSIX installer there, which is also what
# the CLI itself already says on win32 — `services/diagnostics.py install_hint()`
# answers "on Windows the fleet runs inside WSL2" rather than naming a package.
#
# Everything real lives in install.sh. This file detects WSL2, and either
# delegates into it or prints the one command that installs it.

$ErrorActionPreference = 'Stop'

$Repo = 'AISquare-Studio/aisquare-cli'
$InstallUrl = "https://raw.githubusercontent.com/$Repo/main/install.sh"

# Arguments are passed straight through to install.sh, so
# `... | iex` with no arguments and `install.ps1 --yes --no-agent` both work.
$Forward = if ($args.Count -gt 0) { $args -join ' ' } else { '' }

function Write-Step($Message) { Write-Host "==> $Message" -ForegroundColor Cyan }
function Write-Note($Message) { Write-Host "    $Message" -ForegroundColor DarkGray }

Write-Step 'aisquare on Windows runs inside WSL2'
Write-Note 'The fleet UI gives every agent a real tmux pane, and Windows has no tmux.'

# `wsl.exe` exists on modern Windows even with no distribution installed, so its
# presence is not the question — whether a distribution is actually there is.
$wsl = Get-Command wsl.exe -ErrorAction SilentlyContinue
if (-not $wsl) {
    Write-Host ''
    Write-Host 'WSL is not available on this machine.' -ForegroundColor Yellow
    Write-Host 'Install it, reboot, then run this same command again:'
    Write-Host ''
    Write-Host '    wsl --install' -ForegroundColor White
    Write-Host ''
    exit 1
}

# `wsl -l -q` lists installed distributions. It writes UTF-16 to a pipe, which
# is why the output is filtered rather than tested for emptiness — a naive
# `if ($out)` is true even when the only content is a BOM and blank lines.
$distros = @()
try {
    $raw = & wsl.exe --list --quiet 2>$null
    $distros = @($raw | ForEach-Object { $_ -replace "`0", '' } |
        Where-Object { $_.Trim().Length -gt 0 } |
        ForEach-Object { $_.Trim() })
}
catch {
    $distros = @()
}

if ($distros.Count -eq 0) {
    Write-Host ''
    Write-Host 'WSL is present but no Linux distribution is installed.' -ForegroundColor Yellow
    Write-Host 'Install Ubuntu, then run this same command again:'
    Write-Host ''
    Write-Host '    wsl --install -d Ubuntu' -ForegroundColor White
    Write-Host ''
    exit 1
}

Write-Note "found WSL distribution(s): $($distros -join ', ')"
Write-Step "Running the installer inside $($distros[0])"
Write-Host ''

# THE DELEGATION. Two details are load-bearing:
#
#  * `sh -s --` and not `sh`, for exactly the reason §3.5 documents: sh is
#    reading the script on stdin, so arguments have to be handed to it
#    explicitly or they are read as the script's own name.
#  * `-e` on the inner shell so a failure inside WSL becomes this script's
#    failure. Without it the pipeline's status is curl's, and a broken install
#    would report success to whatever ran this.
#
# Deliberately NOT `--yes`: the point of the one-liner is that it ends by
# offering the UI, and a user who ran this by hand is sitting at a terminal.
$inner = "set -e; curl -fsSL '$InstallUrl' | sh -s -- $Forward"
& wsl.exe -d $distros[0] -- bash -lc $inner
$code = $LASTEXITCODE

if ($code -ne 0) {
    Write-Host ''
    Write-Host "The installer exited $code inside WSL." -ForegroundColor Yellow
    Write-Note 'Open the WSL shell and rerun it there to see the full output:'
    Write-Note "  wsl -d $($distros[0])"
    Write-Note "  curl -fsSL $InstallUrl | sh"
    exit $code
}

Write-Host ''
Write-Step 'Done. aisquare is installed inside WSL.'
Write-Note "Open it with:  wsl -d $($distros[0]) -- asq"
Write-Note 'Run everything else from that shell too — the agents live there.'
