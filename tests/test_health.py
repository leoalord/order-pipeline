import httpx
import pytest

API_HEALTH_URL = "http://localhost:8000/health"


def test_health_pings_postgres() -> None:
    try:
        response = httpx.get(API_HEALTH_URL, timeout=5.0)
    except httpx.RequestError as exc:
        pytest.fail(f"API/DB is down: {exc}")
    assert response.status_code == 200, f"expected 200 from /health, got {response.status_code}"
    assert response.json() == {"status": "ok"}
