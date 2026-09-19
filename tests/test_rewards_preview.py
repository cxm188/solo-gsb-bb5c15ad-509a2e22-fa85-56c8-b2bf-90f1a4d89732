"""Tests for POST /rewards/preview: dry-run prize settlement."""

import pytest
from fastapi.testclient import TestClient

import app.db as db_module
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient backed by an isolated SQLite file."""
    monkeypatch.setattr(db_module, "DB_PATH", str(tmp_path / "rewards-test.db"))
    db_module.init_db()
    return TestClient(app)


def _post(client, event_id, participant_id, points, correction_of=None):
    payload = {
        "event_id": event_id,
        "participant_id": participant_id,
        "points": points,
    }
    if correction_of is not None:
        payload["correction_of"] = correction_of
    r = client.post("/events", json=payload)
    assert r.status_code == 201
    return r.json()


def _preview(client, prize_cents, revision=None):
    payload = {"prize_cents": prize_cents}
    if revision is not None:
        payload["revision"] = revision
    r = client.post("/rewards/preview", json=payload)
    assert r.status_code == 200, r.text
    return r.json()


def test_empty_ledger_returns_no_winners(client):
    assert _preview(client, [100, 50]) == {"revision": 0, "winners": []}


def test_distinct_totals_paid_by_rank(client):
    _post(client, "e1", "alice", 30)
    _post(client, "e2", "bob", 20)
    _post(client, "e3", "carol", 10)

    body = _preview(client, [1000, 500, 250, 100])
    assert body["revision"] == 3
    assert body["winners"] == [
        {"participant_id": "alice", "rank_start": 1, "rank_end": 1,
         "total_points": 30, "prize_cents": 1000},
        {"participant_id": "bob", "rank_start": 2, "rank_end": 2,
         "total_points": 20, "prize_cents": 500},
        {"participant_id": "carol", "rank_start": 3, "rank_end": 3,
         "total_points": 10, "prize_cents": 250},
    ]


def test_tie_group_splits_merged_pool(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 10)
    _post(client, "e3", "carol", 5)

    body = _preview(client, [100, 50, 30])
    # alice/bob tie over ranks 1-2: pool 100+50=150, split 75 each.
    assert body["winners"] == [
        {"participant_id": "alice", "rank_start": 1, "rank_end": 2,
         "total_points": 10, "prize_cents": 75},
        {"participant_id": "bob", "rank_start": 1, "rank_end": 2,
         "total_points": 10, "prize_cents": 75},
        {"participant_id": "carol", "rank_start": 3, "rank_end": 3,
         "total_points": 5, "prize_cents": 30},
    ]


def test_leftover_cents_go_to_lowest_participant_id(client):
    _post(client, "e1", "carol", 10)
    _post(client, "e2", "alice", 10)
    _post(client, "e3", "bob", 10)

    body = _preview(client, [100])
    # Three-way tie over ranks 1-3, only rank 1 in the zone: pool 100,
    # share 33 each, the 1 leftover cent goes to the lowest participant_id.
    assert body["winners"] == [
        {"participant_id": "alice", "rank_start": 1, "rank_end": 3,
         "total_points": 10, "prize_cents": 34},
        {"participant_id": "bob", "rank_start": 1, "rank_end": 3,
         "total_points": 10, "prize_cents": 33},
        {"participant_id": "carol", "rank_start": 1, "rank_end": 3,
         "total_points": 10, "prize_cents": 33},
    ]
    assert sum(w["prize_cents"] for w in body["winners"]) == 100


def test_group_straddling_prize_zone_boundary(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 10)

    body = _preview(client, [101])
    # Tie over ranks 1-2 but only rank 1 is prized: pool 101, split 50/51,
    # the leftover cent goes to alice (lower participant_id).
    assert [w["prize_cents"] for w in body["winners"]] == [51, 50]
    assert sum(w["prize_cents"] for w in body["winners"]) == 101


def test_group_beyond_prize_zone_gets_nothing(client):
    _post(client, "e1", "alice", 20)
    _post(client, "e2", "bob", 10)
    _post(client, "e3", "carol", 10)

    body = _preview(client, [100])
    assert body["winners"] == [
        {"participant_id": "alice", "rank_start": 1, "rank_end": 1,
         "total_points": 20, "prize_cents": 100},
    ]


def test_zero_prize_rank_is_still_in_the_zone(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 5)

    body = _preview(client, [0, 50])
    assert body["winners"] == [
        {"participant_id": "alice", "rank_start": 1, "rank_end": 1,
         "total_points": 10, "prize_cents": 0},
        {"participant_id": "bob", "rank_start": 2, "rank_end": 2,
         "total_points": 5, "prize_cents": 50},
    ]


def test_revision_pins_the_snapshot(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 20)

    rev1 = _preview(client, [100], revision=1)
    assert rev1["revision"] == 1
    assert [w["participant_id"] for w in rev1["winners"]] == ["alice"]

    latest = _preview(client, [100])
    assert latest["revision"] == 2
    assert [w["participant_id"] for w in latest["winners"]] == ["bob"]


def test_correction_chain_counts_only_tip(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 8)
    # Re-attribute alice's event to bob with +12.
    _post(client, "e3", "bob", 12, correction_of="e1")

    body = _preview(client, [100, 50])
    assert body["winners"] == [
        {"participant_id": "bob", "rank_start": 1, "rank_end": 1,
         "total_points": 20, "prize_cents": 100},
    ]

    rev2 = _preview(client, [100, 50], revision=2)
    assert [w["participant_id"] for w in rev2["winners"]] == ["alice", "bob"]


def test_unknown_revision_returns_404(client):
    _post(client, "e1", "alice", 10)
    for bad in (0, -1, 2, 99999):
        r = client.post("/rewards/preview",
                        json={"revision": bad, "prize_cents": [100]})
        assert r.status_code == 404, bad


def test_prize_cents_validation(client):
    _post(client, "e1", "alice", 10)
    # Empty/over-long, negative, fractional and non-JSON-integer items are
    # all 422: booleans, numeric strings and whole-number floats must never
    # be lax-coerced into cents.
    for bad in (
        [],
        [-1],
        [100, -5],
        ["abc"],
        [1.5],
        [True],
        [False],
        [1.0],
        [100, 2.0, 50],
        ["100"],
        list(range(101)),
    ):
        r = client.post("/rewards/preview", json={"prize_cents": bad})
        assert r.status_code == 422, bad
    # prize_cents is required
    assert client.post("/rewards/preview", json={}).status_code == 422
    # non-integer revision is a validation error, not a 404
    r = client.post("/rewards/preview",
                    json={"revision": "abc", "prize_cents": [100]})
    assert r.status_code == 422


@pytest.mark.parametrize(
    "revision",
    [
        pytest.param("3", id="numeric-string"),
        pytest.param("1", id="unit-string"),
        pytest.param(1.0, id="whole-float"),
        pytest.param(2.0, id="whole-float-2"),
        pytest.param(3.5, id="fractional-float"),
        pytest.param(True, id="bool-true"),
        pytest.param(False, id="bool-false"),
        pytest.param([1], id="array"),
        pytest.param({"revision": 1}, id="object"),
    ],
)
def test_revision_accepts_only_json_integer_or_null(client, revision):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 20)

    r = client.post(
        "/rewards/preview", json={"revision": revision, "prize_cents": [100]}
    )
    assert r.status_code == 422, revision
    # the error is pinned to the revision field, not the body or prize list
    assert {tuple(err["loc"]) for err in r.json()["detail"]} == {
        ("body", "revision")
    }
    # failed validation never reaches the ledger layer, so no 404 path runs
    # even for values that look out of range after coercion (e.g. False -> 0)
    assert client.get("/leaderboard").json()["revision"] == 2


def test_null_and_omitted_revision_default_to_latest_snapshot(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 20)

    omitted = client.post("/rewards/preview", json={"prize_cents": [100]}).json()
    explicit_null = client.post(
        "/rewards/preview", json={"revision": None, "prize_cents": [100]}
    ).json()
    assert omitted["revision"] == 2
    assert explicit_null == omitted
    assert [w["participant_id"] for w in omitted["winners"]] == ["bob"]


def test_valid_integer_revision_pins_historical_snapshot(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 20)

    body = _preview(client, [100], revision=1)
    assert body["revision"] == 1
    assert [w["participant_id"] for w in body["winners"]] == ["alice"]


def test_huge_integer_revision_is_a_range_404_not_a_type_422(client):
    # A JSON integer outside int64 is still a legal JSON integer: type
    # validation passes and the ledger layer reports it as out of range,
    # exactly as for small out-of-range integers.
    _post(client, "e1", "alice", 10)
    r = client.post(
        "/rewards/preview",
        json={"revision": 2**63, "prize_cents": [100]},
    )
    assert r.status_code == 404
    r = client.post(
        "/rewards/preview",
        json={"revision": 10**100, "prize_cents": [100]},
    )
    assert r.status_code == 404


@pytest.mark.parametrize(
    "bad_value",
    [
        pytest.param(True, id="bool"),
        pytest.param(1.0, id="whole-float"),
        pytest.param("100", id="numeric-string"),
        pytest.param(-1, id="negative"),
    ],
)
def test_nested_prize_cents_errors_pinpoint_array_index(client, bad_value):
    _post(client, "e1", "alice", 10)
    prizes = [100, bad_value, 50]
    r = client.post("/rewards/preview", json={"prize_cents": prizes})
    assert r.status_code == 422
    locs = [tuple(err["loc"]) for err in r.json()["detail"]]
    # the offending element is located at body -> prize_cents -> [1],
    # never reported as a generic body or prize_cents error
    assert ("body", "prize_cents", 1) in locs
    assert all(loc[:2] == ("body", "prize_cents") for loc in locs)


def test_huge_non_negative_integer_prize_is_accepted(client):
    _post(client, "e1", "alice", 10)
    big = 10**100  # beyond int64 but a legal non-negative JSON integer
    body = _preview(client, [big])
    assert body["winners"] == [
        {"participant_id": "alice", "rank_start": 1, "rank_end": 1,
         "total_points": 10, "prize_cents": big},
    ]


@pytest.mark.parametrize("size", [1, 100])
def test_prize_count_boundaries_accepted(client, size):
    _post(client, "e1", "alice", 10)
    body = _preview(client, [0] * size)
    assert body["winners"] == [
        {"participant_id": "alice", "rank_start": 1, "rank_end": 1,
         "total_points": 10, "prize_cents": 0},
    ]


def test_invalid_requests_never_write_to_the_ledger(client):
    _post(client, "e1", "alice", 10)
    before_events = client.get("/events").json()
    before = client.get("/leaderboard").json()

    bad_payloads = [
        {"revision": True, "prize_cents": [100]},
        {"revision": 1.0, "prize_cents": [100]},
        {"revision": "1", "prize_cents": [100]},
        {"prize_cents": [100, True]},
        {"prize_cents": [100, 2.0]},
        {"prize_cents": ["100"]},
        {"prize_cents": [-1]},
        {"prize_cents": []},
    ]
    for payload in bad_payloads:
        r = client.post("/rewards/preview", json=payload)
        assert r.status_code == 422, payload

    assert client.get("/events").json() == before_events
    assert client.get("/leaderboard").json() == before


def test_preview_does_not_write_to_the_ledger(client):
    _post(client, "e1", "alice", 10)
    before_events = client.get("/events").json()
    before_revision = client.get("/leaderboard").json()["revision"]

    _preview(client, [100, 50])

    assert client.get("/events").json() == before_events
    assert client.get("/leaderboard").json()["revision"] == before_revision
