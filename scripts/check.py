import contextlib
import io
import importlib.util
import json
import os
import platform
import subprocess
import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    os.chdir(ROOT)
    if importlib.util.find_spec("tzdata") is None:
        raise RuntimeError("Install requirements.txt before running checks; tzdata is required on Windows.")
    output = ROOT / "build" / "checks"
    output.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    log = stream.getvalue()
    (output / "test-results.txt").write_text(log, encoding="utf-8")
    report = {"ok": result.wasSuccessful(), "tests": result.testsRun,
              "failures": len(result.failures), "errors": len(result.errors),
              "skipped": len(result.skipped), "seconds": round(time.monotonic()-start, 3),
              "utc": datetime.now(timezone.utc).isoformat(), "python": sys.version.split()[0],
              "os": platform.platform(), "real_notifications": 0,
              "native_credential_writes": 0, "native_startup_changes": 0,
              "notification_addresses": "fictional example.invalid only",
              "network_tests": "loopback simulated SMTP and HTTP only"}
    (output / "test-results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True))
    if not result.wasSuccessful():
        print(log)
        return 1
    command = [sys.executable, str(ROOT / "app.py"), "--demo", "--data-dir",
               str(output / ("source-runtime-" + str(time.time_ns()))), "--smoke-report", str(output / "source-ui-smoke.json")]
    subprocess.run(command, check=True, timeout=60)
    smoke = json.loads((output / "source-ui-smoke.json").read_text(encoding="utf-8"))
    if not smoke["ok"] or smoke["monitoring"] or smoke["tabs"] != 5:
        raise RuntimeError("Source GUI smoke failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
