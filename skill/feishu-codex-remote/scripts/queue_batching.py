"""Durable quiet-window selection; callers claim the result under their store lock."""
from datetime import datetime
import json
import time
from gateway_common import message_text, resource_keys

QUIET_WINDOW_SECONDS = 60.0

def route_key(item):
    sender = item.get("sender") or {}
    identity = sender.get("id") or sender.get("sender_id") or {}
    return item.get("chat_id"), json.dumps(identity, sort_keys=True, ensure_ascii=False)

def received_epoch(item):
    value = item.get("received_at")
    if value:
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return int(item.get("create_time") or 0) / 1000.0

def prepared_batch(first, items):
    receipt = first.get("prepared_reply")
    if not receipt:
        return None
    by_id = {item["message_id"]: item for item in items}
    rows = [by_id.get(mid) for mid in receipt.get("message_ids", [])]
    if not rows or any(not row or row.get("status") not in {"pending", "failed"}
                       or int(row.get("attempts") or 0) >= 3
                       or row.get("provider_wait") or row.get("prepared_reply") != receipt for row in rows):
        return []
    return rows

def select_pending(items, forced_single, routing_mode, *, now=None,
                   quiet_seconds=QUIET_WINDOW_SECONDS, max_messages=None,
                   merge_window_seconds=None, parallel=None):
    if parallel is not None:
        items = [item for item in items if not parallel(item)]
    if any(item.get("status") == "processing" for item in items):
        return [], None
    pending = [item for item in items if item.get("status") == "pending"
               and int(item.get("attempts") or 0) < 3]
    pending.sort(key=lambda item: (int(item.get("create_time") or 0), item["message_id"]))
    if not pending:
        failed = [item for item in items if item.get("status") == "failed"
                  and int(item.get("attempts") or 0) < 3]
        failed.sort(key=lambda item: (int(item.get("create_time") or 0), item["message_id"]))
        if failed:
            saved = prepared_batch(failed[0], items)
            if saved is not None:
                return saved, None
        return failed[:1], None
    first = pending[0]
    if first.get("provider_wait"):
        return [], None
    saved = prepared_batch(first, items)
    if saved is not None:
        return saved, None
    # An empty separator is a control marker, not a new model task.
    if message_text(first.get('content')).strip() == '*' and not resource_keys(first.get('content')):
        return [first], 0.0
    # Wait on the whole unstarted same-sender prefix, not the eventual reply
    # routing subdivision. A star ends the prefix's quiet wait immediately.
    prefix = [first]
    star_barrier = False
    for item in pending[1:]:
        if item.get("prepared_reply"):
            break
        if item.get("provider_wait"):
            break
        if route_key(item) != route_key(first):
            break
        if forced_single(item):
            star_barrier = True
            break
        prefix.append(item)
    delay = 0.0 if star_barrier else max(
        0.0, max(received_epoch(item) for item in prefix)
        + max(0.0, quiet_seconds) - (time.time() if now is None else now))
    if delay:
        return [], delay
    selected = [first]
    modes = {routing_mode(first)}
    for item in prefix[1:]:
        if max_messages is not None and len(selected) >= max(1, max_messages):
            break
        if merge_window_seconds is not None and (
            int(item.get("create_time") or 0) - int(selected[-1].get("create_time") or 0)
            > max(0, merge_window_seconds) * 1000
        ):
            break
        next_modes = modes | {routing_mode(item)}
        if "doc" in next_modes and "direct" in next_modes:
            break
        selected.append(item)
        modes = next_modes
    return selected, 0.0
