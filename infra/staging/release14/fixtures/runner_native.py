#!/usr/bin/env python3
"""Local native-command doubles. No network, Android SDK, signing key or Docker."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sys
import zipfile

config_path = Path(os.environ["VECHER_APK14_MOCK_CONFIG"])
config = json.loads(config_path.read_text())
root = config_path.parent
state_path = root / "native-state.json"
state = json.loads(state_path.read_text()) if state_path.exists() else {
    "head": "713b6b34819a6df382da03d5213135986d7c0c0c", "builds": 0, "publications": 0, "backend_checks": 0}
args = sys.argv[1:]
name = Path(sys.argv[0]).name.lower()
with (root / "native-calls.jsonl").open("a") as out:
    out.write(json.dumps({"tool": name, "args": args}) + "\n")


def save():
    state_path.write_text(json.dumps(state))


def fail(message, code=1):
    print(message, file=sys.stderr)
    raise SystemExit(code)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def make_apk(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as archive:
        for entry in ("libflutter.so", "libapp.so", "libjingle_peerconnection_so.so"):
            archive.writestr(zipfile.ZipInfo("lib/arm64-v8a/" + entry, (2026, 10, 23, 0, 0, 0)), b"candidate14-" + entry.encode())


if name == "git":
    if args[:1] == ["-C"]:
        args = args[2:]
    if args == ["rev-parse", "--is-inside-work-tree"]:
        print("true")
    elif args == ["rev-parse", "--show-toplevel"]:
        print(config["repo"])
    elif args == ["rev-parse", "HEAD"]:
        print(state["head"])
    elif args == ["rev-parse", "FETCH_HEAD"]:
        print(config.get("fetch_head", config["target"]))
    elif args == ["config", "--get", "remote.origin.url"]:
        print(config.get("origin", "git@github.com:ais0930271103-glitch/vecher.git"))
    elif args == ["status", "--porcelain"]:
        print(config.get("dirty_after_build" if state["builds"] else "dirty", ""), end="")
    elif args[:3] == ["fetch", "--no-tags", "origin"]:
        assert args[3:] == [config["target"]], args
    elif args[:2] == ["merge-base", "--is-ancestor"]:
        if config.get("diverged"):
            fail("MOCK_DIVERGED")
    elif args[:2] == ["switch", "--detach"]:
        assert args[2] == config["target"]
        state["head"] = args[2]
        save()
    else:
        fail("UNEXPECTED_GIT " + repr(args), 99)
elif name == "flutter.bat":
    if args[:2] == ["build", "apk"]:
        state["builds"] += 1
        save()
        if config.get("build_fail"):
            fail("MOCK_BUILD_FAIL")
        assert args[args.index("--build-name") + 1] == "0.9.0"
        assert args[args.index("--build-number") + 1] == "14"
        assert "android-arm64" in args and "--release" in args
        make_apk(Path(config["repo"]) / "app/build/app/outputs/flutter-apk/app-release.apk")
    elif args[:2] == ["pub", "get"] or args[:1] in (["analyze"], ["test"]):
        pass
    else:
        fail("UNEXPECTED_FLUTTER " + repr(args), 99)
elif name == "apksigner.bat":
    assert args[:1] == ["verify"] and "--print-certs" in args
    old = Path(args[-1]).name == "vecher-0.8.0-13.apk"
    digest = "bf3d541231287a7e37629088f470d82079d6b1577d0692cceeb8597779d520c1"
    if config.get("wrong_old_signer") and old or config.get("wrong_new_signer") and not old:
        digest = "0" * 64
    print("Verifies\nSigner #1 certificate SHA-256 digest: " + digest)
elif name == "aapt.exe":
    assert args[:2] == ["dump", "badging"]
    old = Path(args[-1]).name == "vecher-0.8.0-13.apk"
    package = "wrong.package" if config.get("wrong_manifest") and not old else "online.vecher.vecher_app"
    print("package: name='%s' versionCode='%s' versionName='%s'" % (package, "13" if old else "14", "0.8.0" if old else "0.9.0"))
elif name == "ssh.exe":
    if args[:1] == ["-G"]:
        assert args == ["-G", "vecher"]
        print("user root\nhostname " + config.get("ssh_host", "94.228.166.214") + "\nport 22")
    else:
        position = args.index("vecher")
        assert len(args) == position + 2, ("REMOTE_COMMAND_SPLIT", args)
        command = args[position + 1]
        assert "\n" not in command and "\r" not in command
        stages = re.findall(r"/opt/vecher-test/downloads/\.release-14-[a-f0-9]{32}", command)
        assert stages, ("UNSAFE_STAGE", command)
        stage = root / "remote" / Path(stages[0]).name
        if "mkdir -m 700" in command:
            stage.mkdir(parents=True, mode=0o700, exist_ok=False)
            print("UPLOAD_STAGE_READY")
        elif "python3 ./vecher-publish-apk14.py" in command:
            helper = stage / "vecher-publish-apk14.py"
            helper_pin = re.search(r"'([a-f0-9]{64})' 'vecher-publish-apk14.py'", command)
            assert helper_pin and helper_pin[1] == sha(helper), "HELPER_HASH_MISMATCH"
            if "--check-backend" in command:
                state["backend_checks"] += 1
                save()
                if not config.get("backend_ready", True):
                    fail("MOCK_BACKEND_NOT_READY")
                if not config.get("missing_backend_marker"):
                    print("BLOGS_BACKEND_READY=0.9.0 (14)")
                print("BACKEND_SOURCE_SHA=" + config["target"])
                print("BACKEND_RUNTIME_SHA256=520fb35ae85b4778f584fd4023d048fb69ad7b9afffcaba3a6d750b9fb1b0618")
                print("BACKEND_SCHEMA=0006_public_blogs\nBACKEND_API_VERSION=0.9.0")
            else:
                if not config.get("backend_ready", True):
                    fail("MOCK_BACKEND_NOT_READY")
                bindings = {}
                for key in ("sha256", "size", "metadata-sha256", "source-receipt-sha256", "phone-receipt-sha256"):
                    match = re.search(r"--expected-" + key + r"\s+'?([a-f0-9]+)'?", command)
                    assert match, ("MISSING_BINDING", key)
                    bindings[key] = match[1]
                candidate = stage / "vecher-0.9.0-14.apk"
                metadata = stage / "vecher-0.9.0-14.version.json"
                if candidate.stat().st_size != int(bindings["size"]) or sha(candidate) != bindings["sha256"]:
                    fail("MOCK_UPLOAD_HASH_MISMATCH")
                # Execute the real pure receipt validator on the bytes produced
                # by PowerShell. Network/Docker publication remains a double.
                spec = importlib.util.spec_from_file_location("publisher_receipt_fixture", helper)
                module = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = module
                spec.loader.exec_module(module)
                pair = module.pair_at(stage, candidate.name, metadata.name)
                module.verify_release_declarations(stage, pair, metadata_sha256=bindings["metadata-sha256"],
                                                    source_receipt_sha256=bindings["source-receipt-sha256"],
                                                    phone_receipt_sha256=bindings["phone-receipt-sha256"])
                published = {"sha": sha(candidate), "metadata": sha(metadata)}
                prior = state.get("published")
                if prior and prior != published:
                    fail("MOCK_DIFFERENT14_REJECTED")
                state["published"] = published
                state["publications"] += 1
                save()
                if config.get("disconnect_after_publish_once") and state["publications"] == 1:
                    fail("MOCK_SSH_LOST_AFTER_PUBLISH", 255)
                print("UPLOAD_HASH_OK\nPHONE_ACCEPTANCE_RECORD_OK\nPUBLIC_BLOGS_BACKEND_OK")
                print(("FILES_ALREADY_PUBLISHED" if prior else "FILES_PUBLISHED") + "=0.9.0 (14)")
                print("API_VERSION_OK=0.9.0 (14)")
                print("API_APK_SHA256=" + ("0" * 64 if config.get("wrong_success_hash") else sha(candidate)))
                if not config.get("missing_success_marker"):
                    print("VECHER_UPDATE_PUBLISHED_OK")
        else:
            fail("UNEXPECTED_SSH " + command, 99)
elif name == "scp.exe":
    assert "--" in args and args[-1].startswith("vecher:/opt/vecher-test/downloads/.release-14-")
    if config.get("scp_fail"):
        fail("MOCK_SCP_FAIL")
    sources = args[args.index("--") + 1:-1]
    stage = root / "remote" / Path(args[-1].removeprefix("vecher:").rstrip("/")).name
    assert stage.is_dir() and len(sources) in (1, 5)
    for source in sources:
        shutil.copyfile(source, stage / Path(source).name)
    if config.get("corrupt_upload") and len(sources) == 5:
        (stage / "vecher-0.9.0-14.apk").write_bytes(b"corrupted-upload")
else:
    fail("UNEXPECTED_NATIVE " + name, 99)
