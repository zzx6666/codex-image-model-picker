#!/usr/bin/env python3
"""Codex Image2：使用用户选择的图片模型生成或编辑图片。"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import contextlib
import ctypes
import hashlib
import http.client
import http.cookiejar
import ipaddress
import json
import mimetypes
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable


DEFAULT_MODEL = "gpt-image-2"
PREFERRED_MODEL = "gpt-image-2.5"
DEFAULT_SIZE = "1024x1024"
DEFAULT_QUALITY = "auto"
DEFAULT_OUT_DIR = "."
KEYCHAIN_SERVICE = "codex-image2"
RETRYABLE = {429, 500, 502, 503, 504, 524}
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
SKILL_DIR = Path(__file__).resolve().parent
CONFIGURE_HTML = SKILL_DIR / "src" / "configure.html"
BATCH_JOB_FIELDS = {"prompt", "model", "name", "size", "quality", "n", "out"}


class AppError(Exception):
    """可以直接展示给用户的错误。"""


@dataclass
class Settings:
    api_url: str
    api_key: str = ""
    source: str = "none"
    storage: str = ""


def random_id(length: int = 24) -> str:
    return secrets.token_hex(length)


def config_path() -> Path:
    override = os.environ.get("CODEX_IMAGE2_CONFIG", "").strip()
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "codex-image2" / "config.json"


def normalize_api_base(value: str) -> str:
    value = value.strip()
    if not value:
        raise AppError("请填写 API 地址")
    if "://" not in value:
        value = "https://" + value
    parsed = urllib.parse.urlsplit(value)
    if not parsed.hostname or any(char in parsed.netloc for char in " \t\r\n\\"):
        raise AppError("API 地址格式不正确，请填写服务商提供的网址")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise AppError("API 地址不能包含账号、密码、查询参数或 # 片段")
    hostname = parsed.hostname.lower()
    try:
        parsed.port
    except ValueError as exc:
        raise AppError("API 地址中的端口无效") from exc
    loopback = is_loopback_host(hostname)
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise AppError("API 地址需要使用 https://；本机调试地址可使用 http://")
    path = parsed.path.rstrip("/")
    for suffix in ("/images/generations", "/images/edits", "/models"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    if not path.endswith("/v1"):
        path += "/v1"
    return urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, "", ""))


def is_loopback_host(hostname: str) -> bool:
    hostname = hostname.lower()
    if hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def validate_key(key: str) -> None:
    if not key:
        raise AppError("请填写 API 密钥")
    if len(key) > 1024:
        raise AppError("密钥过长，请确认粘贴的是 API Key")
    if any(ord(char) <= 32 or ord(char) > 126 for char in key):
        raise AppError("密钥中含有空格、换行或非英文字符，请检查复制的内容")


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi(data: bytes, decrypt: bool = False) -> bytes:
    if not data:
        raise AppError("Windows 系统加密失败，请重新填写密钥")
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    input_blob = DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output_blob = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    function = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    function.argtypes = [
        ctypes.POINTER(DATA_BLOB),
        ctypes.c_void_p,
        ctypes.POINTER(DATA_BLOB),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(DATA_BLOB),
    ]
    function.restype = ctypes.c_int
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    ok = function(
        ctypes.byref(input_blob),
        None,
        None,
        None,
        None,
        1,
        ctypes.byref(output_blob),
    )
    if not ok:
        raise AppError("Windows 系统加密失败，请使用当前 Windows 用户重新配置密钥")
    try:
        if output_blob.cbData == 0 or output_blob.cbData > 64 * 1024:
            raise AppError("Windows 凭据数据无效")
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        if output_blob.pbData:
            kernel32.LocalFree(output_blob.pbData)


def _keychain_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def seal_secret(key: str) -> dict[str, str]:
    if sys.platform == "win32":
        encrypted = _dpapi(key.encode("utf-8"))
        return {"kind": "windows-dpapi", "data": base64.b64encode(encrypted).decode("ascii")}
    if sys.platform == "darwin":
        reference = random_id()
        command = (
            f"add-generic-password -a {reference} -s {KEYCHAIN_SERVICE} "
            f"-w {_keychain_quote(key)}\n"
        )
        try:
            subprocess.run(
                ["/usr/bin/security", "-i", "-q"],
                input=command,
                text=True,
                capture_output=True,
                timeout=60,
                check=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise AppError("无法保存到 macOS 钥匙串，请解锁登录钥匙串后重试") from exc
        secret = {"kind": "macos-keychain", "data": reference}
        try:
            if open_secret(secret) != key:
                raise AppError("无法完整保存到 macOS 钥匙串，请检查密钥长度并解锁钥匙串后重试")
        except AppError:
            delete_secret(secret)
            raise
        return secret
    raise AppError("本地密钥存储目前支持 Windows 和 macOS；其他系统请使用环境变量")


def open_secret(secret: dict[str, str]) -> str:
    kind = secret.get("kind", "")
    value = secret.get("data", "")
    if kind == "windows-dpapi" and sys.platform == "win32":
        try:
            encrypted = base64.b64decode(value, validate=True)
            return _dpapi(encrypted, decrypt=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError, AppError) as exc:
            raise AppError("无法读取已保存的密钥，请解锁系统凭据存储，或重新配置") from exc
    if kind == "macos-keychain" and sys.platform == "darwin":
        try:
            result = subprocess.run(
                [
                    "/usr/bin/security",
                    "find-generic-password",
                    "-a",
                    value,
                    "-s",
                    KEYCHAIN_SERVICE,
                    "-w",
                ],
                capture_output=True,
                text=True,
                timeout=60,
                check=True,
            )
            return result.stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:
            raise AppError("无法读取 macOS 钥匙串，请解锁后重试") from exc
    raise AppError("不支持的本地密钥存储格式，请重新配置")


def delete_secret(secret: dict[str, str]) -> None:
    if sys.platform == "darwin" and secret.get("kind") == "macos-keychain":
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                [
                    "/usr/bin/security",
                    "delete-generic-password",
                    "-a",
                    secret.get("data", ""),
                    "-s",
                    KEYCHAIN_SERVICE,
                ],
                capture_output=True,
                timeout=5,
                check=False,
            )


class ConfigStore:
    def __init__(self, path: Path | None = None):
        self.path = path or config_path()

    def resolve(self, require_key: bool = True) -> Settings:
        try:
            data = self.path.read_bytes()
        except FileNotFoundError:
            key = os.environ.get("CODEX_API_KEY", "").strip()
            base = os.environ.get("CODEX_API_URL", "").strip()
            if not key:
                if require_key:
                    raise AppError(
                        "尚未配置图片服务。请运行 configure，在本地配置页填写 API 地址和密钥"
                    )
                return Settings(api_url=normalize_api_base(base) if base else "")
            if not base:
                raise AppError(
                    "检测到 CODEX_API_KEY，但未设置 CODEX_API_URL；为避免把密钥发送到错误服务，请同时设置两者，或运行 configure"
                )
            api_url = normalize_api_base(base)
            validate_key(key)
            return Settings(api_url=api_url, api_key=key, source="environment", storage="environment")
        except OSError as exc:
            raise AppError("无法读取本地配置，请检查用户配置目录的访问权限") from exc
        try:
            if len(data) > 64 * 1024:
                raise ValueError("too large")
            config = json.loads(data)
            if (
                config.get("version") != 1
                or not isinstance(config.get("api_url"), str)
                or not isinstance(config.get("secret"), dict)
                or not config["secret"].get("data")
            ):
                raise ValueError("invalid config")
            api_url = normalize_api_base(config["api_url"])
            secret = config["secret"]
        except (ValueError, TypeError, json.JSONDecodeError, AppError) as exc:
            raise AppError("本地配置损坏，请运行 configure 重新保存配置") from exc
        settings = Settings(
            api_url=api_url,
            source="local",
            storage=str(secret.get("kind", "")),
        )
        if require_key:
            settings.api_key = open_secret(secret)
            validate_key(settings.api_key)
        return settings

    def save(self, api_url: str, key: str) -> None:
        base = normalize_api_base(api_url)
        key = key.strip()
        validate_key(key)
        secret = seal_secret(key)
        committed = False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(
                {"version": 1, "api_url": base, "secret": secret},
                ensure_ascii=False,
                indent=2,
            )
            descriptor, temporary = tempfile.mkstemp(prefix=".config-", dir=self.path.parent)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(payload + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                if os.name == "nt":
                    os.chmod(temporary, 0o600)
                os.replace(temporary, self.path)
                committed = True
            finally:
                with contextlib.suppress(OSError):
                    os.unlink(temporary)
        except OSError as exc:
            raise AppError("无法保存配置，请检查用户配置目录的权限") from exc
        finally:
            if not committed:
                delete_secret(secret)

    def status(self) -> dict[str, Any]:
        try:
            settings = self.resolve(require_key=True)
            result = {
                "configured": True,
                "source": settings.source,
                "api_url": settings.api_url,
                "storage": settings.storage,
                "config_path": str(self.path),
            }
        except AppError as exc:
            try:
                settings = self.resolve(require_key=False)
                source = settings.source
                api_url = settings.api_url
                storage = settings.storage
            except AppError:
                source, api_url, storage = "local", "", ""
            result = {
                "configured": False,
                "source": source,
                "api_url": api_url,
                "storage": storage,
                "config_path": str(self.path),
                "message": str(exc),
            }
        return result


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class SafeImageRedirect(urllib.request.HTTPRedirectHandler):
    """跟随图片下载跳转，但禁止把 API 凭据带到其他源。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urlsplit(newurl)
        if (
            target.scheme not in {"http", "https"}
            or not target.hostname
            or target.username
            or target.password
            or (target.scheme == "http" and not is_loopback_host(target.hostname))
        ):
            return None
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is None:
            return None
        source = urllib.parse.urlsplit(req.full_url)
        source_origin = (source.scheme, source.hostname, source.port)
        target_origin = (target.scheme, target.hostname, target.port)
        if source_origin != target_origin:
            redirected.remove_header("Authorization")
        return redirected


def diagnostic_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(NoRedirect())


def safe_display_text(value: str, limit: int = 1000) -> str:
    cleaned = "".join(
        char
        for char in value
        if unicodedata.category(char) not in {"Cc", "Cf", "Cs", "Co", "Cn"}
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:limit] + ("…" if len(cleaned) > limit else "")


def request_bytes(
    request: urllib.request.Request,
    timeout: float,
    opener: urllib.request.OpenerDirector | None = None,
) -> tuple[int, bytes]:
    try:
        response = (opener or urllib.request.build_opener()).open(request, timeout=timeout)
        with response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise AppError("服务返回的数据超过大小限制")
            return int(response.status), raw
    except urllib.error.HTTPError as exc:
        with exc:
            raw = exc.read(1024 * 1024)
        return int(exc.code), raw
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
        raise AppError("API 请求失败：网络错误或超时") from exc


def fetch_models(
    base: str, key: str, timeout: float = 20
) -> tuple[list[str] | None, int, str | None]:
    request = urllib.request.Request(
        base + "/models",
        headers={"Authorization": "Bearer " + key, "Accept": "application/json"},
        method="GET",
    )
    try:
        status, raw = request_bytes(request, timeout, diagnostic_opener())
    except AppError:
        return None, 0, "network"
    if not 200 <= status < 300:
        return None, status, None
    try:
        payload = json.loads(raw)
        data = payload["data"]
        if not isinstance(data, list):
            raise ValueError
        ids = [item["id"] for item in data if isinstance(item, dict) and isinstance(item.get("id"), str)]
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None, status, "invalid"
    return ids, status, None


def is_image_model(model: str) -> bool:
    return model.strip().lower().startswith("gpt-image-")


def display_model_ids(ids: Iterable[str], key: str) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in ids:
        model = safe_display_text(item, 256)
        if (
            not model
            or len(model) > 256
            or (key and (model == key or (len(key) >= 8 and key in model)))
            or model in seen
        ):
            continue
        seen.add(model)
        result.append(model)

    def priority(model: str) -> tuple[int, str]:
        if model == PREFERRED_MODEL:
            return 0, model
        if model == DEFAULT_MODEL:
            return 1, model
        if is_image_model(model):
            return 2, model
        return 3, model

    return sorted(result, key=priority)


def listed_image_model(ids: Iterable[str]) -> str:
    values = list(ids)
    if PREFERRED_MODEL in values:
        return PREFERRED_MODEL
    return next((model for model in values if is_image_model(model)), DEFAULT_MODEL)


class ModelSelector:
    def __init__(self):
        self._cache: dict[str, str] = {}
        self._lock = threading.Lock()

    def select(self, settings: Settings, requested: str, dry_run: bool = False) -> str:
        if requested and requested != "auto":
            return requested
        if dry_run:
            return DEFAULT_MODEL
        cache_key = hashlib.sha256(
            (settings.api_url + "\0" + settings.api_key).encode("utf-8")
        ).hexdigest()
        with self._lock:
            if cache_key in self._cache:
                return self._cache[cache_key]
        ids, status, error = fetch_models(settings.api_url, settings.api_key, 8)
        model = (
            listed_image_model(ids or [])
            if error is None and ids is not None and 200 <= status < 300
            else DEFAULT_MODEL
        )
        with self._lock:
            self._cache[cache_key] = model
        return model


def endpoint(base: str, operation: str) -> str:
    return normalize_api_base(base) + "/images/" + operation


def sanitize_provider_error(status: int, raw: bytes, key: str) -> str:
    text = raw.decode("utf-8", "replace")[:4096]
    try:
        payload = json.loads(text)
        error = payload.get("error", {})
        if isinstance(error, dict):
            message = error.get("message") or error.get("reason") or ""
            code = error.get("code") or error.get("type") or ""
            request_id = error.get("request_id") or payload.get("request_id") or ""
            pieces = [str(part) for part in (message, code) if part]
            if request_id:
                pieces.append("request_id=" + str(request_id))
            text = "；".join(pieces)
    except (ValueError, TypeError, AttributeError):
        pass
    if key:
        text = text.replace(key, "[已隐藏]")
    text = safe_display_text(text, 1000)
    return f"API 请求失败（HTTP {status}）" + (f"：{text}" if text else "")


def send_with_retries(
    url: str,
    key: str,
    payload: bytes,
    content_type: str,
    timeout: float,
    max_attempts: int,
) -> bytes:
    last_error = "API 请求失败"
    for attempt in range(1, max_attempts + 1):
        request = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Authorization": "Bearer " + key,
                "Content-Type": content_type,
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            status, raw = request_bytes(request, timeout, diagnostic_opener())
        except AppError as exc:
            last_error = str(exc)
            if attempt == max_attempts:
                raise
        else:
            if 200 <= status < 300:
                return raw
            last_error = sanitize_provider_error(status, raw, key)
            if status not in RETRYABLE or attempt == max_attempts:
                raise AppError(last_error)
        time.sleep(0.75 * (2 ** (attempt - 1)))
    raise AppError(last_error)


def download_image_url(
    address: str,
    timeout: float,
    api_url: str = "",
    api_key: str = "",
) -> bytes:
    parsed = urllib.parse.urlsplit(address)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or (parsed.scheme == "http" and not is_loopback_host(parsed.hostname or ""))
    ):
        raise AppError("API 返回了无效的图片链接")
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/132.0.0.0 Safari/537.36"
        ),
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
    }
    if api_url and api_key:
        api_host = urllib.parse.urlsplit(api_url).hostname
        if api_host and parsed.hostname == api_host:
            headers["Authorization"] = "Bearer " + api_key
    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cookie_jar),
        SafeImageRedirect(),
    )
    request = urllib.request.Request(address, headers=headers, method="GET")
    try:
        status, downloaded = request_bytes(request, timeout, opener)
    except AppError as exc:
        raise AppError("无法下载 API 返回的图片链接（网络错误或超时）") from exc
    if not 200 <= status < 300:
        host = parsed.hostname or "未知主机"
        raise AppError(f"无法下载 API 返回的图片链接（HTTP {status}，主机 {host}）")
    if not downloaded:
        raise AppError("API 返回的图片下载结果为空")
    return downloaded


def detect_image_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def validate_image_data(data: bytes) -> str:
    if not data:
        raise AppError("API 返回的图片数据为空")
    mime = detect_image_mime(data)
    if not mime:
        raise AppError("API 返回的数据不是支持的 PNG、JPEG 或 WebP 图片")
    return mime


def decode_images(
    raw: bytes,
    timeout: float,
    api_url: str = "",
    api_key: str = "",
) -> list[bytes]:
    try:
        payload = json.loads(raw)
        data = payload["data"]
        if not isinstance(data, list) or not data:
            raise ValueError
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise AppError("API 返回了无效的图片数据") from exc
    images: list[bytes] = []
    for item in data:
        if not isinstance(item, dict):
            raise AppError("API 返回了无效的图片条目")
        encoded = item.get("b64_json")
        if isinstance(encoded, str) and encoded:
            try:
                image = base64.b64decode(encoded, validate=True)
            except ValueError as exc:
                raise AppError("API 返回了无效的 Base64 图片数据") from exc
            validate_image_data(image)
            images.append(image)
            continue
        address = item.get("url")
        if isinstance(address, str) and address:
            # 图片 CDN 经常用 302 跳转到实际下载地址。这里的跳转只用于无鉴权
            # 的图片下载，不能复用禁止跳转的诊断客户端。
            image = download_image_url(
                address, timeout, api_url=api_url, api_key=api_key
            )
            validate_image_data(image)
            images.append(image)
            continue
        raise AppError("API 图片条目缺少 b64_json 或 url")
    return images


NUMBERED_IMAGE_RE = re.compile(r"^(\d+)\.\s+.+\.(?:png|jpe?g|webp)$", re.IGNORECASE)


def sanitize_output_name(value: str) -> str:
    name = re.sub(r"^\s*\d+\.\s*", "", value.strip())
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "-", name)
    name = re.sub(r"\s+", " ", name).strip(" .-")
    if len(name) > 120:
        name = name[:120].rstrip(" .-")
    return name or "图片"


def next_sequence_number(directory: Path) -> int:
    highest = 0
    try:
        for path in directory.iterdir():
            match = NUMBERED_IMAGE_RE.fullmatch(path.name)
            if match and path.is_file():
                highest = max(highest, int(match.group(1)))
    except FileNotFoundError:
        pass
    return highest + 1


def output_paths(
    out: str,
    out_dir: str,
    prompt: str,
    count: int,
    name: str = "",
) -> list[Path]:
    if out:
        path = Path(out)
        if path.parent == Path("."):
            path = Path(out_dir) / path
        if not path.suffix:
            path = path.with_suffix(".png")
        if count == 1:
            return [path]
        return [
            path.with_name(f"{path.stem}-{index + 1}{path.suffix}")
            for index in range(count)
        ]

    directory = Path(out_dir)
    first = next_sequence_number(directory)
    label = sanitize_output_name(name or prompt[:40])
    return [
        directory / f"{first + index}. {label}.png"
        for index in range(count)
    ]


def check_outputs(paths: Iterable[Path], force: bool) -> None:
    if force:
        return
    for path in paths:
        if path.exists():
            raise AppError(f"拒绝覆盖已有文件：{path}")


def save_images(images: list[bytes], paths: list[Path]) -> list[str]:
    if len(images) != len(paths):
        raise AppError(f"API 返回 {len(images)} 张图片，预期 {len(paths)} 张")
    outputs: list[str] = []
    for image, path in zip(images, paths):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(image)
        outputs.append(str(path.resolve()))
    return outputs


def align_image_extensions(paths: list[Path], images: list[bytes]) -> list[Path]:
    extensions = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
    }
    return [
        path.with_suffix(extensions[validate_image_data(image)])
        for path, image in zip(paths, images)
    ]


def validate_common(args: argparse.Namespace) -> None:
    if not 1 <= args.n <= 10:
        raise AppError("--n 必须在 1 到 10 之间")
    if args.max_attempts < 1:
        raise AppError("--max-attempts 至少为 1")
    if args.timeout <= 0:
        raise AppError("--timeout 必须大于 0")
    if args.quality not in {"low", "medium", "high", "auto"}:
        raise AppError("--quality 必须是 low、medium、high 或 auto")
    if args.size != "auto" and not re.fullmatch(r"[1-9]\d*x[1-9]\d*", args.size):
        raise AppError("--size 必须是 auto 或 宽x高")


def generation_payload(model: str, prompt: str, args: argparse.Namespace) -> dict[str, Any]:
    return {
        "model": model,
        "prompt": prompt,
        "size": args.size,
        "quality": args.quality,
        "n": args.n,
    }


def generate(
    prompt: str,
    out: str,
    args: argparse.Namespace,
    selector: ModelSelector,
) -> dict[str, Any]:
    validate_common(args)
    automatic_output = not out
    paths = getattr(args, "preassigned_paths", None) or output_paths(
        out,
        args.out_dir,
        prompt,
        args.n,
        getattr(args, "name", ""),
    )
    check_outputs(paths, args.force)
    store = ConfigStore()
    settings = store.resolve(require_key=not args.dry_run)
    selected = selector.select(settings, args.model, args.dry_run)
    payload = generation_payload(selected, prompt, args)
    target = endpoint(settings.api_url, "generations")
    if args.dry_run:
        return {
            "dry_run": True,
            "endpoint": target,
            "payload": payload,
            "outputs": [str(path) for path in paths],
            "model_selection": args.model,
        }
    raw = send_with_retries(
        target,
        settings.api_key,
        json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        "application/json",
        args.timeout,
        args.max_attempts,
    )
    images = decode_images(
        raw,
        args.timeout,
        api_url=settings.api_url,
        api_key=settings.api_key,
    )
    if automatic_output:
        paths = align_image_extensions(paths, images)
        check_outputs(paths, args.force)
    outputs = save_images(images, paths)
    return {
        "model": selected,
        "size": args.size,
        "quality": args.quality,
        "outputs": outputs,
    }


def multipart_payload(
    fields: dict[str, str], files: list[tuple[str, Path]]
) -> tuple[bytes, str]:
    boundary = "----CodexImage2" + secrets.token_hex(16)
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode("utf-8")
        )
    for name, path in files:
        filename = path.name.replace('"', "_").replace("\r", "_").replace("\n", "_")
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        chunks.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                f"Content-Type: {mime}\r\n\r\n"
            ).encode("utf-8")
        )
        chunks.append(path.read_bytes())
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(chunks), "multipart/form-data; boundary=" + boundary


def edit(
    prompt: str,
    image_paths: list[str],
    mask: str,
    out: str,
    args: argparse.Namespace,
    selector: ModelSelector,
) -> dict[str, Any]:
    validate_common(args)
    automatic_output = not out
    input_paths = [Path(path) for path in image_paths]
    for path in input_paths + ([Path(mask)] if mask else []):
        if not path.is_file():
            raise AppError(f"找不到输入文件：{path}")
    paths = getattr(args, "preassigned_paths", None) or output_paths(
        out,
        args.out_dir,
        prompt,
        args.n,
        getattr(args, "name", ""),
    )
    check_outputs(paths, args.force)
    store = ConfigStore()
    settings = store.resolve(require_key=not args.dry_run)
    selected = selector.select(settings, args.model, args.dry_run)
    fields = {
        "model": selected,
        "prompt": prompt,
        "size": args.size,
        "quality": args.quality,
        "n": str(args.n),
    }
    target = endpoint(settings.api_url, "edits")
    if args.dry_run:
        return {
            "dry_run": True,
            "endpoint": target,
            "fields": fields,
            "images": image_paths,
            "mask": mask,
            "outputs": [str(path) for path in paths],
            "model_selection": args.model,
        }
    files = [("image", path) for path in input_paths]
    if mask:
        files.append(("mask", Path(mask)))
    payload, content_type = multipart_payload(fields, files)
    raw = send_with_retries(
        target,
        settings.api_key,
        payload,
        content_type,
        args.timeout,
        args.max_attempts,
    )
    images = decode_images(
        raw,
        args.timeout,
        api_url=settings.api_url,
        api_key=settings.api_key,
    )
    if automatic_output:
        paths = align_image_extensions(paths, images)
        check_outputs(paths, args.force)
    outputs = save_images(images, paths)
    return {
        "model": selected,
        "size": args.size,
        "quality": args.quality,
        "outputs": outputs,
    }


def read_prompt(prompt: str, prompt_file: str) -> str:
    if bool(prompt) == bool(prompt_file):
        raise AppError("请且只提供 --prompt 或 --prompt-file 之一")
    if prompt_file:
        try:
            prompt = Path(prompt_file).read_text(encoding="utf-8")
        except OSError as exc:
            raise AppError(f"无法读取提示词文件：{prompt_file}") from exc
    prompt = prompt.strip()
    if not prompt:
        raise AppError("提示词不能为空")
    return prompt


def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--model",
        default="auto",
        help="完整模型 ID；auto 表示优先使用列表中的 gpt-image 模型",
    )
    parser.add_argument("--size", default=DEFAULT_SIZE, help="auto 或 宽x高")
    parser.add_argument(
        "--quality",
        default=DEFAULT_QUALITY,
        choices=("low", "medium", "high", "auto"),
    )
    parser.add_argument("--n", type=int, default=1, help="变体数量（1-10）")
    parser.add_argument(
        "--name",
        default="",
        help="按画面重点命名的文件名，不含编号和扩展名",
    )
    parser.add_argument(
        "--out-dir",
        default=DEFAULT_OUT_DIR,
        help="默认输出目录；默认为当前工作目录",
    )
    parser.add_argument("--force", action="store_true", help="覆盖已有输出")
    parser.add_argument("--dry-run", action="store_true", help="不联网，只校验请求")
    parser.add_argument("--max-attempts", type=int, default=3, help="最大尝试次数")
    parser.add_argument("--timeout", type=float, default=150, help="请求超时秒数")


def parse_generate(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="codex-image2.py generate", allow_abbrev=False)
    add_common_arguments(parser)
    parser.add_argument("--prompt", default="")
    parser.add_argument("--prompt-file", default="")
    parser.add_argument("--out", default="")
    return parser.parse_args(argv)


def parse_edit(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="codex-image2.py edit", allow_abbrev=False)
    add_common_arguments(parser)
    parser.add_argument("--image", action="append", default=[])
    parser.add_argument("--mask", default="")
    parser.add_argument("--prompt", default="")
    parser.add_argument("--prompt-file", default="")
    parser.add_argument("--out", default="")
    return parser.parse_args(argv)


def parse_batch(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="codex-image2.py generate-batch", allow_abbrev=False)
    add_common_arguments(parser)
    parser.add_argument("--input", required=True)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--fail-fast", action="store_true")
    return parser.parse_args(argv)


def run_generate(argv: list[str]) -> None:
    args = parse_generate(argv)
    prompt = read_prompt(args.prompt, args.prompt_file)
    print_json(generate(prompt, args.out, args, ModelSelector()))


def run_edit(argv: list[str]) -> None:
    args = parse_edit(argv)
    if not args.image:
        raise AppError("请至少提供一个 --image")
    prompt = read_prompt(args.prompt, args.prompt_file)
    print_json(edit(prompt, args.image, args.mask, args.out, args, ModelSelector()))


def read_batch(path: str) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    job = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise AppError("每一行 JSONL 都必须是合法 JSON") from exc
                if not isinstance(job, dict) or not str(job.get("prompt", "")).strip():
                    raise AppError("每个 JSONL 任务都必须包含非空 prompt")
                unknown = set(job) - BATCH_JOB_FIELDS
                if unknown:
                    raise AppError(
                        "JSONL 任务包含未知字段："
                        + "、".join(sorted(str(value) for value in unknown))
                    )
                jobs.append(job)
    except OSError as exc:
        raise AppError(f"无法读取批量输入：{path}") from exc
    if not jobs:
        raise AppError("批量输入中没有有效任务")
    return jobs


def batch_job_args(base: argparse.Namespace, job: dict[str, Any]) -> argparse.Namespace:
    values = vars(base).copy()
    for name in ("model", "name", "size", "quality", "n"):
        if name in job and job[name] not in (None, ""):
            values[name] = job[name]
    for name in ("model", "name", "size", "quality"):
        if not isinstance(values[name], str):
            raise AppError(f"JSONL 字段 {name} 必须是字符串")
    if isinstance(values["n"], bool):
        raise AppError("JSONL 字段 n 必须是 1 到 10 的整数")
    try:
        values["n"] = int(values["n"])
    except (TypeError, ValueError) as exc:
        raise AppError("JSONL 字段 n 必须是 1 到 10 的整数") from exc
    values.pop("preassigned_paths", None)
    result = argparse.Namespace(**values)
    validate_common(result)
    return result


def run_batch(argv: list[str]) -> None:
    args = parse_batch(argv)
    if args.concurrency < 1:
        raise AppError("--concurrency 至少为 1")
    jobs = read_batch(args.input)
    selector = ModelSelector()
    results: list[dict[str, Any]] = [
        {
            "index": index + 1,
            "ok": False,
            "skipped": True,
            "error": "前序任务失败，未执行",
        }
        for index in range(len(jobs))
    ]
    failed = False
    lock = threading.Lock()
    out_dir = Path(args.out_dir)
    next_number = next_sequence_number(out_dir)

    prepared: list[tuple[argparse.Namespace, str]] = []
    reserved_outputs: set[Path] = set()
    for job in jobs:
        job_args = batch_job_args(args, job)
        out_value = job.get("out", "")
        if not isinstance(out_value, str):
            raise AppError("JSONL 字段 out 必须是字符串")
        if not out_value:
            label = sanitize_output_name(str(job_args.name or job["prompt"]))
            job_args.preassigned_paths = [
                out_dir / f"{next_number + index}. {label}.png"
                for index in range(job_args.n)
            ]
            next_number += job_args.n
            planned_paths = job_args.preassigned_paths
        else:
            planned_paths = output_paths(
                out_value,
                job_args.out_dir,
                str(job["prompt"]),
                job_args.n,
                job_args.name,
            )
        for path in planned_paths:
            resolved = path.resolve()
            if resolved in reserved_outputs:
                raise AppError(f"批量任务包含重复输出路径：{path}")
            reserved_outputs.add(resolved)
        check_outputs(planned_paths, args.force)
        prepared.append((job_args, out_value))

    def execute(index: int, job: dict[str, Any], job_args: argparse.Namespace, out: str) -> None:
        nonlocal failed
        with lock:
            if args.fail_fast and failed:
                return
        try:
            result = generate(
                str(job["prompt"]).strip(),
                out,
                job_args,
                selector,
            )
            results[index] = {
                "index": index + 1,
                "ok": True,
                "skipped": False,
                "data": result,
            }
        except AppError as exc:
            with lock:
                failed = True
            results[index] = {
                "index": index + 1,
                "ok": False,
                "skipped": False,
                "error": str(exc),
            }

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = [
            executor.submit(execute, index, job, prepared[index][0], prepared[index][1])
            for index, job in enumerate(jobs)
        ]
        for future in futures:
            future.result()
    succeeded = sum(1 for item in results if item.get("ok"))
    failures = sum(
        1
        for item in results
        if not item.get("ok") and not item.get("skipped")
    )
    skipped = sum(1 for item in results if item.get("skipped"))
    print_json(
        {
            "jobs": results,
            "succeeded": succeeded,
            "failed": failures,
            "skipped": skipped,
        }
    )
    if failures:
        raise AppError("一个或多个批量任务失败")


def run_models(argv: list[str]) -> None:
    if argv:
        raise AppError("models 不需要参数")
    settings = ConfigStore().resolve(require_key=True)
    ids, status, error = fetch_models(settings.api_url, settings.api_key)
    if error == "network":
        raise AppError("无法读取服务商模型列表，请检查网络和 API 地址")
    if not 200 <= status < 300:
        raise AppError(f"服务商模型列表请求失败（HTTP {status}）")
    if error:
        raise AppError("服务商返回的模型列表格式不兼容")
    models = display_model_ids(ids or [], settings.api_key)
    image_models = [model for model in models if is_image_model(model)]
    print_json(
        {
            "api_url": settings.api_url,
            "models": models,
            "image_models": image_models,
            "selection_required": True,
        }
    )


def run_status(argv: list[str]) -> None:
    if argv:
        raise AppError("status 不需要参数")
    print_json(ConfigStore().status())


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8")


def print_json(value: Any) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def http_result(status: int) -> dict[str, Any]:
    message = f"服务返回 HTTP {status}，请检查服务商的接口说明"
    level = "error"
    if status == 401:
        message = "密钥未通过验证，请确认密钥有效且属于当前服务"
    elif status == 403:
        message = "服务拒绝访问，请检查密钥权限或联系服务商"
    elif status == 402:
        message = "服务要求付费，请检查账户余额或联系服务商"
    elif status in {404, 405, 501}:
        message = "服务未提供此检测接口，仍需通过实际生图确认图片能力"
        level = "warning"
    elif status == 429:
        message = "请求过于频繁或额度受限，请稍后重试并检查服务额度"
    elif status in {301, 302, 303, 307, 308}:
        message = "此地址发生跳转。为保护密钥未继续请求，请填写服务商的最终 API 地址"
    elif status >= 500:
        message = "服务商暂时出错，请稍后重试"
    return {"ok": False, "level": level, "message": message}


def check_connection(base: str, key: str) -> dict[str, Any]:
    ids, status, error = fetch_models(base, key)
    if error == "network":
        return {"ok": False, "level": "error", "message": "无法连接服务，请检查网络、代理或 API 地址"}
    if not 200 <= status < 300:
        return http_result(status)
    if error or ids is None:
        return {
            "ok": False,
            "level": "warning",
            "message": "地址可以访问，但没有返回兼容的模型列表。请确认 API 地址，并通过实际生图验证",
        }
    models = display_model_ids(ids, key)
    image_models = [model for model in models if is_image_model(model)]
    if not image_models:
        return {
            "ok": False,
            "level": "warning",
            "message": "当前密钥的模型列表未包含可识别的 gpt-image-* 型号。请检查图片模型权限；部分服务可能隐藏图片模型。",
            "models": models,
            "models_checked": True,
        }
    model = listed_image_model(image_models)
    return {
        "ok": True,
        "level": "success",
        "message": f"模型列表接口访问成功，已发现图片模型 {model}。实际生图仍以图片接口结果为准。",
        "model": model,
        "models": models,
        "models_checked": True,
    }


def check_image(base: str, key: str) -> dict[str, Any]:
    settings = Settings(api_url=base, api_key=key)
    model = ModelSelector().select(settings, "auto")
    payload = json.dumps(
        {
            "model": model,
            "prompt": "A small blue circle on a plain white background.",
            "size": "1024x1024",
            "quality": "low",
            "n": 1,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        endpoint(base, "generations"),
        data=payload,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        status, raw = request_bytes(request, 150, diagnostic_opener())
    except AppError:
        return {"ok": False, "level": "error", "message": "无法连接图片接口，请检查网络或代理"}
    if not 200 <= status < 300:
        result = http_result(status)
        if status == 404:
            result = {
                "ok": False,
                "level": "error",
                "message": "找不到图片生成接口，请检查 API 地址和服务商的图片接口支持情况",
            }
        return result
    try:
        payload_value = json.loads(raw)
        first = payload_value["data"][0]
    except (ValueError, TypeError, KeyError, IndexError, json.JSONDecodeError):
        return {"ok": False, "level": "error", "message": "图片接口未返回兼容的图片数据"}
    encoded = first.get("b64_json") if isinstance(first, dict) else None
    if encoded:
        try:
            data = base64.b64decode(encoded, validate=True)
        except ValueError:
            return {"ok": False, "level": "error", "message": "服务返回的图片编码无效"}
        mime = detect_image_mime(data)
        if mime:
            return {
                "ok": True,
                "level": "success",
                "message": f"测试生图成功，使用模型：{model}。",
                "model": model,
                "image": f"data:{mime};base64,{encoded}",
            }
        return {"ok": False, "level": "error", "message": "服务返回的数据不是支持的图片格式"}
    if isinstance(first, dict) and first.get("url"):
        return {
            "ok": True,
            "level": "success",
            "message": f"图片接口已返回图片链接，使用模型：{model}。可以返回 Codex 正式生图。",
            "model": model,
        }
    return {"ok": False, "level": "error", "message": "图片接口未返回图片数据"}


class ConfigurationHandler(BaseHTTPRequestHandler):
    server_version = "CodexImage2"
    sys_version = ""
    timeout = 10

    def log_message(self, format, *args):
        return

    @property
    def ui(self) -> "ConfigurationServer":
        return self.server  # type: ignore[return-value]

    def send_json(self, status: int, value: Any) -> None:
        data = json_bytes(value)
        self.send_response(status)
        self.common_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def common_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")

    def do_GET(self) -> None:
        if self.headers.get("Host") != self.ui.host:
            self.send_error(403, "无效的本机地址")
            return
        if self.path == "/" and self.command == "GET":
            nonce = random_id()
            try:
                html = CONFIGURE_HTML.read_text(encoding="utf-8").replace("{{NONCE}}", nonce)
            except OSError:
                self.send_error(500, "找不到配置页面")
                return
            data = html.encode("utf-8")
            self.send_response(200)
            self.common_headers()
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; script-src 'nonce-"
                + nonce
                + "'; style-src 'nonce-"
                + nonce
                + "'; connect-src 'self'; img-src data:; base-uri 'none'; "
                "frame-ancestors 'none'; form-action 'none'",
            )
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if not self.path.startswith("/api/"):
            self.send_error(404)
            return
        if not self.ui.authorized(self.headers.get("Authorization", "")):
            self.send_json(401, {"message": "配置链接无效或已过期，请回到 Codex 重新打开配置页"})
            return
        if self.command == "GET" and self.path == "/api/status":
            self.send_json(200, self.ui.store.status())
            return
        self.send_error(405, "不支持此操作")

    def do_POST(self) -> None:
        if self.headers.get("Host") != self.ui.host:
            self.send_error(403, "无效的本机地址")
            return
        if not self.path.startswith("/api/"):
            self.send_error(404)
            return
        if not self.ui.authorized(self.headers.get("Authorization", "")):
            self.send_json(401, {"message": "配置链接无效或已过期，请回到 Codex 重新打开配置页"})
            return
        if self.headers.get("Origin") != "http://" + self.ui.host or not self.headers.get(
            "Content-Type", ""
        ).lower().startswith("application/json"):
            self.send_json(403, {"message": "请使用本机配置页提交"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length < 1 or length > 8192:
            self.send_json(400, {"message": "配置内容格式不正确或过长"})
            return
        try:
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError
        except (ValueError, json.JSONDecodeError):
            self.send_json(400, {"message": "配置内容格式不正确或过长"})
            return
        if self.path == "/api/close":
            self.send_json(200, {"message": "配置页已关闭，可以返回 Codex"})
            self.ui.stop()
            return
        if self.path == "/api/reveal-key":
            try:
                with self.ui.lock:
                    current = self.ui.store.resolve(require_key=True)
            except AppError as exc:
                self.send_json(400, {"message": str(exc)})
                return
            self.send_json(200, {"api_key": current.api_key, "api_url": current.api_url})
            return
        allowed = {"api_url", "api_key", "confirm_paid", "confirm_unverified"}
        if set(payload) - allowed:
            self.send_json(400, {"message": "配置内容包含未知字段"})
            return
        try:
            base = normalize_api_base(str(payload.get("api_url", "")))
            key = str(payload.get("api_key", "")).strip()
            if not key:
                with self.ui.lock:
                    current = self.ui.store.resolve(require_key=True)
                if current.api_url != base:
                    raise AppError("切换 API 地址时，请重新粘贴对应服务的密钥")
                key = current.api_key
            validate_key(key)
        except AppError as exc:
            self.send_json(400, {"message": str(exc)})
            return
        if self.path == "/api/save":
            result = check_connection(base, key)
            result["api_url"] = base
            if result["level"] == "error":
                result["message"] += "。配置未保存，原配置保持不变。"
                self.send_json(422, result)
                return
            if not result["ok"] and not payload.get("confirm_unverified"):
                result["requires_confirmation"] = True
                result["message"] += (
                    "\n配置尚未保存。请检查密钥的模型权限；确认服务支持图片接口后，"
                    "可以选择“仍然保存”。"
                )
                self.send_json(200, result)
                return
            try:
                with self.ui.lock:
                    self.ui.store.save(base, key)
            except AppError as exc:
                self.send_json(400, {"message": str(exc)})
                return
            result["saved"] = True
            if result["ok"]:
                result["message"] = (
                    f"配置已保存，将使用 {result['model']}。实际生图仍以图片接口结果为准，"
                    "无需重启 Codex。"
                )
            else:
                result["message"] = "配置已按你的选择保存，但尚未确认生图模型权限。\n" + result[
                    "message"
                ]
            self.send_json(200, result)
            return
        if self.path == "/api/test":
            self.send_json(200, check_connection(base, key))
            return
        if self.path == "/api/test-image":
            if not payload.get("confirm_paid"):
                self.send_json(400, {"message": "请先确认生图测试可能产生费用"})
                return
            self.send_json(200, check_image(base, key))
            return
        self.send_error(404)


class ConfigurationServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], store: ConfigStore):
        super().__init__(address, ConfigurationHandler)
        self.store = store
        self.host = f"{address[0]}:{self.server_address[1]}"
        self.token = random_id()
        self.lock = threading.Lock()

    def authorized(self, value: str) -> bool:
        return secrets.compare_digest(value, "Bearer " + self.token)

    def stop(self) -> None:
        threading.Thread(target=self.shutdown, daemon=True).start()


def open_browser(url: str) -> bool:
    try:
        if sys.platform == "win32":
            os.startfile(url)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(
                ["/usr/bin/open", url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        else:
            program = shutil.which("xdg-open")
            if not program:
                return False
            subprocess.Popen(
                [program, url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        return True
    except OSError:
        return False


def serve_configuration() -> None:
    store = ConfigStore()
    server = ConfigurationServer(("127.0.0.1", 0), store)
    url = f"http://{server.host}/#{server.token}"
    print(json.dumps({"url": url, "expires_in_seconds": 900}), flush=True)
    sys.stdout.close()
    timer = threading.Timer(15 * 60, server.stop)
    timer.daemon = True
    timer.start()
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()


def run_configure(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="codex-image2.py configure", allow_abbrev=False)
    parser.add_argument("--no-browser", action="store_true", help="只输出配置页地址")
    args = parser.parse_args(argv)
    command = [sys.executable, str(Path(__file__).resolve()), "_configure-server"]
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.DEVNULL,
        "text": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen(command, **kwargs)
    result: dict[str, Any] = {}

    def read_ready() -> None:
        nonlocal result
        assert process.stdout is not None
        line = process.stdout.readline()
        if line:
            with contextlib.suppress(json.JSONDecodeError):
                value = json.loads(line)
                if isinstance(value, dict):
                    result = value

    reader = threading.Thread(target=read_ready, daemon=True)
    reader.start()
    reader.join(10)
    if not result.get("url"):
        process.terminate()
        raise AppError("配置页启动失败，请确认本机允许程序监听回环网络端口")
    if process.stdout:
        process.stdout.close()
    browser_opened = False if args.no_browser else open_browser(str(result["url"]))
    message = "请在本地配置页填写信息。保存后立即生效，无需重启 Codex；页面在 15 分钟后失效。"
    if not browser_opened:
        message += " 请用浏览器打开 url 字段中的地址。"
    print_json(
        {
            "url": result["url"],
            "expires_in_seconds": 900,
            "browser_opened": browser_opened,
            "message": message,
        }
    )


def usage() -> None:
    print(
        "Usage: codex-image2.py "
        "<configure|status|models|generate|generate-batch|edit> [options]",
        file=sys.stderr,
    )
    print(
        "configure: 打开本地中文配置页；status: 查看配置状态（不显示密钥）；"
        "models: 获取当前密钥可访问的模型",
        file=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    if not values:
        values = ["configure"]
    command, rest = values[0], values[1:]
    try:
        if command == "configure":
            run_configure(rest)
        elif command == "status":
            run_status(rest)
        elif command == "models":
            run_models(rest)
        elif command == "_configure-server":
            serve_configuration()
        elif command == "generate":
            run_generate(rest)
        elif command == "edit":
            run_edit(rest)
        elif command == "generate-batch":
            run_batch(rest)
        elif command in {"--help", "-h", "help"}:
            usage()
        else:
            raise AppError(f"未知命令：{command}")
        return 0
    except AppError as exc:
        print("error:", exc, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
