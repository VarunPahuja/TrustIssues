<#
.SYNOPSIS
    The ten-beat AAGP demo: earn trust, earn autonomy, lose it to a critical
    error, verify the audit trail -- driven entirely through the real HTTP
    API against a freshly reset Postgres database.

.DESCRIPTION
    Never assumes the database is clean: drops the Postgres volume, brings
    it back up, migrates, and reseeds before beat 1 runs. Starts its own
    backend (uvicorn) and stops it again on exit, success or failure, so two
    consecutive runs never fight over a port or stale data.

    Pauses for a keypress between beats so a presenter can talk over each
    one. Every read that follows a write for the same id goes through a
    short bounded retry (Invoke-ApiWithRetry) -- a known read-your-writes gap
    (roughly 1 in 200-800) can otherwise show a 404 in front of a panel.

    Any step that doesn't return what's expected stops the script immediately
    with a clear message, rather than continuing into a broken demo.

.PARAMETER NoPause
    Skip the "press Enter to continue" waits between beats. For a dry run,
    CI, or re-verifying the script still works end to end without standing
    over the keyboard.

.PARAMETER WithRecovery
    Also run the optional beat 10b: enough clean decisions to clear both
    post-clawback cooldowns and earn a second, real INCREASE. Off by
    default -- the 8 Sept freeze audit measured this at ~105 sequential
    decisions and several minutes; too long to watch live. Without this
    flag, beat 10b just prints what it would show and how long it takes, so
    the presenter can narrate it instead of running it.

.PARAMETER Port
    Local port for the backend this script starts. Default 8099 -- picked
    to avoid colliding with a developer's own `uvicorn` on 8000.

.EXAMPLE
    .\scripts\demo.ps1
    Run the live ten-beat demo, pausing for the presenter between beats.

.EXAMPLE
    .\scripts\demo.ps1 -NoPause -WithRecovery
    Run everything unattended, including the recovery section -- for
    verifying the script itself, not for presenting live.
#>

param(
    [switch]$NoPause,
    [switch]$WithRecovery,
    [int]$Port = 8099
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$BaseUrl = "http://127.0.0.1:$Port/api/v1"
$DatabaseUrl = "postgresql://aagp:aagp_dev_password@localhost:5432/aagp"
$Headers = @{ "X-User-Role" = "admin" }

$script:BackendProcess = $null
$script:StartTime = Get-Date
$script:RecoveryElapsed = $null

# --- output helpers ---------------------------------------------------------

function Write-Caption {
    param([string]$Beat, [string]$Text)
    Write-Host ""
    Write-Host "-- $Beat ------------------------------------------" -ForegroundColor Cyan
    Write-Host $Text -ForegroundColor Cyan
}

function Write-Result {
    param([string]$Text)
    Write-Host "  $Text" -ForegroundColor Green
}

function Write-Note {
    param([string]$Text)
    Write-Host "  $Text" -ForegroundColor DarkGray
}

function Wait-ForPresenter {
    param([string]$Message = "Press Enter to continue")
    if ($NoPause) { return }
    Write-Host ""
    Read-Host $Message | Out-Null
}

function Stop-Backend {
    if ($script:BackendProcess -and -not $script:BackendProcess.HasExited) {
        Stop-Process -Id $script:BackendProcess.Id -Force -ErrorAction SilentlyContinue
    }
}

function Fail {
    param([string]$Message)
    Write-Host ""
    Write-Host "DEMO FAILED: $Message" -ForegroundColor Red
    Stop-Backend
    exit 1
}

# --- HTTP helpers ------------------------------------------------------------

function Invoke-JsonRequest {
    # Windows PowerShell 5.1's Invoke-RestMethod guesses ISO-8859-1 for a
    # JSON response whose Content-Type has no explicit charset (FastAPI's
    # default) — every non-ASCII byte (agent-01's name has an em dash) comes
    # back mangled. Invoke-WebRequest -UseBasicParsing exposes the untouched
    # response bytes via RawContentStream; decoding those as UTF-8 ourselves,
    # before ConvertFrom-Json ever sees the string, sidesteps the guess
    # entirely.
    param(
        [Parameter(Mandatory)][string]$Method,
        [Parameter(Mandatory)][string]$Uri,
        $Body = $null
    )
    if ($null -ne $Body) {
        $json = $Body | ConvertTo-Json -Depth 10
        $response = Invoke-WebRequest -Method $Method -Uri $Uri -Headers $Headers -ContentType "application/json" -Body $json -UseBasicParsing
    } else {
        $response = Invoke-WebRequest -Method $Method -Uri $Uri -Headers $Headers -UseBasicParsing
    }
    $bytes = $response.RawContentStream.ToArray()
    $text = [System.Text.Encoding]::UTF8.GetString($bytes)
    if ([string]::IsNullOrWhiteSpace($text)) { return $null }
    return $text | ConvertFrom-Json
}

function Invoke-Api {
    # A plain call: POST/GET against a resource that either doesn't need a
    # read-after-write guard (the response body itself is the read), or is
    # the very first touch of an id nothing else could have raced on.
    param(
        [Parameter(Mandatory)][string]$Method,
        [Parameter(Mandatory)][string]$Path,
        $Body = $null
    )
    $uri = "$BaseUrl$Path"
    try {
        return Invoke-JsonRequest -Method $Method -Uri $uri -Body $Body
    } catch {
        $status = $null
        try { $status = [int]$_.Exception.Response.StatusCode } catch {}
        $detail = $_.ErrorDetails.Message
        Fail "HTTP $Method $Path returned status=$status`: $detail"
    }
}

function Invoke-ApiWithRetry {
    # For a GET that reads back something a previous write (possibly on a
    # different resource -- e.g. a policy version an approval just created)
    # just committed. Retries a 404 briefly; anything else fails immediately,
    # since a real error should never be mistaken for "not visible yet."
    param(
        [Parameter(Mandatory)][string]$Method,
        [Parameter(Mandatory)][string]$Path,
        [int]$MaxAttempts = 8,
        [int]$DelayMs = 200
    )
    $uri = "$BaseUrl$Path"
    for ($i = 1; $i -le $MaxAttempts; $i++) {
        try {
            return Invoke-JsonRequest -Method $Method -Uri $uri
        } catch {
            $status = $null
            try { $status = [int]$_.Exception.Response.StatusCode } catch {}
            if ($status -eq 404 -and $i -lt $MaxAttempts) {
                Start-Sleep -Milliseconds $DelayMs
                continue
            }
            $detail = $_.ErrorDetails.Message
            Fail "HTTP $Method $Path returned status=$status after $i attempt(s)`: $detail"
        }
    }
}

function Assert-Equal {
    param([string]$What, $Expected, $Actual)
    if ($Actual -ne $Expected) {
        Fail "$What`: expected $Expected, got $Actual"
    }
}

# --- environment reset --------------------------------------------------------

function Reset-Environment {
    Write-Caption "Reset" "Dropping the Postgres volume and rebuilding from scratch -- never assume the database is clean."
    Push-Location $RepoRoot
    try {
        # Via Start-Process, not a direct pipeline call: Windows PowerShell
        # treats a native command's stderr output as a terminating
        # NativeCommandError under $ErrorActionPreference = "Stop" the
        # moment its output is piped anywhere (even to Out-Null), and
        # docker compose's normal progress output goes to stderr.
        # Start-Process routes stderr as a plain OS stream instead, so it
        # never touches PowerShell's error mechanism.
        $downProc = Start-Process -FilePath "docker" -ArgumentList @("compose", "down", "-v", "db") -Wait -PassThru -NoNewWindow
        if ($downProc.ExitCode -ne 0) { Fail "docker compose down -v db failed (exit $($downProc.ExitCode))." }
        $upProc = Start-Process -FilePath "docker" -ArgumentList @("compose", "up", "-d", "--wait", "db") -Wait -PassThru -NoNewWindow
        if ($upProc.ExitCode -ne 0) { Fail "docker compose up -d --wait db failed (exit $($upProc.ExitCode))." }
        Write-Result "Postgres volume dropped and recreated."

        $env:DATABASE_URL = $DatabaseUrl
        $env:PYTHONPATH = $RepoRoot

        # Start-Process again, for the same reason as the docker calls above:
        # alembic and this seed script both log to stderr on a normal,
        # successful run, and `& $VenvPython ...` directly would turn that
        # into a terminating NativeCommandError under $ErrorActionPreference
        # = "Stop".
        $alembicProc = Start-Process -FilePath $VenvPython -ArgumentList @("-m", "alembic", "-c", "backend/alembic.ini", "upgrade", "head") -Wait -PassThru -NoNewWindow
        if ($alembicProc.ExitCode -ne 0) { Fail "alembic upgrade head failed (exit $($alembicProc.ExitCode))." }
        Write-Result "Migrated to head."

        $seedProc = Start-Process -FilePath $VenvPython -ArgumentList @("-m", "backend.app.seed") -Wait -PassThru -NoNewWindow
        if ($seedProc.ExitCode -ne 0) { Fail "backend.app.seed failed (exit $($seedProc.ExitCode))." }
        Write-Result "Seeded."
    } finally {
        Pop-Location
    }
}

function Start-Backend {
    Write-Caption "Reset" "Starting the backend on port $Port."
    $env:DATABASE_URL = $DatabaseUrl
    $env:PYTHONPATH = $RepoRoot
    $logFile = Join-Path $env:TEMP "aagp-demo-backend-$Port.log"
    if (Test-Path $logFile) { Remove-Item $logFile -Force }

    $script:BackendProcess = Start-Process -FilePath $VenvPython `
        -ArgumentList @("-m", "uvicorn", "app.main:app", "--app-dir", "backend", "--port", "$Port") `
        -WorkingDirectory $RepoRoot `
        -RedirectStandardOutput $logFile -RedirectStandardError "$logFile.err" `
        -PassThru -WindowStyle Hidden

    $ready = $false
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Milliseconds 500
        try {
            Invoke-RestMethod -Uri "$BaseUrl/health" -TimeoutSec 2 | Out-Null
            $ready = $true
            break
        } catch { }
        if ($script:BackendProcess.HasExited) {
            Fail "Backend process exited during startup -- see $logFile / $logFile.err"
        }
    }
    if (-not $ready) { Fail "Backend did not become healthy on port $Port within 30s -- see $logFile / $logFile.err" }
    Write-Result "Backend healthy (PID $($script:BackendProcess.Id))."
}

# --- the ten beats -------------------------------------------------------------

function Invoke-Beat1 {
    Write-Caption "Beat 1" "Agent-01 starts this story mid-ladder, on real seed data, not at the floor -- show where it actually stands right now."
    $agent = Invoke-Api -Method GET -Path "/agents/agent-01"
    Write-Result "agent-01 '$($agent.name)': limit INR $($agent.current_limit) (rung $($agent.current_rung)), state $($agent.state)."
    return $agent
}

function Invoke-Beat2 {
    Write-Caption "Beat 2" "Run a simulation to build fresh evidence, then record human rulings on a handful of escalations -- beat 4's INCREASE needs both."

    $runBody = @{ phase = "good"; agent_id = "agent-01"; invoice_count = 120; seed = 1; reason = "demo: build evidence for a live-earned increase" }
    $run = Invoke-Api -Method POST -Path "/simulation/runs" -Body $runBody
    Write-Result "Started simulation run $($run.run_id) (phase=good, 120 invoices, seed=1)."

    $completed = $null
    for ($i = 0; $i -lt 120; $i++) {
        $status = Invoke-ApiWithRetry -Method GET -Path "/simulation/runs/$($run.run_id)"
        if ($status.status -eq "completed") { $completed = $status; break }
        if ($status.status -eq "failed") {
            Fail "Simulation run $($run.run_id) failed: $($status.error_message)"
        }
        Start-Sleep -Milliseconds 250
    }
    if (-not $completed) { Fail "Simulation run $($run.run_id) did not complete within 30s." }
    Write-Result ("Run complete: {0} decisions submitted, accuracy {1:P1}, Wilson lower bound {2:P1}." -f `
        $completed.decisions_submitted, $completed.accuracy, $completed.wilson_lower_bound)

    Write-Note "Recording human rulings on 6 escalations (MIN_RULED_ESCALATIONS_FOR_AGREEMENT=5 in trust/trust_engine/constants.py -- every escalation must be ruled, or the audit agent objects on the gap alone)."
    for ($i = 1; $i -le 6; $i++) {
        $invoiceId = "demo-escalation-$i"
        $decisionBody = @{
            invoice_id = $invoiceId
            amount = 750
            action = "ESCALATE"
            ground_truth = "APPROVE"
            agent_id = "agent-01"
            recommended_action = "APPROVE"
            reason = "ambiguous vendor, escalating for human review"
        }
        $decision = Invoke-Api -Method POST -Path "/decisions" -Body $decisionBody
        $rulingBody = @{ ruling = "APPROVE"; reason = "reviewed the invoice; agent's recommendation was correct" }
        Invoke-Api -Method POST -Path "/decisions/$($decision.decision_id)/ruling" -Body $rulingBody | Out-Null
    }
    Write-Result "6 escalations submitted and ruled (all agreed)."
}

function Invoke-Beat3 {
    Write-Caption "Beat 3" "The trust evaluation: this is the intellectual core. The headline number is never the raw point estimate -- it's the Wilson lower bound."
    $trust = Invoke-ApiWithRetry -Method GET -Path "/agents/agent-01/trust"
    $acc = $trust.accuracy
    Write-Result ("Accuracy point estimate: {0:P1} (n={1})" -f $acc.point, $acc.trials)
    Write-Result ("Wilson lower bound:      {0:P1}  <-- the number the ladder actually trusts" -f $acc.wilson_lower)
    Write-Note ("Gap: {0:N1} percentage points of statistical caution baked into every decision this system makes." -f (($acc.point - $acc.wilson_lower) * 100))
    Write-Result ("Trust score: {0:N1} (needs >= 70.0 for an increase to be eligible)" -f $trust.trust_score)
    return $trust
}

function Invoke-Beat4 {
    Write-Caption "Beat 4" "Generate a recommendation. This is the demo's highest-stakes moment: a live-earned INCREASE, no seed data, four independent opinions."
    $rec = Invoke-Api -Method POST -Path "/agents/agent-01/recommendations"

    Write-Result "direction=$($rec.direction)  status=$($rec.status)  has_dissent=$($rec.has_dissent)  clamped=$($rec.clamped)"
    foreach ($op in $rec.opinions) {
        Write-Note ("[{0,-11}] {1}" -f $op.agent_name, $op.verdict)
    }
    if ($rec.clamped) {
        Write-Note "Clamped from $($rec.clamped_from) to $($rec.proposed_limit) -- governance's ask exceeded what the evidence supports."
    }

    # The whole point of this beat. If this isn't true, the demo is broken
    # at its most important moment -- fail loudly rather than limp forward.
    Assert-Equal -What "beat 4 direction" -Expected "INCREASE" -Actual $rec.direction
    Assert-Equal -What "beat 4 has_dissent" -Expected $false -Actual $rec.has_dissent

    return $rec
}

function Invoke-Beat5 {
    param($Recommendation)
    Write-Caption "Beat 5" "A human approves the recommendation -- the one step ADR-0004 requires before autonomy can go up."
    $body = @{ reason = "evidence reviewed, all four agents concur, approving the increase" }
    $approved = Invoke-Api -Method POST -Path "/recommendations/$($Recommendation.recommendation_id)/approve" -Body $body
    Assert-Equal -What "approval status" -Expected "APPROVED" -Actual $approved.status
    Write-Result "Approved. status=$($approved.status)"
    return $approved
}

function Invoke-Beat6 {
    Write-Caption "Beat 6" "Show the limit actually moved, and the new policy version chained to the one it replaced."
    $agent = Invoke-ApiWithRetry -Method GET -Path "/agents/agent-01"
    Write-Result "agent-01 now: limit INR $($agent.current_limit) (rung $($agent.current_rung))."

    $versions = Invoke-ApiWithRetry -Method GET -Path "/agents/agent-01/policy-versions?page=1&page_size=2"
    $latest = $versions.items[0]
    $previous = $versions.items[1]
    Write-Result "Latest policy version $($latest.id): limit $($latest.limit), rung $($latest.rung), created_by=$($latest.created_by)"
    Write-Note "  previous_version_id=$($latest.previous_version_id) -> matches $($previous.id): $($latest.previous_version_id -eq $previous.id)"

    if ($latest.previous_version_id -ne $previous.id) {
        Fail "Policy version chain broken: latest.previous_version_id ($($latest.previous_version_id)) does not match the prior version ($($previous.id))."
    }
    return $agent
}

function Invoke-Beat7 {
    Write-Caption "Beat 7" "Inject a critical error: an APPROVE the ground truth says should have been a REJECT -- real money that should not have gone out."
    $body = @{
        invoice_id = "demo-critical-error-1"
        amount = 900
        action = "APPROVE"
        ground_truth = "REJECT"
        agent_id = "agent-01"
        reason = "demo: inject a critical error to trigger drift detection"
    }
    $decision = Invoke-Api -Method POST -Path "/decisions" -Body $body
    Write-Result "Decision $($decision.decision_id) recorded: action=$($decision.action), ground_truth=$($decision.ground_truth)."
    return $decision
}

function Invoke-Beat8 {
    Write-Caption "Beat 8" "Show drift detection catch it -- a single critical error is enough for an immediate CRITICAL severity, no waiting for a statistical trend."
    $trust = Invoke-ApiWithRetry -Method GET -Path "/agents/agent-01/trust"
    $drift = $trust.drift
    Write-Result "drift.severity=$($drift.severity)  critical_errors_in_window=$($drift.critical_errors_in_window)"
    Write-Result ("recent_accuracy={0:P1}  baseline_accuracy={1:P1}" -f $drift.recent_accuracy, $drift.baseline_accuracy)
    Assert-Equal -What "beat 8 drift severity" -Expected "CRITICAL" -Actual $drift.severity
    return $trust
}

function Invoke-Beat9 {
    Write-Caption "Beat 9" "Generate a recommendation again. Watch: CLAWBACK applies immediately, with NO approval call -- ADR-0004's asymmetry made visible."
    $before = Invoke-ApiWithRetry -Method GET -Path "/agents/agent-01"
    Write-Result "Limit BEFORE: INR $($before.current_limit) (rung $($before.current_rung))"

    $rec = Invoke-Api -Method POST -Path "/agents/agent-01/recommendations"
    Write-Result "direction=$($rec.direction)  status=$($rec.status)  (status is already APPROVED -- nobody called /approve)"
    Assert-Equal -What "beat 9 direction" -Expected "CLAWBACK" -Actual $rec.direction
    Assert-Equal -What "beat 9 status" -Expected "APPROVED" -Actual $rec.status

    $after = Invoke-ApiWithRetry -Method GET -Path "/agents/agent-01"
    Write-Result "Limit AFTER:  INR $($after.current_limit) (rung $($after.current_rung))"

    if ($after.current_limit -ge $before.current_limit) {
        Fail "Clawback didn't reduce the limit: before=$($before.current_limit) after=$($after.current_limit)"
    }
    return @{ Before = $before; After = $after; Recommendation = $rec }
}

function Invoke-Beat10 {
    Write-Caption "Beat 10" "Verify the audit chain -- every entry hash-linked to the one before it, recomputed fresh, not just asserted."
    $log = Invoke-ApiWithRetry -Method GET -Path "/audit-log?page=1&page_size=100"
    Write-Result "chain_valid=$($log.chain_valid)  chain_verified_scope=$($log.chain_verified_scope)  total_entries=$($log.total)"
    Assert-Equal -What "audit chain validity" -Expected $true -Actual $log.chain_valid
    return $log
}

function Invoke-Beat10Recovery {
    Write-Caption "Beat 10b (optional)" "Recovery: earn a second real INCREASE after the clawback."
    Write-Note "This needs ~105 clean decisions to clear both CLEAN_DECISIONS_AFTER_CLAWBACK=75 and COOLDOWN_BETWEEN_INCREASES=100 (trust/trust_engine/constants.py) -- the 8 Sept freeze audit measured this at several real minutes."

    if (-not $WithRecovery) {
        Write-Note "Skipped by default (-WithRecovery not passed). Narrate this instead of running it live."
        return
    }

    $recoveryStart = Get-Date
    $runBody = @{ phase = "good"; agent_id = "agent-01"; invoice_count = 105; seed = 100; reason = "demo: recovery, clear both post-clawback cooldowns" }
    $run = Invoke-Api -Method POST -Path "/simulation/runs" -Body $runBody
    Write-Result "Started recovery simulation run $($run.run_id) (105 invoices)."

    $completed = $null
    for ($i = 0; $i -lt 240; $i++) {
        $status = Invoke-ApiWithRetry -Method GET -Path "/simulation/runs/$($run.run_id)"
        if ($status.status -eq "completed") { $completed = $status; break }
        if ($status.status -eq "failed") { Fail "Recovery simulation run failed: $($status.error_message)" }
        Start-Sleep -Milliseconds 500
    }
    if (-not $completed) { Fail "Recovery simulation run did not complete within 120s." }
    Write-Result "Recovery run complete: $($completed.decisions_submitted) decisions submitted."

    $rec = Invoke-Api -Method POST -Path "/agents/agent-01/recommendations"
    Write-Result "direction=$($rec.direction)  status=$($rec.status)"
    Assert-Equal -What "beat 10b direction" -Expected "INCREASE" -Actual $rec.direction

    $approveBody = @{ reason = "recovery evidence cleared both cooldowns; approving the second increase" }
    $approved = Invoke-Api -Method POST -Path "/recommendations/$($rec.recommendation_id)/approve" -Body $approveBody
    Assert-Equal -What "beat 10b approval status" -Expected "APPROVED" -Actual $approved.status

    $agent = Invoke-ApiWithRetry -Method GET -Path "/agents/agent-01"
    $script:RecoveryElapsed = (Get-Date) - $recoveryStart
    Write-Result ("Recovered to limit INR {0} (rung {1}) in {2:N1}s." -f $agent.current_limit, $agent.current_rung, $script:RecoveryElapsed.TotalSeconds)
}

# --- main --------------------------------------------------------------------

try {
    Reset-Environment
    Start-Backend

    Invoke-Beat1 | Out-Null
    Wait-ForPresenter

    Invoke-Beat2
    Wait-ForPresenter

    Invoke-Beat3 | Out-Null
    Wait-ForPresenter

    $rec4 = Invoke-Beat4
    Wait-ForPresenter

    Invoke-Beat5 -Recommendation $rec4 | Out-Null
    Wait-ForPresenter

    Invoke-Beat6 | Out-Null
    Wait-ForPresenter

    Invoke-Beat7 | Out-Null
    Wait-ForPresenter

    Invoke-Beat8 | Out-Null
    Wait-ForPresenter

    Invoke-Beat9 | Out-Null
    Wait-ForPresenter

    Invoke-Beat10 | Out-Null
    Wait-ForPresenter

    Invoke-Beat10Recovery

    $elapsed = (Get-Date) - $script:StartTime
    Write-Host ""
    Write-Host "-- Demo complete in $([math]::Round($elapsed.TotalSeconds, 1))s ----------------------" -ForegroundColor Cyan
} finally {
    Stop-Backend
}
