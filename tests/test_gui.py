import json
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

import app
from tests.test_release import MemoryVault, SECRET


class GuiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="resetnotice-gui-test-")
        self.root = tk.Tk()
        self.root.withdraw()
        self.mock = patch.object(app, "WindowsVault", lambda directory: MemoryVault())
        self.mock.start()
        self.dialog = patch.object(app.messagebox, "showerror", side_effect=AssertionError("Unexpected GUI validation error"))
        self.dialog.start()
        self.ui = app.App(self.root, Path(self.temp.name))
        self.root.update_idletasks()
    def tearDown(self):
        if self.ui.worker:
            self.ui.stop_event.set()
            self.ui.worker.join(timeout=5)
        self.ui.store.close()
        self.root.destroy()
        self.mock.stop()
        self.dialog.stop()
        self.temp.cleanup()
    def test_startup_is_stopped_and_secrets_are_opt_in(self):
        self.assertFalse(self.ui.running)
        self.assertIsNone(self.ui.worker)
        self.assertFalse(self.ui.vars["smtp_saved"].get())
        self.assertFalse(self.ui.vars["bark_saved"].get())
        self.assertEqual(len(self.ui.book.tabs()), 5)
    def test_save_configuration_masks_secrets_and_never_serializes_them(self):
        self.ui.vars["smtp_host"].set("smtp.example.invalid")
        self.ui.vars["smtp_user"].set("login@example.invalid")
        self.ui.vars["sender"].set("sender@example.invalid")
        self.ui.vars["recipients"].set("one@example.invalid; two@example.invalid")
        self.ui.vars["smtp_enabled"].set(True)
        self.ui.vars["smtp_saved"].set(True)
        self.ui.smtp_password.set(SECRET)
        self.assertTrue(self.ui.save())
        self.assertEqual(self.ui.smtp_password.get(), "")
        text = Path(self.temp.name, "config.json").read_text(encoding="utf-8")
        self.assertNotIn(SECRET, text)
        self.assertEqual(len(json.loads(text)["recipients"]), 2)
        self.assertNotIn(SECRET, json.dumps(self.ui.store.snapshot()))
    def test_actual_test_declined_never_creates_send_or_network(self):
        self.ui.vars["bark_enabled"].set(True)
        self.ui.bark_key.set(SECRET)
        with patch.object(app.messagebox, "askyesno", return_value=False), patch.object(self.ui.sender, "drain") as drain:
            self.ui.real_test()
        drain.assert_not_called()
        self.assertEqual(self.ui.store.snapshot()["queue"], [])
    def test_mock_test_has_no_production_transport(self):
        with patch.object(app.Transport, "send", side_effect=AssertionError("External send forbidden")):
            self.ui.mock_test()
            self.ui.worker.join(timeout=5)
        self.assertTrue(self.ui.done_event.is_set())
        self.assertEqual(self.ui.store.snapshot()["queue"], [])
        self.assertEqual(self.ui.store.snapshot()["logs"][0]["code"], "MOCK_OK")
    def test_autostart_requires_ui_confirmation_and_uses_own_value(self):
        self.ui.vars["reader_contact"].set("mailto:author@example.invalid")
        with patch.object(app.messagebox, "askyesno", return_value=False), patch.object(app, "change_startup") as mutation:
            self.ui.startup(True)
            mutation.assert_not_called()
        with patch.object(app.messagebox, "askyesno", return_value=True), patch.object(app, "change_startup") as mutation:
            self.ui.startup(True)
            mutation.assert_called_once_with(True, Path(self.temp.name))


if __name__ == "__main__":
    unittest.main(verbosity=2)
