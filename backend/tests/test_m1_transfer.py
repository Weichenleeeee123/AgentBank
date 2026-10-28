from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agentbank.auth.devices import DeviceService
from agentbank.bank.service import TransferService
from agentbank.db import Database
from agentbank.errors import DomainError
from agentbank.seed.loader import load_seed


@pytest.fixture()
def service(tmp_path: Path) -> tuple[TransferService, str]:
    database = Database(f"sqlite:///{tmp_path / 'agentbank.db'}")
    database.create_schema()
    load_seed(database)
    device_token = DeviceService(database).register("user_001")
    return TransferService(database), device_token


def test_q01_balance_and_t01_transfer_200_yuan_create_audited_ledger(service) -> None:
    transfer, _device_token = service
    before = transfer.balance("user_001")

    prepared = transfer.prepare("user_001", "房东", 20_000)
    result = transfer.confirm("user_001", prepared.plan_id, prepared.receipt_id, prepared.display_hash)

    assert result.status == "succeeded"
    assert result.amount_minor == 20_000
    assert transfer.balance("user_001").available_minor == before.available_minor - 20_000
    assert transfer.audit.verify() is True


def test_forged_display_hash_and_receipt_replay_are_rejected(service) -> None:
    transfer, _device_token = service
    prepared = transfer.prepare("user_001", "房东", 20_000)

    with pytest.raises(DomainError, match="HASH_MISMATCH"):
        transfer.confirm("user_001", prepared.plan_id, prepared.receipt_id, "sha256:forged")

    transfer.confirm("user_001", prepared.plan_id, prepared.receipt_id, prepared.display_hash)
    with pytest.raises(DomainError, match="RECEIPT_REPLAYED"):
        transfer.confirm("user_001", prepared.plan_id, prepared.receipt_id, prepared.display_hash)


def test_red_transfer_requires_otp_from_device_inbox(service) -> None:
    transfer, device_token = service
    prepared = transfer.prepare("user_001", "房东", 120_000)

    assert prepared.tier_display == "R"
    challenge = DeviceService(transfer.database).inbox(device_token)[0]
    result = transfer.verify("user_001", prepared.plan_id, challenge["code"])

    assert result.status == "succeeded"
    assert result.amount_minor == 120_000


def test_concurrent_600_yuan_transfers_cannot_both_complete(service) -> None:
    transfer, _device_token = service
    first = transfer.prepare("user_001", "房东", 60_000)
    second = transfer.prepare("user_001", "房东", 60_000)

    def submit(plan):
        try:
            return transfer.confirm("user_001", plan.plan_id, plan.receipt_id, plan.display_hash).status
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(submit, (first, second)))

    assert statuses.count("succeeded") == 1
    assert any(status in {"mfa_required", "DAILY_TOTAL_OVER_1000"} for status in statuses)
