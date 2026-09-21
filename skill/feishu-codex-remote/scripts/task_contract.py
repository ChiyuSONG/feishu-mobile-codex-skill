"""Shared execution contract and durable result receipts, not a domain reviewer."""
import hashlib
import json
import re
from pathlib import Path

from gateway_common import GatewayError, atomic_write_json

EXECUTION_CONTRACT = """
Identify every intent inside Codex; do not create separate model calls just to
split a message. Process contiguous main-line groups in source order, reading
the committed result of the preceding group. Only explicitly parallel turns
may run independently. Within a compound request, respect dependencies; retain
verified completed effects, resume unfinished work, and never claim a dependent
step succeeded when its prerequisite failed. Check actual state before replay.
For applicable project review, read its canonical acceptance criteria BEFORE
generation. Give the reviewer the same evidence and criteria, and retain the
complete prior findings and their resolution across reviews. Do not add criteria
on each pass. A newly discovered omission must be identified as a missed earlier
finding with its rule and evidence, not silently treated as a new user requirement.
Use deterministic checks for objective invariants and semantic review for flexible
judgments; do not introduce an extra reviewer or domain rules for ordinary tasks.
Use only the existing recovery budget, including repair; changed error wording
does not create fresh attempts. Partial success is not whole-task completion.
If still incomplete, write outcome.json next to the final output at the supplied
outcome path with {"status":"incomplete","completed":["verified work"],
"failed":["unfinished work"],"blocked":["dependent work"],"reason":"plain-language
cause","next_step":"action needed","evidence":["local evidence references"]}.
Explain these in the final answer in the user's language. Optionally include a
safe_draft string in the outcome receipt containing only safe user-facing text.
Include the latest safe
rejected draft, clearly labeled unapproved, when useful; never leak private
diagnostics or present unsafe advice as approved. On a retry, verify prior evidence
and continue only unfinished work. A fully completed task needs no outcome file.
Do not leave an incomplete marker after fixing the task in this same run.
If the outcome path is outside your writable scope, do not broaden permissions.
Instead append the same JSON between <codex-task-outcome> and
</codex-task-outcome> on their own lines at the END of the final response.
The gateway removes that transport block before user delivery.
"""

class IncompleteTask(GatewayError):
    pass

def outcome_path(final_path):
    return Path(final_path).with_name("outcome.json")

def extract_transport_outcome(answer, final_path):
    marker = "<codex-task-outcome>"
    if marker not in answer:
        return answer
    match = re.search(r"(?:^|\n)<codex-task-outcome>\s*\n(.*?)\n</codex-task-outcome>\s*$", answer, re.S)
    if not match:
        raise IncompleteTask("Malformed task outcome transport; not sent to the user")
    try:
        outcome = json.loads(match.group(1))
        if not isinstance(outcome, dict) or outcome.get("status") not in {"incomplete", "completed"}:
            raise ValueError("Invalid outcome")
    except ValueError as exc:
        raise IncompleteTask("Invalid task outcome transport; not sent to the user") from exc
    text = answer[:match.start()].strip()
    if not text:
        raise IncompleteTask("The task outcome has no user-facing reply")
    atomic_write_json(outcome_path(final_path), outcome)
    Path(final_path).write_text(text, encoding="utf-8")
    return text

def read_outcome(final_path):
    path = outcome_path(final_path)
    if not path.exists():
        return None
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise IncompleteTask("The task outcome could not be read; completion is unconfirmed") from exc
    if not isinstance(result, dict) or result.get("status") not in {"incomplete", "completed"}:
        raise IncompleteTask("The task outcome is invalid; completion is unconfirmed")
    return result

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

def failure_text(phase, english=False, outcome=None):
    labels = {
        "attachments": ("reading the attachments", "读取附件"),
        "execution": ("executing the task", "执行任务"),
        "review": ("completing all requested work", "完成全部请求"),
        "delivery": ("delivering the finished reply", "发送已经生成的回复"),
        "completion": ("saving the delivery result", "保存投递结果"),
    }
    en, zh = labels.get(phase, labels["execution"])
    if english:
        text = f"The request is retained but failed while {en}. Automatic attempts are exhausted."
        text += (" The generated reply is saved; retry delivery without rerunning the task."
                 if phase == "delivery" else
                 " Check completed effects before retrying; no unverified work is claimed complete.")
    else:
        text = f"这批请求已保留，但在{zh}时失败，已用完本轮自动恢复次数。"
        text += ("已保存生成的回复，后续只需恢复投递，不必重新执行任务。"
                 if phase == "delivery" else
                 "重试前会先核对已有结果，避免重复执行；未核实的部分不算完成。")
    if outcome:
        for key, label in (("completed", "已完成"), ("failed", "未完成"),
                           ("blocked", "受前置任务阻塞"), ("reason", "原因"), ("next_step", "下一步")):
            value = outcome.get(key)
            if value:
                text += "\n" + (key if english else label) + ": " + (
                    "; ".join(str(v) for v in value) if isinstance(value, list) else str(value))
    elif phase != "delivery":
        text += (" The runtime did not provide a verified detailed cause; maintainer diagnosis is needed."
                 if english else "执行端未提供可核实的详细原因，需要维护者诊断，不能据此认定全部未执行。")
    if outcome and isinstance(outcome.get("safe_draft"), str) and outcome["safe_draft"].strip():
        text += ("\n\nLatest draft (not approved):\n" if english else "\n\n最新草稿（未通过审核）：\n") + outcome["safe_draft"]
    return text
