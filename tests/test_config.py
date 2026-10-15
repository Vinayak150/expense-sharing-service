import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_database_url_is_required(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValidationError, match="Field required"):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    "url", ["sqlite:///test.db", "postgresql+asyncpg://host/db", "postgresql://host/db", "bad"]
)
def test_reject_unsupported_database_urls(url):
    with pytest.raises(ValidationError, match="DATABASE_URL"):
        Settings(database_url=url, _env_file=None)


def test_environment_configuration_and_secret_redaction(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:secret@localhost/demo")
    settings = Settings(_env_file=None)
    assert settings.database_url.get_secret_value().endswith("/demo")
    assert "secret" not in repr(settings)


def test_invalid_url_does_not_leak_credentials():
    with pytest.raises(ValidationError) as error:
        Settings(database_url="postgresql+asyncpg://user:private_password@host/db", _env_file=None)
    assert "private_password" not in str(error.value)
