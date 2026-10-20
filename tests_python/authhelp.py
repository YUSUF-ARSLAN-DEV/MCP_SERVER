"""Helpers for the API tests: there are no administrators and no setup code. Everyone just signs up."""
from fastapi.testclient import TestClient

PW = "correct horse battery"


def sign_up(client: TestClient, name: str = "tester1", password: str = PW) -> TestClient:
    """Create an account on the server `client` talks to; `client` is signed in as them afterwards."""
    response = client.post("/api/signup", json={"username": name, "password": password})
    assert response.status_code == 200, response.text
    return client


def another_person(client: TestClient, name: str) -> TestClient:
    """A different person's browser: its own cookies, the same running server."""
    return sign_up(TestClient(client.app), name)
