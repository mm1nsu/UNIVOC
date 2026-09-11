$ErrorActionPreference = 'Continue'
$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
$log = Join-Path $env:TEMP 'univoc-auto-commit.log'
$lock = Join-Path $env:TEMP 'univoc-auto-commit.lock'

if (Test-Path $lock) { exit 0 }
Set-Content -LiteralPath $lock -Value $PID

function Write-Log([string]$message) {
    Add-Content -LiteralPath $log -Value ("{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $message)
}

$watcher = New-Object IO.FileSystemWatcher
$watcher.Path = $repo
$watcher.IncludeSubdirectories = $true
$watcher.NotifyFilter = [IO.NotifyFilters]::FileName -bor [IO.NotifyFilters]::DirectoryName -bor [IO.NotifyFilters]::LastWrite -bor [IO.NotifyFilters]::Size
$watcher.EnableRaisingEvents = $true
$events = @(
    Register-ObjectEvent $watcher Changed -SourceIdentifier 'UNIVOC.Changed'
    Register-ObjectEvent $watcher Created -SourceIdentifier 'UNIVOC.Created'
    Register-ObjectEvent $watcher Deleted -SourceIdentifier 'UNIVOC.Deleted'
    Register-ObjectEvent $watcher Renamed -SourceIdentifier 'UNIVOC.Renamed'
)
$pending = $false
$lastChange = Get-Date
Write-Log 'watcher started'

try {
    while ($true) {
        $event = Wait-Event -Timeout 1
        if ($null -ne $event) {
            $relative = $event.SourceEventArgs.FullPath.Substring($repo.Length).TrimStart('\')
            if ($relative -notlike '.git*') {
                $pending = $true
                $lastChange = Get-Date
            }
            Remove-Event -EventIdentifier $event.EventIdentifier -ErrorAction SilentlyContinue
        }

        if ($pending -and ((Get-Date) - $lastChange).TotalSeconds -ge 5) {
            $pending = $false
            $status = (& git -C $repo status --porcelain 2>&1 | Out-String).Trim()
            if ($status) {
                & git -C $repo add -A
                $message = 'Auto-commit: ' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
                & git -C $repo commit -m $message 2>&1 | Out-Null
                if ($LASTEXITCODE -eq 0) {
                    & git -C $repo push 2>&1 | Out-Null
                    if ($LASTEXITCODE -eq 0) { Write-Log ("committed and pushed: " + $message) }
                    else { Write-Log ("committed but push failed: " + $message) }
                }
            }
        }
    }
}
finally {
    $events | ForEach-Object { Unregister-Event -SubscriptionId $_.Id -ErrorAction SilentlyContinue }
    $watcher.Dispose()
    Remove-Item -LiteralPath $lock -Force -ErrorAction SilentlyContinue
    Write-Log 'watcher stopped'
}
