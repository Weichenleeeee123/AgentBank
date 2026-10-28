from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from agentbank.errors import DomainError
from agentbank.text.parse import parse_amount_minor


@dataclass(frozen=True)
class ParsedIntent:
    intent: str
    capability_id: str | None
    slots: dict[str, Any]
    missing: list[str]
    ambiguities: list[str]


class IntentParser:
    _amount = re.compile(r"(?P<amount>[0-9]+(?:\.[0-9]{1,2})?|[零〇一二两三四五六七八九十百千万亿]+(?:元|块)?)\s*(?:元|块钱|块)?")
    _phone = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")

    def parse(self, text: str) -> ParsedIntent:
        normalized = text.strip()
        if any(word in normalized for word in ("余额", "有多少钱", "账户多少钱")):
            return ParsedIntent("balance", "Q01", {}, [], [])
        if any(word in normalized for word in ("交易记录", "流水", "消费明细")):
            return ParsedIntent("transactions", "Q02", {}, [], [])
        if any(word in normalized for word in ("生日", "纪念日")):
            return ParsedIntent("birthday_discovery", "X01", {}, [], [])
        if any(word in normalized for word in ("风险测评", "风险评估", "投资风险")):
            return ParsedIntent("risk_assessment", "W03", {}, ["answers"], [])
        if any(word in normalized for word in ("消费分类", "分类统计", "花了多少钱")):
            return ParsedIntent("spending_analysis", "B01", {}, [], [])
        if any(word in normalized for word in ("月度账单", "月报")):
            return ParsedIntent("monthly_report", "B03", {}, [], [])
        if any(word in normalized for word in ("年度账单", "年报")):
            return ParsedIntent("yearly_report", "B04", {}, [], [])
        if any(word in normalized for word in ("订阅", "自动扣费", "会员")):
            if any(word in normalized for word in ("取消", "不要了", "停止")):
                subscription_id = "sub_video" if "视频" in normalized else "sub_ai" if "AI" in normalized else ""
                return ParsedIntent("cancel_subscription", "S03", ({"subscription_id": subscription_id} if subscription_id else {}), ([] if subscription_id else ["subscription_id"]), [])
            return ParsedIntent("subscriptions", "S01", {}, [], [])
        if any(word in normalized for word in ("冻结卡", "卡冻结", "卡丢了")):
            return ParsedIntent("freeze_card", "C07", {}, [], [])
        if any(word in normalized for word in ("挂失", "被盗刷")):
            return ParsedIntent("card_loss", "C05", {}, ["card_id"], [])
        if any(word in normalized for word in ("推荐理财", "推荐产品")):
            return ParsedIntent("product_recommendation", "W01", {}, [], [])
        if any(word in normalized for word in ("对比产品", "产品对比")):
            return ParsedIntent("product_comparison", "W02", {}, [], [])
        if any(word in normalized for word in ("申购", "买理财")):
            return ParsedIntent("investment_subscribe", "W04", {}, ["product_id", "amount_minor"], [])
        if any(word in normalized for word in ("赎回", "卖理财")):
            return ParsedIntent("investment_redeem", "W05", {}, ["order_id"], [])
        if any(word in normalized for word in ("转账", "转钱", "汇款", "打钱", "付款", "转")):
            return self._transfer(normalized)
        return ParsedIntent("unknown", None, {}, [], ["intent"])

    def _transfer(self, text: str) -> ParsedIntent:
        amount_match = self._amount.search(text)
        slots: dict[str, Any] = {}
        missing: list[str] = []
        if amount_match:
            raw = amount_match.group("amount")
            try:
                slots["amount_minor"] = parse_amount_minor(raw)
            except DomainError:
                missing.append("amount")
        else:
            missing.append("amount")
        phone = self._phone.search(text)
        if phone:
            slots["phone"] = phone.group(1)
            capability_id = "T02"
        else:
            recipient_match = re.search(r"(?:给|转给|付给)([^，。\s]+)", text)
            recipient = recipient_match.group(1) if recipient_match else ""
            recipient = re.sub(r"(转账|转钱|转款|付款|汇款|转)$", "", recipient)
            if recipient in {"房租", "房东"} or "房租" in text:
                slots["alias"] = "房东"
                capability_id = "T03"
            elif recipient:
                slots["recipient_name"] = recipient
                capability_id = "T01"
            else:
                missing.append("recipient")
                capability_id = "T01"
        if "备注" in text:
            slots["memo"] = text.split("备注", 1)[1].strip(" ：:")
        ambiguities = ["recipient"] if slots.get("recipient_name") == "张三" else []
        if ambiguities:
            missing = [item for item in missing if item != "recipient"]
        return ParsedIntent("transfer", capability_id, slots, missing, ambiguities)
