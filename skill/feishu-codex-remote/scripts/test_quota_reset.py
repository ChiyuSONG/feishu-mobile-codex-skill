"""Offline account-command tests. Never call the real redemption transport."""
import copy
import json
import threading
import unittest
from unittest.mock import Mock, patch

import remote_gateway as gateway
import quota_reset as reset
import test_gateway_lifecycle as fixtures


class ResetTests(unittest.TestCase):
    def setUp(self):
        fixtures.LifecycleTests.setUp(self)
        self.client.app_id = 'fixture-app'
        self.client.list_reactions.return_value = []
        self.client.add_reaction.side_effect = lambda mid, emoji: {'data': {'reaction_id': emoji + '-id'}}
        guard = patch.object(gateway, 'codex_account_request',
                             side_effect=AssertionError('Live account transport forbidden in tests'))
        guard.start()
        self.addCleanup(guard.stop)
        self.account = Mock()
        self.account.read.return_value = {'rateLimitResetCredits': {'availableCount': 0, 'credits': []}}
        self.account.consume.return_value = {'outcome': 'reset'}

    def enqueue(self, mid='one', text='#reset'):
        row = {'message_id': mid, 'chat_id': 'test-chat', 'create_time': '1000',
               'message_type': 'text', 'content': json.dumps({'text': text}),
               'sender': {'sender_type': 'user', 'sender_id': 'fixture-user'}}
        self.worker.enqueue(row)
        return row

    def restart(self):
        self.worker = gateway.ProjectWorker('test', self.project, self.client, self.root / 'log')
        self.store = self.worker.store

    def test_strict_original_text_and_sender(self):
        row = self.enqueue()
        for text in ['#reset', '  #reset\n']:
            row['content'] = json.dumps({'text': text})
            self.assertTrue(reset.is_reset_command(row))
        for text in ['#Reset', '#reset now', 'please #reset', '# reset', '*#reset', '#reset*', '#reset\n#reset']:
            row['content'] = json.dumps({'text': text})
            self.assertFalse(reset.is_reset_command(row))
        row['content'] = json.dumps({'text': '#reset', 'image_key': 'fixture'})
        self.assertFalse(reset.is_reset_command(row))
        row['content'] = json.dumps({'text': '#reset'})
        for sender in ['app', 'bot', 'system', '']:
            row['sender']['sender_type'] = sender
            self.assertFalse(reset.is_reset_command(row))

    def test_earliest_expiry_not_grant_date_and_incomplete_fallback(self):
        rows = [{'id': 'later', 'grantedAt': 1, 'expiresAt': 300,
                 'status': 'available', 'resetType': 'codexRateLimits'},
                {'id': 'soon', 'grantedAt': 100, 'expiresAt': 200,
                 'status': 'available', 'resetType': 'codexRateLimits'}]
        details = {'rateLimitResetCredits': {'availableCount': 2, 'credits': rows}}
        self.assertEqual('soon', reset.select_credit(details, now=150))
        rows[0]['expiresAt'] = None
        self.assertIsNone(reset.select_credit(details, now=150))
        self.assertIsNone(reset.select_credit({}, now=150))

    def test_selected_credit_and_live_result_reply(self):
        self.enqueue()
        self.account.read.side_effect = [
            {'rateLimitResetCredits': {'availableCount': 1, 'credits': [
                {'id': 'soon', 'expiresAt': 9999999999, 'status': 'available', 'resetType': 'codexRateLimits'}]}},
            {'rateLimitResetCredits': {'availableCount': 0}, 'rateLimits': {
                'primary': {'windowDurationMins': 300, 'usedPercent': 0}}}]
        self.worker.process_account_commands(self.account)
        self.assertEqual('soon', self.account.consume.call_args.args[0]['creditId'])
        self.assertIn('100%', self.client.reply_post.call_args.args[1])
        self.assertEqual('completed', self.store.state['messages']['one']['status'])
        self.assertIn('CheckMark', [call.args[1] for call in self.client.add_reaction.call_args_list])
        self.assertEqual('main', self.store.thread_id())
        self.assertEqual(0, self.store.state['messages']['one']['attempts'])

    def test_missing_details_or_read_failure_still_delegates_to_server(self):
        for index, detail in enumerate([{}, ConnectionError('offline')]):
            self.enqueue(str(index))
            self.account.read.side_effect = [detail, {}]
            self.worker.process_account_commands(self.account)
            self.assertNotIn('creditId', self.account.consume.call_args.args[0])
        self.assertEqual(2, self.account.consume.call_count)

    def test_success_survives_usage_refresh_failure(self):
        self.enqueue()
        self.account.read.side_effect = [{}, ConnectionError()]
        self.worker.process_account_commands(self.account)
        self.assertEqual('completed', self.store.state['messages']['one']['status'])
        self.assertIn('兑换已确认', self.client.reply_post.call_args.args[1])

    def test_duplicate_and_restart_never_redeem_again(self):
        row = self.enqueue()
        self.worker.enqueue(row)
        self.worker.process_account_commands(self.account)
        self.restart()
        self.worker.process_account_commands(self.account)
        self.account.consume.assert_called_once()
        self.client.reply_post.assert_called_once()

    def test_lost_delivery_and_missing_receipt_retry_only_same_reply(self):
        self.enqueue()
        self.client.reply_post.side_effect = [ConnectionError('lost'), {}, {'data': {'message_id': 'receipt'}}]
        for _ in range(2):
            with self.assertRaises(Exception):
                self.worker.process_account_commands(self.account)
            self.restart()
        self.worker.process_account_commands(self.account)
        self.account.consume.assert_called_once()
        replies = self.client.reply_post.call_args_list
        self.assertEqual(replies[0], replies[-1])
        self.assertEqual('completed', self.store.state['messages']['one']['status'])

    def test_crash_after_request_intent_stops_ambiguous_redemption(self):
        self.enqueue()
        self.store.update_message('one', quota_reset={'phase': 'requested', 'idempotencyKey': 'fixture'})
        self.restart()
        self.worker.process_account_commands(self.account)
        self.account.consume.assert_not_called()
        self.assertIn('暂未确认', self.client.reply_post.call_args.args[1])
        self.assertTrue(self.worker._terminal_failure(self.store.state['messages']['one']))

    def test_network_and_unknown_outcomes_do_not_claim_no_consumption(self):
        for index, outcome in enumerate([ConnectionError('offline'), {'outcome': 'newUnknown'}]):
            self.enqueue(str(index))
            self.account.consume.side_effect = [outcome]
            self.worker.process_account_commands(self.account)
            self.assertIn('可能已到达', self.client.reply_post.call_args.args[1])
        self.restart()
        self.worker.process_account_commands(self.account)
        self.assertEqual(2, self.account.consume.call_count)

    def test_no_credit_failure_cross_and_nothing_to_reset_completion(self):
        for index, outcome in enumerate(['noCredit', 'nothingToReset', 'alreadyRedeemed']):
            self.enqueue(str(index))
            self.account.consume.return_value = {'outcome': outcome}
            self.worker.process_account_commands(self.account)
            self.assertEqual('failed' if index == 0 else 'completed',
                             self.store.state['messages'][str(index)]['status'])
        self.assertIn('CrossMark', [call.args[1] for call in self.client.add_reaction.call_args_list])
        self.assertEqual([], self.worker.dispatch_parallel())
        self.assertEqual([], self.store.next_pending_batch(quiet_window_seconds=0))

    def test_maintenance_capacity_and_busy_main_do_not_block_command(self):
        self.enqueue('ordinary', 'work')
        self.store.finish('ordinary', 'processing')
        self.worker.maintenance_action('enter', 'fixture', reason='fixture maintenance')
        self.enqueue()
        self.worker.process_account_commands(self.account)
        self.account.consume.assert_called_once()
        self.assertTrue(self.store.pause_requested())
        self.assertEqual([], self.store.next_pending_batch(quiet_window_seconds=0))
        self.assertFalse(any(row.get('message_id') == 'one'
                             for row in self.store.state.get('lifecycle_notices', {}).values()))

    def test_excluded_from_model_and_parent_bootstrap_history(self):
        self.store.set_thread_id('')
        self.enqueue()
        self.assertEqual([], self.store.next_pending_batch(quiet_window_seconds=0))
        self.assertIsNone(self.store.pending_wait_seconds())
        self.assertEqual([], self.worker.dispatch_parallel())
        self.worker.process_account_commands(self.account)
        self.enqueue('branch', '# ordinary branch')
        with patch.object(gateway, 'start_thread', return_value='new-main') as start:
            self.assertEqual('new-main', self.worker._branch_parent())
            start.assert_called_once()

    def test_actual_history_catchup_and_gateway_scope(self):
        service = object.__new__(gateway.GatewayService)
        service.workers = {'test': self.worker}
        service.chat_to_key = {'test-chat': 'test'}
        service.config = {'expected_tenant_key': 'fixture-tenant'}
        service.client, service.log_path = self.client, self.root / 'service.log'
        original = self.enqueue()
        self.assertFalse(service.enqueue(dict(original, message_id='foreign', chat_id='unknown'), 'fixture-tenant'))
        self.assertFalse(service.enqueue(dict(original, message_id='wrong'), 'other-tenant'))
        self.client.list_messages.return_value = [dict(original, message_id='history')]
        self.assertEqual({'test': 1}, service.catch_up())
        self.worker.process_account_commands(self.account)
        self.assertEqual(2, self.account.consume.call_count)
        self.assertEqual([], service.queue_snapshot(['test'])['active'])

    def test_real_redemption_forbidden_even_with_production_shaped_store(self):
        rpc = Mock()
        account = reset.LiveAccount(self.store, rpc, self.root)
        with self.assertRaisesRegex(gateway.GatewayError, 'forbidden'):
            account.consume({'idempotencyKey': 'fixture'})
        rpc.assert_not_called()

    def test_reload_defers_new_commands_and_waits_for_inflight(self):
        self.enqueue()
        self.store.admission_open = False
        self.worker.process_account_commands(self.account)
        self.account.consume.assert_not_called()
        service = object.__new__(gateway.GatewayService)
        service.workers = {'test': self.worker}
        self.worker.account_command_active = True
        with self.assertRaises(gateway.GatewayError):
            service.wait_for_quiescence(['test'], timeout=0.02, quiet_seconds=0)
        self.worker.account_command_active = False
        service.wait_for_quiescence(['test'], timeout=1, quiet_seconds=0)

    def test_simultaneous_dispatch_claims_command_only_once(self):
        self.enqueue()
        started, release = threading.Event(), threading.Event()
        def consume(params):
            started.set()
            release.wait(2)
            return {'outcome': 'reset'}
        self.account.consume.side_effect = consume
        thread = threading.Thread(target=self.worker.process_account_commands, args=(self.account,))
        thread.start()
        self.assertTrue(started.wait(2))
        self.worker.process_account_commands(self.account)
        release.set()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.account.consume.assert_called_once()

    def test_lifecycle_loop_handles_command_without_model(self):
        self.enqueue()
        def deliver(mid, text, key):
            self.worker.lifecycle_stop.set()
            return {'data': {'message_id': 'receipt'}}
        self.client.reply_post.side_effect = deliver
        self.worker.lifecycle_signal.set()
        with patch.object(self.worker, 'live_account', return_value=self.account):
            self.worker._lifecycle_loop()
        self.assertEqual('completed', self.store.state['messages']['one']['status'])

    def test_english_feedback(self):
        self.project['language'] = 'en'
        self.enqueue()
        self.worker.process_account_commands(self.account)
        self.assertIn('Reset succeeded', self.client.reply_post.call_args.args[1])


if __name__ == '__main__':
    unittest.main()
