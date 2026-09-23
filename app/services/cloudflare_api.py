"""Bounded Cloudflare API requests with redacted errors and explicit mutations."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

API_ROOT = "https://api.cloudflare.com/client/v4"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class CloudflareError(ValueError):
    def __init__(self, message: str, *, uncertain: bool = False) -> None:
        super().__init__(message)
        self.uncertain = uncertain


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def resource_id(value: object) -> str:
    result = str(value or "")
    if not re.fullmatch(r"[a-fA-F0-9]{32}", result):
        raise CloudflareError("Cloudflare returned an invalid resource identifier.")
    return result


class CloudflareClient:
    def __init__(self, token: str, *, timeout: int = 15) -> None:
        if not token or not re.fullmatch(r"[A-Za-z0-9_-]{20,256}", token):
            raise CloudflareError("Enter a Cloudflare API token, not a Global API key.")
        self._token = token
        self.timeout = timeout

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = API_ROOT + path + ("?" + urlencode(params) if params else "")
        request = Request(
            url,
            method=method,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "User-Agent": "CNC",
            },
            data=json.dumps(body).encode() if body is not None else None,
        )
        try:
            with build_opener(_NoRedirects()).open(
                request, timeout=self.timeout
            ) as response:
                content = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            if exc.code in {401, 403}:
                message = "Cloudflare denied access. Check the token permissions and selected zones."
            elif exc.code == 429:
                message = "Cloudflare rate limit reached. Try again later."
            else:
                message = f"Cloudflare request failed (HTTP {exc.code})."
            raise CloudflareError(
                message, uncertain=method != "GET" and exc.code >= 500
            ) from None
        except (URLError, TimeoutError, OSError):
            raise CloudflareError(
                "Cloudflare could not be reached. Check the connection before trying again.",
                uncertain=method != "GET",
            ) from None
        if len(content) > MAX_RESPONSE_BYTES:
            raise CloudflareError(
                "Cloudflare response exceeded the size limit.",
                uncertain=method != "GET",
            )
        try:
            payload = json.loads(content)
        except (ValueError, UnicodeError):
            raise CloudflareError(
                "Cloudflare returned an invalid response.", uncertain=method != "GET"
            ) from None
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise CloudflareError(
                "Cloudflare rejected the request. Check token permissions and the requested settings."
            )
        return payload

    def zones(self) -> list[dict[str, str]]:
        zones = []
        for page in range(1, 21):
            payload = self.request(
                "GET", "/zones", params={"page": page, "per_page": 50}
            )
            rows = payload.get("result")
            if not isinstance(rows, list):
                raise CloudflareError("Cloudflare returned an invalid zone list.")
            for zone in rows:
                name = str(zone.get("name") or "").lower().rstrip(".")
                if not re.fullmatch(r"[a-z0-9.-]+", name):
                    raise CloudflareError("Cloudflare returned an invalid domain name.")
                zones.append(
                    {
                        "id": resource_id(zone.get("id")),
                        "name": name,
                        "status": str(zone.get("status") or ""),
                    }
                )
            if page >= int(payload.get("result_info", {}).get("total_pages", 1)):
                return sorted(zones, key=lambda zone: zone["name"])
        raise CloudflareError("Limit the token to at most 1,000 zones.")

    def records(self, zone_id: str, hostname: str) -> list[dict[str, Any]]:
        payload = self.request(
            "GET",
            f"/zones/{resource_id(zone_id)}/dns_records",
            params={"name": hostname, "per_page": 100},
        )
        if int(payload.get("result_info", {}).get("total_count", 0)) > 100:
            raise CloudflareError(
                "Too many DNS records exist for this hostname to manage safely."
            )
        rows = payload.get("result")
        if not isinstance(rows, list):
            raise CloudflareError("Cloudflare returned an invalid DNS response.")
        return [
            {
                key: row.get(key)
                for key in ("id", "type", "name", "content", "proxied", "comment")
            }
            for row in rows
        ]

    def issue_certificate(self, hostname: str, csr: str) -> dict[str, Any]:
        payload = self.request(
            "POST",
            "/certificates",
            body={
                "csr": csr,
                "hostnames": [hostname],
                "request_type": "origin-rsa",
                "requested_validity": 365,
            },
        )
        result = payload.get("result")
        if not isinstance(result, dict) or not isinstance(
            result.get("certificate"), str
        ):
            raise CloudflareError(
                "Cloudflare did not return the issued certificate.", uncertain=True
            )
        return result
