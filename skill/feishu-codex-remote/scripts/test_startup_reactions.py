import json
from pathlib import Path
import tempfile
import unittest
import threading
from unittest.mock import MagicMock, patch

import remote_gateway as gateway


class StartupReactionTests(unittest.TestCase):
    def test_slow_project_reconciliation_does_not_block_other_startup(self):
        slow = MagicMock()
        fast = MagicMock()
        entered = threading.Event()
        release = threading.Event()
        ready = threading.Event()
        def wait():
            entered.set()
            release.wait(5)
        slow.reconcile_reactions.side_effect = wait
        fast.start.side_effect = ready.set
        service = gateway.GatewayService.__new__(gateway.GatewayService)
        service.workers = {'slow':slow, 'fast':fast}
        try:
            service.start_workers()
            self.assertTrue(entered.wait(2))
            self.assertTrue(ready.wait(2))
            slow.start.assert_not_called()
        finally:
            release.set()

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        patched = patch.object(gateway, 'REMOTE_STATE', root)
        patched.start()
        self.addCleanup(patched.stop)
        self.client = MagicMock()
        self.client.app_id = 'app'
        self.worker = gateway.ProjectWorker('test', {'working_directory':str(root), 'chat_id':'chat'}, self.client, root/'log')
        self.worker.store.enqueue({'message_id':'m','chat_id':'chat','create_time':'1000','message_type':'text','content':json.dumps({'text':'test'}),'sender':{'sender_type':'user'}})
        self.client.list_reactions.return_value = []

    def test_completed_clean_receipt_needs_no_network(self):
        self.worker.store.update_message('m', status='completed', completion_reaction_id='done', working_reaction_active=False)
        self.worker.reconcile_reactions()
        self.client.list_reactions.assert_not_called()

    def test_missing_completed_receipt_is_repaired(self):
        self.worker.store.update_message('m', status='completed', working_reaction_active=False)
        self.client.add_reaction.return_value = {'data':{'reaction_id':'done'}}
        self.worker.reconcile_reactions()
        self.client.add_reaction.assert_called_once_with('m',gateway.COMPLETION_REACTION)

    def test_error_is_retried_then_cleared(self):
        self.worker.store.update_message('m', status='completed', completion_reaction_id='done', working_reaction_active=False, reaction_sync_error='offline')
        self.worker.reconcile_reactions()
        self.assertEqual({'Typing', 'CheckMark', 'CrossMark'},
                         {call.args[1] for call in self.client.list_reactions.call_args_list})
        self.assertIsNone(self.worker.store.state['messages']['m']['reaction_sync_error'])

    def test_failed_cleanup_error_stays_retryable(self):
        self.worker.store.update_message('m', status='failed', working_reaction_active=True)
        self.client.list_reactions.side_effect = RuntimeError('offline')
        self.worker.reconcile_reactions()
        self.assertEqual('offline', self.worker.store.state['messages']['m']['reaction_sync_error'])
        self.client.add_reaction.assert_not_called()

    def test_pending_has_no_completion_and_does_not_get_claimed(self):
        self.client.list_reactions.return_value = [{'operator':{'operator_type':'app','operator_id':'app'},'reaction_id':'stale'}]
        self.worker.reconcile_reactions()
        self.assertEqual('pending', self.worker.store.state['messages']['m']['status'])
        self.assertEqual(3,self.client.remove_reaction.call_count)
        self.client.add_reaction.assert_not_called()

    def test_bare_star_is_not_promoted_to_completed_business(self):
        self.worker.store.update_message('m', status='completed', control_action='flush_previous_batch')
        self.worker.reconcile_reactions()
        self.client.list_reactions.assert_not_called()
