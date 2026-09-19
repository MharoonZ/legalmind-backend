from app.core.config import Settings


def test_database_url_switches_to_asyncpg():
    s = Settings(database_url="postgresql://u:p%231@host:5432/db")
    assert s.database_url == "postgresql+asyncpg://u:p%231@host:5432/db"


def test_postgres_scheme_also_works():
    s = Settings(database_url="postgres://u:p@host:5432/db")
    assert s.database_url.startswith("postgresql+asyncpg://")


def test_cors_origins_list():
    s = Settings(database_url="postgresql://u:p@h/db", cors_origins="http://a.com, http://b.com")
    assert s.cors_origins_list == ["http://a.com", "http://b.com"]
