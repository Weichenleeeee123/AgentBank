from pathlib import Path

import pytest

from agentbank.bank.capabilities_full import CapabilityService
from agentbank.db import Database
from agentbank.seed.loader import load_seed


CAP_CASES = {
    "Q01": [{}, {}, {"as_of": True}],
    "Q02": [{}, {"limit": 10}, {"category": "餐饮"}],
    "T01": [{"recipient_name": "房东", "amount_minor": 100}, {"recipient_name": "房东", "amount_minor": 200}, {"recipient_name": "房东", "amount_minor": 300}],
    "T02": [{"phone": "13800000001"}, {"phone": "13800000002"}, {"phone": "13800000001", "amount_minor": 200}],
    "T03": [{"alias": "房东"}, {"recipient_name": "房东"}, {"alias": "房东", "amount_minor": 300}],
    "T04": [{"max_uses": 1}, {"period": "monthly", "max_uses": 2}, {"period": "once"}],
    "T05": [{"total_minor": 10000}, {"total_minor": 10001}, {"total_minor": 30100, "participant_ids": ["user_001", "user_002", "user_003", "user_004"]}],
    "B01": [{}, {"dsl": {"source": "transactions", "group_by": ["month"], "limit": 10}}, {"dsl": {"source": "transactions", "group_by": ["category"], "limit": 5}}],
    "B02": [{}, {"merchant": "重复商户"}, {"reason_code": "DUP_CHARGE"}],
    "B03": [{}, {"start_month": "2026-05", "end_month": "2026-07"}, {"start_month": "2026-01", "end_month": "2026-12"}],
    "B04": [{"year": "2026"}, {"start_month": "2025-11", "end_month": "2026-10"}, {"start_month": "2026-01", "end_month": "2026-12"}],
    "W01": [{}, {}, {}], "W02": [{}, {"product_id": "product_R1"}, {"product_id": "product_R5"}],
    "W03": [{"answers": [1] * 10}, {"answers": [2] * 10}, {"answers": [4] * 10}],
    "W04": [{"product_id": "product_R1", "amount_minor": 1000}, {"product_id": "product_R1", "amount_minor": 2000}, {"product_id": "product_R2", "amount_minor": 3000}],
    "W05": [{}, {"order_id": "order_1"}, {"order_id": "order_2"}], "W06": [{}, {}, {}],
    "C01": [{"virtual": True}, {"virtual": False}, {"virtual": True}],
    "C02": [{"limit_minor": 90000}, {"limit_minor": 80000}, {"limit_minor": 70000}],
    "C03": [{"restriction": "online"}, {"restriction": "international"}, {"restriction": "cash"}],
    "C04": [{"restriction": "online"}, {"restriction": "international"}, {"restriction": "cash"}],
    "C05": [{"card_id": "card_001"}, {"card_id": "card_001"}, {"card_id": "card_001"}],
    "C06": [{"card_id": "card_001"}, {"card_id": "card_001"}, {"card_id": "card_001"}],
    "C07": [{"card_id": "card_001"}, {"card_id": "card_001"}, {"card_id": "card_001"}],
    "C08": [{"card_id": "card_001", "new_pin": "1234"}, {"card_id": "card_001", "new_pin": "2345"}, {"card_id": "card_001", "new_pin": "3456"}],
    "S01": [{}, {}, {}], "S02": [{"days": 3}, {"days": 7}, {"days": 1}],
    "S03": [{"subscription_id": "sub_video"}, {"subscription_id": "sub_video"}, {"subscription_id": "sub_video"}],
    "X01": [{}, {}, {}], "X02": [{"amount_minor": 1000}, {"amount_minor": 2000}, {"amount_minor": 3000}],
    "X03": [{}, {"delivery_date": "2026-11-15"}, {"delivery_date": "2026-11-16"}],
    "X04": [{}, {"delivery_date": "2026-11-15"}, {"delivery_date": "2026-11-16"}],
    "X05": [{"card_id": "card_001"}, {"card_id": "card_001", "mfa_verified": True}, {"card_id": "card_001", "mfa_verified": True}],
}


@pytest.mark.parametrize("capability_id,payload", [(capability_id, payload) for capability_id, payloads in CAP_CASES.items() for payload in payloads])
def test_capability_has_three_business_state_cases(tmp_path: Path, capability_id: str, payload: dict[str, object]) -> None:
    database = Database(f"sqlite:///{tmp_path / f'{capability_id}.db'}")
    load_seed(database)
    service = CapabilityService(database)
    if capability_id in {"W01", "W06"}:
        service.execute("user_001", "W03", {"answers": [1] * 10}, f"setup-{capability_id}")
    if capability_id == "W04":
        service.execute("user_001", "W03", {"answers": [2] * 10}, f"setup-{capability_id}")
    if capability_id == "W05":
        service.execute("user_001", "W03", {"answers": [2] * 10}, f"setup-{capability_id}-risk")
        order = service.execute("user_001", "W04", {"product_id": "product_R1", "amount_minor": 1_000, "assurance": "mfa"}, f"setup-{capability_id}-order")
        payload["order_id"] = order.data["order_id"]
    if capability_id == "C06":
        service.execute("user_001", "C05", {"card_id": "card_001", "assurance": "mfa"}, f"setup-{capability_id}")
    if capability_id in {"W04", "W05", "C04", "C05", "C06", "C08"} or (capability_id == "C01" and payload.get("virtual") is False):
        payload["assurance"] = "mfa"
    result = service.execute("user_001", capability_id, dict(payload), f"cap-{capability_id}-{hash(str(payload))}")
    assert result.record_id and result.evidence["audit_event"]
    assert result.status not in {"stub", "unimplemented"}
