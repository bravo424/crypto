# auto_commit.ps1 — Watch the repo and auto-commit+push changes.
#
# Usage:
#   .\auto_commit.ps1              # watches, commits, and pushes
#   .\auto_commit.ps1 -NoPush      # watches and commits only (no push)
#   .\auto_commit.ps1 -Debounce 10 # wait 10 s after last change before committing
#
# Press Ctrl+C to stop.

param(
    [switch]$NoPush,
    [int]$Debounce = 5   # seconds to wait after last change before committing
)

$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $RepoRoot

Write-Host "Auto-commit watcher started in: $RepoRoot"
Write-Host "Debounce: ${Debounce}s  |  Push: $(-not $NoPush)"
Write-Host "Press Ctrl+C to stop.`n"

$watcher = New-Object System.IO.FileSystemWatcher
$watcher.Path            = $RepoRoot
$watcher.IncludeSubdirectories = $true
$watcher.NotifyFilter    = [System.IO.NotifyFilters]'FileName,DirectoryName,LastWrite'
$watcher.EnableRaisingEvents = $true

# Directories to ignore (supplement .gitignore)
$ignoreDirs = @('.git', '__pycache__', '.venv', 'data', 'config')

$lastChange = [datetime]::MinValue
$pendingChange = $false

$onChange = {
    $path = $Event.SourceEventArgs.FullPath
    foreach ($dir in $ignoreDirs) {
        if ($path -match [regex]::Escape("\$dir\") -or $path -match [regex]::Escape("\$dir" + '$')) {
            return
        }
    }
    $script:lastChange    = [datetime]::UtcNow
    $script:pendingChange = $true
}

Register-ObjectEvent $watcher Changed -Action $onChange | Out-Null
Register-ObjectEvent $watcher Created -Action $onChange | Out-Null
Register-ObjectEvent $watcher Deleted -Action $onChange | Out-Null
Register-ObjectEvent $watcher Renamed -Action $onChange | Out-Null

try {
    while ($true) {
        Start-Sleep -Milliseconds 500

        if ($pendingChange) {
            $elapsed = ([datetime]::UtcNow - $lastChange).TotalSeconds
            if ($elapsed -ge $Debounce) {
                $pendingChange = $false

                # Check if there is anything to commit
                $status = git -C $RepoRoot status --porcelain 2>&1
                if ($status) {
                    $timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
                    $message   = "auto: $timestamp"
                    Write-Host "[$timestamp] Changes detected — committing..."

                    git -C $RepoRoot add -A
                    git -C $RepoRoot commit -m $message

                    if (-not $NoPush) {
                        git -C $RepoRoot push origin master
                        Write-Host "  Pushed to origin/master`n"
                    } else {
                        Write-Host "  Committed (push skipped)`n"
                    }
                }
            }
        }
    }
} finally {
    $watcher.EnableRaisingEvents = $false
    $watcher.Dispose()
    Get-EventSubscriber | Unregister-Event
    Write-Host "`nAuto-commit watcher stopped."
}
