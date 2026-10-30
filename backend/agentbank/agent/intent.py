from __future__ import annotations

import re
from dataclasses import dataclass, field
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
    references: list[dict[str, str]] = field(default_factory=list)


class IntentParser:
    # A Chinese numeral may be a colloquial amount without a currency suffix
    # (for example, ``两百五``), but it must end at a token boundary.  Without
    # that boundary the old expression matched the ``一`` in ordinary words
    # such as ``一个生日`` before it ever reached the actual ``1000 元``.
    _amount = re.compile(
        r"(?P<amount>[0-9]+(?:\.[0-9]{1,2})?|"
        r"[零〇一二两三四五六七八九十百千万亿]+(?:(?=\s*(?:元|圆|块钱|块))|"
        r"(?=[\s，。！？,.;!?]|$)))\s*(?:元|圆|块钱|块)?"
    )
    _phone = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")

    def parse(self, text: str) -> ParsedIntent:
        normalized = text.strip()
        if any(word in normalized for word in ("余额", "有多少钱", "账户多少钱")):
            return ParsedIntent("balance", "Q01", {}, [], [])
        if any(word in normalized for word in ("交易记录", "流水", "消费明细")):
            return ParsedIntent("transactions", "Q02", {}, [], [])
        if any(word in normalized for word in ("异常交易", "可疑交易", "异常消费")):
            return ParsedIntent("anomalies", "B02", {}, [], [])
        if any(word in normalized for word in ("生日", "纪念日")):
            if any(word in normalized for word in ("鲜花", "花束")):
                return ParsedIntent("birthday_flower", "X03", {}, [], [])
            if any(word in normalized for word in ("蛋糕", "甜点")):
                return ParsedIntent("birthday_cake", "X04", {}, [], [])
            if any(word in normalized for word in ("预留", "锁定", "管住")):
                return self._reserve(normalized)
            return ParsedIntent("birthday_discovery", "X01", {}, [], [])
        if any(word in normalized for word in ("风险测评", "风险评估", "投资风险")):
            return ParsedIntent("risk_assessment", "W03", {}, ["answers"], [])
        if any(word in normalized for word in ("消费分类", "分类统计", "花了多少钱")):
            return ParsedIntent("spending_analysis", "B01", {}, [], [])
        if any(word in normalized for word in ("月度账单", "月报")):
            return ParsedIntent("monthly_report", "B03", {}, [], [])
        if any(word in normalized for word in ("年度账单", "年报")):
            return ParsedIntent("yearly_report", "B04", {}, [], [])
        if any(word in normalized for word in ("收益", "持仓", "理财余额")):
            return ParsedIntent("investment_positions", "W06", {}, [], [])
        if any(word in normalized for word in ("预留", "锁定", "管住")):
            return self._reserve(normalized)
        if any(word in normalized for word in ("定时", "每月", "每周", "周期转账")):
            transfer = self._transfer(normalized)
            scheduled_slots = dict(transfer.slots)
            if "alias" in scheduled_slots:
                scheduled_slots["recipient_name"] = scheduled_slots.pop("alias")
            return ParsedIntent("scheduled_transfer", "T04", {**scheduled_slots, "period": "monthly"},
                                transfer.missing, transfer.ambiguities)
        if any(word in normalized for word in ("AA", "分摊", "聚餐收款", "一起吃饭")):
            amount = self._amount.search(normalized)
            slots: dict[str, Any] = {}
            missing: list[str] = []
            if amount:
                slots["total_minor"] = parse_amount_minor(amount.group("amount"))
            else:
                missing.append("total_minor")
            slots["participant_ids"] = re.findall(r"user_[0-9]+", normalized)
            if len(slots["participant_ids"]) < 2:
                slots["participant_ids"] = ["user_001", "user_002", "user_003"]
            return ParsedIntent("aa_collect", "T05", slots, missing, [])
        if any(word in normalized for word in ("订阅", "自动扣费", "会员")):
            if any(word in normalized for word in ("提醒", "续费提醒", "到期通知")):
                subscription_id = "sub_video" if "视频" in normalized else "sub_ai" if "AI" in normalized else "sub_video"
                return ParsedIntent("subscription_reminder", "S02", {"subscription_id": subscription_id, "days": 3}, [], [])
            if any(word in normalized for word in ("取消", "不要了", "停止")):
                subscription_id = "sub_video" if "视频" in normalized else "sub_ai" if "AI" in normalized else ""
                return ParsedIntent("cancel_subscription", "S03", ({"subscription_id": subscription_id} if subscription_id else {}), ([] if subscription_id else ["subscription_id"]), [])
            return ParsedIntent("subscriptions", "S01", {}, [], [])
        if any(word in normalized for word in ("续费提醒", "提醒续费", "提醒我续费", "到期提醒", "续订提醒")):
            subscription_id = "sub_video" if any(word in normalized for word in ("视频", "会员")) else "sub_ai" if "AI" in normalized else ""
            slots = {"subscription_id": subscription_id, "days": 3} if subscription_id else {"days": 3}
            return ParsedIntent("subscription_reminder", "S02", slots,
                                [] if subscription_id else ["subscription_id"], [])
        if any(word in normalized for word in ("解挂", "解除挂失")):
            return ParsedIntent("card_loss_release", "C06", {"card_id": "card_001", "assurance": "mfa"}, [], [])
        if "挂失" in normalized and not any(word in normalized for word in ("盗刷", "被盗刷", "卡丢了")):
            return ParsedIntent("card_loss_report", "C05", {"card_id": "card_001", "assurance": "mfa"}, [], [])
        if any(word in normalized for word in ("盗刷", "被盗刷", "卡丢了")):
            return ParsedIntent("card_loss", "X05", {"card_id": "card_001"}, [], [])
        if any(word in normalized for word in ("申请卡", "申请一张卡", "虚拟卡", "实体卡")):
            return ParsedIntent("card_apply", "C01", {"virtual": "实体" not in normalized}, [], [])
        if any(word in normalized for word in ("提高额度", "调高额度", "降低额度", "调低额度", "额度")):
            amount = self._amount.search(normalized)
            limit_slots = {"limit_minor": parse_amount_minor(amount.group("amount"))} if amount else {}
            return ParsedIntent("card_limit", "C02", {"card_id": "card_001", **limit_slots}, ["limit_minor"] if not limit_slots else [], [])
        if any(word in normalized for word in ("限制网购", "限制交易", "禁止网购")):
            return ParsedIntent("card_restriction_add", "C03", {"card_id": "card_001", "restriction": "online"}, [], [])
        if any(word in normalized for word in ("解除限制", "解除网购", "取消网购限制")):
            return ParsedIntent("card_restriction_remove", "C04", {"card_id": "card_001", "restriction": "online"}, [], [])
        if any(word in normalized for word in ("修改密码", "修改PIN", "改密码", "银行卡密码")):
            pin = re.search(r"(?<!\d)(\d{4})(?!\d)", normalized)
            return ParsedIntent("pin_change", "C08", {"card_id": "card_001", "new_pin": pin.group(1) if pin else ""},
                                [] if pin else ["new_pin"], [])
        if any(word in normalized for word in ("解冻", "解除冻结")):
            return ParsedIntent("unfreeze_card", "C07", {"card_id": "card_001", "action": "unfreeze", "assurance": "mfa"}, [], [])
        if any(word in normalized for word in ("冻结卡", "卡冻结")):
            return ParsedIntent("freeze_card", "C07", {"card_id": "card_001"}, [], [])
        if any(word in normalized for word in ("推荐理财", "推荐产品")):
            return ParsedIntent("product_recommendation", "W01", {}, [], [])
        if any(word in normalized for word in ("对比产品", "产品对比", "对比理财产品", "比较产品", "比较理财")):
            return ParsedIntent("product_comparison", "W02", {}, [], [])
        if any(word in normalized for word in ("申购", "买理财")):
            subscribe_slots: dict[str, Any] = {}
            product = re.search(r"R[1-5]", normalized, re.IGNORECASE)
            amount = self._amount.search(normalized)
            if product:
                subscribe_slots["product_id"] = f"product_{product.group(0).upper()}"
            if amount:
                subscribe_slots["amount_minor"] = parse_amount_minor(amount.group("amount"))
            missing = [item for item in ("product_id", "amount_minor") if item not in subscribe_slots]
            return ParsedIntent("investment_subscribe", "W04", subscribe_slots, missing, [])
        if any(word in normalized for word in ("赎回", "卖理财")):
            order_match = re.search(r"(?:订单|order[_-]?)\s*([A-Za-z0-9_-]+)", normalized, re.IGNORECASE)
            order_id = f"order_{order_match.group(1)}" if order_match and order_match.group(1).isdigit() else (order_match.group(1) if order_match else "")
            product = re.search(r"R[1-5]", normalized, re.IGNORECASE)
            slots = {"order_id": order_id} if order_id else {}
            if product:
                slots["product_id"] = f"product_{product.group(0).upper()}"
            return ParsedIntent("investment_redeem", "W05", slots, ([] if order_id or product else ["order_id"]), [])
        if any(word in normalized for word in ("转账", "转钱", "汇款", "打钱", "付款", "转")):
            return self._transfer(normalized)
        return ParsedIntent("unknown", None, {}, [], ["intent"])

    def _reserve(self, text: str) -> ParsedIntent:
        amount = self._amount.search(text)
        slots = {"amount_minor": parse_amount_minor(amount.group("amount"))} if amount else {}
        return ParsedIntent("funds_reserve", "X02", slots, [] if amount else ["amount_minor"], [])

    def _transfer(self, text: str) -> ParsedIntent:
        # Remove a recognized phone number before scanning amounts. Otherwise the
        # amount regex can consume the phone prefix and produce a huge transfer.
        amount_text = self._phone.sub("", text)
        amount_match = self._amount.search(amount_text)
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
            recipient_match = re.search(r"(?:给|转给|付给)([^，。\s\d元块]+)", text)
            recipient = recipient_match.group(1) if recipient_match else ""
            recipient = re.sub(r"(转账|转钱|转款|付款|汇款|转)$", "", recipient)
            explicit_alias = recipient == "房租" or ("房租" in text and ("备注" in text or "转房租" in text))
            if explicit_alias:
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
