"""A hosted server reads no local files, and its tools say so.

A caller's workspace is another machine, so a path from it can never resolve
here; an agent that passed one was told "Path does not exist" and had to find
the working route by trial and error. On a hosted server every local-file read
is refused with directions to base64, and the schemas lead with base64 and the
way to get a workspace file into it.
"""

import pytest

import gdrive.drive_tools  # noqa: F401
import gmail.gmail_tools  # noqa: F401
from core.server import server
from core.utils import HOSTED_LOCAL_FILE_REFUSAL, UserInputError, validate_file_path

WORKSPACE_ROUTE = "`fieldwork tool run <tool> --args -`"


@pytest.fixture
def hosted(monkeypatch):
    monkeypatch.setattr("auth.oauth_config.is_stateless_mode", lambda: True)


def test_a_hosted_server_refuses_every_local_path_alike(hosted, tmp_path):
    existing = tmp_path / "report.pdf"
    existing.write_bytes(b"%PDF")
    refusals = []
    for path in (
        str(existing),
        "/home/fieldwork/conversations/c/drive/Draft.pdf",
        "/proc/self/environ",
    ):
        with pytest.raises(UserInputError) as refused:
            validate_file_path(path)
        refusals.append(str(refused.value))
    # The same answer whether or not the path exists: nothing about this disk leaks.
    assert refusals == [HOSTED_LOCAL_FILE_REFUSAL] * 3
    assert WORKSPACE_ROUTE in HOSTED_LOCAL_FILE_REFUSAL


def test_a_server_that_is_not_hosted_still_reads_its_permitted_files(
    monkeypatch, tmp_path
):
    monkeypatch.setattr("auth.oauth_config.is_stateless_mode", lambda: False)
    monkeypatch.setenv("ALLOWED_FILE_DIRS", str(tmp_path))
    allowed = tmp_path / "report.pdf"
    allowed.write_bytes(b"%PDF")
    assert validate_file_path(str(allowed)) == allowed.resolve()


async def _schemas():
    return {
        tool.name: tool.parameters["properties"] for tool in await server.list_tools()
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["send_gmail_message", "draft_gmail_message"])
async def test_gmail_attachments_lead_with_base64_and_the_workspace_route(tool_name):
    description = (await _schemas())[tool_name]["attachments"]["description"]
    assert (
        description.index("content")
        < description.index("url")
        < description.index("path")
    )
    assert "not available on a hosted server" in description
    assert WORKSPACE_ROUTE in description
    # These return no URL on a hosted server; pointing at them was a dead end.
    assert "get_drive_file_download_url" not in description


@pytest.mark.asyncio
async def test_drive_base64_content_names_the_workspace_route():
    schema = (await _schemas())["create_drive_file"]
    assert WORKSPACE_ROUTE in schema["base64_content"]["description"]
    assert "not available on a hosted server" in schema["fileUrl"]["description"]


@pytest.mark.asyncio
async def test_sending_with_a_workspace_path_is_refused_with_the_route_and_sends_nothing(
    hosted,
):
    from unittest.mock import Mock

    from gmail.gmail_tools import send_gmail_message

    fn = (
        send_gmail_message.fn
        if hasattr(send_gmail_message, "fn")
        else send_gmail_message
    )
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    service = Mock()

    with pytest.raises(UserInputError) as refused:
        await fn(
            service=service,
            user_google_email="tom@example.com",
            to="daniel@example.com",
            subject="Options",
            body="Attached.",
            attachments=[
                {"path": "/home/fieldwork/conversations/c/drive/Draft Options.pdf"}
            ],
            include_signature=False,
        )

    assert HOSTED_LOCAL_FILE_REFUSAL in str(refused.value)
    assert "Path does not exist" not in str(refused.value)
    service.users().messages().send.assert_not_called()
