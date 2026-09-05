from app.services.error_reporting import (
    ERROR_CODE_RE,
    ERROR_NAME_RE,
    ERROR_REGISTRY,
    CNCError,
    ErrorCode,
    error_context,
    error_ledger_rows,
    new_error_instance,
    cnc_error_from_payload,
)


def test_error_registry_has_one_to_one_codes_and_names() -> None:
    codes = [descriptor.code for descriptor in ERROR_REGISTRY.values()]
    names = [descriptor.name for descriptor in ERROR_REGISTRY.values()]

    assert len(codes) == len(set(codes))
    assert len(names) == len(set(names))
    assert all(ERROR_CODE_RE.fullmatch(code) for code in codes)
    assert all(ERROR_NAME_RE.fullmatch(name) for name in names)
    assert all(descriptor.message for descriptor in ERROR_REGISTRY.values())
    assert ERROR_REGISTRY[ErrorCode.INTERNAL_UNHANDLED_EXCEPTION].code == "CNC-09001"
    assert (
        ERROR_REGISTRY[ErrorCode.INTERNAL_UNHANDLED_EXCEPTION].name
        == "INTERNAL_UNHANDLED_EXCEPTION"
    )


def test_error_instances_are_crockford_codes() -> None:
    generated = new_error_instance()

    assert len(generated) == 8
    assert all(character not in "ILOU" for character in generated)


def test_error_context_includes_code_name_and_instance() -> None:
    context = error_context(
        ErrorCode.RESTORE_TAR_SYMLINK_ESCAPES,
        error_inst="7K2Q9M4D",
    )

    assert context == {
        "error_code": "CNC-05002",
        "error_name": "RESTORE_TAR_SYMLINK_ESCAPES",
        "error_inst": "7K2Q9M4D",
    }


def test_cnc_error_keeps_one_reference_across_payloads_and_http_boundary() -> None:
    error = CNCError(
        ErrorCode.APPLY_NGINX_CONFIG_INVALID,
        "nginx -t failed",
        error_inst="NGINX001",
        details={"phase": "nginx_validate"},
    )

    restored = cnc_error_from_payload(
        {"message": "save failed", "apply": error.as_details()},
        default_error_code=ErrorCode.HTTP_REQUEST_REJECTED,
        status_code=500,
    )

    assert error.payload()["error_inst"] == "NGINX001"
    assert restored.error_code is ErrorCode.APPLY_NGINX_CONFIG_INVALID
    assert restored.error_inst == "NGINX001"
    assert restored.log_context() == error.log_context()


def test_error_ledger_rows_are_sorted_and_complete() -> None:
    rows = error_ledger_rows()

    assert len(rows) == len(ErrorCode)
    assert [row.code for row in rows] == sorted(row.code for row in rows)
    assert rows[0].code == "CNC-01001"
    assert rows[-1].code == "CNC-09001"
    assert all(row.name and row.message for row in rows)
