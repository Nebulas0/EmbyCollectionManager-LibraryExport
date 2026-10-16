import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.most_watched import (
    MostWatchedLibraryExporter,
    PlaybackReportingClient,
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
        self.created_libraries = []
        self.refreshed = []
        self.refreshed_by_name = []

    def get_series_episodes(self, series_id):
        return self.episodes

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
                'movies': {'enabled': False, 'days': 14, 'limit': 10},
                'tv_shows': {'enabled': False, 'days': 14, 'limit': 10},
            },
        )
        settings = normalize_most_watched_settings({
            'movies': {'enabled': 'true', 'days': 5000, 'limit': 0},
            'tv_shows': {'days': 'invalid', 'limit': 200},
        })
        self.assertEqual(settings['movies'], {'enabled': True, 'days': 3650, 'limit': 1})
        self.assertEqual(settings['tv_shows'], {'enabled': False, 'days': 14, 'limit': 100})


class PlaybackReportingClientTests(unittest.TestCase):
    def test_reads_builtin_movie_report_with_configured_days(self):
        emby = FakeEmby()
        emby.session = FakeSession([
            FakeResponse(payload=[{'label': 'The Killers', 'count': 4, 'time': 5400}]),
        ])
        rows = PlaybackReportingClient(emby).get_report('movies', 14)
        self.assertEqual(rows[0]['label'], 'The Killers')
        self.assertEqual(emby.session.calls[0][0], 'http://emby.invalid/user_usage_stats/MoviesReport')
        self.assertEqual(emby.session.calls[0][1]['params'], {'days': 14})

    def test_missing_plugin_is_reported_without_failing_the_page(self):
        emby = FakeEmby()
        emby.session = FakeSession([FakeResponse(404), FakeResponse(404)])
        available, message = PlaybackReportingClient(emby).is_available()
        self.assertFalse(available)
        self.assertIn('not found', message)

    def test_sync_uses_configured_days_and_limit_and_records_ranked_rows(self):
        class SearchableEmby(FakeEmby):
            def search_items_by_name(self, name, item_type):
                return [{
                    'Id': 'movie-id',
                    'Type': item_type,
                    'Name': name,
                    'Path': '/mnt/media/movie.mkv',
                }]

        class RecordingExporter:
            def __init__(self):
                self.calls = []

            def sync(self, kind, rows, create_emby_library):
                self.calls.append((kind, rows, create_emby_library))
                return {'library_name': 'Most Watched Movies-library', 'item_count': len(rows)}

        emby = SearchableEmby()
        emby.session = FakeSession([
            FakeResponse(payload=['Movie', 'Episode']),
            FakeResponse(payload=[
                {'label': 'The Killers', 'count': 8, 'time': 7200},
                {'label': 'Another Movie', 'count': 5, 'time': 5000},
            ]),
        ])
        exporter = RecordingExporter()
        config = {
            'most_watched': {
                'movies': {'enabled': True, 'days': 21, 'limit': 1},
                'tv_shows': {'enabled': False, 'days': 14, 'limit': 10},
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

        self.assertEqual(emby.session.calls[1][1]['params'], {'days': 21})
        calls = {call[0]: call for call in exporter.calls}
        self.assertEqual(calls['movies'][1][0]['name'], 'The Killers')
        self.assertFalse(calls['movies'][2])
        self.assertEqual(calls['tv_shows'][1], [])
        self.assertEqual(state['movies'][0]['plays'], 8)
        self.assertEqual(saved['movies'][0]['rank'], 1)


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

    def test_ranked_movie_pointer_has_badged_poster_and_never_copies_media(self):
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
        pointers = list(output.rglob('*.strm'))
        self.assertEqual(len(pointers), 1)
        self.assertEqual(pointers[0].read_text().strip(), str(movie_path))
        nfo = pointers[0].with_suffix('.nfo').read_text()
        self.assertIn('<sorttitle>01 - The Killers</sorttitle>', nfo)
        self.assertTrue((pointers[0].parent / 'poster.jpg').is_file())
        self.assertEqual(list(output.rglob('*.mkv')), [])
        self.assertTrue(movie_path.is_file())
        self.assertEqual(emby.created_libraries, [])

        exporter.sync('movies', [], create_emby_library=False)
        self.assertFalse(pointers[0].exists())
        self.assertTrue(movie_path.exists())
        self.assertTrue(emby.refreshed_by_name)

    def test_ranked_show_contains_one_series_with_season_episode_pointers(self):
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
        episode_pointer = next(show_folders[0].rglob('*.strm'))
        self.assertEqual(episode_pointer.read_text().strip(), str(episode_path))
        self.assertIn('Season 39', str(episode_pointer))
        self.assertTrue((show_folders[0] / 'poster.jpg').is_file())
        self.assertEqual(list(output.rglob('*.mkv')), [])


if __name__ == '__main__':
    unittest.main()
