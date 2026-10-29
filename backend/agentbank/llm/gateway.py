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
