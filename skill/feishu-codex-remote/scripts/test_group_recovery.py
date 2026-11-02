"""Regression: a three-message group was split by the failed[:1] fallback.

No real accounts, Reset, model calls, Feishu sends or production state.
"""
import json
from unittest.mock import patch
import unittest
import queue_batching as qb
import remote_gateway as gateway
from test_queue_batching import item, choose
import test_task_contract as transactions

class GroupSelectionTests(unittest.TestCase):
    def failed_group(self):
        rows=[item('a',100,'*rule'),item('b',149.809,'notify'),item('c',179.448,'strict rule*')]
        for row in rows:
            row.update(status='failed',attempts=1,batch_message_ids=['a','b','c'],batch_id='fixture')
        return rows
    def test_real_spacing_uses_rolling_window_and_keeps_three_sources(self):
        rows=self.failed_group()
        for row in rows:
            row.update(status='pending',attempts=0)
            row.pop('batch_message_ids');row.pop('batch_id')
        selected,delay=qb.select_pending(rows,gateway.forced_single_message,gateway.explicit_routing_mode,now=180)
        self.assertEqual([r['message_id'] for r in selected],['a','b','c'])
        self.assertEqual(delay,0)
    def test_failed_retry_keeps_exact_original_group(self):
        rows=self.failed_group()
        self.assertEqual([r['message_id'] for r in choose(rows,1000)[0]],['a','b','c'])
    def test_later_pending_group_cannot_overtake_recoverable_original_group(self):
        rows=self.failed_group()+[item('d',200,'new work')]
        self.assertEqual([r['message_id'] for r in choose(rows,1000)[0]],['a','b','c'])
    def test_maintenance_pending_does_not_merge_new_message_into_retry(self):
        rows=self.failed_group()
        for row in rows:row['status']='pending'
        rows.append(item('d',200,'new topic'))
        self.assertEqual([r['message_id'] for r in choose(rows,1000)[0]],['a','b','c'])
    def test_zero_charged_attempt_transport_recovery_keeps_claimed_group(self):
        rows=self.failed_group()
        for row in rows:row.update(status='pending',attempts=0,provider_wait='transport')
        rows.append(item('d',200,'new topic'))
        self.assertEqual(choose(rows,1000)[0],[])
        for row in rows[:3]:row.pop('provider_wait')
        self.assertEqual([r['message_id'] for r in choose(rows,1000)[0]],['a','b','c'])
    def test_group_never_replenishes_exhausted_member_or_reopens_completed_one(self):
        for field,value in (('attempts',3),('status','completed')):
            rows=self.failed_group();rows[1][field]=value
            self.assertEqual(choose(rows,1000)[0],[])
    def test_busy_first_turn_leaves_later_supplements_eligible_together(self):
        rows=[item('a',100,'*first',status='processing'),item('b',200,'second'),item('c',230,'third*')]
        self.assertEqual(choose(rows,300)[0],[])
        rows[0]['status']='completed'
        self.assertEqual([r['message_id'] for r in choose(rows,300)[0]],['b','c'])

class GroupWorkerTests(unittest.TestCase):
    setUp=transactions.TransactionTests.setUp
    enqueue=transactions.TransactionTests.enqueue
    final=transactions.TransactionTests.final
    process=transactions.TransactionTests.process
    def test_maintenance_restart_does_not_reopen_exhausted_skill_task_or_renotify(self):
        self.enqueue('skill-task')
        self.store.update_message('skill-task',status='failed',attempts=3,error='original incomplete skill change')
        self.store.enter_maintenance('fixture-maintenance','fixture repair','fixture notice')
        initial_notices = list(self.store.state.get('lifecycle_notices', {}))
        self.store.recover_interrupted();self.store.recover_interrupted()
        row=self.store.state['messages']['skill-task']
        self.assertEqual((row['status'],row['attempts'],row['error']),('failed',3,'original incomplete skill change'))
        self.assertEqual(list(self.store.state.get('lifecycle_notices', {})), initial_notices)
        # Repair the incorrect exhausted-pending projection left by an older
        # runtime, without executing the original task or resetting its budget.
        self.store.update_message('skill-task',status='pending',defer_requested='fixture-maintenance')
        self.store.recover_interrupted()
        self.assertEqual(self.store.state['messages']['skill-task']['status'],'failed')
        self.assertEqual(list(self.store.state.get('lifecycle_notices', {})), initial_notices)
        self.assertEqual(self.store.next_pending_batch(quiet_window_seconds=0),[])
    def test_runtime_retry_keeps_original_group_and_retained_execution_evidence(self):
        for mid in ('a','b','c'):
            self.enqueue(mid)
        effect = self.root / 'effect.txt'
        effect.write_text('A already applied', encoding='utf-8')
        first = self.store.next_pending_batch(quiet_window_seconds=0)
        self.assertEqual(['a','b','c'], [r['message_id'] for r in first])
        with patch.object(gateway, 'run_codex_batch', side_effect=gateway.GatewayError('process fault after fixture-A')):
            self.worker._process_batch(first)
        self.worker = gateway.ProjectWorker('test', self.project, self.client, self.root / 'log')
        self.store = self.worker.store
        self.enqueue('later', 'A later unrelated request')
        batch = self.store.next_pending_batch(quiet_window_seconds=0)
        self.assertEqual(['a','b','c'], [r['message_id'] for r in batch])
        prompt = gateway.build_prompt('test', self.project, gateway.combined_batch_item(batch), [], first_turn=False)
        self.assertIn('fixture-A', prompt)
        self.assertEqual('A already applied', effect.read_text(encoding='utf-8'))
        for mid in ('a','b','c'):
            self.assertIn('message_id: ' + mid, prompt)
        self.assertEqual((1,1), self.process(batch, self.final('Retained A; finished B and C')))
        self.assertEqual('pending', self.store.state['messages']['later']['status'])
        for mid in ('a','b','c'):
            self.assertEqual('completed', self.store.state['messages'][mid]['status'])
            self.assertEqual('c', self.store.state['messages'][mid]['reply_source_message_id'])
            self.assertEqual(2, self.store.state['messages'][mid]['attempts'])


    def test_exhausted_runtime_group_emits_one_failure_not_three(self):
        for mid in ('a','b','c'):
            self.enqueue(mid)
        with patch.object(gateway, 'run_codex_batch', side_effect=gateway.GatewayError('temporary process fault')) as run:
            for attempt in range(3):
                batch = self.store.next_pending_batch(quiet_window_seconds=0)
                self.assertEqual(['a','b','c'], [r['message_id'] for r in batch])
                self.worker._process_batch(batch)
        self.assertEqual(3, run.call_count)
        failures = [r for k,r in self.store.state['lifecycle_notices'].items() if k.startswith('task-failed:')]
        self.assertEqual(1, len(failures))
        self.assertEqual(['a','b','c'], failures[0]['failure_source_ids'])
        self.assertEqual([], self.store.next_pending_batch(quiet_window_seconds=0))


if __name__=='__main__':unittest.main()
