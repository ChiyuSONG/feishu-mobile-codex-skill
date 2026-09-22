"""Shared execution contract and durable result receipts, not a domain reviewer."""
import hashlib
import json
import re
from pathlib import Path

from gateway_common import GatewayError, atomic_write_json

# One field definition drives both the transport instructions and validation.
# Fields remain optional for legacy receipts; present fields must be well typed.
OUTCOME_FIELDS = {
    "completed": list, "failed": list, "blocked": list, "evidence": list,
    "reason": str, "next_step": str, "safe_draft": str,
    "draft_unavailable_reason": str,
}


def validate_outcome(value):
    errors = []
    if not isinstance(value, dict):
        raise IncompleteTask("outcome: expected an object")
    if value.get("status") not in ("incomplete", "completed"):
        errors.append("status: expected incomplete or completed")
    for name, kind in OUTCOME_FIELDS.items():
        if name not in value:
            continue
        field = value[name]
        if not isinstance(field, kind) or (kind is list and any(not isinstance(x, str) for x in field)):
            errors.append(name + (": expected an array of strings" if kind is list else ": expected a string"))
    if "resolutions" in value:
        resolutions = value["resolutions"]
        if not isinstance(resolutions, list):
            errors.append("resolutions: expected an array")
        else:
            for index, entry in enumerate(resolutions):
                if not isinstance(entry, dict) or any(not isinstance(entry.get(k), str) or not entry[k]
                        for k in ("message_id", "source_sha256")) or not isinstance(entry.get("checks"), list) or not entry["checks"]:
                    errors.append(f"resolutions[{index}]: message_id, source_sha256 and nonempty checks required")
                elif any(not isinstance(c, dict) or c.get("passed") is not True
                         or not isinstance(c.get("artifact"), str) or not isinstance(c.get("sha256"), str)
                         for c in entry["checks"]):
                    errors.append(f"resolutions[{index}].checks: passed, artifact and sha256 required")
        if value.get("status") != "completed":
            errors.append("resolutions: only a completed recovery may resolve another request")
    if errors:
        raise IncompleteTask("Invalid task outcome: " + "; ".join(errors))
    return value


def source_fingerprint(row):
    source = {key: row.get(key) for key in ("message_id", "chat_id", "message_type", "content")}
    return hashlib.sha256(json.dumps(source, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def recovery_context(row):
    """Explicit original input and prior effects, not a repair's own summary."""
    return {"message_id": row["message_id"], "original_content": row.get("content"),
            "source_sha256": source_fingerprint(row), "status": row.get("status"),
            "prior_outcome": row.get("task_outcome"), "prepared_reply": row.get("prepared_reply"),
            "run_event_log": row.get("run_event_log"), "last_final_path": row.get("last_final_path")}


def verified_resolutions(outcome, rows, message_ids, final_path):
    """Integrity/source binding only; project tools/review must verify actual effects."""
    validated = {}
    for entry in (outcome or {}).get("resolutions", []):
        mid = entry["message_id"]
        row = rows.get(mid)
        if (not row or mid in message_ids or mid in validated or row.get("status") != "failed"
                or any(row.get("chat_id") != rows[current].get("chat_id") for current in message_ids)
                or source_fingerprint(row) != entry["source_sha256"]):
            raise IncompleteTask("Recovery does not match a failed original request in this conversation")
        for check in entry["checks"]:
            artifact = (Path(final_path).parent / check["artifact"]).resolve()
            try:
                if hashlib.sha256(artifact.read_bytes()).hexdigest() != check["sha256"]:
                    raise ValueError("changed")
            except (OSError, ValueError) as exc:
                raise IncompleteTask("Original-task recovery evidence is missing or changed") from exc
        validated[mid] = dict(entry)
    return validated

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
Scope review to the current requested work. Historical findings and sibling tasks
are references, not additional requirements. Fetch canonical rules separately from
expensive evidence when the project supports it; reuse still-current evidence,
but revalidate it when inputs, implementation or acceptance criteria change.
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

EXECUTION_CONTRACT += "\nOutcome fields (optional for old receipts; types when supplied): " + ", ".join(
    name + ("=array of strings" if kind is list else "=string") for name, kind in OUTCOME_FIELDS.items()) + """.
If no safe draft can be shown, use draft_unavailable_reason to explain why without
including private diagnostics. Never invent a draft when generation failed.
Only when explicitly repairing a different failed original request, read back that
original input and completed effects first. A completed outcome may include
resolutions: [{message_id, source_sha256, checks: [{passed: true, artifact, sha256}]}].
Use the original source fingerprint supplied by the recovery context and current
project verification artifacts (relative to this run's final output, or absolute).
The final reply must answer the original request, not just say code/tests were fixed.
This links verified recovery to delivery; an unrelated successful reply does not
resolve earlier failures. Do not add a new reviewer or expand authorized task scope.
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
        validate_outcome(outcome)
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
    return validate_outcome(result)

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
    elif phase != "delivery":
        explanation = (outcome or {}).get("draft_unavailable_reason")
        text += ("\nNo safe draft available." if english else "\n目前没有可展示的安全草稿。")
        if explanation:
            text += " " + explanation
    return text
