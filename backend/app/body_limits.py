"""Ограничение тела сигналинга ДО JSON/Pydantic, в том числе chunked-запроса."""
from fastapi import HTTPException
from starlette.responses import JSONResponse

CALL_BODY_LIMIT = 96 * 1024


class CallBodyLimitMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] != "http" or scope.get("method") != "POST" or not (path == "/calls" or path.startswith("/calls/")):
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        try:
            length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            await JSONResponse({"detail": "Некорректная длина запроса"}, status_code=400)(scope, receive, send)
            return
        if length > CALL_BODY_LIMIT:
            await JSONResponse({"detail": "Слишком большой запрос соединения"}, status_code=413)(scope, receive, send)
            return
        received = 0

        async def bounded_receive():
            nonlocal received
            message = await receive()
            received += len(message.get("body", b""))
            if received > CALL_BODY_LIMIT:
                raise HTTPException(413, "Слишком большой запрос соединения")
            return message

        await self.app(scope, bounded_receive, send)
