"""A whitelist package, never a recursive archive of a user's workspace."""
import hashlib
import html
import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from resetnotice import __version__
from privacy_audit import audit as privacy_audit

VERSION = __version__
EXECUTABLE = "CodexResetNotification"
PREFIX = "Codex-Reset-Notification-" + VERSION


def owned(path):
    resolved = path.resolve()
    if not resolved.is_relative_to(ROOT):
        raise RuntimeError("Build output escaped project directory")
    return resolved


def source_files():
    names = ("app.py", "requirements.txt", "requirements-build.txt", ".gitignore",
             "README.md", "LICENSING.md", "THIRD_PARTY_NOTICES.md", "build.ps1",
             "start-source.cmd", "run-tests.cmd")
    result = [ROOT / name for name in names]
    for folder, pattern in (("resetnotice", "*.py"), ("tests", "*.py"), ("scripts", "*.py"), ("docs", "*.md")):
        result.extend(sorted((ROOT / folder).glob(pattern)))
    return result


def guide_html():
    text = (ROOT / "docs" / "中文用户指南.md").read_text(encoding="utf-8")
    return ("<!doctype html><html lang='zh-CN'><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
            "<title>Codex Reset Notification 中文用户指南</title><style>body{max-width:960px;margin:40px auto;padding:0 24px;"
            "font:16px/1.9 'Microsoft YaHei UI',sans-serif;color:#22334b;background:#f6f8fc}pre{white-space:pre-wrap;"
            "font:inherit;background:white;padding:30px;border-radius:14px}a{color:#235cce}</style>"
            "<h1>Codex Reset Notification · 使用指南</h1><p>Data: <a href='https://codex-reset.com/'>codex-reset.com</a> · "
            "只监测公共公告。首次启动保持停止。</p><pre>" + html.escape(text) + "</pre></html>")


def embedded_tk_resources():
    # Python 3.14's Tcl/Tk 9 DLLs embed ZIPs. Explicit scripts also work when
    # a frozen bootloader does not initialize Tcl's automatic ZIP mount.
    arguments = []
    resources = owned(ROOT / "build" / "tk-resources")
    for role, expected in (("tcl", "tcl_library/init.tcl"), ("tk", "tk_library/tk.tcl")):
        found = False
        for dll in (Path(sys.base_prefix) / "DLLs").glob("*tcl*.dll"):
            if not zipfile.is_zipfile(dll):
                continue
            with zipfile.ZipFile(dll) as archive:
                if expected not in archive.namelist():
                    continue
                destination = owned(resources / ("_" + role + "_data"))
                destination.mkdir(parents=True, exist_ok=True)
                for name in archive.namelist():
                    prefix = role + "_library/"
                    if not name.startswith(prefix) or name.endswith("/"):
                        continue
                    relative = Path(name[len(prefix):])
                    target = owned(destination / relative)
                    if not target.is_relative_to(destination):
                        raise RuntimeError("Embedded Tcl/Tk archive path escaped destination")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(archive.read(name))
                arguments += ["--add-data", str(destination) + os.pathsep + ("_" + role + "_data")]
                found = True
                break
        if not found:
            # Conventional Tcl/Tk 8 builds are covered by PyInstaller's standard hook.
            continue
    return arguments


def licenses(binary):
    destination = binary / "licenses"
    destination.mkdir(exist_ok=True)
    copied = []
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if python_license.exists():
        shutil.copyfile(python_license, destination / "CPython-LICENSE.txt")
        copied.append("CPython-LICENSE.txt")
    for path in (Path(sys.base_prefix) / "tcl").rglob("license.terms"):
        name = path.parent.name + "-license.terms"
        shutil.copyfile(path, destination / name)
        copied.append(name)
    for role in ("tcl", "tk"):
        path = binary / "_internal" / ("_" + role + "_data") / "license.terms"
        if path.exists():
            name = role + "-license.terms"
            shutil.copyfile(path, destination / name)
            copied.append(name)
    for package in ("tzdata", "pyinstaller"):
        distribution = importlib.metadata.distribution(package)
        for file in distribution.files or []:
            name = str(file).lower()
            if "dist-info" in name and ("license" in name or "copying" in name) and distribution.locate_file(file).is_file():
                label = package + "-" + Path(file).name
                shutil.copyfile(distribution.locate_file(file), destination / label)
                copied.append(label)
    (destination / "THIRD_PARTY_NOTICES.md").write_text((ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8"), encoding="utf-8")
    shutil.copyfile(ROOT / "LICENSING.md", destination / "LICENSING.md")
    copied.append("LICENSING.md")
    return copied


def dependency_audit(binary):
    import pefile
    packaged = {path.name.lower() for path in binary.rglob("*") if path.is_file()}
    missing, external, runtimes = [], set(), set()
    for path in binary.rglob("*"):
        if path.suffix.lower() not in (".exe", ".dll", ".pyd"):
            continue
        pe = pefile.PE(str(path), fast_load=True)
        pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"], pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT"]])
        for entry in getattr(pe, "DIRECTORY_ENTRY_IMPORT", []) + getattr(pe, "DIRECTORY_ENTRY_DELAY_IMPORT", []):
            name = entry.dll.decode("ascii").lower()
            if name in packaged:
                continue
            if name.startswith(("api-ms-win-", "ext-ms-win-")) or (Path(os.environ["SystemRoot"]) / "System32" / name).exists():
                external.add(name)
            else:
                missing.append([path.name, name])
            if name.startswith(("vcruntime", "msvcp")):
                runtimes.add(name)
        pe.close()
    exe = pefile.PE(str(binary / (EXECUTABLE + ".exe")))
    manifest = ""
    for entry in exe.DIRECTORY_ENTRY_RESOURCE.entries:
        if entry.id == 24:
            for item in entry.directory.entries:
                for language in item.directory.entries:
                    blob = language.data.struct
                    manifest += exe.get_data(blob.OffsetToData, blob.Size).decode("utf-8")
    architecture = "x64" if exe.FILE_HEADER.Machine == 0x8664 else str(exe.FILE_HEADER.Machine)
    exe.close()
    return {"ok": not missing and not runtimes and "asInvoker" in manifest and architecture == "x64",
            "missing_imports": missing, "external_vc_runtime": sorted(runtimes),
            "system_dependencies": sorted(external), "manifest_as_invoker": "asInvoker" in manifest,
            "architecture": architecture, "physical_standard_user_test": False}


def main():
    os.chdir(ROOT)
    if os.name != "nt" or platform.machine().upper() not in ("AMD64", "X86_64"):
        raise RuntimeError("Windows x64 is required to build this release")
    checks = owned(ROOT / "build" / "checks")
    release = owned(ROOT / "release")
    binary = owned(ROOT / "dist" / EXECUTABLE)
    checks.mkdir(parents=True, exist_ok=True)
    release.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, str(ROOT / "scripts" / "check.py")], check=True)
    version_file = owned(ROOT / "build" / "windows-version.txt")
    version_numbers = str(tuple(int(part) for part in VERSION.split(".")) + (0,))
    version_file.write_text("VSVersionInfo(ffi=FixedFileInfo(filevers=" + version_numbers + ", prodvers=" + version_numbers + ", "
        "mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0,0)), "
        "kids=[StringFileInfo([StringTable('040904B0', [StringStruct('FileDescription', 'Codex Reset Notification'), "
        "StringStruct('FileVersion', '" + VERSION + "'), StringStruct('ProductName', 'Codex Reset Notification'), "
        "StringStruct('ProductVersion', '" + VERSION + "'), "
        "StringStruct('OriginalFilename', 'CodexResetNotification.exe')])]), "
        "VarFileInfo([VarStruct('Translation', [1033,1200])])])", encoding="utf-8")
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--onedir", "--windowed", "--noupx",
               "--name", EXECUTABLE, "--version-file", str(version_file), "--collect-all", "tzdata", "--distpath", str(ROOT / "dist"),
               "--workpath", str(ROOT / "build" / "pyinstaller"), "--specpath", str(ROOT / "build"), str(ROOT / "app.py")]
    command[4:4] = embedded_tk_resources()
    with (ROOT / "build" / "pyinstaller.txt").open("w", encoding="utf-8") as log:
        process = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
    if process.returncode:
        raise RuntimeError("PyInstaller failed; see build/pyinstaller.txt")
    guide = guide_html()
    (binary / "中文用户指南.html").write_text(guide, encoding="utf-8")
    copied_licenses = licenses(binary)
    frozen_command = [str(binary / (EXECUTABLE + ".exe")), "--demo", "--data-dir",
                      str(checks / ("frozen-runtime-" + str(time.time_ns()))), "--smoke-report", str(checks / "frozen-ui-smoke.json")]
    subprocess.run(frozen_command, check=True, timeout=60)
    smoke = json.loads((checks / "frozen-ui-smoke.json").read_text(encoding="utf-8"))
    if not smoke["ok"] or not smoke["frozen"] or smoke["monitoring"]:
        raise RuntimeError("Frozen GUI smoke failed")
    dependencies = dependency_audit(binary)
    (checks / "dependency-audit.json").write_text(json.dumps(dependencies, indent=2), encoding="utf-8")
    if not dependencies["ok"]:
        raise RuntimeError("Missing portable dependency, x64 architecture or ordinary-user manifest")
    reports = [checks / name for name in ("test-results.json", "test-results.txt", "source-ui-smoke.json", "frozen-ui-smoke.json", "dependency-audit.json")]
    privacy = privacy_audit(source_files() + reports, binary, EXECUTABLE + ".exe")
    (checks / "privacy-audit.json").write_text(json.dumps(privacy, ensure_ascii=False, indent=2), encoding="utf-8")
    if not privacy["ok"]:
        raise RuntimeError("Publish privacy check failed; see build/checks/privacy-audit.json")
    entries = []
    for path in sorted(binary.rglob("*")):
        if path.is_file():
            entries.append((path, PREFIX + "/Windows/" + path.relative_to(binary).as_posix()))
    for path in source_files():
        entries.append((path, PREFIX + "/source/" + path.relative_to(ROOT).as_posix()))
    for name in ("test-results.json", "test-results.txt", "source-ui-smoke.json", "frozen-ui-smoke.json", "dependency-audit.json", "privacy-audit.json"):
        entries.append((checks / name, PREFIX + "/验证记录/" + name))
    forbidden = ("config.json", ".env", "state.sqlite3", "transfer.json")
    for path, name in entries:
        if path.name in forbidden or path.suffix in (".sqlite3", ".log", ".key", ".pem") or "runtime/" in name:
            raise RuntimeError("Runtime/private path in package")
        if "/source/tests/" not in name and "/source/scripts/" not in name and path.suffix in (".py", ".json", ".txt"):
            if b"fictional-test-secret-DO-NOT-LOG" in path.read_bytes():
                raise RuntimeError("Test credential escaped tests")
    audit = {"ok": True, "source_whitelist": True, "runtime_state_included": False,
             "user_credentials_included": False, "default_accounts_included": False,
             "native_startup_changes": 0, "real_notifications": 0,
             "licenses": copied_licenses, "file_count": len(entries) + 5,
             "target": "Windows 10/11 x64", "cross_physical_computer_test": False,
             "unsigned": True}
    (checks / "package-audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    archive = release / (PREFIX + "-Windows-x64.zip")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as package:
        for path, name in entries:
            package.write(path, name)
        package.write(checks / "package-audit.json", PREFIX + "/验证记录/package-audit.json")
        package.writestr(PREFIX + "/中文用户指南.html", guide)
        package.writestr(PREFIX + "/启动.cmd", '@echo off\r\nstart "" "%~dp0Windows\\CodexResetNotification.exe"\r\n')
        package.writestr(PREFIX + "/离线演示.cmd", '@echo off\r\nstart "" "%~dp0Windows\\CodexResetNotification.exe" --demo\r\n')
    with zipfile.ZipFile(archive) as package:
        if package.testzip():
            raise RuntimeError("ZIP integrity error")
    # Portable check under a new path with Chinese and spaces, without any installed Python path.
    extracted = owned(ROOT / "build" / ("其他电脑模拟 空白目录 " + str(time.time_ns())))
    extracted.mkdir()
    with zipfile.ZipFile(archive) as package:
        package.extractall(extracted)
    env = os.environ.copy()
    for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "TCL_LIBRARY", "TK_LIBRARY"):
        env.pop(key, None)
    env["PATH"] = str(Path(os.environ["SystemRoot"]) / "System32") + ";" + os.environ["SystemRoot"]
    portable_report = checks / "portable-clean-smoke.json"
    portable = extracted / PREFIX / "Windows" / (EXECUTABLE + ".exe")
    subprocess.run([str(portable), "--data-dir", str(extracted / "独立用户数据"),
                    "--smoke-report", str(portable_report)], env=env, cwd=extracted, check=True, timeout=60)
    portability = json.loads(portable_report.read_text(encoding="utf-8"))
    if not portability["ok"] or not portability["frozen"] or portability["monitoring"]:
        raise RuntimeError("Clean extracted portable GUI smoke failed")
    portability.update(installed_python_path_removed=True, source_directory_on_path=False,
                       chinese_and_spaces_path=True, clean_data_directory=True, physical_second_pc=False)
    portable_report.write_text(json.dumps(portability, ensure_ascii=False, indent=2), encoding="utf-8")
    with zipfile.ZipFile(archive, "a", zipfile.ZIP_DEFLATED) as package:
        package.write(portable_report, PREFIX + "/验证记录/portable-clean-smoke.json")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(archive.suffix + ".sha256.txt").write_text(digest + "  " + archive.name + "\n", encoding="ascii")
    print(json.dumps({"ok": True, "archive": str(archive), "bytes": archive.stat().st_size, "sha256": digest,
                      "portable_without_python": True, "physical_second_pc": False}, ensure_ascii=True))


if __name__ == "__main__":
    main()
