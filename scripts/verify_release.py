"""Build a source-only ZIP, install into a fresh venv, verify CLI + tests.

No real model requests. Outputs under dist/, .local/ and verification/ only.
The package includes reviewed source/documentation/evidence, never live state.
"""
import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
import time
import uuid
import zipfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIRECTORIES = ("agent", "data", "tests", "docs", "scripts", "verification")
FILES = ("README.md", "CONTRACT.md", "requirements.txt", "requirements-lock.txt", ".env.example", ".gitignore")
LOCAL_ONLY_FILES = {"docs/learning-progress.md"}


def command(args, cwd, *, input=None, timeout=120):
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    # Offline verification never needs to propagate a credential into subprocesses.
    env.pop("AGENT_API_KEY", None)
    return subprocess.run(args, cwd=cwd, env=env, input=input, text=True, encoding="utf-8",
                          capture_output=True, timeout=timeout)


def selected_files():
    selected = [ROOT / name for name in FILES]
    for directory in DIRECTORIES:
        selected.extend(p for p in (ROOT / directory).rglob("*")
                        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
                        and p.relative_to(ROOT).as_posix() not in LOCAL_ONLY_FILES)
    return sorted(selected)


def build_zip(path):
    selected = selected_files()
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as package:
        for file in selected:
            package.write(file, "interview-solution/" + file.relative_to(ROOT).as_posix())
    return len(selected)


def main():
    out = ROOT / "verification"
    out.mkdir(exist_ok=True)
    stamp = datetime.now(timezone(timedelta(hours=8))).isoformat()
    start = time.monotonic()
    unit = command([sys.executable, "-X", "utf8", "-m", "unittest", "discover", "-s", "tests", "-v"], ROOT)
    transcript = unit.stdout + unit.stderr
    (out / "unit-tests.txt").write_text(transcript, encoding="utf-8")
    unit_meta = {"kind": "offline_tests", "date": stamp, "python": platform.python_version(),
                 "jsonschema": importlib.metadata.version("jsonschema"), "exit_code": unit.returncode,
                 "real_api_calls": 0, "command": "python -X utf8 -m unittest discover -s tests -v",
                 "duration_seconds": round(time.monotonic() - start, 3)}
    (out / "unit-tests.json").write_text(json.dumps(unit_meta, indent=2) + "\n", encoding="utf-8")
    if unit.returncode:
        print("Local tests failed; see verification/unit-tests.txt")
        return 1
    print("Source tests passed.", flush=True)
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    archive = dist / "interview-solution.zip"
    count = build_zip(archive)
    directory = ROOT / ".local" / "reproduction" / uuid.uuid4().hex
    directory.mkdir(parents=True)
    with zipfile.ZipFile(archive) as package:
        for member in package.namelist():
            if not (directory / member).resolve().is_relative_to(directory.resolve()):
                raise ValueError("Unexpected archive member path")
        package.extractall(directory)
    extracted = directory / "interview-solution"
    create = command([sys.executable, "-m", "venv", ".venv"], extracted)
    python = extracted / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if create.returncode:
        print("Fresh venv creation failed.")
        return 1
    install = command([str(python), "-m", "pip", "install", "--disable-pip-version-check", "-r", "requirements-lock.txt"], extracted)
    if install.returncode:
        (out / "reproduction-install.txt").write_text(install.stdout + install.stderr, encoding="utf-8")
        print("Fresh install failed; see reproduction-install.txt")
        return 1
    print("Fresh venv dependencies installed.", flush=True)
    tests = command([str(python), "-X", "utf8", "-m", "unittest", "discover", "-s", "tests", "-q"], extracted)
    cli_input = "计算 7+8\n搜索 Agent\n文档 session\n/switch b\n回顾\n/switch window-1\n回顾\n/quit\n"
    cli = command([str(python), "-X", "utf8", "-m", "agent", "--demo"], extracted, input=cli_input)
    restart = command([str(python), "-X", "utf8", "-m", "agent", "--demo", "--ask", "回顾"], extracted)
    histories = [line.split("本会话可见的用户历史：", 1)[1] for line in cli.stdout.splitlines()
                 if "本会话可见的用户历史：" in line]
    # Verification records change below; code, fixtures and docs must stay equal
    # to the independently extracted copy that was actually tested.
    source_files = [p for p in selected_files() if p.relative_to(ROOT).parts[0] != "verification"]
    source_matches = all((extracted / p.relative_to(ROOT)).is_file()
                         and p.read_bytes() == (extracted / p.relative_to(ROOT)).read_bytes()
                         for p in source_files)
    checks = {"fresh_install": True, "extracted_tests_passed": tests.returncode == 0,
              "interactive_cli_passed": cli.returncode == 0 and '"value":15' in cli.stdout
                  and "mock-local-corpus" in cli.stdout and '"document_id":"session"' in cli.stdout,
              "session_switch_passed": len(histories) == 2 and histories[0] == "[]"
                  and all(text in histories[1] for text in ("计算 7+8", "搜索 Agent", "文档 session")),
              "restart_history_passed": restart.returncode == 0 and "计算 7+8" in restart.stdout,
              "final_source_matches_tested_extraction": source_matches}
    evidence = {"kind": "clean_zip_reproduction_offline", "date": stamp, "python": platform.python_version(),
                "real_api_calls": 0, "archive_file_count": count, "checks": checks,
                "passed": all(checks.values()), "compared_source_files": len(source_files),
                "test_output": tests.stdout + tests.stderr,
                "cli_output": cli.stdout.replace(str(extracted.resolve()), "<extracted-project>"),
                "restart_output": restart.stdout.replace(str(extracted.resolve()), "<extracted-project>")}
    (out / "reproduction.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # Add the observed evidence; source files are unchanged from the tested ZIP.
    count = build_zip(archive)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (dist / "SHA256.txt").write_text(f"{digest}  interview-solution.zip\n", encoding="ascii")
    print(f"{'PASS' if evidence['passed'] else 'FAIL'}: {count} packaged files; no real API calls.")
    return 0 if evidence["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
