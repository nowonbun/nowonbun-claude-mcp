from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from aiohttp import ClientSession
import mcp.server.stdio
import mcp.types as types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions


def bridge_url() -> str:
    return os.environ.get("CODEX_BRIDGE_URL", "http://127.0.0.1:8788").rstrip("/")


def total_wait_ms() -> int:
    return int(os.environ.get("CODEX_BRIDGE_TOTAL_WAIT_MS", "110000"))


def poll_slice_ms() -> int:
    return int(os.environ.get("CODEX_BRIDGE_POLL_SLICE_MS", "15000"))


def elapsed_label(started_at: float) -> str:
    return f"{round(time.time() - started_at)}s"


def text_result(text: str, *, is_error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        isError=is_error,
    )


def build_server() -> Server:
    server = Server("nowonbun-codex-mcp")
    inflight_messages: dict[str, asyncio.Task[types.CallToolResult]] = {}
    inflight_lock = asyncio.Lock()

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name="send_to_claude",
                description="Send a message to Claude through the local bridge and wait for a reply.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "message": {"type": "string", "description": "The message to send to Claude."}
                    },
                    "required": ["message"],
                },
            ),
            types.Tool(
                name="check_claude_messages",
                description="Fetch pending proactive messages from Claude.",
                inputSchema={"type": "object", "properties": {}},
            ),
        ]

    async def _send_to_claude(message: str) -> types.CallToolResult:
        started_at = time.time()
        async with ClientSession() as session:
            async with session.post(
                f"{bridge_url()}/api/from-codex",
                json={"message": message},
            ) as response:
                if response.status != 200:
                    return text_result(
                        f"error sending to bridge: {response.status} {await response.text()}",
                        is_error=True,
                    )
                data = await response.json()
                message_id = data["id"]

            remaining_ms = total_wait_ms()
            while remaining_ms > 0:
                timeout_ms = min(poll_slice_ms(), remaining_ms)
                async with session.get(
                    f"{bridge_url()}/api/poll-reply/{message_id}",
                    params={"timeout": timeout_ms},
                ) as poll_response:
                    if poll_response.status != 200:
                        return text_result(
                            f"error polling reply: {poll_response.status} {await poll_response.text()}",
                            is_error=True,
                        )
                    payload = await poll_response.json()
                failure_reason = payload.get("failure_reason")
                if failure_reason:
                    return text_result(
                        f"bridge failure for {message_id}: {failure_reason}",
                        is_error=True,
                    )
                if payload.get("reply"):
                    return text_result(str(payload["reply"]))

                remaining_ms = total_wait_ms() - int((time.time() - started_at) * 1000)

        return text_result(
            (
                f"Claude did not reply within {elapsed_label(started_at)}. "
                "The same request may still be in flight. Do not immediately resend the exact same prompt."
            )
        )

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        if name == "send_to_claude":
            message = str(arguments.get("message", "")).strip()
            if not message:
                return text_result("error: empty message", is_error=True)

            async with inflight_lock:
                existing = inflight_messages.get(message)
                if existing is None:
                    existing = asyncio.create_task(_send_to_claude(message))
                    inflight_messages[message] = existing

            try:
                return await existing
            finally:
                async with inflight_lock:
                    if inflight_messages.get(message) is existing:
                        inflight_messages.pop(message, None)

        if name == "check_claude_messages":
            async with ClientSession() as session:
                async with session.get(f"{bridge_url()}/api/pending-for-codex") as response:
                    if response.status != 200:
                        return text_result(
                            f"error checking messages: {response.status}",
                            is_error=True,
                        )
                    payload = await response.json()

            messages = payload.get("messages", [])
            if not messages:
                return text_result("No pending messages from Claude.")

            formatted = "\n\n---\n\n".join(
                f"[{item['id']}] {item['text']}" for item in messages
            )
            return text_result(f"{len(messages)} message(s) from Claude:\n\n{formatted}")

        return text_result(f"unknown tool: {name}", is_error=True)

    return server


async def run() -> None:
    server = build_server()
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="nowonbun-codex-mcp",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
