import os

# Tests must never touch the real database. Point to a closed local port so
# connection fails fast, then check the app still behaves. These env vars win
# over anything in .env.
os.environ["DATABASE_URL"] = "postgresql://test:test@127.0.0.1:1/test"
os.environ["DB_CONNECT_RETRIES"] = "1"
os.environ["DB_CONNECT_TIMEOUT"] = "1"
os.environ["DB_SSL"] = "false"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402


@pytest.fixture
def client():
    # Using the client as a context manager runs startup and shutdown (lifespan).
    with TestClient(create_app()) as c:
        yield c
