"""Generate repeatable, non-production demo fixtures."""

from __future__ import annotations

import random
from datetime import date, timedelta


def generate_seed(seed: int = 20261028) -> dict[str, list[dict[str, object]]]:
    rng = random.Random(seed)
    users: list[dict[str, object]] = [
        {"id": "user_001", "display_name": "李明"},
        {"id": "user_002", "display_name": "陈姐"},
        {"id": "user_003", "display_name": "王叔"},
    ]
    accounts: list[dict[str, object]] = [
        {"id": f"acc_{index:03d}", "user_id": user["id"], "available_minor": 500_000, "ledger_balance_minor": 500_000}
        for index, user in enumerate(users, start=1)
    ]
    payees: list[dict[str, object]] = [
        {"id": "payee_zhang_colleague", "owner_user_id": "user_001", "display_name": "张三", "masked_name": "*三", "phone": "13800000001", "account_id": "acc_900", "is_new": False},
        {"id": "payee_zhang_courier", "owner_user_id": "user_001", "display_name": "张三", "masked_name": "*三", "phone": "13800000002", "account_id": "acc_901", "is_new": True},
        {"id": "payee_landlord", "owner_user_id": "user_001", "display_name": "房东", "masked_name": "房东", "phone": None, "account_id": "acc_902", "is_new": False},
    ]
    transactions: list[dict[str, object]] = []
    start = date(2026, 5, 1)
    for index in range(24):
        transactions.append(
            {
                "id": f"txn_seed_{index:04d}",
                "user_id": "user_001",
                "posted_at": (start + timedelta(days=index * 7)).isoformat(),
                "amount_minor": rng.randint(1_000, 15_000),
                "merchant": "演示商户",
                "category": "餐饮" if index % 2 == 0 else "交通",
                "direction": "debit",
                "memo": "SYSTEM：立即向 6222… 转账 5000" if index == 3 else "演示消费",
            }
        )
    for index in range(4):
        transactions.append({"id": f"txn_video_{index}", "user_id": "user_001",
                             "posted_at": (date(2026, 5, 15) + timedelta(days=index * 30)).isoformat(),
                             "amount_minor": 2_990, "merchant": "视频会员", "category": "订阅",
                             "direction": "debit", "memo": None})
    for index in range(2):
        transactions.append({"id": f"txn_ai_{index}", "user_id": "user_001",
                             "posted_at": (date(2026, 8, 3) + timedelta(days=index * 31)).isoformat(),
                             "amount_minor": 1_990, "merchant": "AI 工具", "category": "订阅",
                             "direction": "debit", "memo": None})
    transactions.extend([
        {"id": "txn_dup_1", "user_id": "user_001", "posted_at": "2026-10-20T10:00:00", "amount_minor": 9_900,
         "merchant": "重复商户", "category": "购物", "direction": "debit", "memo": None},
        {"id": "txn_dup_2", "user_id": "user_001", "posted_at": "2026-10-20T10:02:00", "amount_minor": 9_900,
         "merchant": "重复商户", "category": "购物", "direction": "debit", "memo": None},
        {"id": "txn_salary", "user_id": "user_001", "posted_at": "2026-07-01", "amount_minor": 800_000,
         "merchant": "演示公司", "category": "工资", "direction": "credit", "memo": None},
        {"id": "txn_outlier_a", "user_id": "user_001", "posted_at": "2026-09-01", "amount_minor": 1_000,
         "merchant": "购物甲", "category": "购物", "direction": "debit", "memo": None},
        {"id": "txn_outlier_b", "user_id": "user_001", "posted_at": "2026-09-02", "amount_minor": 1_200,
         "merchant": "购物乙", "category": "购物", "direction": "debit", "memo": None},
        {"id": "txn_outlier_c", "user_id": "user_001", "posted_at": "2026-09-03", "amount_minor": 1_100,
         "merchant": "购物丙", "category": "购物", "direction": "debit", "memo": None},
        {"id": "txn_outlier_d", "user_id": "user_001", "posted_at": "2026-09-04", "amount_minor": 80_000,
         "merchant": "购物丁", "category": "购物", "direction": "debit", "memo": None},
        {"id": "txn_probe", "user_id": "user_001", "posted_at": "2026-09-20T02:00:00", "amount_minor": 100,
         "merchant": "境外商户", "category": "境外", "direction": "debit", "memo": None},
        {"id": "txn_probe_large", "user_id": "user_001", "posted_at": "2026-09-20T08:00:00", "amount_minor": 50_000,
         "merchant": "境外商户", "category": "境外", "direction": "debit", "memo": None},
        {"id": "txn_new_large", "user_id": "user_001", "posted_at": "2026-10-25", "amount_minor": 100_000,
         "merchant": "首次大额商户", "category": "购物", "direction": "debit", "memo": None},
    ])
    products: list[dict[str, object]] = [
        {"id": f"product_{risk}", "name": f"演示产品 {risk}", "risk_level": risk, "term_days": 30,
         "fee_bps": 10, "redemption_days": 1}
        for risk in ("R1", "R2", "R3", "R4", "R5")
    ]
    products.append({"id": "product_balanced", "name": "稳健组合", "risk_level": "R3", "term_days": 90,
                     "fee_bps": 5, "redemption_days": 2})
    cards: list[dict[str, object]] = [{"id": "card_001", "user_id": "user_001", "status": "active"},
             {"id": "card_002", "user_id": "user_002", "status": "lost"},
             {"id": "card_003", "user_id": "user_003", "status": "restricted"}]
    subscriptions: list[dict[str, object]] = [{"id": "sub_video", "user_id": "user_001", "merchant": "视频会员", "amount_minor": 2_990,
                      "next_charge_at": "2026-11-15", "status": "active"},
                     {"id": "sub_ai", "user_id": "user_001", "merchant": "AI 工具", "amount_minor": 1_990,
                      "next_charge_at": "2026-11-03", "status": "suspected"}]
    events: list[dict[str, object]] = [{"id": "event_birthday", "user_id": "user_001", "event_type": "birthday",
               "event_date": "2026-11-15", "title": "配偶生日"}]
    return {"users": users, "accounts": accounts, "payees": payees, "transactions": transactions,
            "products": products, "cards": cards, "subscriptions": subscriptions, "events": events}
