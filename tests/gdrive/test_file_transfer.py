import hashlib
from unittest.mock import Mock, AsyncMock

import pytest

from core.utils import UserInputError
from gdrive.file_transfer import MAX_UPLOAD_BYTES, upload_file_from_url

DATA = bytes(range(256)) * 32
SHA = hashlib.sha256(DATA).hexdigest()


@pytest.fixture
def fixture(monkeypatch):
    from gdrive import file_transfer

    drive = Mock()
    folder = {
        "id": "folder",
        "mimeType": "application/vnd.google-apps.folder",
        "trashed": False,
        "capabilities": {"canAddChildren": True},
        "driveId": "shared",
    }
    saved = []
    uploaded = []

    def get(**kwargs):
        return Mock(
            execute=Mock(
                return_value=folder if kwargs["fileId"] == "folder" else saved[0]
            )
        )

    def create(**kwargs):
        def execute(**options):
            assert options == {"num_retries": 0}
            data = kwargs["media_body"].getbytes(0, len(DATA) + 1)
            uploaded.append(data)
            saved.append(
                {
                    **kwargs["body"],
                    "size": str(len(data)),
                    "sha256Checksum": hashlib.sha256(data).hexdigest(),
                    "trashed": False,
                }
            )
            return {"id": "new-id"}

        return Mock(execute=Mock(side_effect=execute))

    drive.files().get.side_effect = get
    drive.files().list.return_value.execute.side_effect = lambda: {
        "files": saved.copy()
    }
    drive.files().generateIds.return_value.execute.return_value = {"ids": ["new-id"]}
    drive.files().create.side_effect = create

    async def download(url, collect):
        assert url == "https://storage.example/file?secret=hidden"
        await collect(DATA[:3])
        await collect(DATA[3:])
        return len(DATA), "text/plain"

    stream = AsyncMock(side_effect=download)
    monkeypatch.setattr(file_transfer, "_stream_url_with_validation", stream)
    return drive, folder, saved, uploaded, stream


async def transfer(fixture, **overrides):
    args = dict(
        file_url="https://storage.example/file?secret=hidden",
        folder_id="folder",
        file_name="invoice.pdf",
        mime_type="application/pdf",
        expected_size=len(DATA),
        expected_sha256=SHA,
    )
    return await upload_file_from_url(fixture[0], **(args | overrides))


@pytest.mark.asyncio
async def test_binary_receipt_shared_drive_and_retry_without_source_url(fixture):
    receipt = await transfer(fixture)
    assert receipt["status"] == "created"
    assert receipt["sha256_checksum"] == SHA
    assert receipt["mime_type"] == "application/pdf"
    assert fixture[3] == [DATA]
    assert "secret" not in str(receipt)
    assert fixture[0].files().list.call_args.kwargs["driveId"] == "shared"
    assert fixture[0].files().create.call_args.kwargs["supportsAllDrives"] is True
    again = await transfer(fixture)
    assert again["status"] == "existing"
    assert again["file_id"] == receipt["file_id"]
    assert fixture[4].await_count == 1
    assert len(fixture[3]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"expected_size": MAX_UPLOAD_BYTES + 1},
        {"expected_size": -1},
        {"expected_size": True},
        {"expected_sha256": "bad"},
        {"mime_type": "application/vnd.google-apps.document"},
        {"mime_type": "bad\r\nvalue"},
        {"file_name": " "},
        {"folder_id": ""},
        {"file_url": "file:///tmp/invoice.pdf"},
        {"file_url": "http://storage.example/file"},
        {"file_url": "https://user:pass@example.com/file"},
        {"file_url": "https://example.com/file#token"},
    ],
)
async def test_invalid_inputs_do_not_fetch_or_upload(fixture, overrides):
    with pytest.raises(UserInputError):
        await transfer(fixture, **overrides)
    fixture[4].assert_not_called()
    fixture[0].files().create.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"expected_size": len(DATA) - 1},
        {"expected_size": len(DATA) + 1},
        {"expected_sha256": "0" * 64},
    ],
)
async def test_mismatching_source_never_uploads(fixture, overrides):
    with pytest.raises(UserInputError):
        await transfer(fixture, **overrides)
    fixture[0].files().create.assert_not_called()


@pytest.mark.asyncio
async def test_conflicting_filename_refuses_second_file(fixture):
    await transfer(fixture)
    with pytest.raises(UserInputError, match="does not match"):
        await transfer(fixture, expected_sha256="0" * 64)
    assert len(fixture[3]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        {"files": [], "incompleteSearch": True},
        {"files": [], "nextPageToken": "next"},
        {"files": [{}, {}]},
    ],
)
async def test_ambiguous_lookup_fails_closed(fixture, result):
    fixture[0].files().list.return_value.execute.side_effect = None
    fixture[0].files().list.return_value.execute.return_value = result
    with pytest.raises(UserInputError, match="ambiguous"):
        await transfer(fixture)
    fixture[4].assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "patch", [{"trashed": True}, {"mimeType": "application/pdf"}, {"capabilities": {}}]
)
async def test_unwritable_destination(fixture, patch):
    fixture[1].update(patch)
    with pytest.raises(UserInputError, match="Destination"):
        await transfer(fixture)
    fixture[4].assert_not_called()


@pytest.mark.asyncio
async def test_download_error_does_not_leak_signed_url(fixture):
    fixture[4].side_effect = RuntimeError("https://storage.example/file?secret=hidden")
    with pytest.raises(UserInputError, match="Could not fetch") as error:
        await transfer(fixture)
    assert "hidden" not in str(error.value)
    fixture[0].files().create.assert_not_called()


@pytest.mark.asyncio
async def test_uncertain_upload_includes_recovery_id(fixture):
    fixture[0].files().create.side_effect = None
    fixture[0].files().create.return_value.execute.side_effect = TimeoutError()
    with pytest.raises(UserInputError, match="Inspect Drive file new-id"):
        await transfer(fixture)


@pytest.mark.asyncio
async def test_verification_detects_drive_corruption(fixture):
    original = fixture[0].files().get.side_effect

    def corrupt(**kwargs):
        if kwargs["fileId"] != "folder":
            fixture[2][0]["sha256Checksum"] = "f" * 64
        return original(**kwargs)

    fixture[0].files().get.side_effect = corrupt
    with pytest.raises(UserInputError, match="does not match"):
        await transfer(fixture)


def test_schema_and_scope_are_registered():
    from gdrive.drive_tools import upload_file_to_drive  # noqa: F401
    from core.server import server
    from core.tool_registry import get_tool_components

    tool = get_tool_components(server)["upload_file_to_drive"]
    assert tool.annotations.readOnlyHint is False
    assert tool.annotations.idempotentHint is False
    assert {
        "file_url",
        "folder_id",
        "file_name",
        "mime_type",
        "expected_size",
        "expected_sha256",
    } <= set(tool.parameters["required"])
    assert set(
        get_tool_components(server)["upload_file_to_drive"].fn._required_google_scopes
    ) == {"https://www.googleapis.com/auth/drive.file"}


@pytest.mark.asyncio
async def test_private_source_is_refused_by_real_ssrf_guard(fixture, monkeypatch):
    from gdrive import drive_helpers, file_transfer

    monkeypatch.setattr(
        file_transfer,
        "_stream_url_with_validation",
        drive_helpers._stream_url_with_validation,
    )
    with pytest.raises(UserInputError, match="Could not fetch"):
        await transfer(fixture, file_url="https://127.0.0.1/private?secret=hidden")
    fixture[0].files().create.assert_not_called()
