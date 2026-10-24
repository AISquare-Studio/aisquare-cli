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
# THE RELEASE STAMP. In the repository these two lines name main's install.sh
# and no version, so this file installs the latest release. The copy attached to
# a GitHub Release is rewritten by scripts/stamp_installers.py to fetch THAT
# release's install.sh asset and to pin its version, so
# releases/download/vX.Y.Z/install.ps1 installs exactly X.Y.Z. A --version among
# the arguments still wins: install.sh reads its options left to right.
$StampedVersion = ''
$InstallUrl = "https://raw.githubusercontent.com/$Repo/main/install.sh"

# Arguments are passed straight through to install.sh, so
# `... | iex` with no arguments and `install.ps1 --yes --no-agent` both work.
$Forward = if ($args.Count -gt 0) { $args -join ' ' } else { '' }
$Pin = if ($StampedVersion) { "--version $StampedVersion " } else { '' }

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
$inner = "set -e; curl -fsSL '$InstallUrl' | sh -s -- $Pin$Forward"

# THE HAND-OFF, for main's copy only: a release's copy is stamped above and
# already fetches its own release's install.sh. main's install.sh installs the
# latest release, but its steps and checks can be ahead of that release, so this
# fetches the release's OWN install.sh instead -- the latest release's, or V's
# for --version V -- into a file inside WSL, and runs it if it is a POSIX sh
# script stamped with the version wanted: V, or what PyPI calls the latest. On
# any failure it runs main's install.sh as before, told not to try the same
# hand-off again. install.sh's own `hand_off` makes the same checks; this is the
# Windows end of it. --no-handoff, --offline and AISQUARE_INSTALL_NO_HANDOFF=1
# skip it, as they do there.
#
# Only single quotes reach bash: Windows PowerShell 5.1 does not escape a double
# quote inside an argument to a native command, so none is put in one.
if (-not $StampedVersion) {
    $Want = ''
    $NoHandoff = $env:AISQUARE_INSTALL_NO_HANDOFF -eq '1'
    for ($i = 0; $i -lt $args.Count; $i++) {
        if ($args[$i] -eq '--no-handoff' -or $args[$i] -eq '--offline') { $NoHandoff = $true }
        elseif ($args[$i] -eq '--version' -and $i + 1 -lt $args.Count) { $Want = $args[$i + 1] }
        elseif ($args[$i] -like '--version=*') { $Want = $args[$i].Substring(10) }
    }
    if ($Want -eq 'latest') { $Want = '' }
    $ReleaseUrl = "https://github.com/$Repo/releases/latest/download/install.sh"
    if ($Want) {
        $ReleaseUrl = "https://github.com/$Repo/releases/download/v$Want/install.sh"
    }
    elseif (-not $NoHandoff) {
        try {
            $Pypi = Invoke-RestMethod -Uri 'https://pypi.org/pypi/aisquare-cli/json' -TimeoutSec 15 -UseBasicParsing
            $Want = [string]$Pypi.info.version
        }
        catch {
            $Want = ''
        }
    }
    # A version and nothing else, since it is written into a bash command.
    if ($Want -notmatch '^[0-9][0-9A-Za-z.]*$') { $Want = '' }
    # Every option given has to be one the release's script names, or it would
    # exit 64 where main's could have carried on (install.sh checks the same).
    $Knows = ''
    foreach ($option in @($args | Where-Object { $_ -like '-*' })) {
        $flag = ($option -split '=')[0]
        if ($flag -notmatch '^-[-A-Za-z0-9]+$') { $Want = '' }
        $Knows += " && grep -qF -e '$flag' `$f"
    }

    $Fallback = "curl -fsSL '$InstallUrl' | AISQUARE_INSTALL_NO_HANDOFF=1 sh -s -- $Forward"
    if ($NoHandoff -or -not $Want) {
        $inner = "set -e; $Fallback"
    }
    else {
        # The stamp line is STAMPED_VERSION="V"; each `.` around V stands for
        # one of its double quotes.
        $StampLine = 'STAMPED_VERSION=.' + ($Want -replace '\.', '\.') + '.'
        $inner = "set -e; f=`$(mktemp); trap 'rm -f `$f' EXIT; " +
        "if curl -fsSL --connect-timeout 5 --max-time 30 -o `$f '$ReleaseUrl' && " +
        "head -n 1 `$f | grep -qx '#!/bin/sh' && sh -n `$f && grep -qx '$StampLine' `$f$Knows; then " +
        "echo '  using the v$Want installer'; AISQUARE_INSTALL_HANDED_OFF=1 sh `$f $Forward; " +
        "else echo '  using this installer'; $Fallback; fi"
    }
}

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
