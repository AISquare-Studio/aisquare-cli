# aisquare installer for Windows -- a shim into WSL2, and nothing more.
#
# PURE ASCII on purpose. Windows PowerShell 5.1 reads a BOM-less file as ANSI, so
# a UTF-8 em dash saved and re-run under powershell.exe comes out as mojibake --
# and a Windows console's codepage (437, 850 and 1252 all still turn up) can
# mangle it even with a BOM. This is the one file whose whole job is to render
# correctly on a machine nobody has configured yet, so it uses no character that
# cannot survive that. PSScriptAnalyzer's PSUseBOMForUnicodeEncodedFile is what
# pointed it out.
#
#   irm https://raw.githubusercontent.com/AISquare-Studio/aisquare-cli/main/install.ps1 | iex
#
# WHY THIS IS A SHIM AND NOT AN INSTALLER (docs/plans/one-line-install.md section 2).
# The fleet UI runs every agent in a real tmux pane, and there is no tmux on
# Windows. So there is no Windows-native install to do: the honest thing is to
# put the user inside WSL2 and run the POSIX installer there, which is also what
# the CLI itself already says on win32 -- `services/diagnostics.py install_hint()`
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
# presence is not the question -- whether a distribution is actually there is.
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
# is why the output is filtered rather than tested for emptiness -- a naive
# `if ($out)` is true even when the only content is a BOM and blank lines.
function Get-WslOutput($WslArgs) {
    try {
        $raw = & wsl.exe @WslArgs 2>$null
        return @($raw | ForEach-Object { $_ -replace "`0", '' } |
            Where-Object { $_.Trim().Length -gt 0 } |
            ForEach-Object { $_.Trim() })
    }
    catch {
        return @()
    }
}

$distros = Get-WslOutput @('--list', '--quiet')

# DOCKER DESKTOP IS WHY THE FIRST ENTRY IS NOT USED.
#
# `--quiet` lists every registered distribution and does NOT mark which is the
# default -- only the verbose form does. Docker Desktop registers
# `docker-desktop` (historically `docker-desktop-data` too), and `d` sorts
# before `Ubuntu`. So on a very ordinary Windows developer machine, taking the
# first entry ran the installer inside Docker's LinuxKit utility VM: a stripped
# image with no bash, no curl and no persistent home. The failure advice then
# named that same VM, sending the user straight back into it.
#
# So: read the DEFAULT from the verbose listing, which marks it with `*`; fall
# back to the first entry that is not one of Docker's; and refuse if that is all
# there is.
$DockerPattern = '^docker-desktop'
$defaultDistro = $null
foreach ($line in (Get-WslOutput @('--list', '--verbose'))) {
    if ($line -match '^\*\s+(\S+)') { $defaultDistro = $Matches[1]; break }
}

$usable = @($distros | Where-Object { $_ -notmatch $DockerPattern })

if ($defaultDistro -and ($defaultDistro -notmatch $DockerPattern)) {
    $target = $defaultDistro
}
elseif ($usable.Count -gt 0) {
    $target = $usable[0]
    if ($defaultDistro) {
        Write-Note "the default distribution is '$defaultDistro', which is Docker Desktop's"
        Write-Note "utility VM and cannot run this -- using '$target' instead"
    }
}
else {
    $target = $null
}

if (-not $target) {
    Write-Host ''
    if ($distros.Count -gt 0) {
        Write-Host "WSL has only Docker Desktop's utility VM ($($distros -join ', '))," -ForegroundColor Yellow
        Write-Host 'which is not a distribution you can install into.' -ForegroundColor Yellow
    }
    else {
        Write-Host 'WSL is present but no Linux distribution is installed.' -ForegroundColor Yellow
    }
    Write-Host 'Install Ubuntu, then run this same command again:'
    Write-Host ''
    Write-Host '    wsl --install -d Ubuntu' -ForegroundColor White
    Write-Host ''
    exit 1
}

Write-Note "found WSL distribution(s): $($distros -join ', ')"
Write-Step "Running the installer inside $target"
Write-Host ''

# THE DELEGATION. Two details are load-bearing:
#
#  * `sh -s --` and not `sh`, for exactly the reason section 3.5 documents: sh is
#    reading the script on stdin, so arguments have to be handed to it
#    explicitly or they are read as the script's own name.
#  * `-e` on the inner shell so a failure inside WSL becomes this script's
#    failure. Without it the pipeline's status is curl's, and a broken install
#    would report success to whatever ran this.
#
# Deliberately NOT `--yes`: the point of the one-liner is that it ends by
# offering the UI, and a user who ran this by hand is sitting at a terminal.
$inner = "set -e; curl -fsSL '$InstallUrl' | sh -s -- $Forward"
& wsl.exe -d $target -- bash -lc $inner
$code = $LASTEXITCODE

if ($code -ne 0) {
    Write-Host ''
    Write-Host "The installer exited $code inside WSL." -ForegroundColor Yellow
    Write-Note 'Open the WSL shell and rerun it there to see the full output:'
    Write-Note "  wsl -d $target"
    Write-Note "  curl -fsSL $InstallUrl | sh"
    exit $code
}

Write-Host ''
Write-Step 'Done. aisquare is installed inside WSL.'
Write-Note "Open it with:  wsl -d $target -- asq"
Write-Note 'Run everything else from that shell too -- the agents live there.'
