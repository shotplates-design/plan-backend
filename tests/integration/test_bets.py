"""Place a bet end-to-end using frontend selection_key convention."""

from tests.conftest import signup


def _board(client):
    r = client.get("/api/v1/games", params={"season": 2026, "week": 5})
    assert r.status_code == 200, r.text
    games = r.json()
    assert len(games) >= 6
    return games


def test_board_shape_matches_frontend_contract(client, seeded, paywall_on):
    games = _board(client)
    g = next(x for x in games if x["id"] == "tb-dal")
    assert set(g["away"]) >= {"abbr", "name", "short", "color"}
    assert g["away"]["abbr"] == "TB" and g["home"]["abbr"] == "DAL"
    assert "kickoff" in g and g["week"] == 5
    assert g["spread"]["away"]["line"].startswith("+") and isinstance(g["spread"]["away"]["price"], int)
    assert isinstance(g["money"]["away"], int) and isinstance(g["money"]["home"], int)
    assert "line" in g["total"] and "over" in g["total"] and "under" in g["total"]
    assert len(g["props"]) >= 1
    prop = next(p for p in g["props"] if p["id"] == "godwin-rec")
    assert prop["player"] == "Chris Godwin"
    assert prop["team"] == "TB"
    assert prop["market"] == "Receptions"
    assert prop["line"] == "Over 4.5"
    assert prop["selectionKey"] == "tb-dal|prop|godwin-rec"
    # Paywall: props not unlocked by the caller show only the L5 teaser; locked values never sent.
    assert prop["hitRate"]["last5"]["pct"] == 100
    assert prop["hitRate"]["season"] is None
    assert prop["hitRate"]["vsOpp"] is None
    assert prop["locked"] == ["season", "vs_opponent"]
    assert prop["unlocked"] is False


def test_board_full_hit_rates_after_unlock(client, seeded, paywall_on):
    body = signup(client, "unlock-board@example.com")
    h = {"Authorization": f"Bearer {body['access_token']}"}
    assert client.get("/api/v1/games/tb-dal/props/godwin-rec", headers=h).status_code == 200
    games = client.get("/api/v1/games", params={"season": 2026, "week": 5}, headers=h).json()
    g = next(x for x in games if x["id"] == "tb-dal")
    prop = next(p for p in g["props"] if p["id"] == "godwin-rec")
    assert prop["hitRate"]["season"]["pct"] == 100
    assert prop["hitRate"]["vsOpp"]["label"] == "vs DAL"
    assert prop["locked"] == [] and prop["unlocked"] is True
    other = next(p for p in g["props"] if p["id"] == "lamb-rec")
    assert other["unlocked"] is False and other["hitRate"]["season"] is None


def test_board_full_hit_rates_with_paywall_off_default(client, seeded):
    g = next(x for x in _board(client) if x["id"] == "tb-dal")
    prop = next(p for p in g["props"] if p["id"] == "godwin-rec")
    assert prop["unlocked"] is True and prop["locked"] == []
    assert prop["hitRate"]["season"]["pct"] == 100 and prop["hitRate"]["vsOpp"]["label"] == "vs DAL"


def test_place_single_and_parlay(client, seeded, auth_headers):
    games = _board(client)
    tb = next(x for x in games if x["id"] == "tb-dal")
    kc = next(x for x in games if x["id"] == "kc-buf")

    # Quote then place a single on TB +7.5
    key = "tb-dal|spread|away"
    price = tb["spread"]["away"]["price"]
    line = tb["spread"]["away"]["line"]
    quote = client.post(
        "/api/v1/bets/quote",
        headers=auth_headers,
        json={
            "type": "single",
            "stake_cents": 1100,
            "legs": [{"selection_key": key, "odds_american": price, "line": line}],
        },
    )
    assert quote.status_code == 200, quote.text
    assert quote.json()["potential_payout_cents"] > 1100

    placed = client.post(
        "/api/v1/bets",
        headers=auth_headers,
        json={
            "type": "single",
            "stake_cents": 1100,
            "idempotency_key": "test-single-1",
            "legs": [{"selection_key": key, "odds_american": price, "line": line}],
        },
    )
    assert placed.status_code == 201, placed.text
    body = placed.json()
    assert body["status"] == "pending"
    assert body["legs"][0]["selection_key"] == key
    assert body["stake_cents"] == 1100

    wallet = client.get("/api/v1/wallet", headers=auth_headers).json()
    assert wallet["balance_cents"] == 100_000 - 1100

    # Idempotent retry
    again = client.post(
        "/api/v1/bets",
        headers=auth_headers,
        json={
            "type": "single",
            "stake_cents": 1100,
            "idempotency_key": "test-single-1",
            "legs": [{"selection_key": key, "odds_american": price, "line": line}],
        },
    )
    assert again.status_code == 200
    assert again.json()["id"] == body["id"]
    assert client.get("/api/v1/wallet", headers=auth_headers).json()["balance_cents"] == 100_000 - 1100

    # Parlay: TB spread + KC moneyline away
    parlay = client.post(
        "/api/v1/bets",
        headers=auth_headers,
        json={
            "type": "parlay",
            "stake_cents": 2000,
            "legs": [
                {"selection_key": "tb-dal|spread|away", "odds_american": price, "line": line},
                {
                    "selection_key": "kc-buf|money|away",
                    "odds_american": kc["money"]["away"],
                },
            ],
        },
    )
    assert parlay.status_code == 201, parlay.text
    assert parlay.json()["type"] == "parlay"
    assert len(parlay.json()["legs"]) == 2

    # Prop leg
    prop = tb["props"][0]
    prop_bet = client.post(
        "/api/v1/bets",
        headers=auth_headers,
        json={
            "type": "single",
            "stake_cents": 500,
            "legs": [
                {
                    "selection_key": prop["selectionKey"],
                    "odds_american": prop["price"],
                    "line": prop["line"],
                }
            ],
        },
    )
    assert prop_bet.status_code == 201, prop_bet.text
    assert "prop|" in prop_bet.json()["legs"][0]["selection_key"]

    mine = client.get("/api/v1/bets", headers=auth_headers).json()
    assert mine["total"] == 3


def test_odds_changed_rejected(client, seeded, auth_headers):
    r = client.post(
        "/api/v1/bets",
        headers=auth_headers,
        json={
            "type": "single",
            "stake_cents": 1000,
            "legs": [{"selection_key": "tb-dal|money|home", "odds_american": -999}],
        },
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "ODDS_CHANGED"


def test_insufficient_funds(client, seeded, auth_headers):
    r = client.post(
        "/api/v1/bets",
        headers=auth_headers,
        json={
            "type": "single",
            "stake_cents": 999_999_00,
            "legs": [{"selection_key": "tb-dal|money|away", "odds_american": 280}],
        },
    )
    # stake above max → STAKE_OUT_OF_RANGE; use just-over-balance within max
    assert r.status_code in (400, 422)
    r = client.post(
        "/api/v1/bets",
        headers=auth_headers,
        json={
            "type": "single",
            "stake_cents": 100_001,  # 1 cent over signup bonus, within max
            "legs": [{"selection_key": "tb-dal|money|away", "odds_american": 280}],
        },
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INSUFFICIENT_FUNDS"


def test_list_and_get_bet(client, seeded, auth_headers):
    placed = client.post(
        "/api/v1/bets",
        headers=auth_headers,
        json={
            "type": "single",
            "stake_cents": 1000,
            "legs": [{"selection_key": "bal-cin|total|over", "odds_american": -110, "line": 51.5}],
        },
    )
    assert placed.status_code == 201, placed.text
    bet_id = placed.json()["id"]
    got = client.get(f"/api/v1/bets/{bet_id}", headers=auth_headers)
    assert got.status_code == 200
    assert got.json()["id"] == bet_id
