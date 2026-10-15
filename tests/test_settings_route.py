import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from web import app as web_app


class LibraryExportSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        config_dir = Path(self.temp_dir.name) / 'config'
        config_dir.mkdir()
        self.original_config_path = web_app.CONFIG_PATH
        self.original_state_path = web_app.STATE_PATH
        web_app.CONFIG_PATH = str(config_dir / 'config.yaml')
        web_app.STATE_PATH = str(config_dir / 'webui_state.json')
        web_app.app.config['TESTING'] = True
        self.client = web_app.app.test_client()

    def tearDown(self):
        web_app.CONFIG_PATH = self.original_config_path
        web_app.STATE_PATH = self.original_state_path
        self.temp_dir.cleanup()

    def test_auto_library_setting_is_saved_as_disabled(self):
        with patch.object(
            web_app,
            'validate_library_export_paths',
            return_value=('/mnt/unionfs', '/mnt/local/exports'),
        ):
            response = self.client.post('/settings', data={
                'library_export_media_root': '/mnt/unionfs',
                'library_export_output_root': '/mnt/local/exports',
                'sync_interval': '24',
            })

        self.assertEqual(response.status_code, 200)
        saved = yaml.safe_load(Path(web_app.CONFIG_PATH).read_text(encoding='utf-8'))
        self.assertFalse(saved['library_exports']['auto_create_emby_library'])
        self.assertEqual(saved['library_exports']['media_root'], '/mnt/unionfs')

    def test_auto_library_setting_is_saved_as_enabled(self):
        with patch.object(
            web_app,
            'validate_library_export_paths',
            return_value=('/mnt/unionfs', '/mnt/local/exports'),
        ):
            response = self.client.post('/settings', data={
                'library_export_media_root': '/mnt/unionfs',
                'library_export_output_root': '/mnt/local/exports',
                'library_export_auto_create_emby_library': 'on',
                'sync_interval': '24',
            })

        self.assertEqual(response.status_code, 200)
        saved = yaml.safe_load(Path(web_app.CONFIG_PATH).read_text(encoding='utf-8'))
        self.assertTrue(saved['library_exports']['auto_create_emby_library'])


if __name__ == '__main__':
    unittest.main()
