"""
Run the synchronous Google API client on the event loop.

The Google client (`googleapiclient`) is synchronous: every call, media
download, resumable upload and batch ends in ONE seam, `http.request(uri,
method, body, headers)` on a pluggable httplib2-shaped transport, and does
nothing but CPU work in between. So the library keeps its code unchanged and
gets a transport that suspends the synchronous frame at that seam, awaits an
async HTTP call on the loop, and resumes the frame with the result. That is
the greenlet bridge SQLAlchemy has shipped since 1.4 to drive its unchanged
synchronous ORM over async database drivers.

    result = await greenlet_spawn(service.users().messages().list(...).execute)

`greenlet_spawn` runs the callable in a greenlet on the loop thread. When the
callable reaches `BridgeHttp.request`, that calls `await_only(coroutine)`,
which switches back to the loop; the loop awaits the coroutine and switches
back into the callable with the response. No thread pool, no thread cap, and
one shared connection pool (HTTP/2, keep-alive) instead of a TLS handshake per
call.

Calling `await_only` outside `greenlet_spawn` raises immediately: a call site
the sweep missed fails the first test that reaches it, never production.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional, TypeVar

import greenlet
import httplib2
import httpx

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: Per-request socket timeout, matching the httplib2.Http(timeout=30) the
#: transport used to be built with.
DEFAULT_TIMEOUT_SECONDS = 30.0

# httplib2 follows these itself and hands the caller the final response. 308 is
# deliberately absent: Drive answers a resumable-upload chunk with
# "308 Resume Incomplete" plus a Range header, which the Google client reads,
# so it must reach the caller untouched.
_FOLLOWED_REDIRECTS = frozenset({300, 301, 302, 303, 307})


class _BridgeGreenlet(greenlet.greenlet):
    """A greenlet whose parent is the loop-side driver, sharing its context
    so contextvars set by the request (auth token, logging fields) are visible
    inside the synchronous frame."""

    __slots__ = ("driver",)

    def __init__(self, fn: Callable[..., Any], driver: greenlet.greenlet) -> None:
        super().__init__(fn, driver)
        self.driver = driver
        self.gr_context = driver.gr_context


async def greenlet_spawn(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run a synchronous callable on the loop thread, awaiting every
    `await_only` it makes on the way. Drop-in for `asyncio.to_thread`."""
    context = _BridgeGreenlet(fn, greenlet.getcurrent())
    # Each switch returns either an awaitable handed up by await_only or, once
    # the greenlet is dead, the callable's return value.
    switch_result = context.switch(*args, **kwargs)
    while not context.dead:
        try:
            value = await switch_result
        except BaseException as exc:  # noqa: BLE001 - re-raised inside the frame
            switch_result = context.throw(exc)
        else:
            switch_result = context.switch(value)
    return switch_result


def await_only(awaitable: Awaitable[T]) -> T:
    """From inside a `greenlet_spawn` frame: await on the loop and return the
    result, raising whatever the awaitable raised."""
    current = greenlet.getcurrent()
    if not isinstance(current, _BridgeGreenlet):
        raise RuntimeError(
            "await_only() called outside greenlet_spawn(): this Google client "
            "call is not being driven by the bridge (wrap the call in "
            "greenlet_spawn rather than asyncio.to_thread)"
        )
    return current.driver.switch(awaitable)


# ---------------------------------------------------------------------------
# The shared HTTP client
# ---------------------------------------------------------------------------

_shared: Optional[tuple[asyncio.AbstractEventLoop, httpx.AsyncClient]] = None


def shared_client() -> httpx.AsyncClient:
    """One connection pool per event loop. An AsyncClient is bound to the loop
    it first runs on; the server has one loop for its whole life, and the
    per-loop check exists for the test suite, which makes a loop per test."""
    global _shared
    loop = asyncio.get_running_loop()
    if _shared is None or _shared[0] is not loop:
        _shared = (
            loop,
            httpx.AsyncClient(
                http2=True,
                follow_redirects=False,
                timeout=httpx.Timeout(DEFAULT_TIMEOUT_SECONDS),
                limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
            ),
        )
    return _shared[1]


async def close_shared_client() -> None:
    global _shared
    if _shared is not None:
        client = _shared[1]
        _shared = None
        await client.aclose()


# ---------------------------------------------------------------------------
# The transport
# ---------------------------------------------------------------------------


def _response_from(r: httpx.Response) -> httplib2.Response:
    """Build the object the Google client reads: a dict of lowercased headers
    with `.status` and `.reason`, duplicates joined the way httplib2 joins
    them. Content arrives decoded, so the encoding header goes, as httplib2
    drops it after decoding."""
    headers: dict[str, str] = {}
    for key, value in r.headers.multi_items():
        key = key.lower()
        headers[key] = f"{headers[key]}, {value}" if key in headers else value
    headers.pop("content-encoding", None)
    headers["status"] = str(r.status_code)
    response = httplib2.Response(headers)
    response.status = r.status_code
    response.reason = r.reason_phrase
    return response


async def _request(
    uri: str,
    method: str,
    body: Any,
    headers: Optional[dict[str, str]],
    redirections: int,
    timeout: Optional[float],
) -> tuple[httplib2.Response, bytes]:
    client = shared_client()
    request_headers = dict(headers or {})
    # The Google client hands a resumable chunk over as a bounded file-like
    # slice; httpx wants the bytes.
    if hasattr(body, "read"):
        body = body.read()
    if isinstance(body, str):
        body = body.encode("utf-8")
    request_timeout = (
        httpx.Timeout(timeout) if timeout is not None else httpx.USE_CLIENT_DEFAULT
    )
    while True:
        r = await client.request(
            method, uri, content=body, headers=request_headers, timeout=request_timeout
        )
        location = r.headers.get("location")
        follow = (
            r.status_code in _FOLLOWED_REDIRECTS
            and location is not None
            and redirections > 0
            and (method in ("GET", "HEAD") or r.status_code == 303)
        )
        if not follow:
            return _response_from(r), r.content
        redirections -= 1
        uri = str(r.url.join(location))
        if r.status_code in (302, 303):
            method, body = "GET", None
            request_headers.pop("content-type", None)
            request_headers.pop("Content-Type", None)


class BridgeHttp:
    """The httplib2-shaped transport the Google client calls. Every request
    made through it must be inside a `greenlet_spawn` frame."""

    def __init__(self, timeout: Optional[float] = None) -> None:
        self.timeout = timeout

    def request(
        self,
        uri: str,
        method: str = "GET",
        body: Any = None,
        headers: Optional[dict[str, str]] = None,
        redirections: int = httplib2.DEFAULT_MAX_REDIRECTS,
        connection_type: Any = None,
    ) -> tuple[httplib2.Response, bytes]:
        return await_only(
            _request(uri, method, body, headers, redirections, self.timeout)
        )
