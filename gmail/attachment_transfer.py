"""Copy Gmail attachments to Drive without returning their bytes to a client."""

import asyncio
import base64
import binascii
import hashlib
import io
import json

from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

from core.utils import UserInputError

MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
SOURCE_PROPERTY = "gmailAttachmentSource"
FILE_FIELDS = "id,name,mimeType,size,md5Checksum,webViewLink,parents,trashed"
FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"


def _attachment_part(payload: dict, attachment_id: str) -> tuple[dict, str]:
    matches = []

    def visit(part: dict, path: str):
        if part.get("body", {}).get("attachmentId") == attachment_id:
            matches.append((part, path))
        for index, child in enumerate(part.get("parts", [])):
            visit(child, f"{path}.{index}")

    visit(payload, "0")
    if len(matches) != 1:
        raise UserInputError(
            "Attachment ID must identify exactly one MIME part in this message. "
            "Fetch the message again to obtain its attachment IDs."
        )
    return matches[0]


def _size(value) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise UserInputError("Gmail returned an invalid attachment size.")
    if value > MAX_ATTACHMENT_BYTES:
        raise UserInputError("Attachment exceeds the 25 MiB transfer limit.")
    return value


def _receipt(file: dict, folder_id: str, size: int, checksum: str, status: str):
    if (
        file.get("trashed") is not False
        or folder_id not in file.get("parents", [])
        or str(file.get("size")) != str(size)
        or file.get("md5Checksum") != checksum
    ):
        raise UserInputError(
            f"Drive file {file.get('id', 'unknown')} could not be verified against "
            "the original attachment. Inspect that file before retrying; "
            "no replacement was created."
        )
    return {
        "status": status,
        "file_id": file["id"],
        "web_view_link": file.get("webViewLink"),
        "name": file.get("name"),
        "mime_type": file.get("mimeType"),
        "size": size,
        "md5_checksum": checksum,
    }


async def save_attachment_to_drive(
    gmail_service,
    drive_service,
    user_google_email: str,
    message_id: str,
    attachment_id: str,
    folder_id: str,
    file_name: str | None = None,
) -> dict:
    if not all(value.strip() for value in (message_id, attachment_id, folder_id)):
        raise UserInputError("message_id, attachment_id and folder_id are required.")
    if file_name is not None and not file_name.strip():
        raise UserInputError("file_name must not be blank.")

    folder = await asyncio.to_thread(
        drive_service.files()
        .get(
            fileId=folder_id,
            fields="id,mimeType,trashed,driveId,capabilities(canAddChildren)",
            supportsAllDrives=True,
        )
        .execute
    )
    if (
        folder.get("mimeType") != FOLDER_MIME_TYPE
        or folder.get("trashed") is not False
        or not folder.get("capabilities", {}).get("canAddChildren")
    ):
        raise UserInputError(
            "Destination must be a live Drive folder you can write to."
        )
    folder_id = folder["id"]

    message = await asyncio.to_thread(
        gmail_service.users()
        .messages()
        .get(userId="me", id=message_id, format="full")
        .execute
    )
    part, part_path = _attachment_part(message.get("payload", {}), attachment_id)
    expected_size = _size(part.get("body", {}).get("size"))
    name = file_name if file_name is not None else part.get("filename")
    if not isinstance(name, str) or not name.strip():
        raise UserInputError("This attachment has no filename; supply file_name.")
    mime_type = part.get("mimeType") or "application/octet-stream"
    if mime_type.startswith("application/vnd.google-apps."):
        raise UserInputError(
            "Attachments must be uploaded without Google-native conversion."
        )

    attachment = await asyncio.to_thread(
        gmail_service.users()
        .messages()
        .attachments()
        .get(userId="me", messageId=message_id, id=attachment_id)
        .execute
    )
    if _size(attachment.get("size")) != expected_size:
        raise UserInputError(
            "Gmail attachment size does not match the selected MIME part."
        )
    encoded = attachment.get("data")
    if not isinstance(encoded, str) or len(encoded) > 4 * (
        (MAX_ATTACHMENT_BYTES + 2) // 3
    ):
        raise UserInputError(
            "Gmail attachment data is missing or exceeds the transfer limit."
        )
    try:
        data = base64.b64decode(
            encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True
        )
    except (ValueError, binascii.Error):
        raise UserInputError("Gmail returned malformed attachment data.") from None
    if len(data) != expected_size:
        raise UserInputError(
            "Decoded attachment size differs from Gmail metadata; nothing uploaded."
        )
    checksum = hashlib.md5(data, usedforsecurity=False).hexdigest()
    source = hashlib.sha256(
        json.dumps([user_google_email.casefold(), message_id, part_path]).encode()
    ).hexdigest()
    escaped_folder = folder_id.replace("\\", "\\\\").replace("'", "\\'")
    search = {
        "q": f"'{escaped_folder}' in parents and trashed = false and "
        f"appProperties has {{ key='{SOURCE_PROPERTY}' and value='{source}' }}",
        "fields": f"files({FILE_FIELDS}),nextPageToken,incompleteSearch",
        "pageSize": 2,
        "spaces": "drive",
        "supportsAllDrives": True,
        "includeItemsFromAllDrives": True,
        "corpora": "drive" if folder.get("driveId") else "user",
    }
    if folder.get("driveId"):
        search["driveId"] = folder["driveId"]
    existing = await asyncio.to_thread(drive_service.files().list(**search).execute)
    if existing.get("incompleteSearch"):
        raise UserInputError("Drive duplicate lookup was incomplete; nothing uploaded.")
    files = existing.get("files", [])
    if len(files) > 1 or existing.get("nextPageToken"):
        raise UserInputError(
            "Multiple filed copies already exist in this folder; resolve them before retrying."
        )
    if files:
        return _receipt(files[0], folder_id, len(data), checksum, "existing")

    # A known ID lets a caller reconcile a lost upload response. Search-before-create
    # only deduplicates sequential calls, so the tool must not claim idempotence.
    ids = await asyncio.to_thread(
        drive_service.files().generateIds(count=1, space="drive").execute
    )
    file_id = ids["ids"][0]
    with io.BytesIO(data) as stream:
        media = MediaIoBaseUpload(
            stream, mimetype=mime_type, resumable=True, chunksize=5 * 1024 * 1024
        )
        request = drive_service.files().create(
            body={
                "id": file_id,
                "name": name,
                "mimeType": mime_type,
                "parents": [folder_id],
                "appProperties": {SOURCE_PROPERTY: source},
            },
            media_body=media,
            fields="id",
            supportsAllDrives=True,
        )
        try:
            await asyncio.to_thread(request.execute, num_retries=0)
        except HttpError as error:
            if error.resp.status < 500 and error.resp.status != 408:
                raise
            raise UserInputError(
                f"Drive upload did not return a confirmed result. Check file {file_id} "
                "before retrying; it may already have been created."
            ) from None
        except Exception:
            raise UserInputError(
                f"Drive upload did not return a confirmed result. Check file {file_id} "
                "before retrying; it may already have been created."
            ) from None
    try:
        saved = await asyncio.to_thread(
            drive_service.files()
            .get(fileId=file_id, fields=FILE_FIELDS, supportsAllDrives=True)
            .execute
        )
    except Exception:
        raise UserInputError(
            f"Drive upload finished but verification failed. Check file {file_id} "
            "before retrying; do not create a replacement blindly."
        ) from None
    return _receipt(saved, folder_id, len(data), checksum, "created")
