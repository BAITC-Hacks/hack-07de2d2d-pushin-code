from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from windcast.evaluate import (
    build_metrics_response,
    evaluate_predictions,
    select_offset,
)


def test_metrics_use_shared_mask_and_json_api_harness() -> None:
    rows = [
        {
            "h": 1,
            "day": "2026-01-01",
            "actual": 0.5,
            "model": 0.5,
            "power_curve": 0.4,
            "climatology": 0.3,
            "persistence": 0.1,
            "p10": 0.4,
            "p90": 0.6,
        },
        {
            "h": 1,
            "day": "2026-01-01",
            "actual": None,
            "model": 0.1,
            "power_curve": 0.1,
            "climatology": 0.1,
            "persistence": 0.1,
            "p10": 0.0,
            "p90": 0.2,
        },
    ]
    metrics = evaluate_predictions(rows)
    assert metrics["issues_count"] == 1
    assert metrics["scored_rows"] == 1
    assert metrics["excluded_rows"] == 1
    assert metrics["coverage_p10_p90"] == 1.0
    app = FastAPI()
    app.get("/api/metrics")(lambda: build_metrics_response(metrics))
    response = TestClient(app).get("/api/metrics")
    assert response.status_code == 200
    assert response.json()["methods"][0]["key"] == "model"


def test_baseline_and_offset_selection_never_uses_future_labels() -> None:
    candidates = {
        5: [
            {"target_time_utc": "2026-01-07T18:00Z", "actual": 0.5, "model": 0.5},
            {"target_time_utc": "2026-01-07T19:00Z", "actual": 0.5, "model": 0.0},
        ],
        6: [
            {"target_time_utc": "2026-01-07T18:00Z", "actual": 0.5, "model": 0.4},
            {"target_time_utc": "2026-01-07T19:00Z", "actual": 0.5, "model": 0.5},
        ],
    }
    assert select_offset(candidates, selection_days=7) == 5
