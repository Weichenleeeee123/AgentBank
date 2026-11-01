from agentbank.analysis.insights import detect_subscriptions
from agentbank.db import Database
from agentbank.seed.generator import generate_seed
from agentbank.seed.loader import load_seed


def test_subscription_detection_does_not_label_single_purchase_or_same_day_duplicates():
    db = Database('sqlite://')
    load_seed(db)
    rows = detect_subscriptions(db, 'user_001')
    merchants = {row['merchant'] for row in rows}
    assert {'视频会员', 'AI 工具'} <= merchants
    assert not merchants & {'购物甲', '购物丁', '去年花店', '重复商户', '首次大额商户'}


def test_seed_has_twenty_four_complete_months_for_each_demo_user():
    seed = generate_seed()
    expected = {f'{2024 + (10 + i) // 12}-{(10 + i) % 12 + 1:02d}' for i in range(24)}
    for user in seed['users']:
        months = {str(t['posted_at'])[:7] for t in seed['transactions'] if t['user_id'] == user['id']}
        assert expected <= months
