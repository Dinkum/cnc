from __future__ import annotations

import pytest

from app.services.llm_help import cached_service_map


@pytest.mark.parametrize(
    "cached_status",
    [
        None,
        {},
        {"services": None},
        {"services": "unavailable"},
        {"services": {"service": "cnc-app-web"}},
    ],
)
def test_cached_service_map_treats_missing_or_malformed_services_as_empty(
    cached_status: object,
) -> None:
    assert cached_service_map(cached_status) == {}


def test_cached_service_map_keeps_only_named_service_records() -> None:
    valid = {"service": "cnc-app-web", "data": {"ActiveState": "active"}}

    assert cached_service_map(
        {
            "services": [
                None,
                {},
                {"service": None},
                {"service": 42},
                {"service": ""},
                valid,
            ]
        }
    ) == {"cnc-app-web": valid}
