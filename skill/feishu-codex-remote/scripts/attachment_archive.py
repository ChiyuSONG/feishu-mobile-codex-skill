"""Verified attachment cache in the existing per-message attachment directory."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile

from gateway_common import GatewayError, load_json


def resource_identity(message_id, resource_type, key):
    return {"message_id": str(message_id), "resource_type": str(resource_type), "resource_key": str(key)}


def receipt_path(directory, identity):
    fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    return Path(directory) / (".resource-" + fingerprint + ".json")


def cached_resource(directory, identity):
    directory = Path(directory)
    receipt = load_json(receipt_path(directory, identity), None)
    if receipt is None:
        return None
    if receipt.get("schema_version") != 1 or receipt.get("source") != identity:
        raise GatewayError("Attachment receipt identity is invalid")
    name = receipt.get("filename")
    if not isinstance(name, str) or Path(name).name != name or name in {"", ".", ".."}:
        raise GatewayError("Attachment receipt path is invalid")
    target = directory / name
    if not target.resolve().is_relative_to(directory.resolve()):
        raise GatewayError("Attachment receipt escapes its message directory")
    if not target.exists():
        return None
    body = target.read_bytes()
    if len(body) != receipt.get("bytes") or hashlib.sha256(body).hexdigest() != receipt.get("sha256"):
        raise GatewayError("Attachment checksum mismatch; retained original evidence, refusing corrupted input")
    return target


def _publish_complete(target, body):
    """Atomically publish a complete file; preserve a competing committed file."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    _check_target(target)
    if not target.exists():
        descriptor, filename = tempfile.mkstemp(prefix=".attachment-", suffix=".tmp", dir=target.parent)
        temporary = Path(filename)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError:
                pass
        finally:
            temporary.unlink(missing_ok=True)
    _check_target(target)


def _check_target(target):
    if target.is_symlink() or not target.resolve().is_relative_to(target.parent.resolve()):
        raise GatewayError("Attachment target escapes its message directory")


def publish_attachment(target, body, digest):
    target = Path(target)
    _publish_complete(target, body)
    if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
        raise GatewayError("Attachment immutable-file checksum conflict; original retained")


def archive_resource(directory, identity, body, content_type, suffix=""):
    directory = Path(directory)
    digest = hashlib.sha256(body).hexdigest()
    # Content-addressed names avoid truncation collisions in legacy resource keys.
    # Existing legacy downloads remain at their original paths for old run evidence.
    suffix = suffix if suffix.startswith(".") and all(c.isalnum() or c in "._-" for c in suffix) else ""
    target = directory / (digest + suffix)
    previous = load_json(receipt_path(directory, identity), None)
    if previous is not None and (previous.get("source") != identity or previous.get("sha256") != digest):
        raise GatewayError("Attachment resource changed; previous receipt and file retained")
    publish_attachment(target, body, digest)
    receipt = {
        "schema_version": 1, "source": identity, "filename": target.name,
        "sha256": digest, "bytes": len(body), "content_type": str(content_type or ""),
    }
    receipt_body = json.dumps(receipt, ensure_ascii=False, sort_keys=True).encode("utf-8")
    _publish_complete(receipt_path(directory, identity), receipt_body)
    committed = load_json(receipt_path(directory, identity), None)
    if not isinstance(committed, dict) or committed.get("source") != identity or committed.get("sha256") != digest:
        raise GatewayError("Attachment resource changed; previous receipt and file retained")
    # The first complete receipt owns the MIME/name; equal bytes remain reusable.
    return cached_resource(directory, identity)
