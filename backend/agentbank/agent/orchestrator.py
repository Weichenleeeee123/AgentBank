from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any
from sqlalchemy import select

from agentbank.bank.capabilities_full import CapabilityService
from agentbank.contracts import CapabilityResult
from agentbank.db import AgentMessage, ConversationReference, ConversationState, Database
from agentbank.errors import AmbiguousRecipient, DomainError
from agentbank.security.fragments import ReplyFragmentChecker
from agentbank.security.dlp import redact_text
from agentbank.agent.intent import IntentParser, ParsedIntent
from agentbank.agent.message_lock import conversation_lock
from agentbank.llm.gateway import LLMGateway
from agentbank.tools import INPUT_FIELDS
from agentbank.workflow.recovery import HandoffService
from agentbank.security.fraud import FraudDetector, FraudLevel, record_risk


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
    def __init__(self, database: Database, llm: LLMGateway | None = None,
                 capabilities: CapabilityService | None = None) -> None:
        self.database = database
        self.capabilities = capabilities or CapabilityService(database, require_device_mfa=True, enforce_prepare=True)
        self.parser = IntentParser()
        self.llm = llm or LLMGateway.from_environment(database)

    def handle(self, user_id: str, conversation_id: str, text: str, channel: str = "app",
               *, message_id: str | None = None) -> AgentResponse:
        if not text.strip() or len(text) > 4000 or len(conversation_id) > 200:
            raise DomainError("MESSAGE_INPUT_INVALID")
        if message_id is not None and (not message_id or len(message_id) > 120):
            raise DomainError("MESSAGE_INPUT_INVALID")
        # The OS releases this lock even on abrupt process termination. Holding
        # it proves no worker can still mutate this conversation, unlike a TTL.
        with conversation_lock(self.database, user_id, conversation_id):
            with self.database.atomic() as session:
                abandoned = session.scalars(select(AgentMessage).where(
                    AgentMessage.user_id == user_id,
                    AgentMessage.conversation_id == conversation_id,
                    AgentMessage.status == 'processing')).all()
                for message in abandoned:
                    message.status = 'unknown'
                    message.error_code = 'MESSAGE_OUTCOME_UNKNOWN'
                    self.capabilities.audit.append(session, f'trace_message_{uuid.uuid4().hex}',
                        user_id, 'agent.message.interrupted',
                        {'conversation_id': conversation_id, 'message_id': message.message_id,
                         'status': 'unknown'})
            # Commit recovery separately: retrying the old ID must remain unknown
            # without rolling recovery back or dispatching its business work again.
            return self._handle_claimed(user_id, conversation_id, text, channel, message_id=message_id)

    def _handle_claimed(self, user_id: str, conversation_id: str, text: str, channel: str,
                        *, message_id: str | None) -> AgentResponse:
        request_id = message_id or uuid.uuid4().hex
        if not request_id or len(request_id) > 120 or not text.strip() or len(text) > 4000 or len(conversation_id) > 200:
            raise DomainError("MESSAGE_INPUT_INVALID")
        request_hash = hashlib.sha256(json.dumps([channel, text], ensure_ascii=False).encode()).hexdigest()
        # Persist the claim before any external model call. Business services
        # retain their own atomic commit boundaries; paid-call metering must
        # survive a later message failure. A crash leaves an explicit unknown
        # outcome instead of silently reissuing potentially completed work.
        with self.database.atomic() as session:
            cached = session.get(AgentMessage, (user_id, conversation_id, request_id))
            if cached is not None:
                if cached.request_hash != request_hash:
                    raise DomainError("IDEMPOTENCY_CONFLICT")
                if cached.status == 'failed':
                    raise DomainError(cached.error_code)
                if cached.status != 'completed':
                    raise DomainError('MESSAGE_OUTCOME_UNKNOWN')
                return AgentResponse(**json.loads(cached.response_json))
            # Different transport IDs still share pending slots and the latest
            # prepared action. Claim the conversation before releasing the DB
            # transaction so concurrent workers cannot interleave those writes.
            processing = session.scalar(select(AgentMessage.message_id).where(
                AgentMessage.user_id == user_id,
                AgentMessage.conversation_id == conversation_id,
                AgentMessage.status == 'processing').limit(1))
            if processing is not None:
                raise DomainError('CONVERSATION_BUSY')
            session.add(AgentMessage(user_id=user_id, conversation_id=conversation_id, message_id=request_id,
                                     request_hash=request_hash, response_json='', status='processing'))
        key = "agent-" + hashlib.sha256(json.dumps([user_id, conversation_id, request_id]).encode()).hexdigest()
        try:
            response = self._handle_message(user_id, conversation_id, text, channel, key)
        except Exception as error:
            with self.database.atomic() as session:
                cached = session.get(AgentMessage, (user_id, conversation_id, request_id))
                assert cached is not None
                cached.status = 'failed'
                cached.error_code = error.code if isinstance(error, DomainError) else 'MESSAGE_PROCESSING_FAILED'
            raise
        with self.database.atomic() as session:
            cached = session.get(AgentMessage, (user_id, conversation_id, request_id))
            assert cached is not None
            cached.status = 'completed'
            cached.response_json = json.dumps(response.model_dump(), ensure_ascii=False)
        return response

    def _handle_message(self, user_id: str, conversation_id: str, text: str, channel: str,
                        key: str) -> AgentResponse:
        recent_messages = self._store_user_message(user_id, conversation_id, text)
        if re.search(r"(?i)(?:otp|验证码)\s*[:：]?\s*\d{6}", text) or re.fullmatch(r"\s*\d{6}\s*", text):
            return AgentResponse(kind="clarification", intent="security",
                                 message="验证码已脱敏。请只在 App 专用验证表单中输入，不要发送到聊天。")
        latest_prepared = self._latest_prepared(user_id, conversation_id)
        if latest_prepared is not None and any(token in text for token in ("别转", "取消", "算了", "不要转")):
            with self.database.sessions() as session:
                from agentbank.db import PlanRecord
                active_plan = session.get(PlanRecord, latest_prepared)
            if active_plan is not None and active_plan.user_id == user_id:
                from agentbank.workflow.planner import BusinessPlanService
                result_data = BusinessPlanService(self.database).cancel(user_id, latest_prepared)
                self._set_latest_prepared(user_id, conversation_id, None)
                self._save_pending(user_id, conversation_id, None)
                return AgentResponse(kind="result", intent="cancel", record_id=latest_prepared,
                                     data=result_data, message="已取消计划，原确认凭证已作废。")
            result = self.capabilities.invalidate_prepared(user_id, latest_prepared, cancelled=True)
            self._save_pending(user_id, conversation_id, None)
            return AgentResponse(kind="result", intent="cancel", capability_id=result.capability_id,
                                 record_id=result.record_id, data={"status": "cancelled"}, message="已取消，原确认凭证已作废。")
        # With a provider, semantic parsing belongs to the planner. The legacy
        # parser is an explicit offline fallback, never an override of the LLM.
        using_planner = self.llm.primary is not None
        parsed = (ParsedIntent("unknown", None, {}, [], []) if using_planner else self.parser.parse(text))
        rules_risk = FraudDetector().evaluate(" ".join(recent_messages), relation={}, behavior={})
        record_risk(self.database, user_id, rules_risk.level.value)
        if rules_risk.level is FraudLevel.high:
            case = HandoffService(self.database).open(user_id, "transfer", "U07")
            return AgentResponse(kind="error", intent="transfer", data={"reason_code": "FRAUD_REVIEW_REQUIRED",
                                 "handoff_case_id": case.id}, message="疑似诱导付款，本次未执行。请联系官方人工核实。")
        pending = self._load_pending(user_id, conversation_id)
        safe_context: dict[str, Any]
        if pending is not None:
            continued = pending if using_planner else self._continue_pending(pending, text)
            safe_context = {"parsed_intent": continued.intent, "slots": continued.slots,
                            "pending_intent": {"intent": pending.intent, "capability_id": pending.capability_id,
                                               "slots": pending.slots, "missing": pending.missing}}
        else:
            continued = parsed
            safe_context = {"parsed_intent": parsed.intent, "slots": parsed.slots}
        if using_planner:
            safe_context.update(self._planner_context(user_id))
        references = self._load_references(user_id, conversation_id)
        if references:
            safe_context["references"] = references
        if latest_prepared is not None:
            prepared = self._prepared_context(user_id, latest_prepared)
            if prepared:
                safe_context["prepared"] = prepared
        if latest_prepared is not None and any(token in text for token in ("不对", "改成", "改为", "重新")):
            with self.database.sessions() as session:
                from agentbank.db import CapabilityRecord
                old_record = session.get(CapabilityRecord, latest_prepared)
                old_data = json.loads(old_record.data_json) if old_record is not None else {}
            old_payload = old_data.get("payload", {})
            if parsed.capability_id is None and old_record is not None:
                amount = self.parser.find_amount(text) if not using_planner else None
                if old_record.capability_id in {"T01", "T02", "T03"} and amount:
                    slots = dict(old_payload)
                    slots["amount_minor"] = self.parser.parse(f"转账 {amount.group('amount')}").slots["amount_minor"]
                    parsed = ParsedIntent("transfer", old_record.capability_id, slots, [], [])
            if parsed.capability_id is not None:
                self.capabilities.invalidate_prepared(user_id, latest_prepared)
        if parsed.capability_id is not None:
            self._set_latest_prepared(user_id, conversation_id, None)
        if (parsed.capability_id == "C08" and parsed.slots.get("new_pin")) or (
                re.search(r"(?:PIN|密码|口令)", text, re.IGNORECASE) and re.search(r"(?<!\d)\d{4}(?!\d)", text)):
            return AgentResponse(kind="clarification", intent=parsed.intent, capability_id="C08",
                                 message="修改密码请在 App 的安全表单中输入，不要在聊天里发送密码。")
        if self.llm.primary is not None:
            planned = self.llm.plan_intent(text, user_id=user_id, recent_user_messages=recent_messages,
                                           context=safe_context)
            if planned.get("explore") is True:
                exploration = self.llm.explore_read_only(
                    user_id, str(planned.get("intent", parsed.intent)), {"slots": planned.get("slots", {})},
                    executor=lambda capability_id, payload, step: self._execute_exploration(
                        user_id, conversation_id, capability_id, payload, step),
                )
                planned = self.llm.plan_intent(text, user_id=user_id, recent_user_messages=recent_messages,
                                               context={**safe_context, "exploration": exploration})
            planned_parsed = self._from_planner(planned)
            if planned.get("source") == "deterministic_fallback":
                using_planner = False
                parsed = self.parser.parse(text)
                if parsed.capability_id in {"T04", "X02", "X03", "X04"}:
                    return AgentResponse(kind="clarification", intent=parsed.intent,
                                         message="当前无法可靠理解这项复杂操作，请稍后重试；本次没有执行。")
                continued = self._continue_pending(pending, text) if pending else parsed
            if pending is not None and pending.intent == "birthday_plan" and planned.get("capability_id") == "X02":
                # A budget answer fills the existing typed plan; it must not
                # silently replace the user's entire birthday task with X02.
                slots = dict(pending.slots)
                slots.update({"reserve_minor": planned.get("slots", {}).get("amount_minor")})
                return self._prepare_birthday_plan(user_id, conversation_id, text, {"slots": slots})
            if planned.get("proposed_dag"):
                return self._prepare_proposed_dag(user_id, conversation_id, planned)
            if planned.get("plan_template") == "birthday":
                return self._prepare_birthday_plan(user_id, conversation_id, text, planned)
            if planned.get("plan_template") in {"query_analysis_report", "investment_review"}:
                return self._prepare_composite_plan(user_id, conversation_id, planned)
            if planned_parsed is not None:
                if latest_prepared is not None and any(token in text for token in ("不对", "改成", "改为", "重新")):
                    with self.database.sessions() as session:
                        from agentbank.db import CapabilityRecord
                        old = session.get(CapabilityRecord, latest_prepared)
                    if old is not None:
                        self.capabilities.invalidate_prepared(user_id, latest_prepared)
                parsed = self._merge_model_continuation(pending, planned_parsed, text)
                self._persist_references(user_id, conversation_id, parsed)
            elif pending is not None and continued.capability_id is not None:
                parsed = continued
            elif parsed.capability_id is None:
                suggestion = self.llm.classify_intent(text, user_id=user_id, recent_user_messages=recent_messages)
                parsed = self._apply_intent_hint(ParsedIntent("unknown", None, {}, [], []),
                                                 str(suggestion.get("intent", "unknown")))
        elif pending is not None and (parsed.capability_id is None or parsed.capability_id == pending.capability_id):
            parsed = continued
        if not using_planner and pending is not None and (parsed.capability_id is None or parsed.capability_id == pending.capability_id
                                    or (pending.capability_id == "S02" and parsed.capability_id == "S01")):
            parsed = continued if parsed.capability_id != continued.capability_id else self._merge_deterministic(parsed, continued)
        if parsed.capability_id is None:
            return AgentResponse(kind="clarification", intent=parsed.intent, message="我还不能确定要办理哪项银行业务，请说明查询、转账、账单、理财、卡片或订阅。")
        if parsed.ambiguities:
            self._save_pending(user_id, conversation_id, parsed)
            message = ("还不能唯一确定收款人，请提供姓名、手机号或联系人备注。" if parsed.capability_id in {"T01", "T02", "T03", "T04"}
                       else "请明确要操作的卡片。" if (parsed.capability_id or "").startswith("C")
                       else "还有信息需要确认，请说明具体的产品、事件或业务条件。")
            return AgentResponse(kind="clarification", intent=parsed.intent, capability_id=parsed.capability_id,
                                 message=message)
        if parsed.missing:
            self._save_pending(user_id, conversation_id, parsed)
            labels = {"amount": "金额", "amount_minor": "金额", "reserve_minor": "预留金额", "total_minor": "总金额",
                      "recipient": "收款人", "recipient_name": "收款人", "answers": "风险测评答案", "product_id": "产品",
                      "order_id": "订单", "subscription_id": "订阅", "card_id": "卡片", "next_due": "首次执行日期",
                      "start_month": "开始月份", "end_month": "结束月份", "dsl": "统计条件", "event_id": "生日事件",
                      "days": "提前提醒的天数", "limit_minor": "额度", "new_pin": "请在 App 安全表单中修改密码",
                      "delivery_date": "送达日期", "dispatch_on": "下单日期", "restriction": "交易限制类型"}
            labels.update({"max_uses": "执行次数", "period": "执行周期", "phone": "收款人手机号"})
            missing = "、".join(dict.fromkeys(labels.get(item, "业务信息") for item in parsed.missing))
            return AgentResponse(kind="clarification", intent=parsed.intent, capability_id=parsed.capability_id,
                                 message=f"还需要补充：{missing}。")
        if parsed.capability_id == "W05" and not parsed.slots.get("order_id") and parsed.slots.get("product_id"):
            with self.database.sessions() as session:
                from agentbank.db import InvestmentOrder
                order = session.scalar(select(InvestmentOrder).where(InvestmentOrder.user_id == user_id,
                                                                       InvestmentOrder.product_id == parsed.slots["product_id"],
                                                                       InvestmentOrder.side == "subscribe",
                                                                       InvestmentOrder.status == "subscribed").order_by(InvestmentOrder.created_at.desc()))
            if order is None:
                return AgentResponse(kind="clarification", intent=parsed.intent, capability_id="W05",
                                     message="没有找到可赎回的该理财持仓，请提供订单号。")
            parsed = ParsedIntent(parsed.intent, parsed.capability_id, {**parsed.slots, "order_id": order.id},
                                  parsed.missing, parsed.ambiguities, parsed.references)
        if parsed.capability_id in {"W04", "W05", "C02", "C04", "C05", "C06", "C07", "C08"}:
            parsed = ParsedIntent(parsed.intent, parsed.capability_id, {**parsed.slots, "assurance": "mfa"}, parsed.missing, parsed.ambiguities)
        capability_id = parsed.capability_id
        assert capability_id is not None
        if capability_id in {"T01", "T02", "T03"}:
            rules = FraudDetector().evaluate("转账 " + " ".join(recent_messages), relation={}, behavior={})
            fraud = (self.llm.classify_fraud(text, user_id=user_id, recent_user_messages=recent_messages)
                     if self.llm.primary is not None else {"category": "unknown", "confidence": 0.0})
            if self.llm.primary is not None and fraud.get("category") == "unknown":
                record_risk(self.database, user_id, "unknown")
            elif self.llm.primary is not None and fraud.get("category") != "safe":
                record_risk(self.database, user_id, "high")
            fraud_confidence = fraud.get("confidence")
            if not isinstance(fraud_confidence, (int, float)) or isinstance(fraud_confidence, bool):
                fraud_confidence = 0.0
            if rules.level == FraudLevel.high or (self.llm.primary is not None and
                                                   (fraud["category"] != "safe" or fraud_confidence < 0.5)):
                reason = ("classifier_unknown" if fraud["category"] == "unknown" else
                          f"fraud_{fraud['category']}")
                case = HandoffService(self.database).open(user_id, "transfer", reason)
                return AgentResponse(kind="error", intent=parsed.intent, capability_id=capability_id,
                                     data={"reason_code": "FRAUD_REVIEW_REQUIRED", "handoff_case_id": case.id},
                                     message="检测到需要人工核实的转账风险，本次没有准备或执行转账。请通过官方渠道核实后再发起操作。")
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
        self._save_pending(user_id, conversation_id, None)
        if result.status in {"awaiting_confirmation", "awaiting_mfa"}:
            self._set_latest_prepared(user_id, conversation_id, result.record_id)
        return self._response(user_id, parsed, result)

    def _merge_model_continuation(self, pending: ParsedIntent | None, proposed: ParsedIntent, text: str) -> ParsedIntent:
        family = {'T01', 'T02', 'T03'}
        if pending is None or pending.capability_id not in family or proposed.capability_id not in family:
            return proposed
        payee_fields = {'recipient_name', 'phone', 'alias'}
        supplied = {field: value for field, value in proposed.slots.items() if field in payee_fields}
        # A changed recipient needs evidence in the current user message. The
        # model still selects its meaning; this only checks the selected value.
        if any(self.llm._value_is_grounded(value, [text]) for value in supplied.values()):
            return proposed
        previous_values = {value for field, value in pending.slots.items() if field in payee_fields}
        if any(value not in previous_values for value in supplied.values()):
            return ParsedIntent(pending.intent, pending.capability_id, dict(pending.slots), ['recipient'], [], [])
        slots = {**pending.slots, **{field: value for field, value in proposed.slots.items() if field not in payee_fields}}
        aliases = {'amount': 'amount_minor', 'recipient': 'recipient_name'}
        missing = [field for field in proposed.missing
                   if aliases.get(field, field) not in slots and field not in payee_fields]
        if 'amount_minor' not in slots and 'amount_minor' not in missing:
            missing.append('amount_minor')
        return ParsedIntent(pending.intent, pending.capability_id, slots, missing, proposed.ambiguities, proposed.references)

    def _planner_context(self, user_id: str) -> dict[str, Any]:
        """Trusted entity IDs, scoped to this user; no merchant/memo instructions."""
        from agentbank.db import Card, Event, Product, Subscription, Payee
        with self.database.sessions() as session:
            cards = session.scalars(select(Card).where(Card.user_id == user_id).order_by(Card.id)).all()
            events = session.scalars(select(Event).where(Event.user_id == user_id).order_by(Event.id)).all()
            products = session.scalars(select(Product).order_by(Product.id).limit(5)).all()
            subscription_ids = session.scalars(select(Subscription.id).where(Subscription.user_id == user_id)
                                               .order_by(Subscription.id).limit(5)).all()
            contacts = session.scalars(select(Payee.display_name).where(Payee.owner_user_id == user_id)
                                      .order_by(Payee.id).limit(5)).all()
            return {"business_date": self.capabilities.clock.business_date(),
                    "card_count": len(cards), "cards": [{"card_id": card.id, "status": card.status} for card in cards[:5]],
                    "event_count": len(events), "events": [{"event_id": event.id, "event_type": event.event_type,
                                                              "event_date": event.event_date} for event in events[:5]],
                    "products": [{"product_id": product.id} for product in products],
                    "subscriptions": [{"subscription_id": item} for item in subscription_ids],
                    "payees": [{"recipient_name": name} for name in contacts]}

    def _latest_prepared(self, user_id: str, conversation_id: str) -> str | None:
        with self.database.sessions() as session:
            state = session.get(ConversationState, (user_id, conversation_id))
            return state.current_plan_id if state is not None else None

    def _prepared_context(self, user_id: str, record_id: str) -> dict[str, Any]:
        with self.database.sessions() as session:
            from agentbank.db import CapabilityRecord
            record = session.get(CapabilityRecord, record_id)
            if record is None or record.user_id != user_id:
                return {}
            data = json.loads(record.data_json)
        allowed = {"recipient_name", "recipient_id", "phone", "alias", "amount_minor",
                   "product_id", "order_id", "card_id", "limit_minor", "restriction", "subscription_id",
                   "days", "event_id", "reserve_minor"}
        payload = data.get("payload", {})
        safe_payload = {key: value for key, value in payload.items() if key in allowed}
        return {"capability_id": record.capability_id, "status": record.status, "slots": safe_payload}

    def _load_references(self, user_id: str, conversation_id: str) -> list[dict[str, Any]]:
        with self.database.sessions() as session:
            references = session.query(ConversationReference).filter(
                ConversationReference.user_id == user_id,
                ConversationReference.conversation_id == conversation_id,
            ).all()
        safe: list[dict[str, Any]] = []
        for reference in references[-10:]:
            try:
                value = json.loads(reference.slots_json)
            except (TypeError, ValueError):
                continue
            if isinstance(value, dict):
                safe.append({"slot": reference.reference_key, "capability_id": reference.capability_id,
                             "value": {key: value[key] for key in ("source", "text") if key in value}})
        return safe

    def _set_latest_prepared(self, user_id: str, conversation_id: str, record_id: str | None) -> None:
        with self.database.sessions.begin() as session:
            state = session.get(ConversationState, (user_id, conversation_id))
            if state is None:
                state = ConversationState(user_id=user_id, conversation_id=conversation_id,
                                          recent_user_messages_json="[]")
                session.add(state)
            state.current_plan_id = record_id

    @staticmethod
    def _from_planner(planned: dict[str, Any]) -> ParsedIntent | None:
        capability_id = planned.get("capability_id")
        if not capability_id or capability_id not in INPUT_FIELDS:
            return None
        slots = {key: value for key, value in dict(planned.get("slots", {})).items()
                 if key in INPUT_FIELDS[capability_id]}
        return ParsedIntent(str(planned.get("intent", "unknown")), str(capability_id),
                            slots, list(planned.get("missing", [])),
                            list(planned.get("ambiguities", [])), list(planned.get("references", [])))

    @staticmethod
    def _merge_deterministic(planned: ParsedIntent, deterministic: ParsedIntent) -> ParsedIntent:
        if planned.capability_id != deterministic.capability_id:
            return planned
        slots = {**planned.slots, **deterministic.slots}
        missing = [item for item in planned.missing if item not in slots]
        missing.extend(item for item in deterministic.missing if item not in slots and item not in missing)
        return ParsedIntent(planned.intent, planned.capability_id, slots, missing,
                            deterministic.ambiguities,
                            planned.references or deterministic.references)

    def _persist_references(self, user_id: str, conversation_id: str, parsed: ParsedIntent) -> None:
        if not parsed.references or parsed.capability_id is None:
            return
        with self.database.sessions.begin() as session:
            for index, reference in enumerate(parsed.references):
                if not isinstance(reference, dict):
                    continue
                key = str(reference.get("slot", f"ref_{index}"))[:80]
                session.merge(ConversationReference(user_id=user_id, conversation_id=conversation_id,
                                                    reference_key=key, capability_id=parsed.capability_id,
                                                    slots_json=json.dumps(reference, ensure_ascii=False)))

    def _execute_exploration(self, user_id: str, conversation_id: str, capability_id: str,
                             payload: dict[str, Any], step: int) -> dict[str, Any]:
        if capability_id not in {"Q01", "Q02", "B01", "B02", "B03", "B04", "W01", "W02", "W06", "X01"}:
            raise DomainError("EXPLORATION_TOOL_NOT_ALLOWED")
        result = self.capabilities.execute(user_id, capability_id, payload,
                                           f"explore-{uuid.uuid4().hex}-{step}-{capability_id}")
        return result.data

    def _store_user_message(self, user_id: str, conversation_id: str, text: str) -> list[str]:
        stored_text = redact_text(text)
        if re.search(r"(?:PIN|密码|口令)", text, re.IGNORECASE):
            stored_text = re.sub(r"(?<!\d)\d{4}(?!\d)", "[REDACTED]", stored_text)
        with self.database.sessions.begin() as session:
            state = session.get(ConversationState, (user_id, conversation_id))
            messages = json.loads(state.recent_user_messages_json) if state is not None else []
            messages.append(stored_text)
            messages = messages[-6:]
            if state is None:
                state = ConversationState(user_id=user_id, conversation_id=conversation_id,
                                          recent_user_messages_json=json.dumps(messages, ensure_ascii=False))
                session.add(state)
            else:
                state.recent_user_messages_json = json.dumps(messages, ensure_ascii=False)
            return messages

    def _load_pending(self, user_id: str, conversation_id: str) -> ParsedIntent | None:
        with self.database.sessions() as session:
            state = session.get(ConversationState, (user_id, conversation_id))
            if state is None or not state.pending_intent_json:
                return None
            data = json.loads(state.pending_intent_json)
            return ParsedIntent(data["intent"], data["capability_id"], data["slots"], data["missing"],
                                data["ambiguities"], data.get("references", []))

    def _save_pending(self, user_id: str, conversation_id: str, pending: ParsedIntent | None) -> None:
        with self.database.sessions.begin() as session:
            state = session.get(ConversationState, (user_id, conversation_id))
            if state is None:
                state = ConversationState(user_id=user_id, conversation_id=conversation_id, recent_user_messages_json="[]")
                session.add(state)
            state.pending_intent_json = json.dumps({"intent": pending.intent, "capability_id": pending.capability_id,
                                                    "slots": pending.slots, "missing": pending.missing,
                                                    "ambiguities": pending.ambiguities,
                                                    "references": pending.references}, ensure_ascii=False) if pending else None

    @staticmethod
    def _apply_intent_hint(parsed: ParsedIntent, hint: str) -> ParsedIntent:
        routes = {"balance": ("balance", "Q01"), "transactions": ("transactions", "Q02"),
                  "birthday_discovery": ("birthday_discovery", "X01"), "spending_analysis": ("spending_analysis", "B01"),
                  "monthly_report": ("monthly_report", "B03"), "yearly_report": ("yearly_report", "B04"),
                  "product_recommendation": ("product_recommendation", "W01"),
                  "product_comparison": ("product_comparison", "W02"), "subscriptions": ("subscriptions", "S01")}
        route = routes.get(hint)
        return ParsedIntent(route[0], route[1], {}, [], []) if route else parsed

    def _continue_pending(self, pending: ParsedIntent, text: str) -> ParsedIntent:
        slots = dict(pending.slots)
        missing = list(pending.missing)
        if pending.capability_id == "W03":
            answers = [int(item) for item in re.findall(r"\d+", text)]
            if len(answers) == 10:
                slots["answers"] = answers
                missing = []
        elif pending.capability_id in {"T01", "T02", "T03"}:
            amount_match = self.parser.find_amount(text)
            if amount_match:
                slots["amount_minor"] = self.parser.parse(text + " 转账").slots.get("amount_minor")
                missing = [item for item in missing if item != "amount"]
        elif pending.capability_id == "W04":
            product = re.search(r"R[1-5]", text, re.IGNORECASE)
            amount_match = self.parser.find_amount(text)
            if product:
                slots["product_id"] = f"product_{product.group(0).upper()}"
                missing = [item for item in missing if item != "product_id"]
            if amount_match:
                slots["amount_minor"] = self.parser.parse(f"转账 {amount_match.group('amount')}").slots.get("amount_minor")
                missing = [item for item in missing if item != "amount_minor"]
        elif pending.capability_id == "W05":
            order_match = re.search(r"(?:订单|order[_-]?)\s*([A-Za-z0-9_-]+)", text, re.IGNORECASE)
            if order_match:
                token = order_match.group(1)
                slots["order_id"] = f"order_{token}" if token.isdigit() else token
                missing = [item for item in missing if item != "order_id"]
        elif pending.capability_id == "T04":
            amount_match = self.parser.find_amount(text)
            if amount_match:
                slots["amount_minor"] = self.parser.parse(f"转账 {amount_match.group('amount')}").slots.get("amount_minor")
                missing = [item for item in missing if item != "amount"]
        elif pending.capability_id == "S02":
            if "视频" in text or "会员" in text:
                slots["subscription_id"] = "sub_video"
                missing = [item for item in missing if item != "subscription_id"]
            elif "AI" in text.upper():
                slots["subscription_id"] = "sub_ai"
                missing = [item for item in missing if item != "subscription_id"]
            days = re.search(r"([0-9一二三四五六七八九十]+)\s*(?:天|日)", text)
            if days:
                token = days.group(1)
                numbers = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
                           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
                slots["days"] = int(token) if token.isdigit() else numbers.get(token, 0)
                if slots["days"] > 0:
                    missing = [item for item in missing if item != "days"]
        return ParsedIntent(pending.intent, pending.capability_id, slots, missing, pending.ambiguities, pending.references)

    def _prepare_birthday_plan(self, user_id: str, conversation_id: str, text: str,
                               planned: dict[str, Any]) -> AgentResponse:
        pending = self._load_pending(user_id, conversation_id)
        slots = dict(pending.slots) if pending is not None and pending.intent == "birthday_plan" else {}
        slots.update(planned.get("slots", {}))
        event_id = slots.get("event_id") if isinstance(slots, dict) else None
        reserve_minor = slots.get("reserve_minor") if isinstance(slots, dict) else None
        if (not isinstance(event_id, str) or not isinstance(reserve_minor, int) or isinstance(reserve_minor, bool)
                or reserve_minor <= 0):
            missing = [field for field in ("event_id", "reserve_minor") if not slots.get(field)]
            self._save_pending(user_id, conversation_id, ParsedIntent("birthday_plan", None,
                               slots if isinstance(slots, dict) else {},
                               missing, [], []))
            return AgentResponse(kind="clarification", intent="birthday_plan",
                                 message="请补充" + "和".join("生日事件" if field == "event_id" else "预留金额" for field in missing) + "。")
        from agentbank.workflow.planner import BusinessPlanService
        try:
            plan = BusinessPlanService(self.database).create_birthday_plan(user_id, event_id, reserve_minor,
                                                                          items=slots.get("items"))
        except DomainError as error:
            return AgentResponse(kind="error", intent="birthday_plan", message=error.code)
        with self.database.sessions() as session:
            from agentbank.db import PlanRecord, ReceiptRecord
            receipt = session.query(ReceiptRecord).filter(ReceiptRecord.plan_id == plan.plan_id).one()
            plan_record = session.get(PlanRecord, plan.plan_id)
            assert plan_record is not None
            display_hash = plan_record.display_hash
        self._save_pending(user_id, conversation_id, None)
        self._set_latest_prepared(user_id, conversation_id, plan.plan_id)
        return AgentResponse(kind="plan_confirmation", intent="birthday_plan", record_id=plan.plan_id,
                             display_hash=display_hash,
                             data={"plan_id": plan.plan_id, "plan_type": "birthday",
                                   "receipt_id": receipt.id, "revision": plan.revision,
                                   "status": plan.status, "display": plan.display},
                             message="请核对生日计划后确认。")

    def _prepare_composite_plan(self, user_id: str, conversation_id: str,
                                planned: dict[str, Any]) -> AgentResponse:
        from agentbank.workflow.planner import BusinessPlanService

        template = str(planned["plan_template"])
        try:
            plan = BusinessPlanService(self.database).create_composite_plan(user_id, template,
                                                                            dict(planned.get("slots", {})))
        except DomainError as error:
            return AgentResponse(kind="error", intent=str(planned.get("intent", template)), message=error.code)
        with self.database.sessions() as session:
            from agentbank.db import PlanRecord, ReceiptRecord
            record = session.get(PlanRecord, plan.plan_id)
            receipt = session.scalar(select(ReceiptRecord).where(ReceiptRecord.plan_id == plan.plan_id))
            assert record is not None and receipt is not None
        self._save_pending(user_id, conversation_id, None)
        self._set_latest_prepared(user_id, conversation_id, plan.plan_id)
        return AgentResponse(kind="plan_confirmation", intent=template, record_id=plan.plan_id,
                             display_hash=record.display_hash,
                             data={"plan_id": plan.plan_id, "plan_type": template,
                                   "receipt_id": receipt.id, "revision": plan.revision,
                                   "status": plan.status, "display": plan.display},
                             message="请核对只读计划后确认。")

    def _prepare_proposed_dag(self, user_id: str, conversation_id: str,
                              planned: dict[str, Any]) -> AgentResponse:
        from agentbank.contracts import PlanNode
        from agentbank.workflow.planner import BusinessPlanService

        try:
            normalized_nodes: list[dict[str, Any]] = []
            for index, raw_node in enumerate(planned["proposed_dag"]):
                node = dict(raw_node)
                node.setdefault("depends_on", [])
                node.setdefault("timeout_ms", 5_000)
                node.setdefault("idempotency_key", f"model-{node.get('id', index)}")
                normalized_nodes.append(node)
            nodes = [PlanNode.model_validate(node) for node in normalized_nodes]
            plan = BusinessPlanService(self.database).create_proposed_plan(
                user_id, str(planned.get("intent", "business plan")), nodes)
        except (DomainError, ValueError, TypeError) as error:
            code = error.code if isinstance(error, DomainError) else "PLAN_INVALID"
            return AgentResponse(kind="error", intent=str(planned.get("intent", "unknown")), message=code)
        with self.database.sessions() as session:
            from agentbank.db import PlanRecord, ReceiptRecord
            record = session.get(PlanRecord, plan.plan_id)
            receipt = session.scalar(select(ReceiptRecord).where(ReceiptRecord.plan_id == plan.plan_id))
            assert record is not None and receipt is not None
        self._save_pending(user_id, conversation_id, None)
        self._set_latest_prepared(user_id, conversation_id, plan.plan_id)
        return AgentResponse(kind="plan_confirmation", intent=str(planned.get("intent", "business_plan")),
                             record_id=plan.plan_id, display_hash=record.display_hash,
                             data={"plan_id": plan.plan_id, "plan_type": "model_dag", "receipt_id": receipt.id,
                                   "revision": plan.revision, "status": plan.status, "assurance": receipt.assurance,
                                   "display": plan.display, "nodes": [node.model_dump(mode="json") for node in plan.nodes]},
                             message="请核对模型规划的业务步骤、金额和授权要求后确认。")

    def confirm(self, user_id: str, record_id: str, display_hash: str, receipt_id: str = "") -> AgentResponse:
        with self.database.sessions() as session:
            from agentbank.db import CapabilityRecord
            from agentbank.db import PlanRecord
            plan_record = session.get(PlanRecord, record_id)
            if plan_record is not None:
                if plan_record.user_id != user_id:
                    raise DomainError("NOT_OWNER")
                plan_confirmation = True
            else:
                plan_confirmation = False
            record = session.get(CapabilityRecord, record_id)
            if not plan_confirmation and (record is None or record.user_id != user_id):
                raise DomainError("NOT_OWNER")
            data = json.loads(record.data_json) if record is not None else {}
        if plan_confirmation:
            from agentbank.workflow.planner import BusinessPlanService
            plan_result = BusinessPlanService(self.database).confirm_plan(user_id, record_id, receipt_id, display_hash)
            if plan_result.get("status") == "awaiting_mfa":
                return AgentResponse(kind="mfa", intent="business_plan", record_id=record_id,
                                     display_hash=display_hash, data=plan_result,
                                     message="计划已确认；请在模拟手机中完成强验证后继续。")
            return AgentResponse(kind="result", intent="birthday_plan", record_id=record_id,
                                 display_hash=display_hash, data=plan_result,
                                 message="计划确认已记录，系统将按授权要求继续执行。")
        if data.get("plan_id"):
            result = self.capabilities.confirm_transfer(user_id, record_id, display_hash)
        else:
            result = self.capabilities.confirm_capability(user_id, record_id, receipt_id,
                                                          str(data.get("action_hash", "")), display_hash)
        if result.status == "cooling_required":
            return AgentResponse(kind="risk", intent="transfer", capability_id=result.capability_id,
                                 record_id=record_id, data=result.data,
                                 message="转账进入冷静期，资金已占用但尚未转出。期满后须重新强验证，也可取消并释放资金。")
        if result.status in {"awaiting_mfa", "mfa_required"}:
            return AgentResponse(kind="mfa", intent="transfer" if result.capability_id.startswith("T") else result.capability_id,
                                 capability_id=result.capability_id, record_id=record_id,
                                 display_hash=display_hash, data=result.data,
                                 message="请在模拟手机中完成强验证。")
        return AgentResponse(kind="result", intent="transfer" if result.capability_id.startswith("T") else result.capability_id,
                             capability_id=result.capability_id,
                             record_id=record_id, data=result.data, message="操作结果已从模拟银行账本确认。",
                             fragments=[{"type": "fact", "label": "操作状态", "value_minor": 0, "status": result.status}])

    def verify(self, user_id: str, record_id: str, otp: str) -> AgentResponse:
        with self.database.sessions() as session:
            from agentbank.db import PlanRecord
            from agentbank.db import CapabilityRecord
            plan = session.get(PlanRecord, record_id)
            record = session.get(CapabilityRecord, record_id)
            data = json.loads(record.data_json) if record is not None else {}
        if plan is not None:
            from agentbank.workflow.planner import BusinessPlanService
            plan_result: dict[str, Any] = BusinessPlanService(self.database).verify_plan(user_id, record_id, otp)
            return AgentResponse(kind="result", intent="business_plan", record_id=record_id,
                                 data=plan_result, message="计划强验证完成，结果已从模拟银行确认。")
        if data.get("plan_id"):
            result = self.capabilities.verify_transfer(user_id, record_id, otp)
        else:
            result = self.capabilities.verify_mfa(user_id, record_id, otp)
        return AgentResponse(kind="result", intent="transfer", capability_id=result.capability_id,
                             record_id=record_id, data=result.data, message="强验证完成，结果已从模拟银行确认。",
                             fragments=[{"type": "fact", "label": "操作状态", "value_minor": 0, "status": result.status}])

    def _response(self, user_id: str, parsed: ParsedIntent, result: CapabilityResult) -> AgentResponse:
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
        if result.status == "awaiting_confirmation":
            return AgentResponse(kind="confirmation", intent=parsed.intent, capability_id=parsed.capability_id,
                                 record_id=result.record_id, display_hash=str(data.get("display_hash", "")),
                                 data=data, message="请核对操作详情后确认。", fragments=[])
        if result.status == "awaiting_mfa":
            return AgentResponse(kind="mfa", intent=parsed.intent, capability_id=parsed.capability_id,
                                 record_id=result.record_id, display_hash=str(data.get("display_hash", "")),
                                 data=data, message="请在模拟手机中完成强验证。", fragments=[])
        fragments = []
        if parsed.capability_id == "Q01":
            fragments = [{"type": "fact", "label": "可用余额", "value_minor": int(data.get("available_minor", 0)), "currency": "CNY"}]
        elif parsed.capability_id == "X01":
            fragments = [{"type": "fact", "label": "生日事件", "value_minor": 0, "text": event.get("event_date", "")}
                         for event in data.get("events", [])]
        elif parsed.capability_id == "B01":
            fragments = [{"type": "fact", "label": str(row.get("category", "消费合计")),
                          "value_minor": int(row["sum_amount_minor"]), "currency": "CNY"}
                         for row in data.get("rows", []) if "sum_amount_minor" in row]
            return AgentResponse(kind="facts", intent=parsed.intent, capability_id="B01", record_id=result.record_id,
                                 data=data, fragments=fragments,
                                 message="已按类别统计支出，收入未计入消费；金额可追溯到交易明细。" if "dsl" not in parsed.slots
                                 else "已按指定条件完成交易统计，金额可追溯到交易明细。")
        rendered = self.llm.render_reply(user_id, parsed.capability_id or "", result.status, result.data, fragments)
        return AgentResponse(kind="facts" if result.status == "succeeded" else "result", intent=parsed.intent,
                             capability_id=parsed.capability_id, record_id=result.record_id, data=data,
                             message=str(rendered.get("message", "")), fragments=list(rendered.get("fragments", fragments)))
