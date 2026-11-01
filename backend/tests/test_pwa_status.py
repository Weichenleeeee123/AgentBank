import base64
import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def run_direct_result(response: dict) -> dict:
    script = f"""
const vm = require('node:vm');
(async () => {{
const {{resultKind}} = await import({json.dumps((ROOT / 'frontend' / 'presentation.js').as_uri())});
const source = require('node:fs').readFileSync({json.dumps(str(ROOT / 'frontend' / 'app.js'))}, 'utf8');
const start = source.indexOf('function showDirectResult(');
const end = source.indexOf('async function submitDirectForm(', start);
const calls = [];
const context = {{ calls, resultKind, addBubble(...args) {{ calls.push(args); }} }};
vm.runInNewContext(source.slice(start, end) + `\nshowDirectResult(${{JSON.stringify({json.dumps(response)})}}, 'done');`, context);
process.stdout.write(Buffer.from(JSON.stringify(context.calls[0])).toString('base64'));
}})().catch(error => {{ console.error(error); process.exitCode = 1; }});
"""
    result = subprocess.run(["node", "-e", script], cwd=ROOT, capture_output=True, text=True,
                            encoding="utf-8")
    assert result.returncode == 0, result.stderr
    return json.loads(base64.b64decode(result.stdout).decode("utf-8"))


def test_direct_mfa_status_stays_an_escalation() -> None:
    bubble = run_direct_result({"status": "mfa_required", "record_id": "rec-1", "data": {}})
    assert bubble[0] == "mfa"
    assert "验证码" in bubble[1]


def test_direct_cooling_status_is_not_reported_as_completed() -> None:
    bubble = run_direct_result({"status": "cooling_required", "record_id": "rec-2", "data": {}})
    assert bubble[0] != "result"
    assert bubble[1] != "done"


def test_direct_success_requires_transfer_result() -> None:
    bubble = run_direct_result({"status": "succeeded", "capability_id": "T01",
                                "record_id": "rec-3", "data": {}})
    assert bubble[0] != "result"
