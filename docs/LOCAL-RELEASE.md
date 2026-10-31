# Local M4 candidate

Use Python 3.12 or newer and Node.js. The exact dependency set was cleanly
resolved and installed on Python 3.13 / Windows. Linux image build is a separate
gate; do not describe Docker or public deployment as verified by local tests.

Create a new directory name for each clean environment. Never remove an old one:

```powershell
python -m venv .tmp/m4-review-001
.tmp/m4-review-001/Scripts/python.exe -m pip install -r requirements-dev.lock
.tmp/m4-review-001/Scripts/python.exe -m pip install --no-deps -e .
.tmp/m4-review-001/Scripts/python.exe -m pip check
```

Start the fully enabled eval instance (loopback only):

```powershell
./scripts/Start-Local.ps1 -Python .tmp/m4-review-001/Scripts/python.exe
```

Open `http://127.0.0.1:8001/app/`. The helper generates persistent independent
session, audit, admin and staff secrets in ignored `.tmp/local-demo/eval.env`.
It safely loads `.env` as literal assignments for optional provider settings,
then selects the eval database in `.tmp/local-demo/eval.db`. No shell expansion
or credential output occurs. Keep the local credentials and audit anchors with
the database between restarts. Public mode uses its own database/credentials and
explicitly disables admin and staff API tokens:

```powershell
./scripts/Start-Local.ps1 -Mode public -Python .tmp/m4-review-001/Scripts/python.exe
```

In another terminal, pair the simulated phone for demo user `user_001`:

```powershell
./scripts/New-DevicePairing.ps1
```

Enter the returned one-time code at `http://127.0.0.1:8001/app/device.html`.
The staff and eval pages need the corresponding local token from
`.tmp/local-demo/eval.env`; enter it directly into their password field.
Do not paste tokens into chat, URLs, screenshots or committed evidence.

After all source edits and review are complete, run mandatory local gates:

```powershell
.tmp/m4-review-001/Scripts/python.exe scripts/release.py
.tmp/m4-review-001/Scripts/python.exe scripts/release.py --package
```

Both commands run dependency consistency, Ruff over backend/scripts/tests, strict
backend mypy, all Python suites, every `tests/frontend/*.test.mjs` Node suite,
syntax checks on every frontend JS/MJS file, all 227 deterministic eval cases and
the HTTP admin self-test (4/4 checks, verified audit and zero unauthorized ledger
writes). The HTTP check uses FastAPI TestClient, a fresh isolated database and
generated process-only credentials; it does not contact a deployed server or
live LLM. Complete eval-case and HTTP-response JSON evidence is retained beside
the per-gate raw logs and `gates.json`. A failing gate prevents packaging.
The package command repeats these gates, scans intended tracked/untracked
source for credential patterns, hashes every selected source/dependency/frontend/
policy/contract/eval/document file, and rejects concurrent source changes.
Each run creates a new `.tmp/releases/<UTC timestamp>-<unique id>` directory.
The archive includes `release-manifest.json`; it excludes local environment
files, databases, caches, logs, audit anchors and Git metadata. It contains source,
tests, prompts, static assets, evals, docs, CI workflows and dependency pins. It is a source
candidate, not an offline wheel bundle. The manifest records external gates as
not run. Preserve the directory to retain exact evidence; these scripts never
delete files. A passing local manifest does not certify live LLM credentials,
Docker image behavior, TLS, public hosting or real banking integrations.
