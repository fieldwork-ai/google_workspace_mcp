"""Full-content Gmail reads have bounded memory.

One account's parallel batch reads of 25 full threads at a time exhausted the
hosted server's memory and took it down for every account. These tests hold the
bounds: full content is fetched in small chunks, a call's output has a budget
(what does not fit is listed for another call), plain-text bodies are capped
like HTML ones, and content reads queue per account and per process.
"""

import asyncio
import base64
import os
import sys
from unittest.mock import Mock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

import gmail.gmail_tools as gmail_tools
from gmail.gmail_tools import (
    HTML_BODY_TRUNCATE_LIMIT,
    _format_body_content,
    get_gmail_messages_content_batch,
    get_gmail_thread_content,
    get_gmail_threads_content_batch,
)


def _unwrap(tool):
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


def _b64(text):
    return base64.urlsafe_b64encode(text.encode()).decode()


def _payload(subject, text):
    return {
        "headers": [
            {"name": "Subject", "value": subject},
            {"name": "From", "value": "sender@example.com"},
            {"name": "To", "value": "recipient@example.com"},
            {"name": "Date", "value": "Fri, 28 Mar 2026 10:00:00 -0400"},
        ],
        "mimeType": "text/plain",
        "body": {"data": _b64(text)},
    }


def _thread(tid, text="thread body"):
    return {
        "id": tid,
        "messages": [{"id": f"m-{tid}", "payload": _payload(f"Subject {tid}", text)}],
    }


def _message(mid, text="message body"):
    return {"id": mid, "payload": _payload(f"Subject {mid}", text)}


class _RecordingBatch:
    """Runs each request and records how many requests one batch carried."""

    sizes: list

    def __init__(self, callback, sizes):
        self._callback = callback
        self._requests = []
        self._sizes = sizes

    def add(self, request, request_id):
        self._requests.append((request_id, request))

    def execute(self):
        self._sizes.append(len(self._requests))
        for request_id, request in self._requests:
            self._callback(request_id, request.execute(), None)


def _service(sizes, thread_text="thread body", message_text="message body"):
    def thread_get(**kwargs):
        request = Mock()
        request.execute.return_value = _thread(kwargs["id"], thread_text)
        return request

    def message_get(**kwargs):
        request = Mock()
        request.execute.return_value = _message(kwargs["id"], message_text)
        return request

    service = Mock()
    service.users().threads().get.side_effect = thread_get
    service.users().messages().get.side_effect = message_get
    service.new_batch_http_request.side_effect = lambda callback: _RecordingBatch(
        callback, sizes
    )
    return service


@pytest.fixture(autouse=True)
def fresh_limits(monkeypatch):
    # asyncio primitives bind to the loop that first waits on them; each test runs its own loop.
    monkeypatch.setattr(
        gmail_tools,
        "_content_reads",
        asyncio.Semaphore(gmail_tools.GMAIL_CONTENT_READS_PER_PROCESS),
    )
    monkeypatch.setattr(gmail_tools, "_account_content_reads", {})


@pytest.mark.asyncio
async def test_full_threads_are_fetched_five_at_a_time():
    sizes = []
    result = await _unwrap(get_gmail_threads_content_batch)(
        service=_service(sizes),
        thread_ids=[f"t{i}" for i in range(12)],
        user_google_email="user@example.com",
    )
    assert sizes == [5, 5, 2]
    assert result.startswith("Retrieved 12 threads:")
    assert all(f"Subject t{i}" in result for i in range(12))


@pytest.mark.asyncio
async def test_full_messages_are_fetched_ten_at_a_time_and_metadata_twenty_five():
    full, metadata = [], []
    await _unwrap(get_gmail_messages_content_batch)(
        service=_service(full),
        message_ids=[f"m{i}" for i in range(23)],
        user_google_email="user@example.com",
    )
    await _unwrap(get_gmail_messages_content_batch)(
        service=_service(metadata),
        message_ids=[f"m{i}" for i in range(30)],
        user_google_email="user@example.com",
        format="metadata",
    )
    assert full == [10, 10, 3]
    assert metadata == [25, 5]


@pytest.mark.asyncio
async def test_threads_past_the_output_budget_are_listed_and_never_fetched(monkeypatch):
    sizes = []
    one_thread = len(gmail_tools._format_thread_content(_thread("t0"), "t0"))
    # Room for exactly two threads.
    monkeypatch.setattr(
        gmail_tools, "GMAIL_CONTENT_OUTPUT_CHAR_LIMIT", one_thread * 2 + 1
    )

    result = await _unwrap(get_gmail_threads_content_batch)(
        service=_service(sizes),
        thread_ids=[f"t{i}" for i in range(12)],
        user_google_email="user@example.com",
    )

    assert result.startswith("Retrieved 2 of 12 threads:")
    assert "Subject t0" in result and "Subject t1" in result
    assert "Subject t2" not in result
    withheld = ", ".join(f"t{i}" for i in range(2, 12))
    assert (
        f"10 threads not retrieved. Request them in another call: {withheld}]" in result
    )
    # The first chunk exhausted the budget, so no later chunk was fetched.
    assert sizes == [5]


@pytest.mark.asyncio
async def test_a_first_item_larger_than_the_budget_is_shortened_not_dropped(
    monkeypatch,
):
    monkeypatch.setattr(gmail_tools, "GMAIL_CONTENT_OUTPUT_CHAR_LIMIT", 200)
    result = await _unwrap(get_gmail_messages_content_batch)(
        service=_service([], message_text="x" * 5000),
        message_ids=["m0", "m1"],
        user_google_email="user@example.com",
    )
    assert "Subject m0" in result
    assert "[Content truncated: output limit reached]" in result
    assert "1 messages not retrieved. Request them in another call: m1]" in result


@pytest.mark.asyncio
async def test_a_single_thread_read_is_held_to_the_same_budget(monkeypatch):
    monkeypatch.setattr(gmail_tools, "GMAIL_CONTENT_OUTPUT_CHAR_LIMIT", 300)
    service = _service([], thread_text="y" * 5000)
    service.users().threads().get.side_effect = None
    service.users().threads().get.return_value.execute.return_value = _thread(
        "t0", "y" * 5000
    )

    result = await _unwrap(get_gmail_thread_content)(
        service=service, thread_id="t0", user_google_email="user@example.com"
    )

    assert len(result) < 400
    assert result.endswith("[Content truncated: output limit reached]")


def test_plain_text_bodies_are_capped_like_html():
    body = _format_body_content("z" * (HTML_BODY_TRUNCATE_LIMIT + 500), "")
    assert body.startswith("z" * HTML_BODY_TRUNCATE_LIMIT)
    assert body.endswith("[Content truncated...]")
    assert len(body) < HTML_BODY_TRUNCATE_LIMIT + 100


async def _peak_concurrent_reads(monkeypatch, accounts):
    active = peak = 0

    async def slow_spawn(fn, *args, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return fn(*args, **kwargs)

    monkeypatch.setattr(gmail_tools, "greenlet_spawn", slow_spawn)
    await asyncio.gather(
        *(
            _unwrap(get_gmail_threads_content_batch)(
                service=_service([]),
                thread_ids=["t0", "t1"],
                user_google_email=account,
            )
            for account in accounts
        )
    )
    return peak


@pytest.mark.asyncio
async def test_one_account_runs_at_most_two_content_reads_at_once(monkeypatch):
    peak = await _peak_concurrent_reads(monkeypatch, ["ceo@example.com"] * 8)
    assert peak == gmail_tools.GMAIL_CONTENT_READS_PER_ACCOUNT == 2


@pytest.mark.asyncio
async def test_the_process_runs_at_most_six_content_reads_at_once(monkeypatch):
    peak = await _peak_concurrent_reads(
        monkeypatch, [f"user{i}@example.com" for i in range(12)]
    )
    assert peak == gmail_tools.GMAIL_CONTENT_READS_PER_PROCESS == 6


@pytest.mark.asyncio
async def test_an_account_is_one_account_whatever_its_case(monkeypatch):
    peak = await _peak_concurrent_reads(
        monkeypatch, ["CEO@example.com", "ceo@example.com", "Ceo@Example.com"]
    )
    assert peak == 2
