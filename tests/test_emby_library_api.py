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
