"""A content read's output is bounded.

Threads and messages that do not fit a call's output budget are listed for
another call and never fetched; the first item is shortened rather than
dropped; plain-text bodies are capped like HTML ones.
"""

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
    _format_thread_content,
    get_gmail_messages_content_batch,
    get_gmail_thread_content,
    get_gmail_threads_content_batch,
)


def _unwrap(tool):
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


def _payload(subject, text):
    return {
        "headers": [
            {"name": "Subject", "value": subject},
            {"name": "From", "value": "sender@example.com"},
        ],
        "mimeType": "text/plain",
        "body": {"data": base64.urlsafe_b64encode(text.encode()).decode()},
    }


def _thread(tid, text="thread body"):
    return {
        "id": tid,
        "messages": [{"id": f"m-{tid}", "payload": _payload(f"Subject {tid}", text)}],
    }


def _message(mid, text="message body"):
    return {"id": mid, "payload": _payload(f"Subject {mid}", text)}


class _RecordingBatch:
    def __init__(self, callback, sizes):
        self._callback, self._requests, self._sizes = callback, [], sizes

    def add(self, request, request_id):
        self._requests.append((request_id, request))

    def execute(self):
        self._sizes.append(len(self._requests))
        for request_id, request in self._requests:
            self._callback(request_id, request.execute(), None)


def _service(sizes, thread_text="thread body", message_text="message body"):
    def get(build):
        def handler(**kwargs):
            request = Mock()
            request.execute.return_value = build(kwargs["id"])
            return request

        return handler

    service = Mock()
    service.users().threads().get.side_effect = get(
        lambda tid: _thread(tid, thread_text)
    )
    service.users().messages().get.side_effect = get(
        lambda mid: _message(mid, message_text)
    )
    service.new_batch_http_request.side_effect = lambda callback: _RecordingBatch(
        callback, sizes
    )
    return service


@pytest.mark.asyncio
async def test_threads_past_the_budget_are_listed_and_their_chunks_never_fetched(
    monkeypatch,
):
    sizes = []
    one_thread = len(_format_thread_content(_thread("t0"), "t0"))
    monkeypatch.setattr(
        gmail_tools, "GMAIL_CONTENT_OUTPUT_CHAR_LIMIT", one_thread * 2 + 1
    )

    result = await _unwrap(get_gmail_threads_content_batch)(
        service=_service(sizes),
        thread_ids=[f"t{i}" for i in range(30)],
        user_google_email="user@example.com",
    )

    assert result.startswith("Retrieved 2 of 30 threads:")
    assert (
        "Subject t0" in result and "Subject t1" in result and "Subject t2" not in result
    )
    withheld = ", ".join(f"t{i}" for i in range(2, 30))
    assert result.endswith(
        f"28 threads not retrieved. Request them in another call: {withheld}]"
    )
    # Batches stay at 25; the second was never sent.
    assert sizes == [25]


@pytest.mark.asyncio
async def test_a_call_within_the_budget_is_unchanged():
    sizes = []
    result = await _unwrap(get_gmail_threads_content_batch)(
        service=_service(sizes),
        thread_ids=[f"t{i}" for i in range(30)],
        user_google_email="user@example.com",
    )
    assert result.startswith("Retrieved 30 threads:")
    assert "not retrieved" not in result
    assert sizes == [25, 5]


@pytest.mark.asyncio
async def test_a_first_message_larger_than_the_budget_is_shortened_not_dropped(
    monkeypatch,
):
    monkeypatch.setattr(gmail_tools, "GMAIL_CONTENT_OUTPUT_CHAR_LIMIT", 200)
    result = await _unwrap(get_gmail_messages_content_batch)(
        service=_service([], message_text="x" * 5000),
        message_ids=["m0", "m1"],
        user_google_email="user@example.com",
    )
    assert result.startswith("Retrieved 1 of 2 messages:")
    assert "Subject m0" in result
    assert "[Content truncated: output limit reached]" in result
    assert result.endswith(
        "1 messages not retrieved. Request them in another call: m1]"
    )


@pytest.mark.asyncio
async def test_a_single_thread_read_is_held_to_the_same_budget(monkeypatch):
    monkeypatch.setattr(gmail_tools, "GMAIL_CONTENT_OUTPUT_CHAR_LIMIT", 300)
    service = Mock()
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
