from ocr_agent.constants import ALLOWED_TYPES
from ocr_agent.decision_policies import TYPE_RULES, policy_context
from helpers import make_page


def test_every_training_type_has_agent_decision_policy() -> None:
    assert set(TYPE_RULES) == ALLOWED_TYPES
    for block_type, policy in TYPE_RULES.items():
        assert policy.get("positive"), block_type
        assert policy.get("negative"), block_type
        assert "required_fields" in policy, block_type


def test_policy_context_contains_candidate_type_rules() -> None:
    page = make_page()
    context = policy_context(page)
    assert "title" in context["type_rules"]
    assert "text" in context["type_rules"]
    assert "bbox" in context["global_fields"]
