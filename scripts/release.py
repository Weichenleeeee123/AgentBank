"""Local release gates and source archive. Never removes or overwrites any file."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = {"backend", "frontend", "evals", "docs", "deploy", "scripts", "tests", ".github"}
ROOT_FILES = {
    "README.md",
    "pyproject.toml",
    "requirements.lock",
    "requirements-dev.lock",
    ".env.example",
    ".gitignore",
}
EXCLUDED_PARTS = {"__pycache__", "node_modules", ".pytest_cache", ".mypy_cache", ".ruff_cache"}


def included(path: str) -> bool:
    p = Path(path)
    return (
        (p.parts[0] in SOURCE_DIRS or path in ROOT_FILES)
        and not any(x in EXCLUDED_PARTS or x.endswith(".egg-info") for x in p.parts)
        and (not p.name.startswith(".env") or path == ".env.example")
        and not any(
            p.name.endswith(x)
            for x in (
                ".db",
                ".db-wal",
                ".db-shm",
                ".pyc",
                ".log",
                ".checkpoints.jsonl",
                ".checkpoints.key",
            )
        )
    )


def source_paths() -> list[str]:
    # Both tracked modifications and intended, untracked source are hashed and scanned.
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return sorted(
        p
        for p in set(result.stdout.decode().split("\0"))
        if p and included(p) and (ROOT / p).is_file()
    )


def secret_findings(paths: list[str]) -> list[str]:
    patterns = [
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
        r"\b(?:sk-proj-|sk-live-|ghp_|github_pat_)[A-Za-z0-9_-]{20,}",
        r"\bsk-[A-Za-z0-9]{32,}\b",
        r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",
    ]
    findings = []
    for path in paths:
        content = (ROOT / path).read_text(encoding="utf-8", errors="replace")
        if any(re.search(pattern, content) for pattern in patterns):
            findings.append(path)  # Never emit the matched credential.
    return findings


def gate_commands(out: Path) -> list[tuple[str, list[str]]]:
    frontend_tests = sorted(
        p.relative_to(ROOT).as_posix() for p in (ROOT / "tests/frontend").glob("*.test.mjs")
    )
    if not frontend_tests:
        raise SystemExit("Frontend test suite is empty.")
    syntax_files = sorted(
        p.relative_to(ROOT).as_posix()
        for p in (ROOT / "frontend").rglob("*")
        if p.suffix in {".js", ".mjs"} and "node_modules" not in p.parts
    )
    commands = [
        ("dependencies", [sys.executable, "-m", "pip", "check"]),
        ("ruff", [sys.executable, "-m", "ruff", "check", "backend", "scripts", "tests"]),
        ("mypy", [sys.executable, "-m", "mypy", "backend/agentbank"]),
        ("python-tests", [sys.executable, "-m", "pytest", "backend/tests", "tests"]),
        ("frontend-tests", ["node", "--test", *frontend_tests]),
    ]
    commands.extend(
        ("syntax-" + path.replace("/", "-"), ["node", "--check", path]) for path in syntax_files
    )
    commands.append(
        ("eval-and-http", [sys.executable, "scripts/verify_evals.py", "--output", str(out)])
    )
    return commands


def run_checks(out: Path) -> list[dict[str, object]]:
    commands = gate_commands(out)
    inventory = [
        {"name": name, "command": command, "status": "pending"} for name, command in commands
    ]
    inventory_path = out / "gates.json"
    inventory_path.write_text(json.dumps(inventory, indent=2), encoding="utf-8")
    for index, (name, command) in enumerate(commands):
        print(f"Running local gate: {name}", flush=True)
        result = subprocess.run(
            command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
        evidence_name = f"check-{index + 1:02d}-{name}.txt"
        (out / evidence_name).write_text(result.stdout + result.stderr, encoding="utf-8")
        inventory[index].update(
            status="passed" if result.returncode == 0 else "failed",
            returncode=result.returncode,
            evidence=evidence_name,
        )
        inventory_path.write_text(json.dumps(inventory, indent=2), encoding="utf-8")
        if result.returncode:
            raise SystemExit(f"Gate failed: {command[:3]}; see {out}")
    return inventory


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--package", action="store_true", help="Run all mandatory local gates and create an archive"
    )
    args = parser.parse_args()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    out = ROOT / ".tmp" / "releases" / stamp
    out.mkdir(parents=True, exist_ok=False)
    paths = source_paths()
    findings = secret_findings(paths)
    if findings:
        raise SystemExit("Potential secret in source (values hidden): " + ", ".join(findings))
    snapshot = {p: (ROOT / p).read_bytes() for p in paths}
    before = {p: hashlib.sha256(content).hexdigest() for p, content in snapshot.items()}
    gates = run_checks(out)
    after = {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in source_paths()}
    if before != after:
        raise SystemExit("Source changed during verification. Rerun when edits are complete.")
    manifest = {
        "created_at": stamp,
        "python": sys.version,
        "git_head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "files": before,
        "source_digest": hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest(),
        "local_gates": "passed",
        "gate_evidence": gates,
        "external_gates": {
            "docker": "not run",
            "public_deployment": "not requested",
            "live_llm": "not run",
        },
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    if args.package:
        with zipfile.ZipFile(
            out / "agentbank-m4-source.zip", mode="x", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            for path in paths:
                archive.writestr(path, snapshot[path])
            archive.write(out / "manifest.json", "release-manifest.json")
    print(f"Local gates passed; evidence: {out}")


if __name__ == "__main__":
    main()
