import pytest

from hyperextract.documents.context_planner import ContextBudget, ContextBudgetError


def test_context_budget_accounts_for_all_request_parts():
    budget = ContextBudget(
        context_window=32_000,
        output_reserve=8_000,
        prompt_tokens=2_000,
        schema_tokens=1_000,
        outline_tokens=3_000,
        known_terms_tokens=1_000,
    )
    assert budget.available_content_tokens == 17_000
    assert budget.total_tokens(content_tokens=12_000) == 27_000
    budget.ensure_fits(content_tokens=17_000)


def test_context_budget_rejects_an_atomic_block_that_cannot_fit():
    budget = ContextBudget(
        context_window=10_000,
        output_reserve=4_000,
        prompt_tokens=2_000,
        schema_tokens=1_000,
        outline_tokens=2_000,
        known_terms_tokens=500,
    )
    with pytest.raises(ContextBudgetError, match="atomic content"):
        budget.ensure_fits(content_tokens=600)
