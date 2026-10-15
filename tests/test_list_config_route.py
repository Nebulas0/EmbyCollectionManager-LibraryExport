import tempfile
import unittest
from pathlib import Path

from web import app as web_app


class ListConfigRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_base_dir = web_app.BASE_DIR
        web_app.BASE_DIR = self.temp_dir.name
        (Path(self.temp_dir.name) / 'config').mkdir()
        web_app.app.config['TESTING'] = True
        self.client = web_app.app.test_client()

    def tearDown(self):
        web_app.BASE_DIR = self.original_base_dir
        self.temp_dir.cleanup()

    def test_partial_updates_preserve_existing_list_metadata(self):
        first = self.client.post('/api/list_config', json={
            'type': 'traktlists',
            'filename': 'Horror.txt',
            'config': {
                'library_ids': ['movies'],
                'collection_name': 'Horror Picks',
            },
        })
        second = self.client.post('/api/list_config', json={
            'type': 'traktlists',
            'filename': 'Horror.txt',
            'config': {'create_library': True},
        })

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        response = self.client.get('/api/list_config?type=traktlists')
        self.assertEqual(response.json['Horror.txt'], {
            'library_ids': ['movies'],
            'collection_name': 'Horror Picks',
            'create_library': True,
        })


if __name__ == '__main__':
    unittest.main()
