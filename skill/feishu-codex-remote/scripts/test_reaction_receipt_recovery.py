"""Synthetic remote reactions and real atomic-write faults; no live accounts."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
import remote_gateway as g

class ReactionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        p=patch.object(g,'REMOTE_STATE',self.root);p.start();self.addCleanup(p.stop)
        self.client=MagicMock();self.client.app_id='app';self.remote={};self.counter=0
        def add(mid,emoji):
            self.counter+=1;rid=str(self.counter);self.remote[rid]=(mid,emoji)
            return {'data':{'reaction_id':rid}}
        def remove(mid,rid):self.remote.pop(rid,None)
        def listing(mid,emoji):return [{'reaction_id':rid,'operator':{'operator_type':'app','operator_id':'app'}} for rid,p in self.remote.items() if p==(mid,emoji)]
        self.client.add_reaction.side_effect=add;self.client.remove_reaction.side_effect=remove;self.client.list_reactions.side_effect=listing
        self.worker=g.ProjectWorker('test',{'chat_id':'chat','working_directory':str(self.root)},self.client,self.root/'log')
        self.s=self.worker.store
    def put(self,mid='m',text='# branch'):
        self.s.enqueue({'message_id':mid,'chat_id':'chat','message_type':'text','create_time':'1000','content':json.dumps({'text':text}),'sender':{'sender_type':'user'}})
        self.s.update_message(mid,status='processing')
        self.s.claim_run([mid]);self.s.save()
        return dict(self.s.state['messages'][mid])
    def test_typing_receipt_survives_failed_atomic_replace_main_and_hash(self):
        for text in ['normal','# parallel']:
            with self.subTest(text=text):
                item=self.put(text,text)
                real=g.atomic_write_json;failed=[]
                def disk(path,value):
                    if not failed:failed.append(True);raise PermissionError(5,'simulated Windows sharing violation')
                    return real(path,value)
                with patch.object(g,'atomic_write_json',side_effect=disk):rid=self.worker._start_working_reaction(item)
                self.assertTrue(rid);self.assertEqual(len(self.remote),1)
                self.s.update_message(text,status='completed')
                self.worker._replace_working_with_completion(text,'')
                self.assertEqual([v[1] for v in self.remote.values()],['CheckMark'])
                self.remote.clear()
    def test_remote_receipt_missing_locally_is_discovered_before_check(self):
        self.put();self.client.add_reaction('m','Typing');self.s.update_message('m',status='completed')
        self.worker._replace_working_with_completion('m','')
        self.assertEqual([p[1] for p in self.remote.values()],['CheckMark'])
    def test_cleanup_failure_cannot_add_check_and_next_cycle_repairs(self):
        item=self.put();rid=self.worker._start_working_reaction(item);self.s.update_message('m',status='completed')
        original=self.client.remove_reaction.side_effect
        self.client.remove_reaction.side_effect=ConnectionError('offline')
        self.assertTrue(self.worker._replace_working_with_completion('m',rid))
        self.assertEqual([p[1] for p in self.remote.values()],['Typing'])
        self.client.remove_reaction.side_effect=original
        self.worker.reconcile_reactions(dirty_only=True)
        self.assertEqual([p[1] for p in self.remote.values()],['CheckMark'])
    def test_existing_remote_check_receipt_is_saved_without_duplicate_send(self):
        self.put()
        self.s.update_message('m', status='completed')
        self.client.add_reaction('m', 'CheckMark')
        calls = self.client.add_reaction.call_count
        self.worker.reconcile_reactions(dirty_only=True)
        self.assertEqual(calls, self.client.add_reaction.call_count)
        self.assertTrue(self.s.state['messages']['m']['completion_reaction_id'])
        self.assertFalse(self.s.state['messages']['m'].get('reaction_sync_error'))
