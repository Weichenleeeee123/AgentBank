"""Deterministic action review over local simulated evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class EntityEvidence:
    name: str
    aliases: tuple[str, ...]
    registration_record_id: str
    registration_detail: str
    qualification_ok: bool
    qualification_record_id: str
    qualification: str
    risk: str
    finance: str
    credit: str
    reputation: str
    evidence_sources: tuple[str, ...]
    gaps: tuple[str, ...]


_ENTITIES = (
    EntityEvidence(
        "星河模拟理财有限公司", ("星河理财",), "SIM-BIZ-0001",
        "在营（本地演示样本）· 成立日期 2021-06-15 · 登记地：演示园区",
        True, "SIM-FIN-0001",
        "演示金融机构样本命中 · 样本有效期至 2027-12-31（非真实牌照）",
        "风险样本：近 24 个月 1 条延迟兑付线索，0 条模拟处罚记录；未覆盖样本外事件",
        "2025 年模拟财务：营收 ¥1,280 万 · 净利润 ¥96 万 · 资产负债率 42%",
        "模拟信用样本：公开涉诉 0 条 · 执行记录 0 条；不代表完整信用调查",
        "模拟口碑样本 26 条：正向 23 · 中性 2 · 负向 1",
        ("登记 SIM-BIZ-0001 · 更新 2026-10-30",
         "资质 SIM-FIN-0001 · 更新 2026-10-30",
         "风险/信用 SIM-RISK-0027 · 更新 2026-10-30",
         "财务 SIM-FINANCE-0012 · 更新 2026-10-30",
         "口碑 SIM-VOICE-0008 · 更新 2026-10-30"),
        ("以上记录均为合成演示数据，不连接外部数据源",),
    ),
    EntityEvidence(
        "远山商贸演示有限公司", ("远山商贸",), "SIM-BIZ-0002",
        "在营（本地演示样本）· 成立日期 2019-03-08 · 登记地：示例市",
        False, "SIM-FIN-NOMATCH-0002",
        "模拟金融机构名录未命中（不等于企业无其他经营资质）",
        "风险样本：近 24 个月 3 条退款争议线索，1 条延迟履约记录",
        "2025 年模拟财务：营收 ¥320 万 · 净利润 -¥18 万 · 资产负债率 78%",
        "模拟信用样本：公开涉诉 2 条 · 执行记录 1 条；需核对主体与时效",
        "模拟口碑样本 18 条：正向 7 · 中性 4 · 负向 7",
        ("登记 SIM-BIZ-0002 · 更新 2026-10-30",
         "风险/信用 SIM-RISK-0031 · 更新 2026-10-30",
         "财务 SIM-FINANCE-0018 · 更新 2026-10-30",
         "口碑 SIM-VOICE-0011 · 更新 2026-10-30"),
        ("以上记录均为合成演示数据，不连接外部数据源", "付款前仍需核对收款账户归属"),
    ),
)

_ORG_SUFFIXES = ("有限公司", "股份有限公司", "集团", "银行", "证券", "基金", "保险", "财富", "资管", "支付")


def looks_like_organization(name: str) -> bool:
    value = name.strip()
    return len(value) >= 3 and any(suffix in value for suffix in _ORG_SUFFIXES)


class PreActionReviewService:
    """Resolve a subject and prepare the evidence report used by action gates."""

    def __init__(self, entities: tuple[EntityEvidence, ...] = _ENTITIES) -> None:
        self.entities = entities

    def review(self, query: str, *, action: str, product_id: str | None = None,
               product: dict[str, Any] | None = None) -> dict[str, Any]:
        entity = self._resolve(query)
        if entity is None:
            return {"source_mode": "local_simulation", "decision": "block", "action": action,
                    "subject": {"query": query, "name": None, "matched": False},
                    "registration": {"status": "not_found", "record_id": None,
                                     "detail": "本地模拟登记样本未匹配"},
                    "qualification": {"status": "not_found", "record_id": None,
                                      "detail": "本地模拟证据包没有匹配主体"},
                    "signals": self._signals(None),
                    "gaps": ["无法确认交易对象主体；模拟证据缺失不能视作通过"],
                    "questions": ["请先核实对方营业执照全称和收款账户户名"],
                    "evidence_sources": [],
                    "product": self._product(product_id, product),
                    "notice": "仅使用本地模拟证据，不代表现实企业调查结果。"}
        gaps = list(entity.gaps)
        if action == "transfer":
            gaps.append("未核验具体收款账户的真实户名与开户行")
            questions = ["请核对合同主体、付款用途与收款户名是否一致",
                         "确认履约节点、退款条件及付款后的争议处理方式"]
        else:
            gaps.append("产品登记编码、完整说明书、净值表现及销售渠道未纳入样本")
            questions = ["请核实产品登记编码及销售渠道",
                         "确认风险等级是否匹配，并核对期限和赎回到账安排"]
        return {"source_mode": "local_simulation", "decision": "pass" if entity.qualification_ok else "block",
                "action": action,
                "subject": {"query": query, "name": entity.name, "matched": True,
                            "match_type": "alias" if query.strip() != entity.name else "exact"},
                "registration": {"status": "active", "record_id": entity.registration_record_id,
                                 "detail": entity.registration_detail},
                "qualification": {"status": "found" if entity.qualification_ok else "not_found",
                                  "record_id": entity.qualification_record_id, "detail": entity.qualification},
                "signals": self._signals(entity), "gaps": gaps, "questions": questions,
                "evidence_sources": list(entity.evidence_sources),
                "product": self._product(product_id, product),
                "notice": "本演示使用本地模拟证据；样本未命中不代表现实中不存在，结论不构成投资建议。"}

    @staticmethod
    def _product(product_id: str | None, product: dict[str, Any] | None) -> dict[str, Any] | None:
        if not product_id:
            return None
        return {**(product or {"id": product_id}), "issuer": (product or {}).get("issuer_name")}

    def _resolve(self, query: str) -> EntityEvidence | None:
        key = self._normalize(query)
        found = [item for item in self.entities
                 if key == self._normalize(item.name) or key in {self._normalize(a) for a in item.aliases}]
        return found[0] if len(found) == 1 else None

    @staticmethod
    def _normalize(value: str) -> str:
        return "".join(value.casefold().split()).replace("（", "(").replace("）", ")")

    @staticmethod
    def _signals(entity: EntityEvidence | None) -> list[dict[str, str]]:
        labels = {"risk": "风险", "finance": "财务", "credit": "信用", "reputation": "口碑"}
        return [{"key": key, "label": labels[key], "value": getattr(entity, key) if entity else "未覆盖",
                 "record_id": "SIM-" + key.upper() + "-" + entity.registration_record_id[-4:] if entity else "",
                 "status": "simulated" if entity else "not_covered"} for key in labels]
