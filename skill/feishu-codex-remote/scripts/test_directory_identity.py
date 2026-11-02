"""Real directory aliases and read-only source profile lookup; synthetic database."""
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import closing
import project_profiles as profiles
from gateway_common import GatewayError

class DirectoryIdentityTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows extended namespace')
    def test_extended_windows_path_is_same_project_but_other_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            log = root / 'session.jsonl'
            log.write_text(json.dumps({'type':'turn_context', 'payload':{'model':'fixture-model'}}), encoding='utf-8')
            database = root / 'state.sqlite'
            with closing(sqlite3.connect(database)) as db:
                db.execute('CREATE TABLE threads (id TEXT, rollout_path TEXT, cwd TEXT)')
                db.execute('INSERT INTO threads VALUES (?,?,?)', ('source', str(log), chr(92) * 2 + '?' + chr(92) + str(root)))
                db.commit()
            before = (database.read_bytes(), log.read_bytes())
            self.assertEqual({'model':'fixture-model'}, profiles.source_task_profile('source', root, database=database))
            other = root / 'other'
            other.mkdir()
            with self.assertRaises(GatewayError):
                profiles.source_task_profile('source', other, database=database)
            self.assertEqual(before, (database.read_bytes(), log.read_bytes()))
