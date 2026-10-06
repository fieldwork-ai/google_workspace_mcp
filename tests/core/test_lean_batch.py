"""The lean batch parser delivers what the stock one does, without its copies.

Runs a real Gmail service object against a fake transport that answers the
batch endpoint with a multipart response, and compares every callback against
googleapiclient's own BatchHttpRequest.
"""

import json
import re
import tracemalloc

import httplib2
import pytest
from googleapiclient.discovery import build
from googleapiclient.errors import BatchError, HttpError
from googleapiclient.http import BatchHttpRequest

from core.lean_batch import LeanBatchHttpRequest, new_lean_batch

BOUNDARY = "batch_abc"


def _part(content_id, status_line, body, newline="\r\n"):
    return (
        f"--{BOUNDARY}{newline}Content-Type: application/http{newline}"
        f"Content-ID: <response-{content_id}>{newline}{newline}"
        f"{status_line}{newline}Content-Type: application/json; charset=UTF-8{newline}{newline}"
        f"{body}{newline}"
    )


class _Transport:
    """Answers each batch POST from a function of (request ids in order, call number)."""

    def __init__(self, answer):
        self._answer = answer
        self.calls = 0

    def request(self, uri, method="GET", body=None, headers=None, **_kwargs):
        self.calls += 1
        content_ids = re.findall(r"Content-ID: <([^>]+)>", body)
        parts = self._answer(content_ids, self.calls)
        content = ("".join(parts) + f"--{BOUNDARY}--").encode()
        resp = httplib2.Response(
            {"status": "200", "content-type": f'multipart/mixed; boundary="{BOUNDARY}"'}
        )
        return resp, content


def _thread(tid):
    return {"id": tid, "messages": [{"id": f"{tid}-m", "snippet": "héllo, ünïcode"}]}


def _mixed_answer(content_ids, _call):
    """t0 and t2 succeed, t1 is missing, t3 is rate limited."""
    parts = []
    for cid in content_ids:
        tid = cid.rsplit("+", 1)[1].strip()
        if tid == "t1":
            parts.append(
                _part(
                    cid,
                    "HTTP/1.1 404 Not Found",
                    json.dumps(
                        {
                            "error": {
                                "code": 404,
                                "message": "Requested entity was not found.",
                            }
                        }
                    ),
                )
            )
        elif tid == "t3":
            parts.append(
                _part(
                    cid,
                    "HTTP/1.1 429 Too Many Requests",
                    json.dumps(
                        {
                            "error": {
                                "code": 429,
                                "errors": [{"reason": "rateLimitExceeded"}],
                            }
                        }
                    ),
                )
            )
        else:
            parts.append(_part(cid, "HTTP/1.1 200 OK", json.dumps(_thread(tid))))
    return parts


def _run(batch_factory, answer, ids=("t0", "t1", "t2", "t3")):
    transport = _Transport(answer)
    service = build("gmail", "v1", http=transport, static_discovery=True)
    results = {}

    def callback(request_id, response, exception):
        results[request_id] = (
            response,
            None
            if exception is None
            else (type(exception), exception.resp.status, exception.content),
        )

    batch = batch_factory(service, callback)
    for tid in ids:
        batch.add(
            service.users().threads().get(userId="me", id=tid, format="full"),
            request_id=tid,
        )
    batch.execute()
    return results, transport, batch


def _stock(service, callback):
    return service.new_batch_http_request(callback=callback)


def test_delivers_exactly_what_the_stock_parser_delivers():
    stock, _, _ = _run(_stock, _mixed_answer)
    lean, _, batch = _run(new_lean_batch, _mixed_answer)

    assert type(batch) is LeanBatchHttpRequest
    assert lean == stock
    assert lean["t0"][0] == _thread("t0")
    assert lean["t1"][1][:2] == (HttpError, 404)
    assert lean["t3"][1][:2] == (HttpError, 429)
    # HttpError needs bytes, to read the reason out of the error body.
    assert isinstance(lean["t3"][1][2], bytes)


def test_reads_parts_whose_lines_end_in_bare_newlines():
    def answer(content_ids, _call):
        return [
            _part(
                cid,
                "HTTP/1.1 200 OK",
                json.dumps(_thread(cid.rsplit("+", 1)[1].strip())),
                newline="\n",
            )
            for cid in content_ids
        ]

    lean, _, _ = _run(new_lean_batch, answer, ids=("t0", "t1"))
    assert lean["t0"][0] == _thread("t0")
    assert lean["t1"][0] == _thread("t1")


def test_resends_unauthorized_parts_as_the_library_does():
    def answer(content_ids, call):
        status = "HTTP/1.1 401 Unauthorized" if call == 1 else "HTTP/1.1 200 OK"
        return [
            _part(cid, status, json.dumps(_thread(cid.rsplit("+", 1)[1].strip())))
            for cid in content_ids
        ]

    lean, transport, _ = _run(new_lean_batch, answer, ids=("t0", "t1"))
    assert transport.calls == 2
    assert lean["t0"][0] == _thread("t0") and lean["t1"][0] == _thread("t1")


def test_a_failed_batch_request_raises_like_the_library():
    class Failing:
        def request(self, *_args, **_kwargs):
            return httplib2.Response({"status": "503"}), b"unavailable"

    service = build("gmail", "v1", http=Failing(), static_discovery=True)
    batch = new_lean_batch(service, lambda *_: None)
    batch.add(service.users().threads().get(userId="me", id="t0"), request_id="t0")
    with pytest.raises(HttpError):
        batch.execute()


def test_a_response_that_is_not_multipart_is_a_batch_error():
    class NotMultipart:
        def request(self, *_args, **_kwargs):
            return httplib2.Response(
                {"status": "200", "content-type": "application/json"}
            ), b"{}"

    service = build("gmail", "v1", http=NotMultipart(), static_discovery=True)
    batch = new_lean_batch(service, lambda *_: None)
    batch.add(service.users().threads().get(userId="me", id="t0"), request_id="t0")
    with pytest.raises(BatchError):
        batch.execute()


def test_leaves_anything_but_the_library_batch_alone():
    sentinel = object()

    class Service:
        def new_batch_http_request(self, callback):
            return sentinel

    assert new_lean_batch(Service(), lambda *_: None) is sentinel


def test_parsing_allocates_a_fraction_of_the_response_where_the_stock_parser_copies_it():
    body = json.dumps(
        {"id": "t", "messages": [{"payload": {"body": {"data": "x" * 400_000}}}]}
    )

    def answer(content_ids, _call):
        return [_part(cid, "HTTP/1.1 200 OK", body) for cid in content_ids]

    def peak(factory):
        transport = _Transport(answer)
        service = build("gmail", "v1", http=transport, static_discovery=True)
        batch = factory(service, lambda *_: None)
        for i in range(10):
            batch.add(
                service.users().threads().get(userId="me", id=f"t{i}"),
                request_id=f"t{i}",
            )
        # Build the response before measuring, so only parsing is counted.
        batch._base_id = "fixed"
        response = transport.request(
            None, body="".join(f"Content-ID: <fixed + t{i}>" for i in range(10))
        )
        transport.request = lambda *_args, **_kwargs: response
        tracemalloc.start()
        try:
            batch.execute()
            return tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

    response_size = 10 * len(body)
    assert peak(_stock) > 3 * response_size
    assert peak(new_lean_batch) < response_size / 4


def test_the_subclass_is_still_a_batch_http_request():
    assert issubclass(LeanBatchHttpRequest, BatchHttpRequest)
