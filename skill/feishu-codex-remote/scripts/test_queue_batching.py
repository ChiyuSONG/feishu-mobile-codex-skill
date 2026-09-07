import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock
import queue_batching as qb
import remote_gateway as gateway

def item(mid, received=100, text="ordinary", status="pending", sender="u1", sent=None):
    return {"message_id": mid, "chat_id": "chat", "sender": {"id": sender},
            "create_time": int((received if sent is None else sent)*1000),
            "received_at": datetime.fromtimestamp(received,timezone.utc).isoformat(),
            "message_type":"text","content":json.dumps({"text":text}),
            "status":status,"attempts":0}

def choose(rows, now):
    return qb.select_pending(rows, gateway.forced_single_message,
                             gateway.explicit_routing_mode, now=now)

class QuietWindowTests(unittest.TestCase):
    def test_waits_exactly_until_last_receipt_plus_fifteen(self):
        rows=[item("a")]
        self.assertEqual(choose(rows,114.9)[0],[])
        self.assertAlmostEqual(choose(rows,114.9)[1],.1)
        self.assertEqual([x["message_id"] for x in choose(rows,115)[0]],["a"])

    def test_followup_resets_entire_unstarted_prefix(self):
        rows=[item("a",100), item("b",114)]
        self.assertEqual(choose(rows,115),( [],14))
        self.assertEqual(len(choose(rows,129)[0]),2)

    def test_busy_backlog_has_no_count_or_time_gap_cap(self):
        rows=[item(str(i),100+i*600) for i in range(12)]
        self.assertEqual(len(choose(rows,10000)[0]),12)

    def test_star_flushes_before_and_isolates_then_waits_after(self):
        rows=[item("a",100),item("b",101,"  * urgent"),item("c",102)]
        self.assertEqual([x["message_id"] for x in choose(rows,102)[0]],["a"])
        rows[0]["status"]="completed"
        self.assertEqual([x["message_id"] for x in choose(rows,102)[0]],["b"])
        rows[1]["status"]="completed"
        self.assertEqual(choose(rows,102),([],15))

    def test_active_batch_blocks_new_claim(self):
        self.assertEqual(choose([item("a",status="processing"),item("b")],1000),([],None))

    def test_failed_history_never_joins_new_pending(self):
        rows=[item("old",0,status="failed"),item("new",100)]
        self.assertEqual([x["message_id"] for x in choose(rows,115)[0]],["new"])

    def test_different_senders_are_separate(self):
        rows=[item("a"),item("b",101,sender="u2")]
        self.assertEqual([x["message_id"] for x in choose(rows,200)[0]],["a"])

    def test_legacy_receipt_falls_back_to_platform_time(self):
        row=item("a"); del row["received_at"]
        self.assertEqual(choose([row],114),([],1))

    def test_response_modes_stay_separate_without_truncating_quiet_wait(self):
        rows=[item("a",100,"/doc report"),item("b",110,"/direct summary")]
        self.assertEqual(choose(rows,115),([],10))
        self.assertEqual([x["message_id"] for x in choose(rows,125)[0]],["a"])

    def test_queue_restart_and_duplicate_do_not_reset_wait(self):
        with tempfile.TemporaryDirectory() as raw, patch.object(gateway,"REMOTE_STATE",Path(raw)):
            store=gateway.ProjectStore("test")
            with patch.object(gateway,"now_iso",return_value=datetime.fromtimestamp(100,timezone.utc).isoformat()):
                self.assertTrue(store.enqueue(item("a")))
            with patch.object(gateway,"now_iso",return_value=datetime.fromtimestamp(110,timezone.utc).isoformat()):
                self.assertFalse(store.enqueue(item("a")))
            restarted=gateway.ProjectStore("test")
            with patch.object(qb.time,"time",return_value=114):
                self.assertEqual(restarted.next_pending_batch(),[])
                self.assertEqual(restarted.pending_wait_seconds(),1)
                self.assertEqual(restarted.state["messages"]["a"]["status"],"pending")
            with patch.object(qb.time,"time",return_value=115):
                self.assertEqual(len(restarted.next_pending_batch()),1)
                self.assertEqual(restarted.state["messages"]["a"]["status"],"processing")

    def test_bot_does_not_reset_wait(self):
        with tempfile.TemporaryDirectory() as raw, patch.object(gateway,"REMOTE_STATE",Path(raw)):
            store=gateway.ProjectStore("test")
            with patch.object(gateway,"now_iso",return_value=datetime.fromtimestamp(100,timezone.utc).isoformat()):
                store.enqueue(item("a"))
            bot=item("bot",114); bot["sender"]={"sender_type":"app"}
            self.assertFalse(store.enqueue(bot))
            with patch.object(qb.time,"time",return_value=115):
                self.assertEqual(len(store.next_pending_batch()),1)

    def test_pending_cannot_add_typing_or_execute(self):
        worker=object.__new__(gateway.ProjectWorker)
        with tempfile.TemporaryDirectory() as raw, patch.object(gateway,"REMOTE_STATE",Path(raw)):
            worker.store=gateway.ProjectStore("test")
            worker.store.enqueue(item("a"))
            worker.client=MagicMock()
            with self.assertRaises(gateway.GatewayError):
                worker._process_batch([item("a")])
            worker.client.add_reaction.assert_not_called()

if __name__=="__main__":
    unittest.main()
