"""Provider-neutral performance and cost reports for course graph runs."""

from __future__ import annotations

from typing import Any


def _rounded(value: float) -> float:
    return round(float(value), 6)


def build_performance_report(
    usage: dict[str, Any] | None,
    *,
    wall_elapsed_seconds: float,
    chunks: int,
    max_workers: int,
    global_edge_candidates: int,
    accepted_edges: int,
    resumed: bool,
) -> dict[str, Any]:
    usage = usage or {}
    candidates = max(0, int(global_edge_candidates))
    accepted = max(0, int(accepted_edges))
    return {
        "schema_name": "HyperExtractPerformanceReport",
        "schema_version": "1.0",
        "wall_elapsed_seconds": _rounded(wall_elapsed_seconds),
        "model_elapsed_seconds": _rounded(usage.get("elapsed_seconds", 0)),
        "chunks": max(0, int(chunks)),
        "max_workers": max(1, int(max_workers)),
        "resumed": bool(resumed),
        "requests": {
            "total": int(usage.get("total_calls", 0)),
            "successful": int(usage.get("successful_calls", 0)),
            "failed": int(usage.get("failed_calls", 0)),
            "repair": int(usage.get("repair_calls", 0)),
            "by_operation": usage.get("by_operation") or {},
        },
        "tokens": {
            "input": int(usage.get("input_tokens", 0)),
            "output": int(usage.get("output_tokens", 0)),
            "total": int(usage.get("input_tokens", 0))
            + int(usage.get("output_tokens", 0)),
            "provider_reported_calls": int(usage.get("provider_reported_calls", 0)),
        },
        "global_edges": {
            "candidates": candidates,
            "accepted": accepted,
            "acceptance_rate": (
                _rounded(accepted / candidates) if candidates else None
            ),
        },
    }


def build_cost_report(
    usage: dict[str, Any] | None,
    *,
    input_cost_per_million: float | None = None,
    output_cost_per_million: float | None = None,
    currency: str | None = None,
) -> dict[str, Any]:
    usage = usage or {}
    input_tokens = int(usage.get("input_tokens", 0))
    output_tokens = int(usage.get("output_tokens", 0))
    priced = input_cost_per_million is not None and output_cost_per_million is not None
    input_cost = (
        _rounded(input_tokens / 1_000_000 * input_cost_per_million) if priced else None
    )
    output_cost = (
        _rounded(output_tokens / 1_000_000 * output_cost_per_million)
        if priced
        else None
    )
    return {
        "schema_name": "HyperExtractCostReport",
        "schema_version": "1.0",
        "status": "estimated" if priced else "unpriced",
        "currency": str(currency or "USD") if priced else None,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "input_cost_per_million": input_cost_per_million,
        "output_cost_per_million": output_cost_per_million,
        "input_cost": input_cost,
        "output_cost": output_cost,
        "estimated_cost": (
            _rounded((input_cost or 0) + (output_cost or 0)) if priced else None
        ),
        "note": (
            "Estimate based on configured per-million-token rates."
            if priced
            else "Token rates were not configured; no monetary amount was inferred."
        ),
    }
