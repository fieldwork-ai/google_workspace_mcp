"""A Google API batch request that parses its response without copying it.

The stock ``BatchHttpRequest._execute`` decodes the whole multipart response to
text, concatenates a header onto it, parses it into a MIME tree, and splits each
part's body out again: roughly six copies of the response alive at once. A batch
of 25 full Gmail threads is tens of megabytes, so a few concurrent batch reads
exhausted the hosted server's memory and took it down for every account.

This splits the response bytes at the multipart boundary and hands each
successful body over as a view into the one response buffer, decoded only when
its callback runs. Building and sending the request, credential refresh and the
401 redo in ``execute`` are the library's own.
"""

import io
from email.generator import Generator
from email.mime.multipart import MIMEMultipart
from email.mime.nonmultipart import MIMENonMultipart

import httplib2
from googleapiclient.errors import BatchError, HttpError
from googleapiclient.http import BatchHttpRequest


class LeanBatchHttpRequest(BatchHttpRequest):
    def _execute(self, http, order, requests):
        # Request serialization as in BatchHttpRequest._execute.
        message = MIMEMultipart("mixed")
        setattr(message, "_write_headers", lambda self: None)
        for request_id in order:
            request = requests[request_id]
            msg = MIMENonMultipart("application", "http")
            msg["Content-Transfer-Encoding"] = "binary"
            msg["Content-ID"] = self._id_to_header(request_id)
            msg.set_payload(self._serialize_request(request))
            message.attach(msg)
        fp = io.StringIO()
        Generator(fp, mangle_from_=False).flatten(message, unixfrom=False)
        headers = {
            "content-type": 'multipart/mixed; boundary="%s"' % message.get_boundary()
        }

        resp, content = http.request(
            self._batch_uri, method="POST", body=fp.getvalue(), headers=headers
        )
        if resp.status >= 300:
            raise HttpError(resp, content, uri=self._batch_uri)
        for request_id, part in _split_multipart(resp, content):
            self._responses[self._header_to_id(request_id)] = part


class _BodyView:
    """A part's body, read from the response buffer when it is deserialized.

    ``JsonModel.deserialize`` calls ``decode`` and nothing else, so one part at
    a time becomes text rather than all of them at once.
    """

    __slots__ = ("_view",)

    def __init__(self, view: memoryview):
        self._view = view

    def decode(self, encoding="utf-8", errors="strict") -> str:
        return str(self._view, encoding, errors)


def _boundary(resp) -> bytes:
    for param in resp.get("content-type", "").split(";")[1:]:
        name, _, value = param.strip().partition("=")
        if name.lower() == "boundary" and value:
            return value.strip('"').encode("ascii")
    raise BatchError("Response not in multipart/mixed format.", resp=resp)


def _header_block(content: bytes, start: int, end: int) -> int:
    """Index just past the blank line ending the header block at ``start``."""
    for terminator in (b"\r\n\r\n", b"\n\n"):
        index = content.find(terminator, start, end)
        if index != -1:
            return index + len(terminator)
    raise BatchError("Malformed part in batch response.")


def _parse_headers(lines: list) -> dict:
    headers = {}
    for line in lines:
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip()
    return headers


def _split_multipart(resp, content: bytes):
    """Yield (Content-ID, (httplib2.Response, body)) for each part."""
    buffer = memoryview(content)
    delimiter = b"--" + _boundary(resp)
    position = content.find(delimiter)
    if position == -1:
        raise BatchError(
            "Response not in multipart/mixed format.", resp=resp, content=content
        )
    while True:
        start = position + len(delimiter)
        if content.startswith(b"--", start):
            return
        end = content.find(delimiter, start)
        if end == -1:
            raise BatchError("Unterminated batch response.", resp=resp)

        part_body = _header_block(content, start, end)
        part_headers = _parse_headers(
            content[start:part_body].decode("utf-8", "replace").splitlines()
        )
        http_body = _header_block(content, part_body, end)
        status_line, *header_lines = (
            content[part_body:http_body].decode("utf-8", "replace").splitlines()
        )
        protocol, status, reason = (status_line.split(" ", 2) + [""])[:3]
        info = _parse_headers(header_lines)
        info["status"] = status
        response = httplib2.Response(info)
        response.reason = reason
        response.version = int(protocol.split("/", 1)[1].replace(".", ""))

        # The line break before a delimiter belongs to the delimiter (RFC 2046).
        body_end = end - 2 if content[end - 2 : end] == b"\r\n" else end
        body = buffer[http_body:body_end]
        # HttpError requires bytes, and an error body is small.
        yield (
            part_headers.get("content-id", ""),
            (response, bytes(body) if response.status >= 300 else _BodyView(body)),
        )
        position = end


def new_lean_batch(service, callback):
    """``service.new_batch_http_request`` with the lean response parser."""
    batch = service.new_batch_http_request(callback=callback)
    if type(batch) is BatchHttpRequest:
        # Same object and state; only the response parsing changes.
        batch.__class__ = LeanBatchHttpRequest
    return batch
