from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import ipaddress
import json
import re
import secrets
import shlex
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models.entities import ClusterJoinToken, ClusterNode, ClusterNodeLatencySample
from app.services.commands import run_command
from app.services.cluster_topology import (
    TAILSCALE_IPV4_NETWORK,
    is_tailnet_host,
    leader_tailnet_ip as resolve_leader_tailnet_ip,
)
from app.services.tailscale_admin import current_tailscale_admin_hostnames
from app.services.update_service import current_app_version


JOIN_TOKEN_BYTES = 32
NODE_UID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
LATENCY_SAMPLE_RETENTION_DAYS = 7


@dataclass(frozen=True)
class JoinCommand:
    command: str
    token: str
    expires_at: datetime
    base_url: str


@dataclass(frozen=True)
class RegisteredNode:
    node: ClusterNode
    leader_tailnet_ip: str


@dataclass(frozen=True)
class LatencySummary:
    avg_ms: float
    p95_ms: float
    p99_ms: float
    sample_count: int


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def normalize_base_url(value: str) -> str:
    normalized = str(value or "").strip().rstrip("/")
    if not normalized.startswith(("http://", "https://")):
        raise ValueError("join base URL must start with http:// or https://")
    return normalized


def current_node_version_label(settings: Settings) -> str:
    raw_version = str(settings.log_version or current_app_version()).strip() or "dev"
    version = (
        raw_version
        if raw_version == "dev" or raw_version.startswith("v")
        else f"v{raw_version}"
    )
    git_hash = _current_git_hash()
    return f"{version} ({git_hash})" if git_hash else version


async def create_join_command(
    session: AsyncSession,
    settings: Settings,
    *,
    request_base_url: str,
) -> JoinCommand:
    base_url = _join_base_url(settings, request_base_url=request_base_url)
    token = secrets.token_urlsafe(JOIN_TOKEN_BYTES)
    expires_at = datetime.now(UTC) + timedelta(
        seconds=settings.cluster_join_token_ttl_sec
    )
    session.add(ClusterJoinToken(token_hash=token_hash(token), expires_at=expires_at))
    await session.commit()

    command = (
        f"curl -fsSL {shlex.quote(f'{base_url}/join/install.sh')} | "
        "sudo "
        f"CNC_LEADER_URL={shlex.quote(base_url)} "
        f"CNC_JOIN_TOKEN={shlex.quote(token)} "
        "bash"
    )
    return JoinCommand(
        command=command, token=token, expires_at=expires_at, base_url=base_url
    )


async def list_cluster_nodes(session: AsyncSession) -> list[ClusterNode]:
    result = await session.execute(
        select(ClusterNode)
        .where(ClusterNode.removed_at.is_(None))
        .order_by(ClusterNode.joined_at.asc(), ClusterNode.id.asc())
    )
    return list(result.scalars().all())


async def cluster_latency_summaries(
    session: AsyncSession,
    node_uids: list[str],
    *,
    since: datetime,
) -> dict[str, LatencySummary]:
    clean_node_uids = [
        node_uid
        for node_uid in dict.fromkeys(node_uids)
        if NODE_UID_RE.fullmatch(node_uid)
    ]
    if not clean_node_uids:
        return {}
    result = await session.execute(
        select(
            ClusterNodeLatencySample.node_uid,
            ClusterNodeLatencySample.latency_ms,
        )
        .where(
            ClusterNodeLatencySample.node_uid.in_(clean_node_uids),
            ClusterNodeLatencySample.recorded_at >= since,
        )
        .order_by(
            ClusterNodeLatencySample.node_uid.asc(),
            ClusterNodeLatencySample.latency_ms.asc(),
        )
    )
    grouped: dict[str, list[float]] = {}
    for node_uid, latency_ms in result.all():
        grouped.setdefault(str(node_uid), []).append(float(latency_ms))
    return {
        node_uid: LatencySummary(
            avg_ms=sum(values) / len(values),
            p95_ms=_nearest_rank_percentile(values, 0.95),
            p99_ms=_nearest_rank_percentile(values, 0.99),
            sample_count=len(values),
        )
        for node_uid, values in grouped.items()
        if values
    }


async def remove_node(
    session: AsyncSession,
    settings: Settings,
    *,
    node_uid: str,
    request_host: str | None,
) -> ClusterNode:
    clean_node_uid = _require_node_uid(node_uid)
    now = datetime.now(UTC)
    result = await session.execute(
        select(ClusterNode).where(
            ClusterNode.node_uid == clean_node_uid,
            ClusterNode.removed_at.is_(None),
        )
    )
    node = result.scalar_one_or_none()
    if node is None:
        raise ValueError("node is not linked")

    node.state = "removed"
    node.removed_at = now
    node.last_seen_at = now
    node.details_json = _merge_details(
        node.details_json,
        {"remove": {"removed_at": now.isoformat()}},
    )
    tokens = await session.execute(
        select(ClusterJoinToken).where(ClusterJoinToken.node_uid == clean_node_uid)
    )
    for token in tokens.scalars().all():
        token.used_at = token.used_at or now
    await session.flush()
    await session.commit()
    await session.refresh(node)
    return node


async def register_node(
    session: AsyncSession,
    settings: Settings,
    *,
    token: str,
    payload: dict[str, Any],
    request_host: str | None,
) -> RegisteredNode:
    now = datetime.now(UTC)
    node_uid = _require_node_uid(payload.get("node_uid"))
    tailnet_ip = _require_tailnet_ip(payload.get("tailnet_ip"))
    node_name = _clean_text(payload.get("name"), default="cnc-node", max_length=255)
    version = _clean_optional_text(payload.get("version"), max_length=64)
    leader_tailnet_ip = _leader_tailnet_ip(settings)
    await _consume_join_token(session, token, node_uid=node_uid, now=now)

    existing = await session.execute(
        select(ClusterNode).where(ClusterNode.node_uid == node_uid)
    )
    node = existing.scalar_one_or_none()
    if node is None:
        node = ClusterNode(
            node_uid=node_uid,
            name=node_name,
            role="follower",
            state="joining",
            wireguard_ip=tailnet_ip,
            wireguard_public_key="",
            public_endpoint=None,
            tailnet_ip=tailnet_ip,
            ram_bytes=_optional_int(payload.get("ram_bytes")),
            cpu_count=_optional_int(payload.get("cpu_count")),
            disk_bytes=_optional_int(payload.get("disk_bytes")),
            version=version,
            details_json=_details_json(payload),
            last_seen_at=now,
        )
        session.add(node)
    else:
        node.name = node_name
        node.state = "joining"
        node.wireguard_ip = tailnet_ip
        node.wireguard_public_key = ""
        node.public_endpoint = None
        node.tailnet_ip = tailnet_ip
        node.ram_bytes = _optional_int(payload.get("ram_bytes"))
        node.cpu_count = _optional_int(payload.get("cpu_count"))
        node.disk_bytes = _optional_int(payload.get("disk_bytes"))
        node.version = version
        node.details_json = _details_json(payload)
        node.last_seen_at = now
        node.removed_at = None

    await session.flush()

    await session.commit()
    await session.refresh(node)
    return RegisteredNode(
        node=node,
        leader_tailnet_ip=leader_tailnet_ip,
    )


async def confirm_node(
    session: AsyncSession,
    *,
    token: str,
    payload: dict[str, Any],
) -> ClusterNode:
    node_uid = _require_node_uid(payload.get("node_uid"))
    now = datetime.now(UTC)
    token_row = await _load_join_token(session, token)
    if token_row.node_uid != node_uid:
        raise ValueError("join token does not match node")
    result = await session.execute(
        select(ClusterNode).where(
            ClusterNode.node_uid == node_uid, ClusterNode.removed_at.is_(None)
        )
    )
    node = result.scalar_one_or_none()
    if node is None:
        raise ValueError("node is not registered")
    if payload.get("tailnet_ip"):
        node.tailnet_ip = _require_tailnet_ip(payload.get("tailnet_ip"))
        node.wireguard_ip = node.tailnet_ip
    if version := _clean_optional_text(payload.get("version"), max_length=64):
        node.version = version
    node.state = "healthy" if bool(payload.get("tailnet_ok")) else "degraded"
    latency_ms = _optional_float(payload.get("latency_ms"))
    node.latency_ms = latency_ms
    node.last_seen_at = now
    node.details_json = _merge_details(
        node.details_json, {"confirm": _jsonable_payload(payload)}
    )
    if latency_ms is not None:
        session.add(
            ClusterNodeLatencySample(
                node_uid=node_uid,
                latency_ms=latency_ms,
                recorded_at=now,
            )
        )
        if _latency_sample_prune_due(now):
            with session.no_autoflush:
                await session.execute(
                    delete(ClusterNodeLatencySample).where(
                        ClusterNodeLatencySample.recorded_at
                        < now - timedelta(days=LATENCY_SAMPLE_RETENTION_DAYS)
                    )
                )
    await session.commit()
    await session.refresh(node)
    return node


def install_script(version_label: str = "dev") -> str:
    version_literal = shlex.quote(str(version_label or "dev"))
    return r"""#!/usr/bin/env bash
set -Eeuo pipefail
umask 027

log() { printf '[cnc-node] %s\n' "$1"; }
fail() { printf '[cnc-node][error] %s\n' "$1" >&2; exit 1; }

[[ "${EUID}" -eq 0 ]] || fail "run as root"
[[ -n "${CNC_LEADER_URL:-}" ]] || fail "CNC_LEADER_URL is required"
[[ -n "${CNC_JOIN_TOKEN:-}" ]] || fail "CNC_JOIN_TOKEN is required"

if [[ ! -f /etc/os-release ]]; then
  fail "cannot detect OS"
fi
# shellcheck disable=SC1091
source /etc/os-release
[[ "${ID:-}" == "ubuntu" ]] || fail "this join installer targets Ubuntu only"

export DEBIAN_FRONTEND=noninteractive
log "installing node dependencies"
apt-get -qq update
apt-get -qq install -y ca-certificates curl jq iproute2 iputils-ping util-linux >/dev/null

install -d -m 0755 /etc/cnc
node_uid_path="/etc/cnc/node-id"
if [[ ! -s "${node_uid_path}" ]]; then
  uuidgen >"${node_uid_path}"
  chmod 0600 "${node_uid_path}"
fi
node_uid="$(tr -d '\r\n' <"${node_uid_path}")"
tailnet_ip="$(tailscale ip -4 2>/dev/null | head -n1 || true)"
[[ -n "${tailnet_ip}" ]] || fail "Tailscale must be connected before running the CNC node installer"
tailnet_dns_name="$(tailscale status --json 2>/dev/null | jq -r '.Self.DNSName // ""' | sed 's/[.]$//' || true)"
tailnet_url=""
if [[ -n "${tailnet_dns_name}" && "${tailnet_dns_name}" != "null" ]]; then
  tailnet_url="https://${tailnet_dns_name}"
fi
cnc_version=__CNC_VERSION_LITERAL__

ram_bytes="$(awk '/MemTotal:/ { print $2 * 1024; exit }' /proc/meminfo 2>/dev/null || true)"
cpu_count="$(nproc 2>/dev/null || echo 0)"
disk_bytes="$(df -B1 / | awk 'NR == 2 { print $2; exit }' 2>/dev/null || true)"
host_name="$(hostname -f 2>/dev/null || hostname 2>/dev/null || echo cnc-node)"

register_payload="$(mktemp)"
jq -n \
  --arg node_uid "${node_uid}" \
  --arg name "${host_name}" \
  --arg tailnet_ip "${tailnet_ip}" \
  --arg tailnet_url "${tailnet_url}" \
  --arg version "${cnc_version}" \
  --argjson ram_bytes "${ram_bytes:-0}" \
  --argjson cpu_count "${cpu_count:-0}" \
  --argjson disk_bytes "${disk_bytes:-0}" \
  '{node_uid:$node_uid,name:$name,tailnet_ip:$tailnet_ip,tailnet_url:$tailnet_url,version:$version,ram_bytes:$ram_bytes,cpu_count:$cpu_count,disk_bytes:$disk_bytes}' \
  >"${register_payload}"

log "registering with leader"
register_response="$(curl -fsS --retry 3 --connect-timeout 10 \
  -H 'Content-Type: application/json' \
  -H "X-CNC-Join-Token: ${CNC_JOIN_TOKEN}" \
  --data @"${register_payload}" \
  "${CNC_LEADER_URL%/}/join/register")"
rm -f "${register_payload}"

leader_tailnet_ip="$(printf '%s' "${register_response}" | jq -r '.leader.tailnet_ip')"
[[ -n "${leader_tailnet_ip}" && "${leader_tailnet_ip}" != "null" ]] || fail "leader did not return Tailscale IP"

latency_ms=""
if ping_output="$(ping -c 3 -W 2 "${leader_tailnet_ip}" 2>/dev/null)"; then
  latency_ms="$(printf '%s\n' "${ping_output}" | awk '/^(rtt|round-trip)/ { sub(/^.*= /, ""); split($1, parts, "/"); print parts[2]; exit }')"
fi
tailnet_ok="false"
if [[ -n "${latency_ms}" ]]; then
  tailnet_ok="true"
fi

confirm_payload="$(mktemp)"
jq -n \
  --arg node_uid "${node_uid}" \
  --argjson tailnet_ok "${tailnet_ok}" \
  --arg latency_ms "${latency_ms:-}" \
  '{node_uid:$node_uid,tailnet_ok:$tailnet_ok,latency_ms:($latency_ms | if . == "" then null else tonumber end)}' \
  >"${confirm_payload}"

log "confirming tailnet reachability"
curl -fsS --retry 3 --connect-timeout 10 \
  -H 'Content-Type: application/json' \
  -H "X-CNC-Join-Token: ${CNC_JOIN_TOKEN}" \
  --data @"${confirm_payload}" \
  "${CNC_LEADER_URL%/}/join/confirm" >/dev/null
rm -f "${confirm_payload}"
install -m 0700 -d /etc/cnc
{
  printf 'CNC_LEADER_URL=%q\n' "${CNC_LEADER_URL}"
  printf 'CNC_JOIN_TOKEN=%q\n' "${CNC_JOIN_TOKEN}"
  printf 'CNC_NODE_UID=%q\n' "${node_uid}"
  printf 'CNC_LEADER_TAILNET_IP=%q\n' "${leader_tailnet_ip}"
  printf 'CNC_NODE_VERSION=%q\n' "${cnc_version}"
} >/etc/cnc/node.env
chmod 0600 /etc/cnc/node.env

cat >/usr/local/bin/cnc-node-heartbeat <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail

[[ -f /etc/cnc/node.env ]] || exit 0
# shellcheck disable=SC1091
source /etc/cnc/node.env

tailnet_ip="$(tailscale ip -4 2>/dev/null | head -n1 || true)"
tailnet_dns_name="$(tailscale status --json 2>/dev/null | jq -r '.Self.DNSName // ""' | sed 's/[.]$//' || true)"
tailnet_url=""
if [[ -n "${tailnet_dns_name}" && "${tailnet_dns_name}" != "null" ]]; then
  tailnet_url="https://${tailnet_dns_name}"
fi

latency_ms=""
if [[ -n "${CNC_LEADER_TAILNET_IP:-}" ]] && ping_output="$(ping -c 3 -W 2 "${CNC_LEADER_TAILNET_IP}" 2>/dev/null)"; then
  latency_ms="$(printf '%s\n' "${ping_output}" | awk '/^(rtt|round-trip)/ { sub(/^.*= /, ""); split($1, parts, "/"); print parts[2]; exit }')"
fi

tailnet_ok="false"
if [[ -n "${latency_ms}" ]]; then
  tailnet_ok="true"
fi

payload="$(mktemp)"
trap 'rm -f "${payload}"' EXIT
jq -n \
  --arg node_uid "${CNC_NODE_UID}" \
  --arg tailnet_ip "${tailnet_ip}" \
  --arg tailnet_url "${tailnet_url}" \
  --arg version "${CNC_NODE_VERSION:-unknown}" \
  --argjson tailnet_ok "${tailnet_ok}" \
  --arg latency_ms "${latency_ms:-}" \
  '{node_uid:$node_uid,tailnet_ip:$tailnet_ip,tailnet_url:$tailnet_url,version:$version,tailnet_ok:$tailnet_ok,latency_ms:($latency_ms | if . == "" then null else tonumber end)}' \
  >"${payload}"

curl -fsS --retry 2 --connect-timeout 10 \
  -H 'Content-Type: application/json' \
  -H "X-CNC-Join-Token: ${CNC_JOIN_TOKEN}" \
  --data @"${payload}" \
  "${CNC_LEADER_URL%/}/join/confirm" >/dev/null
EOF
chmod 0755 /usr/local/bin/cnc-node-heartbeat

cat >/etc/systemd/system/cnc-node-heartbeat.service <<'EOF'
[Unit]
Description=CNC node heartbeat
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/cnc-node-heartbeat
EOF

cat >/etc/systemd/system/cnc-node-heartbeat.timer <<'EOF'
[Unit]
Description=CNC node heartbeat timer

[Timer]
OnBootSec=1min
OnUnitActiveSec=1min
AccuracySec=15s
Unit=cnc-node-heartbeat.service

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now cnc-node-heartbeat.timer >/dev/null
log "node joined as follower"
""".replace("__CNC_VERSION_LITERAL__", version_literal)


async def _consume_join_token(
    session: AsyncSession, token: str, *, node_uid: str, now: datetime
) -> ClusterJoinToken:
    token_digest = token_hash(str(token or ""))
    consumed_id = (
        await session.execute(
            update(ClusterJoinToken)
            .where(
                ClusterJoinToken.token_hash == token_digest,
                ClusterJoinToken.used_at.is_(None),
                ClusterJoinToken.expires_at >= now,
            )
            .values(used_at=now, node_uid=node_uid)
            .returning(ClusterJoinToken.id)
        )
    ).scalar_one_or_none()
    if consumed_id is not None:
        token_row = await session.get(ClusterJoinToken, consumed_id)
        if token_row is None:
            raise ValueError("join token is invalid")
        return token_row

    token_row = await _load_join_token(session, token)
    if token_row.used_at is not None:
        raise ValueError("join token has already been used")
    if _aware(token_row.expires_at) < now:
        raise ValueError("join token has expired")
    raise ValueError("join token could not be consumed")


async def _load_join_token(session: AsyncSession, token: str) -> ClusterJoinToken:
    result = await session.execute(
        select(ClusterJoinToken).where(
            ClusterJoinToken.token_hash == token_hash(str(token or ""))
        )
    )
    token_row = result.scalar_one_or_none()
    if token_row is None:
        raise ValueError("join token is invalid")
    return token_row


def _join_base_url(settings: Settings, *, request_base_url: str) -> str:
    configured = str(settings.cluster_join_tailnet_base_url or "").strip()
    if configured:
        return normalize_base_url(configured)

    hostnames = current_tailscale_admin_hostnames(settings)
    if hostnames:
        return f"https://{hostnames[0]}"

    request_base = normalize_base_url(request_base_url)
    request_host = urlsplit(request_base).hostname or ""
    if is_tailnet_host(request_host):
        return request_base
    raise ValueError("Tailscale admin URL is required before adding a node")


def _leader_tailnet_ip(settings: Settings) -> str:
    return resolve_leader_tailnet_ip(settings)


def _nearest_rank_percentile(sorted_values: list[float], percentile: float) -> float:
    if not sorted_values:
        raise ValueError("percentile requires at least one value")
    index = max(
        0,
        min(
            len(sorted_values) - 1, int(len(sorted_values) * percentile + 0.999999) - 1
        ),
    )
    return sorted_values[index]


def _latency_sample_prune_due(now: datetime) -> bool:
    # Heartbeats arrive every minute; hourly pruning keeps retention bounded without
    # adding an extra DELETE to every follower check-in.
    return now.minute == 0


def _current_git_hash() -> str:
    result = run_command(["git", "rev-parse", "--short=7", "HEAD"], timeout_sec=2)
    return result.stdout.strip() if result.ok else ""


def _is_tailnet_host(value: str) -> bool:
    return is_tailnet_host(value)


def _require_tailnet_ip(value: Any) -> str:
    candidate = str(value or "").strip()
    try:
        ip = ipaddress.ip_address(candidate)
    except ValueError as exc:
        raise ValueError("Tailscale IP is invalid") from exc
    if ip not in TAILSCALE_IPV4_NETWORK:
        raise ValueError("Tailscale IP must be a tailnet IPv4 address")
    return str(ip)


def _require_node_uid(value: Any) -> str:
    node_uid = str(value or "").strip()
    if not NODE_UID_RE.fullmatch(node_uid):
        raise ValueError("node id is invalid")
    return node_uid


def _clean_text(value: Any, *, default: str, max_length: int) -> str:
    text = str(value or "").strip()
    text = re.sub(r"[\r\n\t]+", " ", text)
    return (text or default)[:max_length]


def _clean_optional_text(value: Any, *, max_length: int) -> str | None:
    text = _clean_text(value, default="", max_length=max_length)
    return text or None


def _optional_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _optional_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _details_json(payload: dict[str, Any]) -> str:
    return json.dumps({"register": _jsonable_payload(payload)}, sort_keys=True)


def _merge_details(raw: str, update: dict[str, Any]) -> str:
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    payload.update(update)
    return json.dumps(payload, sort_keys=True)


def _jsonable_payload(payload: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            safe[str(key)] = value
    return safe


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
