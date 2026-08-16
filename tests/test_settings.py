import pytest
from pydantic import ValidationError

from order_pipeline.api.settings import APISettings

_DSN = "postgresql+psycopg://postgres:postgres@localhost:5432/order_pipeline"


@pytest.fixture(autouse=True)
def _clear_api_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("API_DATABASE_URL", raising=False)
    monkeypatch.delenv("API_ACCEPT_CONCURRENCY", raising=False)
    monkeypatch.delenv("API_PLACE_KEY_TTL_H", raising=False)
    monkeypatch.delenv("API_RESTAURANT_ADMIN_URL", raising=False)
    monkeypatch.delenv("API_COURIER_ADMIN_URL", raising=False)


def test_code_defaults() -> None:
    settings = APISettings(database_url=_DSN)
    assert settings.accept_concurrency == 32
    assert settings.place_key_ttl_h == 48
    assert settings.restaurant_admin_url == "http://restaurant:8081"
    assert settings.courier_admin_url == "http://courier:8082"


def test_unprefixed_env_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://ignored")
    monkeypatch.setenv("ACCEPT_CONCURRENCY", "0")
    monkeypatch.setenv("PLACE_KEY_TTL_H", "1")
    monkeypatch.setenv("RESTAURANT_ADMIN_URL", "http://ignored:1")
    monkeypatch.setenv("COURIER_ADMIN_URL", "http://ignored:2")
    with pytest.raises(ValidationError):
        APISettings()
    settings = APISettings(database_url=_DSN)
    assert settings.accept_concurrency == 32
    assert settings.place_key_ttl_h == 48
    assert settings.database_url == _DSN
    assert settings.restaurant_admin_url == "http://restaurant:8081"
    assert settings.courier_admin_url == "http://courier:8082"


def test_accept_concurrency_below_one_fails_boot() -> None:
    with pytest.raises(ValidationError):
        APISettings(database_url=_DSN, accept_concurrency=0)


def test_place_key_ttl_below_24h_fails_boot() -> None:
    with pytest.raises(ValidationError):
        APISettings(database_url=_DSN, place_key_ttl_h=23)


def test_env_wrong_defaults_fail_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_DATABASE_URL", _DSN)
    monkeypatch.setenv("API_ACCEPT_CONCURRENCY", "0")
    with pytest.raises(ValidationError):
        APISettings()
    monkeypatch.setenv("API_ACCEPT_CONCURRENCY", "32")
    monkeypatch.setenv("API_PLACE_KEY_TTL_H", "12")
    with pytest.raises(ValidationError):
        APISettings()


def test_prefixed_admin_urls_override_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_DATABASE_URL", _DSN)
    monkeypatch.setenv("API_RESTAURANT_ADMIN_URL", "http://rsim:8081")
    monkeypatch.setenv("API_COURIER_ADMIN_URL", "http://csim:8082")
    settings = APISettings()
    assert settings.restaurant_admin_url == "http://rsim:8081"
    assert settings.courier_admin_url == "http://csim:8082"
