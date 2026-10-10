"""Manual session costs must survive saving and a fresh database read."""

import pytest

BASE = "/api/vehicles/V1/charging-costs"


@pytest.fixture
def cost_client(auth_client):
    response = auth_client.put(
        "/api/charging/tiers/public_ac", json={"price_kwh": 0.25}
    )
    assert response.status_code == 200
    return auth_client


def create_session(client, **fields):
    response = client.post(
        BASE,
        json={
            "start_ts": "2026-09-10T20:00:00",
            "end_ts": "2026-09-10T21:00:00",
            "tier_id": "public_ac",
            "energy_kwh": 10.0,
            **fields,
        },
    )
    assert response.status_code == 201
    return response.json()


def assert_saved_cost(client, session, expected):
    assert session["cost"] == expected
    response = client.get(BASE)
    assert response.status_code == 200
    [stored] = response.json()
    assert stored["id"] == session["id"]
    assert stored["cost"] == expected
    # The charging tab refreshes this list after saving.
    response = client.get("/api/vehicles/V1/charging-sessions?year=2026&month=9")
    assert response.status_code == 200
    [row] = response.json()["sessions"]
    assert row["id"] == session["id"]
    assert row["cost"] == expected


@pytest.mark.parametrize("cost", [12.34, 0.0])
@pytest.mark.parametrize("energy", [10.0, None])
def test_create_with_cost_override(cost_client, cost, energy):
    session = create_session(cost_client, cost=cost, energy_kwh=energy)
    assert_saved_cost(cost_client, session, cost)


@pytest.mark.parametrize("cost", [12.34, 0.0])
@pytest.mark.parametrize("energy", [10.0, None])
def test_update_with_cost_override(cost_client, cost, energy):
    session = create_session(cost_client, energy_kwh=energy)
    response = cost_client.put(
        f"{BASE}/{session['id']}",
        json={"cost": cost, "energy_kwh": energy, "note": "Receipt total"},
    )
    assert response.status_code == 200
    assert response.json()["note"] == "Receipt total"
    assert_saved_cost(cost_client, response.json(), cost)


@pytest.mark.parametrize("fields", [{}, {"cost": None}])
def test_create_without_override_calculates_tariff(cost_client, fields):
    session = create_session(cost_client, **fields)
    assert_saved_cost(cost_client, session, 2.5)


@pytest.mark.parametrize("fields", [{}, {"cost": None}])
def test_update_without_override_calculates_tariff(cost_client, fields):
    session = create_session(cost_client)
    response = cost_client.put(
        f"{BASE}/{session['id']}", json={"energy_kwh": 20.0, **fields}
    )
    assert response.status_code == 200
    assert_saved_cost(cost_client, response.json(), 5.0)


@pytest.mark.parametrize("energy, expected", [(10.0, 2.5), (None, None)])
def test_clear_override_returns_to_automatic_cost(cost_client, energy, expected):
    session = create_session(cost_client, cost=12.34, energy_kwh=energy)
    assert session["cost"] == 12.34
    response = cost_client.put(f"{BASE}/{session['id']}", json={"cost": None})
    assert response.status_code == 200
    assert_saved_cost(cost_client, response.json(), expected)
