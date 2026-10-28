from agentbank.audit.chain import AuditChain


def test_audit_chain_detects_tampering() -> None:
    chain = AuditChain()
    chain.append("trace_1", "user_001", "turn.received", {"message": "查余额"})
    chain.append("trace_1", "user_001", "policy.decided", {"tier": "G"})

    assert chain.verify() is True
    chain.events[0].payload["message"] = "被篡改"
    assert chain.verify() is False
