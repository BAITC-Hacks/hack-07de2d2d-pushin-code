"""Contract tests for the read-only dispatcher chat (§6.8)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from windcast import api, ask, watcher

ISSUE = "2026-02-13"


@pytest.fixture()
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("WINDCAST_ROOT", str(tmp_path))
    monkeypatch.setenv("LIVE_WATCH_MINUTES", "0")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    forecasts = tmp_path / "outputs" / "forecasts"
    forecasts.mkdir(parents=True)
    record = {
        "issue_date": ISSUE,
        "latest_version": 1,
        "versions": {
            "1": {
                "version": 1,
                "summary": "Сводка выпуска",
                "change_note": None,
                "flags": [{"kind": "ramp", "text": "быстрый спад"}],
                "weather_runs": [],
                "rows": [
                    {
                        "h": 1,
                        "target_time_local": "2026-02-14T00:00+05:00",
                        "turbine": "plant",
                        "p10": 0.1,
                        "p50": 0.2,
                        "p90": 0.3,
                    },
                    {
                        "h": 2,
                        "target_time_local": "2026-02-14T01:00+05:00",
                        "turbine": "plant",
                        "p10": 0.3,
                        "p50": 0.4,
                        "p90": 0.5,
                    },
                ],
            }
        },
    }
    (forecasts / f"{ISSUE}.json").write_text(json.dumps(record), encoding="utf-8")
    yield tmp_path
    watcher.reset()


class FakeClient:
    def __init__(self):
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        has_tool_result = any(item["role"] == "tool" for item in kwargs["messages"])
        if has_tool_result:
            message = SimpleNamespace(
                content="Пик 40 % номинала — 14.02 в 01:00.", tool_calls=None
            )
        else:
            call = SimpleNamespace(
                id="summary_1",
                function=SimpleNamespace(
                    name="get_issue_summary",
                    arguments=json.dumps({"issue_date": ISSUE}),
                ),
            )
            message = SimpleNamespace(content=None, tool_calls=[call])
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def test_ask_uses_fake_client_tool_and_assembles_answer(root, monkeypatch):
    fake = FakeClient()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
    monkeypatch.setattr(ask, "make_client", lambda: fake)

    body = ask.ask("Когда пик?", ISSUE)

    assert body["mode"] == "agent"
    assert body["answer"] == "Пик 40 % номинала — 14.02 в 01:00."
    assert body["tools"] == [
        {"name": "get_issue_summary", "args": {"issue_date": ISSUE}}
    ]
    assert any(item["role"] == "tool" for item in fake.requests[-1]["messages"])
    assert "temperature" not in fake.requests[0]
    assert "reasoning_effort" not in fake.requests[0]


def test_ask_without_key_returns_deterministic_summary(root):
    body = ask.ask("Когда пик?", ISSUE)

    assert body["mode"] == "deterministic"
    assert "пик 40 %" in body["answer"].lower()
    assert "без ключа openai отвечаю сводкой" in body["answer"].lower()
    assert body["tools"][0]["name"] == "get_issue_summary"


def test_ask_api_rejects_empty_question(root):
    with TestClient(api.app) as client:
        response = client.post(
            "/api/ask", json={"question": "   ", "issue_date": ISSUE}
        )

    assert response.status_code == 400
    assert response.json()["error"]
