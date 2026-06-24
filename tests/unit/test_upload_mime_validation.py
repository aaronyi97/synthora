from agoracle.api.routes.misc import _resolve_office_csv_mime_alias


def test_xlsx_zip_alias_is_accepted() -> None:
    resolved = _resolve_office_csv_mime_alias(
        "xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/zip",
    )

    assert resolved == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def test_csv_text_plain_alias_is_accepted() -> None:
    resolved = _resolve_office_csv_mime_alias(
        "csv",
        "text/csv",
        "text/plain",
    )

    assert resolved == "text/csv"


def test_xlsx_non_document_mime_is_rejected() -> None:
    resolved = _resolve_office_csv_mime_alias(
        "xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "image/png",
    )

    assert resolved is None
