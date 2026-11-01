"""Versioned local-demo questionnaire. Options, scoring and display share one catalogue."""
from typing import Any

VERSION = '2026-11-01.1'
_QUESTIONS = [
    ('您的投资知识掌握程度？', ['完全不了解', '了解存款', '了解债券', '了解基金', '了解股票', '了解复杂产品']),
    ('您的投资经验有多久？', ['没有经验', '不足1年', '1至2年', '2至3年', '3至5年', '5年以上']),
    ('您可接受的最长投资期限？', ['随时使用', '1个月', '3个月', '6个月', '1年', '3年以上']),
    ('未来一年需使用这笔资金的比例？', ['全部', '八成以上', '六至八成', '四至六成', '两至四成', '不足两成']),
    ('这笔投资占可支配金融资产的比例？', ['九成以上', '七至九成', '五至七成', '三至五成', '一至三成', '不足一成']),
    ('您的收入稳定性？', ['没有稳定收入', '很不稳定', '有较大波动', '基本稳定', '稳定且有储备', '多来源且储备充足']),
    ('应急储备可覆盖多久生活费用？', ['没有储备', '不足1个月', '1至3个月', '3至6个月', '6至12个月', '12个月以上']),
    ('您能承受的本金最大损失？', ['不能损失', '不超过2%', '不超过5%', '不超过10%', '不超过20%', '可以超过20%']),
    ('投资短期下跌10%时，您更可能如何处理？', ['立即全部退出', '退出大部分', '退出一部分', '保持并观察', '按计划继续持有', '有能力且愿意增加投资']),
    ('您的首要投资目标？', ['保全本金', '小幅稳定收益', '稳健增值', '平衡收益与波动', '追求较高收益', '愿承担较大波动追求高收益']),
]


def questionnaire() -> dict[str, Any]:
    return {'version': VERSION, 'title': '投资风险测评（本地模拟）',
            'description': '请按自身情况逐题选择。每题0至5分，总分0至50，C1至C5依次递增；仅用于本地模拟适当性校验。',
            'questions': [{'id': f'q{index + 1}', 'text': text,
                           'options': [{'value': score, 'text': option} for score, option in enumerate(options)]}
                          for index, (text, options) in enumerate(_QUESTIONS)]}


def answer_display(answers: list[int]) -> list[dict[str, str]]:
    return [{'label': f'第{i + 1}题：{text}', 'value': options[answers[i]]}
            for i, (text, options) in enumerate(_QUESTIONS)]
