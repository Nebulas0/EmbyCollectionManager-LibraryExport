import unittest
from unittest.mock import patch

from src.app_logic import _sync_collection
from src.app_logic import _sync_library_export


class EmptyCollectionClient:
    def __init__(self):
        self.updated = []

    def get_or_create_collection(self, collection_name):
        return 'collection-id'

    def get_library_item_ids_by_tmdb_ids(self, tmdb_ids, library_ids):
        return []

    def update_collection_items(self, collection_id, item_ids):
        self.updated.append((collection_id, item_ids))
        return True


class SyncCollectionTests(unittest.TestCase):
    def test_empty_results_preserve_default_and_clear_only_when_opted_in(self):
        default_client = EmptyCollectionClient()
        opted_in_client = EmptyCollectionClient()

        self.assertEqual(_sync_collection(default_client, 'Horror', [1]), 'collection-id')
        self.assertEqual(default_client.updated, [])

        self.assertEqual(
            _sync_collection(opted_in_client, 'Horror', [1], clear_when_empty=True),
            'collection-id',
        )
        self.assertEqual(opted_in_client.updated, [('collection-id', [])])

    def test_settings_can_skip_automatic_emby_library_creation(self):
        emby = EmptyCollectionClient()
        config = {
            'library_exports': {
                'media_root': '/media',
                'output_root': '/exports',
                'auto_create_emby_library': False,
            },
        }
        with patch('src.app_logic.CollectionLibraryExporter') as exporter_class:
            _sync_library_export(emby, 'collection-id', 'Horror', config, True)

        exporter_class.return_value.export_collection.assert_called_once_with(
            'collection-id',
            'Horror',
            create_emby_library=False,
        )


if __name__ == '__main__':
    unittest.main()
