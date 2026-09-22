"""Release-only reminder behavior; no production scheduling or network."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import remote_gateway as gateway


class ReminderDeliveryTests(unittest.TestCase):
    def test_missing_or_failed_receipt_hands_off_safe_failure_without_marking_sent(self):
        for result in [RuntimeError('private diagnostic'), {}]:
            with self.subTest(result=type(result).__name__), tempfile.TemporaryDirectory() as temporary:
                client = MagicMock()
                client.tenant_info.return_value = {'tenant_key': 'tenant'}
                if isinstance(result, Exception):
                    client.send_post.side_effect = result
                else:
                    client.send_post.return_value = result
                config = {'expected_tenant_key': 'tenant', 'projects': {'test': {'chat_id': 'chat'}}}
                with patch.object(gateway, 'due_reminders', return_value=[{'reminder_id': 'reminder', 'text': 'Example'}]), \
                     patch.object(gateway, 'load_app_credentials', return_value=('app', 'fixture')), \
                     patch.object(gateway, 'FeishuClient', return_value=client), \
                     patch.object(gateway, 'mark_sent') as sent, \
                     patch.object(gateway, 'SYNC_REQUESTS', Path(temporary)):
                    with self.assertRaises(Exception):
                        gateway.send_due_reminders(config, 'test')
                sent.assert_not_called()
                notices = list(Path(temporary).glob('*.json'))
                self.assertEqual(1, len(notices))
                payload = json.loads(notices[0].read_text(encoding='utf-8'))
                self.assertIsNone(payload['task_notice']['message_id'])
                self.assertNotIn('private diagnostic', payload['task_notice']['text'])

    def test_success_and_no_due_reminders_preserve_original_behavior(self):
        client = MagicMock()
        client.tenant_info.return_value = {'tenant_key': 'tenant'}
        client.send_post.return_value = {'data': {'message_id': 'delivered'}}
        config = {'expected_tenant_key': 'tenant', 'projects': {'test': {'chat_id': 'chat', 'language': 'en'}}}
        with patch.object(gateway, 'due_reminders', return_value=[{'reminder_id': 'reminder', 'text': 'Example'}]) as due, \
             patch.object(gateway, 'load_app_credentials', return_value=('app', 'fixture')), \
             patch.object(gateway, 'FeishuClient', return_value=client), \
             patch.object(gateway, 'mark_sent') as sent, \
             patch.object(gateway, 'queue_task_notice_request') as notice:
            self.assertEqual([{'reminder_id': 'reminder', 'message_id': 'delivered'}], gateway.send_due_reminders(config, 'test'))
            sent.assert_called_once_with('test', 'reminder')
            notice.assert_not_called()
            due.return_value = []
            self.assertEqual([], gateway.send_due_reminders(config, 'test'))
            self.assertEqual(1, client.send_post.call_count)
