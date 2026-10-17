import unittest
from unittest.mock import Mock

from src.emby_client import EmbyClient


class EmbyLibraryApiTests(unittest.TestCase):
    def setUp(self):
        self.client = EmbyClient('http://emby.invalid', 'token', 'user')

    def test_collection_items_are_paginated_using_total_record_count(self):
        self.client._make_api_request = Mock(side_effect=[
            {'Items': [{'Id': 'one'}], 'TotalRecordCount': 2},
            {'Items': [{'Id': 'two'}], 'TotalRecordCount': 2},
        ])

        items = self.client.get_collection_items('collection')

        self.assertEqual([item['Id'] for item in items], ['one', 'two'])
        self.assertEqual(self.client._make_api_request.call_count, 2)

    def test_episode_members_resolve_to_the_series_folder(self):
        series_path = '/mnt/unionfs/television/00s/The Amazing Race (2001) {tvdb-77666}'
        self.client._make_api_request = Mock(side_effect=[
            {
                'Items': [{'Id': 'episode-id', 'Type': 'Episode', 'SeriesId': 'series-id'}],
                'TotalRecordCount': 1,
            },
            {'Id': 'series-id', 'Type': 'Series', 'Path': series_path},
        ])

        items = self.client.get_collection_items('collection-id')

        self.assertEqual(items, [{'Id': 'series-id', 'Type': 'Series', 'Path': series_path}])

    def test_series_episode_items_are_paginated(self):
        self.client._make_api_request = Mock(side_effect=[
            {'Items': [{'Id': 'episode-one'}], 'TotalRecordCount': 2},
            {'Items': [{'Id': 'episode-two'}], 'TotalRecordCount': 2},
        ])

        episodes = self.client.get_series_episodes('series-id')

        self.assertEqual([item['Id'] for item in episodes], ['episode-one', 'episode-two'])
        self.assertEqual(self.client._make_api_request.call_count, 2)

    def test_title_search_returns_matching_item_fields(self):
        expected = {'Id': 'movie-id', 'Name': 'The Killers', 'Path': '/mnt/media/movie.mkv'}
        self.client._make_api_request = Mock(return_value={
            'Items': [expected],
            'TotalRecordCount': 1,
        })

        items = self.client.search_items_by_name('The Killers', 'Movie')

        self.assertEqual(items, [expected])
        self.client._make_api_request.assert_called_once()
        self.assertEqual(
            self.client._make_api_request.call_args.kwargs['params']['IncludeItemTypes'],
            'Movie',
        )

    def test_items_by_ids_are_batched_through_emby_items_api(self):
        self.client._make_api_request = Mock(return_value={
            'Items': [{'Id': '2328102', 'Type': 'Movie', 'Path': '/mnt/media/movie.mkv'}],
        })

        items = self.client.get_items_by_ids(['2328102'])

        self.assertEqual(items[0]['Id'], '2328102')
        self.assertEqual(self.client._make_api_request.call_args.kwargs['params']['Ids'], '2328102')

    def test_item_ids_sort_by_release_year_newest_first(self):
        self.client._make_api_request = Mock(return_value={
            'Items': [
                {'Id': 'old', 'Name': 'Old', 'ProductionYear': 1990},
                {'Id': 'new', 'Name': 'New', 'ProductionYear': 2024},
                {'Id': 'unknown', 'Name': 'Unknown'},
            ],
        })

        self.assertEqual(
            self.client.sort_item_ids_by_year(['old', 'unknown', 'new']),
            ['new', 'old', 'unknown'],
        )

    def test_collection_sort_name_updates_only_the_sort_field(self):
        self.client.session.post = Mock(return_value=Mock(status_code=204))

        self.assertTrue(
            self.client.update_collection_sort_name(
                'box-id',
                '0000000001 Christmas',
                'Christmas',
            )
        )
        self.client.session.post.assert_called_once_with(
            'http://emby.invalid/Items/box-id',
            json={
                'Id': 'box-id',
                'Name': 'Christmas',
                'SortName': '0000000001 Christmas',
            },
            timeout=20,
        )

    def test_collection_item_sort_is_saved_per_user_client_and_preserves_custom_prefs(self):
        self.client.session.get = Mock(return_value=Mock(
            status_code=200,
            json=Mock(return_value={
                'Id': 'box-id',
                'Client': 'Emby Web',
                'SortBy': 'SortName',
                'SortOrder': 'Ascending',
                'CustomPrefs': {'view': 'poster'},
            }),
        ))
        self.client.session.post = Mock(return_value=Mock(status_code=204))

        self.assertTrue(self.client.update_collection_item_sort('box-id'))

        self.client.session.get.assert_called_once_with(
            'http://emby.invalid/DisplayPreferences/box-id',
            params={'UserId': 'user', 'Client': 'Emby Web'},
            timeout=15,
        )
        self.client.session.post.assert_called_once_with(
            'http://emby.invalid/DisplayPreferences/box-id',
            params={'UserId': 'user'},
            json={
                'Id': 'box-id',
                'Client': 'Emby Web',
                'SortBy': 'ProductionYear',
                'SortOrder': 'Descending',
                'CustomPrefs': {'view': 'poster'},
            },
            timeout=20,
        )

    def test_seasonal_delete_refuses_non_boxsets_and_path_backed_items(self):
        self.client.get_items_by_ids = Mock(return_value=[{
            'Id': 'movie-id',
            'Type': 'Movie',
            'Path': '/mnt/unionfs/movies/movie.mkv',
        }])
        self.client.session.delete = Mock()
        self.assertFalse(self.client.delete_collection('movie-id'))
        self.client.session.delete.assert_not_called()

        self.client.get_items_by_ids.return_value = [{
            'Id': 'box-id',
            'Type': 'BoxSet',
            'Path': None,
        }]
        self.client.session.delete.return_value = Mock(status_code=204)
        self.assertTrue(self.client.delete_collection('box-id'))
        self.client.session.delete.assert_called_once_with(
            'http://emby.invalid/Items/box-id',
            timeout=20,
        )

    def test_existing_library_at_the_same_path_is_reused(self):
        self.client._make_api_request = Mock(return_value=[{
            'Name': 'Horror-library',
            'ItemId': 'library-id',
            'CollectionType': 'movies',
            'Locations': ['/exports/Horror-library'],
        }])
        self.client.session.post = Mock()

        result = self.client.ensure_library('Horror-library', '/exports/Horror-library', 'movies')

        self.assertEqual(result, ('library-id', False))
        self.client.session.post.assert_not_called()

    def test_same_name_at_another_path_is_not_modified(self):
        self.client._make_api_request = Mock(return_value=[{
            'Name': 'Horror-library',
            'ItemId': 'library-id',
            'CollectionType': 'movies',
            'Locations': ['/exports/old'],
        }])
        self.client.session.post = Mock()

        result = self.client.ensure_library('Horror-library', '/exports/new', 'movies')

        self.assertIsNone(result)
        self.client.session.post.assert_not_called()

    def test_new_library_creation_is_verified_before_return(self):
        response = Mock(status_code=204)
        self.client.session.post = Mock(return_value=response)
        self.client._make_api_request = Mock(side_effect=[
            [],
            [{
                'Name': 'Horror-library',
                'ItemId': 'library-id',
                'CollectionType': 'movies',
                'Locations': ['/exports/Horror-library'],
            }],
        ])

        result = self.client.ensure_library('Horror-library', '/exports/Horror-library', 'movies')

        self.assertEqual(result, ('library-id', True))
        self.client.session.post.assert_called_once()
        self.assertEqual(
            self.client.session.post.call_args.kwargs['json'],
            {
                'Name': 'Horror-library',
                'CollectionType': 'movies',
                'Paths': ['/exports/Horror-library'],
                'RefreshLibrary': True,
            },
        )


if __name__ == '__main__':
    unittest.main()
