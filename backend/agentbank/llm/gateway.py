from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from agentbank.audit.store import PersistentAudit
from agentbank.db import Database
from agentbank.security.dlp import redact_text
from agentbank.tools import CAPABILITIES, INPUT_FIELDS, READ_ONLY, validate_capability_payload


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    base_url: str
    api_key: str
    model: str
    timeout_seconds: float = 4.0


Transport = Callable[[ProviderConfig, str, dict[str, Any]], dict[str, Any]]


class LLMGateway:
    PROMPT_VERSION = "agent-intent-v1"
    PLAN_PROMPT_VERSION = "agent-plan-v1"
    REPLY_PROMPT_VERSION = "agent-reply-v1"
    EXPLORER_PROMPT_VERSION = "agent-explore-v1"
    READ_ONLY_CAPABILITIES = frozenset(READ_ONLY - {"S01"})
    PLANNER_ACTIONS = ("clarify", "explore", "execute", "confirm")

    def __init__(self, database: Database, primary: ProviderConfig | None,
                 backup: ProviderConfig | None, *, transport: Transport | None = None) -> None:
        self.database, self.primary, self.backup = database, primary, backup
        self.audit = PersistentAudit(database)
        self.transport = transport or self._http_transport

    def classify_intent(self, user_text: str, *, user_id: str,
                        recent_user_messages: list[str] | None = None) -> dict[str, Any]:
        redacted = redact_text(user_text)
        text_hash = hashlib.sha256(redacted.encode("utf-8")).hexdigest()
        if self.primary is None:
            self._audit(user_id, "llm.call.skipped", {"reason": "no_provider", "text_hash": text_hash})
            return {"intent": "unknown", "confidence": 0.0, "source": "deterministic_fallback"}
        failures: list[str] = []
        for config in (self.primary, self.backup):
            if config is None:
                continue
            attempts = 1 if config.name != (self.primary.name if self.primary else "") else 2
            for attempt in range(attempts):
                try:
                    response = self.transport(config, "intent", {"text": redacted,
                                                                   "recent_user_messages": [redact_text(item) for item in (recent_user_messages or [])[-6:]],
                                                                   "temperature": 0,
                                                                   "prompt_version": self.PROMPT_VERSION,
                                                                   "response_schema": {"intent": "string", "confidence": "number"}})
                    if not isinstance(response.get("intent"), str) or not isinstance(response.get("confidence"), (int, float)):
                        raise ValueError("response schema invalid")
                    safe = {"intent": response["intent"], "confidence": float(response["confidence"]),
                            "source": config.name, "prompt_version": self.PROMPT_VERSION}
                    self._audit(user_id, "llm.call.completed", {"provider": config.name, "model": config.model,
                                 "prompt_version": self.PROMPT_VERSION, "text_hash": text_hash, "attempt": attempt + 1})
                    return safe
                except Exception as error:
                    failures.append(f"{config.name}:{type(error).__name__}")
                    self._audit(user_id, "llm.call.failed", {"provider": config.name, "model": config.model,
                                 "prompt_version": self.PROMPT_VERSION, "text_hash": text_hash,
                                 "attempt": attempt + 1, "error_type": type(error).__name__})
        return {"intent": "unknown", "confidence": 0.0, "source": "deterministic_fallback",
                "failures": failures}

    def plan_intent(self, user_text: str, *, user_id: str,
                    recent_user_messages: list[str] | None = None,
                    context: dict[str, Any] | None = None) -> dict[str, Any]:
        fallback: dict[str, Any] = {"intent": "unknown", "capability_id": None, "slots": {}, "missing": [],
                    "ambiguities": [], "suggested_next_action": "clarify", "source": "deterministic_fallback"}
        result = self._call_role("planner", self.PLAN_PROMPT_VERSION, user_text, user_id,
                                 {"recent_user_messages": [redact_text(item) for item in (recent_user_messages or [])[-6:]],
                                  "context": self._sanitize_context(context or {}),
                                  "capability_catalogue": self.capability_catalogue(),
                                  "response_schema": {"intent": "string", "capability_id": "string|null",
                                                       "slots": "object", "missing": "array", "ambiguities": "array",
                                                       "suggested_next_action": self.PLANNER_ACTIONS}},
                                 validator=self._validate_plan)
        if result is None:
            return fallback
        capability_id = result.get("capability_id")
        slots = dict(result["slots"])
        missing = list(result.get("missing", []))
        grounded = self._grounded_values(user_text, self._sanitize_context(context or {}))
        provenance_required = {"amount_minor", "total_minor", "limit_minor", "max_uses", "days", "limit",
                               "recipient_name", "phone", "alias", "product_id", "order_id", "card_id",
                               "subscription_id", "bill_id", "plan_id", "start_date", "end_date",
                               "start_month", "end_month", "year", "next_due", "delivery_date", "dispatch_on"}
        for key in list(slots):
            if key in provenance_required and not self._value_is_grounded(slots[key], grounded):
                slots.pop(key)
                if key not in missing:
                    missing.append(key)
        return {"intent": result["intent"], "capability_id": capability_id,
                "slots": slots, "missing": missing,
                "ambiguities": [str(item) for item in result.get("ambiguities", [])],
                "references": [],
                "explore": result.get("explore", False),
                "suggested_next_action": str(result.get("suggested_next_action", "clarify")),
                "source": result.get("source", "model")}

    def render_reply(self, user_id: str, capability_id: str, status: str,
                     data: dict[str, Any], fragments: list[dict[str, Any]]) -> dict[str, Any]:
        fallback = {"message": "操作结果已从模拟银行确认。", "fragments": fragments,
                    "source": "template_fallback"}
        result = self._call_role("reply", self.REPLY_PROMPT_VERSION, "", user_id,
                                 {"capability_id": capability_id, "status": status,
                                  "facts": self._sanitize_fragments(fragments),
                                  "structured_data": {"capability_id": capability_id, "status": status},
                                  "response_schema": {"message": "string", "fragments": "array"}})
        if result is None or not isinstance(result.get("message"), str) or not isinstance(result.get("fragments"), list):
            return fallback
        if not self._reply_facts_match(result["message"], result["fragments"], fragments):
            return fallback
        return {"message": result["message"], "fragments": result["fragments"],
                "source": result.get("source", "model")}

    @staticmethod
    def _reply_facts_match(message: str, candidate: list[Any], facts: list[dict[str, Any]]) -> bool:
        allowed = {json.dumps(item, ensure_ascii=False, sort_keys=True) for item in facts}
        for item in candidate:
            if not isinstance(item, dict) or json.dumps(item, ensure_ascii=False, sort_keys=True) not in allowed:
                return False
        fact_values = {str(value) for item in facts for key, value in item.items()
                       if key in {"value_minor", "text"} and value not in (None, "")}
        return all(token in fact_values for token in re.findall(r"(?<![0-9])\d+(?:\.\d+)?(?![0-9])", message))

    def explore_read_only(self, user_id: str, intent: str, context: dict[str, Any], *, max_steps: int = 5,
                          executor: Callable[[str, dict[str, Any], int], dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        if self.primary is None or executor is None:
            self._audit(user_id, "llm.exploration.skipped", {"intent": intent,
                                                              "reason": "no_provider" if self.primary is None else "no_executor"})
            return []
        observations: list[dict[str, Any]] = []
        for step in range(1, min(max_steps, 5) + 1):
            result = self._call_role("explorer", self.EXPLORER_PROMPT_VERSION, "", user_id,
                                     {"step": step, "intent": intent, "context": self._sanitize_context(context),
                                      "feedback": self._sanitize_observations(observations),
                                      "capability_catalogue": {key: self.capability_catalogue()[key] for key in self.READ_ONLY_CAPABILITIES},
                                      "allowed_tools": ["Q01", "Q02", "B01", "B02", "B03", "B04", "W01", "W02", "W06", "X01"],
                                      "response_schema": {"done": "boolean", "tool": "string",
                                                          "payload": "object"}},
                                     validator=self._validate_explorer)
            if result is None:
                break
            tool = result.get("tool")
            if tool is not None and tool not in {"Q01", "Q02", "B01", "B02", "B03", "B04", "W01", "W02", "W06", "X01"}:
                break
            try:
                observation = executor(tool, dict(result.get("payload", {})), step)
            except Exception as error:
                self._audit(user_id, "llm.exploration.failed", {"step": step, "tool": tool,
                                                                   "error_type": type(error).__name__})
                break
            if not isinstance(observation, dict):
                break
            observations.append({"step": step, "tool": tool, "observation": observation})
            if result.get("done") is True:
                break
        return observations

    def _call_role(self, role: str, prompt_version: str, user_text: str, user_id: str,
                   payload: dict[str, Any], *, validator: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any] | None:
        if self.primary is None:
            self._audit(user_id, "llm.call.skipped", {"role": role, "reason": "no_provider",
                                                       "prompt_version": prompt_version})
            return None
        request = {"text": redact_text(user_text), "temperature": 0, "prompt_version": prompt_version,
                   "system_prompt": self._load_prompt(role), **payload}
        for config in (self.primary, self.backup):
            if config is None:
                continue
            attempts = 2 if config is self.primary else 1
            for attempt in range(attempts):
                try:
                    response = self.transport(config, role, request)
                    if not isinstance(response, dict):
                        raise ValueError("response must be object")
                    if validator is not None:
                        validator(response)
                    self._audit(user_id, "llm.call.completed", {"role": role, "provider": config.name,
                                 "model": config.model, "prompt_version": prompt_version, "attempt": attempt + 1})
                    response = dict(response)
                    response["source"] = config.name
                    return response
                except Exception as error:
                    self._audit(user_id, "llm.call.failed", {"role": role, "provider": config.name,
                                 "model": config.model, "prompt_version": prompt_version,
                                 "attempt": attempt + 1, "error_type": type(error).__name__})
        return None

    @staticmethod
    def _load_prompt(role: str) -> str:
        prompt_path = Path(__file__).resolve().parents[1] / "agent" / "prompts" / f"{role}.md"
        try:
            return prompt_path.read_text(encoding="utf-8")
        except OSError:
            return "Return only structured JSON. The server remains authoritative."

    @staticmethod
    def capability_catalogue() -> dict[str, dict[str, Any]]:
        """Small, versioned capability contract exposed to the planner."""
        required = {"T01": {"recipient_name", "amount_minor"}, "T02": {"phone", "amount_minor"},
                    "T03": {"amount_minor"}, "S02": {"subscription_id", "days"},
                    "W03": {"answers"}, "W04": {"product_id", "amount_minor"},
                    "W05": {"order_id"}, "C02": {"card_id", "limit_minor"}, "C03": {"card_id", "restriction"},
                    "C04": {"card_id", "restriction"}, "C05": {"card_id"}, "C06": {"card_id"},
                    "C07": {"card_id", "action"}}
        descriptions = {
            "Q01": "查询本人账户余额", "Q02": "查询本人交易明细", "T01": "向姓名匹配的收款人转账",
            "T02": "向手机号匹配的收款人转账", "T03": "向别名或姓名匹配的收款人转账",
            "T04": "创建定期转账", "T05": "创建AA账单或分摊请求", "B01": "按明确条件分析本人交易",
            "B02": "检查本人交易异常", "B03": "生成月度财务报告", "B04": "生成年度财务报告",
            "W01": "查询理财产品推荐", "W02": "比较指定理财产品", "W03": "提交风险测评答案",
            "W04": "申购指定理财产品", "W05": "赎回指定理财订单", "W06": "查询本人理财持仓",
            "C01": "申请银行卡", "C02": "修改银行卡额度", "C03": "修改银行卡限制",
            "C04": "修改银行卡限制并验证身份", "C05": "报告银行卡丢失", "C06": "解除挂失",
            "C07": "冻结或解冻银行卡", "C08": "修改银行卡密码", "S01": "查询本人订阅",
            "S02": "设置订阅续费提醒", "S03": "取消订阅", "X01": "查询本人事件",
            "X02": "预留资金", "X03": "创建商户订单", "X04": "创建商户订单", "X05": "冻结银行卡并验证身份",
        }
        model_excluded = {"assurance", "device_token", "otp", "new_pin", "simulate_failure"}
        integer_slots = {"amount_minor", "total_minor", "limit_minor", "max_uses", "days", "limit"}
        boolean_slots = {"virtual"}
        array_slots = {"answers", "product_ids", "participant_ids"}
        return {key: {"name": descriptions[key], "kind": "query" if key in READ_ONLY and key != "S01" else "command",
                      "slots": {slot: {"type": "boolean" if slot in boolean_slots else
                                       "array" if slot in array_slots else
                                       "integer" if slot in integer_slots else "string"}
                                for slot in sorted(INPUT_FIELDS[key] - model_excluded)},
                      "required_slots": sorted(required.get(key, set()))}
                for key in sorted(CAPABILITIES)}

    @classmethod
    def _validate_plan(cls, result: dict[str, Any]) -> None:
        if not isinstance(result.get("intent"), str) or not isinstance(result.get("slots"), dict):
            raise ValueError("planner schema invalid")
        capability_id = result.get("capability_id")
        if capability_id is not None:
            if capability_id not in CAPABILITIES:
                raise ValueError("capability is not registered")
            forbidden_model_slots = {"assurance", "device_token", "otp", "new_pin", "simulate_failure"}
            if set(result["slots"]) & forbidden_model_slots:
                raise ValueError("planner included server-owned field")
            try:
                validate_capability_payload(capability_id, result["slots"])
            except Exception as exc:
                raise ValueError("planner slots invalid") from exc
        elif result["slots"]:
            raise ValueError("planner slots require a registered capability")
        for key in ("missing", "ambiguities"):
            if not isinstance(result.get(key, []), list) or any(not isinstance(item, str) for item in result.get(key, [])):
                raise ValueError("planner list invalid")
        required = cls.capability_catalogue()[capability_id]["required_slots"] if capability_id else []
        missing = set(result.get("missing", []))
        if any(slot not in result["slots"] and slot not in missing for slot in required):
            raise ValueError("planner omitted required slots")
        if "explore" in result and not isinstance(result["explore"], bool):
            raise ValueError("planner explore flag invalid")
        if result.get("suggested_next_action", "clarify") not in cls.PLANNER_ACTIONS:
            raise ValueError("planner action invalid")

    @classmethod
    def _validate_explorer(cls, result: dict[str, Any]) -> None:
        if not isinstance(result.get("done"), bool) or not isinstance(result.get("tool"), str):
            raise ValueError("explorer schema invalid")
        tool = result["tool"]
        if tool not in cls.READ_ONLY_CAPABILITIES or not isinstance(result.get("payload"), dict):
            raise ValueError("explorer tool invalid")
        validate_capability_payload(tool, result["payload"])

    @staticmethod
    def _sanitize_context(value: Any) -> Any:
        allowed = {"parsed_intent", "slots", "exploration", "step", "intent", "feedback", "observation", "status",
                   "capability_id", "available_minor", "ledger_balance_minor", "currency", "as_of",
                   "txn_ids", "events", "event_date", "amount_minor", "transaction_id", "subscription_id",
                   "days", "product_id", "order_id", "card_id", "phone", "recipient_name", "alias",
                   "start_date", "end_date", "start_month", "end_month", "year", "next_due",
                   "delivery_date", "dispatch_on", "limit_minor", "total_minor", "max_uses", "limit"}
        if isinstance(value, dict):
            return {str(key): LLMGateway._sanitize_context(item) for key, item in value.items() if str(key) in allowed}
        if isinstance(value, list):
            return [LLMGateway._sanitize_context(item) for item in value[:5]]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return redact_text(str(value)) if isinstance(value, str) else value
        return None

    @staticmethod
    def _grounded_values(user_text: str, context: Any) -> list[str]:
        values = [user_text]

        def collect(value: Any) -> None:
            if isinstance(value, dict):
                for child in value.values():
                    collect(child)
            elif isinstance(value, list):
                for child in value:
                    collect(child)
            elif isinstance(value, (str, int, float)) and not isinstance(value, bool):
                values.append(str(value))

        collect(context)
        return values

    @staticmethod
    def _value_is_grounded(value: Any, sources: list[str]) -> bool:
        if isinstance(value, list):
            return bool(value) and all(LLMGateway._value_is_grounded(item, sources) for item in value)
        candidate = str(value).strip()
        if not candidate:
            return False
        pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(candidate)}(?![A-Za-z0-9_])")
        return any(pattern.search(source) is not None for source in sources)

    @staticmethod
    def _sanitize_fragments(fragments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        keys = {"type", "label", "value_minor", "currency", "text"}
        return [{key: item[key] for key in keys if key in item} for item in fragments if isinstance(item, dict)]

    @classmethod
    def _sanitize_result_data(cls, capability_id: str, status: str, data: dict[str, Any]) -> dict[str, Any]:
        safe_keys = {"status", "available_minor", "ledger_balance_minor", "currency", "as_of", "txn_ids"}
        return {key: data[key] for key in safe_keys if key in data and isinstance(data[key], (str, int, float, bool, list))}

    @classmethod
    def _sanitize_observations(cls, observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{"step": item.get("step"), "tool": item.get("tool"), "observation": cls._sanitize_context(item.get("observation", {}))}
                for item in observations[-5:]]

    @classmethod
    def from_environment(cls, database: Database) -> LLMGateway:
        primary = _provider_from_environment("AGENTBANK_LLM_PRIMARY")
        backup = _provider_from_environment("AGENTBANK_LLM_BACKUP")
        return cls(database, primary, backup)

    def _audit(self, user_id: str, event_type: str, payload: dict[str, Any]) -> None:
        with self.database.sessions.begin() as session:
            self.audit.append(session, f"trace_llm_{hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]}",
                              user_id, event_type, payload)

    @staticmethod
    def _http_transport(config: ProviderConfig, role: str, payload: dict[str, Any]) -> dict[str, Any]:
        import httpx

        response = httpx.post(f"{config.base_url.rstrip('/')}/chat/completions", timeout=config.timeout_seconds,
                              headers={"Authorization": f"Bearer {config.api_key}"},
                              json={"model": config.model, "temperature": 0, "response_format": {"type": "json_object"},
                                    "messages": [{"role": "system", "content": str(payload.get("system_prompt", f"Return only JSON for task {role}."))},
                                                 {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]})
        response.raise_for_status()
        envelope = response.json()
        content = envelope["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        if not isinstance(parsed, dict):
            raise ValueError("response must be object")
        return parsed


def _provider_from_environment(prefix: str) -> ProviderConfig | None:
    import os

    base_url = os.getenv(f"{prefix}_BASE_URL", "")
    api_key = os.getenv(f"{prefix}_API_KEY", "")
    model = os.getenv(f"{prefix}_MODEL", "")
    if not (base_url and api_key and model):
        return None
    return ProviderConfig(prefix.lower(), base_url, api_key, model,
                          float(os.getenv(f"{prefix}_TIMEOUT_SECONDS", "4")))
