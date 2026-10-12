"""A thread read keeps its newest message whole.

Replies lose the earlier messages they quote, since those are in the thread
already, and when a thread is still too long for one read, older bodies are
dropped before the newest message's.
"""

import base64
import json
import os
import sys
from unittest.mock import Mock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from gmail.gmail_tools import (
    GMAIL_THREAD_CHAR_BUDGET,
    QUOTED_HISTORY_NOTE,
    _format_thread_content,
    _strip_quoted_history,
    get_gmail_thread_content,
    get_gmail_threads_content_batch,
)


def _unwrap(tool):
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


def _message(mid, sender, text, subject="Stoa"):
    return {
        "id": mid,
        "payload": {
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "From", "value": sender},
                {"name": "Date", "value": f"Thu, 1 Oct 2026 11:{mid[-1]}0:00 +0000"},
            ],
            "mimeType": "text/plain",
            "body": {"data": base64.urlsafe_b64encode(text.encode()).decode()},
        },
    }


DISCLAIMER = "This communication is confidential. " * 40


def _replying_thread(count, newest):
    """A thread where every reply quotes everything before it, as mail clients do."""
    messages, history = [], ""
    for n in range(count):
        own = newest if n == count - 1 else f"Reply number {n}, about the second call."
        text = f"{own}\n\nLorcan\n{DISCLAIMER}"
        if history:
            quoted = "\n".join(f"> {line}" for line in history.split("\n"))
            text += f"\n\nOn Thu, Oct 1, 2026 at 11:{n}0, Ann <ann@example.com> wrote:\n\n{quoted}"
        messages.append(_message(f"m{n}", "Lorcan <lorcan@example.com>", text))
        history = text
    return {"id": "t0", "messages": messages}


def test_a_reply_loses_the_messages_it_quotes():
    body = (
        "Sounds good, let's pass.\n\nLorcan\n\n"
        "On Thu, Oct 1, 2026 at 2:17 PM Ann <ann@example.com> wrote:\n\n"
        "> Shall we do a second call?\n> Ann"
    )
    assert _strip_quoted_history(body) == (
        f"Sounds good, let's pass.\n\nLorcan\n\n{QUOTED_HISTORY_NOTE}"
    )


def test_an_attribution_wrapped_over_lines_is_found():
    body = (
        "Thanks!\n\nOn Thu, Oct 01, 2026 at 12:13:42, Mikko Savolainen < mikko. savolainen@ illusian.\n"
        "org ( mikko.savolainen@illusian.org ) > wrote:\n\n> Over to you"
    )
    assert _strip_quoted_history(body) == f"Thanks!\n\n{QUOTED_HISTORY_NOTE}"


def test_an_unprefixed_quote_and_an_outlook_header_are_both_cut():
    unprefixed = (
        "Copying Johannes.\n\nOn Thu, Oct 01, 2026 at 2:10 PM, Michael <michael@example.com> wrote:\n"
        "Fab! Would love an intro."
    )
    outlook = (
        "Agreed.\n\n________________________________\nFrom: Ann <ann@example.com>\n"
        "Sent: Thursday, October 1, 2026 11:00\nTo: Lorcan\nSubject: Stoa\n\nShall we?"
    )
    assert (
        _strip_quoted_history(unprefixed)
        == f"Copying Johannes.\n\n{QUOTED_HISTORY_NOTE}"
    )
    assert _strip_quoted_history(outlook).startswith("Agreed.\n\n____")
    assert _strip_quoted_history(outlook).endswith(QUOTED_HISTORY_NOTE)
    assert "Shall we?" not in _strip_quoted_history(outlook)


def test_an_inline_reply_and_a_bare_quote_are_kept():
    inline = (
        "See below.\n\nOn Thu, Oct 1, 2026 at 2:17 PM Ann <ann@example.com> wrote:\n"
        "> Can you do Monday?\nYes, 10am.\n> And bring the deck?\nWill do."
    )
    only_quote = "On Thu, Oct 1, 2026 at 2:17 PM Ann <ann@example.com> wrote:\n> Hello"
    prose = "On Monday the team wrote:\nnothing new."
    assert _strip_quoted_history(inline) == inline
    assert _strip_quoted_history(only_quote) == only_quote
    assert _strip_quoted_history(prose) == prose


def test_the_first_message_and_forwards_keep_their_quotes():
    quoting = "Context below.\n\nOn Thu, Oct 1, 2026 at 2:17 PM Ann <ann@example.com> wrote:\n> Earlier"
    thread = {
        "id": "t0",
        "messages": [
            _message("m0", "a@example.com", quoting),
            _message("m1", "b@example.com", quoting, subject="Fwd: Stoa"),
            _message("m2", "c@example.com", quoting),
        ],
    }
    content = _format_thread_content(thread, "t0")
    assert content.count("> Earlier") == 2
    assert content.count(QUOTED_HISTORY_NOTE) == 1


def test_a_long_thread_keeps_its_newest_message_whole():
    thread = _replying_thread(8, newest="We have decided to pass on Stoa.")
    unstripped = sum(
        len(base64.urlsafe_b64decode(m["payload"]["body"]["data"]))
        for m in thread["messages"]
    )
    assert unstripped > 30_000

    content = _format_thread_content(thread, "t0", char_budget=GMAIL_THREAD_CHAR_BUDGET)

    assert "We have decided to pass on Stoa." in content
    assert content.count("=== Message") == 8
    # A client's 30,000-character cap applies after JSON escaping.
    assert len(json.dumps({"result": content})) < 30_000


def test_older_bodies_go_first_and_say_how_to_read_them():
    thread = _replying_thread(8, newest="We have decided to pass on Stoa.")

    content = _format_thread_content(thread, "t0", char_budget=7_500)

    assert "We have decided to pass on Stoa." in content
    assert "Reply number 0" in content
    assert "Reply number 6" in content
    assert "Reply number 5," not in content
    assert "get_gmail_messages_content_batch(message_ids=['m1'])" in content
    assert len(content) <= 7_500


def test_without_a_budget_every_body_stays():
    thread = _replying_thread(8, newest="We have decided to pass on Stoa.")
    content = _format_thread_content(thread, "t0")
    assert all(f"Reply number {n}," in content for n in range(7))


@pytest.mark.asyncio
async def test_both_thread_tools_apply_the_budget():
    thread = _replying_thread(30, newest="We have decided to pass on Stoa.")
    service = Mock()
    service.users().threads().get.return_value.execute.return_value = thread
    service.new_batch_http_request.side_effect = RuntimeError("no batch")

    single = await _unwrap(get_gmail_thread_content)(
        service=service, thread_id="t0", user_google_email="user@example.com"
    )
    batch = await _unwrap(get_gmail_threads_content_batch)(
        service=service, thread_ids=["t0"], user_google_email="user@example.com"
    )

    for result in (single, batch):
        assert "We have decided to pass on Stoa." in result
        assert len(result) < GMAIL_THREAD_CHAR_BUDGET + 200
