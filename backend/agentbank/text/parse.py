from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from agentbank.clock import month_end
from agentbank.errors import DomainError

_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9}
_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10_000, "亿": 100_000_000}


def _chinese_integer(text: str) -> int:
    total = 0
    section = 0
    number = 0
    for char in text:
        if char in _DIGITS:
            number = _DIGITS[char]
        elif char in _UNITS:
            unit = _UNITS[char]
            if unit < 10_000:
                section += (number or 1) * unit
            else:
                total += (section + number) * unit
                section = 0
            number = 0
        else:
            raise ValueError(char)
    return total + section + number


def parse_amount_minor(text: str) -> int:
    normalized = text.strip().replace("人民币", "").replace("块钱", "元").replace("块", "元")
    if re.search(r"多$|几百|几千|几万", normalized):
        raise DomainError("AMBIGUOUS_AMOUNT")
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]{1,2})?)\s*(?:元|圆)?", normalized)
    if match:
        return int(round(float(match.group(1)) * 100))
    match = re.fullmatch(r"(.+?)(?:元|圆)?", normalized)
    if not match:
        raise DomainError("INVALID_AMOUNT")
    value = match.group(1)
    try:
        colloquial = re.fullmatch(r"(.+?)([二三四五六七八九])", value)
        if colloquial and any(unit in colloquial.group(1) for unit in ("百", "千", "万")):
            prefix = _chinese_integer(colloquial.group(1))
            suffix = _DIGITS[colloquial.group(2)]
            unit = next(_UNITS[char] for char in reversed(colloquial.group(1)) if char in _UNITS)
            return (prefix + suffix * (unit // 10)) * 100
        integer = _chinese_integer(value)
    except ValueError as exc:
        raise DomainError("INVALID_AMOUNT") from exc
    return integer * 100


def parse_date_expression(expression: str, reference: datetime) -> date:
    value = expression.strip()
    current = reference.date()
    literal = re.fullmatch(r"(\d{4})(?:年|[-/])(\d{1,2})(?:月|[-/])(\d{1,2})[日号]?", value)
    if literal:
        try:
            return date(*(int(part) for part in literal.groups()))
        except ValueError as error:
            raise DomainError("INVALID_DATE") from error
    if value in {"今天", "今日"}:
        return current
    if value == "明天":
        return current + timedelta(days=1)
    if value in {"月底", "本月底"}:
        return month_end(current)
    weekdays = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
    match = re.fullmatch(r"下周([一二三四五六日天])", value)
    if match:
        target = weekdays[match.group(1)]
        days = (7 - current.weekday()) + target
        return current + timedelta(days=days)
    match = re.fullmatch(r"每月\s*([0-9]{1,2})\s*号", value)
    if match:
        day = int(match.group(1))
        if day > 31:
            raise DomainError("INVALID_DATE")
        return current.replace(day=min(day, month_end(current).day))
    raise DomainError("AMBIGUOUS_DATE")
