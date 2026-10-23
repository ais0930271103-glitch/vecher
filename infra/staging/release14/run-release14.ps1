#requires -Version 5.1
[CmdletBinding()]
param(
    [ValidateSet('Prepare', 'Server', 'Build', 'Accept', 'Publish', 'Status', 'DisableBlogs')]
    [string]$Mode = 'Prepare',
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
        'server_release.py', 'media_rehearsal.py', 'notes-0.9.0.txt'
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
    $VecherUploads = @('server_release.py', 'media_rehearsal.py') | ForEach-Object {
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
    if (@('preflight', 'deploy', 'status', 'enable-blogs', 'disable-blogs') -cnotcontains $Action) { throw 'Unknown server action.' }
    Assert-VecherVerifiedFile 'server_release.py'
    Assert-VecherVerifiedFile 'media_rehearsal.py'
    $VecherServerHash = $script:VecherExpectedFileHashes['server_release.py']
    $VecherMediaHash = $script:VecherExpectedFileHashes['media_rehearsal.py']
    $VecherParts = @(
        'set -eu;'
        ('cd ''{0}'';' -f $script:VecherRemoteStage)
        ('printf ''%s  %s\n'' ''{0}'' ''server_release.py'' ''{1}'' ''media_rehearsal.py'' | sha256sum -c -;' -f $VecherServerHash, $VecherMediaHash)
        ('python3 ./server_release.py ''{0}'' </dev/null' -f $Action)
    )
    $VecherArgs = $script:VecherSshOptions + @('vecher', ($VecherParts -join ' '))
    # Stream progress during a potentially long backup/build/rehearsal. Keeping
    # stdout in an array would hide minutes of meaningful server output.
    & $script:VecherSsh @VecherArgs
    $VecherExit = $LASTEXITCODE
    if ($VecherExit -ne 0) {
        throw ('Server action ' + $Action + ' did not complete (exit ' + $VecherExit + '). Keep the output. Use -Mode Status before any retry; do not restore a database manually.')
    }
}

function Get-VecherPublicJson([string]$Path) {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    return Invoke-RestMethod -Method Get -Uri ($script:VecherOrigin + $Path) -TimeoutSec 30
}

function Show-VecherPublicStatus([string]$ExpectedApiVersion = '') {
    $VecherHealth = Get-VecherPublicJson '/health'
    $VecherVersion = Get-VecherPublicJson '/app/version'
    if ($VecherHealth.ok -ne $true -or $VecherHealth.env -cne 'staging') { throw 'Public HTTPS health check failed.' }
    if ($ExpectedApiVersion -and $VecherHealth.version -cne $ExpectedApiVersion) {
        throw 'The public HTTPS endpoint is not serving API0.9.0 yet. Keep the server output and inspect -Mode Status before device acceptance.'
    }
    Write-Host ('PUBLIC_API_VERSION=' + $VecherHealth.version)
    Write-Host ('PUBLIC_APK_VERSION={0} ({1})' -f $VecherVersion.version_name, $VecherVersion.version_code)
    Write-Host ('PUBLIC_APK_SHA256=' + $VecherVersion.sha256)
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
$VecherLeaf = Join-Path $VecherPackage 'release-apk14.ps1'
$VecherLeafArgs = @{ RepoPath = $RepoPath; FlutterPath = $FlutterPath; AndroidSdk = $AndroidSdk }
if ($Mode -in @('Prepare', 'Server', 'Status', 'DisableBlogs')) {
    Send-VecherServerTools
    if ($Mode -eq 'Status') {
        Invoke-VecherServerAction 'status'
        Show-VecherPublicStatus
        return
    }
    if ($Mode -eq 'DisableBlogs') {
        Invoke-VecherServerAction 'disable-blogs'
        Show-VecherPublicStatus '0.9.0'
        return
    }
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
    Assert-VecherVerifiedFile 'release-apk14.ps1'
    & $VecherLeaf -Mode Publish @VecherLeafArgs
    if (-not $?) { throw 'APK14 publication did not complete. Keep the same candidate and receipts.' }
    Assert-VecherPublicApk
}
