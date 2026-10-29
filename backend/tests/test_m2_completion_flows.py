from pathlib import Path

from agentbank.bank.capabilities_full import CapabilityService
from agentbank.db import Database
from agentbank.seed.loader import load_seed


def service(tmp_path: Path) -> CapabilityService:
    database = Database(f"sqlite:///{tmp_path / 'completion.db'}")
    load_seed(database)
    return CapabilityService(database)


def test_aa_participant_confirms_and_bill_closes(tmp_path: Path) -> None:
    capability = service(tmp_path)
    created = capability.execute("user_001", "T05", {"total_minor": 10_000, "participant_ids": ["user_001", "user_002", "user_003"]}, "aa-create")
    bill_id = created.data["bill_id"]
    first = capability.execute("user_002", "T05", {"action": "pay_share", "bill_id": bill_id}, "aa-pay-u2")
    assert first.data["share_status"] == "paid"
    final = capability.execute("user_003", "T05", {"action": "pay_share", "bill_id": bill_id}, "aa-pay-u3")
    assert final.data["bill_status"] == "settled"


def test_subscription_charge_is_blocked_after_cancel(tmp_path: Path) -> None:
    capability = service(tmp_path)
    charged = capability.simulate_subscription_charge("user_001", "sub_video")
    assert charged["charged"] is True
    capability.execute("user_001", "S03", {"subscription_id": "sub_video"}, "cancel-before-next")
    blocked = capability.simulate_subscription_charge("user_001", "sub_video")
    assert blocked["charged"] is False


def test_redeem_progresses_from_processing_to_settled(tmp_path: Path) -> None:
    capability = service(tmp_path)
    capability.execute("user_001", "W03", {"answers": [2] * 10}, "redeem-risk")
    subscribed = capability.execute("user_001", "W04", {"product_id": "product_R1", "amount_minor": 1_000, "assurance": "mfa"}, "redeem-subscribe")
    redeemed = capability.execute("user_001", "W05", {"order_id": subscribed.data["order_id"], "assurance": "mfa"}, "redeem-request")
    assert redeemed.data["status"] == "processing"
    settled = capability.progress_investments("user_001")
    assert settled[0].data["status"] == "settled"
