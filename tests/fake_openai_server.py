"""A scripted OpenAI-compatible server.

Lives in ``tests/`` so production code can never reach it. It speaks just enough
of the wire format to exercise the adapter honestly: model discovery, chat
completions, error codes, malformed payloads, slow responses and redirects.

Run it for local development:

    .venv/bin/python -m tests.fake_openai_server --port 8123
    # base_url for the admin panel: http://127.0.0.1:8123/v1
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

class FakeOpenAIServer:
    """ASGI app with scriptable behaviour per route."""

    def __init__(
        self,
        *,
        models: tuple[dict[str, Any], ...] | None = None,
        completion: str = "<!DOCTYPE html><html><body>built</body></html>",
        finish_reason: str = "stop",
        model_name: str = "fake-1",
        status: int = 200,
        error_body: str = "",
        raw_body: str | None = None,
        delay_s: float = 0.0,
        redirect_to: str | None = None,
        require_bearer: str | None = None,
    ) -> None:
        self.models = models or (
            {"id": "fake-1", "name": "Fake One"},
            {"id": "fake-2"},
        )
        self.completion = completion
        self.finish_reason = finish_reason
        self.model_name = model_name
        self.status = status
        self.error_body = error_body
        self.raw_body = raw_body
        self.delay_s = delay_s
        self.redirect_to = redirect_to
        self.require_bearer = require_bearer
        self.requests: list[dict] = []

    async def __call__(self, scope, receive, send) -> None:  # noqa: ANN001
        path = scope["path"].rstrip("/")
        body = await _read_body(receive)
        self.requests.append(
            {
                "method": scope["method"],
                "path": path,
                "headers": dict(_headers(scope)),
                "body": body,
            }
        )

        if scope["method"] != "POST" and not path.endswith("/models"):
            await _respond(send, 405, {"error": "method not allowed"})
            return

        if self.require_bearer:
            auth = self.requests[-1]["headers"].get("authorization", "")
            if auth != f"Bearer {self.require_bearer}":
                await _respond(send, 401, {"error": {"message": "bad credentials"}})
                return

        if self.delay_s:
            await asyncio.sleep(self.delay_s)

        # Scripted failure modes come before route defaults, so an error status
        # or a malformed body can be aimed at either endpoint.
        if self.redirect_to:
            await _respond(
                send,
                307,
                {"redirecting": True},
                extra_headers=[(b"location", self.redirect_to.encode())],
            )
            return

        if self.status != 200:
            await _respond(
                send,
                self.status,
                {"error": {"message": self.error_body or "server error"}},
            )
            return

        if self.raw_body is not None:
            await _respond_raw(send, 200, self.raw_body)
            return

        if path.endswith("/models"):
            await _respond(send, 200, {"object": "list", "data": list(self.models)})
            return

        await _respond(
            send,
            200,
            {
                "id": "chatcmpl-fake",
                "object": "chat.completion",
                "model": self.model_name,
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": self.finish_reason,
                        "message": {"role": "assistant", "content": self.completion},
                    }
                ],
                "usage": {"prompt_tokens": 11, "completion_tokens": 22, "total_tokens": 33},
            },
        )


async def _read_body(receive) -> Any:  # noqa: ANN001
    message = await receive()
    raw = message.get("body") or b""
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return {}


def _headers(scope) -> Any:  # noqa: ANN001
    return {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}


async def _respond(send, status: int, payload: dict, extra_headers=None) -> None:  # noqa: ANN001
    body = json.dumps(payload).encode()
    await _respond_raw(send, status, body.decode(), extra_headers)


async def _respond_raw(send, status: int, body_text: str, extra_headers=None) -> None:  # noqa: ANN001
    body = body_text.encode() if isinstance(body_text, str) else body_text
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
    ]
    if extra_headers:
        headers.extend(extra_headers)
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


if __name__ == "__main__":  # pragma: no cover - developer tool
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="Fake OpenAI-compatible endpoint")
    parser.add_argument("--port", type=int, default=8123)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--delay", type=float, default=0.0, help="slow responses, for timeout tests")
    parser.add_argument("--status", type=int, default=200, help="force an error status")
    args = parser.parse_args()

    server = FakeOpenAIServer(delay_s=args.delay, status=args.status)
    uvicorn.run(server, host=args.host, port=args.port, log_level="warning")
