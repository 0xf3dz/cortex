import hashlib
import hmac
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.whatsapp_client import FakeWhatsAppClient


APP_SECRET = "test-app-secret"
VERIFY_TOKEN = "test-verify-token-value"
ALLOWED_WA_ID = "61400000000"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        meta_app_secret=APP_SECRET,
        meta_access_token="test-access-token",
        meta_verify_token=VERIFY_TOKEN,
        allowed_whatsapp_wa_id=ALLOWED_WA_ID,
        whatsapp_phone_number_id="1228458710361487",
        whatsapp_graph_api_version="v25.0",
        database_path=tmp_path / "agent.sqlite3",
        worker_enabled=False,
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings, FakeWhatsAppClient())) as test_client:
        yield test_client


@pytest.fixture
def fixture_body() -> bytes:
    return Path("tests/fixtures/inbound_text.json").read_bytes()


@pytest.fixture
def sign() -> Callable[[bytes], dict[str, str]]:
    def signed_headers(body: bytes) -> dict[str, str]:
        digest = hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
        return {
            "content-type": "application/json",
            "x-hub-signature-256": f"sha256={digest}",
        }

    return signed_headers
