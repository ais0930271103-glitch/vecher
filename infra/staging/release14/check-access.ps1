#requires -Version 5.1
# Diagnose access only. Does not invoke a release, fetch, checkout or migration.
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$VecherDiagnosticOrigin = 'https://api-test.xn--b1aga8b7a.online'

function Invoke-VecherDiagnosticProcess([string]$Path, [string]$Arguments, [int]$TimeoutMs = 20000) {
    $VecherInfo = New-Object System.Diagnostics.ProcessStartInfo
    $VecherInfo.FileName = $Path
    $VecherInfo.Arguments = $Arguments
    $VecherInfo.UseShellExecute = $false
    $VecherInfo.CreateNoWindow = $true
    $VecherInfo.RedirectStandardOutput = $true
    $VecherInfo.RedirectStandardError = $true
    $VecherProcess = New-Object System.Diagnostics.Process
    $VecherProcess.StartInfo = $VecherInfo
    try {
        $null = $VecherProcess.Start()
        $VecherOutputTask = $VecherProcess.StandardOutput.ReadToEndAsync()
        $VecherErrorTask = $VecherProcess.StandardError.ReadToEndAsync()
        $VecherTimedOut = -not $VecherProcess.WaitForExit($TimeoutMs)
        if ($VecherTimedOut) { $VecherProcess.Kill(); $VecherProcess.WaitForExit() }
        $VecherOutput = $VecherOutputTask.Result
        $null = $VecherErrorTask.Result
        if ($VecherOutput.Length -gt 65536) { throw 'Diagnostic output exceeded its limit.' }
        return @{ ExitCode = $VecherProcess.ExitCode; Stdout = $VecherOutput; TimedOut = $VecherTimedOut }
    } finally {
        $VecherProcess.Dispose()
    }
}

function Show-VecherWindowsHttps {
    $VecherResult = [ordered]@{ reachable = $false; contract_ok = $false; api_version = $null; error_category = $null; http_status = $null }
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        $VecherHealth = Invoke-RestMethod -Method Get -Uri ($script:VecherDiagnosticOrigin + '/health') -TimeoutSec 12
        $VecherResult.reachable = $true
        $VecherResult.contract_ok = ($VecherHealth.ok -eq $true -and $VecherHealth.env -ceq 'staging')
        if ([string]$VecherHealth.version -cmatch '^[0-9]+\.[0-9]+\.[0-9]+$') { $VecherResult.api_version = [string]$VecherHealth.version }
    } catch {
        $VecherException = $_.Exception
        for ($VecherDepth = 0; $VecherDepth -lt 8 -and $null -ne $VecherException; $VecherDepth++) {
            if ($VecherException -is [Net.WebException]) {
                $VecherResult.error_category = [string]$VecherException.Status
                if ($null -ne $VecherException.Response -and $VecherException.Response -is [Net.HttpWebResponse]) {
                    $VecherResult.http_status = [int]$VecherException.Response.StatusCode
                }
                break
            }
            $VecherException = $VecherException.InnerException
        }
        if ($null -eq $VecherResult.error_category) { $VecherResult.error_category = 'RequestOrResponseFailure' }
    }
    Write-Output ('WINDOWS_POWERSHELL_HTTPS=' + ($VecherResult | ConvertTo-Json -Compress))
}

function Show-VecherWindowsCurl {
    $VecherResult = [ordered]@{ available = $false; exit_code = $null; timed_out = $false; metrics = @{}; error_category = $null }
    $VecherCurl = Join-Path $env:WINDIR 'System32\curl.exe'
    if (-not (Test-Path -LiteralPath $VecherCurl -PathType Leaf)) {
        $VecherResult.error_category = 'CurlNotFound'
        Write-Output ('WINDOWS_CURL_HTTPS=' + ($VecherResult | ConvertTo-Json -Depth 3 -Compress))
        return
    }
    $VecherResult.available = $true
    $VecherFormat = 'http_code=%{http_code};dns=%{time_namelookup};connect=%{time_connect};tls=%{time_appconnect};first_byte=%{time_starttransfer};total=%{time_total};tls_verify=%{ssl_verify_result}'
    $VecherArgs = '-q --silent --show-error --connect-timeout 8 --max-time 15 --output NUL --write-out "' + $VecherFormat + '" "' + $script:VecherDiagnosticOrigin + '/health"'
    try {
        $VecherCapture = Invoke-VecherDiagnosticProcess $VecherCurl $VecherArgs 20000
        $VecherResult.exit_code = $VecherCapture.ExitCode
        $VecherResult.timed_out = $VecherCapture.TimedOut
        foreach ($VecherItem in $VecherCapture.Stdout.Trim().Split(';')) {
            if ($VecherItem -cmatch '^(http_code|dns|connect|tls|first_byte|total|tls_verify)=([0-9.]+)$') {
                $VecherResult.metrics[$Matches[1]] = $Matches[2]
            }
        }
        if ($VecherCapture.TimedOut) { $VecherResult.error_category = 'Timeout' }
        elseif ($VecherCapture.ExitCode -ne 0) {
            $VecherResult.error_category = switch ($VecherCapture.ExitCode) {
                5 { 'ProxyNameResolution' }
                6 { 'NameResolution' }
                7 { 'ConnectFailure' }
                28 { 'Timeout' }
                35 { 'TlsHandshake' }
                60 { 'CertificateValidation' }
                default { 'CurlFailure' }
            }
        }
    } catch {
        $VecherResult.error_category = 'CurlExecutionFailure'
    }
    Write-Output ('WINDOWS_CURL_HTTPS=' + ($VecherResult | ConvertTo-Json -Depth 3 -Compress))
}

if ([string]::IsNullOrWhiteSpace($env:WINDIR)) { throw 'Run this diagnostic on the original Windows build PC.' }
$VecherSsh = Join-Path $env:WINDIR 'System32\OpenSSH\ssh.exe'
if (-not (Test-Path -LiteralPath $VecherSsh -PathType Leaf)) { throw 'Windows OpenSSH was not found.' }
$VecherConfig = Invoke-VecherDiagnosticProcess $VecherSsh '-G vecher' 15000
if ($VecherConfig.ExitCode -ne 0 -or $VecherConfig.TimedOut) { throw 'Could not read the existing vecher SSH alias.' }
$VecherTarget = @{}
foreach ($VecherLine in $VecherConfig.Stdout.Split("`n")) {
    if ($VecherLine.Trim() -cmatch '^(user|hostname|port)\s+(\S+)\s*$') {
        if ($VecherTarget.ContainsKey($Matches[1])) { throw 'Duplicate SSH target field.' }
        $VecherTarget[$Matches[1]] = $Matches[2]
    }
}
if ($VecherTarget['user'] -cne 'root' -or $VecherTarget['hostname'] -cne '94.228.166.214' -or $VecherTarget['port'] -cne '22') {
    throw 'SSH alias vecher must still resolve to root@94.228.166.214:22. Configuration was not changed.'
}

$VecherRemotePython = [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'diagnose_access.py'), [Text.Encoding]::UTF8)
Write-Output 'VECHER_ACCESS_CHECK_START'
Show-VecherWindowsHttps
Show-VecherWindowsCurl
Write-Output 'SERVER_DIAGNOSTIC_VIA_EXISTING_SSH'
$VecherRemotePython | & $VecherSsh '-T' '-o' 'ConnectTimeout=20' '-o' 'ServerAliveInterval=15' '-o' 'ServerAliveCountMax=4' 'vecher' 'python3' '-'
$VecherSshExit = $LASTEXITCODE
Write-Output ('SSH_DIAGNOSTIC_EXIT=' + $VecherSshExit)
if ($VecherSshExit -ne 0) { throw 'The server diagnostic did not complete. Keep the diagnostic output above.' }
Write-Output 'VECHER_ACCESS_CHECK_FINISHED'
