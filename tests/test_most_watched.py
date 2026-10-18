import importlib.util
import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.most_watched import (
    MostWatchedLibraryExporter,
    PlaybackReportingClient,
    _rank_shows_from_events,
    normalize_most_watched_settings,
    should_create_most_watched_libraries,
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
        self.library_items = []
        self.sort_name_updates = []

    def get_series_episodes(self, series_id):
        return self.episodes

    def get_libraries(self):
        return self.libraries

    def get_items_by_ids(self, item_ids):
        return [self.items[str(item_id)] for item_id in item_ids if str(item_id) in self.items]

    def get_items_in_library(self, library_id, include_item_types=None):
        return [
            item for item in self.library_items
            if include_item_types is None or item.get('Type') in include_item_types
        ]

    def update_item_sort_name(self, item_id, sort_name, expected_path):
        self.sort_name_updates.append((item_id, sort_name, expected_path))
        return True

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
                'movies': {
                    'enabled': False, 'days': 14, 'limit': 10, 'library_ids': None,
                    'target_library_id': None,
                    'output_mode': 'symlink', 'image_variants': ['poster', 'thumb'], 'badge_style': 'neutral',
                },
                'tv_shows': {
                    'enabled': False, 'days': 14, 'limit': 10, 'library_ids': None,
                    'target_library_id': None,
                    'output_mode': 'symlink', 'image_variants': ['poster', 'thumb'], 'badge_style': 'neutral',
                },
            },
        )
        settings = normalize_most_watched_settings({
            'movies': {'enabled': 'true', 'days': 5000, 'limit': 0},
            'tv_shows': {'days': 'invalid', 'limit': 200},
        })
        self.assertEqual(settings['movies'], {
            'enabled': True, 'days': 3650, 'limit': 1, 'library_ids': None,
            'target_library_id': None,
            'output_mode': 'symlink', 'image_variants': ['poster', 'thumb'], 'badge_style': 'neutral',
        })
        self.assertEqual(settings['tv_shows'], {
            'enabled': False, 'days': 14, 'limit': 100, 'library_ids': None,
            'target_library_id': None,
            'output_mode': 'symlink', 'image_variants': ['poster', 'thumb'], 'badge_style': 'neutral',
        })

    def test_output_settings_are_validated(self):
        settings = normalize_most_watched_settings({
            'movies': {
                'output_mode': 'STRM',
                'image_variants': ['thumb', 'poster', 'thumb', 'unknown'],
                'badge_style': 'ocean-blue',
            },
            'tv_shows': {
                'output_mode': 'invalid',
                'image_variants': [],
                'badge_style': 'invalid',
            },
        })
        self.assertEqual(settings['movies']['output_mode'], 'strm')
        self.assertEqual(settings['movies']['image_variants'], ['thumb', 'poster'])
        self.assertEqual(settings['movies']['badge_style'], 'ocean-blue')
        self.assertEqual(settings['tv_shows']['output_mode'], 'symlink')
        self.assertEqual(settings['tv_shows']['image_variants'], ['poster'])
        self.assertEqual(settings['tv_shows']['badge_style'], 'neutral')

    def test_most_watched_library_creation_can_inherit_or_override_global_setting(self):
        self.assertTrue(should_create_most_watched_libraries({
            'library_exports': {'auto_create_emby_library': True},
        }))
        self.assertFalse(should_create_most_watched_libraries({
            'library_exports': {'auto_create_emby_library': True},
            'most_watched': {'auto_create_emby_library': False},
        }))
        self.assertTrue(should_create_most_watched_libraries({
            'library_exports': {'auto_create_emby_library': False},
            'most_watched': {'auto_create_emby_library': True},
        }))


class PlaybackReportingClientTests(unittest.TestCase):
    def test_reads_raw_playback_events_with_configured_days_and_ignored_users(self):
        emby = FakeEmby()
        emby.session = FakeSession([
            FakeResponse(payload=[
                {'id': 'ignored', 'in_list': True},
                {'id': 'visible', 'in_list': False},
            ]),
            FakeResponse(payload=[
                {'item_id': 123, 'item_type': 'Movie', 'user_id': 'visible', 'duration': '5400'},
            ]),
        ])
        rows = PlaybackReportingClient(emby).get_playback_events(14)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['item_id'], 123)
        self.assertEqual(emby.session.calls[1][0], 'http://emby.invalid/user_usage_stats/UserPlaylist')
        self.assertEqual(emby.session.calls[1][1]['params'], {
            'user_id': 'visible', 'aggregate_data': 'false', 'days': 14,
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

            def sync(self, kind, rows, create_emby_library, **options):
                self.calls.append((kind, rows, create_emby_library, options))
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
            FakeResponse(payload=[
                {'id': 'u1', 'in_list': False},
                {'id': 'u2', 'in_list': False},
            ]),
            FakeResponse(payload=[
                event for event in playback_events if event['user_id'] == 'u1'
            ]),
            FakeResponse(payload=[
                event for event in playback_events if event['user_id'] == 'u2'
            ]),
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

        self.assertEqual(emby.session.calls[2][1]['params'], {
            'user_id': 'u1', 'aggregate_data': 'false', 'days': 21,
        })
        calls = {call[0]: call for call in exporter.calls}
        self.assertEqual(calls['movies'][1][0]['name'], 'The Killers')
        self.assertEqual(calls['movies'][1][0]['plays'], 8)
        self.assertFalse(calls['movies'][2])
        self.assertEqual(calls['tv_shows'][1], [])
        self.assertEqual(calls['movies'][3], {
            'output_mode': 'symlink',
            'image_variants': ['poster', 'thumb'],
            'badge_style': 'neutral',
            'target_library_id': None,
            'limit': 1,
        })
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
            media_mount_root=self.media_root,
            output_mount_root=self.output_root,
        )

    def test_selected_output_library_is_used_and_refreshed_by_id(self):
        emby = FakeEmby()
        target_dir = self.output_root / 'Top Played Movies'
        emby.libraries = [{
            'Id': 'top-movies-id',
            'Name': 'Top Played Movies',
            'CollectionType': 'movies',
            'Locations': [str(target_dir)],
        }]
        exporter = self.make_exporter(emby)
        movie_dir = self.media_root / 'movies' / 'Sorted Movie (2024)'
        movie_dir.mkdir(parents=True)
        movie_path = movie_dir / 'Sorted Movie.mkv'
        movie_path.write_bytes(b'movie')
        item = {
            'Id': 'movie-id',
            'Type': 'Movie',
            'Name': 'Sorted Movie',
            'ProductionYear': 2024,
            'Path': str(movie_path),
            'ProviderIds': {'Imdb': 'tt1234567'},
        }

        result = exporter.sync(
            'movies',
            [{'item': item, 'name': item['Name'], 'plays': 3, 'watch_seconds': 1000}],
            create_emby_library=True,
            target_library_id='top-movies-id',
            limit=5,
        )

        self.assertEqual(result['library_name'], 'Top Played Movies')
        self.assertEqual(result['library_path'], str(target_dir))
        self.assertEqual(result['library_id'], 'top-movies-id')
        self.assertEqual(emby.created_libraries, [])
        self.assertEqual(emby.refreshed, ['top-movies-id'])
        self.assertEqual(emby.refreshed_by_name, [])
        manifest = json.loads((target_dir / '.most-watched-manifest.json').read_text())
        self.assertEqual(manifest['kind'], 'movies')
        self.assertEqual(manifest['library_id'], 'top-movies-id')
        self.assertEqual(manifest['limit'], 5)
        self.assertEqual(result['limit'], 5)

    def test_selected_output_library_must_be_inside_writable_export_mount(self):
        emby = FakeEmby()
        emby.libraries = [{
            'Id': 'unsafe-id',
            'Name': 'Unsafe',
            'CollectionType': 'movies',
            'Locations': [str(self.base.parent / 'outside-output')],
        }]

        with self.assertRaisesRegex(ValueError, 'inside the writable export mount'):
            self.make_exporter(emby)._library_dir('movies', 'unsafe-id')

    def test_exporter_enforces_and_persists_configured_limit(self):
        emby = FakeEmby()
        exporter = self.make_exporter(emby)
        ranked = []
        for index in range(12):
            movie_dir = self.media_root / 'movies' / f'Movie {index:02d} (2024)'
            movie_dir.mkdir(parents=True)
            movie_path = movie_dir / f'Movie {index:02d}.mkv'
            movie_path.write_bytes(b'movie')
            item = {
                'Id': f'movie-{index}',
                'Type': 'Movie',
                'Name': f'Movie {index:02d}',
                'ProductionYear': 2024,
                'Path': str(movie_path),
                'ProviderIds': {'Imdb': f'tt{index:07d}'},
            }
            ranked.append({'item': item, 'name': item['Name'], 'plays': 12 - index, 'watch_seconds': 100})

        result = exporter.sync(
            'movies',
            ranked,
            create_emby_library=False,
            limit=10,
        )

        output = Path(result['library_path'])
        self.assertEqual(result['item_count'], 10)
        self.assertEqual(len(list(output.rglob('*.mkv'))), 10)
        manifest = json.loads((output / '.most-watched-manifest.json').read_text())
        self.assertEqual(manifest['limit'], 10)

    def test_selected_output_library_must_not_overlap_source_media_mount(self):
        emby = FakeEmby()
        emby.libraries = [{
            'Id': 'media-id',
            'Name': 'Movies',
            'CollectionType': 'movies',
            'Locations': [str(self.media_root / 'movies')],
        }]

        overlapping_mounts = MostWatchedLibraryExporter(
            emby,
            media_root=str(self.media_root),
            output_root=str(self.output_root),
            media_mount_root=self.base,
            output_mount_root=self.base,
        )
        with self.assertRaisesRegex(ValueError, 'must not overlap any source media mount'):
            overlapping_mounts._library_dir('movies', 'media-id')

    def test_selected_output_library_rejects_collection_export_directory(self):
        emby = FakeEmby()
        target_dir = self.output_root / 'Collection Export'
        target_dir.mkdir()
        (target_dir / '.embycollectionmanager.json').write_text('{}')
        emby.libraries = [{
            'Id': 'collection-export-id',
            'Name': 'Collection Export',
            'CollectionType': 'movies',
            'Locations': [str(target_dir)],
        }]

        with self.assertRaisesRegex(ValueError, 'managed by a collection export'):
            self.make_exporter(emby)._library_dir('movies', 'collection-export-id')

    def test_ranked_movie_symlink_has_badged_poster_and_never_copies_media(self):
        emby = FakeEmby()
        exporter = self.make_exporter(emby)
        movie_dir = self.media_root / 'movies' / 'The Killers (1946) {imdb-tt0038669}'
        movie_dir.mkdir(parents=True)
        movie_path = movie_dir / 'The Killers.mkv'
        movie_path.write_bytes(b'original-media')
        from PIL import Image
        Image.new('RGB', (80, 120), (10, 20, 220)).save(movie_dir / 'poster.jpg')
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
        ranked_poster = pointers[0].parent / 'poster.jpg'
        self.assertTrue(ranked_poster.is_file())
        with Image.open(ranked_poster) as poster:
            source_pixel = poster.getpixel((75, 115))
            self.assertTrue(all(abs(actual - expected) <= 3 for actual, expected in zip(source_pixel, (10, 20, 220))))
        self.assertEqual(list(output.rglob('*.strm')), [])
        self.assertTrue(movie_path.is_file())
        self.assertEqual(emby.created_libraries, [])

        exporter.sync('movies', [], create_emby_library=False)
        self.assertFalse(pointers[0].exists())
        self.assertTrue(movie_path.exists())
        self.assertTrue(emby.refreshed_by_name)

    def test_ranked_movie_symlink_preserves_mp4_extension(self):
        emby = FakeEmby()
        exporter = self.make_exporter(emby)
        movie_dir = self.media_root / 'movies' / 'Example Movie (2020)'
        movie_dir.mkdir(parents=True)
        movie_path = movie_dir / 'Example Movie (2020).mp4'
        movie_path.write_bytes(b'original-mp4')
        item = {
            'Id': 'mp4-id',
            'Type': 'Movie',
            'Name': 'Example Movie',
            'ProductionYear': 2020,
            'Path': str(movie_path),
            'ProviderIds': {'Tmdb': '12345'},
        }

        result = exporter.sync(
            'movies',
            [{'item': item, 'name': item['Name'], 'plays': 3, 'watch_seconds': 6000}],
            create_emby_library=False,
        )

        link = next(Path(result['library_path']).rglob('*.mp4'))
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(), movie_path)

    def test_folder_and_sort_titles_are_zero_padded_for_top_100(self):
        emby = FakeEmby()
        exporter = self.make_exporter(emby)
        movie_dir = self.media_root / 'movies' / 'Large Ranked Set'
        movie_dir.mkdir(parents=True)
        ranked = []
        for rank in range(1, 101):
            movie_path = movie_dir / f'Movie {rank:03d}.mp4'
            movie_path.write_bytes(b'movie')
            item = {
                'Id': f'movie-{rank}',
                'Type': 'Movie',
                'Name': f'Movie {rank}',
                'ProductionYear': 2020,
                'Path': str(movie_path),
            }
            ranked.append({'item': item, 'name': item['Name'], 'plays': 101 - rank, 'watch_seconds': 60})

        with patch.object(exporter, '_ranked_poster', return_value=b'poster'), patch.object(
            exporter, '_ranked_thumb', return_value=b'thumb',
        ):
            planned_files, planned_links = exporter._movie_files(ranked)

        self.assertIn('100 - Large Ranked Set/Movie 100.nfo', planned_files)
        self.assertIn('<sorttitle>100 - Movie 100</sorttitle>', planned_files[
            '100 - Large Ranked Set/Movie 100.nfo'
        ].decode('utf-8'))
        self.assertIn('100 - Large Ranked Set/Movie 100.mp4', planned_links)
        self.assertIn('001 - Large Ranked Set/Movie 001.mp4', planned_links)

    def test_ranked_movie_strm_points_to_original_and_writes_hsc_sidecars(self):
        emby = FakeEmby()
        exporter = self.make_exporter(emby)
        movie_dir = self.media_root / 'movies' / 'Example Movie (2020)'
        movie_dir.mkdir(parents=True)
        movie_path = movie_dir / 'Example Movie (2020).mp4'
        movie_path.write_bytes(b'original-mp4')
        from PIL import Image
        source_poster = movie_dir / 'poster.jpg'
        Image.new('RGB', (80, 120), (10, 20, 220)).save(source_poster)
        original_poster_bytes = source_poster.read_bytes()
        item = {
            'Id': 'strm-movie-id',
            'Type': 'Movie',
            'Name': 'Example Movie',
            'ProductionYear': 2020,
            'Path': str(movie_path),
            'ProviderIds': {'Tmdb': '12345'},
        }

        result = exporter.sync(
            'movies',
            [{'item': item, 'name': item['Name'], 'plays': 3, 'watch_seconds': 6000}],
            create_emby_library=False,
            output_mode='strm',
            image_variants=['poster', 'thumb'],
            badge_style='emby-green',
        )

        output = Path(result['library_path'])
        pointer = next(output.rglob('*.strm'))
        self.assertFalse(pointer.is_symlink())
        self.assertEqual(pointer.read_text(), str(movie_path))
        self.assertTrue((pointer.parent / f'{pointer.stem}.nfo').is_file())
        self.assertTrue((pointer.parent / f'{pointer.stem}.jpg').is_file())
        self.assertTrue((pointer.parent / f'{pointer.stem}-thumb.jpg').is_file())
        self.assertIn('<sorttitle>01 - Example Movie</sorttitle>', (pointer.parent / f'{pointer.stem}.nfo').read_text())
        for generated_file in (
            pointer,
            pointer.parent / f'{pointer.stem}.nfo',
            pointer.parent / f'{pointer.stem}.jpg',
            pointer.parent / f'{pointer.stem}-thumb.jpg',
        ):
            self.assertEqual(stat.S_IMODE(generated_file.stat().st_mode), 0o644)
        self.assertEqual(list(output.rglob('*.mp4')), [])
        self.assertEqual(source_poster.read_bytes(), original_poster_bytes)
        self.assertEqual(movie_path.read_bytes(), b'original-mp4')

        exporter.sync('movies', [], create_emby_library=False, output_mode='strm')
        self.assertFalse(pointer.exists())
        self.assertTrue(movie_path.exists())

    def test_strm_sync_sets_rank_sort_name_on_only_the_indexed_copy(self):
        emby = FakeEmby()
        exporter = self.make_exporter(emby)
        movie_dir = self.media_root / 'movies' / 'Sort Me Movie (2021)'
        movie_dir.mkdir(parents=True)
        movie_path = movie_dir / 'Sort Me Movie.mkv'
        movie_path.write_bytes(b'original')
        expected_strm = (
            self.output_root / 'Most Watched Movies-library'
            / '01 - Sort Me Movie (2021)' / 'Sort Me Movie.strm'
        )
        emby.library_items = [{
            'Id': 'strm-copy-id',
            'Type': 'Movie',
            'Path': str(expected_strm),
            'SortName': 'Sort Me Movie',
        }]
        item = {
            'Id': 'source-movie-id',
            'Type': 'Movie',
            'Name': 'Sort Me Movie',
            'ProductionYear': 2021,
            'Path': str(movie_path),
        }

        exporter.sync(
            'movies',
            [{'item': item, 'name': item['Name'], 'plays': 1, 'watch_seconds': 60}],
            create_emby_library=True,
            output_mode='strm',
        )

        self.assertEqual(
            emby.sort_name_updates,
            [('strm-copy-id', '01', str(expected_strm))],
        )
        self.assertTrue(movie_path.exists())

    def test_same_content_generated_file_permissions_are_repaired(self):
        emby = FakeEmby()
        exporter = self.make_exporter(emby)
        movie_dir = self.media_root / 'movies' / 'Permission Test (2022)'
        movie_dir.mkdir(parents=True)
        movie_path = movie_dir / 'Permission Test.mkv'
        movie_path.write_bytes(b'original-media')
        item = {
            'Id': 'permission-movie',
            'Type': 'Movie',
            'Name': 'Permission Test',
            'ProductionYear': 2022,
            'Path': str(movie_path),
        }
        ranked = [{'item': item, 'name': item['Name'], 'plays': 1, 'watch_seconds': 60}]

        with patch.object(exporter, '_ranked_poster', return_value=b'generated-poster'):
            first = exporter.sync(
                'movies', ranked, create_emby_library=False, output_mode='strm', image_variants=['poster'],
            )
            poster = next(Path(first['library_path']).rglob('*.jpg'))
            poster.chmod(0o600)
            self.assertEqual(stat.S_IMODE(poster.stat().st_mode), 0o600)

            exporter.sync(
                'movies', ranked, create_emby_library=False, output_mode='strm', image_variants=['poster'],
            )

        self.assertEqual(stat.S_IMODE(poster.stat().st_mode), 0o644)
        self.assertTrue(movie_path.exists())

    def test_switching_movie_output_modes_prunes_only_managed_pointer_or_link(self):
        emby = FakeEmby()
        exporter = self.make_exporter(emby)
        movie_dir = self.media_root / 'movies' / 'Mode Switch (2021)'
        movie_dir.mkdir(parents=True)
        movie_path = movie_dir / 'Mode Switch.mp4'
        movie_path.write_bytes(b'original')
        item = {
            'Id': 'mode-switch',
            'Type': 'Movie',
            'Name': 'Mode Switch',
            'ProductionYear': 2021,
            'Path': str(movie_path),
        }
        ranked = [{'item': item, 'name': item['Name'], 'plays': 1, 'watch_seconds': 60}]

        first = exporter.sync('movies', ranked, create_emby_library=False, output_mode='symlink')
        symlink = next(Path(first['library_path']).rglob('*.mp4'))
        self.assertTrue(symlink.is_symlink())

        second = exporter.sync('movies', ranked, create_emby_library=False, output_mode='strm')
        pointer = next(Path(second['library_path']).rglob('*.strm'))
        self.assertTrue(pointer.is_file())
        self.assertFalse(symlink.exists())
        self.assertTrue(movie_path.exists())

        third = exporter.sync('movies', ranked, create_emby_library=False, output_mode='symlink')
        replacement_link = next(Path(third['library_path']).rglob('*.mp4'))
        self.assertTrue(replacement_link.is_symlink())
        self.assertFalse(pointer.exists())
        self.assertTrue(movie_path.exists())

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

    def test_top_ten_series_change_prunes_stale_owned_directories_and_refreshes_library(self):
        emby = FakeEmby()
        exporter = self.make_exporter(emby)
        target_dir = self.output_root / 'Top TV'
        emby.libraries = [{
            'Id': 'tv-view-id',
            'Name': 'Top TV',
            'CollectionType': 'tvshows',
            'Locations': [str(target_dir)],
        }]
        episode_path = self.media_root / 'television' / 'Source Show' / 'Season 01' / 'Episode.mkv'
        episode_path.parent.mkdir(parents=True)
        episode_path.write_bytes(b'episode')
        emby.episodes = [{
            'Id': 'episode-id',
            'Type': 'Episode',
            'Name': 'Episode',
            'Path': str(episode_path),
            'ParentIndexNumber': 1,
            'IndexNumber': 1,
        }]

        def series_entry(index):
            series = {
                'Id': f'series-{index}',
                'Type': 'Series',
                'Name': f'Show {index:02d}',
                'ProductionYear': 2020,
                'Path': f'/source/Show {index:02d}',
                'ProviderIds': {'Tvdb': str(10000 + index)},
            }
            return {'item': series, 'name': series['Name'], 'plays': 20 - index, 'watch_seconds': 100}

        exporter.sync(
            'tv_shows',
            [series_entry(index) for index in range(12)],
            create_emby_library=False,
            target_library_id='tv-view-id',
            output_mode='symlink',
            image_variants=['poster'],
            limit=10,
        )
        manifest_path = target_dir / '.most-watched-manifest.json'
        old_manifest = json.loads(manifest_path.read_text())
        old_manifest.pop('directories', None)
        manifest_path.write_text(json.dumps(old_manifest), encoding='utf-8')
        result = exporter.sync(
            'tv_shows',
            [series_entry(index) for index in range(2, 12)],
            create_emby_library=False,
            target_library_id='tv-view-id',
            output_mode='symlink',
            image_variants=['poster'],
            limit=10,
        )

        output = Path(result['library_path'])
        series_folders = [path for path in output.iterdir() if path.is_dir()]
        self.assertEqual(len(series_folders), 10)
        self.assertEqual(
            {path.name for path in series_folders},
            {f'{index:02d} - Show {index + 1:02d}' for index in range(1, 11)},
        )
        self.assertEqual(len(list(output.rglob('*.mkv'))), 10)
        self.assertEqual(emby.refreshed, ['tv-view-id', 'tv-view-id'])
        manifest = json.loads((output / '.most-watched-manifest.json').read_text())
        self.assertEqual(manifest['limit'], 10)
        self.assertEqual(len(manifest['links']), 10)
        self.assertTrue(all(
            (output / relative).is_dir()
            for relative in manifest['directories']
        ))

    def test_pruning_preserves_unmanaged_file_in_obsolete_series_directory(self):
        emby = FakeEmby()
        exporter = self.make_exporter(emby)
        episode_path = self.media_root / 'television' / 'Source Show' / 'Season 01' / 'Episode.mkv'
        episode_path.parent.mkdir(parents=True)
        episode_path.write_bytes(b'episode')
        emby.episodes = [{
            'Id': 'episode-id',
            'Type': 'Episode',
            'Name': 'Episode',
            'Path': str(episode_path),
            'ParentIndexNumber': 1,
            'IndexNumber': 1,
        }]
        alpha = {'item': {'Id': 'alpha', 'Type': 'Series', 'Name': 'Alpha', 'Path': '/source/Alpha'}}
        beta = {'item': {'Id': 'beta', 'Type': 'Series', 'Name': 'Beta', 'Path': '/source/Beta'}}
        first = exporter.sync('tv_shows', [alpha, beta], create_emby_library=False, limit=10)
        output = Path(first['library_path'])
        stale_folder = next(path for path in output.iterdir() if path.is_dir() and 'Beta' in path.name)
        unmanaged_file = stale_folder / 'user-note.txt'
        unmanaged_file.write_text('keep', encoding='utf-8')

        exporter.sync('tv_shows', [alpha], create_emby_library=False, limit=10)

        self.assertTrue(stale_folder.is_dir())
        self.assertEqual(unmanaged_file.read_text(encoding='utf-8'), 'keep')

    def test_ranked_show_strm_mode_keeps_a_series_with_seasons_and_ranked_art(self):
        emby = FakeEmby()
        exporter = self.make_exporter(emby)
        series_dir = self.media_root / 'television' / '20s' / 'Chief of War (2025)'
        episode_path = series_dir / 'Season 01' / 'Chief of War S01E07.mkv'
        episode_path.parent.mkdir(parents=True)
        episode_path.write_bytes(b'episode')
        emby.episodes = [{
            'Id': 'chief-episode',
            'Type': 'Episode',
            'Name': 'Episode 7',
            'Path': str(episode_path),
            'ParentIndexNumber': 1,
            'IndexNumber': 7,
            'Overview': 'Episode synopsis',
        }]
        series = {
            'Id': 'chief-series',
            'Type': 'Series',
            'Name': 'Chief of War',
            'ProductionYear': 2025,
            'Path': str(series_dir),
            'ProviderIds': {'Tvdb': '418633'},
        }

        result = exporter.sync(
            'tv_shows',
            [{'item': series, 'name': series['Name'], 'plays': 5, 'watch_seconds': 18000}],
            create_emby_library=False,
            output_mode='strm',
        )

        output = Path(result['library_path'])
        show_folders = [path for path in output.iterdir() if path.is_dir()]
        self.assertEqual(len(show_folders), 1)
        show_folder = show_folders[0]
        self.assertTrue((show_folder / 'tvshow.nfo').is_file())
        self.assertTrue((show_folder / 'poster.jpg').is_file())
        self.assertTrue((show_folder / 'landscape.jpg').is_file())
        self.assertIn('<sorttitle>01 - Chief of War</sorttitle>', (show_folder / 'tvshow.nfo').read_text())
        pointer = next(show_folder.rglob('*.strm'))
        self.assertEqual(pointer.read_text(), str(episode_path))
        self.assertFalse(pointer.is_symlink())
        self.assertIn('Season 01', str(pointer))
        self.assertTrue(pointer.with_suffix('.nfo').is_file())
        self.assertEqual(list(output.rglob('*.mkv')), [])
        self.assertTrue(episode_path.exists())


if __name__ == '__main__':
    unittest.main()
