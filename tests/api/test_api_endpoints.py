"""Task 4: in-process endpoint tests for live, readiness, and correlation."""

from fastapi.testclient import TestClient

from apps.api import main as api


class _FakeChecker:
    def __init__(self, result: tuple[int, dict]):
        self._result = result

    async def run(self) -> tuple[int, dict]:
        return self._result


def test_live_endpoint_reports_process_health() -> None:
    with TestClient(api.app) as client:
        response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "live"}


def test_readiness_endpoint_maps_checker_verdict(monkeypatch) -> None:
    healthy = (200, {"status": "ready", "checks": {"postgres": {"ok": True}, "redis": {"ok": True}}})
    degraded = (503, {"status": "unavailable", "checks": {"postgres": {"ok": False}}})

    for verdict, expected_status in (healthy, 200), (degraded, 503):
        monkeypatch.setattr(api, "readiness", _FakeChecker(verdict))
        with TestClient(api.app) as client:
            response = client.get("/health/ready")

        assert response.status_code == expected_status
        assert response.json() == verdict[1]


def test_correlation_middleware_echoes_incoming_and_generates_absent() -> None:
    with TestClient(api.app) as client:
        echoed = client.get("/health/live", headers={"x-correlation-id": "given-123"})
        generated = client.get("/health/live")

    assert echoed.headers["x-correlation-id"] == "given-123"
    assert len(generated.headers["x-correlation-id"]) == 12
