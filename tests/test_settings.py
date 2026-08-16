import pytest
from pydantic import ValidationError

from order_pipeline.api.settings import APISettings

_DSN = "postgresql+psycopg://postgres:postgres@localhost:5432/order_pipeline"


@pytest.fixture(autouse=True)
def _clear_api_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("API_DATABASE_URL", raising=False)
    monkeypatch.delenv("API_ACCEPT_CONCURRENCY", raising=False)
    monkeypatch.delenv("API_PLACE_KEY_TTL_H", raising=False)


def test_code_defaults() -> None:
    settings = APISettings(database_url=_DSN)
    assert settings.accept_concurrency == 32
    assert settings.place_key_ttl_h == 48


def test_unprefixed_env_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://ignored")
    monkeypatch.setenv("ACCEPT_CONCURRENCY", "0")
    monkeypatch.setenv("PLACE_KEY_TTL_H", "1")
    with pytest.raises(ValidationError):
        APISettings()
    settings = APISettings(database_url=_DSN)
    assert settings.accept_concurrency == 32
    assert settings.place_key_ttl_h == 48
    assert settings.database_url == _DSN


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
