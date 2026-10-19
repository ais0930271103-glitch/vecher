#requires -Version 5.1
# Publish the already built and verified Vecher 0.2.1 (6) APK.
# Run from this repository's infra/staging directory, or pass this file to
# powershell.exe -NoProfile -ExecutionPolicy Bypass -File <path>.
# This script does not build, sign, or modify the APK or its version metadata.

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$VecherRepo = [IO.Path]::GetFullPath((Join-Path -Path $PSScriptRoot -ChildPath '..\..'))
$VecherArtifacts = Join-Path -Path $VecherRepo -ChildPath 'artifacts'
$VecherApk = Join-Path -Path $VecherArtifacts -ChildPath 'vecher-0.2.1-6.apk'
$VecherMetadata = Join-Path -Path $VecherArtifacts -ChildPath 'vecher-0.2.1-6.version.json'
$VecherPublisher = Join-Path -Path $PSScriptRoot -ChildPath 'vecher-publish-apk6.py'
$VecherExpectedBytes = [long]34299136
$VecherExpectedHash = '73c3f84dd7c16a4d463206957ab9dfa8eb7919237a8ace1ced39b4d1d3f11f75'

foreach ($VecherFile in @($VecherApk, $VecherMetadata, $VecherPublisher)) {
    if (-not (Test-Path -LiteralPath $VecherFile -PathType Leaf)) {
        throw "Required file is missing: $VecherFile"
    }
}

$VecherApkInfo = Get-Item -LiteralPath $VecherApk
if ($VecherApkInfo.Length -ne $VecherExpectedBytes) {
    throw "APK size does not match the verified build: expected $VecherExpectedBytes bytes."
}
$VecherApkHash = (Get-FileHash -LiteralPath $VecherApk -Algorithm SHA256).Hash.ToLowerInvariant()
if ($VecherApkHash -cne $VecherExpectedHash) {
    throw 'APK SHA-256 does not match the verified build. Publication stopped.'
}
Write-Host 'APK_INPUT_HASH_OK'
Write-Host "APK_BYTES=$VecherExpectedBytes"
Write-Host "APK_FILE_SHA256=$VecherApkHash"

if ((Get-Item -LiteralPath $VecherMetadata).Length -gt 65536) {
    throw 'Version metadata is unexpectedly large.'
}
$VecherUtf8 = [Text.UTF8Encoding]::new($false, $true)
$VecherMetadataText = [IO.File]::ReadAllText($VecherMetadata, $VecherUtf8)
$VecherVersion = ConvertFrom-Json -InputObject $VecherMetadataText -ErrorAction Stop
if ($null -eq $VecherVersion -or $VecherVersion -isnot [pscustomobject]) {
    throw 'Version metadata must be a JSON object.'
}
$VecherExpectedFields = @('version_code', 'version_name', 'notes', 'required')
$VecherActualFields = @($VecherVersion.PSObject.Properties.Name)
if ($VecherActualFields.Count -ne $VecherExpectedFields.Count) {
    throw 'Version metadata has an unexpected set of fields.'
}
foreach ($VecherField in $VecherExpectedFields) {
    if ($VecherActualFields -cnotcontains $VecherField) {
        throw "Version metadata field is missing: $VecherField"
    }
}
if ((($VecherVersion.version_code -isnot [int]) -and
     ($VecherVersion.version_code -isnot [long])) -or
    $VecherVersion.version_code -ne 6) {
    throw 'Version metadata must have integer version_code 6.'
}
if ($VecherVersion.version_name -isnot [string] -or
    $VecherVersion.version_name -cne '0.2.1') {
    throw 'Version metadata must have version_name 0.2.1.'
}
if ($VecherVersion.required -isnot [bool] -or $VecherVersion.required) {
    throw 'Version metadata must have required set to the JSON boolean false.'
}
if ($VecherVersion.notes -isnot [string] -or
    [string]::IsNullOrWhiteSpace($VecherVersion.notes) -or
    $VecherUtf8.GetByteCount($VecherVersion.notes) -gt 4096) {
    throw 'Version notes must contain nonempty text of at most 4096 UTF-8 bytes.'
}
Write-Host 'VERSION_JSON_INPUT_OK=0.2.1 (6)'

$VecherSsh = Join-Path -Path $env:WINDIR -ChildPath 'System32\OpenSSH\ssh.exe'
$VecherScp = Join-Path -Path $env:WINDIR -ChildPath 'System32\OpenSSH\scp.exe'
foreach ($VecherProgram in @($VecherSsh, $VecherScp)) {
    if (-not (Test-Path -LiteralPath $VecherProgram -PathType Leaf)) {
        throw "Windows OpenSSH program was not found: $VecherProgram"
    }
}

# -G resolves the existing alias locally. Only the three target fields are
# inspected; ProxyJump/ProxyCommand and host-key verification remain in force.
$VecherConfigArgs = @('-G', 'vecher')
$VecherSshConfig = @(& $VecherSsh @VecherConfigArgs)
$VecherSshExit = $LASTEXITCODE
if ($VecherSshExit -ne 0) {
    throw "Could not resolve SSH alias vecher (exit $VecherSshExit)."
}
$VecherTarget = @{}
foreach ($VecherConfigLine in $VecherSshConfig) {
    if ($VecherConfigLine -cmatch '^(user|hostname|port)\s+(\S+)\s*$') {
        $VecherConfigKey = $Matches[1]
        if ($VecherTarget.ContainsKey($VecherConfigKey)) {
            throw "SSH configuration repeats target field: $VecherConfigKey"
        }
        $VecherTarget[$VecherConfigKey] = $Matches[2]
    }
}
if ($VecherTarget['user'] -cne 'root' -or
    $VecherTarget['hostname'] -cne '94.228.166.214' -or
    $VecherTarget['port'] -cne '22') {
    throw 'SSH alias vecher must resolve to root@94.228.166.214 on port 22.'
}
Write-Host 'SSH_TARGET_OK=root@94.228.166.214:22 (vecher)'

$VecherStage = '/opt/vecher-test/downloads/.release-6-' + [Guid]::NewGuid().ToString('N')
if ($VecherStage -cnotmatch '^/opt/vecher-test/downloads/\.release-6-[0-9a-f]{32}$') {
    throw 'Internal error: upload stage path is invalid.'
}
Write-Host "UPLOAD_STAGE=$VecherStage"

# The remote command is assembled from fixed shell fragments and a validated
# hexadecimal stage suffix. No user-provided text is inserted into shell code.
# Avoid embedded double quotes here: Windows PowerShell 5.1 uses legacy native
# argument quoting. Joining the fragments also avoids sending CRLF to /bin/sh.
$VecherPrepareParts = @(
    'set -eu;'
    'test $(id -u) -eq 0;'
    'case $(hostname) in'
    'elegant-red|elegant-red.ptr.network) ;;'
    '*) printf ''%s\n'' ''SERVER_HOSTNAME_MISMATCH'' >&2; exit 1 ;;'
    'esac;'
    'test -d /opt/vecher-test/downloads;'
    'test ! -L /opt/vecher-test/downloads;'
    'umask 077;'
    ('mkdir -m 700 -- ''{0}'';' -f $VecherStage)
    'printf ''%s\n'' ''UPLOAD_STAGE_READY'''
)
$VecherPrepareCommand = $VecherPrepareParts -join ' '
$VecherSshOptions = @(
    '-T', '-n',
    '-o', 'ConnectTimeout=20',
    '-o', 'ServerAliveInterval=15',
    '-o', 'ServerAliveCountMax=4'
)
$VecherPrepareArgs = $VecherSshOptions + @('vecher', $VecherPrepareCommand)
& $VecherSsh @VecherPrepareArgs
$VecherSshExit = $LASTEXITCODE
if ($VecherSshExit -ne 0) {
    throw "Upload-stage preparation failed (exit $VecherSshExit). No publication was requested. Stage: $VecherStage"
}

$VecherScpArgs = @(
    '-S', $VecherSsh,
    '-o', 'ConnectTimeout=20',
    '-o', 'ServerAliveInterval=15',
    '-o', 'ServerAliveCountMax=4',
    '--', $VecherApk, $VecherMetadata, $VecherPublisher,
    ('vecher:{0}/' -f $VecherStage)
)
& $VecherScp @VecherScpArgs
$VecherScpExit = $LASTEXITCODE
if ($VecherScpExit -ne 0) {
    throw "File upload failed (exit $VecherScpExit). The stage is retained for diagnosis: $VecherStage"
}

$VecherPublishCommand = 'python3 ''{0}/vecher-publish-apk6.py'' --stage ''{0}'' </dev/null' -f $VecherStage
$VecherPublishArgs = $VecherSshOptions + @('vecher', $VecherPublishCommand)
& $VecherSsh @VecherPublishArgs
$VecherSshExit = $LASTEXITCODE
if ($VecherSshExit -ne 0) {
    throw "The publisher returned an error or SSH was interrupted (exit $VecherSshExit). Keep the output for diagnosis. Stage: $VecherStage"
}
# Successful publication and API/hash verification markers are printed by the
# Python publisher above. Keep them for the final external HTTPS check.
