import os
from pathlib import Path
from tempfile import TemporaryDirectory
import tkinter as tk
from unittest import TestCase
from unittest.mock import patch

import studio


class Startup(TestCase):
    def test_all_pages_initialize_and_bind_evidence(self):
        with TemporaryDirectory() as folder, patch.dict(os.environ,SLASHER_STUDIO_SETTINGS=folder):
            root=tk.Tk()
            root.withdraw()
            try:
                app=studio.Studio(root)
                root.update()
                self.assertEqual(len(app.tabs.tabs()),3)
                self.assertIn('evidence',app.fields)
                self.assertTrue(app.snapshot_files.winfo_exists())
                self.assertTrue(app.unresolved_open.winfo_exists())
                self.assertFalse((Path(folder)/'settings.json').exists())
            finally:
                root.destroy()
