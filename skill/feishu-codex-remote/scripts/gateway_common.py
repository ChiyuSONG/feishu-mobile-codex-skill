from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import json
import mimetypes
import os
from pathlib import Path
import platform
import subprocess
import tempfile
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request


API_BASE = "https://open.feishu.cn/open-apis"


def user_data_root(system: str | None = None) -> Path:
    current = system or platform.system()
    if current == "Windows":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    if current == "Darwin":
        return Path.home() / "Library" / "Application Support"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))


PUBLISH_STATE = user_data_root() / "CodexFeishuPublish"
REMOTE_STATE = user_data_root() / "CodexFeishuRemote"
CONFIG_PATH = REMOTE_STATE / "config.json"
SECRET_PATH = PUBLISH_STATE / "secret.bin"
PUBLISH_CONFIG_PATH = PUBLISH_STATE / "config.json"
KEYCHAIN_SERVICE = "feishu-codex-remote"


class GatewayError(RuntimeError):
    pass


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes) -> tuple[DATA_BLOB, object]:
    buffer = ctypes.create_string_buffer(data)
    return DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def dpapi_unprotect(data: bytes) -> bytes:
    if os.name != "nt":
        raise GatewayError("DPAPI credential storage requires Windows")
    source, source_buffer = _blob(data)
    output = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(source), None, None, None, None, 0x1, ctypes.byref(output)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(output.pbData)
        del source_buffer


def dpapi_protect(data: bytes) -> bytes:
    if os.name != "nt":
        raise GatewayError("DPAPI credential storage requires Windows")
    source, source_buffer = _blob(data)
    output = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(source), "FeishuCodexRemote", None, None, None, 0x1, ctypes.byref(output)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(output.pbData)
        del source_buffer


def credential_backend(system: str | None = None) -> str:
    current = system or platform.system()
    if current == "Windows":
        return "Windows DPAPI"
    if current == "Darwin":
        return "macOS Keychain"
    raise GatewayError(f"Unsupported credential platform: {current}")


def _keychain_account(path: Path) -> str:
    return f"{path.parent.name}:{path.name}"


def save_protected(path: Path, data: bytes, *, system: str | None = None) -> None:
    current = system or platform.system()
    if current == "Windows":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64encode(dpapi_protect(data)))
        return
    if current == "Darwin":
        encoded = base64.b64encode(data).decode("ascii")
        result = subprocess.run(
            [
                "security",
                "add-generic-password",
                "-U",
                "-s",
                KEYCHAIN_SERVICE,
                "-a",
                _keychain_account(path),
                "-w",
                encoded,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise GatewayError("Could not save credentials in macOS Keychain")
        return
    raise GatewayError(f"Unsupported credential platform: {current}")


def load_protected(path: Path, *, system: str | None = None) -> bytes:
    current = system or platform.system()
    if current == "Windows":
        if not path.exists():
            raise GatewayError(f"Missing protected local credential: {path}")
        return dpapi_unprotect(base64.b64decode(path.read_bytes()))
    if current == "Darwin":
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                KEYCHAIN_SERVICE,
                "-a",
                _keychain_account(path),
                "-w",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise GatewayError("Missing credential in macOS Keychain")
        return base64.b64decode(result.stdout.strip())
    raise GatewayError(f"Unsupported credential platform: {current}")


def protected_exists(path: Path, *, system: str | None = None) -> bool:
    current = system or platform.system()
    if current == "Windows":
        return path.exists()
    if current == "Darwin":
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                KEYCHAIN_SERVICE,
                "-a",
                _keychain_account(path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return result.returncode == 0
    return False


def delete_protected(path: Path, *, system: str | None = None) -> None:
    current = system or platform.system()
    if current == "Windows":
        path.unlink(missing_ok=True)
        return
    if current == "Darwin":
        subprocess.run(
            [
                "security",
                "delete-generic-password",
                "-s",
                KEYCHAIN_SERVICE,
                "-a",
                _keychain_account(path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return
    raise GatewayError(f"Unsupported credential platform: {current}")


def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def load_config() -> dict[str, Any]:
    config = load_json(CONFIG_PATH, {})
    if not isinstance(config, dict):
        raise GatewayError(f"Invalid gateway config: {CONFIG_PATH}")
    return config


def load_app_credentials(config: dict[str, Any]) -> tuple[str, str]:
    publish_config = load_json(PUBLISH_CONFIG_PATH, {})
    app_id = str(config.get("app_id") or "").strip()
    if app_id != str(publish_config.get("app_id") or "").strip():
        raise GatewayError("Remote gateway app_id does not match the verified publisher app")
    if not protected_exists(SECRET_PATH):
        raise GatewayError(f"Missing protected Feishu App Secret ({credential_backend()})")
    secret = load_protected(SECRET_PATH).decode("utf-8")
    if not app_id.startswith("cli_") or len(secret.strip()) < 16:
        raise GatewayError("Invalid Feishu application credentials")
    return app_id, secret.strip()


def now_epoch() -> int:
    return int(time.time())


def parse_content(raw: Any) -> Any:
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"text": raw}


def iter_text(value: Any):
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"text", "content"} and isinstance(child, str):
                yield child
            else:
                yield from iter_text(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_text(child)


def message_text(raw: Any) -> str:
    parsed = parse_content(raw)
    if isinstance(parsed, dict) and isinstance(parsed.get("text"), str):
        return parsed["text"].strip()
    if isinstance(parsed, dict) and parsed.get("content_v2") is not None:
        parsed = parsed["content_v2"]
    return "\n".join(part.strip() for part in iter_text(parsed) if part.strip()).strip()


def resource_keys(raw: Any) -> list[tuple[str, str]]:
    parsed = parse_content(raw)
    found: set[tuple[str, str]] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            if isinstance(value.get("image_key"), str):
                found.add(("image", value["image_key"]))
            if isinstance(value.get("file_key"), str):
                found.add(("file", value["file_key"]))
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(parsed)
    return sorted(found)


class FeishuClient:
    def __init__(self, app_id: str, app_secret: str) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self._token = ""
        self._token_deadline = 0.0
        self._verified_tenant_key = ""

    def _open(self, request: urllib.request.Request):
        try:
            return urllib.request.urlopen(request, timeout=60)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise GatewayError(f"Feishu HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise GatewayError(f"Feishu network error: {exc.reason}") from exc

    def tenant_token(self, force: bool = False) -> str:
        if not force and self._token and time.monotonic() < self._token_deadline:
            return self._token
        body = json.dumps({"app_id": self.app_id, "app_secret": self.app_secret}).encode("utf-8")
        request = urllib.request.Request(
            f"{API_BASE}/auth/v3/tenant_access_token/internal/",
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with self._open(request) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if payload.get("code", 0) != 0 or not payload.get("tenant_access_token"):
            raise GatewayError(f"Failed to obtain tenant token: {payload}")
        self._token = str(payload["tenant_access_token"])
        expires = max(60, int(payload.get("expire", 7200)))
        self._token_deadline = time.monotonic() + expires - min(300, expires // 4)
        return self._token

    def request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        query = urllib.parse.urlencode(
            {key: value for key, value in (params or {}).items() if value is not None}
        )
        url = f"{API_BASE}{path}" + (f"?{query}" if query else "")
        data = None
        headers = {"Authorization": f"Bearer {self.tenant_token()}"}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        with self._open(request) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if payload.get("code", 0) != 0:
            raise GatewayError(f"Feishu API error at {path}: {payload}")
        return payload

    def upload_message_image(self, image_path: str | Path, allowed_root: str | Path) -> str:
        """Upload one explicit image, restricted to the bound project root."""
        if not self._verified_tenant_key:
            raise GatewayError("Refusing image upload before the Listener verifies the target tenant")
        root = Path(allowed_root).expanduser().resolve(strict=True)
        path = Path(image_path).expanduser().resolve(strict=True)
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise GatewayError(f"Refusing image outside the bound project: {path}") from exc
        if not path.is_file():
            raise GatewayError(f"Image is not a file: {path}")
        size = path.stat().st_size
        if size <= 0:
            raise GatewayError(f"Image is empty: {path}")
        if size > 10 * 1024 * 1024:
            raise GatewayError(f"Image exceeds Feishu's 10 MB message-image limit: {path}")

        boundary = "----CodexFeishuRemote" + os.urandom(12).hex()
        filename = path.name.replace('"', "_").replace("\r", "_").replace("\n", "_")
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        prefix = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="image_type"\r\n\r\n'
            "message\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="image"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("utf-8")
        suffix = f"\r\n--{boundary}--\r\n".encode("ascii")
        body = prefix + path.read_bytes() + suffix
        request = urllib.request.Request(
            f"{API_BASE}/im/v1/images",
            data=body,
            headers={
                "Authorization": f"Bearer {self.tenant_token()}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(body)),
            },
            method="POST",
        )
        with self._open(request) as response:
            payload = json.loads(response.read().decode("utf-8"))
        image_key = str((payload.get("data") or {}).get("image_key") or "")
        if payload.get("code", 0) != 0 or not image_key:
            raise GatewayError(f"Feishu image upload failed: {payload}")
        return image_key

    def tenant_info(self) -> dict[str, Any]:
        payload = self.request_json("GET", "/tenant/v2/tenant/query")
        tenant = (payload.get("data") or {}).get("tenant")
        if not isinstance(tenant, dict):
            raise GatewayError("Feishu tenant query returned no tenant")
        return tenant

    def list_chats(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page_token = None
        while True:
            payload = self.request_json(
                "GET", "/im/v1/chats", params={"page_size": 100, "page_token": page_token}
            )
            data = payload.get("data") or {}
            items.extend(data.get("items") or [])
            if not data.get("has_more"):
                return items
            page_token = data.get("page_token")

    def create_chat(self, name: str, owner_open_id: str, description: str) -> dict[str, Any]:
        body = {
            "name": name,
            "description": description,
            "chat_mode": "group",
            "chat_type": "private",
            "owner_id": owner_open_id,
            "user_id_list": [owner_open_id],
        }
        return self.request_json(
            "POST", "/im/v1/chats", params={"user_id_type": "open_id"}, body=body
        )

    def list_messages(self, chat_id: str, start_time: int) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page_token = None
        while True:
            payload = self.request_json(
                "GET",
                "/im/v1/messages",
                params={
                    "container_id_type": "chat",
                    "container_id": chat_id,
                    "start_time": start_time,
                    "sort_type": "ByCreateTimeAsc",
                    "page_size": 50,
                    "page_token": page_token,
                },
            )
            data = payload.get("data") or {}
            items.extend(data.get("items") or [])
            if not data.get("has_more"):
                return items
            page_token = data.get("page_token")

    def _post_content(self, markdown: str) -> str:
        return json.dumps(
            {"zh_cn": {"content": [[{"tag": "md", "text": markdown}]]}},
            ensure_ascii=False,
        )

    def send_post(self, chat_id: str, markdown: str, request_uuid: str) -> dict[str, Any]:
        return self.request_json(
            "POST",
            "/im/v1/messages",
            params={"receive_id_type": "chat_id"},
            body={
                "receive_id": chat_id,
                "msg_type": "post",
                "content": self._post_content(markdown),
                "uuid": request_uuid,
            },
        )

    def reply_post(self, message_id: str, markdown: str, request_uuid: str) -> dict[str, Any]:
        quoted = urllib.parse.quote(message_id, safe="")
        return self.request_json(
            "POST",
            f"/im/v1/messages/{quoted}/reply",
            body={
                "msg_type": "post",
                "content": self._post_content(markdown),
                "uuid": request_uuid,
            },
        )

    def reply_image(self, message_id: str, image_key: str, request_uuid: str) -> dict[str, Any]:
        quoted = urllib.parse.quote(message_id, safe="")
        return self.request_json(
            "POST",
            f"/im/v1/messages/{quoted}/reply",
            body={
                "msg_type": "image",
                "content": json.dumps({"image_key": image_key}, ensure_ascii=False),
                "uuid": request_uuid,
            },
        )

    def add_reaction(self, message_id: str, emoji_type: str = "Typing") -> dict[str, Any]:
        quoted = urllib.parse.quote(message_id, safe="")
        return self.request_json(
            "POST",
            f"/im/v1/messages/{quoted}/reactions",
            body={"reaction_type": {"emoji_type": emoji_type}},
        )

    def list_reactions(self, message_id: str, emoji_type: str | None = None) -> list[dict[str, Any]]:
        quoted = urllib.parse.quote(message_id, safe="")
        items: list[dict[str, Any]] = []
        page_token = None
        while True:
            payload = self.request_json(
                "GET",
                f"/im/v1/messages/{quoted}/reactions",
                params={
                    "reaction_type": emoji_type,
                    "page_size": 50,
                    "page_token": page_token,
                },
            )
            data = payload.get("data") or {}
            items.extend(item for item in data.get("items") or [] if isinstance(item, dict))
            if not data.get("has_more") or not data.get("page_token"):
                return items
            page_token = str(data["page_token"])

    def remove_reaction(self, message_id: str, reaction_id: str) -> dict[str, Any]:
        quoted_message = urllib.parse.quote(message_id, safe="")
        quoted_reaction = urllib.parse.quote(reaction_id, safe="")
        return self.request_json(
            "DELETE",
            f"/im/v1/messages/{quoted_message}/reactions/{quoted_reaction}",
        )

    def download_resource(self, message_id: str, resource_type: str, key: str, target_dir: Path) -> Path:
        quoted_message = urllib.parse.quote(message_id, safe="")
        quoted_key = urllib.parse.quote(key, safe="")
        url = (
            f"{API_BASE}/im/v1/messages/{quoted_message}/resources/{quoted_key}?"
            + urllib.parse.urlencode({"type": resource_type})
        )
        request = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {self.tenant_token()}"},
            method="GET",
        )
        with self._open(request) as response:
            body = response.read()
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0]
            disposition = response.headers.get("Content-Disposition", "")
        filename = ""
        if "filename=" in disposition:
            filename = disposition.split("filename=", 1)[1].strip().strip('"')
        suffix = Path(filename).suffix or mimetypes.guess_extension(content_type) or ""
        safe_key = "".join(char if char.isalnum() or char in "-_" else "_" for char in key)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{resource_type}-{safe_key[:80]}{suffix}"
        target.write_bytes(body)
        return target
