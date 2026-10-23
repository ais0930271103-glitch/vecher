#!/usr/bin/env python3
"""APK14 leaf integration with native doubles on Linux PowerShell 7, Legacy args.

This does not execute Windows 5.1, build Android code, use a real signing key,
contact a server, or perform physical acceptance. Only temporary fixture pins
are substituted; the reviewed builder and release logic execute unchanged.
Run: VECHER_PWSH=/path/to/pwsh python3 -m unittest discover -s . -p test_runner.py
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
import zipfile

HERE = Path(__file__).resolve().parent
SOURCE = HERE
REPO = HERE.parents[2]
PWSH = os.environ.get("VECHER_PWSH", shutil.which("pwsh") or "/tmp/vecher-powershell-7.6.8/pwsh")
TARGET = "40e43d85a2e434d4ee2090884a872a507f60fc93"
OLD_SHA = "a87d14f11e874f81509e4c6964f1c7355103a61e24d4e279ff61e454102bc1bc"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


class Case:
    def __init__(self, **config):
        self.temporary = tempfile.TemporaryDirectory(prefix="vecher-apk14-native-")
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo with spaces"
        self.package = self.root / "reviewed package"
        self.bin = self.root / "native tools"
        self.sdk = self.root / "Android sdk"
        self.win = self.root / "Windows mock"
        self.profile = self.root / "Original build account"
        (self.profile / ".android").mkdir(parents=True)
        (self.profile / ".android/debug.keystore").write_bytes(b"EXISTING_KEY_FIXTURE")
        for folder in (self.repo / "app/android", self.repo / "artifacts", self.package, self.bin):
            folder.mkdir(parents=True)
        (self.repo / "app/android/key.properties").write_text("KEY_PROPERTIES_FIXTURE_UNTOUCHED")
        shutil.copyfile(REPO / "app/build-staging-update.ps1", self.repo / "app/build-staging-update.ps1")
        shutil.copyfile(SOURCE / "notes-0.9.0.txt", self.package / "notes-0.9.0.txt")
        self.old = self.repo / "artifacts/vecher-0.8.0-13.apk"
        with zipfile.ZipFile(self.old, "w", zipfile.ZIP_STORED) as archive:
            for name in ("libflutter.so", "libapp.so", "libjingle_peerconnection_so.so"):
                archive.writestr(zipfile.ZipInfo("lib/arm64-v8a/" + name, (2026, 10, 23, 0, 0, 0)), b"old13-" + name.encode())
        self.old_bytes = self.old.read_bytes()
        self.apk = self.repo / "artifacts/vecher-0.9.0-14.apk"
        self.metadata = self.repo / "artifacts/vecher-0.9.0-14.version.json"
        self.receipt = self.repo / "artifacts/vecher-0.9.0-14.source.json"
        self.phone = self.repo / "artifacts/vecher-0.9.0-14.phone.json"
        self.config_path = self.root / "config.json"
        self.config = {"repo": str(self.repo), "target": TARGET, "answers": [], **config}
        self.helper = self.package / "vecher-publish-apk14.py"
        shutil.copyfile(SOURCE / self.helper.name, self.helper)
        runner = (SOURCE / "release-apk14.ps1").read_text(encoding="utf-8-sig")
        assert OLD_SHA in runner
        runner = runner.replace(OLD_SHA, sha(self.old)).replace("36591005", str(self.old.stat().st_size))
        runner = re.sub(r"(?m)^\$VecherPublisherExpectedHash = '[^']*'$",
                        "$VecherPublisherExpectedHash = '" + sha(self.helper) + "'", runner)
        self.script = self.package / "release-apk14.ps1"
        self.script.write_text(runner, encoding="utf-8-sig")
        build_tools = self.sdk / "build-tools/36.0.0"
        build_tools.mkdir(parents=True)
        ssh_dir = self.win / "System32/OpenSSH"
        ssh_dir.mkdir(parents=True)
        native = self.root / "native_doubles.py"
        shutil.copyfile(HERE / "fixtures/runner_native.py", native)
        native.chmod(0o700)
        self.flutter = self.bin / "flutter.bat"
        for path in (self.bin / "git", self.flutter, build_tools / "apksigner.bat", build_tools / "aapt.exe",
                     ssh_dir / "ssh.exe", ssh_dir / "scp.exe"):
            path.symlink_to(native)
        self.driver = self.root / "driver.ps1"
        self.driver.write_text(
            "param([string]$Mode='Build')\n$ErrorActionPreference='Stop'\n$PSNativeCommandArgumentPassing='Legacy'\n"
            "$global:MockAnswerIndex=0\n"
            "function global:Read-Host { param([string]$Prompt)\n"
            "  $cfg=ConvertFrom-Json ([IO.File]::ReadAllText($env:VECHER_APK14_MOCK_CONFIG))\n"
            "  if($global:MockAnswerIndex -ge $cfg.answers.Count){ throw 'MOCK_ANSWERS_EXHAUSTED' }\n"
            "  $answer=$cfg.answers[$global:MockAnswerIndex]; $global:MockAnswerIndex++\n"
            "  [IO.File]::AppendAllText((Join-Path (Split-Path $env:VECHER_APK14_MOCK_CONFIG) 'prompts.log'), $Prompt+[Environment]::NewLine)\n"
            "  return [string]$answer\n}\n"
            "try { & " + ps_quote(self.script) + " -Mode $Mode -RepoPath " + ps_quote(self.repo) +
            " -FlutterPath " + ps_quote(self.flutter) + " -AndroidSdk " + ps_quote(self.sdk) +
            " } catch { [Console]::Error.WriteLine($_.Exception.Message); exit 1 }\n",
            encoding="utf-8-sig")
        self.env = {**os.environ, "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
                    "WINDIR": str(self.win), "USERPROFILE": str(self.profile), "LOCALAPPDATA": str(self.root / "AppData"),
                    "VECHER_APK14_MOCK_CONFIG": str(self.config_path), "XDG_CACHE_HOME": str(self.root / "cache"),
                    "XDG_CONFIG_HOME": str(self.root / "config"), "XDG_DATA_HOME": str(self.root / "data")}

    def close(self):
        self.temporary.cleanup()

    def run(self, mode="Build", **changes):
        self.config.update(changes)
        self.config_path.write_text(json.dumps(self.config))
        return subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(self.driver), "-Mode", mode],
                              env=self.env, capture_output=True, text=True, timeout=40)

    def answers(self):
        return ["Operator fixture", "Android A / 14", "Android B / 15", "Separate reader fixture", sha(self.apk)] + ["YES"] * 9

    def state(self):
        path = self.root / "native-state.json"
        return json.loads(path.read_text()) if path.exists() else {"builds": 0, "publications": 0, "backend_checks": 0}

    def calls(self):
        path = self.root / "native-calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@unittest.skipUnless(os.name != "nt" and Path(PWSH).is_file(), "Linux PowerShell native-double runtime is not installed")
class RunnerTests(unittest.TestCase):
    def case(self, **config):
        case = Case(**config)
        self.addCleanup(case.close)
        return case

    def ok(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + "\n" + result.stderr)

    def bad(self, result):
        self.assertNotEqual(result.returncode, 0, result.stdout + "\n" + result.stderr)

    def built(self, **config):
        case = self.case(**config)
        self.ok(case.run())
        return case

    def accepted(self, **config):
        case = self.built(**config)
        self.ok(case.run("Accept", answers=case.answers()))
        return case

    def untouched(self, case):
        self.assertEqual(case.old.read_bytes(), case.old_bytes)
        self.assertEqual((case.profile / ".android/debug.keystore").read_bytes(), b"EXISTING_KEY_FIXTURE")
        self.assertEqual((case.repo / "app/android/key.properties").read_text(), "KEY_PROPERTIES_FIXTURE_UNTOUCHED")

    def test_build_is_default_and_never_uses_ssh_or_accepts_devices(self):
        case = self.built()
        self.assertEqual(case.state()["builds"], 1)
        self.assertEqual(case.state()["publications"], 0)
        self.assertFalse(any(call["tool"] in {"ssh.exe", "scp.exe"} for call in case.calls()))
        self.assertTrue(case.receipt.exists())
        self.assertFalse(case.phone.exists())
        self.assertFalse((case.root / "prompts.log").exists())
        self.untouched(case)

    def test_publish_without_build_or_without_phone_receipt_never_builds(self):
        case = self.case()
        self.bad(case.run("Publish"))
        self.assertEqual(case.state()["builds"], 0)
        self.ok(case.run())
        self.bad(case.run("Publish"))
        self.assertEqual(case.state()["builds"], 1)
        self.assertEqual(case.state()["publications"], 0)

    def test_operator_acceptance_is_explicit_and_does_not_publish(self):
        case = self.built()
        answers = case.answers()
        answers[-1] = ""
        self.bad(case.run("Accept", answers=answers))
        self.assertFalse(case.phone.exists())
        self.assertEqual(case.state()["publications"], 0)
        result = case.run("Accept", answers=case.answers())
        self.ok(result)
        self.assertIn("VECHER_APK14_PHONE_ACCEPTED", result.stdout)
        record = json.loads(case.phone.read_bytes())
        self.assertEqual(len(record["checks"]), 9)
        self.assertTrue(all(value is True for value in record["checks"].values()))
        self.assertEqual(record["apk_sha256"], sha(case.apk))
        self.assertEqual(record["source_receipt_sha256"], sha(case.receipt))
        self.assertEqual(case.state()["publications"], 0)

    def test_backend_failure_or_missing_marker_prevents_acceptance_questions(self):
        for change in ({"backend_ready": False}, {"missing_backend_marker": True}):
            with self.subTest(change=change):
                case = self.built()
                self.bad(case.run("Accept", answers=case.answers(), **change))
                self.assertFalse(case.phone.exists())
                self.assertFalse((case.root / "prompts.log").exists())

    def test_success_accept_and_publish_uses_exact_saved_bytes_and_real_receipt_parser(self):
        case = self.accepted()
        saved = {path: path.read_bytes() for path in (case.apk, case.metadata, case.receipt, case.phone)}
        result = case.run("Publish")
        self.ok(result)
        self.assertIn("VECHER_APK14_RELEASE_OK", result.stdout)
        self.assertEqual(case.state()["builds"], 1)
        self.assertEqual(case.state()["publications"], 1)
        self.assertTrue(all(path.read_bytes() == raw for path, raw in saved.items()))
        self.untouched(case)

    def test_disconnect_after_publish_retries_without_building_or_changing_acceptance(self):
        case = self.accepted()
        phone = case.phone.read_bytes()
        self.bad(case.run("Publish", disconnect_after_publish_once=True))
        result = case.run("Publish")
        self.ok(result)
        self.assertIn("FILES_ALREADY_PUBLISHED", result.stdout)
        self.assertEqual(case.state()["builds"], 1)
        self.assertEqual(case.phone.read_bytes(), phone)

    def test_changed_or_wrapped_phone_record_cannot_be_published(self):
        for change in ("array", "false", "hash"):
            with self.subTest(change=change):
                case = self.accepted()
                record = json.loads(case.phone.read_bytes())
                if change == "array":
                    record = [record]
                elif change == "false":
                    record["checks"]["interrupted_upload_retry"] = False
                else:
                    record["apk_sha256"] = "f" * 64
                case.phone.write_text(json.dumps(record))
                before = len(case.calls())
                self.bad(case.run("Publish"))
                self.assertEqual(case.state()["publications"], 0)
                self.assertFalse(any(call["tool"] == "scp.exe" for call in case.calls()[before:]))

    def test_changed_apk_or_metadata_is_refused_before_upload(self):
        for field in ("apk", "metadata"):
            with self.subTest(field=field):
                case = self.accepted()
                path = getattr(case, field)
                path.write_bytes(path.read_bytes() + b" ")
                before = len(case.calls())
                self.bad(case.run("Publish"))
                self.assertFalse(any(call["tool"] == "scp.exe" for call in case.calls()[before:]))
                self.assertEqual(case.state()["publications"], 0)

    def test_source_signer_manifest_and_build_failure_gates_preserve_existing_key(self):
        for change in ({"wrong_old_signer": True}, {"wrong_new_signer": True}, {"wrong_manifest": True},
                       {"build_fail": True}, {"dirty": " M app/pubspec.yaml\n"},
                       {"dirty_after_build": " M app/lib/main.dart\n"}, {"fetch_head": "0" * 40}, {"diverged": True}):
            with self.subTest(change=change):
                case = self.case(**change)
                self.bad(case.run())
                self.assertFalse(case.receipt.exists())
                self.assertEqual(case.state()["publications"], 0)
                self.untouched(case)

    def test_missing_original_key_does_not_create_a_new_one(self):
        case = self.case()
        key = case.profile / ".android/debug.keystore"
        key.unlink()
        self.bad(case.run())
        self.assertFalse(key.exists())
        self.assertEqual(case.state()["builds"], 0)

    def test_upload_failure_and_corruption_never_report_publication(self):
        for change in ({"scp_fail": True}, {"corrupt_upload": True}, {"backend_ready": False}):
            with self.subTest(change=change):
                case = self.accepted()
                self.bad(case.run("Publish", **change))
                self.assertEqual(case.state()["publications"], 0)
                self.assertEqual(case.state()["builds"], 1)

    def test_remote_exit_zero_without_exact_success_markers_is_not_success(self):
        for change in ({"missing_success_marker": True}, {"wrong_success_hash": True}):
            with self.subTest(change=change):
                case = self.accepted()
                self.bad(case.run("Publish", **change))
                self.assertEqual(case.state()["publications"], 1)
                self.assertEqual(case.state()["builds"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
