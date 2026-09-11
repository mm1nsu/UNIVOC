$ErrorActionPreference = 'Continue'
$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
$log = Join-Path $env:TEMP 'univoc-auto-commit.log'
$lock = Join-Path $env:TEMP 'univoc-auto-commit.lock'

if (Test-Path $lock) { exit 0 }
Set-Content -LiteralPath $lock -Value $PID

function Write-Log([string]$message) {
    Add-Content -LiteralPath $log -Value ("{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $message)
}

function Get-WorkingTreeStatus {
    return ((& git -C $repo status --porcelain --untracked-files=all 2>&1 | Out-String).Trim())
}

$previousStatus = Get-WorkingTreeStatus
$changedAt = $null
Write-Log 'polling watcher started'

try {
    while ($true) {
        Start-Sleep -Seconds 2
        $currentStatus = Get-WorkingTreeStatus
        if ($currentStatus -ne $previousStatus) {
            $previousStatus = $currentStatus
            if ($currentStatus) {
                $changedAt = Get-Date
                Write-Log 'changes detected'
            } else {
                $changedAt = $null
            }
        }

        if ($changedAt -and ((Get-Date) - $changedAt).TotalSeconds -ge 5) {
            $statusBeforeCommit = Get-WorkingTreeStatus
            if ($statusBeforeCommit) {
                & git -C $repo add -A
                $message = 'Auto-commit: ' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
                & git -C $repo commit -m $message 2>&1 | Out-Null
                if ($LASTEXITCODE -eq 0) {
                    & git -C $repo push 2>&1 | Out-Null
                    if ($LASTEXITCODE -eq 0) { Write-Log ("committed and pushed: " + $message) }
                    else { Write-Log ("committed but push failed: " + $message) }
                } else {
                    Write-Log 'commit failed'
                }
            }
            $previousStatus = Get-WorkingTreeStatus
            $changedAt = $null
        }
    }
}
finally {
    Remove-Item -LiteralPath $lock -Force -ErrorAction SilentlyContinue
    Write-Log 'watcher stopped'
}
