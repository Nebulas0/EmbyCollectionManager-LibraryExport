import json
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
        self.original_most_watched_state_path = web_app.MOST_WATCHED_STATE_PATH
        self.original_base_dir = web_app.BASE_DIR
        web_app.BASE_DIR = self.temp_dir.name
        web_app.CONFIG_PATH = str(config_dir / 'config.yaml')
        web_app.STATE_PATH = str(config_dir / 'webui_state.json')
        web_app.MOST_WATCHED_STATE_PATH = str(config_dir / 'most_watched_results.json')
        web_app.app.config['TESTING'] = True
        self.client = web_app.app.test_client()

    def tearDown(self):
        web_app.CONFIG_PATH = self.original_config_path
        web_app.STATE_PATH = self.original_state_path
        web_app.MOST_WATCHED_STATE_PATH = self.original_most_watched_state_path
        web_app.BASE_DIR = self.original_base_dir
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

    def test_collection_recipe_auto_library_override_is_saved(self):
        response = self.client.post('/api/recipe_override', json={
            'recipe_name': 'Horror Movies',
            'override': {
                'create_library': True,
                'auto_create_emby_library': False,
            },
        })

        self.assertEqual(response.status_code, 200)
        saved = json.loads((Path(self.temp_dir.name) / 'config' / 'recipe_overrides.json').read_text())
        self.assertFalse(saved['overrides']['Horror Movies']['auto_create_emby_library'])

    def test_collections_editor_exposes_per_recipe_auto_library_selector(self):
        response = self.client.get('/collections')

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'id="detail-auto-create-library"', response.data)
        self.assertIn(b'id="dup-auto-create-library"', response.data)

    def test_most_watched_tab_saves_independent_movie_and_tv_ranges(self):
        libraries = [
            {'Id': 'movie-lib', 'Name': 'Movies', 'CollectionType': 'movies', 'Locations': ['/media/movies']},
            {'Id': 'tv-lib', 'Name': 'Television', 'CollectionType': 'tvshows', 'Locations': ['/media/tv']},
        ]
        with patch.object(web_app, 'get_emby_libraries', return_value=libraries):
            response = self.client.post('/most-watched', data={
                'movies_enabled': 'on',
                'movies_days': '21',
                'movies_limit': '12',
                'movies_library_ids': 'movie-lib',
                'movies_output_mode': 'strm',
                'movies_image_variants': ['poster', 'thumb'],
                'movies_badge_style': 'ocean-blue',
                'most_watched_auto_create_emby_library': 'off',
                'tv_shows_enabled': 'on',
                'tv_shows_days': '14',
                'tv_shows_limit': '8',
                'tv_shows_library_ids': 'tv-lib',
                'tv_shows_output_mode': 'symlink',
                'tv_shows_image_variants': ['poster'],
                'tv_shows_badge_style': 'soft-red',
            })

        self.assertEqual(response.status_code, 200)
        saved = yaml.safe_load(Path(web_app.CONFIG_PATH).read_text(encoding='utf-8'))
        self.assertEqual(saved['most_watched']['movies'], {
            'enabled': True, 'days': 21, 'limit': 12, 'library_ids': ['movie-lib'],
            'target_library_id': None,
            'output_mode': 'strm', 'image_variants': ['poster', 'thumb'], 'badge_style': 'ocean-blue',
        })
        self.assertEqual(saved['most_watched']['tv_shows'], {
            'enabled': True, 'days': 14, 'limit': 8, 'library_ids': ['tv-lib'],
            'target_library_id': None,
            'output_mode': 'symlink', 'image_variants': ['poster'], 'badge_style': 'soft-red',
        })
        self.assertFalse(saved['most_watched']['auto_create_emby_library'])

    def test_most_watched_can_target_existing_export_library_for_refresh(self):
        libraries = [
            {'Id': 'movies-source', 'Name': 'Movies', 'CollectionType': 'movies', 'Locations': ['/media/movies']},
            {
                'Id': 'top-movies',
                'Name': 'Top Played Movies',
                'CollectionType': 'movies',
                'Locations': ['/mnt/local/exports/Top Played Movies'],
            },
        ]
        with patch.object(web_app, 'DEFAULT_OUTPUT_ROOT', '/mnt/local/exports'), patch.object(
            web_app,
            'get_emby_libraries',
            return_value=libraries,
        ):
            response = self.client.post('/most-watched', data={
                'movies_enabled': 'on',
                'movies_days': '14',
                'movies_limit': '10',
                'movies_library_ids': 'movies-source',
                'movies_target_library_id': 'top-movies',
                'movies_output_mode': 'symlink',
                'movies_image_variants': ['poster', 'thumb'],
                'movies_badge_style': 'neutral',
            })

        self.assertEqual(response.status_code, 200)
        saved = yaml.safe_load(Path(web_app.CONFIG_PATH).read_text(encoding='utf-8'))
        self.assertEqual(saved['most_watched']['movies']['target_library_id'], 'top-movies')

    def test_most_watched_target_selector_is_visible(self):
        libraries = [{
            'Id': 'top-movies',
            'Name': 'Top Played Movies',
            'CollectionType': 'movies',
            'Locations': ['/mnt/local/exports/Top Played Movies'],
        }]
        with patch.object(web_app, 'DEFAULT_OUTPUT_ROOT', '/mnt/local/exports'), patch.object(
            web_app,
            'get_emby_libraries',
            return_value=libraries,
        ):
            response = self.client.get('/most-watched')

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'name="movies_target_library_id"', response.data)
        self.assertIn(b'Top Played Movies', response.data)

    def test_most_watched_page_shows_default_values_and_plugin_setup_hint(self):
        response = self.client.get('/most-watched')

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Playback Reporting', response.data)
        self.assertIn(b'STRM pointers (HomeScreenCompanion-style)', response.data)
        self.assertIn(b'Thumb / landscape', response.data)
        self.assertIn(b'value="14"', response.data)
        self.assertIn(b'value="10"', response.data)

    def test_seasonal_recipe_override_uses_holiday_preset_and_validates_dates(self):
        page = self.client.get('/collections')
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'id="detail-seasonal-enabled"', page.data)
        self.assertIn(b'id="detail-season-start"', page.data)

        detail = self.client.get('/api/recipe_detail/Christmas')
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json['seasonal_defaults'], {'start': '12-01', 'end': '01-02'})

        response = self.client.post('/api/recipe_override', json={
            'recipe_name': 'Christmas',
            'override': {
                'seasonal': {'enabled': True, 'start': '', 'end': ''},
            },
        })
        self.assertEqual(response.status_code, 200)
        saved = json.loads((Path(self.temp_dir.name) / 'config' / 'recipe_overrides.json').read_text())
        self.assertEqual(saved['overrides']['Christmas']['seasonal'], {
            'enabled': True, 'start': '12-01', 'end': '01-02',
        })

        invalid = self.client.post('/api/recipe_override', json={
            'recipe_name': 'Christmas',
            'override': {'seasonal': {'enabled': True, 'start': '13-40', 'end': '01-02'}},
        })
        self.assertEqual(invalid.status_code, 400)


if __name__ == '__main__':
    unittest.main()
