"""The bridge drives the unchanged Google client on the event loop.

Every path the client takes ends at `http.request`; these tests push each of
them (plain call, media download, resumable upload, batch, redirects) through
`BridgeHttp` against an in-process httpx mock transport and assert the frame
never leaves the loop thread.
"""

from __future__ import annotations

import asyncio
import contextvars
import io
import json
import re
import threading

import httpx
import pytest
from google.oauth2.credentials import Credentials
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.http import (
    BatchHttpRequest,
    HttpRequest,
    MediaIoBaseDownload,
    MediaIoBaseUpload,
)

from core import async_bridge
from core.async_bridge import BridgeHttp, await_only, greenlet_spawn


def _json_postproc(resp, content):
    return json.loads(content.decode("utf-8")) if content else None


@pytest.fixture
def transport(monkeypatch):
    """A mock transport standing in for the shared client; `seen` records
    every request the client made and on which thread."""
    seen: list[dict] = []
    handlers: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            {
                "method": request.method,
                "url": str(request.url),
                "headers": dict(request.headers),
                "content": request.content,
                "thread": threading.current_thread(),
            }
        )
        return (
            handlers.pop(0)(request)
            if handlers
            else httpx.Response(200, json={"ok": True})
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    )
    monkeypatch.setattr(async_bridge, "shared_client", lambda: client)
    return {"seen": seen, "handlers": handlers}


def _authorized() -> AuthorizedHttp:
    return AuthorizedHttp(Credentials(token="ya29.test"), http=BridgeHttp())


@pytest.mark.asyncio
async def test_greenlet_spawn_runs_the_frame_and_awaits_inside_it():
    async def add(a, b):
        await asyncio.sleep(0)
        return a + b

    def frame(x):
        return await_only(add(x, 1)) * 2

    assert await greenlet_spawn(frame, 20) == 42


@pytest.mark.asyncio
async def test_exceptions_cross_the_bridge_both_ways():
    async def boom():
        raise ValueError("from the loop")

    def frame():
        try:
            await_only(boom())
        except ValueError as exc:
            raise KeyError(str(exc)) from exc

    with pytest.raises(KeyError, match="from the loop"):
        await greenlet_spawn(frame)


def test_await_only_outside_the_bridge_is_loud():
    async def never():
        return None

    coro = never()
    try:
        with pytest.raises(RuntimeError, match="outside greenlet_spawn"):
            await_only(coro)
    finally:
        coro.close()


@pytest.mark.asyncio
async def test_contextvars_are_visible_inside_the_frame():
    var: contextvars.ContextVar[str] = contextvars.ContextVar("who")
    var.set("archie")
    assert await greenlet_spawn(var.get) == "archie"


@pytest.mark.asyncio
async def test_plain_request_stays_on_the_loop_thread_and_carries_the_bearer(transport):
    request = HttpRequest(
        _authorized(),
        _json_postproc,
        "https://gmail.googleapis.com/gmail/v1/users/me/profile",
    )
    result = await greenlet_spawn(request.execute)
    assert result == {"ok": True}
    [call] = transport["seen"]
    assert call["thread"] is threading.main_thread()
    assert call["headers"]["authorization"] == "Bearer ya29.test"


@pytest.mark.asyncio
async def test_media_download_reads_ranges_until_done(transport):
    body = b"0123456789"

    def chunk(request: httpx.Request) -> httpx.Response:
        start, end = (
            int(x) for x in request.headers["range"].removeprefix("bytes=").split("-")
        )
        end = min(end, len(body) - 1)
        return httpx.Response(
            206,
            content=body[start : end + 1],
            headers={"content-range": f"bytes {start}-{end}/{len(body)}"},
        )

    transport["handlers"].extend([chunk, chunk, chunk])
    request = HttpRequest(
        _authorized(),
        _json_postproc,
        "https://www.googleapis.com/drive/v3/files/x?alt=media",
    )
    sink = io.BytesIO()
    downloader = MediaIoBaseDownload(sink, request, chunksize=4)

    def download():
        done = False
        while not done:
            _, done = downloader.next_chunk()

    await greenlet_spawn(download)
    assert sink.getvalue() == body
    assert len(transport["seen"]) == 3


@pytest.mark.asyncio
async def test_resumable_upload_sees_308_resume_incomplete_itself(transport):
    payload = b"abcdefgh"

    def start(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"location": "https://upload.googleapis.com/session/1"}
        )

    def first_chunk(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        assert request.content == payload[:4]
        return httpx.Response(308, headers={"range": "bytes=0-3"})

    def last_chunk(request: httpx.Request) -> httpx.Response:
        assert request.content == payload[4:]
        return httpx.Response(200, json={"id": "uploaded"})

    transport["handlers"].extend([start, first_chunk, last_chunk])
    media = MediaIoBaseUpload(
        io.BytesIO(payload), mimetype="text/plain", chunksize=4, resumable=True
    )
    request = HttpRequest(
        _authorized(),
        _json_postproc,
        "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable",
        method="POST",
        body="{}",
        headers={"content-type": "application/json"},
        resumable=media,
    )

    def upload():
        response = None
        while response is None:
            _, response = request.next_chunk()
        return response

    assert await greenlet_spawn(upload) == {"id": "uploaded"}
    assert [c["method"] for c in transport["seen"]] == ["POST", "PUT", "PUT"]


@pytest.mark.asyncio
async def test_batch_request_round_trips_multipart(transport):
    def batch_response(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        # The client names each part <base+id> and expects <response-base+id> back.
        ids = re.findall(r"Content-ID: <([^>]+)>", request.content.decode())
        assert [i.rsplit("+", 1)[1].strip() for i in ids] == ["1", "2"]
        boundary = "batch_reply"
        parts = []
        for content_id, payload in zip(ids, ({"n": 1}, {"n": 2})):
            parts.append(
                f"--{boundary}\r\n"
                "Content-Type: application/http\r\n"
                f"Content-ID: <response-{content_id}>\r\n\r\n"
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: application/json\r\n\r\n"
                f"{json.dumps(payload)}\r\n"
            )
        body = "".join(parts) + f"--{boundary}--\r\n"
        return httpx.Response(
            200,
            content=body.encode(),
            headers={"content-type": f'multipart/mixed; boundary="{boundary}"'},
        )

    transport["handlers"].append(batch_response)
    results: dict[str, dict] = {}

    def collect(request_id, response, exception):
        assert exception is None
        results[request_id] = response

    batch = BatchHttpRequest(
        callback=collect, batch_uri="https://www.googleapis.com/batch/gmail/v1"
    )
    http = _authorized()
    for n in ("1", "2"):
        batch.add(
            HttpRequest(
                http,
                _json_postproc,
                f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{n}",
            ),
            request_id=n,
        )
    await greenlet_spawn(batch.execute)
    assert results == {"1": {"n": 1}, "2": {"n": 2}}


@pytest.mark.asyncio
async def test_get_redirects_are_followed_but_308_is_returned(transport):
    transport["handlers"].append(
        lambda r: httpx.Response(
            302, headers={"location": "https://example.test/final"}
        )
    )
    transport["handlers"].append(lambda r: httpx.Response(200, json={"at": "final"}))
    request = HttpRequest(_authorized(), _json_postproc, "https://example.test/start")
    assert await greenlet_spawn(request.execute) == {"at": "final"}
    assert [c["url"] for c in transport["seen"]] == [
        "https://example.test/start",
        "https://example.test/final",
    ]

    transport["seen"].clear()
    transport["handlers"].append(
        lambda r: httpx.Response(308, headers={"range": "bytes=0-3"})
    )
    response, _ = await greenlet_spawn(
        BridgeHttp().request, "https://upload.test/session", "PUT", b"data"
    )
    assert response.status == 308
    assert response["range"] == "bytes=0-3"
    assert len(transport["seen"]) == 1
