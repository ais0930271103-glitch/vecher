# Build and verify an update for the existing staging Android installation.
# This script only creates local artifacts. It does not upload or publish them.
# Run from any directory: powershell -NoProfile -ExecutionPolicy Bypass -File .\app\build-staging-update.ps1
[CmdletBinding()]
param(
    [ValidateRange(6, 2100000000)][int]$Build = 6,
    [ValidatePattern('^\d+\.\d+\.\d+$')][string]$Name = '0.2.1',
    [ValidatePattern('^[0-9a-fA-F]{64}$')][string]$ExpectedCertificateSha256 = 'bf3d541231287a7e37629088f470d82079d6b1577d0692cceeb8597779d520c1',
    [string]$AndroidSdk = '',
    [string]$FlutterPath = '',
    [string]$Notes = ''
)

$ErrorActionPreference = 'Stop'
Push-Location -LiteralPath $PSScriptRoot
try {
    if (-not $AndroidSdk) {
        foreach ($Candidate in @($env:ANDROID_HOME, $env:ANDROID_SDK_ROOT, (Join-Path $env:LOCALAPPDATA 'Android\Sdk'))) {
            if ($Candidate -and (Test-Path -LiteralPath (Join-Path $Candidate 'build-tools') -PathType Container)) {
                $AndroidSdk = $Candidate
                break
            }
        }
    }
    if (-not $AndroidSdk) { throw 'Android SDK not found. Pass -AndroidSdk with its existing path.' }
    $BuildTools = @(Get-ChildItem -LiteralPath (Join-Path $AndroidSdk 'build-tools') -Directory |
        Where-Object { $_.Name -match '^\d+\.\d+\.\d+$' } |
        Sort-Object { [version]$_.Name } -Descending |
        Where-Object {
            (Test-Path -LiteralPath (Join-Path $_.FullName 'apksigner.bat') -PathType Leaf) -and
            (Test-Path -LiteralPath (Join-Path $_.FullName 'aapt.exe') -PathType Leaf)
        })
    if ($BuildTools.Count -eq 0) { throw 'Installed Android build-tools with apksigner and aapt were not found.' }
    $ApkSigner = Join-Path $BuildTools[0].FullName 'apksigner.bat'
    $Aapt = Join-Path $BuildTools[0].FullName 'aapt.exe'

    if ($FlutterPath) {
        $Flutter = Get-Command $FlutterPath -CommandType Application -ErrorAction Stop
    } else {
        $Flutter = Get-Command 'flutter' -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $Flutter -and (Test-Path -LiteralPath 'C:\dev\flutter\bin\flutter.bat' -PathType Leaf)) {
            $Flutter = Get-Command 'C:\dev\flutter\bin\flutter.bat' -CommandType Application -ErrorAction Stop
        }
        if (-not $Flutter) { throw 'Flutter not found. Pass -FlutterPath with the path to the existing flutter.bat.' }
    }
    & $Flutter.Source pub get --enforce-lockfile
    if ($LASTEXITCODE -ne 0) { throw 'flutter pub get failed.' }
    & $Flutter.Source analyze --no-fatal-infos --no-fatal-warnings
    if ($LASTEXITCODE -ne 0) { throw 'Flutter analysis failed.' }
    & $Flutter.Source test
    if ($LASTEXITCODE -ne 0) { throw 'Flutter tests failed.' }

    Write-Host "BUILDING_APK=$Name ($Build)"
    $BuildArguments = @(
        'build', 'apk', '--release', '--target-platform', 'android-arm64',
        '--build-name', $Name, '--build-number', [string]$Build,
        '--dart-define=API_URL=https://api-test.xn--b1aga8b7a.online',
        '--dart-define=CALL_RELAY_ONLY=false'
    )
    & $Flutter.Source @BuildArguments
    if ($LASTEXITCODE -ne 0) { throw 'APK build failed.' }
    $BuiltApk = Join-Path $PSScriptRoot 'build\app\outputs\flutter-apk\app-release.apk'
    if (-not (Test-Path -LiteralPath $BuiltApk -PathType Leaf)) { throw 'Built APK not found.' }

    # Capture stdout only. Java warnings on stderr are not signature failures.
    $VerifyArguments = @('verify', '--verbose', '--print-certs', $BuiltApk)
    $SignatureOutput = @(& $ApkSigner @VerifyArguments)
    if ($LASTEXITCODE -ne 0) { throw 'apksigner rejected the APK signature.' }
    # Build-tools 36 uses "Signer #1"; 37 also uses "V2 Signer".
    $SignatureMatches = [regex]::Matches(($SignatureOutput -join "`n"), '(?im)^(?:Signer #\d+|V\d+(?:\.\d+)? Signer(?: #\d+)?):? certificate SHA-256 digest:\s*([a-f0-9]{64})\s*$')
    $CertificateDigests = @($SignatureMatches | ForEach-Object { $_.Groups[1].Value.ToLowerInvariant() } | Sort-Object -Unique)
    if ($CertificateDigests.Count -eq 0) {
        throw 'Could not parse the signing certificate. This does not mean the signing key is different; keep this APK for diagnosis.'
    }
    if ($CertificateDigests.Count -ne 1 -or $CertificateDigests[0] -ne $ExpectedCertificateSha256.ToLowerInvariant()) {
        throw 'The APK signer does not match the installed staging app. Do not publish this file or replace the existing keystore.'
    }
    Write-Host 'APK_SIGNATURE_OK'

    $ManifestArguments = @('dump', 'badging', $BuiltApk)
    $ManifestOutput = @(& $Aapt @ManifestArguments)
    if ($LASTEXITCODE -ne 0) { throw 'aapt could not read the APK manifest.' }
    $Package = [regex]::Match(($ManifestOutput -join "`n"), "(?m)^package: name='([^']+)' versionCode='([0-9]+)' versionName='([^']+)'")
    if (-not $Package.Success -or $Package.Groups[1].Value -ne 'online.vecher.vecher_app' -or
        [int]$Package.Groups[2].Value -ne $Build -or $Package.Groups[3].Value -ne $Name) {
        throw 'Unexpected application ID or version in APK manifest.'
    }
    Write-Host 'APK_MANIFEST_OK'

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $Zip = [IO.Compression.ZipFile]::OpenRead($BuiltApk)
    try {
        $NativeEntries = @($Zip.Entries | Where-Object { $_.FullName -match '^lib/[^/]+/[^/]+\.so$' } | ForEach-Object { $_.FullName })
        $Abis = @($NativeEntries | ForEach-Object { $_.Split('/')[1] } | Sort-Object -Unique)
        if ($Abis.Count -ne 1 -or $Abis[0] -ne 'arm64-v8a' -or
            $NativeEntries -notcontains 'lib/arm64-v8a/libflutter.so' -or
            $NativeEntries -notcontains 'lib/arm64-v8a/libapp.so' -or
            $NativeEntries -notcontains 'lib/arm64-v8a/libjingle_peerconnection_so.so') {
            throw 'APK does not contain the complete arm64 Flutter/WebRTC libraries.'
        }
    } finally { $Zip.Dispose() }
    Write-Host 'APK_ARM64_OK'

    $Artifacts = Join-Path (Split-Path -Parent $PSScriptRoot) 'artifacts'
    New-Item -ItemType Directory -Force -Path $Artifacts | Out-Null
    $ApkPath = Join-Path $Artifacts "vecher-$Name-$Build.apk"
    $VersionPath = Join-Path $Artifacts "vecher-$Name-$Build.version.json"
    $BuiltHash = (Get-FileHash -LiteralPath $BuiltApk -Algorithm SHA256).Hash.ToLowerInvariant()
    if (Test-Path -LiteralPath $ApkPath) {
        $ExistingHash = (Get-FileHash -LiteralPath $ApkPath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($ExistingHash -ne $BuiltHash) {
            throw "A different APK already exists at $ApkPath. Keep it until its publication status is known; choose a new build number for a new release."
        }
    } else { Copy-Item -LiteralPath $BuiltApk -Destination $ApkPath }
    if (-not $Notes) {
        # Read UTF-8 explicitly so Windows PowerShell 5.1 displays Russian correctly.
        $Notes = [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'release-notes-0.2.1.txt'), [Text.Encoding]::UTF8).Trim()
    }
    $Metadata = [ordered]@{version_code = $Build; version_name = $Name; notes = $Notes; required = $false}
    [IO.File]::WriteAllText($VersionPath, ($Metadata | ConvertTo-Json -Compress), (New-Object Text.UTF8Encoding $false))
    if ((Get-FileHash -LiteralPath $ApkPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $BuiltHash) {
        throw 'Artifact hash changed while copying.'
    }
    Write-Host "APK_VERSION=$Name ($Build)"
    Write-Host "APK_BYTES=$((Get-Item -LiteralPath $ApkPath).Length)"
    Write-Host "APK_FILE_SHA256=$BuiltHash"
    Write-Host "APK_PATH=$ApkPath"
    Write-Host "VERSION_JSON_PATH=$VersionPath"
    Write-Host 'VECHER_APK_READY'
} finally { Pop-Location }
