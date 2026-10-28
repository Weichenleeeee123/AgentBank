from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from agentbank.bank.capabilities_full import CapabilityService
from agentbank.contracts import CapabilityResult
from agentbank.db import Database
from agentbank.errors import AmbiguousRecipient, DomainError
from agentbank.security.fragments import ReplyFragmentChecker
from agentbank.agent.intent import IntentParser, ParsedIntent


class AgentResponse:
    def __init__(self, *, kind: str, intent: str, message: str = "", capability_id: str | None = None,
                 record_id: str | None = None, display_hash: str = "", data: dict[str, Any] | None = None,
                 fragments: list[dict[str, Any]] | None = None) -> None:
        self.kind, self.intent, self.message = kind, intent, message
        self.capability_id, self.record_id, self.display_hash = capability_id, record_id, display_hash
        self.data = data or {}
        self.fragments = ReplyFragmentChecker().validate(fragments or [])

    def model_dump(self) -> dict[str, Any]:
        return {"kind": self.kind, "intent": self.intent, "message": self.message,
                "capability_id": self.capability_id, "record_id": self.record_id,
                "display_hash": self.display_hash, "data": self.data, "fragments": self.fragments}


class AgentOrchestrator:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.capabilities = CapabilityService(database)
        self.parser = IntentParser()
        self._pending: dict[tuple[str, str], ParsedIntent] = {}

    def handle(self, user_id: str, conversation_id: str, text: str, channel: str = "app") -> AgentResponse:
        parsed = self.parser.parse(text)
        pending = self._pending.get((user_id, conversation_id))
        if parsed.capability_id is None and pending is not None:
            parsed = self._continue_pending(pending, text)
        if parsed.capability_id is None:
            return AgentResponse(kind="clarification", intent=parsed.intent, message="我还不能确定要办理哪项银行业务，请说明查询、转账、账单、理财、卡片或订阅。")
        if parsed.ambiguities:
            return AgentResponse(kind="clarification", intent=parsed.intent, capability_id=parsed.capability_id,
                                 message="请确认收款人：我找到多个同名联系人，请提供手机号或联系人备注。")
        if parsed.missing:
            self._pending[(user_id, conversation_id)] = parsed
            labels = {"amount": "金额", "recipient": "收款人", "answers": "风险测评答案", "product_id": "产品", "order_id": "订单", "subscription_id": "订阅", "card_id": "卡片"}
            missing = "、".join(labels.get(item, item) for item in parsed.missing)
            return AgentResponse(kind="clarification", intent=parsed.intent, capability_id=parsed.capability_id,
                                 message=f"还需要补充：{missing}。")
        key = "agent-" + hashlib.sha256(f"{conversation_id}:{text}".encode()).hexdigest()[:32]
        if parsed.capability_id in {"W04", "W05", "C04", "C05", "C06", "C08"}:
            parsed = ParsedIntent(parsed.intent, parsed.capability_id, {**parsed.slots, "assurance": "mfa"}, parsed.missing, parsed.ambiguities)
        capability_id = parsed.capability_id
        assert capability_id is not None
        try:
            result = self.capabilities.execute(user_id, capability_id, parsed.slots, key)
        except AmbiguousRecipient:
            return AgentResponse(kind="clarification", intent=parsed.intent, capability_id=parsed.capability_id,
                                 message="存在多个同名收款人，请提供手机号或联系人备注。")
        except DomainError as error:
            if error.code == "MFA_REQUIRED":
                return AgentResponse(kind="mfa", intent=parsed.intent, capability_id=parsed.capability_id,
                                     message="这项操作需要模拟手机验证码验证，请输入验证码。")
            return AgentResponse(kind="error", intent=parsed.intent, capability_id=parsed.capability_id, message=error.code)
        self._pending.pop((user_id, conversation_id), None)
        return self._response(parsed, result)

    def _continue_pending(self, pending: ParsedIntent, text: str) -> ParsedIntent:
        slots = dict(pending.slots)
        missing = list(pending.missing)
        if pending.capability_id == "W03":
            answers = [int(item) for item in re.findall(r"\d+", text)]
            if len(answers) == 10:
                slots["answers"] = answers
                missing = []
        elif pending.capability_id in {"T01", "T02", "T03"}:
            amount_match = self.parser._amount.search(text)
            if amount_match:
                slots["amount_minor"] = self.parser.parse(text + " 转账").slots.get("amount_minor")
                missing = [item for item in missing if item != "amount"]
        elif pending.capability_id == "W04":
            product = re.search(r"R[1-5]", text, re.IGNORECASE)
            amount_match = self.parser._amount.search(text)
            if product:
                slots["product_id"] = f"product_{product.group(0).upper()}"
                missing = [item for item in missing if item != "product_id"]
            if amount_match:
                slots["amount_minor"] = self.parser.parse(f"转账 {amount_match.group('amount')}").slots.get("amount_minor")
                missing = [item for item in missing if item != "amount_minor"]
        return ParsedIntent(pending.intent, pending.capability_id, slots, missing, pending.ambiguities)

    def confirm(self, user_id: str, record_id: str, display_hash: str) -> AgentResponse:
        result = self.capabilities.confirm_transfer(user_id, record_id, display_hash)
        return AgentResponse(kind="result", intent="transfer", capability_id=result.capability_id,
                             record_id=record_id, data=result.data, message="操作结果已从模拟银行账本确认。",
                             fragments=[{"type": "fact", "label": "操作状态", "value_minor": 0, "status": result.status}])

    def verify(self, user_id: str, record_id: str, otp: str) -> AgentResponse:
        with self.database.sessions() as session:
            from agentbank.db import CapabilityRecord
            record = session.get(CapabilityRecord, record_id)
            data = json.loads(record.data_json) if record is not None else {}
        if data.get("plan_id"):
            result = self.capabilities.verify_transfer(user_id, record_id, otp)
        else:
            result = self.capabilities.verify_mfa(user_id, record_id, otp)
        return AgentResponse(kind="result", intent="transfer", capability_id=result.capability_id,
                             record_id=record_id, data=result.data, message="强验证完成，结果已从模拟银行确认。",
                             fragments=[{"type": "fact", "label": "操作状态", "value_minor": 0, "status": result.status}])

    def _response(self, parsed: ParsedIntent, result: CapabilityResult) -> AgentResponse:
        data = result.data
        if result.status == "awaiting_mfa":
            return AgentResponse(kind="mfa", intent=parsed.intent, capability_id=parsed.capability_id,
                                 record_id=result.record_id, data=data, message="请在模拟手机中完成强验证。")
        if parsed.capability_id in {"T01", "T02", "T03"}:
            display = data.get("display", [])
            fragments = [{"type": "fact", "label": item.get("label", "详情"), "value_minor": 0, "text": item.get("value", "")}
                         for item in display]
            kind = "mfa" if data.get("assurance") == "mfa" else "confirmation"
            return AgentResponse(kind=kind, intent=parsed.intent, capability_id=parsed.capability_id,
                                 record_id=result.record_id, display_hash=str(data.get("display_hash", "")), data=data,
                                 message="请核对操作详情后确认。" if kind == "confirmation" else "请在模拟手机中完成强验证。",
                                 fragments=fragments)
        fragments = []
        if parsed.capability_id == "Q01":
            fragments = [{"type": "fact", "label": "可用余额", "value_minor": int(data.get("available_minor", 0)), "currency": "CNY"}]
        elif parsed.capability_id == "X01":
            fragments = [{"type": "fact", "label": "生日事件", "value_minor": 0, "text": event.get("event_date", "")}
                         for event in data.get("events", [])]
        return AgentResponse(kind="facts" if result.status == "succeeded" else "result", intent=parsed.intent,
                             capability_id=parsed.capability_id, record_id=result.record_id, data=data, fragments=fragments)
