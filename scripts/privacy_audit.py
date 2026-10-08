"""Publish only whitelisted files; reports never echo matching secret text."""
import io
import json
import marshal
import re
import types
import zipfile
from pathlib import Path

USER_PATH = re.compile(r"(?:[A-Za-z]:[\\/]Users[\\/]|[A-Za-z]:[\\/]Documents and Settings[\\/]|/(?:Users|home)/)[A-Za-z0-9_. -]+", re.I)
TOKEN = re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|sk-[A-Za-z0-9_-]{32,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
FORBIDDEN = {"config.json", "state.sqlite3", "transfer.json", "交付记录.json", ".env"}


def audit(source, binary, executable):
    from PyInstaller.archive.readers import CArchiveReader
    issues, stats = [], {"source_files": 0, "binary_files": 0, "embedded_code_objects": 0}

    def issue(name, kind):
        row = {"file": name, "kind": kind}
        if row not in issues:
            issues.append(row)

    def text_checks(value, name, project=False):
        if USER_PATH.search(value):
            issue(name, "absolute_user_path")
        if TOKEN.search(value):
            issue(name, "credential_pattern")
        if project and any(not address.lower().endswith(".invalid") for address in EMAIL.findall(value)):
            issue(name, "non_fictional_project_email")

    def bytes_checks(data, name, project=False):
        text_checks(data.decode("utf-8", errors="ignore"), name, project)
        for offset in (0, 1):
            text_checks(data[offset:].decode("utf-16-le", errors="ignore"), name)

    def code_checks(code, name, project=False):
        if not isinstance(code, types.CodeType):
            return
        stats["embedded_code_objects"] += 1
        if re.match(r"^[A-Za-z]:[\\/]|^/", code.co_filename):
            issue(name, "absolute_code_filename")
        text_checks(code.co_filename, name)
        for value in code.co_consts:
            if isinstance(value, types.CodeType):
                code_checks(value, name, project)
            elif isinstance(value, str):
                text_checks(value, name, project)
            elif isinstance(value, bytes):
                bytes_checks(value, name)

    for path in source:
        name = path.name
        stats["source_files"] += 1
        if name in FORBIDDEN or path.suffix in (".log", ".key", ".pem", ".sqlite3"):
            issue(name, "private_or_runtime_file")
        bytes_checks(path.read_bytes(), name, True)

    for path in binary.rglob("*"):
        if not path.is_file():
            continue
        name = path.relative_to(binary).as_posix()
        stats["binary_files"] += 1
        if path.name in FORBIDDEN or path.suffix in (".log", ".key", ".pem", ".sqlite3"):
            issue(name, "private_or_runtime_file")
        data = path.read_bytes()
        bytes_checks(data, name)
        if zipfile.is_zipfile(io.BytesIO(data)):
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                for member in archive.namelist():
                    content = archive.read(member)
                    label = name + "::" + member
                    bytes_checks(content, label)
                    if member.endswith(".pyc"):
                        code_checks(marshal.loads(content[16:]), label)

    archive = CArchiveReader(str(binary / executable))
    for name, entry in archive.toc.items():
        if entry[-1] == "s":
            code_checks(marshal.loads(archive.extract(name)), "exe::" + name, name == "app")
    embedded = archive.open_embedded_archive("PYZ.pyz")
    for name in embedded.toc:
        code_checks(embedded.extract(name), "pyz::" + name, name.startswith("resetnotice"))
    return {"ok": not issues, "issues": issues, **stats,
            "source_emails": "fictional .invalid only", "runtime_files_included": False,
            "scanner_is_not_a_security_proof": True,
            "third_party_attribution_emails_retained": True}


if __name__ == "__main__":
    from build_release import ROOT, source_files
    result = audit(source_files(), ROOT / "dist" / "CodexResetNotification", "CodexResetNotification.exe")
    print(json.dumps(result, ensure_ascii=True, indent=2))
    raise SystemExit(0 if result["ok"] else 1)
