#!/usr/bin/env python3
"""Publish explicitly selected local artifacts to a personal Feishu Drive."""

from __future__ import annotations

import argparse
import ctypes
import getpass
import http.server
import json
import mimetypes
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

from gateway_common import (
    FeishuClient,
    GatewayError,
    PUBLISH_STATE,
    credential_backend,
    delete_protected,
    load_protected as load_protected_bytes,
    protected_exists,
    save_protected as save_protected_bytes,
)


API_BASE = "https://open.feishu.cn/open-apis"
AUTH_BASE = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
STATE_DIR = PUBLISH_STATE
CONFIG_PATH = STATE_DIR / "config.json"
SECRET_PATH = STATE_DIR / "secret.bin"
TOKENS_PATH = STATE_DIR / "tokens.bin"
HISTORY_PATH = STATE_DIR / "history.jsonl"
DEFAULT_REDIRECT = "http://127.0.0.1:17321/callback"
DEFAULT_FOLDER = "Codex 移动预览"
DEFAULT_SCOPES = "drive:drive offline_access"
MAX_SIMPLE_UPLOAD = 20 * 1024 * 1024
SENSITIVE_NAMES = {".env", ".npmrc", ".pypirc", "id_rsa", "id_ed25519", "credentials.json"}
SENSITIVE_SUFFIXES = {".key", ".pem", ".pfx", ".p12"}


class PublishError(RuntimeError):
    pass


def ensure_state_dir() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def save_protected(path: Path, value: dict | str) -> None:
    ensure_state_dir()
    raw = json.dumps(value, ensure_ascii=False).encode("utf-8") if isinstance(value, dict) else value.encode("utf-8")
    save_protected_bytes(path, raw)


def load_protected(path: Path):
    if not protected_exists(path):
        raise PublishError(f"Missing protected local state: {path}")
    raw = load_protected_bytes(path)
    text = raw.decode("utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise PublishError("Not configured. Run the configure command first.")
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def configure(args: argparse.Namespace) -> dict:
    app_id = args.app_id.strip()
    tenant_key = (args.expected_tenant_key or "").strip()
    if not app_id.startswith("cli_"):
        raise PublishError("App ID must start with cli_")
    secret = getpass.getpass("Paste App Secret (input is hidden): ").strip()
    if not secret:
        raise PublishError("App Secret was empty")
    if not tenant_key:
        tenant = FeishuClient(app_id, secret).tenant_info()
        tenant_key = str(tenant.get("tenant_key") or "").strip()
        if not tenant_key:
            raise PublishError("Could not determine the Feishu tenant key")
    ensure_state_dir()
    config = {
        "app_id": app_id,
        "expected_tenant_key": tenant_key,
        "redirect_uri": args.redirect_uri,
        "root_folder_name": args.folder_name,
        "scopes": DEFAULT_SCOPES,
    }
    CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    save_protected(SECRET_PATH, secret)
    if protected_exists(TOKENS_PATH):
        delete_protected(TOKENS_PATH)
    return {
        "ok": True,
        "config": str(CONFIG_PATH),
        "tenant_key": tenant_key,
        "secret_storage": credential_backend(),
    }


def windows_clipboard_text() -> str:
    if os.name != "nt":
        raise PublishError("Clipboard credential import requires Windows")
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.GetClipboardData.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    CF_UNICODETEXT = 13
    for _ in range(20):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        raise PublishError("Could not open the Windows clipboard")
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            raise PublishError("Clipboard does not contain Unicode text")
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            raise PublishError("Could not lock clipboard text")
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def configure_clipboard(args: argparse.Namespace) -> dict:
    secret = windows_clipboard_text().strip()
    if len(secret) < 16 or any(char.isspace() for char in secret):
        raise PublishError("Clipboard does not look like a Feishu App Secret")
    ensure_state_dir()
    config = {
        "app_id": args.app_id.strip(),
        "expected_tenant_key": args.expected_tenant_key.strip(),
        "redirect_uri": args.redirect_uri,
        "root_folder_name": args.folder_name,
        "scopes": DEFAULT_SCOPES,
    }
    if not config["app_id"].startswith("cli_") or not config["expected_tenant_key"]:
        raise PublishError("Valid App ID and expected personal tenant_key are required")
    CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    save_protected(SECRET_PATH, secret)
    if protected_exists(TOKENS_PATH):
        delete_protected(TOKENS_PATH)
    return {"ok": True, "config": str(CONFIG_PATH), "tenant_key": config["expected_tenant_key"], "secret_storage": credential_backend()}


def request_json(method: str, url: str, *, token: str | None = None, body: dict | None = None, timeout: int = 60) -> dict:
    headers = {"Accept": "application/json"}
    data = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise PublishError(f"HTTP {exc.code} at {url}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise PublishError(f"Network error at {url}: {exc.reason}") from exc
    if payload.get("code", 0) != 0:
        raise PublishError(f"Feishu API error at {url}: {json.dumps(payload, ensure_ascii=False)}")
    return payload


def encode_multipart(fields: dict[str, str], file_field: str, file_path: Path) -> tuple[bytes, str]:
    return encode_multipart_bytes(
        fields,
        file_field,
        file_path.name,
        file_path.read_bytes(),
        mimetypes.guess_type(file_path.name)[0] or "application/octet-stream",
    )


def encode_multipart_bytes(
    fields: dict[str, str],
    file_field: str,
    file_name: str,
    content: bytes,
    mime: str = "application/octet-stream",
) -> tuple[bytes, str]:
    boundary = "----CodexFeishu" + secrets.token_hex(16)
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
            value.encode("utf-8"),
            b"\r\n",
        ])
    chunks.extend([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="{file_field}"; filename="{file_name}"\r\n'.encode("utf-8"),
        f"Content-Type: {mime}\r\n\r\n".encode(),
        content,
        b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ])
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def upload_multipart(url: str, token: str, fields: dict[str, str], file_path: Path) -> dict:
    data, content_type = encode_multipart(fields, "file", file_path)
    return post_multipart(url, token, data, content_type)


def upload_multipart_bytes(url: str, token: str, fields: dict[str, str], file_name: str, content: bytes) -> dict:
    mime = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
    data, content_type = encode_multipart_bytes(fields, "file", file_name, content, mime)
    return post_multipart(url, token, data, content_type)


def post_multipart(url: str, token: str, data: bytes, content_type: str) -> dict:
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Authorization": f"Bearer {token}", "Content-Type": content_type, "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise PublishError(f"Upload HTTP {exc.code}: {detail}") from exc
    if payload.get("code", 0) != 0:
        raise PublishError(f"Feishu upload error: {json.dumps(payload, ensure_ascii=False)}")
    return payload


def upload_in_parts(base_url: str, token: str, source: Path, prepare_body: dict) -> dict:
    prepare = request_json("POST", f"{base_url}/upload_prepare", token=token, body=prepare_body)
    data = prepare.get("data", {})
    upload_id = str(data.get("upload_id") or "")
    block_size = int(data.get("block_size") or 0)
    block_num = int(data.get("block_num") or 0)
    if not upload_id or block_size <= 0 or block_num <= 0:
        raise PublishError("Feishu multipart prepare returned an invalid upload strategy")
    uploaded = 0
    with source.open("rb") as handle:
        for seq in range(block_num):
            chunk = handle.read(block_size)
            if not chunk:
                raise PublishError("Feishu multipart strategy expected more file blocks")
            upload_multipart_bytes(
                f"{base_url}/upload_part",
                token,
                {"upload_id": upload_id, "seq": str(seq), "size": str(len(chunk))},
                source.name,
                chunk,
            )
            uploaded += len(chunk)
        has_extra = bool(handle.read(1))
    if has_extra or uploaded != source.stat().st_size:
        raise PublishError("Feishu multipart upload did not consume the complete file")
    return request_json(
        "POST",
        f"{base_url}/upload_finish",
        token=token,
        body={"upload_id": upload_id, "block_num": block_num},
    )


def exchange_code(config: dict, code: str) -> dict:
    secret = load_protected(SECRET_PATH)
    return request_json(
        "POST",
        f"{API_BASE}/authen/v2/oauth/token",
        body={
            "grant_type": "authorization_code",
            "client_id": config["app_id"],
            "client_secret": secret,
            "code": code,
            "redirect_uri": config["redirect_uri"],
        },
    )


def user_info(access_token: str) -> dict:
    payload = request_json("GET", f"{API_BASE}/authen/v1/user_info", token=access_token)
    return payload.get("data", payload)


def assert_personal_tenant(config: dict, info: dict) -> None:
    actual = str(info.get("tenant_key") or "")
    expected = str(config["expected_tenant_key"])
    if not actual:
        raise PublishError("Feishu user info did not return tenant_key; refusing an unverified tenant")
    if actual != expected:
        raise PublishError(f"Wrong Feishu tenant: expected {expected}, got {actual}. Refusing to continue.")


def authenticate(args: argparse.Namespace) -> dict:
    config = load_config()
    if not protected_exists(SECRET_PATH):
        raise PublishError("App Secret is not configured")
    parsed = urllib.parse.urlparse(config["redirect_uri"])
    if parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise PublishError("Redirect URI must use localhost")
    state = secrets.token_urlsafe(24)
    result: dict[str, str] = {}
    done = threading.Event()

    class Callback(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            request_url = urllib.parse.urlparse(self.path)
            if request_url.path != parsed.path or done.is_set():
                self.send_response(204)
                self.end_headers()
                return
            query = urllib.parse.parse_qs(request_url.query)
            if query.get("state", [""])[0] != state:
                result["error"] = "OAuth state mismatch"
                status, message = 400, "授权校验失败，请关闭页面。"
            elif query.get("code"):
                result["code"] = query["code"][0]
                status, message = 200, "个人飞书授权已收到，可以关闭此页面返回 Codex。"
            else:
                result["error"] = query.get("error", ["missing authorization code"])[0]
                status, message = 400, "未完成授权，请关闭页面。"
            body = f"<!doctype html><meta charset='utf-8'><title>Codex 飞书授权</title><h2>{message}</h2>".encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            done.set()

        def log_message(self, *_):
            return

    server = http.server.ThreadingHTTPServer((parsed.hostname, parsed.port or 80), Callback)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    query = urllib.parse.urlencode({
        "client_id": config["app_id"],
        "redirect_uri": config["redirect_uri"],
        "scope": config["scopes"],
        "state": state,
    })
    auth_url = f"{AUTH_BASE}?{query}"
    print("Opening Feishu authorization in the default browser. Confirm the intended Feishu account and tenant.")
    print(auth_url)
    webbrowser.open(auth_url)
    if not done.wait(args.timeout):
        server.shutdown()
        raise PublishError("Timed out waiting for Feishu authorization")
    server.shutdown()
    if "error" in result:
        raise PublishError(result["error"])
    token_payload = exchange_code(config, result["code"])
    tokens = token_payload.get("data", token_payload)
    access_token = tokens.get("access_token")
    if not access_token:
        raise PublishError("OAuth response did not contain access_token")
    info = user_info(access_token)
    assert_personal_tenant(config, info)
    tokens["obtained_at"] = int(time.time())
    save_protected(TOKENS_PATH, tokens)
    return {
        "ok": True,
        "tenant_key": info.get("tenant_key"),
        "user": info.get("name") or info.get("en_name"),
        "open_id": info.get("open_id"),
    }


def access_token() -> tuple[str, dict, dict]:
    config = load_config()
    tokens = load_protected(TOKENS_PATH)
    if not isinstance(tokens, dict):
        raise PublishError("Stored OAuth token data is invalid")
    now = int(time.time())
    expires = int(tokens.get("expires_in", 0))
    obtained = int(tokens.get("obtained_at", 0))
    if tokens.get("access_token") and now < obtained + expires - 300:
        token = str(tokens["access_token"])
    else:
        refresh = tokens.get("refresh_token")
        if not refresh:
            raise PublishError("OAuth session cannot be refreshed; run auth again")
        secret = load_protected(SECRET_PATH)
        payload = request_json(
            "POST",
            f"{API_BASE}/authen/v2/oauth/token",
            body={
                "grant_type": "refresh_token",
                "client_id": config["app_id"],
                "client_secret": secret,
                "refresh_token": refresh,
            },
        )
        new_tokens = payload.get("data", payload)
        new_tokens["obtained_at"] = now
        save_protected(TOKENS_PATH, new_tokens)
        tokens = new_tokens
        token = str(tokens["access_token"])
    info = user_info(token)
    assert_personal_tenant(config, info)
    return token, config, info


def list_files(token: str, folder_token: str = "") -> list[dict]:
    query = urllib.parse.urlencode({"folder_token": folder_token, "page_size": 200, "order_by": "EditedTime", "direction": "DESC"})
    payload = request_json("GET", f"{API_BASE}/drive/v1/files?{query}", token=token)
    data = payload.get("data", {})
    return list(data.get("files") or data.get("items") or [])


def ensure_folder(token: str, name: str, parent: str = "") -> tuple[str, str]:
    for item in list_files(token, parent):
        if item.get("name") == name and item.get("type") == "folder":
            folder_token = str(item.get("token"))
            return folder_token, str(item.get("url") or f"https://www.feishu.cn/drive/folder/{folder_token}")
    payload = request_json(
        "POST",
        f"{API_BASE}/drive/v1/files/create_folder",
        token=token,
        body={"name": name, "folder_token": parent},
    )
    data = payload.get("data", {})
    return str(data["token"]), str(data.get("url") or f"https://www.feishu.cn/drive/folder/{data['token']}")


def sanitize_title(value: str) -> str:
    cleaned = "".join("_" if c in '<>:"/\\|?*' else c for c in value).strip(" .")
    return cleaned[:100] or "Codex 产出"


def assert_source_safe(path: Path, allow_sensitive: bool) -> None:
    if not path.is_file():
        raise PublishError(f"Source is not a file: {path}")
    lower_name = path.name.lower()
    if not allow_sensitive and (lower_name in SENSITIVE_NAMES or path.suffix.lower() in SENSITIVE_SUFFIXES or any(x in lower_name for x in ("secret", "token", "credential", "password"))):
        raise PublishError(f"Sensitive-looking source refused: {path}. Explicit confirmation and --allow-sensitive are required.")
    if path.stat().st_size == 0:
        raise PublishError(f"Empty files cannot be uploaded: {path}")


def convert_for_reading(source: Path, title: str, temp_dir: Path) -> Path:
    if source.suffix.lower() not in {".md", ".markdown", ".mark", ".html", ".htm", ".txt"}:
        return source
    pandoc = shutil.which("pandoc")
    if not pandoc:
        raise PublishError("Pandoc is required to convert Markdown/HTML/text into a mobile-readable Feishu document")
    output = temp_dir / f"{sanitize_title(title)}.docx"
    cmd = [pandoc, str(source), "-o", str(output), "--metadata", f"title={title}", "--resource-path", str(source.parent)]
    completed = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    if completed.returncode != 0:
        raise PublishError(f"Pandoc conversion failed: {completed.stderr.strip()}")
    return output


def upload_import_source(token: str, source: Path, title: str, folder_token: str) -> dict:
    ext = source.suffix.lower().lstrip(".")
    upload_fields = {
            "file_name": source.name,
            "parent_type": "ccm_import_open",
            "size": str(source.stat().st_size),
            "extra": json.dumps({"obj_type": "docx", "file_extension": ext}, separators=(",", ":")),
    }
    if source.stat().st_size <= MAX_SIMPLE_UPLOAD:
        upload = upload_multipart(f"{API_BASE}/drive/v1/medias/upload_all", token, upload_fields, source)
    else:
        upload = upload_in_parts(
            f"{API_BASE}/drive/v1/medias",
            token,
            source,
            {**upload_fields, "parent_node": "", "size": source.stat().st_size},
        )
    file_token = upload.get("data", {}).get("file_token")
    if not file_token:
        raise PublishError("Import upload returned no file_token")
    task = request_json(
        "POST",
        f"{API_BASE}/drive/v1/import_tasks",
        token=token,
        body={
            "file_extension": ext,
            "file_token": file_token,
            "type": "docx",
            "file_name": title,
            "point": {"mount_type": 1, "mount_key": folder_token},
        },
    )
    ticket = task.get("data", {}).get("ticket")
    if not ticket:
        raise PublishError("Create import task returned no ticket")
    deadline = time.monotonic() + 120
    last = None
    while time.monotonic() < deadline:
        payload = request_json("GET", f"{API_BASE}/drive/v1/import_tasks/{ticket}", token=token)
        result = payload.get("data", {}).get("result") or payload.get("data", {})
        last = result
        if result.get("url") or result.get("token") or result.get("file_token"):
            result["ticket"] = ticket
            return result
        status = result.get("job_status")
        if status not in (None, 1, 2):
            raise PublishError(f"Feishu import failed: {json.dumps(result, ensure_ascii=False)}")
        time.sleep(2)
    raise PublishError(f"Feishu import timed out: {json.dumps(last, ensure_ascii=False)}")


def upload_regular_file(token: str, source: Path, folder_token: str) -> dict:
    upload_fields = {
            "file_name": source.name,
            "parent_type": "explorer",
            "parent_node": folder_token,
            "size": str(source.stat().st_size),
    }
    if source.stat().st_size <= MAX_SIMPLE_UPLOAD:
        payload = upload_multipart(f"{API_BASE}/drive/v1/files/upload_all", token, upload_fields, source)
    else:
        payload = upload_in_parts(
            f"{API_BASE}/drive/v1/files",
            token,
            source,
            {**upload_fields, "size": source.stat().st_size},
        )
    file_token = str(payload.get("data", {}).get("file_token") or "")
    if not file_token:
        raise PublishError("File upload returned no file_token")
    return {"file_token": file_token, "url": f"https://www.feishu.cn/file/{file_token}"}


def harden_link_permissions(token: str, file_token: str, file_type: str) -> dict:
    url = f"{API_BASE}/drive/v1/permissions/{file_token}/public?type={file_type}"
    request_json(
        "PATCH",
        url,
        token=token,
        body={
            "external_access": False,
            "invite_external": False,
            "link_share_entity": "closed",
            "security_entity": "only_full_access",
            "share_entity": "only_full_access",
        },
    )
    verified = request_json("GET", url, token=token)
    permission = (verified.get("data") or {}).get("permission_public") or {}
    if permission.get("link_share_entity") != "closed":
        raise PublishError(f"Could not close link sharing for {file_type}:{file_token}")
    return permission


def publish(args: argparse.Namespace) -> dict:
    token, config, info = access_token()
    sources = [Path(value).expanduser().resolve() for value in args.source]
    for source in sources:
        assert_source_safe(source, args.allow_sensitive)
    root_token, root_url = ensure_folder(token, config.get("root_folder_name", DEFAULT_FOLDER))
    if args.folder_token:
        target_token = args.folder_token
        target_url = f"https://www.feishu.cn/drive/folder/{target_token}"
    elif args.flat:
        target_token, target_url = root_token, root_url
    else:
        stamp = time.strftime("%Y-%m-%d %H%M")
        target_token, target_url = ensure_folder(token, f"{stamp} {sanitize_title(args.title)}", root_token)
    items = []
    with tempfile.TemporaryDirectory(prefix="codex-feishu-") as temp:
        temp_dir = Path(temp)
        for index, source in enumerate(sources, start=1):
            item_title = args.title if len(sources) == 1 else f"{args.title} - {index:02d} {source.stem}"
            prepared = convert_for_reading(source, item_title, temp_dir)
            if prepared.suffix.lower() in {".docx", ".doc", ".md", ".markdown", ".mark", ".html", ".htm", ".txt"}:
                result = upload_import_source(token, prepared, item_title, target_token)
                kind = "docx"
            else:
                result = upload_regular_file(token, prepared, target_token)
                kind = "file"
            remote_token = str(result.get("token") or result.get("file_token") or "")
            if not remote_token:
                raise PublishError(f"Published {kind} returned no remote token")
            result["permission"] = harden_link_permissions(token, remote_token, kind)
            items.append({"source": str(source), "kind": kind, **result})
    visible = list_files(token, target_token)
    visible_tokens = {str(item.get("token")) for item in visible}
    for item in items:
        remote_token = str(item.get("token") or item.get("file_token") or "")
        item["listed_in_folder"] = bool(remote_token and remote_token in visible_tokens)
    receipt = {
        "published_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "tenant_key": info.get("tenant_key"),
        "folder_token": target_token,
        "folder_url": target_url,
        "title": args.title,
        "items": items,
    }
    ensure_state_dir()
    with HISTORY_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(receipt, ensure_ascii=False) + "\n")
    return {"ok": True, **receipt}


def status(_args: argparse.Namespace) -> dict:
    token, config, info = access_token()
    folders = [item for item in list_files(token, "") if item.get("type") == "folder" and item.get("name") == config.get("root_folder_name", DEFAULT_FOLDER)]
    return {
        "ok": True,
        "app_id": config["app_id"],
        "tenant_key": info.get("tenant_key"),
        "expected_tenant_key": config["expected_tenant_key"],
        "user": info.get("name") or info.get("en_name"),
        "root_folder": folders[0] if folders else None,
        "credential_storage": credential_backend(),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    cfg = sub.add_parser("configure", help="Store app configuration and OS-protected App Secret")
    cfg.add_argument("--app-id", required=True)
    cfg.add_argument("--expected-tenant-key")
    cfg.add_argument("--redirect-uri", default=DEFAULT_REDIRECT)
    cfg.add_argument("--folder-name", default=DEFAULT_FOLDER)
    cfg.set_defaults(func=configure)
    clip = sub.add_parser("configure-clipboard", help="Import App Secret from clipboard directly into Windows DPAPI")
    clip.add_argument("--app-id", required=True)
    clip.add_argument("--expected-tenant-key", required=True)
    clip.add_argument("--redirect-uri", default=DEFAULT_REDIRECT)
    clip.add_argument("--folder-name", default=DEFAULT_FOLDER)
    clip.set_defaults(func=configure_clipboard)
    auth = sub.add_parser("auth", help="Authorize the personal Feishu user")
    auth.add_argument("--timeout", type=int, default=300)
    auth.set_defaults(func=authenticate)
    check = sub.add_parser("status", help="Verify OAuth, personal tenant, and destination folder")
    check.set_defaults(func=status)
    pub = sub.add_parser("publish", help="Publish selected local files")
    pub.add_argument("--title", required=True)
    pub.add_argument("--source", action="append", required=True)
    pub.add_argument("--folder-token")
    pub.add_argument("--flat", action="store_true")
    pub.add_argument("--allow-sensitive", action="store_true", help="Use only after explicit user confirmation")
    pub.set_defaults(func=publish)
    return parser


def main() -> int:
    try:
        args = build_parser().parse_args()
        result = args.func(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (PublishError, GatewayError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
