from tests.conftest import signup


def test_health(client):
    assert client.get("/health").json()["status"] == "ok"
    body = client.get("/api/v1/health").json()
    assert body["database"] == "ok"


def test_signup_returns_token_cookie_and_wallet(client):
    body = signup(client, "New@Example.com")
    assert body["user"]["email"] == "new@example.com"
    assert body["user"]["theme"] is None  # frontend shows skin picker
    assert body["token_type"] == "bearer"
    assert "plan_session" in client.cookies
    headers = {"Authorization": f"Bearer {body['access_token']}"}
    wallet = client.get("/api/v1/wallet", headers=headers).json()
    assert wallet["balance_cents"] == 100_000
    assert wallet["currency"] == "PLAY"
    txs = client.get("/api/v1/wallet/transactions", headers=headers).json()
    assert txs["total"] == 1 and txs["items"][0]["type"] == "signup_bonus"


def test_signup_with_theme(client):
    # Direct signup with theme
    r = client.post(
        "/api/v1/auth/signup",
        json={"email": "fan-fd@example.com", "password": "hunter2-long", "theme": "fanduel"},
    )
    assert r.status_code == 201
    assert r.json()["user"]["theme"] == "fanduel"


def test_signup_duplicate_email(client):
    signup(client, "dup@example.com")
    r = client.post("/api/v1/auth/signup", json={"email": "DUP@example.com", "password": "another-pass"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "EMAIL_TAKEN"


def test_signup_validation(client):
    r = client.post("/api/v1/auth/signup", json={"email": "not-an-email", "password": "short"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_login_and_me_with_bearer(client):
    signup(client, "a@example.com", "correct-horse")
    client.cookies.clear()
    r = client.post("/api/v1/auth/login", json={"email": "a@example.com", "password": "correct-horse"})
    assert r.status_code == 200
    token = r.json()["access_token"]
    client.cookies.clear()
    me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["email"] == "a@example.com"


def test_me_with_cookie(client):
    signup(client, "cookie@example.com")
    assert client.get("/api/v1/auth/me").json()["email"] == "cookie@example.com"


def test_login_wrong_password_and_unknown_user(client):
    signup(client, "b@example.com", "right-password")
    for email, pw in [("b@example.com", "wrong-password"), ("nobody@example.com", "whatever1")]:
        r = client.post("/api/v1/auth/login", json={"email": email, "password": pw})
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "INVALID_CREDENTIALS"


def test_me_requires_auth(client):
    r = client.get("/api/v1/auth/me")
    assert r.status_code == 401
    r = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer garbage"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "INVALID_TOKEN"


def test_logout_revokes_session(client):
    token = signup(client, "c@example.com")["access_token"]
    client.cookies.clear()
    h = {"Authorization": f"Bearer {token}"}
    assert client.post("/api/v1/auth/logout", headers=h).status_code == 204
    r = client.get("/api/v1/auth/me", headers=h)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "SESSION_REVOKED"


def test_theme_preferences(client, auth_headers):
    r = client.get("/api/v1/users/me/preferences", headers=auth_headers)
    assert r.json() == {"theme": None}
    r = client.patch("/api/v1/users/me/preferences", json={"theme": "draftkings"}, headers=auth_headers)
    assert r.json() == {"theme": "draftkings"}
    assert client.get("/api/v1/auth/me", headers=auth_headers).json()["theme"] == "draftkings"
    bad = client.patch("/api/v1/users/me/preferences", json={"theme": "default"}, headers=auth_headers)
    assert bad.status_code == 422
