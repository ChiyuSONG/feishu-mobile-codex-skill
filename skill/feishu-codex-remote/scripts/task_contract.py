"""Transport context and durable reply receipts; reply quality is project-owned."""
import hashlib
import json
from pathlib import Path

from gateway_common import GatewayError

def terminal_failure(row):
    """Existing terminal infrastructure/control states; never inspect reply content."""
    return row.get("status") == "failed" and (
        int(row.get("attempts") or 0) >= 3
        or row.get("branch_state") == "fork_outcome_unknown"
        or bool(row.get("quota_reset")))


def source_fingerprint(row):
    source = {key: row.get(key) for key in ("message_id", "chat_id", "message_type", "content")}
    return hashlib.sha256(json.dumps(source, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def recovery_context(row):
    """Explicit original input and prior effects, not a repair's own summary."""
    return {"message_id": row["message_id"], "original_content": row.get("content"),
            "source_sha256": source_fingerprint(row), "status": row.get("status"),
            "prior_outcome": row.get("task_outcome"), "prepared_reply": row.get("prepared_reply"),
            "run_event_log": row.get("run_event_log"), "last_final_path": row.get("last_final_path")}


EXECUTION_CONTRACT = """
Answer the user through the bound project instructions. Clarifying questions and
uncertainty are normal replies. The bridge requires no quality review, approval
receipt or outcome file. Project-specific review is owned by that project.
On an infrastructure retry, inspect retained execution evidence and continue only
unfinished work. Keep the original message group together and avoid repeating
already applied effects. Account Reset remains a separate native user command;
never redeem or retry a voucher from a model turn.
"""

def prepared_result(answer, thread_id, final_path, message_ids):
    return {"answer": answer, "thread_id": thread_id, "final_path": str(final_path),
            "message_ids": list(message_ids),
            "sha256": hashlib.sha256(answer.encode("utf-8")).hexdigest()}

def restore_result(receipt, message_ids):
    if receipt.get("message_ids") != list(message_ids):
        raise GatewayError("Prepared reply belongs to a different source batch")
    answer = receipt.get("answer", "")
    if not answer or hashlib.sha256(answer.encode("utf-8")).hexdigest() != receipt.get("sha256"):
        raise GatewayError("Prepared reply failed integrity validation")
    path = Path(receipt["final_path"])
    if not path.is_file() or path.read_text(encoding="utf-8").strip() != answer:
        raise GatewayError("Prepared reply artifact is missing or changed")
    return answer, receipt["thread_id"], path

def failure_text(phase, english=False):
    labels = {
        "attachments": ("reading attachments", "读取附件"),
        "execution": ("calling Codex", "调用 Codex"),
        "delivery": ("delivering the reply", "发送回复"),
        "completion": ("saving delivery state", "保存投递状态"),
    }
    en, zh = labels.get(phase, labels["execution"])
    if english:
        text = f"The request is retained but failed while {en}. Automatic attempts are exhausted."
        text += (" The generated reply is saved; retry delivery without rerunning the task."
                 if phase in {"delivery", "completion"} else
                 " Check retained execution evidence before retrying to avoid duplicate actions.")
    else:
        text = f"这批请求已保留，但在{zh}时失败，已用完本轮自动恢复次数。"
        text += ("已保存生成的回复，后续只需恢复投递或状态，不必重新执行任务。"
                 if phase in {"delivery", "completion"} else
                 "重试前会先核对已有执行记录，避免重复操作。")
    return text
