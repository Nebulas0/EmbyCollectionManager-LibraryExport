import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.most_watched import (
    MostWatchedLibraryExporter,
    PlaybackReportingClient,
    _rank_shows_from_events,
    normalize_most_watched_settings,
    sync_most_watched,
)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, content=b''):
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self._payload = payload
        self.content = content

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


class FakeEmby:
    server_url = 'http://emby.invalid'

    def __init__(self, episodes=None):
        self.session = FakeSession([])
        self.episodes = episodes or []
        self.libraries = []
        self.items = {}
        self.created_libraries = []
        self.refreshed = []
        self.refreshed_by_name = []

    def get_series_episodes(self, series_id):
        return self.episodes

    def get_libraries(self):
        return self.libraries

    def get_items_by_ids(self, item_ids):
        return [self.items[str(item_id)] for item_id in item_ids if str(item_id) in self.items]

    def get_item_image_bytes(self, item_id, image_type='Primary'):
        return None

    def ensure_library(self, name, path, collection_type):
        self.created_libraries.append((name, path, collection_type))
        return ('library-id', False)

    def refresh_library(self, library_id):
        self.refreshed.append(library_id)
        return True

    def refresh_library_by_name(self, name, path):
        self.refreshed_by_name.append((name, path))
        return True


class MostWatchedSettingsTests(unittest.TestCase):
    def test_defaults_and_bounds_are_normalized(self):
        self.assertEqual(
            normalize_most_watched_settings({}),
            {
                'movies': {'enabled': False, 'days': 14, 'limit': 10, 'library_ids': None},
                'tv_shows': {'enabled': False, 'days': 14, 'limit': 10, 'library_ids': None},
            },
        )
        settings = normalize_most_watched_settings({
            'movies': {'enabled': 'true', 'days': 5000, 'limit': 0},
            'tv_shows': {'days': 'invalid', 'limit': 200},
        })
        self.assertEqual(settings['movies'], {
            'enabled': True, 'days': 3650, 'limit': 1, 'library_ids': None,
        })
        self.assertEqual(settings['tv_shows'], {
            'enabled': False, 'days': 14, 'limit': 100, 'library_ids': None,
        })


class PlaybackReportingClientTests(unittest.TestCase):
    def test_reads_raw_playback_events_with_configured_days_and_ignored_users(self):
        emby = FakeEmby()
        emby.session = FakeSession([
            FakeResponse(payload=[
                {'item_id': 123, 'item_type': 'Movie', 'user_id': 'visible', 'duration': '5400'},
                {'item_id': 124, 'item_type': 'Movie', 'user_id': 'ignored', 'duration': '3600'},
            ]),
            FakeResponse(payload=[
                {'id': 'ignored', 'in_list': True},
                {'id': 'visible', 'in_list': False},
            ]),
        ])
        rows = PlaybackReportingClient(emby).get_playback_events(14)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['item_id'], 123)
        self.assertEqual(emby.session.calls[0][0], 'http://emby.invalid/user_usage_stats/UserPlaylist')
        self.assertEqual(emby.session.calls[0][1]['params'], {
            'user_id': '', 'aggregate_data': 'false', 'days': 14,
        })

    def test_missing_plugin_is_reported_without_failing_the_page(self):
        emby = FakeEmby()
        emby.session = FakeSession([FakeResponse(404), FakeResponse(404)])
        available, message = PlaybackReportingClient(emby).is_available()
        self.assertFalse(available)
        self.assertIn('not found', message)

    def test_sync_uses_configured_days_and_limit_and_records_ranked_rows(self):
        class RecordingExporter:
            def __init__(self):
                self.calls = []

            def sync(self, kind, rows, create_emby_library):
                self.calls.append((kind, rows, create_emby_library))
                return {'library_name': 'Most Watched Movies-library', 'item_count': len(rows)}

        emby = FakeEmby()
        movie = {
            'Id': 'movie-id',
            'Type': 'Movie',
            'Name': 'The Killers',
            'Path': '/mnt/media/movies/the-killers.mkv',
            'ProviderIds': {'Imdb': 'tt0038669'},
        }
        other_movie = {
            'Id': 'other-movie-id',
            'Type': 'Movie',
            'Name': 'Other Library Hit',
            'Path': '/mnt/media/other/other-hit.mkv',
        }
        emby.items = {'movie-id': movie, 'other-movie-id': other_movie}
        emby.libraries = [{
            'Id': 'movie-lib',
            'Name': 'Movies',
            'CollectionType': 'movies',
            'Locations': ['/mnt/media/movies'],
        }, {
            'Id': 'other-lib',
            'Name': 'Other Movies',
            'CollectionType': 'movies',
            'Locations': ['/mnt/media/other'],
        }]
        playback_events = [
            {'item_id': 'movie-id', 'item_type': 'Movie', 'user_id': 'u1', 'duration': '900'}
            for _ in range(8)
        ] + [
            {'item_id': 'other-movie-id', 'item_type': 'Movie', 'user_id': 'u2', 'duration': '1800'}
            for _ in range(20)
        ]
        emby.session = FakeSession([
            FakeResponse(payload=['Movie', 'Episode']),
            FakeResponse(payload=playback_events),
            FakeResponse(payload=[]),
        ])
        exporter = RecordingExporter()
        config = {
            'most_watched': {
                'movies': {'enabled': True, 'days': 21, 'limit': 1, 'library_ids': ['movie-lib']},
                'tv_shows': {'enabled': False, 'days': 14, 'limit': 10, 'library_ids': []},
            },
            'library_exports': {
                'media_root': '/mnt/media',
                'output_root': '/mnt/exports',
                'auto_create_emby_library': False,
            },
        }
        with tempfile.TemporaryDirectory() as state_dir, patch(
            'src.most_watched.MostWatchedLibraryExporter',
            return_value=exporter,
        ):
            state = sync_most_watched(emby, config, state_dir)
            saved = json.loads((Path(state_dir) / 'most_watched_results.json').read_text())

        self.assertEqual(emby.session.calls[1][1]['params'], {
            'user_id': '', 'aggregate_data': 'false', 'days': 21,
        })
        calls = {call[0]: call for call in exporter.calls}
        self.assertEqual(calls['movies'][1][0]['name'], 'The Killers')
        self.assertEqual(calls['movies'][1][0]['plays'], 8)
        self.assertFalse(calls['movies'][2])
        self.assertEqual(calls['tv_shows'][1], [])
        self.assertEqual(state['movies'][0]['plays'], 8)
        self.assertEqual(saved['movies'][0]['rank'], 1)

    def test_episode_playbacks_aggregate_to_series_and_honor_library_roots(self):
        emby = FakeEmby()
        emby.items = {
            'episode-one': {
                'Id': 'episode-one', 'Type': 'Episode', 'SeriesId': 'series-one',
                'Path': '/media/tv/Chief of War/Season 01/episode-1.mkv',
            },
            'episode-two': {
                'Id': 'episode-two', 'Type': 'Episode', 'SeriesId': 'series-one',
                'Path': '/media/tv/Chief of War/Season 01/episode-2.mkv',
            },
            'other-episode': {
                'Id': 'other-episode', 'Type': 'Episode', 'SeriesId': 'other-series',
                'Path': '/media/other/Show/Season 01/episode.mkv',
            },
            'series-one': {
                'Id': 'series-one', 'Type': 'Series', 'Name': 'Chief of War',
                'Path': '/media/tv/Chief of War', 'ProviderIds': {'Tvdb': '418633'},
            },
            'other-series': {
                'Id': 'other-series', 'Type': 'Series', 'Name': 'Other Show',
                'Path': '/media/other/Show',
            },
        }
        events = [
            {'item_id': 'episode-one', 'item_type': 'Episode', 'duration': 1200},
            {'item_id': 'episode-one', 'item_type': 'Episode', 'duration': 900},
            {'item_id': 'episode-two', 'item_type': 'Episode', 'duration': 1500},
            {'item_id': 'other-episode', 'item_type': 'Episode', 'duration': 6000},
        ]

        shows = _rank_shows_from_events(events, emby, [Path('/media/tv')], 10)

        self.assertEqual(len(shows), 1)
        self.assertEqual(shows[0]['name'], 'Chief of War')
        self.assertEqual(shows[0]['plays'], 3)
        self.assertEqual(shows[0]['watch_seconds'], 3600)


@unittest.skipUnless(importlib.util.find_spec('PIL'), 'Pillow is needed to render ranked posters')
class MostWatchedLibraryExporterTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)
        self.media_root = self.base / 'media'
        self.output_root = self.base / 'exports'
        self.media_root.mkdir()
        self.output_root.mkdir()

    def tearDown(self):
        self.temp_dir.cleanup()

    def make_exporter(self, emby):
        return MostWatchedLibraryExporter(
            emby,
            media_root=str(self.media_root),
            output_root=str(self.output_root),
            media_mount_root=self.base,
            output_mount_root=self.base,
        )

    def test_ranked_movie_symlink_has_badged_poster_and_never_copies_media(self):
        emby = FakeEmby()
        exporter = self.make_exporter(emby)
        movie_dir = self.media_root / 'movies' / 'The Killers (1946) {imdb-tt0038669}'
        movie_dir.mkdir(parents=True)
        movie_path = movie_dir / 'The Killers.mkv'
        movie_path.write_bytes(b'original-media')
        item = {
            'Id': 'movie-id',
            'Type': 'Movie',
            'Name': 'The Killers',
            'ProductionYear': 1946,
            'Path': str(movie_path),
            'ProviderIds': {'Imdb': 'tt0038669', 'Tmdb': '14638'},
        }

        result = exporter.sync(
            'movies',
            [{'item': item, 'name': item['Name'], 'plays': 7, 'watch_seconds': 9000}],
            create_emby_library=False,
        )

        output = Path(result['library_path'])
        pointers = list(output.rglob('*.mkv'))
        self.assertEqual(len(pointers), 1)
        self.assertTrue(pointers[0].is_symlink())
        self.assertEqual(pointers[0].resolve(), movie_path)
        nfo = pointers[0].with_suffix('.nfo').read_text()
        self.assertIn('<sorttitle>01 - The Killers</sorttitle>', nfo)
        self.assertTrue((pointers[0].parent / 'poster.jpg').is_file())
        self.assertEqual(list(output.rglob('*.strm')), [])
        self.assertTrue(movie_path.is_file())
        self.assertEqual(emby.created_libraries, [])

        exporter.sync('movies', [], create_emby_library=False)
        self.assertFalse(pointers[0].exists())
        self.assertTrue(movie_path.exists())
        self.assertTrue(emby.refreshed_by_name)

    def test_ranked_show_contains_one_series_with_season_episode_symlinks(self):
        emby = FakeEmby()
        exporter = self.make_exporter(emby)
        series_dir = self.media_root / 'television' / '00s' / 'The Amazing Race (2001) {tvdb-77666}'
        episode_path = series_dir / 'Season 39' / 'The Amazing Race S39E04.mkv'
        episode_path.parent.mkdir(parents=True)
        episode_path.write_bytes(b'episode')
        emby.episodes = [{
            'Id': 'episode-id',
            'Type': 'Episode',
            'Name': 'Episode 4',
            'Path': str(episode_path),
            'ParentIndexNumber': 39,
            'IndexNumber': 4,
            'Overview': 'Race episode',
        }]
        series = {
            'Id': 'series-id',
            'Type': 'Series',
            'Name': 'The Amazing Race',
            'ProductionYear': 2001,
            'Path': str(series_dir),
            'ProviderIds': {'Tvdb': '77666'},
        }

        result = exporter.sync(
            'tv_shows',
            [{'item': series, 'name': series['Name'], 'plays': 5, 'watch_seconds': 18000}],
            create_emby_library=False,
        )

        output = Path(result['library_path'])
        show_folders = [path for path in output.iterdir() if path.is_dir()]
        self.assertEqual(len(show_folders), 1)
        self.assertTrue(show_folders[0].name.startswith('01 - The Amazing Race'))
        self.assertIn('<sorttitle>01 - The Amazing Race</sorttitle>', (show_folders[0] / 'tvshow.nfo').read_text())
        episode_pointer = next(show_folders[0].rglob('*.mkv'))
        self.assertTrue(episode_pointer.is_symlink())
        self.assertEqual(episode_pointer.resolve(), episode_path)
        self.assertIn('Season 39', str(episode_pointer))
        self.assertTrue((show_folders[0] / 'poster.jpg').is_file())
        self.assertEqual(list(output.rglob('*.strm')), [])


if __name__ == '__main__':
    unittest.main()
