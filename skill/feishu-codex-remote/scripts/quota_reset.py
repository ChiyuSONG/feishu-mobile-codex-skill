"""Native reset-voucher command. Tests must inject an account, never redeem live."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import threading
import time
import uuid

from gateway_common import GatewayError, message_text, resource_keys


COMMAND = '#reset'
_ACCOUNT_LOCK = threading.Lock()


def is_reset_command(row):
    return ((row.get('sender') or {}).get('sender_type') == 'user'
            and not row.get('deleted')
            and not resource_keys(row.get('content'))
            and message_text(row.get('content')).strip() == COMMAND)


def select_credit(result, *, now=None):
    data = result.get('rateLimitResetCredits') or {}
    rows, count = data.get('credits'), data.get('availableCount')
    if not isinstance(rows, list) or type(count) is not int or len(rows) != count or not rows:
        return None
    for row in rows:
        if (not isinstance(row, dict) or not row.get('id') or row.get('status') != 'available'
                or row.get('resetType') != 'codexRateLimits'
                or type(row.get('expiresAt')) not in (int, float)):
            return None
    now = time.time() if now is None else now
    eligible = [row for row in rows if row['expiresAt'] > now]
    return min(eligible, key=lambda row: (row['expiresAt'], str(row['id'])))['id'] if eligible else None


class LiveAccount:
    def __init__(self, store, rpc, runtime_root):
        self.store, self.rpc, self.runtime_root = store, rpc, Path(runtime_root)

    def read(self):
        return self.rpc('account/rateLimits/read', {})

    def consume(self, params):
        # No CLI for redemption. The live Listener supplies its registered store.
        expected = self.runtime_root / 'projects' / self.store.project_key / 'state.json'
        if ('unittest' in sys.modules or 'pytest' in sys.modules
                or os.environ.get('PYTEST_CURRENT_TEST') or os.environ.get('NUTRITION_REPLAY_ROOT')
                or self.store.path.resolve() != expected.resolve()):
            raise GatewayError('Live voucher redemption is forbidden in tests and isolated runtimes')
        return self.rpc('account/rateLimitResetCredit/consume', params)


def unknown_text(english):
    return ('Reset outcome is unconfirmed; automatic redemption has stopped. The request may have reached '
            'the server. Check Codex usage and remaining vouchers before sending another #reset.' if english else
            '重置结果暂未确认，自动兑换已停止。请求可能已到达服务端，请先查看 Codex 用量和剩余重置次数，不要连续发送新的 #reset。')


def error_text(exc, english):
    value = str(exc).lower()
    if any(term in value for term in ('401', 'unauthorized', 'auth', 'login')):
        return 'Codex authentication was rejected.' if english else 'Codex 登录或认证未通过。'
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)) or any(
            term in value for term in ('timeout', 'timed out', 'connection', 'network', 'eof')):
        return ('The account service connection was interrupted or did not respond.' if english else
                '与 Codex 账户服务的连接中断或未及时收到响应。')
    return 'The account service returned no confirmable result.' if english else '账户服务未返回可确认的结果。'


def exchange(account, state, english):
    """Exactly one consume call. Follow-up lookup failure cannot undo success."""
    try:
        outcome = account.consume(state['params']).get('outcome')
        if outcome in {'reset', 'alreadyRedeemed'}:
            text = 'Reset succeeded using one voucher.' if english else '重置成功，已使用一张重置券。'
            try:
                fresh = account.read()
                count = (fresh.get('rateLimitResetCredits') or {}).get('availableCount')
                if type(count) is int:
                    text += f' {count} remaining.' if english else f' 剩余 {count} 张。'
                limits = (fresh.get('rateLimitsByLimitId') or {}).get('codex') or fresh.get('rateLimits') or {}
                for name in ('primary', 'secondary'):
                    window = limits.get(name) or {}
                    if type(window.get('usedPercent')) in (int, float) and type(window.get('windowDurationMins')) is int:
                        hours = window['windowDurationMins'] / 60
                        remaining = max(0, min(100, 100 - window['usedPercent']))
                        text += (f' {hours:g}h window: {remaining:g}% remaining.' if english else
                                 f' {hours:g}小时额度剩余 {remaining:g}%。')
            except Exception:
                text += (' Redemption confirmed; refreshed usage unavailable.' if english else
                         ' 兑换已确认，但最新用量暂未查到。')
            return text, 'completed'
        if outcome == 'noCredit':
            return (('Reset failed: no vouchers available. Nothing was purchased or topped up.' if english else
                     '未能重置：当前账户没有可用的重置券。本次未购买或充值。'), 'failed')
        if outcome == 'nothingToReset':
            return (('No eligible usage window to reset; no voucher was used.' if english else
                     '当前没有可重置的额度窗口，本次未使用重置券。'), 'completed')
        return unknown_text(english), 'failed'
    except Exception as exc:
        state['diagnostic'] = type(exc).__name__ + ': ' + str(exc)
        return unknown_text(english) + ' ' + error_text(exc, english), 'failed'


class QuotaResetWorker:
    def init_account_commands(self):
        self.account_command_lock = threading.Lock()
        self.account_command_active = False

    def process_account_commands(self, account=None):
        if not self.account_command_lock.acquire(blocking=False):
            return
        try:
            with self.store.lock:
                if not self.store.admission_open:
                    return
                self.account_command_active = True
                rows = sorted((dict(row) for row in self.store.state.get('messages', {}).values()
                               if is_reset_command(row)), key=lambda row: (int(row.get('create_time') or 0), row['message_id']))
            for row in rows:
                saved = row.get('quota_reset') or {}
                if not saved and row.get('status') != 'pending':
                    continue
                if saved.get('phase') == 'delivered':
                    if row.get('reaction_sync_error') or (row.get('status') == 'completed' and not row.get('completion_reaction_id')):
                        self._reset_reaction(row['message_id'])
                    continue
                with _ACCOUNT_LOCK:
                    self._process_account_command(row, account or self.live_account())
        finally:
            with self.store.lock:
                self.account_command_active = False
            self.account_command_lock.release()

    def _save_reset(self, message_id, state):
        self.store.update_message(message_id, quota_reset=dict(state))

    def _process_account_command(self, row, account):
        mid = row['message_id']
        english = str(self.project.get('language') or '').lower().startswith('en')
        state = dict(row.get('quota_reset') or {})
        if not state:
            state = {'phase': 'reading', 'idempotencyKey': str(uuid.uuid5(uuid.NAMESPACE_URL, 'feishu-reset:' + mid))}
            self._save_reset(mid, state)
        if state['phase'] == 'reading':
            try:
                self._start_working_reaction(row)
            except Exception:
                pass  # Reaction availability cannot prevent an account command.
            try:
                details = account.read()
            except Exception:
                details = {}
            credit = select_credit(details)
            params = {'idempotencyKey': state['idempotencyKey']}
            if credit:
                params['creditId'] = credit
            state.update(phase='requested', params=params)
            self._save_reset(mid, state)
            text, terminal = exchange(account, state, english)
            state.update(phase='resolved', text=text, terminal=terminal)
            self._save_reset(mid, state)
        elif state['phase'] == 'requested':
            # Crash after request intent: never resubmit an uncertain redemption.
            state.update(phase='resolved', text=unknown_text(english), terminal='failed')
            self._save_reset(mid, state)
        result = self.client.reply_post(mid, state['text'], state['idempotencyKey'])
        receipt = (result.get('data') or {}).get('message_id') if isinstance(result, dict) else None
        if not receipt:
            raise GatewayError('Reset reply has no remote receipt')
        state.update(phase='delivered', reply_message_id=receipt)
        self.store.finish(mid, state['terminal'], quota_reset=dict(state))
        self._reset_reaction(mid)

    def _reset_reaction(self, mid):
        with self.store.lock:
            row = dict(self.store.state['messages'][mid])
        try:
            if row['status'] == 'completed':
                self._replace_working_with_completion(mid, row.get('working_reaction_id') or '')
            else:
                self._clear_working_reaction(mid, row.get('working_reaction_id') or '')
                self._sync_failure_reaction(row)
        except Exception as exc:
            self.store.update_message(mid, reaction_sync_error=str(exc))
