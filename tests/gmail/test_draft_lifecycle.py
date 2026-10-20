import base64
import os
import re
import sys
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from unittest.mock import Mock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

import gmail.gmail_tools as gmail_tools
from auth.scopes import GMAIL_COMPOSE_SCOPE, GMAIL_SEND_SCOPE
from core.utils import UserInputError
from gmail.gmail_tools import (
    delete_gmail_draft,
    list_gmail_drafts,
    send_gmail_draft,
    update_gmail_draft,
)


def _unwrap(tool):
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


def _required_scopes(tool):
    fn = tool.fn if hasattr(tool, "fn") else tool
    while not hasattr(fn, "_required_google_scopes") and hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return set(fn._required_google_scopes)


PNG = b"\x89PNG\r\n\x1a\nfake-image-bytes"
PDF = b"%PDF-1.4 fake pdf bytes"


def _stored_reply_draft(*, subject="Re: Quarterly numbers", cid="ii_abc123"):
    """A reply draft as Gmail stores it: HTML with a plain alternative, an
    inline image and a PDF, threaded by its reply headers."""
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = "Example User <user@example.com>"
    message["To"] = "alex@example.com"
    message["Cc"] = "sam@example.com"
    message["Bcc"] = "archive@example.com"
    message["Subject"] = subject
    message["In-Reply-To"] = "<orig@example.com>"
    message["References"] = "<root@example.com> <orig@example.com>"
    message.set_content("Hi Alex,\n\nNumbers attached.")
    message.add_alternative(
        f'<p>Hi Alex,</p><p>Numbers attached.</p><img src="cid:{cid}">',
        subtype="html",
    )
    message.get_payload()[1].add_related(
        PNG, maintype="image", subtype="png", cid=f"<{cid}>", filename="chart.png"
    )
    message.add_attachment(
        PDF, maintype="application", subtype="pdf", filename="Q3 numbers.pdf"
    )
    return base64.urlsafe_b64encode(message.as_bytes()).decode().rstrip("=")


def _stored_new_draft():
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = "user@example.com"
    message["To"] = "alex@example.com"
    message["Subject"] = "Lunch?"
    message.set_content("Free on Thursday?")
    return base64.urlsafe_b64encode(message.as_bytes()).decode()


def _service(raw, *, thread_id="thread-1", signature=""):
    service = Mock()
    drafts = service.users.return_value.drafts.return_value
    drafts.get.return_value.execute.return_value = {
        "id": "r-draft-1",
        "message": {"id": "msg-old", "threadId": thread_id, "raw": raw},
    }
    drafts.update.return_value.execute.return_value = {
        "id": "r-draft-1",
        "message": {"id": "msg-new", "threadId": thread_id},
    }
    service.users.return_value.settings.return_value.sendAs.return_value.list.return_value.execute.return_value = {
        "sendAs": [
            {
                "sendAsEmail": "user@example.com",
                "isPrimary": True,
                "isDefault": True,
                "signature": signature,
            }
        ]
    }
    return service


def _updated(service):
    kwargs = service.users.return_value.drafts.return_value.update.call_args.kwargs
    raw = kwargs["body"]["message"]["raw"]
    message = BytesParser(policy=policy.default).parsebytes(
        base64.urlsafe_b64decode(raw)
    )
    return kwargs, message


def _leaf_parts(message):
    return {
        part.get_filename(): part
        for part in message.walk()
        if not part.is_multipart() and part.get_filename()
    }


@pytest.mark.asyncio
async def test_update_changing_only_recipients_keeps_everything_else():
    service = _service(_stored_reply_draft())

    result = await _unwrap(update_gmail_draft)(
        service=service,
        user_google_email="user@example.com",
        draft_id="r-draft-1",
        to="alex@example.com, jo@example.com",
    )

    kwargs, message = _updated(service)
    assert kwargs["id"] == "r-draft-1"
    assert kwargs["body"]["id"] == "r-draft-1"
    assert kwargs["body"]["message"]["threadId"] == "thread-1"
    assert message["To"] == "alex@example.com, jo@example.com"
    assert message["Cc"] == "sam@example.com"
    assert message["Bcc"] == "archive@example.com"
    assert message["Subject"] == "Re: Quarterly numbers"
    assert message["From"] == "Example User <user@example.com>"
    assert message["In-Reply-To"] == "<orig@example.com>"
    assert message["References"] == "<root@example.com> <orig@example.com>"
    assert "Numbers attached." in message.get_body(("html",)).get_content()
    assert "Numbers attached." in message.get_body(("plain",)).get_content()

    parts = _leaf_parts(message)
    assert parts["Q3 numbers.pdf"].get_payload(decode=True) == PDF
    assert parts["Q3 numbers.pdf"].get_content_disposition() == "attachment"
    assert parts["chart.png"].get_payload(decode=True) == PNG
    assert parts["chart.png"]["Content-ID"] == "<ii_abc123>"
    assert parts["chart.png"].get_content_disposition() == "inline"

    assert result == "Draft updated! Draft ID: r-draft-1\nMessage ID: msg-new"
    # The app reads the draft id as the token after "Draft ID:".
    assert re.search(r"Draft ID:\s*([^\s\"]+)", result).group(1) == "r-draft-1"


@pytest.mark.asyncio
async def test_update_keeps_a_deliberately_renamed_reply_subject():
    service = _service(_stored_reply_draft(subject="November update"))

    await _unwrap(update_gmail_draft)(
        service=service,
        user_google_email="user@example.com",
        draft_id="r-draft-1",
        cc="",
    )

    _, message = _updated(service)
    assert message["Subject"] == "November update"
    assert message["Cc"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("new_subject", "expected"),
    [
        ("Quarterly numbers", "Re: Quarterly numbers"),
        ("Re: Quarterly numbers", "Re: Quarterly numbers"),
        ("Revised forecast", "Revised forecast"),
    ],
)
async def test_update_subject_on_a_reply_follows_draft_gmail_message(
    new_subject, expected
):
    service = _service(_stored_reply_draft())

    await _unwrap(update_gmail_draft)(
        service=service,
        user_google_email="user@example.com",
        draft_id="r-draft-1",
        subject=new_subject,
    )

    _, message = _updated(service)
    assert message["Subject"] == expected


@pytest.mark.asyncio
async def test_update_body_replaces_it_appends_the_signature_and_keeps_attachments():
    service = _service(_stored_reply_draft(), signature="<div>Best,<br>Example</div>")

    await _unwrap(update_gmail_draft)(
        service=service,
        user_google_email="user@example.com",
        draft_id="r-draft-1",
        body='<p>Shorter version.</p><img src="cid:ii_abc123">',
        body_format="html",
    )

    _, message = _updated(service)
    html_body = message.get_body(("html",)).get_content()
    assert "Shorter version." in html_body
    assert "Numbers attached." not in html_body
    assert "Best,<br>Example" in html_body
    parts = _leaf_parts(message)
    assert set(parts) == {"chart.png", "Q3 numbers.pdf"}


@pytest.mark.asyncio
async def test_update_adds_and_removes_attachments():
    service = _service(_stored_reply_draft())

    result = await _unwrap(update_gmail_draft)(
        service=service,
        user_google_email="user@example.com",
        draft_id="r-draft-1",
        attachments=[
            {
                "content": base64.b64encode(b"a,b\n1,2\n").decode(),
                "filename": "data.csv",
                "mime_type": "text/csv",
            }
        ],
        remove_attachments=["q3 NUMBERS.pdf"],
    )

    _, message = _updated(service)
    parts = _leaf_parts(message)
    assert set(parts) == {"chart.png", "data.csv"}
    assert parts["data.csv"].get_payload(decode=True) == b"a,b\n1,2\n"
    assert "added 1 attachment(s), removed 1 attachment(s)" in result


@pytest.mark.asyncio
async def test_update_refuses_to_remove_an_attachment_the_draft_does_not_have():
    service = _service(_stored_reply_draft())

    with pytest.raises(UserInputError, match="no attachment named 'budget.xlsx'"):
        await _unwrap(update_gmail_draft)(
            service=service,
            user_google_email="user@example.com",
            draft_id="r-draft-1",
            remove_attachments=["budget.xlsx"],
        )

    service.users.return_value.drafts.return_value.update.assert_not_called()


@pytest.mark.asyncio
async def test_update_carries_over_an_inline_image_another_client_named_loosely():
    service = _service(_stored_reply_draft(cid="image001.png$01DA=AB"))

    await _unwrap(update_gmail_draft)(
        service=service,
        user_google_email="user@example.com",
        draft_id="r-draft-1",
        subject="Quarterly numbers",
    )

    _, message = _updated(service)
    assert _leaf_parts(message)["chart.png"]["Content-ID"] == "<image001.png$01DA=AB>"


@pytest.mark.asyncio
async def test_update_never_saves_a_draft_that_lost_an_attachment(monkeypatch):
    service = _service(_stored_reply_draft())
    real_prepare = gmail_tools._prepare_gmail_message

    def drop_one(**kwargs):
        raw, thread_id, count, errors = real_prepare(**kwargs)
        return raw, thread_id, count - 1, ["chart.png: could not be attached"]

    monkeypatch.setattr(gmail_tools, "_prepare_gmail_message", drop_one)

    with pytest.raises(UserInputError, match="was not updated: 1 of its 2"):
        await _unwrap(update_gmail_draft)(
            service=service,
            user_google_email="user@example.com",
            draft_id="r-draft-1",
            to="alex@example.com",
        )

    service.users.return_value.drafts.return_value.update.assert_not_called()


@pytest.mark.asyncio
async def test_update_of_a_new_draft_sends_no_thread_id():
    service = _service(_stored_new_draft(), thread_id="own-thread")

    await _unwrap(update_gmail_draft)(
        service=service,
        user_google_email="user@example.com",
        draft_id="r-draft-1",
        body="Free on Friday instead?",
        include_signature=False,
    )

    kwargs, message = _updated(service)
    assert "threadId" not in kwargs["body"]["message"]
    assert message["Subject"] == "Lunch?"
    assert (
        message.get_body(("plain",)).get_content().strip() == "Free on Friday instead?"
    )
    service.users.return_value.settings.return_value.sendAs.return_value.list.assert_not_called()


@pytest.mark.asyncio
async def test_update_reads_the_stored_draft_raw():
    service = _service(_stored_new_draft())

    await _unwrap(update_gmail_draft)(
        service=service,
        user_google_email="user@example.com",
        draft_id="r-draft-1",
        subject="Lunch on Friday?",
    )

    service.users.return_value.drafts.return_value.get.assert_called_with(
        userId="me", id="r-draft-1", format="raw"
    )


@pytest.mark.asyncio
async def test_list_gmail_drafts_names_each_draft_and_its_message(monkeypatch):
    service = Mock()
    service.users.return_value.drafts.return_value.list.return_value.execute.return_value = {
        "drafts": [
            {"id": "r-1", "message": {"id": "m-1", "threadId": "t-1"}},
            {"id": "r-2", "message": {"id": "m-2", "threadId": "t-2"}},
        ],
        "nextPageToken": "next-page",
    }

    async def headers(_service, message_ids):
        assert message_ids == ["m-1", "m-2"]
        return {
            "m-1": {
                "To": "alex@example.com",
                "Cc": "sam@example.com",
                "Subject": "Re: Quarterly numbers",
                "Date": "Mon, 19 Oct 2026 10:00:00 +0100",
            },
            "m-2": None,
        }

    monkeypatch.setattr(gmail_tools, "_fetch_search_result_headers", headers)

    result = await _unwrap(list_gmail_drafts)(
        service=service,
        user_google_email="user@example.com",
        query="to:alex@example.com",
        page_size=5,
    )

    service.users.return_value.drafts.return_value.list.assert_called_with(
        userId="me", maxResults=5, q="to:alex@example.com"
    )
    assert "Found 2 drafts matching 'to:alex@example.com':" in result
    assert "1. Draft ID: r-1\n     Message ID: m-1\n     Thread ID: t-1" in result
    assert "To: alex@example.com" in result
    assert "Cc: sam@example.com" in result
    assert "Subject: Re: Quarterly numbers" in result
    assert "2. Draft ID: r-2" in result
    assert "Headers: unavailable" in result
    assert "page_token='next-page'" in result


@pytest.mark.asyncio
async def test_list_gmail_drafts_with_none():
    service = Mock()
    service.users.return_value.drafts.return_value.list.return_value.execute.return_value = {}

    result = await _unwrap(list_gmail_drafts)(
        service=service, user_google_email="user@example.com"
    )

    assert result == "No drafts found."


@pytest.mark.asyncio
async def test_send_gmail_draft_sends_it_by_id():
    service = Mock()
    service.users.return_value.drafts.return_value.send.return_value.execute.return_value = {
        "id": "sent-1",
        "threadId": "thread-1",
        "labelIds": ["SENT"],
    }

    result = await _unwrap(send_gmail_draft)(
        service=service, user_google_email="user@example.com", draft_id="r-draft-1"
    )

    service.users.return_value.drafts.return_value.send.assert_called_with(
        userId="me", body={"id": "r-draft-1"}
    )
    assert result == "Draft sent! Message ID: sent-1\nThread ID: thread-1"


def test_send_gmail_draft_belongs_to_the_send_permission_level():
    assert _required_scopes(send_gmail_draft) == {GMAIL_COMPOSE_SCOPE, GMAIL_SEND_SCOPE}
    assert _required_scopes(update_gmail_draft) == {GMAIL_COMPOSE_SCOPE}
    assert _required_scopes(delete_gmail_draft) == {GMAIL_COMPOSE_SCOPE}


@pytest.mark.asyncio
async def test_delete_gmail_draft_deletes_it_by_id():
    service = Mock()

    result = await _unwrap(delete_gmail_draft)(
        service=service, user_google_email="user@example.com", draft_id="r-draft-1"
    )

    service.users.return_value.drafts.return_value.delete.assert_called_with(
        userId="me", id="r-draft-1"
    )
    assert result == "Draft r-draft-1 deleted."
