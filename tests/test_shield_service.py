import sqlite3

from app.services.shield_service import RateLimitPolicy, ShieldStore, make_code_hash


def test_shield_store_grants_session_for_valid_code(tmp_path) -> None:
    db_path = tmp_path / "shield.db"
    secret = "test-secret"
    code_hash = make_code_hash(secret, "open sesame")
    store = ShieldStore(db_path)

    result = store.authenticate_code(
        secret_key=secret,
        expected_code_hash=code_hash,
        submitted_code="open sesame",
        identity_key="ip:203.0.113.10",
        now=1000,
    )

    assert result.ok
    assert result.session_token
    assert store.session_is_valid(
        secret_key=secret, session_token=result.session_token, now=1001
    )


def test_shield_store_rejects_bad_code_and_records_event(tmp_path) -> None:
    db_path = tmp_path / "shield.db"
    secret = "test-secret"
    code_hash = make_code_hash(secret, "correct")
    store = ShieldStore(db_path)

    result = store.authenticate_code(
        secret_key=secret,
        expected_code_hash=code_hash,
        submitted_code="wrong",
        identity_key="ip:203.0.113.10",
        now=1000,
    )

    assert not result.ok
    with sqlite3.connect(db_path) as conn:
        events = conn.execute("SELECT event FROM shield_event").fetchall()
    assert events == [("denied",)]


def test_shield_store_rate_limits_auth_attempts(tmp_path) -> None:
    db_path = tmp_path / "shield.db"
    secret = "test-secret"
    code_hash = make_code_hash(secret, "correct")
    store = ShieldStore(db_path)

    results = [
        store.authenticate_code(
            secret_key=secret,
            expected_code_hash=code_hash,
            submitted_code="wrong",
            identity_key="ip:203.0.113.10",
            now=1000,
        )
        for _ in range(6)
    ]

    assert [result.rate_limited for result in results] == [
        False,
        False,
        False,
        False,
        False,
        True,
    ]
    with sqlite3.connect(db_path) as conn:
        events = conn.execute("SELECT event FROM shield_event ORDER BY id").fetchall()
    assert events[-1] == ("rate_limited",)


def test_shield_store_session_expires_after_ttl(tmp_path) -> None:
    db_path = tmp_path / "shield.db"
    secret = "test-secret"
    code_hash = make_code_hash(secret, "correct")
    store = ShieldStore(db_path)

    result = store.authenticate_code(
        secret_key=secret,
        expected_code_hash=code_hash,
        submitted_code="correct",
        identity_key="ip:203.0.113.10",
        now=1000,
        session_ttl_sec=60,
    )

    assert result.session_token
    assert store.session_is_valid(
        secret_key=secret, session_token=result.session_token, now=1059
    )
    assert not store.session_is_valid(
        secret_key=secret, session_token=result.session_token, now=1060
    )


def test_shield_session_validation_is_read_only(tmp_path) -> None:
    db_path = tmp_path / "shield.db"
    secret = "test-secret"
    store = ShieldStore(db_path)
    result = store.authenticate_code(
        secret_key=secret,
        expected_code_hash=make_code_hash(secret, "correct"),
        submitted_code="correct",
        identity_key="ip:203.0.113.10",
        now=1000,
        session_ttl_sec=60,
    )
    assert result.session_token

    with sqlite3.connect(db_path) as conn:
        before = conn.execute("SELECT last_seen_at FROM shield_session").fetchone()
        conn.execute(
            "INSERT INTO shield_session(id, output_key, session_hash, created_at, expires_at, last_seen_at) "
            "VALUES ('expired', 'global', 'expired-hash', 1, 2, 1)"
        )
        conn.commit()

    assert store.session_is_valid(
        secret_key=secret,
        session_token=result.session_token,
        now=1001,
    )

    with sqlite3.connect(db_path) as conn:
        after = conn.execute(
            "SELECT last_seen_at FROM shield_session WHERE id != 'expired'"
        ).fetchone()
        expired_count = conn.execute(
            "SELECT COUNT(*) FROM shield_session WHERE id = 'expired'"
        ).fetchone()[0]
    assert after == before
    assert expired_count == 1


def test_shield_session_validation_does_not_initialize_schema(
    tmp_path, monkeypatch
) -> None:
    db_path = tmp_path / "shield.db"
    secret = "test-secret"
    store = ShieldStore(db_path)
    result = store.authenticate_code(
        secret_key=secret,
        expected_code_hash=make_code_hash(secret, "correct"),
        submitted_code="correct",
        identity_key="ip:203.0.113.10",
        now=1000,
    )
    assert result.session_token
    monkeypatch.setattr(
        store, "init", lambda: (_ for _ in ()).throw(AssertionError("init called"))
    )

    assert store.session_is_valid(
        secret_key=secret,
        session_token=result.session_token,
        now=1001,
    )


def test_shield_events_are_bounded_by_age_and_count(tmp_path) -> None:
    db_path = tmp_path / "shield.db"
    secret = "test-secret"
    code_hash = make_code_hash(secret, "correct")
    store = ShieldStore(db_path, event_retention_sec=100, event_max_rows=3)

    for timestamp in (1, 2, 200, 201, 202, 203):
        store.authenticate_code(
            secret_key=secret,
            expected_code_hash=code_hash,
            submitted_code="wrong",
            identity_key=f"ip:203.0.113.{timestamp}",
            now=timestamp,
            policies=(),
        )

    with sqlite3.connect(db_path) as conn:
        events = conn.execute(
            "SELECT at, event FROM shield_event ORDER BY id"
        ).fetchall()
        indexes = {row[1] for row in conn.execute("PRAGMA index_list('shield_event')")}
    assert events == [(201, "denied"), (202, "denied"), (203, "denied")]
    assert "ix_shield_event_at" in indexes


def test_shield_rate_buckets_expire_after_longest_refill_window(tmp_path) -> None:
    db_path = tmp_path / "shield.db"
    secret = "test-secret"
    code_hash = make_code_hash(secret, "correct")
    policy = (RateLimitPolicy("short", 2, 10),)
    store = ShieldStore(db_path)

    for timestamp, identity in ((1, "first"), (12, "second")):
        store.authenticate_code(
            secret_key=secret,
            expected_code_hash=code_hash,
            submitted_code="wrong",
            identity_key=identity,
            now=timestamp,
            policies=policy,
        )

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT key, updated_at FROM rate_limit_bucket ORDER BY key"
        ).fetchall()
        indexes = {
            row[1] for row in conn.execute("PRAGMA index_list('rate_limit_bucket')")
        }
    assert rows == [("shield_auth:second:short", 12)]
    assert "ix_rate_limit_bucket_updated_at" in indexes


def test_shield_rate_bucket_capacity_fails_closed(tmp_path) -> None:
    db_path = tmp_path / "shield.db"
    secret = "test-secret"
    code_hash = make_code_hash(secret, "correct")
    policy = (RateLimitPolicy("short", 2, 100),)
    store = ShieldStore(db_path, rate_limit_bucket_max_rows=2)

    results = [
        store.authenticate_code(
            secret_key=secret,
            expected_code_hash=code_hash,
            submitted_code="wrong",
            identity_key=f"identity-{index}",
            now=10,
            policies=policy,
        )
        for index in range(3)
    ]

    assert [result.rate_limited for result in results] == [False, False, True]
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM rate_limit_bucket").fetchone()[0] == 2
