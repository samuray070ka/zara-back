"""Shared fixtures for UzMarket backend tests."""
import os
import time
import requests
import pytest

BASE_URL = os.environ.get("EXPO_PUBLIC_BACKEND_URL") or os.environ.get(
    "EXPO_BACKEND_URL"
) or "https://couriers.preview.emergentagent.com"
BASE_URL = BASE_URL.rstrip("/")
API = f"{BASE_URL}/api"

ADMIN_PHONE = "+998900000000"
SELLER_PHONE = "+998901111111"
COURIER_PHONE = "+998902222222"
CLIENT_PHONE = "+998903333333"
PENDING_SELLER_PHONE = "+998905555555"


def _login(session: requests.Session, phone: str, retries: int = 3) -> dict:
    """Send OTP and verify, handling 429 rate limit by waiting."""
    last_err = None
    for attempt in range(retries):
        r = session.post(f"{API}/auth/send-otp", json={"phone": phone})
        if r.status_code == 429:
            wait = 62
            print(f"[login] 429 rate-limit for {phone}, sleep {wait}s")
            time.sleep(wait)
            continue
        if r.status_code != 200:
            last_err = f"send-otp {r.status_code}: {r.text}"
            time.sleep(2)
            continue
        code = r.json()["demo_code"]
        v = session.post(
            f"{API}/auth/verify-otp",
            json={"phone": phone, "code": code, "first_name": "Test"},
        )
        if v.status_code == 200:
            return v.json()
        last_err = f"verify-otp {v.status_code}: {v.text}"
        time.sleep(2)
    raise RuntimeError(f"login failed for {phone}: {last_err}")


@pytest.fixture(scope="session")
def api_base() -> str:
    return API


@pytest.fixture(scope="session")
def http() -> requests.Session:
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="session")
def admin_auth(http):
    data = _login(http, ADMIN_PHONE)
    return data


@pytest.fixture(scope="session")
def seller_auth(http):
    return _login(http, SELLER_PHONE)


@pytest.fixture(scope="session")
def courier_auth(http):
    return _login(http, COURIER_PHONE)


@pytest.fixture(scope="session")
def client_auth(http):
    return _login(http, CLIENT_PHONE)


def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
