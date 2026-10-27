import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import os
import state_sync


class StateTests(unittest.TestCase):
    def test_pull_and_push_preserve_private_state(self):
        state = {"last_snapshot": {"signals": {"Test": "signal"}},
                 "last_discovery": {"recent": ["TEST"]}}
        with tempfile.TemporaryDirectory() as folder:
            previous = os.getcwd()
            try:
                os.chdir(folder)
                with patch.object(state_sync, "load_remote", return_value=("block", state)):
                    state_sync.sync("pull")
                    self.assertEqual(json.loads(Path("last_snapshot.json").read_text()),
                                     state["last_snapshot"])
                    Path("last_discovery.json").write_text('{"recent": ["NEW"]}')
                    with patch.object(state_sync, "request") as request:
                        state_sync.sync("push")
                        method, path, body = request.call_args.args
                        self.assertEqual((method, path), ("PATCH", "blocks/block"))
                        saved = json.loads("".join(t["text"]["content"] for t in body["code"]["rich_text"]))
                        self.assertEqual(saved["last_snapshot"], state["last_snapshot"])
                        self.assertEqual(saved["last_discovery"]["recent"], ["NEW"])
            finally:
                os.chdir(previous)

    def test_state_read_failure_does_not_overwrite_remote(self):
        with patch.object(state_sync, "load_remote", side_effect=RuntimeError()), \
                patch.object(state_sync, "request") as request:
            with self.assertRaises(RuntimeError):
                state_sync.sync("push")
            request.assert_not_called()

    def test_long_state_is_split_into_safe_rich_text_chunks(self):
        state = {"last_snapshot": {"signals": {"x": "a" * 5000}}}
        with tempfile.TemporaryDirectory() as folder:
            previous = os.getcwd()
            try:
                os.chdir(folder)
                with patch.object(state_sync, "load_remote", return_value=("block", state)), \
                        patch.object(state_sync, "request") as request:
                    state_sync.sync("push")
                    parts = request.call_args.args[2]["code"]["rich_text"]
                    self.assertTrue(all(len(p["text"]["content"]) <= 1800 for p in parts))
                    self.assertEqual(json.loads("".join(p["text"]["content"] for p in parts)), state)
            finally:
                os.chdir(previous)

