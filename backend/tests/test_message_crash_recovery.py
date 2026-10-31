import os
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy import select

from agentbank.agent.orchestrator import AgentOrchestrator
from agentbank.db import AgentMessage, Database, LedgerEntry, PlanRecord
from agentbank.audit.store import PersistentAudit
from agentbank.errors import DomainError
from agentbank.seed.loader import load_seed


CHILD = r'''
import os, sys
from agentbank.agent.orchestrator import AgentOrchestrator
from agentbank.bank.service import TransferService
from agentbank.db import Database
db = Database(sys.argv[1])
agent = AgentOrchestrator(db)
def interrupted(*args):
    if sys.argv[2] in {'prepared', 'committed'}:
        service = TransferService(db)
        p = service.prepare('user_001', '房东', 2000)
        if sys.argv[2] == 'committed':
            service.confirm('user_001', p.plan_id, p.receipt_id, p.display_hash)
    if sys.argv[2] == 'active':
        print('claimed', flush=True)
        sys.stdin.readline()
    os._exit(23)
agent._handle_message = interrupted
agent.handle('user_001', 'same', '给房东转20元', message_id='original')
'''


def child_command(url, mode):
    return [sys.executable, '-c', CHILD, url, mode]


def child_env():
    env = dict(os.environ)
    env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1])
    return env


@pytest.mark.parametrize('mode', ['claimed', 'prepared', 'committed'])
@pytest.mark.parametrize('retry_first', [False, True])
def test_process_exit_releases_conversation_without_replaying_money(tmp_path, mode, retry_first):
    url = f"sqlite:///{tmp_path / 'restart.db'}"
    db = Database(url)
    load_seed(db)
    assert subprocess.run(child_command(url, mode), env=child_env(), timeout=30).returncode == 23
    restarted = Database(url)
    agent = AgentOrchestrator(restarted)
    if retry_first:
        with pytest.raises(DomainError, match='MESSAGE_OUTCOME_UNKNOWN'):
            agent.handle('user_001', 'same', '给房东转20元', message_id='original')
    # A fresh message in the SAME conversation must work, even after a committed write.
    result = agent.handle('user_001', 'same', '查余额', message_id='new')
    assert result.data['available_minor'] == (498000 if mode == 'committed' else 500000)
    for _ in range(2):
        with pytest.raises(DomainError, match='MESSAGE_OUTCOME_UNKNOWN'):
            agent.handle('user_001', 'same', '给房东转20元', message_id='original')
    with restarted.sessions() as session:
        original = session.get(AgentMessage, ('user_001', 'same', 'original'))
        assert original.status == 'unknown'
        assert len(session.scalars(select(LedgerEntry)).all()) == (2 if mode == 'committed' else 0)
        assert len(session.scalars(select(PlanRecord)).all()) == (0 if mode == 'claimed' else 1)
    assert PersistentAudit(restarted).verify()
    with pytest.raises(DomainError, match='IDEMPOTENCY_CONFLICT'):
        agent.handle('user_001', 'same', '给房东转30元', message_id='original')


def test_live_process_claim_is_not_recovered_by_another_worker(tmp_path):
    url = f"sqlite:///{tmp_path / 'live.db'}"
    db = Database(url)
    load_seed(db)
    with subprocess.Popen(child_command(url, 'active'), env=child_env(),
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) as child:
        try:
            assert child.stdout.readline().strip() == 'claimed'
            other = AgentOrchestrator(Database(url))
            with pytest.raises(DomainError, match='CONVERSATION_BUSY'):
                other.handle('user_001', 'same', '查余额', message_id='new')
            with db.sessions() as session:
                assert session.get(AgentMessage, ('user_001', 'same', 'original')).status == 'processing'
        finally:
            child.communicate('\n', timeout=30)
    assert other.handle('user_001', 'same', '查余额', message_id='new').kind == 'facts'
