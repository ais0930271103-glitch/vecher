"""Run the access diagnostic in real PowerShell with offline native doubles.

The source script is copied unchanged. Only Invoke-RestMethod is shadowed in
the test driver; curl.exe and ssh.exe are local executable fixtures. No remote
Python, network connection, release action, signing key or Windows installation
is used. Linux PowerShell 7 with Legacy arguments is a compatibility surrogate,
not proof that the script has run on Windows PowerShell 5.1.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


HERE = Path(__file__).resolve().parent
PWSH = os.environ.get("VECHER_PWSH") or shutil.which("pwsh")
if not PWSH:
    candidate = Path("/tmp/vecher-diagnostic-pwsh-7.6.8/runtime/pwsh")
    if candidate.is_file():
        PWSH = str(candidate)

pytestmark = pytest.mark.skipif(
    os.name == "nt" or not PWSH,
    reason="These POSIX executable doubles require a local Linux PowerShell runtime",
)

ORIGIN = "https://api-test.xn--b1aga8b7a.online"
METRICS_FORMAT = (
    "http_code=%{http_code};dns=%{time_namelookup};connect=%{time_connect};"
    "tls=%{time_appconnect};first_byte=%{time_starttransfer};"
    "total=%{time_total};tls_verify=%{ssl_verify_result}"
)
SERVER_REPORT = {
    "diagnostic": "offline_native_fixture",
    "release_actions": False,
    "server_api_local": {"reachable": True, "api_version": "0.8.0"},
}


def ps_quote(value: object) -> str:
    return "'" + str(value).replace("'", "''") + "'"


NATIVE_DOUBLE = r'''
import hashlib
import json
import os
from pathlib import Path
import sys

config_path = Path(os.environ["VECHER_ACCESS_CHECK_FIXTURE"])
root = config_path.parent
config = json.loads(config_path.read_text())
program, argv = Path(sys.argv[0]).name, sys.argv[1:]

def event(kind, **fields):
    with (root / "events.jsonl").open("a", encoding="utf-8") as output:
        output.write(json.dumps({"kind": kind, "argv": argv, **fields}) + "\n")

if program == "curl.exe":
    event("curl")
    code = config.get("curl_exit", 7)
    status = "200" if code == 0 else "000"
    print("http_code=" + status + ";dns=0.001;connect=0.000;tls=0.000;"
          "first_byte=0.000;total=0.002;tls_verify=0;untrusted=SECRET_CURL_STDOUT")
    print("SECRET_CURL_STDERR password=fixture-do-not-display", file=sys.stderr)
    sys.exit(code)

if program != "ssh.exe":
    raise SystemExit("UNEXPECTED_NATIVE_TOOL")

if argv == ["-G", "vecher"]:
    event("ssh_config")
    print("user root")
    print("hostname " + config.get("ssh_host", "94.228.166.214"))
    print("port 22")
    print("proxycommand SECRET_SSH_CONFIG_DO_NOT_DISPLAY")
    sys.exit(0)

event("ssh_remote")
expected = ["-T", "-o", "ConnectTimeout=20", "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=4", "vecher", "python3", "-"]
if argv != expected:
    raise SystemExit("UNEXPECTED_REMOTE_COMMAND")
payload = sys.stdin.buffer.read()
(root / "ssh-stdin.bin").write_bytes(payload)
if config.get("ssh_exit", 0):
    print("SSH_FIXTURE_CONNECTION_FAILED", file=sys.stderr)
    sys.exit(config["ssh_exit"])
print(json.dumps(config["server_report"], sort_keys=True))
print("VECHER_ACCESS_DIAGNOSTIC_COMPLETE")
'''


class AccessCase:
    def __init__(self, root: Path):
        self.root = root
        self.package = root / "diagnostic package with spaces"
        self.package.mkdir()
        self.script = self.package / "check-access.ps1"
        shutil.copyfile(HERE / self.script.name, self.script)
        self.python_source = (HERE / "diagnose_access.py").read_bytes()
        # The real payload must survive Windows PowerShell 5.1's ASCII pipe.
        self.python_source.decode("ascii")
        (self.package / "diagnose_access.py").write_bytes(self.python_source)
        self.script_hash = hashlib.sha256(self.script.read_bytes()).hexdigest()
        self.config = {"server_report": SERVER_REPORT}
        self.config_path = root / "fixture.json"
        self.windows = root / "Windows fixture with spaces"
        system = self.windows / "System32"
        (system / "OpenSSH").mkdir(parents=True)
        native = root / "native_double.py"
        native.write_text("#!" + sys.executable + "\n" + NATIVE_DOUBLE, encoding="utf-8")
        native.chmod(0o700)
        (system / "curl.exe").symlink_to(native)
        (system / "OpenSSH/ssh.exe").symlink_to(native)
        self.driver = root / "driver.ps1"
        self.driver.write_text(
            "$ErrorActionPreference='Stop'\n"
            "$PSNativeCommandArgumentPassing='Legacy'\n"
            "function global:Invoke-RestMethod {\n"
            "  param([string]$Method, [string]$Uri, [int]$TimeoutSec)\n"
            "  $fixtureRoot=Split-Path -Parent $env:VECHER_ACCESS_CHECK_FIXTURE\n"
            "  $event=@{kind='https';method=$Method;uri=$Uri;timeout=$TimeoutSec}\n"
            "  $line=($event | ConvertTo-Json -Compress)+[Environment]::NewLine\n"
            "  [IO.File]::AppendAllText((Join-Path $fixtureRoot 'events.jsonl'),$line)\n"
            "  throw [Net.WebException]::new('SECRET_DOTNET_EXCEPTION', [Net.WebExceptionStatus]::ConnectFailure)\n"
            "}\n"
            "try { & " + ps_quote(self.script) + " }\n"
            "catch { [Console]::Error.WriteLine($_.Exception.Message); exit 1 }\n",
            encoding="utf-8-sig",
        )
        self.env = {
            **os.environ,
            "WINDIR": str(self.windows),
            "VECHER_ACCESS_CHECK_FIXTURE": str(self.config_path),
            "XDG_CACHE_HOME": str(root / "cache"),
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_DATA_HOME": str(root / "data"),
        }

    def run(self, **changes):
        self.config.update(changes)
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")
        result = subprocess.run(
            [str(PWSH), "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(self.driver)],
            env=self.env, capture_output=True, text=True, timeout=35,
        )
        assert hashlib.sha256(self.script.read_bytes()).hexdigest() == self.script_hash
        return result

    def events(self):
        path = self.root / "events.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    @staticmethod
    def report(result, prefix):
        lines = [line[len(prefix):] for line in result.stdout.splitlines() if line.startswith(prefix)]
        assert len(lines) == 1, result.stdout + result.stderr
        return json.loads(lines[0])


@pytest.fixture
def case(tmp_path):
    return AccessCase(tmp_path)


def test_https_failure_is_reported_without_leaking_stderr_or_blocking_server_diagnostic(case):
    result = case.run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SECRET_" not in result.stdout + result.stderr
    dotnet = case.report(result, "WINDOWS_POWERSHELL_HTTPS=")
    assert dotnet == {
        "reachable": False, "contract_ok": False, "api_version": None,
        "error_category": "ConnectFailure", "http_status": None,
    }
    curl = case.report(result, "WINDOWS_CURL_HTTPS=")
    assert curl["available"] is True and curl["exit_code"] == 7
    assert curl["timed_out"] is False and curl["error_category"] == "ConnectFailure"
    assert curl["metrics"]["http_code"] == "000"
    assert "untrusted" not in curl["metrics"]
    reports = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{"diagnostic"')]
    assert reports == [SERVER_REPORT]
    assert "SSH_DIAGNOSTIC_EXIT=0" in result.stdout
    assert "VECHER_ACCESS_DIAGNOSTIC_COMPLETE" in result.stdout
    assert "VECHER_ACCESS_CHECK_FINISHED" in result.stdout
    assert [event["kind"] for event in case.events()] == ["ssh_config", "https", "curl", "ssh_remote"]
    payload = (case.root / "ssh-stdin.bin").read_bytes()
    payload.decode("ascii")
    assert payload.rstrip(b"\r\n") == case.python_source.rstrip(b"\r\n")
    assert "VECHER_APK14" not in result.stdout and "RELEASE14_PHASE" not in result.stdout


def test_wrong_ssh_target_stops_before_http_or_remote_execution(case):
    result = case.run(ssh_host="192.0.2.55")
    assert result.returncode != 0
    assert "SSH alias vecher must still resolve" in result.stderr
    assert [event["kind"] for event in case.events()] == ["ssh_config"]
    assert not (case.root / "ssh-stdin.bin").exists()
    assert "VECHER_ACCESS_CHECK_START" not in result.stdout
    assert "SECRET_" not in result.stdout + result.stderr


def test_real_native_capture_preserves_curl_quoted_arguments_and_security_flags(case):
    result = case.run(curl_exit=0)
    assert result.returncode == 0, result.stdout + result.stderr
    curl = next(event for event in case.events() if event["kind"] == "curl")
    assert curl["argv"] == [
        "-q", "--silent", "--show-error", "--connect-timeout", "8", "--max-time", "15",
        "--output", "NUL", "--write-out", METRICS_FORMAT, ORIGIN + "/health",
    ]
    assert not set(curl["argv"]) & {"-k", "--insecure", "-L", "--location", "-v", "--verbose"}
    report = case.report(result, "WINDOWS_CURL_HTTPS=")
    assert report["exit_code"] == 0 and report["metrics"]["http_code"] == "200"
    assert report["error_category"] is None
    assert "SECRET_" not in result.stdout + result.stderr


def test_server_ssh_failure_keeps_nonzero_exit_and_never_prints_completion(case):
    result = case.run(ssh_exit=255)
    assert result.returncode != 0
    assert "The server diagnostic did not complete" in result.stderr
    assert "SSH_DIAGNOSTIC_EXIT=255" in result.stdout
    assert "VECHER_ACCESS_CHECK_FINISHED" not in result.stdout
    assert "VECHER_ACCESS_DIAGNOSTIC_COMPLETE" not in result.stdout
    assert [event["kind"] for event in case.events()] == ["ssh_config", "https", "curl", "ssh_remote"]

