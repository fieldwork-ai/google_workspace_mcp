"""Verified, bounded uploads from a client's temporary download URL."""

import asyncio
import hashlib
import re
from tempfile import SpooledTemporaryFile
from urllib.parse import urlsplit

from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

from core.utils import UserInputError
from gdrive.drive_helpers import _stream_url_with_validation
from core.async_bridge import greenlet_spawn

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
FILE_FIELDS = "id,name,mimeType,size,sha256Checksum,webViewLink,parents,trashed"
FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"


def _receipt(file, folder_id, name, size, sha256, status):
    if (
        file.get("trashed") is not False
        or folder_id not in file.get("parents", [])
        or file.get("name") != name
        or str(file.get("size")) != str(size)
        or file.get("sha256Checksum") != sha256
    ):
        raise UserInputError(
            f"Drive file {file.get('id', 'unknown')} does not match the requested "
            "name, folder, size and SHA-256. Inspect it before retrying; "
            "no replacement was created."
        )
    return {
        "status": status,
        "file_id": file["id"],
        "web_view_link": file.get("webViewLink"),
        "name": file["name"],
        "mime_type": file.get("mimeType"),
        "size": size,
        "sha256_checksum": sha256,
    }


async def upload_file_from_url(
    service,
    file_url: str,
    folder_id: str,
    file_name: str,
    mime_type: str,
    expected_size: int,
    expected_sha256: str,
) -> dict:
    if not folder_id.strip() or not file_name.strip():
        raise UserInputError("folder_id and file_name must not be blank.")
    mime_type = mime_type.strip().lower()
    if not re.fullmatch(
        r"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+", mime_type
    ) or mime_type.startswith("application/vnd.google-apps."):
        raise UserInputError(
            "Supply a binary file MIME type; no Google-native conversion."
        )
    if (
        not isinstance(expected_size, int)
        or isinstance(expected_size, bool)
        or not 0 <= expected_size <= MAX_UPLOAD_BYTES
    ):
        raise UserInputError("expected_size must be between 0 and 25 MiB.")
    if not re.fullmatch(r"[a-fA-F0-9]{64}", expected_sha256):
        raise UserInputError(
            "expected_sha256 must be the source file's SHA-256 hex digest."
        )
    expected_sha256 = expected_sha256.lower()
    try:
        parsed = urlsplit(file_url)
        valid_url = (
            parsed.scheme == "https"
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and not parsed.fragment
        )
    except ValueError:
        valid_url = False
    if not valid_url:
        raise UserInputError(
            "file_url must be an HTTPS download URL without user info or fragment."
        )

    folder = await greenlet_spawn(
        service.files()
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

    def escaped(value):
        return value.replace("\\", "\\\\").replace("'", "\\'")

    search = {
        "q": f"'{escaped(folder_id)}' in parents and trashed = false "
        f"and name = '{escaped(file_name)}'",
        "fields": f"files({FILE_FIELDS}),nextPageToken,incompleteSearch",
        "pageSize": 2,
        "spaces": "drive",
        "supportsAllDrives": True,
        "includeItemsFromAllDrives": True,
        "corpora": "drive" if folder.get("driveId") else "user",
    }
    if folder.get("driveId"):
        search["driveId"] = folder["driveId"]
    existing = await greenlet_spawn(service.files().list(**search).execute)
    files = existing.get("files", [])
    if (
        existing.get("incompleteSearch")
        or existing.get("nextPageToken")
        or len(files) > 1
    ):
        raise UserInputError(
            "Drive filename lookup is incomplete or ambiguous; nothing uploaded."
        )
    if files:
        return _receipt(
            files[0], folder_id, file_name, expected_size, expected_sha256, "existing"
        )

    with SpooledTemporaryFile(max_size=1024 * 1024) as stream:
        size = 0
        digest = hashlib.sha256()

        async def collect(chunk):
            nonlocal size
            size += len(chunk)
            if size > expected_size:
                raise UserInputError("Source exceeds expected_size; nothing uploaded.")
            digest.update(chunk)
            await asyncio.to_thread(stream.write, chunk)

        try:
            async with asyncio.timeout(120):
                await _stream_url_with_validation(file_url, collect)
        except UserInputError:
            raise
        except Exception:
            # Signed URLs are credentials; neither provider errors nor logs may echo them.
            raise UserInputError(
                "Could not fetch the source file. Renew its download URL and retry."
            ) from None
        if size != expected_size or digest.hexdigest() != expected_sha256:
            raise UserInputError("Source size or SHA-256 mismatch; nothing uploaded.")
        await asyncio.to_thread(stream.seek, 0)
        ids = await greenlet_spawn(
            service.files().generateIds(count=1, space="drive").execute
        )
        file_id = ids["ids"][0]
        media = MediaIoBaseUpload(
            stream, mimetype=mime_type, resumable=True, chunksize=5 * 1024 * 1024
        )
        request = service.files().create(
            body={
                "id": file_id,
                "name": file_name,
                "mimeType": mime_type,
                "parents": [folder_id],
            },
            media_body=media,
            fields="id",
            supportsAllDrives=True,
        )
        try:
            await greenlet_spawn(request.execute, num_retries=0)
        except HttpError as error:
            if error.resp.status < 500 and error.resp.status != 408:
                raise
            raise UserInputError(
                f"Upload outcome unknown. Inspect Drive file {file_id} before retrying."
            ) from None
        except Exception:
            raise UserInputError(
                f"Upload outcome unknown. Inspect Drive file {file_id} before retrying."
            ) from None
    try:
        saved = await greenlet_spawn(
            service.files()
            .get(fileId=file_id, fields=FILE_FIELDS, supportsAllDrives=True)
            .execute
        )
    except Exception:
        raise UserInputError(
            f"Upload verification failed. Inspect Drive file {file_id} before retrying."
        ) from None
    return _receipt(
        saved, folder_id, file_name, expected_size, expected_sha256, "created"
    )
