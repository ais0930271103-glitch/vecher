"""Body bounds before JSON/Pydantic, including streamed uploads and chunked JSON."""
import re

from fastapi import HTTPException
from starlette.responses import JSONResponse

from .config import get_settings

CALL_BODY_LIMIT = 96 * 1024
BLOG_JSON_BODY_LIMIT = 32 * 1024
_MEDIA_UPLOAD = re.compile(r"^/blogs/[^/]+/media/?$")


class CallBodyLimitMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        method = scope.get("method")
        call = method == "POST" and (path == "/calls" or path.startswith("/calls/"))
        blog = method in {"POST", "PUT", "PATCH", "DELETE"} and (path == "/blogs" or path.startswith("/blogs/"))
        content = method in {"POST", "PUT", "PATCH", "DELETE"} and (path == "/content" or path.startswith("/content/"))
        if scope["type"] != "http" or not (call or blog or content):
            await self.app(scope, receive, send)
            return
        limit = (CALL_BODY_LIMIT if call else get_settings().blog_media_max_bytes
                 if method == "POST" and _MEDIA_UPLOAD.fullmatch(path) else BLOG_JSON_BODY_LIMIT)
        detail = "Слишком большой запрос соединения" if call else "request_body_limit"
        headers = dict(scope.get("headers", []))
        try:
            length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            await JSONResponse({"detail": "Некорректная длина запроса"}, status_code=400)(scope, receive, send)
            return
        if length < 0:
            await JSONResponse({"detail": "Некорректная длина запроса"}, status_code=400)(scope, receive, send)
            return
        if length > limit:
            await JSONResponse({"detail": detail}, status_code=413)(scope, receive, send)
            return
        received = 0

        async def bounded_receive():
            nonlocal received
            message = await receive()
            received += len(message.get("body", b""))
            if received > limit:
                raise HTTPException(413, detail)
            return message

        await self.app(scope, bounded_receive, send)
