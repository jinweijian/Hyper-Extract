"""Token budgeting primitives for structured long-document requests."""

from __future__ import annotations

from dataclasses import dataclass


class ContextBudgetError(ValueError):
    """Raised before a request when an atomic content unit cannot fit."""


@dataclass(frozen=True)
class ContextBudget:
    context_window: int
    output_reserve: int
    prompt_tokens: int
    schema_tokens: int
    outline_tokens: int
    known_terms_tokens: int

    @property
    def fixed_input_tokens(self) -> int:
        return (
            self.prompt_tokens
            + self.schema_tokens
            + self.outline_tokens
            + self.known_terms_tokens
        )

    @property
    def available_content_tokens(self) -> int:
        return max(
            0, self.context_window - self.output_reserve - self.fixed_input_tokens
        )

    def total_tokens(self, *, content_tokens: int) -> int:
        return self.fixed_input_tokens + content_tokens + self.output_reserve

    def ensure_fits(self, *, content_tokens: int) -> None:
        if content_tokens > self.available_content_tokens:
            raise ContextBudgetError(
                "The atomic content unit does not fit the model context budget: "
                f"content={content_tokens}, available={self.available_content_tokens}, "
                f"window={self.context_window}, output_reserve={self.output_reserve}"
            )
