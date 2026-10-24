#requires -Version 5.1
[CmdletBinding()]
param(
    [ValidateSet('Prepare', 'Server', 'Build', 'Accept', 'Publish', 'Status', 'DisableBlogs')]
    [string]$Mode = 'Prepare',
    [ValidateSet('Direct', 'Server')]
    [string]$HttpsCheck = 'Direct',
    [string]$RepoPath = 'C:\dev\vecher-update',
    [string]$FlutterPath = 'C:\dev\flutter\bin\flutter.bat',
    [string]$AndroidSdk = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$VecherSource = '40e43d85a2e434d4ee2090884a872a507f60fc93'
$VecherPackage = $PSScriptRoot
$VecherOrigin = 'https://api-test.xn--b1aga8b7a.online'
$VecherUtf8 = New-Object System.Text.UTF8Encoding($false, $true)
$VecherExpectedFileHashes = @{}
$VecherSshOptions = @('-T', '-n', '-o', 'ConnectTimeout=20', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=4')

function Get-VecherHash([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Assert-VecherPackage {
    $VecherRecordPath = Join-Path $script:VecherPackage 'release-record.json'
    if (-not (Test-Path -LiteralPath $VecherRecordPath -PathType Leaf)) {
        throw 'The complete release14 ZIP is required: release-record.json was not found.'
    }
    $VecherRecord = [IO.File]::ReadAllText($VecherRecordPath, $script:VecherUtf8) | ConvertFrom-Json
    if ($VecherRecord.format -ne 1 -or $VecherRecord.source.commit -cne $script:VecherSource -or
        $VecherRecord.target.version_code -ne 14 -or $VecherRecord.target.version_name -cne '0.9.0') {
        throw 'This release package does not describe the pinned 0.9.0 (14) source.'
    }
    $VecherRequired = @(
        'run-release14.ps1', 'release-apk14.ps1', 'vecher-publish-apk14.py',
        'server_release.py', 'media_rehearsal.py', 'diagnose_access.py', 'notes-0.9.0.txt'
    )
    $VecherSeen = @{}
    foreach ($VecherFile in $VecherRecord.files) {
        if ($VecherFile.path -isnot [string] -or $VecherFile.path -cnotmatch '^[A-Za-z0-9][A-Za-z0-9_.-]+$' -or
            $VecherFile.sha256 -isnot [string] -or $VecherFile.sha256 -cnotmatch '^[a-f0-9]{64}$' -or
            $VecherSeen.ContainsKey($VecherFile.path)) {
            throw 'The release package contains an invalid or duplicate file record.'
        }
        $VecherSeen[$VecherFile.path] = $true
        $script:VecherExpectedFileHashes[$VecherFile.path] = $VecherFile.sha256
        $VecherPath = Join-Path $script:VecherPackage $VecherFile.path
        if (-not (Test-Path -LiteralPath $VecherPath -PathType Leaf) -or
            (Get-VecherHash $VecherPath) -cne $VecherFile.sha256) {
            throw ('Release package file is missing or changed: ' + $VecherFile.path)
        }
    }
    foreach ($VecherName in $VecherRequired) {
        if (-not $VecherSeen.ContainsKey($VecherName)) { throw ('Release package does not bind ' + $VecherName) }
    }
    Write-Host 'RELEASE14_PACKAGE_HASHES_OK'
    if ($Mode -in @('Prepare', 'Server', 'Build', 'Accept', 'Publish')) {
        $VecherReadiness = $VecherRecord.PSObject.Properties['ready_for_prepare']
        if ($null -eq $VecherReadiness -or $VecherReadiness.Value -isnot [bool] -or $VecherReadiness.Value -ne $true) {
            throw 'This is a review snapshot. Release actions are blocked until the final rehearsal is confirmed. See RELEASE_STATUS.ru.txt. No server action was requested.'
        }
    }
}

function Assert-VecherVerifiedFile([string]$Name) {
    if (-not $script:VecherExpectedFileHashes.ContainsKey($Name) -or
        (Get-VecherHash (Join-Path $script:VecherPackage $Name)) -cne $script:VecherExpectedFileHashes[$Name]) {
        throw ('A verified release file changed: ' + $Name)
    }
}

function Set-VecherSsh {
    if ([string]::IsNullOrWhiteSpace($env:WINDIR)) { throw 'Use the original Windows build PC with its existing SSH configuration.' }
    $script:VecherSsh = Join-Path $env:WINDIR 'System32\OpenSSH\ssh.exe'
    $script:VecherScp = Join-Path $env:WINDIR 'System32\OpenSSH\scp.exe'
    foreach ($VecherProgram in @($script:VecherSsh, $script:VecherScp)) {
        if (-not (Test-Path -LiteralPath $VecherProgram -PathType Leaf)) { throw ('Windows OpenSSH was not found: ' + $VecherProgram) }
    }
    $VecherConfig = @(& $script:VecherSsh '-G' 'vecher')
    if ($LASTEXITCODE -ne 0) { throw 'Could not resolve the existing vecher SSH alias.' }
    $VecherTarget = @{}
    foreach ($VecherLine in $VecherConfig) {
        if ($VecherLine -cmatch '^(user|hostname|port)\s+(\S+)\s*$') {
            if ($VecherTarget.ContainsKey($Matches[1])) { throw 'Duplicate SSH target field.' }
            $VecherTarget[$Matches[1]] = $Matches[2]
        }
    }
    if ($VecherTarget['user'] -cne 'root' -or $VecherTarget['hostname'] -cne '94.228.166.214' -or $VecherTarget['port'] -cne '22') {
        throw 'SSH alias vecher must resolve to root@94.228.166.214:22. The script will not change SSH configuration.'
    }
    Write-Host 'SSH_TARGET_OK=root@94.228.166.214:22 (vecher)'
}

function Send-VecherServerTools {
    Set-VecherSsh
    $script:VecherRemoteStage = '/root/vecher-release14-tools/run-' + [Guid]::NewGuid().ToString('N')
    if ($script:VecherRemoteStage -cnotmatch '^/root/vecher-release14-tools/run-[a-f0-9]{32}$') { throw 'Invalid tool upload path.' }
    $VecherPrepare = @(
        'set -eu;'
        'test $(id -u) -eq 0;'
        'case $(hostname) in elegant-red|elegant-red.ptr.network) ;; *) exit 71 ;; esac;'
        'test -d /opt/vecher-test/repo;'
        'test ! -L /root/vecher-release14-tools;'
        'umask 077;'
        'mkdir -p -m 700 -- /root/vecher-release14-tools;'
        ('mkdir -m 700 -- ''{0}'';' -f $script:VecherRemoteStage)
        'printf ''%s\n'' ''RELEASE14_TOOLS_STAGE_READY'''
    ) -join ' '
    $VecherArgs = $script:VecherSshOptions + @('vecher', $VecherPrepare)
    & $script:VecherSsh @VecherArgs
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the private release14 tool stage. No deployment was requested.' }
    $VecherUploads = @('server_release.py', 'media_rehearsal.py', 'diagnose_access.py') | ForEach-Object {
        Assert-VecherVerifiedFile $_
        Join-Path $script:VecherPackage $_
    }
    $VecherCopyArgs = @('-S', $script:VecherSsh, '-o', 'ConnectTimeout=20', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=4', '--') +
        $VecherUploads + @('vecher:' + $script:VecherRemoteStage + '/')
    & $script:VecherScp @VecherCopyArgs
    if ($LASTEXITCODE -ne 0) { throw ('Tool upload failed. No deployment was requested. Stage: ' + $script:VecherRemoteStage) }
    Write-Host ('SERVER_TOOLS_STAGE=' + $script:VecherRemoteStage)
}

function Invoke-VecherServerAction([string]$Action) {
    if (@('preflight', 'deploy', 'status', 'enable-blogs', 'disable-blogs', 'public-status') -cnotcontains $Action) { throw 'Unknown server action.' }
    Assert-VecherVerifiedFile 'server_release.py'
    Assert-VecherVerifiedFile 'media_rehearsal.py'
    Assert-VecherVerifiedFile 'diagnose_access.py'
    $VecherServerHash = $script:VecherExpectedFileHashes['server_release.py']
    $VecherMediaHash = $script:VecherExpectedFileHashes['media_rehearsal.py']
    $VecherDiagnosticHash = $script:VecherExpectedFileHashes['diagnose_access.py']
    $VecherJsonArgument = if ($Action -ceq 'public-status') { ' --json' } else { '' }
    $VecherParts = @(
        'set -eu;'
        ('cd ''{0}'';' -f $script:VecherRemoteStage)
        ('printf ''%s  %s\n'' ''{0}'' ''server_release.py'' ''{1}'' ''media_rehearsal.py'' ''{2}'' ''diagnose_access.py'' | sha256sum -c -;' -f $VecherServerHash, $VecherMediaHash, $VecherDiagnosticHash)
        ('python3 ./server_release.py ''{0}''{1} </dev/null' -f $Action, $VecherJsonArgument)
    )
    if ($Action -ceq 'public-status') {
        return Read-VecherServerPublicStatus ($VecherParts -join ' ')
    }
    $VecherArgs = $script:VecherSshOptions + @('vecher', ($VecherParts -join ' '))
    # Stream progress during a potentially long backup/build/rehearsal. Keeping
    # stdout in an array would hide minutes of meaningful server output.
    & $script:VecherSsh @VecherArgs
    $VecherExit = $LASTEXITCODE
    if ($VecherExit -ne 0) {
        throw ('Server action ' + $Action + ' did not complete (exit ' + $VecherExit + '). Keep the output. Use -Mode Status before any retry; do not restore a database manually.')
    }
}

function Read-VecherServerPublicStatus([string]$Command) {
    # Only this short, read-only action is captured. Deployment progress stays
    # streamed above. The fixed command contains no embedded double quotes.
    if ($Command.Contains('"') -or $Command.Contains("`r") -or $Command.Contains("`n")) {
        throw 'Invalid server public-status command.'
    }
    $VecherInfo = New-Object System.Diagnostics.ProcessStartInfo
    $VecherInfo.FileName = $script:VecherSsh
    $VecherInfo.Arguments = ($script:VecherSshOptions -join ' ') + ' vecher "' + $Command + '"'
    $VecherInfo.UseShellExecute = $false
    # The existing SSH jump host asks for a password in the Windows console.
    # Inherit that console/stdin/stderr; CREATE_NO_WINDOW would remove CONIN$.
    $VecherInfo.CreateNoWindow = $false
    $VecherInfo.RedirectStandardInput = $false
    $VecherInfo.RedirectStandardOutput = $true
    $VecherInfo.RedirectStandardError = $false
    $VecherProcess = New-Object System.Diagnostics.Process
    $VecherProcess.StartInfo = $VecherInfo
    $VecherStarted = $false
    $VecherClock = [Diagnostics.Stopwatch]::StartNew()
    $VecherText = New-Object Text.StringBuilder
    try {
        $VecherStarted = $VecherProcess.Start()
        if (-not $VecherStarted) { throw 'Server public HTTPS process did not start.' }
        $VecherOutBuffer = New-Object char[] 2048
        $VecherOutTask = $VecherProcess.StandardOutput.ReadAsync($VecherOutBuffer, 0, $VecherOutBuffer.Length)
        $VecherOutDone = $false
        while (-not $VecherOutDone -or -not $VecherProcess.HasExited) {
            if ($VecherClock.ElapsedMilliseconds -gt 60000) { throw 'Server public HTTPS check timed out.' }
            if (-not $VecherOutDone -and $VecherOutTask.IsCompleted) {
                $VecherCount = $VecherOutTask.Result
                if ($VecherCount -eq 0) { $VecherOutDone = $true }
                else {
                    if ($VecherText.Length + $VecherCount -gt 16384) { throw 'Server public HTTPS output exceeded its limit.' }
                    $null = $VecherText.Append($VecherOutBuffer, 0, $VecherCount)
                    $VecherOutTask = $VecherProcess.StandardOutput.ReadAsync($VecherOutBuffer, 0, $VecherOutBuffer.Length)
                }
            }
            if (-not $VecherOutDone -or -not $VecherProcess.HasExited) {
                [Threading.Thread]::Sleep(10)
            }
        }
        if ($VecherProcess.ExitCode -ne 0) { throw 'Server public HTTPS check failed.' }
    } catch {
        throw 'Server public HTTPS was not verified. Its response was not printed; SSH console prompts and errors above were preserved.'
    } finally {
        $VecherClock.Stop()
        if ($VecherStarted -and -not $VecherProcess.HasExited) {
            $VecherProcess.Kill()
            $null = $VecherProcess.WaitForExit(2000)
        }
        $VecherProcess.Dispose()
    }
    $VecherLines = @($VecherText.ToString() -split '\r?\n' | Where-Object { $_ -cne '' })
    $VecherExpectedLines = @('server_release.py: OK', 'media_rehearsal.py: OK', 'diagnose_access.py: OK', 'RELEASE14_PUBLIC_HTTPS_OK')
    foreach ($VecherExpected in $VecherExpectedLines) {
        if (@($VecherLines | Where-Object { $_ -ceq $VecherExpected }).Count -ne 1) {
            throw 'Server public HTTPS proof is incomplete.'
        }
    }
    $VecherJsonLines = @($VecherLines | Where-Object { $VecherExpectedLines -cnotcontains $_ })
    if ($VecherJsonLines.Count -ne 1 -or -not $VecherJsonLines[0].StartsWith('{') -or -not $VecherJsonLines[0].EndsWith('}')) {
        throw 'Server public HTTPS response has an unexpected shape.'
    }
    try { $VecherPublic = ConvertFrom-Json -InputObject $VecherJsonLines[0] -ErrorAction Stop }
    catch { throw 'Server public HTTPS returned invalid JSON. Its contents were not printed.' }
    Assert-VecherPublicStatus $VecherPublic
    return $VecherPublic
}

function Get-VecherPublicJson([string]$Path) {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    try {
        return Invoke-RestMethod -Method Get -Uri ($script:VecherOrigin + $Path) -TimeoutSec 30 -MaximumRedirection 0
    } catch {
        throw 'Windows public HTTPS request failed. Run the separate access diagnostic; no response or exception details were printed.'
    }
}

function Assert-VecherPublicStatus($Public, [string]$ExpectedApiVersion = '') {
    $VecherFields = @('api_version', 'env', 'version_name', 'version_code', 'sha256', 'bytes', 'required')
    if ($null -eq $Public -or $Public -isnot [PSCustomObject] -or @($Public.PSObject.Properties).Count -ne $VecherFields.Count) {
        throw 'Public HTTPS returned an invalid release contract.'
    }
    foreach ($VecherField in $VecherFields) {
        if ($null -eq $Public.PSObject.Properties[$VecherField]) { throw 'Public HTTPS returned an incomplete release contract.' }
    }
    if ($Public.api_version -isnot [string] -or $Public.api_version -cnotmatch '^[0-9]+\.[0-9]+\.[0-9]+\z' -or
        $Public.env -cne 'staging' -or $Public.env -isnot [string] -or
        $Public.version_name -isnot [string] -or $Public.version_name -cnotmatch '^[0-9]+\.[0-9]+\.[0-9]+\z' -or
        ($Public.version_code -isnot [int] -and $Public.version_code -isnot [long]) -or $Public.version_code -lt 1 -or
        $Public.sha256 -isnot [string] -or $Public.sha256 -cnotmatch '^[0-9a-f]{64}\z' -or
        ($Public.bytes -isnot [int] -and $Public.bytes -isnot [long]) -or $Public.bytes -lt 2 -or $Public.bytes -gt 209715200 -or
        $Public.required -isnot [bool]) {
        throw 'Public HTTPS returned invalid release values.'
    }
    if ($ExpectedApiVersion -and $Public.api_version -cne $ExpectedApiVersion) {
        throw 'Public HTTPS is not serving the required API version. Keep the output and inspect Status before retrying.'
    }
}

function Get-VecherDirectPublicStatus {
    $VecherHealth = Get-VecherPublicJson '/health'
    $VecherVersion = Get-VecherPublicJson '/app/version'
    try {
        if ($VecherHealth -isnot [PSCustomObject] -or $VecherVersion -isnot [PSCustomObject] -or
            $VecherHealth.ok -isnot [bool] -or $VecherHealth.ok -ne $true) { throw 'Invalid health response.' }
        $VecherPublic = [PSCustomObject]@{
            api_version = $VecherHealth.version; env = $VecherHealth.env
            version_name = $VecherVersion.version_name; version_code = $VecherVersion.version_code
            sha256 = $VecherVersion.sha256; bytes = $VecherVersion.bytes; required = $VecherVersion.required
        }
        Assert-VecherPublicStatus $VecherPublic
        return $VecherPublic
    } catch {
        throw 'Windows public HTTPS returned an invalid health or release contract. Its contents were not printed.'
    }
}

function Show-VecherPublicStatus([string]$ExpectedApiVersion = '') {
    if ($HttpsCheck -eq 'Server') { $VecherPublic = Invoke-VecherServerAction 'public-status' }
    else { $VecherPublic = Get-VecherDirectPublicStatus }
    Assert-VecherPublicStatus $VecherPublic $ExpectedApiVersion
    Write-Host ('PUBLIC_API_VERSION=' + $VecherPublic.api_version)
    Write-Host ('PUBLIC_APK_VERSION={0} ({1})' -f $VecherPublic.version_name, $VecherPublic.version_code)
    Write-Host ('PUBLIC_APK_SHA256=' + $VecherPublic.sha256)
    if ($HttpsCheck -eq 'Server') {
        Write-Host 'SERVER_PUBLIC_HTTPS_VERIFIED'
        Write-Host 'WINDOWS_PUBLIC_HTTPS_NOT_CHECKED'
    } else { Write-Host 'WINDOWS_PUBLIC_HTTPS_VERIFIED' }
}

function Assert-VecherPublicApk {
    $VecherApkPath = Join-Path $RepoPath 'artifacts\vecher-0.9.0-14.apk'
    $VecherMetadataPath = Join-Path $RepoPath 'artifacts\vecher-0.9.0-14.version.json'
    $VecherLocal = [IO.File]::ReadAllText($VecherMetadataPath, $script:VecherUtf8) | ConvertFrom-Json
    $VecherLocalHash = Get-VecherHash $VecherApkPath
    $VecherLocalBytes = [long](Get-Item -LiteralPath $VecherApkPath).Length
    if ($VecherLocal.version_code -ne 14 -or $VecherLocal.version_name -cne '0.9.0' -or
        $VecherLocal.sha256 -cne $VecherLocalHash -or $VecherLocal.bytes -ne $VecherLocalBytes -or
        $VecherLocalBytes -lt 2 -or $VecherLocalBytes -gt 209715200) { throw 'Candidate files changed after publication.' }
    $VecherHealth = Get-VecherPublicJson '/health'
    $VecherPublic = Get-VecherPublicJson ('/app/version?release_check=' + [Guid]::NewGuid().ToString('N'))
    if ($VecherHealth.ok -ne $true -or $VecherHealth.version -cne '0.9.0' -or $VecherHealth.env -cne 'staging' -or
        $VecherPublic.version_code -ne 14 -or $VecherPublic.version_name -cne '0.9.0' -or
        $VecherPublic.required -ne $false -or $VecherPublic.sha256 -cne $VecherLocalHash -or
        $VecherPublic.bytes -ne $VecherLocalBytes -or $VecherPublic.notes -cne $VecherLocal.notes) {
        throw 'Public HTTPS metadata does not match the accepted APK14. The local publication may already have completed; keep the same files and inspect -Mode Status.'
    }
    $VecherRequest = [Net.HttpWebRequest]::Create($script:VecherOrigin + '/download/vecher.apk?sha256=' + $VecherLocalHash)
    $VecherRequest.AllowAutoRedirect = $false
    $VecherRequest.Timeout = 30000
    $VecherRequest.ReadWriteTimeout = 30000
    $VecherResponse = $null
    $VecherInput = $null
    $VecherHasher = [Security.Cryptography.SHA256]::Create()
    try {
        $VecherResponse = $VecherRequest.GetResponse()
        if ([int]$VecherResponse.StatusCode -ne 200 -or
            ($VecherResponse.ContentLength -ne -1 -and $VecherResponse.ContentLength -ne $VecherLocalBytes)) {
            throw 'Public APK response status or Content-Length differs from the candidate.'
        }
        $VecherInput = $VecherResponse.GetResponseStream()
        $VecherBuffer = New-Object byte[] 81920
        [long]$VecherReceived = 0
        while (($VecherRead = $VecherInput.Read($VecherBuffer, 0, $VecherBuffer.Length)) -gt 0) {
            $VecherReceived += $VecherRead
            if ($VecherReceived -gt $VecherLocalBytes) { throw 'Public APK is longer than the accepted candidate.' }
            $null = $VecherHasher.TransformBlock($VecherBuffer, 0, $VecherRead, $VecherBuffer, 0)
        }
        $null = $VecherHasher.TransformFinalBlock((New-Object byte[] 0), 0, 0)
        $VecherRemoteHash = ([BitConverter]::ToString($VecherHasher.Hash)).Replace('-', '').ToLowerInvariant()
        if ($VecherReceived -ne $VecherLocalBytes -or $VecherRemoteHash -cne $VecherLocalHash) {
            throw 'Public HTTPS APK bytes do not match the accepted candidate.'
        }
    } catch {
        throw 'The published APK was not verified by public HTTPS. Publication may already have completed; keep the same artifacts and inspect Status. No response or exception details were printed.'
    } finally {
        if ($null -ne $VecherInput) { $VecherInput.Dispose() }
        if ($null -ne $VecherResponse) { $VecherResponse.Dispose() }
        $VecherHasher.Dispose()
    }
    Write-Host ('PUBLIC_HTTPS_APK14_SHA256=' + $VecherRemoteHash)
    Write-Host ('PUBLIC_HTTPS_APK14_BYTES=' + $VecherReceived)
    Write-Host 'VECHER_APK14_PUBLIC_HTTPS_VERIFIED'
}

Assert-VecherPackage
if ($HttpsCheck -eq 'Server' -and $Mode -notin @('Prepare', 'Server', 'Status')) {
    throw 'Server HTTPS checks are allowed only for Prepare, Server and Status. Build, Accept, Publish and DisableBlogs must use Direct.'
}
$VecherLeaf = Join-Path $VecherPackage 'release-apk14.ps1'
$VecherLeafArgs = @{ RepoPath = $RepoPath; FlutterPath = $FlutterPath; AndroidSdk = $AndroidSdk }
if ($HttpsCheck -eq 'Direct' -and $Mode -in @('Prepare', 'Server')) {
    # Fail before deployment or tool staging if this PC cannot reach public HTTPS.
    Show-VecherPublicStatus
}
if ($Mode -in @('Prepare', 'Server', 'Status', 'DisableBlogs')) {
    Send-VecherServerTools
    if ($Mode -eq 'Status') {
        Invoke-VecherServerAction 'status'
        Write-Host 'SERVER_STATUS_RECEIVED'
        try { Show-VecherPublicStatus }
        catch {
            if ($HttpsCheck -eq 'Server') {
                Write-Host 'SERVER_PUBLIC_HTTPS_UNVERIFIED'
                Write-Host 'WINDOWS_PUBLIC_HTTPS_NOT_CHECKED'
            } else { Write-Host 'WINDOWS_PUBLIC_HTTPS_UNVERIFIED' }
            Write-Host 'STATUS_RESULT=PARTIAL'
            Write-Host 'The SSH status above was received. The selected public HTTPS check was not verified; this is not release readiness.'
        }
        return
    }
    if ($Mode -eq 'DisableBlogs') {
        Invoke-VecherServerAction 'disable-blogs'
        Show-VecherPublicStatus '0.9.0'
        return
    }
    if ($HttpsCheck -eq 'Server') { Show-VecherPublicStatus }
    Invoke-VecherServerAction 'deploy'
    Invoke-VecherServerAction 'enable-blogs'
    Show-VecherPublicStatus '0.9.0'
    Write-Host 'VECHER_SERVER14_PREPARED_FOR_DEVICE_ACCEPTANCE'
    if ($Mode -eq 'Server') { return }
}
if ($Mode -eq 'Prepare' -or $Mode -eq 'Build') {
    Assert-VecherVerifiedFile 'release-apk14.ps1'
    & $VecherLeaf -Mode Build @VecherLeafArgs
    if (-not $?) { throw 'APK14 candidate build did not complete.' }
    Write-Host 'VECHER_RELEASE14_READY_FOR_PHYSICAL_DEVICE_TESTS'
    Write-Host 'Install artifacts\vecher-0.9.0-14.apk on the test phones. Complete DEVICE_CHECKS.ru.txt, then run -Mode Accept.'
    return
}
if ($Mode -eq 'Accept') {
    Assert-VecherVerifiedFile 'release-apk14.ps1'
    & $VecherLeaf -Mode Accept @VecherLeafArgs
    if (-not $?) { throw 'Device acceptance did not complete. APK14 has not been published by this command.' }
    return
}
if ($Mode -eq 'Publish') {
    # Publishing is always Direct; verify public reachability before the leaf
    # can replace any server APK files, then retain the complete byte postcheck.
    Show-VecherPublicStatus '0.9.0'
    Assert-VecherVerifiedFile 'release-apk14.ps1'
    & $VecherLeaf -Mode Publish @VecherLeafArgs
    if (-not $?) { throw 'APK14 publication did not complete. Keep the same candidate and receipts.' }
    Assert-VecherPublicApk
}
