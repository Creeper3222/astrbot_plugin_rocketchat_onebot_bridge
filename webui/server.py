from __future__ import annotations

import asyncio
import logging
import secrets
import socket
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

import uvicorn
from astrbot.api import logger
from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles


class BridgeLogBuffer:
    PREFIX = "[RocketChatOneBotBridge]"

    def __init__(self, max_entries: int = 5000):
        self.max_entries = int(max_entries)
        self._entries: deque[dict[str, Any]] = deque(maxlen=self.max_entries)
        self._lock = threading.Lock()
        self._next_id = 1

    def append_record(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if self.PREFIX not in message:
            return

        level = record.levelname.upper()
        if level == "WARNING":
            level = "WARN"

        entry = {
            "id": self._next_id,
            "timestamp": datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S")
            + f".{int(record.msecs):03d}",
            "level": level,
            "message": message,
            "line": f"[{datetime.fromtimestamp(record.created).strftime('%Y-%m-%d %H:%M:%S')}.{int(record.msecs):03d}] [{level}] {message}",
        }

        with self._lock:
            self._entries.append(entry)
            self._next_id += 1

    def get_entries(self, *, after_id: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(entry) for entry in self._entries if int(entry["id"]) > int(after_id)]

    def clear(self) -> int:
        with self._lock:
            cleared = len(self._entries)
            self._entries.clear()
            self._next_id = 1
            return cleared


class BridgeLogHandler(logging.Handler):
    def __init__(self, buffer: BridgeLogBuffer):
        super().__init__(level=logging.DEBUG)
        self.buffer = buffer

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.buffer.append_record(record)
        except Exception:
            self.handleError(record)


class IndependentWebUIServer:
    def __init__(self, manager: Any, *, host: str, port: int, access_password: str = ""):
        self.manager = manager
        self.host = host
        self.requested_port = int(port)
        self.port = int(port)
        self._access_password = str(access_password or "").strip()
        self._auth_required = bool(self._access_password)
        self._session_timeout = 3600
        self._session_max_lifetime = 86400
        self._session_cookie_name = "rocketcat_webui_token"
        self._sessions: dict[str, dict[str, float]] = {}
        self._session_lock = asyncio.Lock()
        self._failed_attempts: dict[str, list[float]] = {}
        self._attempt_lock = asyncio.Lock()
        self._server: uvicorn.Server | None = None
        self._server_task: asyncio.Task | None = None
        self._bound_socket: socket.socket | None = None
        self._log_buffer = BridgeLogBuffer(max_entries=5000)
        self._log_handler = BridgeLogHandler(self._log_buffer)
        self._app = FastAPI(title="RocketCat Shell", version="0.1.0")
        self._static_dir = Path(__file__).resolve().parent / "static"
        self._login_file = self._static_dir / "login.html"
        self._setup_routes()

    def _setup_routes(self) -> None:
        @self._app.middleware("http")
        async def _disable_cache(request: Request, call_next):
            path = request.url.path or "/"
            if self._auth_required and path.startswith("/api/") and path not in {"/api/login"}:
                if not await self._is_request_authenticated(request):
                    return JSONResponse(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        content={"detail": "请先登录管理 WebUI"},
                    )

            response = await call_next(request)
            if path == "/" or path.startswith("/static/"):
                response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
                response.headers["Pragma"] = "no-cache"
                response.headers["Expires"] = "0"
            return response

        self._app.mount(
            "/static",
            StaticFiles(directory=str(self._static_dir)),
            name="static",
        )
        self._app.add_api_route("/", self._handle_index, methods=["GET"])
        self._app.add_api_route("/api/status", self._handle_status, methods=["GET"])
        self._app.add_api_route("/api/login", self._handle_login, methods=["POST"])
        self._app.add_api_route("/api/logout", self._handle_logout, methods=["POST"])
        self._app.add_api_route("/api/basic-info", self._handle_basic_info, methods=["GET"])
        self._app.add_api_route("/api/logs", self._handle_logs, methods=["GET"])
        self._app.add_api_route("/api/logs/clear", self._handle_clear_logs, methods=["POST"])
        self._app.add_api_route("/api/bots", self._handle_list_bots, methods=["GET"])
        self._app.add_api_route("/api/bots", self._handle_create_bot, methods=["POST"])
        self._app.add_api_route(
            "/api/bots/{bot_id}",
            self._handle_update_bot,
            methods=["PUT"],
        )
        self._app.add_api_route(
            "/api/bots/{bot_id}",
            self._handle_delete_bot,
            methods=["DELETE"],
        )

    async def start(self) -> None:
        if self._server_task is not None and not self._server_task.done():
            return

        self._attach_log_handler()
        try:
            bound_socket, selected_port, fallback_reason = self._acquire_start_socket(
                self.host,
                self.requested_port,
            )
            self._bound_socket = bound_socket
            self.port = selected_port
            config = uvicorn.Config(
                app=self._app,
                host=self.host,
                port=self.port,
                log_level="warning",
                loop="asyncio",
                lifespan="on",
            )
            self._server = uvicorn.Server(config)
            self._server_task = asyncio.create_task(
                self._server.serve(sockets=[bound_socket])
            )

            for _ in range(50):
                if getattr(self._server, "started", False):
                    if fallback_reason:
                        logger.warning(
                            "[RocketChatOneBotBridge] 独立WebUI请求端口 %s 不可用，已自动回退到 %s。原因: %s",
                            self.requested_port,
                            self.port,
                            fallback_reason,
                        )
                    logger.info(
                        f"[RocketChatOneBotBridge] 独立WebUI已启动: http://{self.host}:{self.port}/"
                    )
                    return
                if self._server_task.done():
                    error = self._server_task.exception()
                    await self._cleanup_failed_start(reset_logs=True)
                    if error is None:
                        raise RuntimeError("独立WebUI启动失败: 未知错误")
                    raise RuntimeError(f"独立WebUI启动失败: {error}") from error
                await asyncio.sleep(0.1)

            logger.warning(
                f"[RocketChatOneBotBridge] 独立WebUI启动耗时较长，仍在后台启动中: http://{self.host}:{self.port}/"
            )
        except Exception:
            await self._cleanup_failed_start(reset_logs=True)
            raise

    async def stop(self) -> None:
        if self._server is None and self._server_task is None and self._bound_socket is None:
            return

        if self._server is not None:
            self._server.should_exit = True
        if self._server_task is not None:
            try:
                await self._server_task
            except asyncio.CancelledError:
                pass
            except BaseException as exc:
                logger.warning(f"[RocketChatOneBotBridge] 独立WebUI停止时出现异常: {exc!r}")
        self._server = None
        self._server_task = None
        if self._bound_socket is not None:
            try:
                self._bound_socket.close()
            finally:
                self._bound_socket = None
        self._detach_log_handler()
        self._log_buffer.clear()
        logger.info("[RocketChatOneBotBridge] 独立WebUI已停止。")

    def _acquire_start_socket(
        self,
        host: str,
        preferred_port: int,
    ) -> tuple[socket.socket, int, str | None]:
        candidates = [preferred_port, 5751, 0]
        seen: set[int] = set()
        preferred_error: OSError | None = None

        for candidate in candidates:
            if candidate in seen:
                continue
            seen.add(candidate)
            try:
                sock = self._bind_socket(host, candidate)
                actual_port = int(sock.getsockname()[1])
                if candidate == preferred_port:
                    return sock, actual_port, None
                reason = str(preferred_error) if preferred_error is not None else "请求端口不可用"
                return sock, actual_port, reason
            except OSError as exc:
                if candidate == preferred_port:
                    preferred_error = exc

        raise RuntimeError("独立WebUI无法绑定任何候选端口")

    def _bind_socket(self, host: str, port: int) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind((host, int(port)))
            sock.listen(128)
            sock.setblocking(False)
            return sock
        except Exception:
            sock.close()
            raise

    async def _cleanup_failed_start(self, *, reset_logs: bool = False) -> None:
        self._server = None
        self._server_task = None
        if self._bound_socket is not None:
            try:
                self._bound_socket.close()
            finally:
                self._bound_socket = None
        if reset_logs:
            self._detach_log_handler()
            self._log_buffer.clear()

    async def _cleanup_sessions_locked(self) -> None:
        now = time.time()
        expired_tokens = []
        for token, session in self._sessions.items():
            created_at = float(session.get("created_at") or 0.0)
            last_active = float(session.get("last_active") or 0.0)
            if now - created_at > self._session_max_lifetime:
                expired_tokens.append(token)
                continue
            if now - last_active > self._session_timeout:
                expired_tokens.append(token)

        for token in expired_tokens:
            self._sessions.pop(token, None)

    async def _cleanup_failed_attempts_locked(self) -> None:
        now = time.time()
        expired_clients = []
        for client_ip, attempts in self._failed_attempts.items():
            recent_attempts = [attempt for attempt in attempts if now - attempt < 300]
            if recent_attempts:
                self._failed_attempts[client_ip] = recent_attempts
            else:
                expired_clients.append(client_ip)

        for client_ip in expired_clients:
            self._failed_attempts.pop(client_ip, None)

    async def _check_rate_limit(self, client_ip: str) -> bool:
        async with self._attempt_lock:
            await self._cleanup_failed_attempts_locked()
            attempts = self._failed_attempts.get(client_ip, [])
            return len(attempts) < 5

    async def _record_failed_attempt(self, client_ip: str) -> None:
        async with self._attempt_lock:
            attempts = self._failed_attempts.setdefault(client_ip, [])
            attempts.append(time.time())

    def _extract_session_token(self, request: Request) -> str:
        return str(request.cookies.get(self._session_cookie_name, "") or "").strip()

    async def _is_request_authenticated(self, request: Request) -> bool:
        if not self._auth_required:
            return True

        token = self._extract_session_token(request)
        if not token:
            return False

        async with self._session_lock:
            await self._cleanup_sessions_locked()
            session = self._sessions.get(token)
            if not session:
                return False
            session["last_active"] = time.time()
            return True

    def _get_client_ip(self, request: Request) -> str:
        if request.client and request.client.host:
            return str(request.client.host)
        return "unknown"

    def _attach_log_handler(self) -> None:
        if self._log_handler not in logger.handlers:
            logger.addHandler(self._log_handler)

    def _detach_log_handler(self) -> None:
        if self._log_handler in logger.handlers:
            logger.removeHandler(self._log_handler)

    async def _handle_index(self, request: Request) -> FileResponse:
        if self._auth_required and not await self._is_request_authenticated(request):
            return FileResponse(self._login_file)
        return FileResponse(self._static_dir / "index.html")

    async def _handle_login(self, request: Request, payload: dict[str, Any]) -> JSONResponse:
        if not self._auth_required:
            return JSONResponse({"ok": True, "auth_required": False})

        password = str(payload.get("password", "")).strip()
        if not password:
            raise HTTPException(status_code=400, detail="访问密码不能为空")

        client_ip = self._get_client_ip(request)
        if not await self._check_rate_limit(client_ip):
            raise HTTPException(status_code=429, detail="尝试次数过多，请 5 分钟后再试")

        if not secrets.compare_digest(password, self._access_password):
            await self._record_failed_attempt(client_ip)
            await asyncio.sleep(0.8)
            raise HTTPException(status_code=401, detail="访问密码错误")

        token = secrets.token_urlsafe(32)
        now = time.time()
        async with self._session_lock:
            await self._cleanup_sessions_locked()
            self._sessions[token] = {
                "created_at": now,
                "last_active": now,
            }

        response = JSONResponse({"ok": True, "auth_required": True})
        response.set_cookie(
            key=self._session_cookie_name,
            value=token,
            max_age=self._session_max_lifetime,
            expires=self._session_max_lifetime,
            path="/",
            httponly=True,
            samesite="lax",
            secure=False,
        )
        return response

    async def _handle_logout(self, request: Request) -> JSONResponse:
        token = self._extract_session_token(request)
        if token:
            async with self._session_lock:
                self._sessions.pop(token, None)

        response = JSONResponse({"ok": True, "detail": "已退出登录"})
        response.delete_cookie(key=self._session_cookie_name, path="/")
        return response

    async def _handle_status(self) -> dict[str, Any]:
        return await self.manager.get_webui_state()

    async def _handle_basic_info(self) -> dict[str, Any]:
        return await self.manager.get_basic_info_state()

    async def _handle_logs(
        self,
        after_id: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        return {
            "items": self._log_buffer.get_entries(after_id=after_id),
            "max_entries": self._log_buffer.max_entries,
        }

    async def _handle_clear_logs(self) -> dict[str, Any]:
        return {
            "ok": True,
            "cleared": self._log_buffer.clear(),
            "max_entries": self._log_buffer.max_entries,
        }

    async def _handle_list_bots(self) -> dict[str, Any]:
        return {"items": await self.manager.list_sub_bots()}

    async def _handle_create_bot(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            created = await self.manager.create_sub_bot(payload)
        except ValueError as exc:
            from fastapi import HTTPException

            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.error(f"[RocketChatOneBotBridge] 创建副bot失败: {exc!r}")
            from fastapi import HTTPException

            raise HTTPException(status_code=500, detail="创建副bot失败") from exc
        return {"item": created}

    async def _handle_update_bot(self, bot_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            updated = await self.manager.update_sub_bot(bot_id, payload)
        except KeyError:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="找不到目标副bot")
        except ValueError as exc:
            from fastapi import HTTPException

            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.error(f"[RocketChatOneBotBridge] 更新副bot失败: {exc!r}")
            from fastapi import HTTPException

            raise HTTPException(status_code=500, detail="更新副bot失败") from exc
        return {"item": updated}

    async def _handle_delete_bot(self, bot_id: str) -> dict[str, bool]:
        try:
            await self.manager.delete_sub_bot(bot_id)
        except KeyError:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="找不到目标副bot")
        except Exception as exc:
            logger.error(f"[RocketChatOneBotBridge] 删除副bot失败: {exc!r}")
            from fastapi import HTTPException

            raise HTTPException(status_code=500, detail="删除副bot失败") from exc
        return {"ok": True}