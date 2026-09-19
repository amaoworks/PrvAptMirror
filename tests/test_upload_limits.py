import asyncio

import pytest
from starlette.requests import Request
from starlette.exceptions import HTTPException

from prvaptmirror.uploads import bounded_upload_form, MULTIPART_OVERHEAD_BYTES


@pytest.mark.parametrize("declared_length", [None, "20"])
def test_chunked_or_false_length_cannot_bypass_limit(declared_length, monkeypatch):
    import starlette.formparsers
    import tempfile

    opened = []
    consumed = []

    def tracked_file(*args, **kwargs):
        file = tempfile.SpooledTemporaryFile(*args, **kwargs)
        opened.append(file)
        return file

    monkeypatch.setattr(starlette.formparsers, "SpooledTemporaryFile", tracked_file)
    prefix = b'--test\r\nContent-Disposition: form-data; name="files"; filename="x.deb"\r\n\r\n'
    chunks = [prefix, b"x" * 1024, b"x" * (MULTIPART_OVERHEAD_BYTES + 1024), b"never read"]

    async def receive():
        consumed.append(True)
        return {"type": "http.request", "body": chunks.pop(0), "more_body": bool(chunks)}

    headers = [(b"content-type", b"multipart/form-data; boundary=test")]
    if declared_length:
        headers.append((b"content-length", declared_length.encode()))
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": headers, "app": object()}, receive)

    async def parse():
        async with bounded_upload_form(request, limit=1024, max_files=1):
            pytest.fail("oversized body accepted")

    with pytest.raises(HTTPException) as error:
        asyncio.run(parse())
    assert error.value.status_code == 413
    assert len(consumed) == 3
    assert opened and all(file.closed for file in opened)
