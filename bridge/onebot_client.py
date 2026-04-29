from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable

import aiohttp
from astrbot.api import logger

from .config import BridgeConfig


ActionHandler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
FailureCallback = Callable[[str, int, str], Awaitable[None]]


class OneBotReverseWsClient:
    def __init__(
        self,
        config: BridgeConfig,
        action_handler: ActionHandler,
        on_reconnect_exhausted: FailureCallback | None = None,
    ):
        self.config = config
        self._action_handler = action_handler
        self._on_reconnect_exhausted = on_reconnect_exhausted
        self._http_session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._running = False
        self._task: asyncio.Task | None = None
        self._sender_task: asyncio.Task | None = None
        self._outgoing: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._consecutive_reconnect_failures = 0

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45.0))
        self._task = asyncio.create_task(self._run_forever())

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._sender_task is not None:
            self._sender_task.cancel()
            try:
                await self._sender_task
            except asyncio.CancelledError:
                pass
            self._sender_task = None
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        self._ws = None
        if self._http_session is not None:
            await self._http_session.close()
        self._http_session = None
        self._consecutive_reconnect_failures = 0

    async def emit_event(self, payload: dict[str, Any]) -> None:
        if not self._running:
            return
        await self._outgoing.put(payload)

    async def _run_forever(self) -> None:
        if self._http_session is None:
            raise RuntimeError("OneBot HTTP session 尚未初始化")
        while self._running:
            try:
                headers = {
                    "X-Self-ID": str(self.config.onebot_self_id),
                    "X-Client-Role": "Universal",
                }
                if self.config.onebot_access_token:
                    headers["Authorization"] = f"Bearer {self.config.onebot_access_token}"
                async with self._http_session.ws_connect(
                    self.config.onebot_ws_url,
                    headers=headers,
                    heartbeat=30.0,
                    autoping=True,
                ) as ws:
                    self._ws = ws
                    self._consecutive_reconnect_failures = 0
                    logger.info("[RocketChatOneBotBridge] 已连接 AstrBot OneBot reverse WebSocket。")
                    self._sender_task = asyncio.create_task(self._sender_loop())
                    await self._listen_loop(ws)
                    if self._running:
                        logger.warning(
                            f"[RocketChatOneBotBridge] OneBot reverse WS 已断开，{self.config.reconnect_delay:.1f}s 后重连。"
                        )
                        await asyncio.sleep(self.config.reconnect_delay)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self._running:
                    break
                self._consecutive_reconnect_failures += 1
                if self._should_stop_reconnect():
                    await self._handle_reconnect_exhausted(exc)
                    break
                logger.warning(
                    f"[RocketChatOneBotBridge] OneBot reverse WS 重连失败第 {self._consecutive_reconnect_failures} 次: {exc!r}，{self.config.reconnect_delay:.1f}s 后继续重连。"
                )
                await asyncio.sleep(self.config.reconnect_delay)
            finally:
                self._ws = None
                if self._sender_task is not None:
                    self._sender_task.cancel()
                    try:
                        await self._sender_task
                    except asyncio.CancelledError:
                        pass
                    self._sender_task = None

    def _should_stop_reconnect(self) -> bool:
        max_attempts = self.config.max_reconnect_attempts
        return max_attempts > 0 and self._consecutive_reconnect_failures >= max_attempts

    async def _handle_reconnect_exhausted(self, exc: Exception) -> None:
        self._running = False
        logger.error("[RocketChatOneBotBridge] 连接失败，已自动关闭rocketchat桥接器，请检查网络或目标服务器状态")
        if self._on_reconnect_exhausted is not None:
            await self._on_reconnect_exhausted(
                "OneBot reverse WebSocket",
                self._consecutive_reconnect_failures,
                repr(exc),
            )

    async def _sender_loop(self) -> None:
        while self._running and self._ws is not None and not self._ws.closed:
            payload = await self._outgoing.get()
            if self._ws is None or self._ws.closed:
                break
            await self._ws.send_json(payload)

    async def _listen_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        async for raw in ws:
            if raw.type != aiohttp.WSMsgType.TEXT:
                if raw.type in {
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.ERROR,
                }:
                    break
                continue
            data = json.loads(raw.data)
            action = data.get("action")
            if not action:
                continue
            params = data.get("params") or {}
            echo = data.get("echo")
            response = await self._action_handler(str(action), params)
            response_payload = {
                "status": response.get("status", "ok"),
                "retcode": response.get("retcode", 0),
                "data": response.get("data"),
                "wording": response.get("wording", ""),
                "echo": echo,
            }
            await ws.send_json(response_payload)