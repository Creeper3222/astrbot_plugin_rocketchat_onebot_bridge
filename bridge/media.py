from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import tempfile
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import aiohttp
from astrbot.api import logger


class RocketChatMediaBridge:
    def __init__(self, client: Any) -> None:
        self.client = client

    def classify_file_kind(self, file_obj: dict[str, Any]) -> str:
        candidates: list[str] = []

        for key in (
            "type",
            "mimeType",
            "contentType",
            "image_type",
            "audio_type",
            "video_type",
        ):
            value = file_obj.get(key)
            if isinstance(value, str) and value:
                candidates.append(value)

        for key in (
            "name",
            "title",
            "url",
            "path",
            "title_link",
            "titleLink",
            "link",
            "image_url",
            "imageUrl",
            "audio_url",
            "audioUrl",
            "video_url",
            "videoUrl",
        ):
            value = file_obj.get(key)
            if not isinstance(value, str) or not value:
                continue
            guessed, _ = mimetypes.guess_type(value.split("?", 1)[0])
            if guessed:
                candidates.append(guessed)

        for candidate in candidates:
            if candidate.startswith("image/"):
                return "image"
            if candidate.startswith("audio/"):
                return "audio"
            if candidate.startswith("video/"):
                return "video"

        return "file"

    def get_all_attachments_recursive(
        self,
        payload: dict[str, Any],
        *,
        skip_quote_attachments: bool = False,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        att_raw = payload.get("attachments", [])
        atts = [att_raw] if isinstance(att_raw, dict) else [item for item in att_raw if isinstance(item, dict)]
        for att in atts:
            if skip_quote_attachments and att.get("message_link"):
                continue
            result.append(att)
            result.extend(
                self.get_all_attachments_recursive(
                    att,
                    skip_quote_attachments=skip_quote_attachments,
                )
            )
        return result

    def _is_encrypted_media_attachment(self, file_obj: dict[str, Any]) -> bool:
        encryption = file_obj.get("encryption")
        return (
            isinstance(encryption, dict)
            and isinstance(encryption.get("key"), dict)
            and isinstance(encryption.get("iv"), str)
            and bool(encryption.get("iv"))
        )

    async def download_remote_bytes(self, url: str) -> Optional[bytes]:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            logger.warning(f"[RocketChatOneBotBridge] 拒绝下载不支持的媒体协议: {url}")
            return None
        if self.client._http_session is None:
            return None

        try:
            async with self.client._http_session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=30, connect=10),
                allow_redirects=True,
                max_redirects=3,
            ) as resp:
                if resp.status >= 400:
                    logger.error(f"[RocketChatOneBotBridge] 下载媒体失败 {resp.status}: {url}")
                    return None

                content_length = resp.content_length
                if (
                    content_length is not None
                    and content_length > self.client.config.remote_media_max_size
                ):
                    logger.error(
                        f"[RocketChatOneBotBridge] 下载媒体失败，文件过大: {content_length} > {self.client.config.remote_media_max_size} ({url})"
                    )
                    return None

                raw = bytearray()
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    raw.extend(chunk)
                    if len(raw) > self.client.config.remote_media_max_size:
                        logger.error(
                            f"[RocketChatOneBotBridge] 下载媒体失败，文件超过限制: {len(raw)} > {self.client.config.remote_media_max_size} ({url})"
                        )
                        return None
                return bytes(raw)
        except Exception as exc:
            logger.error(f"[RocketChatOneBotBridge] 下载媒体异常: {exc!r}")
            return None

    async def _select_media_url(
        self,
        file_obj: dict[str, Any],
        target_kind: str,
    ) -> Optional[str]:
        key_candidates: dict[str, tuple[str, ...]] = {
            "image": (
                "image_url",
                "imageUrl",
                "image",
                "thumb_url",
                "thumbUrl",
                "image_preview",
                "imagePreview",
                "title_link",
                "titleLink",
                "url",
                "path",
                "link",
            ),
            "audio": (
                "audio_url",
                "audioUrl",
                "title_link",
                "titleLink",
                "url",
                "path",
                "link",
            ),
            "video": (
                "video_url",
                "videoUrl",
                "title_link",
                "titleLink",
                "url",
                "path",
                "link",
            ),
            "file": (
                "title_link",
                "titleLink",
                "url",
                "path",
                "link",
            ),
        }

        for key in key_candidates.get(target_kind, ()): 
            value = file_obj.get(key)
            if isinstance(value, str) and value:
                return await self.client._normalize_media_url(value)
        return None

    def _guess_media_suffix(
        self,
        file_obj: dict[str, Any],
        media_url: str,
        default_suffix: str,
    ) -> str:
        for candidate in (
            file_obj.get("name"),
            file_obj.get("title"),
            media_url,
        ):
            if not isinstance(candidate, str) or not candidate:
                continue
            parsed = urlparse(candidate)
            _, ext = os.path.splitext(parsed.path or candidate)
            if ext:
                return ext

        for key in (
            "type",
            "mimeType",
            "contentType",
            "image_type",
            "audio_type",
            "video_type",
        ):
            mime_value = file_obj.get(key)
            if not isinstance(mime_value, str) or not mime_value:
                continue
            ext = mimetypes.guess_extension(mime_value.split(";", 1)[0].strip())
            if ext:
                return ext

        return default_suffix

    def _write_temp_media_file(self, raw: bytes, suffix: str) -> str:
        tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        try:
            tmp.write(raw)
            tmp.close()
            return tmp.name
        except Exception:
            tmp.close()
            os.unlink(tmp.name)
            raise

    async def _materialize_media_reference(
        self,
        file_obj: dict[str, Any],
        target_kind: str,
    ) -> Optional[dict[str, str]]:
        media_url = await self._select_media_url(file_obj, target_kind)
        if not media_url:
            return None

        name = (
            file_obj.get("name")
            or file_obj.get("title")
            or os.path.basename(urlparse(media_url).path)
            or "attachment"
        )

        if not self._is_encrypted_media_attachment(file_obj):
            return {"name": str(name), "url": media_url}

        raw = await self.download_remote_bytes(media_url)
        if raw is None:
            return None

        try:
            encryption = file_obj["encryption"]
            decrypted = self.client.e2ee.decrypt_uploaded_media(
                raw,
                key_data=encryption["key"],
                iv_b64=encryption["iv"],
            )
        except Exception as exc:
            logger.warning(f"[RocketChatOneBotBridge][E2EE] 媒体解密失败: {exc!r}")
            return None

        expected_hash = (
            file_obj.get("hashes", {}).get("sha256")
            if isinstance(file_obj.get("hashes"), dict)
            else None
        )
        if expected_hash:
            actual_hash = hashlib.sha256(decrypted).hexdigest()
            if actual_hash.lower() != str(expected_hash).lower():
                logger.warning(
                    f"[RocketChatOneBotBridge][E2EE] 媒体哈希校验失败: expected={expected_hash} actual={actual_hash}"
                )
                return None

        suffix = self._guess_media_suffix(file_obj, media_url, ".bin")
        local_path = self._write_temp_media_file(decrypted, suffix)
        return {"name": str(name), "path": local_path}

    async def _extract_media_payloads(
        self,
        raw_msg: dict[str, Any],
        target_kind: str,
    ) -> list[dict[str, str]]:
        results: list[dict[str, str]] = []

        async def add_candidate(file_obj: dict[str, Any]) -> None:
            if self.classify_file_kind(file_obj) != target_kind:
                return
            materialized = await self._materialize_media_reference(file_obj, target_kind)
            if materialized:
                results.append(materialized)

        all_attachments = self.get_all_attachments_recursive(
            raw_msg,
            skip_quote_attachments=True,
        )

        for context in [raw_msg] + all_attachments:
            files_raw = context.get("files", [])
            iterable = [files_raw] if isinstance(files_raw, dict) else [item for item in files_raw if isinstance(item, dict)]
            for file_obj in iterable:
                await add_candidate(file_obj)

            for file_key in ("file", "fileUpload"):
                single_file = context.get(file_key)
                if isinstance(single_file, dict):
                    await add_candidate(single_file)

            if context is not raw_msg:
                await add_candidate(context)

        return results

    async def extract_onebot_segments(self, raw_msg: dict[str, Any]) -> list[dict[str, Any]]:
        segments: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()

        for media in await self._extract_media_payloads(raw_msg, "image"):
            file_ref = media.get("path") or media.get("url") or ""
            key = ("image", file_ref)
            if not file_ref or key in seen:
                continue
            seen.add(key)
            segments.append({"type": "image", "data": {"file": file_ref}})

        for media in await self._extract_media_payloads(raw_msg, "audio"):
            file_ref = media.get("path") or media.get("url") or ""
            key = ("record", file_ref)
            if not file_ref or key in seen:
                continue
            seen.add(key)
            segments.append({"type": "record", "data": {"file": file_ref}})

        for media in await self._extract_media_payloads(raw_msg, "video"):
            file_ref = media.get("path") or media.get("url") or ""
            key = ("video", file_ref)
            if not file_ref or key in seen:
                continue
            seen.add(key)
            segments.append({"type": "video", "data": {"file": file_ref}})

        for media in await self._extract_media_payloads(raw_msg, "file"):
            file_ref = media.get("path") or media.get("url") or ""
            key = ("file", file_ref)
            if not file_ref or key in seen:
                continue
            seen.add(key)
            name = media.get("name") or "attachment"
            if media.get("path"):
                segments.append(
                    {
                        "type": "text",
                        "data": {
                            "text": f"[加密文件] {name}",
                        },
                    }
                )
                continue
            segments.append(
                {
                    "type": "file",
                    "data": {
                        "url": file_ref,
                        "file_name": name,
                        "name": name,
                    },
                }
            )

        for url_obj in raw_msg.get("urls", []):
            if not isinstance(url_obj, dict):
                continue
            meta = url_obj.get("meta") if isinstance(url_obj.get("meta"), dict) else {}
            headers = url_obj.get("headers") if isinstance(url_obj.get("headers"), dict) else {}
            content_type = (
                meta.get("contentType")
                or headers.get("contentType")
                or headers.get("content-type")
                or ""
            )
            if not str(content_type).startswith("image/"):
                continue
            candidate = url_obj.get("url")
            if not isinstance(candidate, str) or not candidate:
                continue
            normalized = await self.client._normalize_media_url(candidate)
            key = ("image", normalized)
            if key in seen:
                continue
            seen.add(key)
            segments.append({"type": "image", "data": {"file": normalized}})

        return segments

    def infer_upload_content_type(self, file_path: str, filename: str) -> str:
        guessed_type, _ = mimetypes.guess_type(filename)
        if guessed_type:
            return guessed_type

        guessed_type, _ = mimetypes.guess_type(file_path)
        if guessed_type:
            return guessed_type

        try:
            with open(file_path, "rb") as fp:
                header = fp.read(16)
        except Exception:
            return "application/octet-stream"

        if header.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if header.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if header.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif"
        if header.startswith(b"BM"):
            return "image/bmp"
        if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
            return "image/webp"

        return "application/octet-stream"

    async def post_multipart_json(
        self,
        url: str,
        form: aiohttp.FormData,
    ) -> Optional[dict[str, Any]]:
        headers = {
            "X-Auth-Token": self.client.auth_token,
            "X-User-Id": self.client.user_id,
        }
        if self.client._http_session is None:
            return None

        try:
            async with self.client._http_session.post(url, data=form, headers=headers) as resp:
                data = await resp.json(content_type=None)
                if resp.status >= 400 or not data.get("success", resp.status < 400):
                    logger.error(f"[RocketChatOneBotBridge] 上传请求失败: status={resp.status} data={data}")
                    return None
                return data
        except Exception as exc:
            logger.error(f"[RocketChatOneBotBridge] 上传请求异常: {exc!r}")
            return None

    async def upload_plain_file(
        self,
        room_id: str,
        file_path: str,
        resolved_name: str,
        description: str = "",
        tmid: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        url = f"{self.client.config.server_url}/api/v1/rooms.upload/{room_id}"
        with open(file_path, "rb") as fp:
            form = aiohttp.FormData()
            content_type = self.infer_upload_content_type(file_path, resolved_name)
            form.add_field("file", fp, filename=resolved_name, content_type=content_type)
            if description:
                form.add_field("description", description)
            if tmid:
                form.add_field("tmid", tmid)
            return await self.post_multipart_json(url, form)

    async def upload_local_file(
        self,
        room_id: str,
        file_path: str,
        resolved_name: str,
        description: str = "",
        tmid: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        room_info = await self.client.get_room_info(room_id)
        if self._is_e2ee_room_info(room_info):
            return await self.upload_encrypted_file(
                room_id,
                file_path,
                resolved_name,
                description=description,
                tmid=tmid,
            )
        return await self.upload_plain_file(
            room_id,
            file_path,
            resolved_name,
            description=description,
            tmid=tmid,
        )

    def _is_e2ee_room_info(self, room_info: dict[str, Any]) -> bool:
        return bool(room_info.get("encrypted") and room_info.get("t") in {"d", "p"})

    async def upload_encrypted_file(
        self,
        room_id: str,
        file_path: str,
        resolved_name: str,
        description: str = "",
        tmid: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        try:
            with open(file_path, "rb") as fp:
                file_bytes = fp.read()
        except FileNotFoundError:
            logger.error(f"[RocketChatOneBotBridge] 文件不存在: {file_path}")
            return None

        upload = await self.client.e2ee.prepare_encrypted_upload(
            room_id,
            file_name=resolved_name,
            mime_type=self.infer_upload_content_type(file_path, resolved_name),
            file_bytes=file_bytes,
        )
        if not upload:
            logger.warning(
                f"[RocketChatOneBotBridge][E2EE] 未能准备加密上传数据，已跳过 room_id={room_id!r}"
            )
            return None

        file_content = await self.client.e2ee.build_upload_file_content(room_id, upload)
        if not file_content:
            logger.warning(
                f"[RocketChatOneBotBridge][E2EE] 未能生成加密文件元数据，已跳过 room_id={room_id!r}"
            )
            return None

        form = aiohttp.FormData()
        form.add_field(
            "file",
            upload.encrypted_bytes,
            filename=upload.encrypted_name,
            content_type="application/octet-stream",
        )
        form.add_field("content", json.dumps(file_content["encrypted"], ensure_ascii=False))

        upload_resp = await self.post_multipart_json(
            f"{self.client.config.server_url}/api/v1/rooms.media/{room_id}",
            form,
        )
        if not upload_resp:
            return None

        uploaded_file = upload_resp.get("file") or {}
        file_id = uploaded_file.get("_id")
        file_url = uploaded_file.get("url")
        if not file_id or not file_url:
            logger.error(
                f"[RocketChatOneBotBridge][E2EE] rooms.media 响应缺少文件信息: {upload_resp}"
            )
            return None

        confirm_payload = await self.client.e2ee.build_media_confirm_payload(
            room_id,
            upload_id=file_id,
            upload_url=file_url,
            upload=upload,
            text=description,
            tmid=tmid,
        )
        if not confirm_payload:
            logger.warning(
                f"[RocketChatOneBotBridge][E2EE] 未能生成 mediaConfirm 负载，已跳过 room_id={room_id!r}"
            )
            return None

        data = await self.client._post_json_message(
            f"{self.client.config.server_url}/api/v1/rooms.mediaConfirm/{room_id}/{file_id}",
            confirm_payload,
        )
        return (data or {}).get("message") or data

    async def send_remote_media_fallback(
        self,
        room_id: str,
        media_url: str,
        *,
        media_kind: str,
        text: str = "",
        tmid: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        fallback_text = f"远程{media_kind}发送失败，原文件链接：{media_url}"
        if text:
            fallback_text = f"{text}\n{fallback_text}".strip()
        return await self.client.send_text(room_id, fallback_text, tmid=tmid)

    async def send_image_url(
        self,
        room_id: str,
        image_url: str,
        text: str = "",
        tmid: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        local_path, cleanup = await self.download_remote_media(image_url, ".png")
        if not local_path:
            room_info = await self.client.get_room_info(room_id)
            if self._is_e2ee_room_info(room_info):
                return await self.send_remote_media_fallback(
                    room_id,
                    image_url,
                    media_kind="图片",
                    text=text,
                    tmid=tmid,
                )
            if text:
                return await self.client._send_structured_message(
                    room_id,
                    text,
                    attachments=[{"image_url": image_url}],
                    tmid=tmid,
                )
            return await self.send_remote_media_fallback(
                room_id,
                image_url,
                media_kind="图片",
                text=text,
                tmid=tmid,
            )

        try:
            return await self.send_image_file(
                room_id,
                local_path,
                description=text,
                tmid=tmid,
            )
        finally:
            if cleanup:
                cleanup()

    async def send_image_file(
        self,
        room_id: str,
        file_path: str,
        description: str = "",
        tmid: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        try:
            filename = os.path.basename(file_path) or "image.png"
            data = await self.upload_local_file(
                room_id,
                file_path,
                filename,
                description=description,
                tmid=tmid,
            )
            return (data or {}).get("message") or data
        except FileNotFoundError:
            logger.error(f"[RocketChatOneBotBridge] 图片文件不存在: {file_path}")
            return None
        except Exception as exc:
            logger.error(f"[RocketChatOneBotBridge] 上传图片异常: {exc!r}")
            return None

    async def send_file(
        self,
        room_id: str,
        file_path: str,
        filename: Optional[str] = None,
        description: str = "",
        tmid: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        try:
            resolved_name = filename or os.path.basename(file_path) or "attachment"
            data = await self.upload_local_file(
                room_id,
                file_path,
                resolved_name,
                description=description,
                tmid=tmid,
            )
            return (data or {}).get("message") or data
        except FileNotFoundError:
            logger.error(f"[RocketChatOneBotBridge] 文件不存在: {file_path}")
            return None
        except Exception as exc:
            logger.error(f"[RocketChatOneBotBridge] 上传文件异常: {exc!r}")
            return None

    async def download_remote_media(
        self,
        url: str,
        default_suffix: str,
    ) -> tuple[str | None, Callable[[], None] | None]:
        parsed = urlparse(url)
        filename = os.path.basename(parsed.path)
        _, ext = os.path.splitext(filename)
        suffix = ext if ext else default_suffix
        raw = await self.download_remote_bytes(url)
        if raw is None:
            return None, None

        tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        try:
            tmp.write(raw)
            tmp.close()
            return tmp.name, lambda: os.unlink(tmp.name)
        except Exception:
            tmp.close()
            os.unlink(tmp.name)
            raise

    def decode_base64_media(
        self,
        file_ref: str,
        default_suffix: str,
    ) -> tuple[str | None, Callable[[], None] | None]:
        try:
            raw = base64.b64decode(file_ref[len("base64://") :])
        except Exception as exc:
            logger.error(f"[RocketChatOneBotBridge] Base64 媒体处理失败: {exc!r}")
            return None, None

        tmp = tempfile.NamedTemporaryFile(suffix=default_suffix, delete=False)
        try:
            tmp.write(raw)
            tmp.close()
            return tmp.name, lambda: os.unlink(tmp.name)
        except Exception:
            tmp.close()
            os.unlink(tmp.name)
            raise


def summarize_unsupported_media(raw_msg: dict) -> str | None:
    attachment_count = 0
    attachments = raw_msg.get("attachments")
    if isinstance(attachments, dict):
        attachment_count += 1
    elif isinstance(attachments, list):
        attachment_count += len([item for item in attachments if isinstance(item, dict)])

    file_count = 0
    if raw_msg.get("file"):
        file_count += 1
    files = raw_msg.get("files")
    if isinstance(files, list):
        file_count += len(files)

    if attachment_count == 0 and file_count == 0:
        return None

    total = attachment_count + file_count
    return f"[当前仍有未识别媒体消息，共 {total} 个媒体项]"