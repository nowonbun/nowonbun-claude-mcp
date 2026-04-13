from __future__ import annotations

import asyncio
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any

from aiohttp import ClientSession, web
import mcp.server.stdio
import mcp.types as types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions


def bridge_host() -> str:
    return os.environ.get("CODEX_BRIDGE_HOST", "127.0.0.1")


def bridge_port() -> int:
    return int(os.environ.get("CODEX_BRIDGE_PORT", "8788"))


def bridge_url() -> str:
    return f"http://{bridge_host()}:{bridge_port()}"


def normalize_message(message: str) -> str:
    return " ".join(message.strip().split())


def parse_opt_int(raw: str | None) -> int | None:
    return None if raw in (None, "") else int(raw)


def text_result(text: str, *, is_error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        isError=is_error,
    )


def format_messages(messages: list[dict[str, str]]) -> str:
    body = "\n\n---\n\n".join(f"[{m['id']}] {m['text']}" for m in messages)
    return f"Pending message(s) from Codex:\n\n{body}\n\nUse reply with reply_to=<message id>."


@dataclass
class PendingReply:
    message_id: str
    created_at: float
    normalized_message: str | None
    reply: str | None = None
    failure_reason: str | None = None
    event: asyncio.Event = field(default_factory=asyncio.Event)


class BridgeState:
    def __init__(self, max_pending_reply_ms: int = 10 * 60 * 1000) -> None:
        self.max_pending_reply_ms = max_pending_reply_ms
        self._seq = 0
        self._lock = asyncio.Lock()
        self._pending_replies: dict[str, PendingReply] = {}
        self._inflight_by_message: dict[str, str] = {}
        self._pending_for_codex: list[dict[str, str]] = []
        self._pending_for_claude: list[dict[str, str]] = []
        self._pending_for_claude_event = asyncio.Event()

    def next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}{int(time.time() * 1000)}-{self._seq}"

    def _set_reply_locked(self, pending: PendingReply, text: str) -> None:
        pending.reply = text
        if pending.normalized_message:
            self._inflight_by_message.pop(pending.normalized_message, None)
        pending.event.set()

    def _take_claude_locked(self, limit: int | None) -> list[dict[str, str]]:
        if limit is None or limit >= len(self._pending_for_claude):
            messages = list(self._pending_for_claude)
            self._pending_for_claude.clear()
        else:
            messages = list(self._pending_for_claude[:limit])
            self._pending_for_claude = self._pending_for_claude[limit:]
        if not self._pending_for_claude:
            self._pending_for_claude_event.clear()
        return messages

    async def prune_expired(self) -> None:
        async with self._lock:
            now_ms = time.time() * 1000
            expired = [
                msg_id
                for msg_id, p in self._pending_replies.items()
                if now_ms - (p.created_at * 1000) > self.max_pending_reply_ms
            ]
            for msg_id in expired:
                p = self._pending_replies.pop(msg_id, None)
                if not p:
                    continue
                if p.normalized_message:
                    self._inflight_by_message.pop(p.normalized_message, None)
                p.event.set()

    async def create_or_get_codex_request(self, message: str) -> str:
        normalized = normalize_message(message)
        async with self._lock:
            existing = self._inflight_by_message.get(normalized)
            if existing and existing in self._pending_replies:
                return existing
            msg_id = self.next_id("codex-")
            self._pending_replies[msg_id] = PendingReply(
                message_id=msg_id,
                created_at=time.time(),
                normalized_message=normalized,
            )
            self._inflight_by_message[normalized] = msg_id
            return msg_id

    async def resolve_codex_reply(self, reply_to: str | None, text: str) -> bool:
        async with self._lock:
            if reply_to:
                p = self._pending_replies.get(reply_to)
                if not p or p.reply is not None:
                    return False
                self._set_reply_locked(p, text)
                return True
            unresolved = [p for p in self._pending_replies.values() if p.reply is None]
            if len(unresolved) != 1:
                return False
            self._set_reply_locked(unresolved[0], text)
            return True

    async def wait_for_reply(self, message_id: str, timeout_ms: int) -> dict[str, str | None] | None:
        async with self._lock:
            p = self._pending_replies.get(message_id)
            if not p:
                return None
            if p.reply is not None or p.failure_reason is not None:
                self._pending_replies.pop(message_id, None)
                return {"reply": p.reply, "failure_reason": p.failure_reason}
            event = p.event
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout_ms / 1000)
        except TimeoutError:
            return None
        async with self._lock:
            p = self._pending_replies.pop(message_id, None)
            return None if not p else {"reply": p.reply, "failure_reason": p.failure_reason}

    async def queue_for_codex(self, text: str, prefix: str = "claude-init-") -> str:
        async with self._lock:
            msg_id = self.next_id(prefix)
            self._pending_for_codex.append({"id": msg_id, "text": text})
            return msg_id

    async def pop_pending_for_codex(self) -> list[dict[str, str]]:
        async with self._lock:
            items = list(self._pending_for_codex)
            self._pending_for_codex.clear()
            return items

    async def queue_for_claude(self, message_id: str, text: str) -> None:
        async with self._lock:
            if any(item["id"] == message_id for item in self._pending_for_claude):
                return
            self._pending_for_claude.append({"id": message_id, "text": text})
            self._pending_for_claude_event.set()

    async def pop_pending_for_claude(self) -> list[dict[str, str]]:
        async with self._lock:
            return self._take_claude_locked(None)

    async def wait_pending_for_claude(self, *, timeout_ms: int, limit: int | None = None) -> list[dict[str, str]]:
        async with self._lock:
            if self._pending_for_claude:
                return self._take_claude_locked(limit)
            event = self._pending_for_claude_event
        if timeout_ms <= 0:
            return []
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout_ms / 1000)
        except TimeoutError:
            return []
        async with self._lock:
            return [] if not self._pending_for_claude else self._take_claude_locked(limit)

    async def count_pending_for_claude(self) -> int:
        async with self._lock:
            return len(self._pending_for_claude)


class ClaudeBridgeApp:
    def __init__(self) -> None:
        self.state = BridgeState()

    def make_web_app(self) -> web.Application:
        app = web.Application()
        app.add_routes(
            [
                web.post("/api/from-codex", self.handle_from_codex),
                web.post("/api/reply", self.handle_reply),
                web.post("/api/send-to-codex", self.handle_send_to_codex),
                web.get("/api/poll-reply/{message_id}", self.handle_poll_reply),
                web.get("/api/pending-for-codex", self.handle_pending_for_codex),
                web.get("/api/pending-for-claude", self.handle_pending_for_claude),
                web.get("/api/poll-for-claude", self.handle_poll_for_claude),
                web.get("/api/count-for-claude", self.handle_count_for_claude),
                web.get("/api/health", self.handle_health),
            ]
        )
        return app

    async def handle_from_codex(self, request: web.Request) -> web.Response:
        await self.state.prune_expired()
        message = str((await request.json()).get("message", "")).strip()
        if not message:
            return web.Response(text="missing message", status=400)
        msg_id = await self.state.create_or_get_codex_request(message)
        await self.state.queue_for_claude(msg_id, message)
        print(f"[bridge] queued message_id={msg_id} chars={len(message)}", file=sys.stderr, flush=True)
        return web.json_response({"id": msg_id})

    async def handle_reply(self, request: web.Request) -> web.Response:
        body = await request.json()
        text = str(body.get("text", "")).strip()
        reply_to = str(body.get("reply_to", "")).strip() or None
        if not text:
            return web.Response(text="missing text", status=400)
        ok = await self.state.resolve_codex_reply(reply_to, text)
        return web.json_response({"ok": True}) if ok else web.Response(text="could not match pending request", status=404)

    async def handle_send_to_codex(self, request: web.Request) -> web.Response:
        text = str((await request.json()).get("text", "")).strip()
        if not text:
            return web.Response(text="missing text", status=400)
        return web.json_response({"id": await self.state.queue_for_codex(text)})

    async def handle_poll_reply(self, request: web.Request) -> web.Response:
        await self.state.prune_expired()
        msg_id = request.match_info["message_id"]
        timeout_ms = int(request.query.get("timeout", "120000"))
        result = await self.state.wait_for_reply(msg_id, timeout_ms)
        if result is None:
            return web.json_response({"timeout": True, "reply": None})
        return web.json_response({"timeout": False, "reply": result.get("reply"), "failure_reason": result.get("failure_reason")})

    async def handle_pending_for_codex(self, _: web.Request) -> web.Response:
        await self.state.prune_expired()
        return web.json_response({"messages": await self.state.pop_pending_for_codex()})

    async def handle_pending_for_claude(self, _: web.Request) -> web.Response:
        await self.state.prune_expired()
        return web.json_response({"messages": await self.state.pop_pending_for_claude()})

    async def handle_poll_for_claude(self, request: web.Request) -> web.Response:
        await self.state.prune_expired()
        timeout_ms = int(request.query.get("timeout", "30000"))
        limit = parse_opt_int(request.query.get("limit"))
        if limit is not None and limit <= 0:
            return web.Response(text="limit must be greater than 0", status=400)
        messages = await self.state.wait_pending_for_claude(timeout_ms=timeout_ms, limit=limit)
        return web.json_response({"timeout": len(messages) == 0, "messages": messages})

    async def handle_count_for_claude(self, _: web.Request) -> web.Response:
        await self.state.prune_expired()
        return web.json_response({"count": await self.state.count_pending_for_claude()})

    async def handle_health(self, _: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})


class ToolBackend:
    async def reply(self, text: str, reply_to: str | None) -> tuple[bool, str | None]:
        raise NotImplementedError

    async def poll_from_codex(self, timeout_ms: int, limit: int | None) -> tuple[bool, list[dict[str, str]]]:
        raise NotImplementedError

    async def send_to_codex(self, text: str) -> tuple[bool, str]:
        raise NotImplementedError

    async def count_from_codex(self) -> tuple[bool, int]:
        raise NotImplementedError


class LocalBackend(ToolBackend):
    def __init__(self, state: BridgeState) -> None:
        self.state = state

    async def reply(self, text: str, reply_to: str | None) -> tuple[bool, str | None]:
        return await self.state.resolve_codex_reply(reply_to, text), None

    async def poll_from_codex(self, timeout_ms: int, limit: int | None) -> tuple[bool, list[dict[str, str]]]:
        return True, await self.state.wait_pending_for_claude(timeout_ms=timeout_ms, limit=limit)

    async def send_to_codex(self, text: str) -> tuple[bool, str]:
        return True, await self.state.queue_for_codex(text)

    async def count_from_codex(self) -> tuple[bool, int]:
        return True, await self.state.count_pending_for_claude()


class RemoteBackend(ToolBackend):
    async def _post(self, path: str, payload: dict[str, Any]) -> tuple[int, Any]:
        async with ClientSession() as session:
            async with session.post(f"{bridge_url()}{path}", json=payload) as r:
                try:
                    data = await r.json()
                except Exception:
                    data = await r.text()
                return r.status, data

    async def _get(self, path: str, params: dict[str, Any]) -> tuple[int, Any]:
        async with ClientSession() as session:
            async with session.get(f"{bridge_url()}{path}", params=params) as r:
                try:
                    data = await r.json()
                except Exception:
                    data = await r.text()
                return r.status, data

    async def reply(self, text: str, reply_to: str | None) -> tuple[bool, str | None]:
        try:
            status, _ = await self._post("/api/reply", {"reply_to": reply_to, "text": text})
            return status == 200, None
        except Exception as e:
            return False, str(e)

    async def poll_from_codex(self, timeout_ms: int, limit: int | None) -> tuple[bool, list[dict[str, str]]]:
        params: dict[str, Any] = {"timeout": timeout_ms}
        if limit is not None:
            params["limit"] = limit
        try:
            status, data = await self._get("/api/poll-for-claude", params)
            if status != 200:
                return False, []
            return True, data.get("messages", [])
        except Exception:
            return False, []

    async def send_to_codex(self, text: str) -> tuple[bool, str]:
        try:
            status, data = await self._post("/api/send-to-codex", {"text": text})
            if status != 200:
                return False, "unknown"
            return True, data.get("id", "unknown")
        except Exception:
            return False, "unknown"

    async def count_from_codex(self) -> tuple[bool, int]:
        try:
            status, data = await self._get("/api/count-for-claude", {})
            if status != 200:
                return False, 0
            return True, int(data.get("count", 0))
        except Exception:
            return False, 0


def build_server(app: ClaudeBridgeApp, *, primary: bool) -> Server:
    server = Server("nowonbun-claude-bridge")
    backend: ToolBackend = LocalBackend(app.state) if primary else RemoteBackend()

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name="reply",
                description="Send a reply to Codex. Always pass reply_to with message_id.",
                inputSchema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}, "reply_to": {"type": "string"}},
                    "required": ["text"],
                },
            ),
            types.Tool(
                name="poll_from_codex",
                description="Poll pending Codex messages.",
                inputSchema={
                    "type": "object",
                    "properties": {"timeout_ms": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1}},
                },
            ),
            types.Tool(
                name="send_to_codex",
                description="Queue a proactive message for Codex.",
                inputSchema={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
            ),
            types.Tool(
                name="count_from_codex",
                description="Return the number of pending messages from Codex.",
                inputSchema={"type": "object", "properties": {}},
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        if name == "reply":
            text = str(arguments.get("text", "")).strip()
            reply_to = str(arguments.get("reply_to", "")).strip() or None
            if not text:
                return text_result("error: empty text", is_error=True)
            ok, err = await backend.reply(text, reply_to)
            if not ok:
                return text_result(f"error: could not match pending request{f' ({err})' if err else ''}", is_error=True)
            return text_result("sent")

        if name == "poll_from_codex":
            timeout_ms = int(arguments.get("timeout_ms", 30000))
            limit = int(arguments["limit"]) if arguments.get("limit") is not None else None
            if timeout_ms < 0:
                return text_result("error: timeout_ms must be >= 0", is_error=True)
            if limit is not None and limit <= 0:
                return text_result("error: limit must be >= 1", is_error=True)
            ok, messages = await backend.poll_from_codex(timeout_ms, limit)
            if not ok:
                return text_result("error: bridge unreachable", is_error=True)
            return text_result("No pending messages from Codex.") if not messages else text_result(format_messages(messages))

        if name == "send_to_codex":
            text = str(arguments.get("text", "")).strip()
            if not text:
                return text_result("error: empty text", is_error=True)
            ok, msg_id = await backend.send_to_codex(text)
            if not ok:
                return text_result("error: bridge unreachable", is_error=True)
            return text_result(f"queued for codex ({msg_id})")

        if name == "count_from_codex":
            ok, count = await backend.count_from_codex()
            if not ok:
                return text_result("error: bridge unreachable", is_error=True)
            return text_result(f"Pending count from Codex: {count}")

        return text_result(f"unknown tool: {name}", is_error=True)

    return server


async def run() -> None:
    app = ClaudeBridgeApp()
    runner = web.AppRunner(app.make_web_app())
    await runner.setup()
    site = web.TCPSite(runner, bridge_host(), bridge_port())

    try:
        await site.start()
        primary = True
        print(f"claude-bridge {bridge_url()}", file=sys.stderr, flush=True)
    except OSError:
        await runner.cleanup()
        runner = None
        primary = False
        print(f"[bridge] secondary mode → {bridge_url()}", file=sys.stderr, flush=True)

    try:
        server = build_server(app, primary=primary)
        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                InitializationOptions(
                    server_name="nowonbun-claude-bridge",
                    server_version="0.3.0",
                    capabilities=server.get_capabilities(
                        notification_options=NotificationOptions(),
                        experimental_capabilities={},
                    ),
                    instructions=(
                        "Use poll_from_codex to fetch pending messages from Codex, "
                        "then respond with reply and include reply_to."
                    ),
                ),
            )
    finally:
        if runner is not None:
            await runner.cleanup()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
