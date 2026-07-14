from hyperextract.documents.run_reports import (
    build_cost_report,
    build_performance_report,
)


def usage():
    return {
        "schema_name": "HyperExtractModelUsage",
        "schema_version": "1.0",
        "total_calls": 4,
        "successful_calls": 3,
        "failed_calls": 1,
        "repair_calls": 1,
        "provider_reported_calls": 4,
        "input_tokens": 1200,
        "output_tokens": 300,
        "elapsed_seconds": 18.5,
        "by_operation": {
            "local_nodes": {
                "calls": 2,
                "successful_calls": 2,
                "failed_calls": 0,
                "input_tokens": 800,
                "output_tokens": 200,
                "elapsed_seconds": 10.0,
            }
        },
        "by_mode": {},
    }


def test_performance_report_separates_wall_time_from_model_wait_time():
    report = build_performance_report(
        usage(),
        wall_elapsed_seconds=12.25,
        chunks=3,
        max_workers=2,
        global_edge_candidates=24,
        accepted_edges=7,
        resumed=True,
    )

    assert report["schema_name"] == "HyperExtractPerformanceReport"
    assert report["wall_elapsed_seconds"] == 12.25
    assert report["model_elapsed_seconds"] == 18.5
    assert report["requests"]["total"] == 4
    assert report["global_edges"]["acceptance_rate"] == 0.291667
    assert report["resumed"] is True


def test_cost_report_is_explicitly_unpriced_without_rates():
    report = build_cost_report(usage())

    assert report["status"] == "unpriced"
    assert report["estimated_cost"] is None
    assert report["currency"] is None


def test_cost_report_calculates_configured_per_million_token_rates():
    report = build_cost_report(
        usage(),
        input_cost_per_million=1.5,
        output_cost_per_million=6.0,
        currency="CNY",
    )

    assert report["status"] == "estimated"
    assert report["input_cost"] == 0.0018
    assert report["output_cost"] == 0.0018
    assert report["estimated_cost"] == 0.0036
    assert report["currency"] == "CNY"
