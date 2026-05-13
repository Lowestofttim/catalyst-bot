$ErrorActionPreference = "Continue"

$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonwCandidates = @(
    $env:CATALYST_PYTHONW,
    (Join-Path $repo ".venv\Scripts\pythonw.exe"),
    "C:\Python312\pythonw.exe",
    (Get-Command pythonw.exe -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Source)
) | Where-Object { $_ }
$pythonw = $pythonwCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
$app = Join-Path $repo "desktop_app.py"
$logDir = Join-Path $env:APPDATA "Catalyst"
$log = Join-Path $logDir "launcher.log"
$expectedRemotes = @(
    "https://github.com/catalystxch/catalyst-bot.git",
    "git@github.com:catalystxch/catalyst-bot.git"
)

function Write-LaunchLog {
    param([string]$Message)
    try {
        New-Item -ItemType Directory -Force -Path $logDir | Out-Null
        $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
        Add-Content -Path $log -Value "[$stamp] $Message"
    } catch {
    }
}

function Test-LocalPortOpen {
    param([int]$Port)
    $client = $null
    try {
        $client = New-Object Net.Sockets.TcpClient
        $async = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne(200)) {
            return $false
        }
        $client.EndConnect($async)
        return $true
    } catch {
        return $false
    } finally {
        if ($client) {
            $client.Close()
        }
    }
}

function Try-FastForwardMain {
    if (Test-LocalPortOpen -Port 5000) {
        Write-LaunchLog "Catalyst already running; skipped git update before launch."
        return
    }
    if (-not (Test-Path $repo)) {
        Write-LaunchLog "Repo not found at $repo; skipped git update."
        return
    }

    $git = Get-Command git.exe -ErrorAction SilentlyContinue
    if (-not $git) {
        Write-LaunchLog "git.exe not found; skipped git update."
        return
    }

    try {
        $remoteName = "github"
        $remote = (& git -C $repo remote get-url $remoteName 2>$null)
        if (-not $remote) {
            $remoteName = "origin"
            $remote = (& git -C $repo remote get-url $remoteName 2>$null)
        }
        if ($expectedRemotes -notcontains $remote) {
            Write-LaunchLog "Remote '$remote' was not an expected Catalyst repo; skipped git update."
            return
        }

        $branch = (& git -C $repo branch --show-current 2>$null).Trim()
        if ($branch -ne "main") {
            Write-LaunchLog "Current branch is '$branch', not main; skipped git update."
            return
        }

        & git -C $repo diff --quiet -- 2>$null
        $dirtyWorktree = $LASTEXITCODE -ne 0
        & git -C $repo diff --cached --quiet -- 2>$null
        $dirtyIndex = $LASTEXITCODE -ne 0
        if ($dirtyWorktree -or $dirtyIndex) {
            Write-LaunchLog "Tracked changes present; skipped git update."
            return
        }

        & git -C $repo fetch $remoteName main --tags --prune 2>&1 | ForEach-Object {
            Write-LaunchLog "git fetch: $_"
        }
        if ($LASTEXITCODE -ne 0) {
            Write-LaunchLog "git fetch failed with exit $LASTEXITCODE."
            return
        }

        $local = (& git -C $repo rev-parse main 2>$null).Trim()
        $remoteBranch = "$remoteName/main"
        $remoteHead = (& git -C $repo rev-parse $remoteBranch 2>$null).Trim()
        $base = (& git -C $repo merge-base main $remoteBranch 2>$null).Trim()

        if ($local -eq $remoteHead) {
            Write-LaunchLog "main already up to date at $local."
            return
        }
        if ($base -ne $local) {
            Write-LaunchLog "main is not a clean fast-forward; skipped git update."
            return
        }

        & git -C $repo pull --ff-only $remoteName main 2>&1 | ForEach-Object {
            Write-LaunchLog "git pull: $_"
        }
        Write-LaunchLog "Updated main from $local to $remoteHead."
    } catch {
        Write-LaunchLog "git update skipped after error: $($_.Exception.Message)"
    }
}

Try-FastForwardMain

if (-not $pythonw) {
    Write-LaunchLog "pythonw.exe not found."
    exit 1
}
if (-not (Test-Path $app)) {
    Write-LaunchLog "desktop_app.py not found at $app."
    exit 1
}

Write-LaunchLog "Launching Catalyst from $app"
$scriptArg = '"{0}"' -f $app
Start-Process -FilePath $pythonw -ArgumentList @($scriptArg) -WorkingDirectory $repo -WindowStyle Hidden
