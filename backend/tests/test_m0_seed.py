from agentbank.seed.generator import generate_seed


def test_seed_generation_is_deterministic_and_contains_required_fixtures() -> None:
    first = generate_seed(seed=20261028)
    second = generate_seed(seed=20261028)

    assert first == second
    assert len(first["users"]) == 3
    assert len(first["payees"]) >= 2
    assert any("SYSTEM" in txn["memo"] for txn in first["transactions"])
    assert {product["risk_level"] for product in first["products"]} == {"R1", "R2", "R3", "R4", "R5"}
