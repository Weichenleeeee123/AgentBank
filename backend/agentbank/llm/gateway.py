from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable

from agentbank.audit.store import PersistentAudit
from agentbank.db import Database
from agentbank.security.dlp import redact_text


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
                                  "context": context or {},
                                  "response_schema": {"intent": "string", "capability_id": "string|null",
                                                       "slots": "object", "missing": "array", "ambiguities": "array",
                                                       "suggested_next_action": "string"}})
        if result is None:
            return fallback
        capability_id = result.get("capability_id")
        if capability_id is not None and (not isinstance(capability_id, str)
                                          or not __import__("re").fullmatch(r"[QTBWCSX][0-9]{2}", capability_id)):
            return fallback
        if not isinstance(result.get("intent"), str) or not isinstance(result.get("slots"), dict):
            return fallback
        return {"intent": result["intent"], "capability_id": capability_id,
                "slots": result["slots"], "missing": [str(item) for item in result.get("missing", [])],
                "ambiguities": [str(item) for item in result.get("ambiguities", [])],
                "references": [item for item in result.get("references", []) if isinstance(item, dict)],
                "suggested_next_action": str(result.get("suggested_next_action", "clarify")),
                "source": result.get("source", "model")}

    def render_reply(self, user_id: str, capability_id: str, status: str,
                     data: dict[str, Any], fragments: list[dict[str, Any]]) -> dict[str, Any]:
        fallback = {"message": "操作结果已从模拟银行确认。", "fragments": fragments,
                    "source": "template_fallback"}
        result = self._call_role("reply", self.REPLY_PROMPT_VERSION, "", user_id,
                                 {"capability_id": capability_id, "status": status,
                                  "facts": fragments, "structured_data": data,
                                  "response_schema": {"message": "string", "fragments": "array"}})
        if result is None or not isinstance(result.get("message"), str) or not isinstance(result.get("fragments"), list):
            return fallback
        return {"message": result["message"], "fragments": result["fragments"],
                "source": result.get("source", "model")}

    def explore_read_only(self, user_id: str, intent: str, context: dict[str, Any], *, max_steps: int = 5) -> list[dict[str, Any]]:
        if self.primary is None:
            self._audit(user_id, "llm.exploration.skipped", {"intent": intent, "reason": "no_provider"})
            return []
        observations: list[dict[str, Any]] = []
        for step in range(1, min(max_steps, 5) + 1):
            result = self._call_role("explorer", "agent-explore-v1", "", user_id,
                                     {"step": step, "intent": intent, "context": context,
                                      "allowed_tools": ["Q01", "Q02", "B01", "B02", "B03", "B04", "W01", "W02", "W06", "X01"],
                                      "response_schema": {"done": "boolean", "tool": "string|null",
                                                          "payload": "object", "observation": "object"}})
            if result is None:
                break
            tool = result.get("tool")
            if tool is not None and tool not in {"Q01", "Q02", "B01", "B02", "B03", "B04", "W01", "W02", "W06", "X01"}:
                break
            observations.append({"step": step, "tool": tool, "observation": result.get("observation", {})})
            if result.get("done") is True:
                break
        return observations

    def _call_role(self, role: str, prompt_version: str, user_text: str, user_id: str,
                   payload: dict[str, Any]) -> dict[str, Any] | None:
        if self.primary is None:
            self._audit(user_id, "llm.call.skipped", {"role": role, "reason": "no_provider",
                                                       "prompt_version": prompt_version})
            return None
        request = {"text": redact_text(user_text), "temperature": 0, "prompt_version": prompt_version, **payload}
        for config in (self.primary, self.backup):
            if config is None:
                continue
            attempts = 2 if config is self.primary else 1
            for attempt in range(attempts):
                try:
                    response = self.transport(config, role, request)
                    if not isinstance(response, dict):
                        raise ValueError("response must be object")
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
                                    "messages": [{"role": "system", "content": f"Return only JSON for task {role}."},
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
