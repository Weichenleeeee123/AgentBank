from pathlib import Path

from agentbank.audit.store import PersistentAudit
from agentbank.db import Database
from agentbank.seed.loader import load_seed


def test_audit_checkpoint_and_verify_cli_surface(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'cli.db'}")
    load_seed(database)
    audit = PersistentAudit(database)
    with database.sessions.begin() as session:
        audit.append(session, "trace-cli", "user_001", "test", {"ok": True})
    checkpoint = audit.checkpoint()
    assert checkpoint is not None
    assert audit.verify() is True
