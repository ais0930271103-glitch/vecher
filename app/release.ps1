# Выпуск новой версии «Вечера» на тестовый сервер: собрать APK → загрузить → у всех появится «Новая версия».
#
# Запуск (в папке app):   .\release.ps1 -Build 3 -Notes "Что нового"
#   -Build     номер сборки: каждый раз на 1 больше прошлого (у первой отладочной был 1)
#   -Name      видимая версия, по умолчанию из pubspec.yaml (например 0.1.0)
#   -Notes     что нового — покажется в окне обновления
#   -Required  обязательное обновление (без кнопки «Позже»)
#
# Нужен ярлык «vecher» в %USERPROFILE%\.ssh\config (см. claude/server-test-2026-10-12.md в проекте).

param(
  [Parameter(Mandatory = $true)][int]$Build,
  [string]$Name = "",
  [string]$Notes = "Исправления и улучшения",
  [switch]$Required
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$Api = "https://api-test.xn--b1aga8b7a.online"

if (-not $Name) {
  $Name = (Select-String -Path pubspec.yaml -Pattern '^version:\s*([0-9.]+)').Matches[0].Groups[1].Value
}

Write-Host "== Сборка $Name ($Build)" -ForegroundColor Yellow
flutter build apk --release --build-name $Name --build-number $Build --dart-define=API_URL=$Api
if ($LASTEXITCODE -ne 0) { throw "Сборка не удалась" }

$Out = Join-Path $env:TEMP "vecher-release"
New-Item -ItemType Directory -Force -Path $Out | Out-Null
Copy-Item "build\app\outputs\flutter-apk\app-release.apk" (Join-Path $Out "vecher.apk.new") -Force
$Json = @{ version_code = $Build; version_name = $Name; notes = $Notes; required = [bool]$Required } | ConvertTo-Json -Compress
[IO.File]::WriteAllText((Join-Path $Out "version.json.new"), $Json, (New-Object Text.UTF8Encoding $false))

Write-Host "== Загрузка на сервер" -ForegroundColor Yellow
scp (Join-Path $Out "vecher.apk.new") (Join-Path $Out "version.json.new") vecher:/opt/vecher-test/downloads/
if ($LASTEXITCODE -ne 0) { throw "Не удалось загрузить файлы" }
# Сначала APK, потом version.json — чтобы никто не увидел «новую версию» раньше, чем файл готов.
ssh vecher "cd /opt/vecher-test/downloads && mv vecher.apk.new vecher.apk && mv version.json.new version.json && cat version.json"
if ($LASTEXITCODE -ne 0) { throw "Не удалось опубликовать" }

Write-Host ""
Write-Host "Готово. Ссылка для установки: $Api/download/vecher.apk" -ForegroundColor Green
