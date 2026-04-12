from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
import os
import sys
import time
from typing import Any

from aiohttp import web
import mcp.server.stdio
import mcp.types as types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.session import ServerSession


def bridge_host() -> str:
    return os.environ.get("CODEX_BRIDGE_HOST", "127.0.0.1")


def bridge_port() -> int:
    return int(os.environ.get("CODEX_BRIDGE_PORT", "8788"))


def normalize_message(message: str) -> str:
    return " ".join(message.strip().split())


def text_result(text: str, *, is_error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        isError=is_error,
    )


@dataclass
class PendingReply:
    message_id: str
    created_at: float
    normalized_message: str | None = None
    reply: str | None = None
    failure_reason: str | None = None
    event: asyncio.Event = field(default_factory=asyncio.Event)


class BridgeState:
    def __init__(self, max_pending_reply_ms: int = 10 * 60 * 1000) -> None:
        self.max_pending_reply_ms = max_pending_reply_ms
        self._lock = asyncio.Lock()
        self._seq = 0
        self._pending_replies: dict[str, PendingReply] = {}
        self._inflight_by_message: dict[str, str] = {}
        self._pending_for_codex: list[dict[str, str]] = []

    def next_id(self, prefix: str = "m") -> str:
        self._seq += 1
        return f"{prefix}{int(time.time() * 1000)}-{self._seq}"

    async def prune_expired(self) -> None:
        async with self._lock:
            now_ms = time.time() * 1000
            expired = [
                reply_id
                for reply_id, pending in self._pending_replies.items()
                if now_ms - (pending.created_at * 1000) > self.max_pending_reply_ms
            ]
            for reply_id in expired:
                pending = self._pending_replies.pop(reply_id, None)
                if not pending:
                    continue
                if pending.normalized_message:
                    self._inflight_by_message.pop(pending.normalized_message, None)
                pending.event.set()

    async def create_or_get_codex_request(self, message: str) -> str:
        normalized = normalize_message(message)
        async with self._lock:
            existing_id = self._inflight_by_message.get(normalized)
            if existing_id and existing_id in self._pending_replies:
                return existing_id

            request_id = self.next_id("codex-")
            self._pending_replies[request_id] = PendingReply(
                message_id=request_id,
                created_at=time.time(),
                normalized_message=normalized,
            )
            self._inflight_by_message[normalized] = request_id
            return request_id

    async def fail_codex_request(self, message_id: str, reason: str | None = None) -> None:
        async with self._lock:
            pending = self._pending_replies.get(message_id)
            if not pending:
                return
            if pending.normalized_message:
                self._inflight_by_message.pop(pending.normalized_message, None)
            pending.failure_reason = reason or "bridge request failed"
            pending.event.set()

    async def resolve_codex_reply(self, reply_to: str | None, text: str) -> bool:
        if not reply_to:
            async with self._lock:
                unresolved = [
                    pending
                    for pending in self._pending_replies.values()
                    if pending.reply is None
                ]
                if len(unresolved) != 1:
                    return False
                pending = unresolved[0]
                pending.reply = text
                if pending.normalized_message:
                    self._inflight_by_message.pop(pending.normalized_message, None)
                pending.event.set()
                return True
        async with self._lock:
            pending = self._pending_replies.get(reply_to)
            if not pending or pending.reply is not None:
                return False
            pending.reply = text
            if pending.normalized_message:
                self._inflight_by_message.pop(pending.normalized_message, None)
            pending.event.set()
            return True

    async def wait_for_reply(self, message_id: str, timeout_ms: int) -> dict[str, str | None] | None:
        async with self._lock:
            pending = self._pending_replies.get(message_id)
            if not pending:
                return None
            if pending.reply is not None:
                self._pending_replies.pop(message_id, None)
                return {"reply": pending.reply, "failure_reason": pending.failure_reason}
            if pending.failure_reason is not None:
                self._pending_replies.pop(message_id, None)
                return {"reply": pending.reply, "failure_reason": pending.failure_reason}
            event = pending.event

        try:
            await asyncio.wait_for(event.wait(), timeout=timeout_ms / 1000)
        except TimeoutError:
            return None

        async with self._lock:
            pending = self._pending_replies.pop(message_id, None)
            if not pending:
                return None
            return {"reply": pending.reply, "failure_reason": pending.failure_reason}

    async def queue_for_codex(self, text: str, *, prefix: str = "claude-init-") -> str:
        async with self._lock:
            message_id = self.next_id(prefix)
            self._pending_for_codex.append({"id": message_id, "text": text})
            return message_id

    async def pop_pending_for_codex(self) -> list[dict[str, str]]:
        async with self._lock:
            messages = list(self._pending_for_codex)
            self._pending_for_codex.clear()
            return messages


class ClaudeChannelRelay:
    def __init__(self) -> None:
        self._session: ServerSession | None = None

    async def attach(self, session: ServerSession) -> None:
        self._session = session

    async def capability_summary(self) -> dict[str, Any]:
        session = self._session
        if session is None:
            return {
                "claude_session_attached": False,
            }
        return {
            "claude_session_attached": True,
        }

    async def notify_claude(self, message_id: str, text: str, sender: str) -> None:
        session = self._session
        if session is None:
            raise RuntimeError("Claude session is not attached yet.")

        await session.send_notification(
            types.Notification[dict[str, Any], str](
                method="notifications/claude/channel",
                params={
                    "content": text,
                    "meta": {
                        "chat_id": "nowonbun-claude-bridge",
                        "message_id": message_id,
                        "sender": sender,
                    },
                },
            )
        )


class ClaudeBridgeApp:
    def __init__(self) -> None:
        self.state = BridgeState()
        self.relay = ClaudeChannelRelay()

    def make_web_app(self) -> web.Application:
        app = web.Application()
        app.add_routes(
            [
                web.post("/api/from-codex", self.handle_from_codex),
                web.get("/api/poll-reply/{message_id}", self.handle_poll_reply),
                web.get("/api/pending-for-codex", self.handle_pending_for_codex),
                web.get("/api/health", self.handle_health),
            ]
        )
        return app

    @staticmethod
    def normalize_failure_reason(exc: Exception) -> str:
        return f"{type(exc).__name__}: {exc}"

    async def handle_from_codex(self, request: web.Request) -> web.Response:
        await self.state.prune_expired()
        body = await request.json()
        message = str(body.get("message", "")).strip()
        if not message:
            return web.Response(text="missing message", status=400)

        message_id = await self.state.create_or_get_codex_request(message)
        try:
            print(
                f"[bridge] dispatch start message_id={message_id} chars={len(message)}",
                file=sys.stderr,
                flush=True,
            )
            await self.relay.notify_claude(message_id, message, "codex")
            print(
                f"[bridge] dispatch delivered message_id={message_id}",
                file=sys.stderr,
                flush=True,
            )
            return web.json_response({"id": message_id})
        except Exception as exc:
            reason = self.normalize_failure_reason(exc)
            print(
                f"[bridge] dispatch failure message_id={message_id} reason={reason}",
                file=sys.stderr,
                flush=True,
            )
            await self.state.fail_codex_request(message_id, reason)
            return web.Response(text=reason, status=502)

    async def handle_poll_reply(self, request: web.Request) -> web.Response:
        await self.state.prune_expired()
        message_id = request.match_info["message_id"]
        timeout_ms = int(request.query.get("timeout", "120000"))
        result = await self.state.wait_for_reply(message_id, timeout_ms)
        if result is None:
            return web.json_response({"timeout": True, "reply": None})
        return web.json_response(
            {
                "timeout": False,
                "reply": result.get("reply"),
                "failure_reason": result.get("failure_reason"),
            }
        )

    async def handle_pending_for_codex(self, _: web.Request) -> web.Response:
        await self.state.prune_expired()
        return web.json_response({"messages": await self.state.pop_pending_for_codex()})

    async def handle_health(self, _: web.Request) -> web.Response:
        capability_summary = await self.relay.capability_summary()
        return web.json_response({"status": "ok", **capability_summary})


def build_server(app: ClaudeBridgeApp) -> Server:
    server = Server("nowonbun-claude-bridge")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        with suppress(LookupError):
            await app.relay.attach(server.request_context.session)
        return [
            types.Tool(
                name="reply",
                description=(
                    "Send a reply to Codex through the bridge. "
                    "Always pass reply_to with the original message_id."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "reply_to": {"type": "string"},
                    },
                    "required": ["text"],
                },
            ),
            types.Tool(
                name="send_to_codex",
                description="Queue a proactive message for Codex.",
                inputSchema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        with suppress(LookupError):
            await app.relay.attach(server.request_context.session)

        if name == "reply":
            text = str(arguments.get("text", "")).strip()
            reply_to = str(arguments.get("reply_to", "")).strip() or None
            if not text:
                return text_result("error: empty text", is_error=True)
            resolved = await app.state.resolve_codex_reply(reply_to, text)
            if not resolved:
                return text_result(
                    "error: could not match pending Codex request; pass reply_to or keep only one pending request",
                    is_error=True,
                )
            return text_result("sent")

        if name == "send_to_codex":
            text = str(arguments.get("text", "")).strip()
            if not text:
                return text_result("error: empty text", is_error=True)
            message_id = await app.state.queue_for_codex(text)
            return text_result(f"queued for codex ({message_id})")

        return text_result(f"unknown tool: {name}", is_error=True)

    return server


async def run() -> None:
    app = ClaudeBridgeApp()
    web_app = app.make_web_app()
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, bridge_host(), bridge_port())
    await site.start()

    server = build_server(app)
    print(f"claude-bridge http://{bridge_host()}:{bridge_port()}", file=sys.stderr, flush=True)
    try:
        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                InitializationOptions(
                    server_name="nowonbun-claude-bridge",
                    server_version="0.1.0",
                    capabilities=server.get_capabilities(
                        notification_options=NotificationOptions(),
                        experimental_capabilities={"claude/channel": {}},
                    ),
                    instructions=(
                        "You are connected to nowonbun-claude-bridge. "
                        "When Codex sends a message through Claude Code Channels, "
                        "answer with the reply tool and always set reply_to to the incoming message_id."
                    ),
                ),
            )
    finally:
        await runner.cleanup()


def main() -> None:
    import asyncio

    asyncio.run(run())


if __name__ == "__main__":
    main()
