#requires -Version 5.1
# Build, record physical acceptance, or publish Vecher 0.9.0 (14).
# Build is the default and never uploads or publishes. Keep the package together.
# Run with Windows PowerShell on the original build PC:
# powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\release-apk14.ps1
# The existing signing key is required. No key, persistent environment setting,
# or Windows HTTP connection is created by this runner.

[CmdletBinding()]
param(
    [ValidateSet('Build', 'Accept', 'Publish')]
    [string]$Mode = 'Build',
    [ValidateNotNullOrEmpty()]
    [string]$RepoPath = 'C:\dev\vecher-update',
    [ValidateNotNullOrEmpty()]
    [string]$FlutterPath = 'C:\dev\flutter\bin\flutter.bat',
    [string]$AndroidSdk = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$VecherSourceSha = '40e43d85a2e434d4ee2090884a872a507f60fc93'
$VecherRuntimeHash = '520fb35ae85b4778f584fd4023d048fb69ad7b9afffcaba3a6d750b9fb1b0618'
$VecherOldExpectedBytes = [long]36591005
$VecherOldExpectedHash = 'a87d14f11e874f81509e4c6964f1c7355103a61e24d4e279ff61e454102bc1bc'
$VecherCertificateHash = 'bf3d541231287a7e37629088f470d82079d6b1577d0692cceeb8597779d520c1'
$VecherPublisherExpectedHash = '199e5cb0cda2bf1b4ac2ec595ef26c57a1b324092005cf57d6257e956b75669c'
$VecherMaxApkBytes = [long]209715200
$VecherUtf8 = New-Object Text.UTF8Encoding($false, $true)
$VecherRepo = [IO.Path]::GetFullPath($RepoPath)
$VecherArtifacts = Join-Path $VecherRepo 'artifacts'
$VecherOldApk = Join-Path $VecherArtifacts 'vecher-0.8.0-13.apk'
$VecherApk = Join-Path $VecherArtifacts 'vecher-0.9.0-14.apk'
$VecherMetadata = Join-Path $VecherArtifacts 'vecher-0.9.0-14.version.json'
$VecherReceipt = Join-Path $VecherArtifacts 'vecher-0.9.0-14.source.json'
$VecherPhoneReceipt = Join-Path $VecherArtifacts 'vecher-0.9.0-14.phone.json'
$VecherBuilder = Join-Path $VecherRepo 'app\build-staging-update.ps1'
$VecherBuiltApk = Join-Path $VecherRepo 'app\build\app\outputs\flutter-apk\app-release.apk'
$VecherPublisher = Join-Path $PSScriptRoot 'vecher-publish-apk14.py'
$VecherNotesPath = Join-Path $PSScriptRoot 'notes-0.9.0.txt'
$VecherAcceptanceVersion = 'apk14-device-v1'
$VecherPhoneChecks = @(
    'upgrade_preserves_accounts', 'personal_video_sound_and_seek',
    'couple_exact_revision_consent', 'draft_hidden_from_reader',
    'reviewed_post_reader_and_follow', 'interrupted_upload_retry',
    'picker_background_pin_call', 'withdrawal_closes_new_requests',
    'old_apk13_compatibility'
)
$VecherPhoneReceiptFields = @(
    'format', 'acceptance_version', 'source_sha', 'version_code', 'version_name',
    'apk_sha256', 'apk_bytes', 'metadata_sha256', 'source_receipt_sha256',
    'signing_certificate_sha256', 'publisher_sha256', 'backend_source_sha',
    'backend_runtime_sha256', 'backend_schema', 'backend_api_version',
    'operator_label', 'devices', 'checks', 'accepted_utc'
)

function Get-VecherHash {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required file is missing: $Path"
    }
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Invoke-VecherGit {
    param([string[]]$GitArguments)
    $VecherOutput = @(& $script:VecherGit -C $script:VecherRepo @GitArguments)
    if ($LASTEXITCODE -ne 0) {
        throw ('Git operation failed: {0}. Local changes have not been discarded.' -f $GitArguments[0])
    }
    return $VecherOutput
}

function Assert-VecherSource {
    $VecherCurrentHead = ((Invoke-VecherGit @('rev-parse', 'HEAD')) -join "`n").Trim()
    if ($VecherCurrentHead -cne $script:VecherSourceSha) {
        throw 'Source HEAD changed or does not match the reviewed release commit. Publication stopped.'
    }
    $VecherChanges = @(Invoke-VecherGit @('status', '--porcelain'))
    if ($VecherChanges.Count -ne 0) {
        throw 'The source repository has local changes. Preserve them and review before retrying.'
    }
}

function Read-VecherJsonObject {
    param([string]$Path, [string[]]$Fields)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf) -or
        (Get-Item -LiteralPath $Path).Length -gt 65536) {
        throw "Required JSON file is missing or unexpectedly large: $Path"
    }
    $VecherText = [IO.File]::ReadAllText($Path, $script:VecherUtf8)
    # ConvertFrom-Json can unwrap a one-element root array. Require an object
    # in the original text as well as in the parsed value.
    if (-not $VecherText.TrimStart().StartsWith('{')) { throw "Expected a JSON object: $Path" }
    $VecherObject = ConvertFrom-Json -InputObject $VecherText -ErrorAction Stop
    if ($null -eq $VecherObject -or $VecherObject -isnot [pscustomobject]) {
        throw "Expected a JSON object: $Path"
    }
    $VecherFields = @($VecherObject.PSObject.Properties.Name)
    if ($VecherFields.Count -ne $Fields.Count) {
        throw "JSON has an unexpected set of fields: $Path"
    }
    foreach ($VecherField in $Fields) {
        if ($VecherFields -cnotcontains $VecherField) {
            throw "JSON field is missing: $VecherField"
        }
    }
    return $VecherObject
}

function Assert-VecherApk {
    param([string]$Path, [int]$VersionCode, [string]$VersionName)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required APK is missing: $Path"
    }
    $VecherBytes = (Get-Item -LiteralPath $Path).Length
    if ($VecherBytes -lt 2 -or $VecherBytes -gt $script:VecherMaxApkBytes) {
        throw 'APK size is outside the supported 2..209715200 byte range.'
    }
    # Capture stdout only: Java warnings on stderr are not signature failures.
    $VecherSignature = @(& $script:VecherApkSigner 'verify' '--verbose' '--print-certs' $Path)
    if ($LASTEXITCODE -ne 0) { throw 'apksigner rejected the APK signature.' }
    $VecherSignatureMatches = [regex]::Matches(($VecherSignature -join "`n"), '(?im)^(?:Signer #\d+|V\d+(?:\.\d+)? Signer(?: #\d+)?):? certificate SHA-256 digest:\s*([a-f0-9]{64})\s*$')
    $VecherDigests = @($VecherSignatureMatches | ForEach-Object { $_.Groups[1].Value.ToLowerInvariant() } | Sort-Object -Unique)
    if ($VecherDigests.Count -ne 1 -or $VecherDigests[0] -cne $script:VecherCertificateHash) {
        throw 'APK certificate does not match the existing APK13 certificate. Do not replace or create a signing key.'
    }
    $VecherManifest = @(& $script:VecherAapt 'dump' 'badging' $Path)
    if ($LASTEXITCODE -ne 0) { throw 'aapt could not read the APK manifest.' }
    $VecherPackage = [regex]::Match(($VecherManifest -join "`n"), "(?m)^package: name='([^']+)' versionCode='([0-9]+)' versionName='([^']+)'")
    if (-not $VecherPackage.Success -or
        $VecherPackage.Groups[1].Value -cne 'online.vecher.vecher_app' -or
        [long]$VecherPackage.Groups[2].Value -ne $VersionCode -or
        $VecherPackage.Groups[3].Value -cne $VersionName) {
        throw 'Unexpected application ID or version in the APK manifest.'
    }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $VecherZip = [IO.Compression.ZipFile]::OpenRead($Path)
    try {
        $VecherNative = @($VecherZip.Entries | Where-Object { $_.FullName -cmatch '^lib/[^/]+/[^/]+\.so$' } | ForEach-Object { $_.FullName })
        $VecherAbis = @($VecherNative | ForEach-Object { $_.Split('/')[1] } | Sort-Object -Unique)
        if ($VecherAbis.Count -ne 1 -or $VecherAbis[0] -cne 'arm64-v8a' -or
            $VecherNative -cnotcontains 'lib/arm64-v8a/libflutter.so' -or
            $VecherNative -cnotcontains 'lib/arm64-v8a/libapp.so' -or
            $VecherNative -cnotcontains 'lib/arm64-v8a/libjingle_peerconnection_so.so') {
            throw 'APK does not contain the complete arm64 Flutter/WebRTC libraries.'
        }
    } finally { $VecherZip.Dispose() }
    return [pscustomobject]@{
        sha256 = Get-VecherHash $Path
        bytes = [long]$VecherBytes
    }
}

function Assert-VecherMetadata {
    param($ApkInfo)
    $VecherVersion = Read-VecherJsonObject $script:VecherMetadata @('version_code', 'version_name', 'notes', 'required', 'sha256', 'bytes')
    if ((($VecherVersion.version_code -isnot [int]) -and ($VecherVersion.version_code -isnot [long])) -or
        $VecherVersion.version_code -ne 14 -or
        $VecherVersion.version_name -isnot [string] -or $VecherVersion.version_name -cne '0.9.0') {
        throw 'Metadata must have integer version_code 14 and version_name 0.9.0.'
    }
    if ($VecherVersion.required -isnot [bool] -or $VecherVersion.required) {
        throw 'Metadata required must be the JSON boolean false.'
    }
    if ($VecherVersion.notes -isnot [string] -or [string]::IsNullOrWhiteSpace($VecherVersion.notes) -or
        $script:VecherUtf8.GetByteCount($VecherVersion.notes) -gt 4096 -or
        $VecherVersion.notes -cne $script:VecherNotes) {
        throw 'Release notes must contain nonempty text of at most 4096 UTF-8 bytes.'
    }
    if ($VecherVersion.sha256 -isnot [string] -or $VecherVersion.sha256 -cnotmatch '^[0-9a-f]{64}$' -or
        $VecherVersion.sha256 -cne $ApkInfo.sha256 -or
        (($VecherVersion.bytes -isnot [int]) -and ($VecherVersion.bytes -isnot [long])) -or
        $VecherVersion.bytes -ne $ApkInfo.bytes) {
        throw 'Metadata SHA-256 or integer byte count does not match the APK.'
    }
    return Get-VecherHash $script:VecherMetadata
}

$VecherReceiptFields = @(
    'format', 'source_sha', 'previous_source_sha', 'version_code', 'version_name',
    'builder_sha256', 'builder_apk_sha256', 'apk_sha256', 'apk_bytes',
    'metadata_sha256', 'signing_certificate_sha256', 'publisher_sha256', 'created_utc'
)

function Assert-VecherReceipt {
    param($ApkInfo, [string]$MetadataHash)
    $VecherProvenance = Read-VecherJsonObject $script:VecherReceipt $script:VecherReceiptFields
    foreach ($VecherField in @('source_sha', 'previous_source_sha', 'version_name', 'builder_sha256',
            'builder_apk_sha256', 'apk_sha256', 'metadata_sha256', 'signing_certificate_sha256', 'publisher_sha256', 'created_utc')) {
        if ($VecherProvenance.$VecherField -isnot [string]) { throw 'Invalid provenance receipt field type.' }
    }
    if ((($VecherProvenance.format -isnot [int]) -and ($VecherProvenance.format -isnot [long])) -or
        $VecherProvenance.format -ne 1 -or
        (($VecherProvenance.version_code -isnot [int]) -and ($VecherProvenance.version_code -isnot [long])) -or
        $VecherProvenance.version_code -ne 14 -or $VecherProvenance.version_name -cne '0.9.0' -or
        $VecherProvenance.source_sha -cne $script:VecherSourceSha -or
        $VecherProvenance.previous_source_sha -cnotmatch '^[0-9a-f]{40}$' -or
        $VecherProvenance.builder_sha256 -cne (Get-VecherHash $script:VecherBuilder) -or
        $VecherProvenance.builder_apk_sha256 -cne $ApkInfo.sha256 -or
        $VecherProvenance.apk_sha256 -cne $ApkInfo.sha256 -or
        (($VecherProvenance.apk_bytes -isnot [int]) -and ($VecherProvenance.apk_bytes -isnot [long])) -or
        $VecherProvenance.apk_bytes -ne $ApkInfo.bytes -or
        $VecherProvenance.metadata_sha256 -cne $MetadataHash -or
        $VecherProvenance.signing_certificate_sha256 -cne $script:VecherCertificateHash -or
        $VecherProvenance.publisher_sha256 -cne $script:VecherPublisherExpectedHash) {
        throw 'Existing APK14 provenance does not match this reviewed source, helper, or artifact bytes. Nothing will be overwritten.'
    }
    $VecherCreated = [DateTimeOffset]::MinValue
    if (-not [DateTimeOffset]::TryParseExact($VecherProvenance.created_utc, 'yyyy-MM-dd HH:mm:ss.fffffff UTC',
            [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::AssumeUniversal, [ref]$VecherCreated)) {
        throw 'Provenance creation timestamp is invalid.'
    }
}

function Save-VecherJsonNew {
    param([string]$Path, $Value)
    if (Test-Path -LiteralPath $Path) { throw 'An existing receipt will not be overwritten.' }
    $VecherTemp = $Path + '.' + [Guid]::NewGuid().ToString('N') + '.tmp'
    try {
        $VecherBytes = $script:VecherUtf8.GetBytes(($Value | ConvertTo-Json -Compress -Depth 6))
        $VecherStream = [IO.File]::Open($VecherTemp, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try { $VecherStream.Write($VecherBytes, 0, $VecherBytes.Length) }
        finally { $VecherStream.Dispose() }
        [IO.File]::Move($VecherTemp, $Path)
    } finally {
        if (Test-Path -LiteralPath $VecherTemp -PathType Leaf) { Remove-Item -LiteralPath $VecherTemp }
    }
}

function Assert-VecherPhoneReceipt {
    param($ApkInfo, [string]$MetadataHash, [string]$SourceReceiptHash)
    $VecherPhone = Read-VecherJsonObject $script:VecherPhoneReceipt $script:VecherPhoneReceiptFields
    foreach ($VecherField in @('source_sha', 'acceptance_version', 'version_name', 'apk_sha256',
            'metadata_sha256', 'source_receipt_sha256', 'signing_certificate_sha256', 'publisher_sha256',
            'backend_source_sha', 'backend_runtime_sha256', 'backend_schema', 'backend_api_version',
            'operator_label', 'accepted_utc')) {
        if ($VecherPhone.$VecherField -isnot [string]) { throw 'Invalid phone acceptance field type.' }
    }
    if ((($VecherPhone.format -isnot [int]) -and ($VecherPhone.format -isnot [long])) -or
        $VecherPhone.format -ne 1 -or
        (($VecherPhone.version_code -isnot [int]) -and ($VecherPhone.version_code -isnot [long])) -or
        $VecherPhone.version_code -ne 14 -or $VecherPhone.version_name -cne '0.9.0' -or
        $VecherPhone.acceptance_version -cne $script:VecherAcceptanceVersion -or
        $VecherPhone.source_sha -cne $script:VecherSourceSha -or
        $VecherPhone.apk_sha256 -cne $ApkInfo.sha256 -or
        (($VecherPhone.apk_bytes -isnot [int]) -and ($VecherPhone.apk_bytes -isnot [long])) -or
        $VecherPhone.apk_bytes -ne $ApkInfo.bytes -or
        $VecherPhone.metadata_sha256 -cne $MetadataHash -or
        $VecherPhone.source_receipt_sha256 -cne $SourceReceiptHash -or
        $VecherPhone.signing_certificate_sha256 -cne $script:VecherCertificateHash -or
        $VecherPhone.publisher_sha256 -cne $script:VecherPublisherExpectedHash -or
        $VecherPhone.backend_source_sha -cne $script:VecherSourceSha -or
        $VecherPhone.backend_runtime_sha256 -cne $script:VecherRuntimeHash -or
        $VecherPhone.backend_schema -cne '0006_public_blogs' -or
        $VecherPhone.backend_api_version -cne '0.9.0' -or
        [string]::IsNullOrWhiteSpace($VecherPhone.operator_label) -or $VecherPhone.operator_label.Length -gt 120) {
        throw 'Phone acceptance does not match these exact APK14/source/metadata bytes.'
    }
    if ($VecherPhone.checks -isnot [pscustomobject] -or
        @($VecherPhone.checks.PSObject.Properties.Name).Count -ne $script:VecherPhoneChecks.Count) {
        throw 'Physical device acceptance is incomplete.'
    }
    foreach ($VecherCheck in $script:VecherPhoneChecks) {
        if (@($VecherPhone.checks.PSObject.Properties.Name) -cnotcontains $VecherCheck -or
            $VecherPhone.checks.$VecherCheck -isnot [bool] -or $VecherPhone.checks.$VecherCheck -ne $true) {
            throw 'Every physical device check must have been explicitly accepted.'
        }
    }
    if ($VecherPhone.devices -isnot [pscustomobject] -or @($VecherPhone.devices.PSObject.Properties.Name).Count -ne 3) {
        throw 'Device acceptance labels are incomplete.'
    }
    foreach ($VecherDevice in @('partner_a', 'partner_b', 'reader')) {
        if (@($VecherPhone.devices.PSObject.Properties.Name) -cnotcontains $VecherDevice -or
            $VecherPhone.devices.$VecherDevice -isnot [string] -or
            [string]::IsNullOrWhiteSpace($VecherPhone.devices.$VecherDevice) -or
            $VecherPhone.devices.$VecherDevice.Length -gt 120) { throw 'Invalid device acceptance label.' }
    }
    $VecherAccepted = [DateTimeOffset]::MinValue
    if (-not [DateTimeOffset]::TryParseExact($VecherPhone.accepted_utc, 'yyyy-MM-dd HH:mm:ss.fffffff UTC',
            [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::AssumeUniversal, [ref]$VecherAccepted)) {
        throw 'Phone acceptance timestamp is invalid.'
    }
    $VecherSourceRecord = Read-VecherJsonObject $script:VecherReceipt $script:VecherReceiptFields
    if ($VecherPhone.accepted_utc -clt $VecherSourceRecord.created_utc) { throw 'Phone acceptance predates the build.' }
    return Get-VecherHash $script:VecherPhoneReceipt
}

function Read-VecherLabel {
    param([string]$Prompt)
    $VecherLabel = (Read-Host $Prompt).Trim()
    if ([string]::IsNullOrWhiteSpace($VecherLabel) -or $VecherLabel.Length -gt 120) {
        throw 'Acceptance cancelled: a short nonempty label is required.'
    }
    return $VecherLabel
}

function Request-VecherPhoneAcceptance {
    param($ApkInfo, [string]$MetadataHash, [string]$SourceReceiptHash)
    Write-Host 'Проверка на телефонах не выполняется этим скриптом.'
    Write-Host 'Ответ YES означает, что вы уже лично проверили сценарий на указанном APK. Enter или любой другой ответ остановит приёмку.'
    Write-Host ('APK для приёмки: ' + $script:VecherApk)
    Write-Host ('SHA-256: ' + $ApkInfo.sha256)
    $VecherOperator = Read-VecherLabel 'Метка проверяющего (без паролей и личных данных)'
    $VecherDevices = [ordered]@{
        partner_a = Read-VecherLabel 'Первый Android-телефон: модель / версия Android'
        partner_b = Read-VecherLabel 'Второй Android-телефон: модель / версия Android'
        reader = Read-VecherLabel 'Отдельный тестовый читатель: устройство / отдельная учётная запись, без логина'
    }
    $VecherConfirmedHash = (Read-Host 'Вставьте SHA-256 именно того APK14, который установлен для этих проверок').Trim()
    if ($VecherConfirmedHash -cne $ApkInfo.sha256) { throw 'The tested APK hash does not match this candidate.' }
    $VecherQuestions = @(
        'APK 0.9.0 (14) установлен поверх прежнего Вечера на обоих телефонах; вход и данные сохранились?',
        'Личный блог: снятый телефоном ролик загрузился и обработался; настоящее видео играет со звуком и перемоткой?',
        'Общий блог: оба подтвердили профиль и точную версию поста; после правки старое подтверждение со второго телефона отклонено?',
        'Отдельный читатель не видит черновик и ролик до согласий и операторской проверки?',
        'После операторской проверки читатель смотрит ролик; подписка на автора показывает его материал в Подписках?',
        'При обрыве сети во время загрузки понятен повтор; повтор не создал дубликат публикации?',
        'Выбор файла, фон, PIN и открытие звонка проверены: текст сохраняется в своём аккаунте, закрытый экран не играет видео?',
        'Снятие поста и отзыв профиля закрывают новые запросы читателя; повторное согласие само не возвращает старые посты?',
        'Прежний APK 0.8.0 (13) на обновлённом сервере открывает старую ленту и видео; неподдерживаемые новые файлы ему не выдаются?'
    )
    $VecherChecks = [ordered]@{}
    for ($VecherIndex = 0; $VecherIndex -lt $script:VecherPhoneChecks.Count; $VecherIndex++) {
        $VecherAnswer = (Read-Host ($VecherQuestions[$VecherIndex] + ' [YES / Enter]')).Trim()
        if ($VecherAnswer -cne 'YES') { throw 'Physical acceptance is incomplete. Nothing was published or accepted.' }
        $VecherChecks[$script:VecherPhoneChecks[$VecherIndex]] = $true
    }
    Assert-VecherSource
    if ((Get-VecherHash $script:VecherApk) -cne $ApkInfo.sha256 -or
        (Get-VecherHash $script:VecherMetadata) -cne $MetadataHash -or
        (Get-VecherHash $script:VecherReceipt) -cne $SourceReceiptHash) {
        throw 'An input changed during device acceptance. No receipt was saved.'
    }
    $VecherPhone = [ordered]@{
        format = 1
        acceptance_version = $script:VecherAcceptanceVersion
        source_sha = $script:VecherSourceSha
        version_code = 14
        version_name = '0.9.0'
        apk_sha256 = $ApkInfo.sha256
        apk_bytes = [long]$ApkInfo.bytes
        metadata_sha256 = $MetadataHash
        source_receipt_sha256 = $SourceReceiptHash
        signing_certificate_sha256 = $script:VecherCertificateHash
        publisher_sha256 = $script:VecherPublisherExpectedHash
        backend_source_sha = $script:VecherSourceSha
        backend_runtime_sha256 = $script:VecherRuntimeHash
        backend_schema = '0006_public_blogs'
        backend_api_version = '0.9.0'
        operator_label = $VecherOperator
        devices = $VecherDevices
        checks = $VecherChecks
        accepted_utc = [DateTimeOffset]::UtcNow.ToString('yyyy-MM-dd HH:mm:ss.fffffff UTC', [Globalization.CultureInfo]::InvariantCulture)
    }
    Save-VecherJsonNew $script:VecherPhoneReceipt $VecherPhone
}

# Check the package before source checkout or a potentially long build.
if ($VecherPublisherExpectedHash -cnotmatch '^[0-9a-f]{64}$' -or
    (Get-VecherHash $VecherPublisher) -cne $VecherPublisherExpectedHash) {
    throw 'Publisher helper SHA-256 does not match the finalized release package. Stop and use the reviewed package.'
}
if (-not (Test-Path -LiteralPath $VecherNotesPath -PathType Leaf)) { throw 'Release14 notes are missing from the package.' }
$VecherNotes = [IO.File]::ReadAllText($VecherNotesPath, $VecherUtf8).Trim()
if ([string]::IsNullOrWhiteSpace($VecherNotes) -or $VecherUtf8.GetByteCount($VecherNotes) -gt 4096) {
    throw 'Release14 notes must be nonempty UTF-8 text of at most 4096 bytes.'
}
if (-not (Test-Path -LiteralPath $VecherRepo -PathType Container)) {
    throw "Existing source repository was not found: $VecherRepo"
}
$VecherGitCommand = Get-Command 'git' -CommandType Application -ErrorAction Stop | Select-Object -First 1
$VecherGit = $VecherGitCommand.Source
if (((Invoke-VecherGit @('rev-parse', '--is-inside-work-tree')) -join '').Trim() -cne 'true') {
    throw 'RepoPath must be an existing Git work tree.'
}
$VecherTopLevel = ((Invoke-VecherGit @('rev-parse', '--show-toplevel')) -join "`n").Trim()
if ([IO.Path]::GetFullPath($VecherTopLevel).TrimEnd([char[]]'\/') -ine $VecherRepo.TrimEnd([char[]]'\/')) {
    throw 'RepoPath must point to the repository root.'
}
$VecherOrigin = ((Invoke-VecherGit @('config', '--get', 'remote.origin.url')) -join "`n").Trim()
$VecherAllowedOrigins = @(
    'https://github.com/ais0930271103-glitch/vecher.git',
    'https://github.com/ais0930271103-glitch/vecher',
    'git@github.com:ais0930271103-glitch/vecher.git',
    'git@github.com:ais0930271103-glitch/vecher',
    'ssh://git@github.com/ais0930271103-glitch/vecher.git',
    'git@github-vecher:ais0930271103-glitch/vecher.git'
)
if ($VecherAllowedOrigins -cnotcontains $VecherOrigin) {
    throw 'origin does not match the existing ais0930271103-glitch/vecher repository. Its value has not been printed.'
}
if (@(Invoke-VecherGit @('status', '--porcelain')).Count -ne 0) {
    throw 'The repository has local changes. Preserve them before starting this release; no reset or clean was performed.'
}
$VecherPreviousHead = ((Invoke-VecherGit @('rev-parse', 'HEAD')) -join '').Trim()
if ($VecherPreviousHead -cnotmatch '^[0-9a-f]{40}$') { throw 'Could not identify the existing source commit.' }
Write-Host 'SOURCE_REPOSITORY_OK'
if ($Mode -eq 'Build') {
    # Tooling can advance in its own PR. Fetch the reviewed application commit,
    # not a moving feature-branch tip and not the tooling commit itself.
    Invoke-VecherGit @('fetch', '--no-tags', 'origin', $VecherSourceSha) | Out-Host
    $VecherFetchedHead = ((Invoke-VecherGit @('rev-parse', 'FETCH_HEAD')) -join '').Trim()
    if ($VecherFetchedHead -cne $VecherSourceSha) {
        throw 'Fetched source is not the reviewed application commit.'
    }
    & $VecherGit -C $VecherRepo 'merge-base' '--is-ancestor' $VecherPreviousHead $VecherSourceSha
    if ($LASTEXITCODE -ne 0) {
        throw 'The existing HEAD is not an ancestor of the reviewed application source. No local commits were discarded.'
    }
    Invoke-VecherGit @('switch', '--detach', $VecherSourceSha) | Out-Host
}
Assert-VecherSource
if (-not (Test-Path -LiteralPath $VecherBuilder -PathType Leaf)) { throw 'The reviewed staging builder is missing.' }
Write-Host "SOURCE_COMMIT_OK=$VecherSourceSha"

# Use the installed SDK and existing signing key. Do not create or replace keys.
if ([string]::IsNullOrWhiteSpace($env:USERPROFILE)) { throw 'USERPROFILE is missing; use the original Windows build account.' }
$VecherKeystore = Join-Path $env:USERPROFILE '.android\debug.keystore'
if ($Mode -eq 'Build' -and -not (Test-Path -LiteralPath $VecherKeystore -PathType Leaf)) {
    throw 'The original .android\debug.keystore is missing from this Windows account. Stop; do not create a replacement key.'
}
if (-not (Test-Path -LiteralPath $VecherOldApk -PathType Leaf)) {
    throw 'The verified artifacts\vecher-0.8.0-13.apk is missing. Use the original build PC and preserve its existing signing key.'
}
if ((Get-Item -LiteralPath $VecherOldApk).Length -ne $VecherOldExpectedBytes -or
    (Get-VecherHash $VecherOldApk) -cne $VecherOldExpectedHash) {
    throw 'The local APK13 does not match the published build12 bytes. Publication stopped.'
}
if (-not $AndroidSdk) {
    $VecherSdkCandidates = @($env:ANDROID_HOME, $env:ANDROID_SDK_ROOT)
    if ($env:LOCALAPPDATA) { $VecherSdkCandidates += Join-Path $env:LOCALAPPDATA 'Android\Sdk' }
    foreach ($VecherCandidate in $VecherSdkCandidates) {
        if ($VecherCandidate -and (Test-Path -LiteralPath (Join-Path $VecherCandidate 'build-tools') -PathType Container)) {
            $AndroidSdk = $VecherCandidate
            break
        }
    }
}
if (-not $AndroidSdk -or -not (Test-Path -LiteralPath (Join-Path $AndroidSdk 'build-tools') -PathType Container)) {
    throw 'Installed Android SDK not found. Pass -AndroidSdk with its existing path.'
}
$VecherBuildTools = @(Get-ChildItem -LiteralPath (Join-Path $AndroidSdk 'build-tools') -Directory |
    Where-Object { $_.Name -match '^\d+\.\d+\.\d+$' } |
    Sort-Object { [version]$_.Name } -Descending |
    Where-Object {
        (Test-Path -LiteralPath (Join-Path $_.FullName 'apksigner.bat') -PathType Leaf) -and
        (Test-Path -LiteralPath (Join-Path $_.FullName 'aapt.exe') -PathType Leaf)
    })
if ($VecherBuildTools.Count -eq 0) { throw 'Installed Android build-tools with apksigner and aapt were not found.' }
$VecherApkSigner = Join-Path $VecherBuildTools[0].FullName 'apksigner.bat'
$VecherAapt = Join-Path $VecherBuildTools[0].FullName 'aapt.exe'
$VecherOldInfo = Assert-VecherApk $VecherOldApk 13 '0.8.0'
if ($VecherOldInfo.sha256 -cne $VecherOldExpectedHash -or $VecherOldInfo.bytes -ne $VecherOldExpectedBytes) {
    throw 'APK13 changed during validation.'
}
Write-Host 'APK13_HASH_AND_CERTIFICATE_OK'

$VecherExistingCount = 0
foreach ($VecherPath in @($VecherApk, $VecherMetadata, $VecherReceipt)) {
    if (Test-Path -LiteralPath $VecherPath) { $VecherExistingCount++ }
}
if ($VecherExistingCount -ne 0 -and $VecherExistingCount -ne 3) {
    throw 'Partial or unknown APK14 artifacts exist. Keep them for review; do not delete, overwrite, or invent a provenance receipt.'
}
if ($VecherExistingCount -ne 3 -and ($Mode -ne 'Build' -or (Test-Path -LiteralPath $VecherPhoneReceipt))) {
    throw 'Accept and Publish require the complete existing APK14, metadata and source receipt. Build mode must finish first.'
}
if ($VecherExistingCount -eq 3) {
    # A retry revalidates the stored bytes and provenance, without rebuilding.
    $VecherApkInfo = Assert-VecherApk $VecherApk 14 '0.9.0'
    $VecherMetadataHash = Assert-VecherMetadata $VecherApkInfo
    Assert-VecherReceipt $VecherApkInfo $VecherMetadataHash
    Write-Host 'REUSING_VERIFIED_APK14_NO_REBUILD'
} else {
    Get-Command $FlutterPath -CommandType Application -ErrorAction Stop | Out-Null
    $VecherBuilderHash = Get-VecherHash $VecherBuilder
    & $VecherBuilder -Build 14 -Name '0.9.0' -ExpectedCertificateSha256 $VecherCertificateHash -FlutterPath $FlutterPath -AndroidSdk $AndroidSdk -Notes $VecherNotes
    if (-not $?) { throw 'The reviewed staging builder did not complete successfully.' }
    Assert-VecherSource
    if ((Get-VecherHash $VecherBuilder) -cne $VecherBuilderHash) { throw 'The reviewed builder changed during the build.' }
    $VecherApkInfo = Assert-VecherApk $VecherApk 14 '0.9.0'
    $VecherMetadataHash = Assert-VecherMetadata $VecherApkInfo
    $VecherBuiltHash = Get-VecherHash $VecherBuiltApk
    if ($VecherBuiltHash -cne $VecherApkInfo.sha256) { throw 'The builder output and release artifact differ.' }
    # This receipt is written only after this runner executes the pinned builder.
    # It records local provenance; it is not synthesized for preexisting APKs.
    $VecherProvenance = [ordered]@{
        format = 1
        source_sha = $VecherSourceSha
        previous_source_sha = $VecherPreviousHead
        version_code = 14
        version_name = '0.9.0'
        builder_sha256 = $VecherBuilderHash
        builder_apk_sha256 = $VecherBuiltHash
        apk_sha256 = $VecherApkInfo.sha256
        apk_bytes = [long]$VecherApkInfo.bytes
        metadata_sha256 = $VecherMetadataHash
        signing_certificate_sha256 = $VecherCertificateHash
        publisher_sha256 = $VecherPublisherExpectedHash
        # An explicit UTC suffix keeps ConvertFrom-Json from silently changing
        # this field to DateTime on some PowerShell versions.
        created_utc = [DateTimeOffset]::UtcNow.ToString('yyyy-MM-dd HH:mm:ss.fffffff UTC', [Globalization.CultureInfo]::InvariantCulture)
    }
    $VecherReceiptTemp = $VecherReceipt + '.' + [Guid]::NewGuid().ToString('N') + '.tmp'
    try {
        $VecherReceiptBytes = $VecherUtf8.GetBytes(($VecherProvenance | ConvertTo-Json -Compress))
        $VecherReceiptStream = [IO.File]::Open($VecherReceiptTemp, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try { $VecherReceiptStream.Write($VecherReceiptBytes, 0, $VecherReceiptBytes.Length) }
        finally { $VecherReceiptStream.Dispose() }
        [IO.File]::Move($VecherReceiptTemp, $VecherReceipt)
    } finally {
        if (Test-Path -LiteralPath $VecherReceiptTemp -PathType Leaf) { Remove-Item -LiteralPath $VecherReceiptTemp }
    }
    Assert-VecherReceipt $VecherApkInfo $VecherMetadataHash
    Write-Host 'SOURCE_PROVENANCE_RECEIPT_SAVED'
}
Write-Host 'APK14_SIGNATURE_MANIFEST_ARM64_AND_METADATA_OK'
Write-Host 'APK_VERSION=0.9.0 (14)'
Write-Host ("APK_BYTES={0}" -f $VecherApkInfo.bytes)
Write-Host ("APK_FILE_SHA256={0}" -f $VecherApkInfo.sha256)
Write-Host "VERSION_JSON_SHA256=$VecherMetadataHash"
Write-Host "SOURCE_RECEIPT=$VecherReceipt"
$VecherSourceReceiptHash = Get-VecherHash $VecherReceipt
Write-Host "SOURCE_RECEIPT_SHA256=$VecherSourceReceiptHash"
if ($Mode -eq 'Build') {
    Write-Host "APK_PATH=$VecherApk"
    Write-Host 'VECHER_APK14_BUILD_READY'
    Write-Host 'Install these exact bytes on the test Android phones. Record real checks with -Mode Accept before -Mode Publish.'
    return
}
if ($Mode -eq 'Publish') {
    $VecherPhoneReceiptHash = Assert-VecherPhoneReceipt $VecherApkInfo $VecherMetadataHash $VecherSourceReceiptHash
    Write-Host "PHONE_RECEIPT_SHA256=$VecherPhoneReceiptHash"
}

# Windows OpenSSH honors the existing alias, proxies and host-key checks.
if ([string]::IsNullOrWhiteSpace($env:WINDIR)) { throw 'Windows OpenSSH requires the existing WINDIR setting.' }
$VecherSsh = Join-Path $env:WINDIR 'System32\OpenSSH\ssh.exe'
$VecherScp = Join-Path $env:WINDIR 'System32\OpenSSH\scp.exe'
foreach ($VecherProgram in @($VecherSsh, $VecherScp)) {
    if (-not (Test-Path -LiteralPath $VecherProgram -PathType Leaf)) { throw "Windows OpenSSH program was not found: $VecherProgram" }
}
$VecherSshConfig = @(& $VecherSsh '-G' 'vecher')
if ($LASTEXITCODE -ne 0) { throw 'Could not resolve the existing SSH alias vecher.' }
$VecherTarget = @{}
foreach ($VecherConfigLine in $VecherSshConfig) {
    if ($VecherConfigLine -cmatch '^(user|hostname|port)\s+(\S+)\s*$') {
        $VecherConfigKey = $Matches[1]
        if ($VecherTarget.ContainsKey($VecherConfigKey)) { throw "SSH configuration repeats target field: $VecherConfigKey" }
        $VecherTarget[$VecherConfigKey] = $Matches[2]
    }
}
if ($VecherTarget['user'] -cne 'root' -or $VecherTarget['hostname'] -cne '94.228.166.214' -or $VecherTarget['port'] -cne '22') {
    throw 'SSH alias vecher must resolve to root@94.228.166.214 on port 22.'
}
Write-Host 'SSH_TARGET_OK=root@94.228.166.214:22 (vecher)'

# Recheck source and the exact inputs immediately before any remote operation.
Assert-VecherSource
if ((Get-VecherHash $VecherApk) -cne $VecherApkInfo.sha256 -or
    (Get-Item -LiteralPath $VecherApk).Length -ne $VecherApkInfo.bytes -or
    (Get-VecherHash $VecherMetadata) -cne $VecherMetadataHash -or
    (Get-VecherHash $VecherReceipt) -cne $VecherSourceReceiptHash -or
    (Get-VecherHash $VecherPublisher) -cne $VecherPublisherExpectedHash) {
    throw 'A release input changed after validation. Publication stopped.'
}
Assert-VecherReceipt $VecherApkInfo $VecherMetadataHash
if ($Mode -eq 'Publish' -and
    (Assert-VecherPhoneReceipt $VecherApkInfo $VecherMetadataHash $VecherSourceReceiptHash) -cne $VecherPhoneReceiptHash) {
    throw 'Phone receipt changed after validation.'
}
$VecherStage = '/opt/vecher-test/downloads/.release-14-' + [Guid]::NewGuid().ToString('N')
if ($VecherStage -cnotmatch '^/opt/vecher-test/downloads/\.release-14-[0-9a-f]{32}$') { throw 'Invalid upload-stage path.' }
Write-Host "UPLOAD_STAGE=$VecherStage"
$VecherSshOptions = @('-T', '-n', '-o', 'ConnectTimeout=20', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=4')
# Fixed single-quoted shell fragments avoid the Windows PowerShell 5.1 native
# embedded-double-quote pitfall. Joining them also avoids remote CRLF commands.
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
$VecherPrepareArgs = $VecherSshOptions + @('vecher', ($VecherPrepareParts -join ' '))
& $VecherSsh @VecherPrepareArgs
$VecherSshExit = $LASTEXITCODE
if ($VecherSshExit -ne 0) {
    throw "Upload-stage preparation failed (exit $VecherSshExit). No publication requested. Stage: $VecherStage"
}
$VecherUploadFiles = @($VecherPublisher)
if ($Mode -eq 'Publish') { $VecherUploadFiles = @($VecherApk, $VecherMetadata, $VecherReceipt, $VecherPhoneReceipt, $VecherPublisher) }
$VecherScpArgs = @(
    '-S', $VecherSsh,
    '-o', 'ConnectTimeout=20', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=4',
    '--'
) + $VecherUploadFiles + @(('vecher:{0}/' -f $VecherStage))
& $VecherScp @VecherScpArgs
$VecherScpExit = $LASTEXITCODE
if ($VecherScpExit -ne 0) {
    throw "Upload failed (exit $VecherScpExit). Keep the artifacts and receipt; retry this runner without rebuilding. Stage: $VecherStage"
}
$VecherSizeArgument = $VecherApkInfo.bytes.ToString([Globalization.CultureInfo]::InvariantCulture)
if ($VecherApkInfo.sha256 -cnotmatch '^[0-9a-f]{64}$' -or $VecherSizeArgument -cnotmatch '^[0-9]+$') {
    throw 'Invalid candidate hash or size argument.'
}
$VecherPublishParts = @(
    'set -eu;'
    ('cd ''{0}'';' -f $VecherStage)
    ('printf ''%s  %s\n'' ''{0}'' ''vecher-publish-apk14.py'' | sha256sum -c -;' -f $VecherPublisherExpectedHash)
)
if ($Mode -eq 'Accept') {
    $VecherPublishParts += 'python3 ./vecher-publish-apk14.py --check-backend </dev/null'
} else {
    $VecherPublishParts += ('python3 ./vecher-publish-apk14.py --stage ''{0}'' --expected-sha256 ''{1}'' --expected-size ''{2}'' --expected-metadata-sha256 ''{3}'' --expected-source-receipt-sha256 ''{4}'' --expected-phone-receipt-sha256 ''{5}'' </dev/null' -f $VecherStage, $VecherApkInfo.sha256, $VecherSizeArgument, $VecherMetadataHash, $VecherSourceReceiptHash, $VecherPhoneReceiptHash)
}
$VecherPublishArgs = $VecherSshOptions + @('vecher', ($VecherPublishParts -join ' '))
$VecherPublishOutput = @(& $VecherSsh @VecherPublishArgs)
$VecherSshExit = $LASTEXITCODE
foreach ($VecherLine in $VecherPublishOutput) { Write-Host $VecherLine }
if ($VecherSshExit -ne 0) {
    throw "The publisher or SSH returned an error (exit $VecherSshExit). Keep the output, artifacts and receipt; retry uses the same bytes. Stage: $VecherStage"
}
if ($Mode -eq 'Accept') {
    if ($VecherPublishOutput -cnotcontains 'BLOGS_BACKEND_READY=0.9.0 (14)' -or
        $VecherPublishOutput -cnotcontains ('BACKEND_SOURCE_SHA=' + $VecherSourceSha) -or
        $VecherPublishOutput -cnotcontains ('BACKEND_RUNTIME_SHA256=' + $VecherRuntimeHash) -or
        $VecherPublishOutput -cnotcontains 'BACKEND_SCHEMA=0006_public_blogs' -or
        $VecherPublishOutput -cnotcontains 'BACKEND_API_VERSION=0.9.0') {
        throw 'Backend readiness for physical APK14 acceptance was not confirmed.'
    }
    if (Test-Path -LiteralPath $VecherPhoneReceipt) {
        $VecherPhoneReceiptHash = Assert-VecherPhoneReceipt $VecherApkInfo $VecherMetadataHash $VecherSourceReceiptHash
        Write-Host 'REUSING_RECORDED_PHYSICAL_ACCEPTANCE'
    } else {
        Request-VecherPhoneAcceptance $VecherApkInfo $VecherMetadataHash $VecherSourceReceiptHash
        $VecherPhoneReceiptHash = Assert-VecherPhoneReceipt $VecherApkInfo $VecherMetadataHash $VecherSourceReceiptHash
    }
    Write-Host "PHONE_RECEIPT=$VecherPhoneReceipt"
    Write-Host "PHONE_RECEIPT_SHA256=$VecherPhoneReceiptHash"
    Write-Host 'VECHER_APK14_PHONE_ACCEPTED'
    Write-Host 'No APK was published. -Mode Publish will recheck these exact files and current backend readiness.'
    return
}
if ($VecherPublishOutput -cnotcontains 'VECHER_UPDATE_PUBLISHED_OK' -or
    $VecherPublishOutput -cnotcontains 'API_VERSION_OK=0.9.0 (14)' -or
    $VecherPublishOutput -cnotcontains ('API_APK_SHA256=' + $VecherApkInfo.sha256)) {
    throw 'SSH completed but publication of this exact APK was not confirmed. Keep the output and retry the same artifacts for an idempotent check.'
}
Write-Host 'VECHER_APK14_RELEASE_OK'
Write-Host 'Keep the APK, version JSON and source receipt. External HTTPS and on-device update checks follow separately.'
