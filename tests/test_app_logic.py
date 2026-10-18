import unittest
from unittest.mock import patch

from src.app_logic import (
    _apply_collection_sorting,
    _expire_seasonal_collection,
    _remove_managed_collection,
    _should_generate_list_backdrop,
    _sync_collection,
    _sync_library_export,
)


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


class OrderedCollectionClient(EmptyCollectionClient):
    def __init__(self):
        super().__init__()
        self.sort_name = None
        self.item_sort = None
        self.get_library_item_ids_by_tmdb_ids = lambda tmdb_ids, library_ids: ['old', 'new']

    def sort_item_ids_by_year(self, item_ids):
        return ['new', 'old']

    def update_collection_sort_name(self, collection_id, sort_name, collection_name):
        self.sort_name = (collection_id, sort_name, collection_name)
        return True

    def update_collection_item_sort(self, collection_id):
        self.item_sort = collection_id
        return True


class SyncCollectionTests(unittest.TestCase):
    def test_custom_poster_does_not_trigger_an_unrequested_backdrop_fallback(self):
        self.assertFalse(_should_generate_list_backdrop('https://poster.test/poster', None, [1]))
        self.assertFalse(_should_generate_list_backdrop(None, 'https://poster.test/backdrop', [1]))
        self.assertTrue(_should_generate_list_backdrop(None, None, [1]))

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

    def test_members_are_sorted_by_year_and_collection_sort_name_is_set(self):
        client = OrderedCollectionClient()

        self.assertEqual(
            _sync_collection(client, 'Christmas', [1], sort_name='0000000001 Christmas'),
            'collection-id',
        )

        self.assertEqual(client.updated, [('collection-id', ['new', 'old'])])
        self.assertEqual(client.sort_name, ('collection-id', '0000000001 Christmas', 'Christmas'))
        self.assertEqual(client.item_sort, 'collection-id')

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

    def test_collection_export_can_override_global_library_creation(self):
        for global_default, override, expected in (
            (True, False, False),
            (False, True, True),
            (False, None, False),
        ):
            with self.subTest(global_default=global_default, override=override):
                config = {
                    'library_exports': {
                        'media_root': '/media',
                        'output_root': '/exports',
                        'auto_create_emby_library': global_default,
                    },
                }
                with patch('src.app_logic.CollectionLibraryExporter') as exporter_class:
                    _sync_library_export(
                        EmptyCollectionClient(),
                        'collection-id',
                        'Horror',
                        config,
                        True,
                        auto_create_emby_library=override,
                    )

                exporter_class.return_value.export_collection.assert_called_once_with(
                    'collection-id',
                    'Horror',
                    create_emby_library=expected,
                )

    def test_expired_seasonal_collection_is_removed_and_managed_links_are_cleared(self):
        class SeasonalEmby:
            def get_collection_id(self, name):
                return 'box-id'

            def delete_collection(self, collection_id):
                self.deleted = collection_id
                return True

        emby = SeasonalEmby()
        with patch('src.app_logic._sync_library_export') as sync_export:
            self.assertTrue(_expire_seasonal_collection(emby, 'Halloween', {}))

        self.assertEqual(emby.deleted, 'box-id')
        sync_export.assert_called_once_with(emby, None, 'Halloween', {}, False)

    def test_deselected_managed_collection_removes_boxset_and_owned_export(self):
        class ManagedEmby:
            def get_collection_id(self, name):
                return 'box-id'

            def delete_collection(self, collection_id):
                self.deleted = collection_id
                return True

        emby = ManagedEmby()
        with patch('src.app_logic._sync_library_export', return_value=True) as sync_export:
            self.assertTrue(_remove_managed_collection(emby, 'Seasonal Picks', {}))

        self.assertEqual(emby.deleted, 'box-id')
        sync_export.assert_called_once_with(emby, None, 'Seasonal Picks', {}, False)

    def test_all_emby_collections_get_outer_and_inner_sort_preferences(self):
        class CollectionListEmby:
            def __init__(self):
                self.sort_names = []
                self.item_sorts = []

            def get_all_collections(self):
                return [
                    {'Id': 'regular-id', 'Name': 'A Regular Collection'},
                    {'Id': 'seasonal-id', 'Name': 'Halloween'},
                ]

            def update_collection_sort_name(self, collection_id, sort_name, name):
                self.sort_names.append((collection_id, sort_name, name))

            def update_collection_item_sort(self, collection_id):
                self.item_sorts.append(collection_id)

        emby = CollectionListEmby()
        _apply_collection_sorting(
            emby,
            {'Halloween': '0000000001 Halloween'},
        )

        self.assertEqual(emby.sort_names, [
            ('regular-id', 'A Regular Collection', 'A Regular Collection'),
            ('seasonal-id', '0000000001 Halloween', 'Halloween'),
        ])
        self.assertEqual(emby.item_sorts, ['regular-id', 'seasonal-id'])


if __name__ == '__main__':
    unittest.main()
