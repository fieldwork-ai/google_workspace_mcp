import asyncio
import base64
import hashlib
import json
import re
from contextvars import ContextVar
from unittest.mock import Mock

import pytest

from core.utils import UserInputError
from gmail.attachment_transfer import (
    MAX_ATTACHMENT_BYTES,
    SOURCE_PROPERTY,
    save_attachment_to_drive,
)
from gmail.gmail_tools import save_gmail_attachment_to_drive
from core.server import server
from core.tool_registry import get_tool_components


def services(payload=bytes(range(256)) * 256):
    gmail, drive = Mock(), Mock()
    folder = {
        "id": "folder-1",
        "mimeType": "application/vnd.google-apps.folder",
        "trashed": False,
        "capabilities": {"canAddChildren": True},
    }
    message = {
        "payload": {
            "parts": [
                {
                    "mimeType": "multipart/mixed",
                    "parts": [
                        {
                            "partId": "0.0",
                            "filename": "invoice.pdf",
                            "mimeType": "application/pdf",
                            "body": {
                                "attachmentId": "attachment-1",
                                "size": len(payload),
                            },
                        }
                    ],
                }
            ]
        }
    }
    gmail.users().messages().get().execute.return_value = message
    gmail.users().messages().attachments().get().execute.return_value = {
        "size": len(payload),
        "data": base64.urlsafe_b64encode(payload).decode().rstrip("="),
    }
    saved = []
    uploads = []

    def get(**kwargs):
        result = (
            folder
            if kwargs["fileId"] in {"folder-1", "root"}
            else next(f for f in saved if f["id"] == kwargs["fileId"])
        )
        return Mock(execute=Mock(return_value=result))

    def list_files(**kwargs):
        key = re.search(r"value='([a-f0-9]+)'", kwargs["q"])[1]
        matches = [f for f in saved if f["appProperties"][SOURCE_PROPERTY] == key]
        return Mock(execute=Mock(return_value={"files": matches}))

    def create(**kwargs):
        def execute(**options):
            assert options == {"num_retries": 0}
            media = kwargs["media_body"]
            content = media.getbytes(0, media.size())
            uploads.append(content)
            saved.append(
                {
                    **kwargs["body"],
                    "trashed": False,
                    "size": str(len(content)),
                    "md5Checksum": hashlib.md5(
                        content, usedforsecurity=False
                    ).hexdigest(),
                    "webViewLink": "https://drive.google.com/file/d/created-1/view",
                }
            )
            return {"id": "created-1"}

        return Mock(execute=Mock(side_effect=execute))

    drive.files().get.side_effect = get
    drive.files().list.side_effect = list_files
    drive.files().create.side_effect = create
    drive.files().generateIds.return_value.execute.return_value = {"ids": ["created-1"]}
    return gmail, drive, folder, message, saved, uploads


async def transfer(gmail, drive, **kwargs):
    return await save_attachment_to_drive(
        gmail,
        drive,
        **{
            "user_google_email": "owner@example.com",
            "message_id": "message-1",
            "attachment_id": "attachment-1",
            "folder_id": "folder-1",
            **kwargs,
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [b"", b"a", bytes(range(256)) * 256])
async def test_transfers_exact_original_bytes_with_small_verified_receipt(payload):
    gmail, drive, _, _, saved, uploads = services(payload)
    result = await transfer(gmail, drive)
    assert uploads == [payload]
    assert result["status"] == "created"
    assert result["name"] == "invoice.pdf"
    assert result["mime_type"] == "application/pdf"
    assert result["size"] == len(payload)
    assert (
        result["md5_checksum"]
        == hashlib.md5(payload, usedforsecurity=False).hexdigest()
    )
    assert len(json.dumps(result)) < 1000
    assert saved[0]["parents"] == ["folder-1"]
    assert drive.files().get.call_args.kwargs["supportsAllDrives"] is True
    assert drive.files().create.call_args.kwargs["supportsAllDrives"] is True
    assert gmail.users().messages().get.call_args.kwargs["userId"] == "me"
    assert gmail.users().messages().attachments().get.call_args.kwargs["userId"] == "me"


@pytest.mark.asyncio
async def test_sequential_retry_returns_existing_even_when_filename_changes():
    gmail, drive, _, _, saved, uploads = services()
    first = await transfer(gmail, drive, file_name="2026-vendor.pdf")
    second = await transfer(gmail, drive, file_name="different.pdf")
    assert first["status"] == "created"
    assert second["status"] == "existing"
    assert second["file_id"] == first["file_id"]
    assert second["name"] == "2026-vendor.pdf"
    assert len(saved) == len(uploads) == 1
    drive.files().create.assert_called_once()


@pytest.mark.asyncio
async def test_resolves_root_alias_and_scopes_shared_drive_lookup():
    gmail, drive, folder, _, _, _ = services()
    folder["driveId"] = "shared-drive"
    await transfer(gmail, drive, folder_id="root")
    query = drive.files().list.call_args.kwargs
    assert query["driveId"] == "shared-drive"
    assert query["corpora"] == "drive"
    assert query["includeItemsFromAllDrives"] is True
    assert "'folder-1' in parents" in query["q"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["missing", "ambiguous", "oversized", "native"])
async def test_refuses_invalid_mime_selection_before_downloading(change):
    gmail, drive, _, message, _, _ = services()
    part = message["payload"]["parts"][0]["parts"][0]
    if change == "missing":
        part["body"]["attachmentId"] = "other"
    elif change == "ambiguous":
        message["payload"]["parts"].append(part)
    elif change == "oversized":
        part["body"]["size"] = MAX_ATTACHMENT_BYTES + 1
    else:
        part["mimeType"] = "application/vnd.google-apps.document"
    gmail.users().messages().attachments().get().execute.reset_mock()
    with pytest.raises(UserInputError):
        await transfer(gmail, drive)
    gmail.users().messages().attachments().get().execute.assert_not_called()
    drive.files().create.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "update",
    [
        {"size": -1},
        {"size": True},
        {"size": 1},
        {"size": None},
        {"data": "!!!!"},
        {"data": "a"},
        {"data": "☃"},
        {"data": None},
        {"data": base64.urlsafe_b64encode(b"short").decode()},
    ],
)
async def test_refuses_malformed_or_incomplete_bytes(update):
    gmail, drive, _, _, _, _ = services()
    gmail.users().messages().attachments().get().execute.return_value.update(update)
    with pytest.raises(UserInputError):
        await transfer(gmail, drive)
    drive.files().create.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["trashed", "readonly", "not_folder"])
async def test_destination_must_be_a_writable_live_folder(change):
    gmail, drive, folder, _, _, _ = services()
    if change == "trashed":
        folder["trashed"] = True
    elif change == "readonly":
        folder["capabilities"]["canAddChildren"] = False
    else:
        folder["mimeType"] = "application/pdf"
    gmail.users().messages().get().execute.reset_mock()
    with pytest.raises(UserInputError):
        await transfer(gmail, drive)
    gmail.users().messages().get().execute.assert_not_called()
    drive.files().create.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "listing",
    [
        {"incompleteSearch": True},
        {"nextPageToken": "more"},
        {"files": [{}, {}]},
    ],
)
async def test_ambiguous_duplicate_lookup_never_creates(listing):
    gmail, drive, _, _, _, _ = services()
    drive.files().list.side_effect = None
    drive.files().list.return_value.execute.return_value = listing
    with pytest.raises(UserInputError):
        await transfer(gmail, drive)
    drive.files().create.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [("md5Checksum", "bad"), ("size", "1"), ("trashed", True), ("parents", [])],
)
async def test_existing_file_must_still_match_source(field, value):
    gmail, drive, _, _, saved, _ = services()
    await transfer(gmail, drive)
    saved[0][field] = value
    with pytest.raises(UserInputError, match="created-1.*could not be verified"):
        await transfer(gmail, drive)
    drive.files().create.assert_called_once()


@pytest.mark.asyncio
async def test_failed_post_upload_verification_exposes_file_id():
    gmail, drive, _, _, saved, _ = services()
    original_create = drive.files().create.side_effect

    def create(**kwargs):
        request = original_create(**kwargs)
        original_execute = request.execute.side_effect

        def execute(**options):
            result = original_execute(**options)
            saved[0]["md5Checksum"] = "bad"
            return result

        request.execute.side_effect = execute
        return request

    drive.files().create.side_effect = create
    with pytest.raises(UserInputError, match="created-1.*could not be verified"):
        await transfer(gmail, drive)
    drive.files().create.assert_called_once()


@pytest.mark.asyncio
async def test_lost_upload_response_does_not_retry_and_gives_recovery_id():
    gmail, drive, _, _, _, _ = services()
    drive.files().create.side_effect = None
    drive.files().create.return_value.execute.side_effect = TimeoutError(
        "lost response"
    )
    with pytest.raises(UserInputError, match="Check file created-1.*before retrying"):
        await transfer(gmail, drive)
    drive.files().create.return_value.execute.assert_called_once_with(num_retries=0)


@pytest.mark.asyncio
async def test_different_accounts_do_not_share_duplicate_keys():
    gmail, drive, _, _, saved, _ = services()
    await transfer(gmail, drive, user_google_email="one@example.com")
    await transfer(gmail, drive, user_google_email="two@example.com")
    assert saved[0]["appProperties"] != saved[1]["appProperties"]
    assert len(saved) == 2


def test_discovery_requires_both_services_and_advertises_mutation():
    tool = get_tool_components(server)[save_gmail_attachment_to_drive.__name__]
    props = tool.parameters["properties"]
    assert {"message_id", "attachment_id", "folder_id", "file_name"} <= props.keys()
    assert (
        not {"gmail_service", "drive_service", "base64_content", "token"} & props.keys()
    )
    assert {"message_id", "attachment_id", "folder_id"} <= set(
        tool.parameters["required"]
    )
    assert tool.annotations.readOnlyHint is False
    assert tool.annotations.idempotentHint is False
    assert set(tool.fn._required_google_scopes) == {
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/drive.file",
    }


@pytest.mark.asyncio
async def test_interleaved_authenticated_requests_use_their_own_services(monkeypatch):
    import auth.service_decorator as auth

    current = ContextVar("account")
    accounts = {
        email: services(email.encode())
        for email in ["one@example.com", "two@example.com"]
    }

    async def context(_):
        return current.get(), "fastmcp_oauth", None

    async def authenticate(
        use_oauth21,
        service_name,
        version,
        tool_name,
        email,
        scopes,
        session,
        authenticated,
    ):
        await asyncio.sleep(0)
        assert use_oauth21 and email == authenticated == current.get()
        return accounts[email][0 if service_name == "gmail" else 1], email

    monkeypatch.setattr(auth, "is_oauth21_enabled", lambda: True)
    monkeypatch.setattr(auth, "_get_auth_context", context)
    monkeypatch.setattr(auth, "_authenticate_service", authenticate)
    monkeypatch.setattr(auth, "_release_google_service_cycles", lambda: None)
    tool = get_tool_components(server)["save_gmail_attachment_to_drive"].fn

    async def run(email):
        token = current.set(email)
        try:
            return await tool(
                message_id="message-1",
                attachment_id="attachment-1",
                folder_id="folder-1",
                user_google_email="spoofed@example.com",
            )
        finally:
            current.reset(token)

    results = await asyncio.gather(*(run(email) for email in accounts))
    for email, result in zip(accounts, results):
        gmail, drive, _, _, _, uploads = accounts[email]
        assert result["status"] == "created"
        assert uploads == [email.encode()]
        gmail.close.assert_called_once()
        drive.close.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_provider_permission_errors_remain_actionable(status):
    from googleapiclient.errors import HttpError
    from httplib2 import Response

    gmail, drive, _, _, _, _ = services()
    error = HttpError(
        Response({"status": str(status)}), b'{"error":{"message":"denied"}}'
    )
    drive.files().create.side_effect = None
    drive.files().create.return_value.execute.side_effect = error
    with pytest.raises(HttpError) as raised:
        await transfer(gmail, drive)
    assert raised.value is error
    drive.files().create.return_value.execute.assert_called_once_with(num_retries=0)


@pytest.mark.asyncio
async def test_refreshed_attachment_id_for_same_mime_part_still_deduplicates():
    gmail, drive, _, message, _, uploads = services()
    first = await transfer(gmail, drive)
    message["payload"]["parts"][0]["parts"][0]["body"]["attachmentId"] = "new-id"
    second = await transfer(gmail, drive, attachment_id="new-id")
    assert second["status"] == "existing"
    assert second["file_id"] == first["file_id"]
    assert len(uploads) == 1


@pytest.mark.asyncio
async def test_verification_request_failure_keeps_recovery_id():
    gmail, drive, _, _, _, _ = services()
    original = drive.files().get.side_effect

    def get(**kwargs):
        if kwargs["fileId"] == "created-1":
            return Mock(execute=Mock(side_effect=TimeoutError("read timed out")))
        return original(**kwargs)

    drive.files().get.side_effect = get
    with pytest.raises(UserInputError, match="verification failed.*created-1"):
        await transfer(gmail, drive)
    drive.files().create.assert_called_once()
