"""Generic recovery contract tests using synthetic inputs and durable restarts."""
import copy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import gateway_lifecycle as lifecycle
import remote_gateway as gateway
import task_contract as contract
import test_gateway_lifecycle as fixtures
import test_task_contract as transactions


class RecoveryTests(unittest.TestCase):
    setUp = fixtures.LifecycleTests.setUp
    enqueue = fixtures.LifecycleTests.enqueue
    enter = fixtures.LifecycleTests.enter
    acceptance = fixtures.LifecycleTests.acceptance
    final = transactions.TransactionTests.final
    process = transactions.TransactionTests.process

    def reaction_client(self):
        self.client.app_id = 'app'
        self.client.list_reactions.return_value = []
        self.client.add_reaction.side_effect = lambda mid, emoji: {'data': {'reaction_id': emoji + '-id'}}

    def restart(self):
        self.worker = gateway.ProjectWorker('test', self.project, self.client, self.root / 'log')
        self.store = self.worker.store

    def test_schema_reports_all_present_field_errors_and_keeps_legacy(self):
        bad = {'status': 'incomplete', 'failed': 'not a list', 'reason': [], 'completed': [3]}
        with self.assertRaises(contract.IncompleteTask) as result:
            contract.validate_outcome(bad)
        for field in ['failed', 'reason', 'completed']:
            self.assertIn(field, str(result.exception))
        self.assertEqual({'status': 'incomplete'}, contract.validate_outcome({'status': 'incomplete'}))
        self.assertIsNone(contract.read_outcome(self.final()))

    def test_transport_and_file_have_same_validation(self):
        path = self.final()
        invalid = {'status': 'completed', 'evidence': 'bad'}
        path.with_name('outcome.json').write_text(json.dumps(invalid), encoding='utf-8')
        for call in [lambda: contract.read_outcome(path), lambda: contract.extract_transport_outcome(
                'Reply\n<codex-task-outcome>\n' + json.dumps(invalid) + '\n</codex-task-outcome>', path)]:
            with self.assertRaises(contract.IncompleteTask):
                call()

    def test_failure_without_draft_explains_and_never_uses_raw_exception(self):
        text = contract.failure_text('execution', outcome={'status': 'incomplete',
            'draft_unavailable_reason': '没有生成可用正文'})
        self.assertIn('没有生成可用正文', text)
        self.assertNotIn('Traceback', text)
        text = contract.failure_text('review', True, {'safe_draft': 'Draft only'})
        self.assertIn('not approved', text)
        self.assertIn('Draft only', text)

    def test_terminal_failure_cross_survives_restart_and_pending_clears_it(self):
        self.reaction_client()
        self.enqueue()
        self.store.finish('one', 'failed', attempts=3)
        self.worker.reconcile_failure_reactions()
        self.assertTrue(self.store.state['messages']['one']['failure_reaction_active'])
        self.client.add_reaction.assert_called_once_with('one', 'CrossMark')
        self.restart()
        self.worker.reconcile_failure_reactions()
        self.assertEqual(1, self.client.add_reaction.call_count)
        self.store.finish('one', 'pending', attempts=0)
        self.worker.reconcile_failure_reactions()
        self.client.remove_reaction.assert_called_with('one', 'CrossMark-id')
        self.assertFalse(self.store.state['messages']['one']['failure_reaction_active'])

    def test_provider_pending_and_retryable_failure_never_get_cross(self):
        self.reaction_client()
        self.enqueue()
        for status, attempts in [('pending', 0), ('failed', 1), ('processing', 3)]:
            self.store.finish('one', status, attempts=attempts)
            self.worker.reconcile_failure_reactions()
        self.client.add_reaction.assert_not_called()

    def test_failed_reaction_is_retryable_without_executing_task(self):
        self.reaction_client()
        self.enqueue()
        self.store.finish('one', 'failed', attempts=3)
        self.client.add_reaction.side_effect = RuntimeError('offline')
        self.worker.reconcile_failure_reactions()
        self.assertEqual('offline', self.store.state['messages']['one']['reaction_sync_error'])
        self.client.add_reaction.side_effect = None
        self.client.add_reaction.return_value = {'data': {'reaction_id': 'cross'}}
        self.worker.reconcile_failure_reactions()
        self.assertIsNone(self.store.state['messages']['one']['reaction_sync_error'])

    def test_failure_reconciliation_preserves_typing_cleanup_error(self):
        self.reaction_client()
        self.enqueue()
        self.store.finish('one', 'failed', attempts=3, working_reaction_active=True, working_reaction_id='typing')
        self.client.remove_reaction.side_effect = RuntimeError('cleanup offline')
        self.worker.reconcile_failure_reactions()
        self.client.add_reaction.assert_not_called()
        self.assertEqual('cleanup offline', self.store.state['messages']['one']['reaction_sync_error'])
        self.client.remove_reaction.side_effect = None
        self.worker.reconcile_failure_reactions()
        self.assertFalse(self.store.state['messages']['one']['working_reaction_active'])
        self.assertTrue(self.store.state['messages']['one']['failure_reaction_active'])

    def test_state_change_during_reaction_api_remains_reconcilable(self):
        self.reaction_client()
        self.enqueue()
        self.store.finish('one', 'failed', attempts=3)
        def add(mid, emoji):
            self.store.finish(mid, 'pending', attempts=0)
            return {'data': {'reaction_id': 'raced-cross'}}
        self.client.add_reaction.side_effect = add
        self.worker.reconcile_failure_reactions()
        self.assertIsNotNone(self.store.state['messages']['one']['reaction_sync_error'])
        self.worker.reconcile_failure_reactions()
        self.client.remove_reaction.assert_called_with('one', 'raced-cross')
        self.assertFalse(self.store.state['messages']['one']['failure_reaction_active'])

    def test_branch_failure_offline_restart_uses_same_outbox_identity(self):
        self.reaction_client()
        self.enqueue(text='# task')
        self.store.finish('one', 'failed', branch_state='fork_outcome_unknown')
        self.worker._report_branch_failure('one')
        self.client.reply_post.side_effect = RuntimeError('private diagnostic')
        self.worker.deliver_lifecycle_notices()
        first_uuid = self.client.reply_post.call_args.args[2]
        self.restart()
        self.client.reply_post.side_effect = None
        with patch.object(lifecycle.time, 'time', return_value=10**12):
            self.worker.deliver_lifecycle_notices()
        self.assertEqual(first_uuid, self.client.reply_post.call_args.args[2])
        self.assertNotIn('private diagnostic', self.client.reply_post.call_args.args[1])
        self.assertEqual('failed', self.store.state['messages']['one']['status'])

    def test_background_notice_has_no_fake_source_and_is_durable(self):
        self.store.queue_task_notice('job-run', 'Task delivery failed')
        self.client.send_post.side_effect = RuntimeError('offline')
        self.worker.deliver_lifecycle_notices()
        first_uuid = self.client.send_post.call_args.args[2]
        self.restart()
        self.client.send_post.side_effect = None
        with patch.object(lifecycle.time, 'time', return_value=10**12):
            self.worker.deliver_lifecycle_notices()
        self.client.reply_post.assert_not_called()
        self.assertEqual(first_uuid, self.client.send_post.call_args.args[2])
        self.assertEqual('delivered', self.store.state['lifecycle_notices']['task:job-run']['status'])
        with self.assertRaises(gateway.GatewayError):
            self.store.queue_task_notice('job-run', 'Different task')

    def test_notice_handoff_never_writes_inbox_and_conflicts_are_detected(self):
        requests = self.root / 'requests'
        before = self.store.path.read_bytes()
        with patch.object(gateway, 'SYNC_REQUESTS', requests):
            a = gateway.queue_task_notice_request('test', 'run', 'safe text')
            b = gateway.queue_task_notice_request('test', 'run', 'safe text')
            self.assertEqual(a, b)
            with self.assertRaises(gateway.GatewayError):
                gateway.queue_task_notice_request('test', 'run', 'different')
        self.assertEqual(before, self.store.path.read_bytes())
        self.assertEqual(1, len(list(requests.glob('*.json'))))

    def test_exit_before_outbox_insertion_recovers_after_restart(self):
        self.enter()
        accepted = self.acceptance()
        self.worker.maintenance_action('exit', 'exit', release_id='release', acceptance_file=accepted)
        self.assertNotIn('complete:release', self.store.state.get('lifecycle_notices', {}))
        self.restart()
        self.worker.deliver_lifecycle_notices()
        self.worker.deliver_lifecycle_notices()
        self.client.send_post.assert_called_once()
        self.assertEqual('delivered', self.store.state['lifecycle_notices']['complete:release']['status'])

    def test_changed_source_blocks_queued_completion_but_not_other_notices(self):
        self.enter()
        accepted = self.acceptance()
        self.worker.maintenance_action('exit', 'exit', release_id='release', acceptance_file=accepted)
        self.store.reconcile_completion_notice()
        (self.root / 'acceptance-test.txt').write_text('changed code', encoding='utf-8')
        self.store.queue_task_notice('other', 'Other task failed')
        self.worker.deliver_lifecycle_notices()
        self.client.send_post.assert_called_once()
        self.assertEqual('Other task failed', self.client.send_post.call_args.args[1])
        self.assertEqual('pending', self.store.state['lifecycle_notices']['complete:release']['status'])

    def test_replacing_acceptance_cannot_reapprove_old_notice(self):
        self.store.queue_completion('release', 'done', self.acceptance())
        path = self.root / 'release-acceptance.json'
        data = json.loads(path.read_text())
        data['scope'] = 'different scope'
        path.write_text(json.dumps(data), encoding='utf-8')
        self.worker.deliver_lifecycle_notices()
        self.client.send_post.assert_not_called()

    def test_new_maintenance_does_not_send_old_completion_intent(self):
        self.enter()
        self.worker.maintenance_action('exit', 'exit', release_id='release', acceptance_file=self.acceptance())
        self.enter('new-generation')
        self.store.exit_maintenance()
        self.worker.deliver_lifecycle_notices()
        self.client.send_post.assert_not_called()

    def recovery(self):
        self.enqueue('original', 'Do the original task')
        self.store.finish('original', 'failed', attempts=3, task_outcome={'completed': ['A']})
        self.enqueue('repair', 'Finish the original task')
        path = self.final('The original result, not just a repair summary')
        artifact = self.root / 'verified.txt'
        artifact.write_text('actual effects verified', encoding='utf-8')
        receipt = {'message_id': 'original', 'source_sha256': contract.source_fingerprint(self.store.state['messages']['original']),
                   'checks': [{'passed': True, 'artifact': str(artifact),
                               'sha256': hashlib.sha256(artifact.read_bytes()).hexdigest()}]}
        path.with_name('outcome.json').write_text(json.dumps({'status': 'completed', 'resolutions': [receipt]}), encoding='utf-8')
        return path, receipt, artifact

    def test_original_task_resolves_only_after_bound_recovery_delivery(self):
        self.reaction_client()
        path, receipt, _ = self.recovery()
        self.process(self.store.next_pending_batch(quiet_window_seconds=0), path, RuntimeError('offline'))
        self.assertEqual('failed', self.store.state['messages']['original']['status'])
        self.restart()
        self.assertEqual((0, 1), self.process(self.store.next_pending_batch(quiet_window_seconds=0), path))
        original = self.store.state['messages']['original']
        self.assertEqual('completed', original['status'])
        self.assertEqual(['repair'], original['resolution']['completion_message_ids'])
        self.assertEqual(receipt, original['resolution']['receipt'])

    def test_unrelated_completion_cannot_clear_old_failure(self):
        path, _, _ = self.recovery()
        path.with_name('outcome.json').unlink()
        self.process(self.store.next_pending_batch(quiet_window_seconds=0), path)
        self.assertEqual('failed', self.store.state['messages']['original']['status'])

    def test_resolution_rejects_changed_input_evidence_and_foreign_chat(self):
        path, receipt, artifact = self.recovery()
        rows = self.store.state['messages']
        outcome = {'status': 'completed', 'resolutions': [receipt]}
        for field, value in [('content', 'changed'), ('chat_id', 'another-chat'), ('status', 'pending')]:
            changed = copy.deepcopy(rows)
            changed['original'][field] = value
            with self.assertRaises(contract.IncompleteTask):
                contract.verified_resolutions(outcome, changed, ['repair'], path)
        artifact.write_text('stale', encoding='utf-8')
        with self.assertRaises(contract.IncompleteTask):
            contract.verified_resolutions(outcome, rows, ['repair'], path)

    def test_recovery_context_preserves_original_and_committed_parts(self):
        self.recovery()
        row = self.store.state['messages']['original']
        context = contract.recovery_context(row)
        self.assertEqual(row['content'], context['original_content'])
        self.assertEqual(['A'], context['prior_outcome']['completed'])
        self.assertEqual(contract.source_fingerprint(row), context['source_sha256'])

    def test_atomic_resolution_rolls_back_all_rows(self):
        path, receipt, _ = self.recovery()
        self.store.next_pending_batch(quiet_window_seconds=0)
        with patch.object(self.store, 'save', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.store.complete_batch(['repair'], verified_resolutions={'original': receipt})
        self.assertEqual('failed', self.store.state['messages']['original']['status'])
        self.assertEqual('processing', self.store.state['messages']['repair']['status'])

    def test_exit_disk_failure_leaves_maintenance_active(self):
        self.enter()
        accepted = self.acceptance()
        with patch.object(self.store, 'save', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.worker.maintenance_action('exit', 'exit', release_id='release', acceptance_file=accepted)
        self.assertTrue(self.store.state['maintenance']['active'])
        self.assertNotIn('completion_intent', self.store.state['maintenance'])

    def test_old_failure_notice_is_cancelled_after_original_resolves(self):
        path, _, _ = self.recovery()
        notice = self.store._queue_lifecycle_notice('old-failure', 'Original failed', 'original')
        notice['failure_source_ids'] = ['original']
        self.store.save()
        self.process(self.store.next_pending_batch(quiet_window_seconds=0), path)
        self.worker.deliver_lifecycle_notices()
        self.client.reply_post.assert_not_called()
        self.assertEqual('cancelled', self.store.state['lifecycle_notices']['old-failure']['status'])

    def test_evidence_changed_during_delivery_does_not_resolve_original(self):
        path, _, artifact = self.recovery()
        def delivery(*args, **kwargs):
            artifact.write_text('changed during delivery', encoding='utf-8')
        self.process(self.store.next_pending_batch(quiet_window_seconds=0), path, delivery)
        self.assertEqual('failed', self.store.state['messages']['original']['status'])
        self.assertNotIn('resolution', self.store.state['messages']['original'])

    def test_legacy_failure_notice_cannot_override_newer_success(self):
        self.enqueue()
        self.store.finish('one', 'completed')
        self.store._queue_lifecycle_notice('task-failed:legacy', 'Old failure', 'one')
        self.store.save()
        self.worker.deliver_lifecycle_notices()
        self.client.reply_post.assert_not_called()
        self.assertEqual('cancelled', self.store.state['lifecycle_notices']['task-failed:legacy']['status'])

    def test_no_source_notification_is_consumed_only_by_listener(self):
        requests, responses = self.root / 'requests', self.root / 'responses'
        service = gateway.GatewayService.__new__(gateway.GatewayService)
        service.workers = {'test': self.worker}
        service.log_path = self.root / 'log'
        with patch.object(gateway, 'SYNC_REQUESTS', requests), patch.object(gateway, 'SYNC_RESPONSES', responses):
            receipt = gateway.queue_task_notice_request('test', 'background-run', 'safe text')
            with patch.object(gateway.time, 'sleep', side_effect=InterruptedError('end fixture loop')):
                with self.assertRaises(InterruptedError):
                    service.process_sync_requests()
            response = json.loads((responses / (receipt['request_id'] + '.json')).read_text())
        self.assertTrue(response['ok'])
        self.assertFalse(list(requests.glob('*.json')))
        self.assertIsNone(self.store.state['lifecycle_notices']['task:background-run']['message_id'])

    def test_failed_catchup_cannot_exit_or_schedule_completion(self):
        self.enter()
        accepted = self.acceptance()
        requests, responses = self.root / 'requests', self.root / 'responses'
        requests.mkdir()
        (requests / 'exit.json').write_text(json.dumps({'project_keys': ['test'],
            'maintenance_action': 'exit', 'release_id': 'release', 'acceptance_file': accepted}), encoding='utf-8')
        service = gateway.GatewayService.__new__(gateway.GatewayService)
        service.workers = {'test': self.worker}
        service.log_path = self.root / 'log'
        with patch.object(gateway, 'SYNC_REQUESTS', requests), patch.object(gateway, 'SYNC_RESPONSES', responses), \
             patch.object(service, 'catch_up', side_effect=RuntimeError('offline')), \
             patch.object(gateway.time, 'sleep', side_effect=InterruptedError('end fixture loop')):
            with self.assertRaises(InterruptedError):
                service.process_sync_requests()
        self.assertTrue(self.store.state['maintenance']['active'])
        self.assertNotIn('completion_intent', self.store.state['maintenance'])
        self.assertFalse(json.loads((responses / 'exit.json').read_text())['ok'])


if __name__ == '__main__':
    unittest.main()
