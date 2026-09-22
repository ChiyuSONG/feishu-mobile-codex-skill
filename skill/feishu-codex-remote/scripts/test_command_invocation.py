"""Actual shell transport and generated patrol instructions."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from command_invocation import exec_spec
import remote_gateway as gateway


class CommandInvocationTests(unittest.TestCase):
    def test_posix_round_trip(self):
        argv = ["/a path/python", "/中文 it's/script.py", "--key", "value & $HOME"]
        spec = exec_spec(argv, windows=False)
        self.assertEqual(shlex.split(spec["cmd"]), argv)
        self.assertFalse(spec["login"])
        self.assertFalse(spec["tty"])

    @unittest.skipUnless(os.name == "nt", "Windows command transport")
    def test_real_cmd_preserves_unicode_spaces_special_characters_and_exit(self):
        with tempfile.TemporaryDirectory(prefix="command 中文 & ' ") as raw:
            root = Path(raw)
            script = root / "echo args.py"
            script.write_text(
                "import json,sys\nfrom pathlib import Path\n"
                "Path(__file__).with_name('result.json').write_text(json.dumps(sys.argv[1:]),encoding='utf-8')\n"
                "sys.exit(7)\n", encoding="utf-8")
            values = ["中文 with spaces", "x&y", "%PATH%", "$name", "it's literal", "a;b"]
            spec = exec_spec([sys.executable, str(script), *values], windows=True)
            proc = subprocess.run([spec["shell"], "/d", "/c", spec["cmd"]],
                                  capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=30)
            self.assertEqual(proc.returncode, 7, proc.stderr)
            self.assertEqual(json.loads((root / "result.json").read_text()), values)

    def test_generated_prompt_has_one_explicit_noninteractive_spec(self):
        with patch.object(gateway, "SCRIPT_DIR", Path("C:/fixture 中文 & space")):
            text = gateway.automation_prompt("fixture", "unused")
        spec = json.loads(text.split("执行参数：", 1)[1])
        self.assertFalse(spec["login"])
        self.assertFalse(spec["tty"])
        if os.name == "nt":
            self.assertEqual(spec["shell"], "cmd.exe")
            self.assertIn("-EncodedCommand", spec["cmd"])
