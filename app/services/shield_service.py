from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import secrets
import sqlite3
import threading
import time
from pathlib import Path


CODE_HASH_PREFIX = "hmac-sha256:v1:"
SESSION_COOKIE_NAME = "__Host-shield_session"
SESSION_TTL_SEC = 60 * 60 * 24 * 7
MAX_CODE_CHARS = 128
SHIELD_EVENT_RETENTION_SEC = 60 * 60 * 24 * 30
SHIELD_EVENT_MAX_ROWS = 10_000
RATE_LIMIT_BUCKET_MAX_ROWS = 20_000


@dataclass(frozen=True)
class RateLimitPolicy:
    suffix: str
    capacity: float
    refill_seconds: int

    @property
    def refill_per_second(self) -> float:
        return self.capacity / self.refill_seconds


@dataclass(frozen=True)
class AuthAttemptResult:
    ok: bool
    rate_limited: bool = False
    session_token: str | None = None


DEFAULT_AUTH_POLICIES: tuple[RateLimitPolicy, ...] = (
    RateLimitPolicy("hour", 5, 60 * 60),
    RateLimitPolicy("day", 10, 60 * 60 * 24),
)


def normalize_access_code(value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > MAX_CODE_CHARS:
        return ""
    return normalized


def make_code_hash(secret_key: str, access_code: str) -> str:
    normalized = normalize_access_code(access_code)
    if not normalized:
        raise ValueError("access code cannot be empty")
    digest = hmac.new(
        secret_key.encode("utf-8"), normalized.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"{CODE_HASH_PREFIX}{digest}"


def verify_code_hash(secret_key: str, access_code: str, expected_hash: str) -> bool:
    normalized = normalize_access_code(access_code)
    expected = expected_hash.strip()
    if not normalized or not expected.startswith(CODE_HASH_PREFIX):
        return False
    actual = make_code_hash(secret_key, normalized)
    return hmac.compare_digest(actual, expected)


def _session_hash(secret_key: str, session_token: str) -> str:
    return hmac.new(
        secret_key.encode("utf-8"), session_token.encode("utf-8"), hashlib.sha256
    ).hexdigest()


class ShieldStore:
    def __init__(
        self,
        db_path: Path | str,
        *,
        event_retention_sec: int = SHIELD_EVENT_RETENTION_SEC,
        event_max_rows: int = SHIELD_EVENT_MAX_ROWS,
        rate_limit_bucket_max_rows: int = RATE_LIMIT_BUCKET_MAX_ROWS,
    ) -> None:
        self.db_path = Path(db_path)
        self.event_retention_sec = max(0, event_retention_sec)
        self.event_max_rows = max(1, event_max_rows)
        self.rate_limit_bucket_max_rows = max(0, rate_limit_bucket_max_rows)
        self._initialized = False
        self._init_lock = threading.Lock()

    def init(self) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS shield_session (
                      id TEXT PRIMARY KEY,
                      output_key TEXT NOT NULL,
                      session_hash TEXT NOT NULL UNIQUE,
                      created_at INTEGER NOT NULL,
                      expires_at INTEGER NOT NULL,
                      last_seen_at INTEGER NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS rate_limit_bucket (
                      key TEXT PRIMARY KEY,
                      tokens REAL NOT NULL,
                      updated_at INTEGER NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS shield_event (
                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                      at INTEGER NOT NULL,
                      output_key TEXT,
                      event TEXT NOT NULL,
                      identity_key TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS ix_shield_session_hash
                      ON shield_session(session_hash);
                    CREATE INDEX IF NOT EXISTS ix_shield_session_expires
                      ON shield_session(expires_at);
                    CREATE INDEX IF NOT EXISTS ix_shield_event_at
                      ON shield_event(at);
                    CREATE INDEX IF NOT EXISTS ix_rate_limit_bucket_updated_at
                      ON rate_limit_bucket(updated_at);
                    """
                )
            self._initialized = True

    def authenticate_code(
        self,
        *,
        secret_key: str,
        expected_code_hash: str,
        submitted_code: str,
        identity_key: str,
        output_key: str = "global",
        now: int | None = None,
        policies: tuple[RateLimitPolicy, ...] = DEFAULT_AUTH_POLICIES,
        session_ttl_sec: int = SESSION_TTL_SEC,
    ) -> AuthAttemptResult:
        timestamp = int(time.time()) if now is None else now
        self.init()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._prune_expired_sessions(conn, timestamp)
            self._prune_rate_limit_buckets(conn, timestamp, policies)
            if not self._consume_rate_limits(conn, identity_key, timestamp, policies):
                self._record_event(
                    conn, timestamp, output_key, "rate_limited", identity_key
                )
                conn.commit()
                return AuthAttemptResult(ok=False, rate_limited=True)
            if not verify_code_hash(secret_key, submitted_code, expected_code_hash):
                self._record_event(conn, timestamp, output_key, "denied", identity_key)
                conn.commit()
                return AuthAttemptResult(ok=False)
            token = secrets.token_urlsafe(32)
            token_hash = _session_hash(secret_key, token)
            conn.execute(
                """
                INSERT INTO shield_session(id, output_key, session_hash, created_at, expires_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    secrets.token_hex(16),
                    output_key,
                    token_hash,
                    timestamp,
                    timestamp + session_ttl_sec,
                    timestamp,
                ),
            )
            self._record_event(conn, timestamp, output_key, "granted", identity_key)
            conn.commit()
            return AuthAttemptResult(ok=True, session_token=token)

    def session_is_valid(
        self,
        *,
        secret_key: str,
        session_token: str | None,
        output_key: str = "global",
        now: int | None = None,
    ) -> bool:
        if not session_token:
            return False
        timestamp = int(time.time()) if now is None else now
        token_hash = _session_hash(secret_key, session_token)
        try:
            # Nginx calls this for every protected request, so validation cannot prune or refresh rows.
            with self._connect(read_only=True) as conn:
                row = conn.execute(
                    """
                    SELECT 1
                    FROM shield_session
                    WHERE session_hash = ?
                      AND expires_at > ?
                      AND output_key IN (?, 'global')
                    LIMIT 1
                    """,
                    (token_hash, timestamp, output_key),
                ).fetchone()
        except sqlite3.OperationalError:
            return False
        return row is not None

    def _connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        if read_only:
            database = f"{self.db_path.resolve().as_uri()}?mode=ro"
            conn = sqlite3.connect(
                database, timeout=1.0, isolation_level=None, uri=True
            )
        else:
            conn = sqlite3.connect(str(self.db_path), timeout=1.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=1000")
        return conn

    def _consume_rate_limits(
        self,
        conn: sqlite3.Connection,
        identity_key: str,
        now: int,
        policies: tuple[RateLimitPolicy, ...],
    ) -> bool:
        updates: list[tuple[str, float, int]] = []
        missing_bucket_count = 0
        for policy in policies:
            key = f"shield_auth:{identity_key}:{policy.suffix}"
            row = conn.execute(
                "SELECT tokens, updated_at FROM rate_limit_bucket WHERE key = ?",
                (key,),
            ).fetchone()
            if row is None:
                missing_bucket_count += 1
                tokens = policy.capacity
                updated_at = now
            else:
                elapsed = max(0, now - int(row["updated_at"]))
                tokens = min(
                    policy.capacity,
                    float(row["tokens"]) + elapsed * policy.refill_per_second,
                )
                updated_at = now
            if tokens < 1:
                return False
            updates.append((key, tokens - 1, updated_at))
        if missing_bucket_count:
            bucket_count = int(
                conn.execute("SELECT COUNT(*) FROM rate_limit_bucket").fetchone()[0]
            )
            # Capacity exhaustion fails closed instead of weakening rate limits by
            # evicting live identities to make room for an attacker.
            if bucket_count + missing_bucket_count > self.rate_limit_bucket_max_rows:
                return False
        for key, tokens, updated_at in updates:
            conn.execute(
                """
                INSERT INTO rate_limit_bucket(key, tokens, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET tokens = excluded.tokens, updated_at = excluded.updated_at
                """,
                (key, tokens, updated_at),
            )
        return True

    @staticmethod
    def _prune_expired_sessions(conn: sqlite3.Connection, now: int) -> None:
        conn.execute("DELETE FROM shield_session WHERE expires_at <= ?", (now,))

    @staticmethod
    def _prune_rate_limit_buckets(
        conn: sqlite3.Connection,
        now: int,
        policies: tuple[RateLimitPolicy, ...],
    ) -> None:
        longest_refill = max((policy.refill_seconds for policy in policies), default=0)
        conn.execute(
            "DELETE FROM rate_limit_bucket WHERE updated_at <= ?",
            (now - longest_refill,),
        )

    def _record_event(
        self,
        conn: sqlite3.Connection,
        timestamp: int,
        output_key: str,
        event: str,
        identity_key: str,
    ) -> None:
        conn.execute(
            "INSERT INTO shield_event(at, output_key, event, identity_key) VALUES (?, ?, ?, ?)",
            (timestamp, output_key, event, identity_key),
        )
        conn.execute(
            "DELETE FROM shield_event WHERE at <= ?",
            (timestamp - self.event_retention_sec,),
        )
        conn.execute(
            """
            DELETE FROM shield_event
            WHERE id <= COALESCE(
              (SELECT id FROM shield_event ORDER BY id DESC LIMIT 1 OFFSET ?),
              0
            )
            """,
            (self.event_max_rows,),
        )
