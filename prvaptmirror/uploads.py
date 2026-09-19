"""Bound request bodies before handing bytes to the multipart parser."""

from contextlib import asynccontextmanager

from starlette.exceptions import HTTPException
from starlette.formparsers import MultiPartException
from starlette.requests import Request

# File bytes are checked separately; this bounds multipart headers and fields.
MULTIPART_OVERHEAD_BYTES = 64 * 1024


@asynccontextmanager
async def bounded_upload_form(request: Request, *, limit: int, max_files: int):
    body_limit = limit + MULTIPART_OVERHEAD_BYTES
    raw_length = request.headers.get("content-length")
    if raw_length is not None:
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise HTTPException(400, "Invalid Content-Length") from exc
        if length < 0:
            raise HTTPException(400, "Invalid Content-Length")
        if length > body_limit:
            raise HTTPException(413, "上传超过大小限制")
    received = 0
    exceeded = False

    async def receive():
        nonlocal received, exceeded
        message = await request.receive()
        if message["type"] == "http.disconnect":
            raise MultiPartException("Upload disconnected")
        if message["type"] == "http.request":
            received += len(message.get("body", b""))
            if received > body_limit:
                exceeded = True
                # Starlette cleans up partial spooled files for this exception.
                raise MultiPartException("上传超过大小限制")
        return message

    bounded = Request(request.scope, receive=receive)
    try:
        async with bounded.form(max_files=max_files, max_fields=4) as form:
            yield form
    except HTTPException:
        if exceeded:
            raise HTTPException(413, "上传超过大小限制") from None
        raise
