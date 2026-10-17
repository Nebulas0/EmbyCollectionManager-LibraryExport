import tempfile
import unittest
from pathlib import Path

import yaml
from web import app as web_app
from src.list_metadata import ListMetadataManager
from src.mdblist_processor import MDBListProcessor
from src.trakt_list_processor import TraktListProcessor


class DummyTmdb:
    def get_movie_details(self, tmdb_id):
        return {'id': tmdb_id, 'title': f'Movie {tmdb_id}'}


class ListEditorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        (self.root / 'config').mkdir()
        (self.root / 'traktlists').mkdir()
        (self.root / 'mdblists').mkdir()
        self.old_base_dir = web_app.BASE_DIR
        self.old_config_path = web_app.CONFIG_PATH
        self.old_state_path = web_app.STATE_PATH
        web_app.BASE_DIR = self.temp_dir.name
        web_app.CONFIG_PATH = str(self.root / 'config' / 'config.yaml')
        web_app.STATE_PATH = str(self.root / 'config' / 'webui_state.json')
        Path(web_app.CONFIG_PATH).write_text(yaml.safe_dump({
            'traktlists': {'directory': 'traktlists', 'enabled': True, 'max_items_per_collection': 1},
            'mdblists': {'directory': 'mdblists', 'enabled': True, 'max_items_per_collection': 1},
        }), encoding='utf-8')
        web_app.app.config['TESTING'] = True
        self.client = web_app.app.test_client()

    def tearDown(self):
        web_app.BASE_DIR = self.old_base_dir
        web_app.CONFIG_PATH = self.old_config_path
        web_app.STATE_PATH = self.old_state_path
        self.temp_dir.cleanup()

    def test_editor_merges_file_defaults_with_saved_metadata(self):
        file_path = self.root / 'traktlists' / 'Halloween.yaml'
        file_path.write_text(
            'collection_name: Halloween from YAML\n'
            'item_limit: 30\n'
            'library_ids: [movie-yaml]\n'
            'poster_url: https://example.test/yaml.jpg\n'
            'items: [101, 102]\n',
            encoding='utf-8',
        )
        ListMetadataManager(self.temp_dir.name).set_list_config('traktlists', file_path.name, {
            'collection_name': 'Halloween Picks',
            'item_limit': 5,
            'library_ids': ['movie-saved'],
            'create_library': True,
            'poster_url': 'https://example.test/saved.jpg',
        })

        response = self.client.get('/api/list_editor?type=traktlists&filename=Halloween.yaml')

        self.assertEqual(response.status_code, 200)
        self.assertIn('items: [101, 102]', response.json['content'])
        self.assertEqual(response.json['config']['collection_name'], 'Halloween Picks')
        self.assertEqual(response.json['config']['item_limit'], 5)
        self.assertEqual(response.json['config']['library_ids'], ['movie-saved'])
        self.assertTrue(response.json['config']['create_library'])
        self.assertEqual(response.json['config']['poster_url'], 'https://example.test/saved.jpg')
        self.assertEqual(response.json['config']['seasonal']['start'], '10-01')

    def test_trakt_and_mdblist_pages_share_the_complete_editor_without_library_buttons(self):
        for route in ('/traktlists', '/mdblists'):
            response = self.client.get(route)
            self.assertEqual(response.status_code, 200)
            self.assertIn(b'id="list-item-limit"', response.data)
            self.assertIn(b'id="list-libraries"', response.data)
            self.assertIn(b'id="list-poster-url"', response.data)
            self.assertIn(b'id="create-library"', response.data)
            self.assertIn(b'onclick="saveFile()"', response.data)
            self.assertIn(b'onclick="closeModal()"', response.data)
            self.assertIn(b'async function saveFile()', response.data)
            self.assertIn(b'function closeModal()', response.data)
            self.assertNotIn(b'showLibSelect', response.data)

    def test_per_list_item_limit_overrides_global_limit_for_both_sources(self):
        cases = [
            ('traktlists', TraktListProcessor, 'process_traktlist_file'),
            ('mdblists', MDBListProcessor, 'process_mdblist_file'),
        ]
        for list_type, processor_class, method_name in cases:
            with self.subTest(list_type=list_type):
                file_path = self.root / list_type / 'Limited.txt'
                file_path.write_text('101\n102\n103\n', encoding='utf-8')
                ListMetadataManager(self.temp_dir.name).set_list_config(list_type, file_path.name, {
                    'collection_name': 'Custom Limited',
                    'item_limit': 2,
                    'library_ids': ['selected-library'],
                    'create_library': True,
                    'seasonal': {'enabled': True, 'start': '10-01', 'end': '11-01'},
                })
                config = {
                    'script_dir': self.temp_dir.name,
                    list_type: {
                        'directory': str(file_path.parent),
                        'enabled': True,
                        'max_items_per_collection': 1,
                    },
                }
                processor = processor_class(DummyTmdb(), None, config)
                result = getattr(processor, method_name)(file_path)

                self.assertEqual(result['tmdb_ids'], [101, 102])
                self.assertEqual(result['name'], 'Custom Limited')
                self.assertEqual(result['library_ids'], ['selected-library'])
                self.assertTrue(result['create_library'])
                self.assertEqual(result['item_limit'], 2)
                self.assertEqual(result['seasonal'], {
                    'enabled': True, 'start': '10-01', 'end': '11-01',
                })

    def test_list_settings_validate_seasonal_dates(self):
        response = self.client.post('/api/list_config', json={
            'type': 'mdblists',
            'filename': 'Holiday.txt',
            'config': {
                'seasonal': {'enabled': True, 'start': '14-45', 'end': '12-01'},
            },
        })
        self.assertEqual(response.status_code, 400)


if __name__ == '__main__':
    unittest.main()
