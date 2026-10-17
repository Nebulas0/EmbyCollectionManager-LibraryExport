import json
import tempfile
import unittest
from pathlib import Path

from src.library_export import CollectionLibraryExporter, validate_library_export_paths


class FakeEmbyClient:
    def __init__(self, items):
        self.items = items
        self.libraries = []
        self.refreshed = []
        self.refreshed_by_name = []

    def get_collection_items(self, collection_id):
        return self.items

    def ensure_library(self, name, path, collection_type):
        self.libraries.append((name, path, collection_type))
        return ('library-id', False)

    def refresh_library(self, library_id):
        self.refreshed.append(library_id)
        return True

    def refresh_library_by_name(self, name, path):
        self.refreshed_by_name.append((name, path))
        return True


class CollectionLibraryExporterTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)
        self.media_root = self.base / 'media'
        self.output_mount = self.base / 'exports'
        self.media_root.mkdir()
        self.output_mount.mkdir()

    def tearDown(self):
        self.temp_dir.cleanup()

    def create_exporter(self, emby, output_root=None):
        return CollectionLibraryExporter(
            emby,
            media_root=str(self.media_root),
            output_root=str(output_root or self.output_mount),
            media_mount_root=self.media_root,
            output_mount_root=self.output_mount,
        )

    def test_movie_file_links_its_movie_directory_and_creates_library(self):
        movie_dir = self.media_root / 'movies' / '70s' / 'The Killers (1946) {imdb-tt0038669} {tmdb-14638}'
        movie_dir.mkdir(parents=True)
        media_file = movie_dir / 'The Killers (1946) [Remux-1080p 8-bit AVC DTS-HD MA 2.0]-FraMeSToR.mkv'
        media_file.write_text('media', encoding='utf-8')
        emby = FakeEmbyClient([{
            'Id': 'movie-id',
            'Type': 'Movie',
            'Path': str(media_file),
        }])

        self.assertTrue(self.create_exporter(emby).export_collection('collection-id', 'Horror'))

        output = self.output_mount / 'Horror-library'
        link = output / movie_dir.name
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(), movie_dir)
        self.assertEqual(emby.libraries, [('Horror-library', str(output), 'movies')])
        self.assertEqual(emby.refreshed, ['library-id'])
        self.assertEqual(media_file.read_text(encoding='utf-8'), 'media')

    def test_mp4_item_uses_same_directory_symlink_logic(self):
        movie_dir = self.media_root / 'movies' / 'Example Movie (2020)'
        movie_dir.mkdir(parents=True)
        media_file = movie_dir / 'Example Movie (2020).mp4'
        media_file.write_text('media', encoding='utf-8')
        emby = FakeEmbyClient([{
            'Id': 'movie-id',
            'Type': 'Movie',
            'Path': str(media_file),
        }])

        self.assertTrue(self.create_exporter(emby).export_collection('collection-id', 'MP4 Picks'))

        link = self.output_mount / 'MP4 Picks-library' / movie_dir.name
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(), movie_dir)
        self.assertEqual(media_file.read_text(encoding='utf-8'), 'media')

    def test_series_folder_is_linked_without_episode_path_guessing(self):
        series_dir = self.media_root / 'television' / '00s' / 'The Amazing Race (2001) {tvdb-77666}'
        season_dir = series_dir / 'Season 39'
        season_dir.mkdir(parents=True)
        episode = season_dir / 'The Amazing Race (2001) - S39E04 [AMZN WEBDL-1080p 8-bit h264 EAC3 2.0]-RAWR.mkv'
        episode.write_text('media', encoding='utf-8')
        emby = FakeEmbyClient([{
            'Id': 'series-id',
            'Type': 'Series',
            'Path': str(series_dir),
        }])

        self.assertTrue(self.create_exporter(emby).export_collection('collection-id', 'TV Picks'))

        link = self.output_mount / 'TV Picks-library' / series_dir.name
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.name, 'The Amazing Race (2001) {tvdb-77666}')
        self.assertEqual(link.resolve(), series_dir)
        self.assertTrue((link / 'Season 39' / episode.name).is_file())
        self.assertEqual(emby.libraries[0][2], 'tvshows')

    def test_auto_library_creation_can_be_disabled_without_disabling_export(self):
        movie_dir = self.media_root / 'Horror Title'
        movie_dir.mkdir()
        media_file = movie_dir / 'Horror.mkv'
        media_file.write_text('media', encoding='utf-8')
        emby = FakeEmbyClient([{
            'Id': 'movie-id',
            'Type': 'Movie',
            'Path': str(media_file),
        }])
        output = self.output_mount / 'Horror-library'

        self.assertTrue(
            self.create_exporter(emby).export_collection(
                'collection-id',
                'Horror',
                create_emby_library=False,
            )
        )

        self.assertTrue((output / movie_dir.name).is_symlink())
        self.assertEqual(emby.libraries, [])
        self.assertEqual(emby.refreshed_by_name, [('Horror-library', str(output))])

    def test_removed_collection_items_prune_only_managed_links(self):
        movie_dir = self.media_root / 'Horror Title'
        movie_dir.mkdir()
        media_file = movie_dir / 'Horror.strm'
        media_file.write_text('stream', encoding='utf-8')
        emby = FakeEmbyClient([{
            'Id': 'movie-id',
            'Type': 'Movie',
            'Path': str(media_file),
        }])
        exporter = self.create_exporter(emby)
        output = self.output_mount / 'Horror-library'
        link = output / movie_dir.name

        self.assertTrue(exporter.export_collection('collection-id', 'Horror'))
        emby.items = []
        self.assertTrue(exporter.export_collection('collection-id', 'Horror'))

        self.assertFalse(link.is_symlink())
        self.assertTrue(movie_dir.is_dir())
        manifest = json.loads((output / '.embycollectionmanager.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['items'], {})
        self.assertEqual(emby.refreshed_by_name, [('Horror-library', str(output))])

    def test_changed_source_replaces_only_the_owned_symlink(self):
        first_dir = self.media_root / 'library-a' / 'Same Title'
        second_dir = self.media_root / 'library-b' / 'Same Title'
        first_dir.mkdir(parents=True)
        second_dir.mkdir(parents=True)
        first_file = first_dir / 'movie.strm'
        second_file = second_dir / 'movie.strm'
        first_file.write_text('first', encoding='utf-8')
        second_file.write_text('second', encoding='utf-8')
        emby = FakeEmbyClient([{'Id': 'first', 'Type': 'Movie', 'Path': str(first_file)}])
        exporter = self.create_exporter(emby)
        link = self.output_mount / 'Horror-library' / 'Same Title'

        self.assertTrue(exporter.export_collection('collection-id', 'Horror'))
        emby.items = [{'Id': 'second', 'Type': 'Movie', 'Path': str(second_file)}]
        self.assertTrue(exporter.export_collection('collection-id', 'Horror'))

        self.assertEqual(link.resolve(), second_dir)
        self.assertEqual(first_file.read_text(encoding='utf-8'), 'first')

    def test_unmanaged_files_are_never_removed(self):
        movie_dir = self.media_root / 'Horror Title'
        movie_dir.mkdir()
        media_file = movie_dir / 'Horror.strm'
        media_file.write_text('stream', encoding='utf-8')
        output = self.output_mount / 'Horror-library'
        output.mkdir()
        user_file = output / 'notes.txt'
        user_file.write_text('keep', encoding='utf-8')
        emby = FakeEmbyClient([{
            'Id': 'movie-id',
            'Type': 'Movie',
            'Path': str(media_file),
        }])
        exporter = self.create_exporter(emby)

        self.assertTrue(exporter.export_collection('collection-id', 'Horror'))
        emby.items = []
        self.assertTrue(exporter.export_collection('collection-id', 'Horror'))

        self.assertEqual(user_file.read_text(encoding='utf-8'), 'keep')
        self.assertTrue(user_file.exists())

    def test_failed_collection_read_does_not_prune_existing_links(self):
        movie_dir = self.media_root / 'Horror Title'
        movie_dir.mkdir()
        media_file = movie_dir / 'Horror.strm'
        media_file.write_text('stream', encoding='utf-8')
        emby = FakeEmbyClient([{
            'Id': 'movie-id',
            'Type': 'Movie',
            'Path': str(media_file),
        }])
        exporter = self.create_exporter(emby)
        link = self.output_mount / 'Horror-library' / movie_dir.name
        self.assertTrue(exporter.export_collection('collection-id', 'Horror'))

        emby.items = None
        self.assertFalse(exporter.export_collection('collection-id', 'Horror'))

        self.assertTrue(link.is_symlink())

    def test_paths_outside_mounts_are_rejected(self):
        with self.assertRaises(ValueError):
            validate_library_export_paths(
                str(self.media_root),
                str(self.base / 'outside'),
                self.media_root,
                self.output_mount,
            )

    def test_media_roots_can_be_selected_from_multiple_mounts(self):
        unionfs_root = self.base / 'unionfs'
        unionfs_root.mkdir()

        media_root, output_root = validate_library_export_paths(
            str(unionfs_root),
            str(self.output_mount),
            (self.media_root, unionfs_root),
            self.output_mount,
        )

        self.assertEqual(media_root, str(unionfs_root))
        self.assertEqual(output_root, str(self.output_mount))


if __name__ == '__main__':
    unittest.main()
