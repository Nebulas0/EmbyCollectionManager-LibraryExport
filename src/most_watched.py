import hashlib
import json
import logging
import os
import re
import stat
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .library_export import (
    DEFAULT_MEDIA_ROOT,
    DEFAULT_OUTPUT_ROOT,
    MANIFEST_NAME,
    MEDIA_MOUNT_ROOTS,
    OUTPUT_MOUNT_ROOT,
    validate_library_export_paths,
)

logger = logging.getLogger(__name__)

DEFAULT_MOST_WATCHED_SETTINGS = {
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
}

MOST_WATCHED_OUTPUT_MODES = {'symlink', 'strm'}
MOST_WATCHED_IMAGE_VARIANTS = {'poster', 'thumb'}
MOST_WATCHED_BADGE_STYLES = {
    'neutral', 'slate-grey', 'emby-green', 'ocean-blue', 'soft-red', 'violet', 'none',
}

STATE_FILE_NAME = 'most_watched_results.json'
MOVIE_LIBRARY_NAME = 'Most Watched Movies-library'
TV_LIBRARY_NAME = 'Most Watched TV Shows-library'


class PlaybackReportingError(RuntimeError):
    pass


def normalize_most_watched_settings(value: Any) -> Dict[str, Dict[str, Any]]:
    value = value if isinstance(value, dict) else {}
    normalized = {}
    for kind, defaults in DEFAULT_MOST_WATCHED_SETTINGS.items():
        section = value.get(kind)
        section = section if isinstance(section, dict) else {}
        try:
            days = int(section.get('days', defaults['days']))
        except (TypeError, ValueError):
            days = defaults['days']
        try:
            limit = int(section.get('limit', defaults['limit']))
        except (TypeError, ValueError):
            limit = defaults['limit']
        enabled_value = section.get('enabled', defaults['enabled'])
        enabled = (
            enabled_value.strip().lower() in {'1', 'true', 'yes', 'on'}
            if isinstance(enabled_value, str)
            else bool(enabled_value)
        )
        raw_library_ids = section.get('library_ids') if 'library_ids' in section else None
        if raw_library_ids is None:
            library_ids = None
        elif isinstance(raw_library_ids, (list, tuple, set)):
            library_ids = list(dict.fromkeys(str(value) for value in raw_library_ids if value))
        else:
            library_ids = []
        raw_target_library_id = section.get('target_library_id')
        target_library_id = str(raw_target_library_id) if raw_target_library_id else None
        output_mode = str(section.get('output_mode', defaults['output_mode'])).lower()
        if output_mode not in MOST_WATCHED_OUTPUT_MODES:
            output_mode = defaults['output_mode']
        raw_variants = section.get('image_variants', defaults['image_variants'])
        if isinstance(raw_variants, (list, tuple, set)):
            image_variants = [
                variant for variant in dict.fromkeys(str(value).lower() for value in raw_variants)
                if variant in MOST_WATCHED_IMAGE_VARIANTS
            ]
        else:
            image_variants = list(defaults['image_variants'])
        if not image_variants:
            image_variants = ['poster']
        badge_style = str(section.get('badge_style', defaults['badge_style'])).lower()
        if badge_style not in MOST_WATCHED_BADGE_STYLES:
            badge_style = defaults['badge_style']
        normalized[kind] = {
            'enabled': enabled,
            'days': max(1, min(days, 3650)),
            'limit': max(1, min(limit, 100)),
            'library_ids': library_ids,
            'target_library_id': target_library_id,
            'output_mode': output_mode,
            'image_variants': image_variants,
            'badge_style': badge_style,
        }
    return normalized


def should_create_most_watched_libraries(config: Dict[str, Any]) -> bool:
    exports = config.get('library_exports') or {}
    global_default = exports.get('auto_create_emby_library', True)
    default_enabled = (
        global_default.strip().lower() in {'1', 'true', 'yes', 'on'}
        if isinstance(global_default, str)
        else bool(global_default)
    )
    value = (config.get('most_watched') or {}).get('auto_create_emby_library')
    if value is None:
        return default_enabled
    return value.strip().lower() in {'1', 'true', 'yes', 'on'} if isinstance(value, str) else bool(value)


class PlaybackReportingClient:
    """Read per-item playback events from the Emby Playback Reporting plugin."""

    def __init__(self, emby_client):
        self.emby = emby_client
        self._playback_user_ids: Optional[List[str]] = None

    def _request_json(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Any:
        base = self.emby.server_url.rstrip('/')
        paths = [endpoint]
        if not base.lower().endswith('/emby'):
            paths.append('/emby' + endpoint)

        last_status = None
        for path in paths:
            try:
                response = self.emby.session.get(
                    base + path,
                    params=params,
                    timeout=30,
                    headers={'Accept': 'application/json'},
                )
            except Exception as exc:
                raise PlaybackReportingError(f'Playback Reporting request failed: {exc}') from exc
            if response.status_code == 404:
                last_status = 404
                continue
            if response.status_code in (401, 403):
                raise PlaybackReportingError(
                    'Playback Reporting requires an Emby administrator API token.'
                )
            if not response.ok:
                raise PlaybackReportingError(
                    f'Playback Reporting returned HTTP {response.status_code}.'
                )
            try:
                return response.json()
            except ValueError as exc:
                raise PlaybackReportingError('Playback Reporting returned invalid JSON.') from exc

        if last_status == 404:
            raise PlaybackReportingError('Playback Reporting plugin was not found on Emby.')
        raise PlaybackReportingError('Playback Reporting endpoint is unavailable.')

    def is_available(self) -> Tuple[bool, str]:
        try:
            self._request_json('/user_usage_stats/type_filter_list')
            return True, 'Playback Reporting is available.'
        except PlaybackReportingError as exc:
            return False, str(exc)

    def _playback_users(self) -> List[str]:
        if self._playback_user_ids is None:
            user_rows = self._request_json('/user_usage_stats/user_list')
            if not isinstance(user_rows, list):
                raise PlaybackReportingError('Playback Reporting returned an unexpected user-list format.')
            self._playback_user_ids = [
                str(row['id']).replace('-', '')
                for row in user_rows
                if isinstance(row, dict) and row.get('id') and not row.get('in_list')
            ]
        return self._playback_user_ids

    def get_playback_events(self, days: int) -> List[Dict[str, Any]]:
        rows = []
        for user_id in self._playback_users():
            user_rows = self._request_json(
                '/user_usage_stats/UserPlaylist',
                params={
                    'user_id': user_id,
                    'aggregate_data': 'false',
                    'days': int(days),
                },
            )
            if not isinstance(user_rows, list):
                raise PlaybackReportingError('Playback Reporting returned an unexpected playback-event format.')
            rows.extend(row for row in user_rows if isinstance(row, dict))
            if len(rows) > 100000:
                raise PlaybackReportingError('Playback Reporting returned too many events for one refresh.')
        return rows


def _as_int(value: Any) -> int:
    try:
        return max(0, int(float(value or 0)))
    except (TypeError, ValueError):
        return 0


def _safe_component(value: str) -> str:
    value = re.sub(r'[\\/:*?"<>|]', '_', str(value or 'Unknown'))
    value = re.sub(r'\s+', ' ', value).strip(' .')
    if not value or value in {'.', '..'}:
        return 'Unknown'
    return value[:180].rstrip(' .')


def _within(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((str(path), str(root))) == str(root)
    except ValueError:
        return False


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _provider_id(item: Dict[str, Any], *names: str) -> str:
    provider_ids = item.get('ProviderIds')
    if not isinstance(provider_ids, dict):
        return ''
    for name in names:
        value = provider_ids.get(name) or provider_ids.get(name.lower()) or provider_ids.get(name.upper())
        if value:
            return str(value)
    return ''


def _add_xml_text(parent: ET.Element, tag: str, value: Any) -> None:
    if value is None or value == '':
        return
    child = ET.SubElement(parent, tag)
    child.text = str(value)


def _nfo_bytes(root_name: str, fields: List[Tuple[str, Any]]) -> bytes:
    root = ET.Element(root_name)
    for tag, value in fields:
        _add_xml_text(root, tag, value)
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)


class MostWatchedLibraryExporter:
    """Create ranked libraries with managed media pointers and generated artwork."""

    _manifest_name = '.most-watched-manifest.json'

    def __init__(
        self,
        emby_client,
        media_root: Optional[str],
        output_root: Optional[str],
        media_mount_root=MEDIA_MOUNT_ROOTS,
        output_mount_root=OUTPUT_MOUNT_ROOT,
    ):
        self.emby = emby_client
        media, output = validate_library_export_paths(
            media_root or DEFAULT_MEDIA_ROOT,
            output_root or DEFAULT_OUTPUT_ROOT,
            media_mount_root,
            output_mount_root,
        )
        self.media_root = Path(media)
        self.output_root = Path(output)
        self.output_mount_root = Path(output_mount_root).resolve()
        if isinstance(media_mount_root, (str, os.PathLike)):
            media_mounts = [Path(media_mount_root)]
        else:
            media_mounts = [Path(root) for root in media_mount_root]
        self.media_mounts = [root.resolve() for root in media_mounts]

    def _library_info(self, kind: str) -> Tuple[str, str]:
        if kind == 'movies':
            return MOVIE_LIBRARY_NAME, 'movies'
        if kind == 'tv_shows':
            return TV_LIBRARY_NAME, 'tvshows'
        raise ValueError(f'Unsupported Most Watched type: {kind}')

    def _source_file(self, value: Any) -> Optional[Path]:
        if not isinstance(value, str) or not value:
            return None
        path = Path(os.path.abspath(value))
        if not any(_within(path, root) for root in self.media_mounts):
            return None
        return path if path.is_file() else None

    def _ranked_image(
        self,
        item: Dict[str, Any],
        rank: int,
        badge_style: str = 'neutral',
        image_variant: str = 'poster',
    ) -> bytes:
        from PIL import Image, ImageDraw, ImageFont

        image_data = None
        item_id = item.get('Id')
        if item_id and hasattr(self.emby, 'get_item_image_bytes'):
            image_types = ('Primary',) if image_variant == 'poster' else ('Thumb', 'Backdrop', 'Primary')
            for image_type in image_types:
                try:
                    image_data = self.emby.get_item_image_bytes(str(item_id), image_type)
                except Exception:
                    image_data = None
                if image_data:
                    break

        try:
            image = Image.open(BytesIO(image_data)).convert('RGB') if image_data else None
        except Exception:
            image = None
        if image is None:
            image = self._local_source_image(item, Image, image_variant)
        if image is None:
            image = Image.new('RGB', (900, 506) if image_variant == 'thumb' else (600, 900), (30, 37, 43))

        if badge_style != 'none':
            badge_colors = {
                'neutral': (0, 0, 0, 210),
                'slate-grey': (65, 65, 75, 224),
                'emby-green': (82, 181, 75, 200),
                'ocean-blue': (46, 134, 193, 210),
                'soft-red': (201, 69, 69, 210),
                'violet': (123, 82, 181, 210),
            }
            badge_color = badge_colors.get(badge_style, badge_colors['neutral'])
            overlay = Image.new('RGBA', image.size, (0, 0, 0, 0))
            draw = ImageDraw.Draw(overlay)
            short_side = min(image.width, image.height)
            radius = max(28, int(short_side * 0.15))
            margin = max(10, int(short_side * 0.04))
            center_x, center_y = margin + radius, margin + radius
            draw.ellipse(
                (center_x - radius, center_y - radius, center_x + radius, center_y + radius),
                fill=badge_color,
            )
            font_path = Path(__file__).resolve().parent.parent / 'resources' / 'fonts' / 'OpenSans-Bold.ttf'
            font_size = max(20, int(radius * 1.1))
            try:
                font = ImageFont.truetype(str(font_path), font_size)
            except OSError:
                font = ImageFont.load_default()
            label = str(rank)
            while draw.textbbox((0, 0), label, font=font)[2] > radius * 1.5 and font_size > 8:
                font_size -= 1
                try:
                    font = ImageFont.truetype(str(font_path), font_size)
                except OSError:
                    font = ImageFont.load_default()
                    break
            bounds = draw.textbbox((0, 0), label, font=font)
            text_width, text_height = bounds[2] - bounds[0], bounds[3] - bounds[1]
            draw.text(
                (center_x - text_width / 2, center_y - text_height / 2 - bounds[1]),
                label,
                fill=(255, 255, 255),
                font=font,
            )
            image = Image.alpha_composite(image.convert('RGBA'), overlay).convert('RGB')
        output = BytesIO()
        image.save(output, format='JPEG', quality=92)
        return output.getvalue()

    def _ranked_poster(self, item: Dict[str, Any], rank: int, badge_style: str = 'neutral') -> bytes:
        return self._ranked_image(item, rank, badge_style, 'poster')

    def _ranked_thumb(self, item: Dict[str, Any], rank: int, badge_style: str = 'neutral') -> bytes:
        return self._ranked_image(item, rank, badge_style, 'thumb')

    def _local_source_image(self, item: Dict[str, Any], image_module, image_variant: str):
        raw_path = item.get('Path')
        if not isinstance(raw_path, str) or not raw_path:
            return None
        item_path = Path(os.path.abspath(raw_path))
        if not any(_within(item_path, root) for root in self.media_mounts):
            return None
        directory = item_path if item_path.is_dir() else item_path.parent
        if image_variant == 'thumb':
            names = [
                'landscape.jpg', 'landscape.png', 'thumb.jpg', 'thumb.png',
                'fanart.jpg', 'fanart.png', 'backdrop.jpg', 'backdrop.png',
            ]
        else:
            names = ['poster.jpg', 'poster.png', 'folder.jpg', 'folder.png', 'cover.jpg', 'cover.png']
        if item_path.is_file():
            suffixes = ('-thumb.jpg', '-landscape.jpg', '-fanart.jpg', '-thumb.png') if image_variant == 'thumb' else (
                '-poster.jpg', '.jpg', '-poster.png',
            )
            names.extend(f'{item_path.stem}{suffix}' for suffix in suffixes)
        for name in names:
            candidate = directory / name
            if not candidate.is_file():
                continue
            try:
                with image_module.open(candidate) as source:
                    return source.convert('RGB')
            except Exception:
                continue
        if image_variant == 'thumb':
            return self._local_source_image(item, image_module, 'poster')
        return None

    @staticmethod
    def _item_folder_name(item: Dict[str, Any], rank: int, is_series: bool = False, digits: int = 2) -> str:
        path = item.get('Path')
        if isinstance(path, str) and path:
            source_path = Path(path)
            source_name = source_path.name if is_series else source_path.parent.name
        else:
            source_name = ''
        if not source_name:
            source_name = item.get('Name') or 'Unknown'
            year = item.get('ProductionYear')
            if year:
                source_name += f' ({year})'
        return f'{rank:0{max(2, digits)}d} - {_safe_component(source_name)}'

    def _movie_files(
        self,
        ranked_items: List[Dict[str, Any]],
        output_mode: str = 'symlink',
        image_variants: Optional[List[str]] = None,
        badge_style: str = 'neutral',
    ) -> Tuple[Dict[str, bytes], Dict[str, str]]:
        planned_files: Dict[str, bytes] = {}
        planned_links: Dict[str, str] = {}
        image_variants = image_variants or ['poster', 'thumb']
        valid_items = []
        for entry in ranked_items:
            source = self._source_file(entry.get('item', {}).get('Path'))
            if source:
                valid_items.append((entry, source))
        digits = max(2, len(str(max(1, len(valid_items)))))
        for index, (entry, source) in enumerate(valid_items, 1):
            item = entry['item']
            rank = index
            folder = self._item_folder_name(item, rank, digits=digits)
            folder_rel = Path(folder)
            media_name = _safe_component(source.stem) + source.suffix
            base_name = Path(media_name).stem
            sort_title = f'{rank:0{digits}d} - {item.get("Name", folder)}'
            fields = [
                ('title', item.get('Name')),
                ('originaltitle', item.get('OriginalTitle')),
                ('sorttitle', sort_title),
                ('year', item.get('ProductionYear')),
                ('plot', item.get('Overview')),
                ('imdbid', _provider_id(item, 'Imdb')),
                ('tmdbid', _provider_id(item, 'Tmdb')),
                ('tvdbid', _provider_id(item, 'Tvdb')),
                ('lockedfields', 'SortName|Images'),
            ]
            planned_files[str(folder_rel / f'{base_name}.nfo')] = _nfo_bytes('movie', fields)
            if 'poster' in image_variants:
                image_name = f'{base_name}.jpg' if output_mode == 'strm' else 'poster.jpg'
                planned_files[str(folder_rel / image_name)] = self._ranked_poster(item, rank, badge_style)
            if 'thumb' in image_variants:
                image_name = f'{base_name}-thumb.jpg' if output_mode == 'strm' else 'landscape.jpg'
                planned_files[str(folder_rel / image_name)] = self._ranked_thumb(item, rank, badge_style)
            if output_mode == 'strm':
                planned_files[str(folder_rel / f'{base_name}.strm')] = str(source).encode('utf-8')
            else:
                planned_links[str(folder_rel / media_name)] = str(source)
        return planned_files, planned_links

    def _tv_files(
        self,
        ranked_items: List[Dict[str, Any]],
        output_mode: str = 'symlink',
        image_variants: Optional[List[str]] = None,
        badge_style: str = 'neutral',
    ) -> Tuple[Dict[str, bytes], Dict[str, str]]:
        planned_files: Dict[str, bytes] = {}
        planned_links: Dict[str, str] = {}
        image_variants = image_variants or ['poster', 'thumb']
        valid_items = []
        for entry in ranked_items:
            series_id = entry.get('item', {}).get('Id')
            episodes = self.emby.get_series_episodes(str(series_id)) if series_id else None
            if episodes:
                accessible = [
                    (episode, self._source_file(episode.get('Path')))
                    for episode in episodes
                    if episode.get('ParentIndexNumber') is not None
                    and episode.get('IndexNumber') is not None
                ]
                accessible = [(episode, source) for episode, source in accessible if source]
                if accessible:
                    valid_items.append((entry, accessible))
            elif episodes is None:
                raise RuntimeError(
                    f"Could not read episodes for series '{entry.get('item', {}).get('Name', series_id)}'"
                )
        digits = max(2, len(str(max(1, len(valid_items)))))
        for index, (entry, episodes) in enumerate(valid_items, 1):
            series = entry['item']
            folder = self._item_folder_name(series, index, is_series=True, digits=digits)
            series_rel = Path(folder)
            sort_title = f'{index:0{digits}d} - {series.get("Name", folder)}'
            tv_fields = [
                ('title', series.get('Name')),
                ('originaltitle', series.get('OriginalTitle')),
                ('sorttitle', sort_title),
                ('year', series.get('ProductionYear')),
                ('premiered', series.get('PremiereDate')),
                ('plot', series.get('Overview')),
                ('imdbid', _provider_id(series, 'Imdb')),
                ('tmdbid', _provider_id(series, 'Tmdb')),
                ('tvdbid', _provider_id(series, 'Tvdb')),
                ('lockedfields', 'SortName|Images'),
            ]
            series_episodes = []
            seen_episodes = set()
            for episode, source in episodes:
                season_number = episode.get('ParentIndexNumber')
                episode_number = episode.get('IndexNumber')
                if not source or season_number is None or episode_number is None:
                    continue
                key = (int(season_number), int(episode_number))
                if key in seen_episodes:
                    continue
                seen_episodes.add(key)
                series_episodes.append((episode, source, key[0], key[1]))
            if not series_episodes:
                logger.warning("Skipping Most Watched series without accessible episodes: %s", series.get('Name'))
                continue

            planned_files[str(series_rel / 'tvshow.nfo')] = _nfo_bytes('tvshow', tv_fields)
            if 'poster' in image_variants:
                planned_files[str(series_rel / 'poster.jpg')] = self._ranked_poster(series, index, badge_style)
            if 'thumb' in image_variants:
                planned_files[str(series_rel / 'landscape.jpg')] = self._ranked_thumb(series, index, badge_style)
            season_numbers = sorted({season for _, _, season, _ in series_episodes})
            for season in season_numbers:
                season_dir = Path('Specials') if season == 0 else Path(f'Season {season:02d}')
                planned_files[str(series_rel / season_dir / 'season.nfo')] = _nfo_bytes(
                    'season',
                    [('seasonnumber', season), ('lockedfields', 'Images')],
                )
            for episode, source, season, number in series_episodes:
                season_dir = Path('Specials') if season == 0 else Path(f'Season {season:02d}')
                media_name = _safe_component(source.stem) + source.suffix
                link_name = f'{Path(media_name).stem}.strm' if output_mode == 'strm' else media_name
                episode_fields = [
                    ('title', episode.get('Name')),
                    ('season', season),
                    ('episode', number),
                    ('plot', episode.get('Overview')),
                    ('aired', episode.get('PremiereDate')),
                    ('imdbid', _provider_id(episode, 'Imdb')),
                    ('tmdbid', _provider_id(episode, 'Tmdb')),
                    ('tvdbid', _provider_id(episode, 'Tvdb')),
                ]
                planned_files[str(series_rel / season_dir / f'{Path(link_name).stem}.nfo')] = _nfo_bytes(
                    'episodedetails',
                    episode_fields,
                )
                if output_mode == 'strm':
                    planned_files[str(series_rel / season_dir / link_name)] = str(source).encode('utf-8')
                else:
                    planned_links[str(series_rel / season_dir / link_name)] = str(source)
        return planned_files, planned_links

    def _library_dir(
        self,
        kind: str,
        target_library_id: Optional[str] = None,
    ) -> Tuple[str, str, Path, Optional[str]]:
        library_name, collection_type = self._library_info(kind)
        if target_library_id:
            libraries = self.emby.get_libraries() if hasattr(self.emby, 'get_libraries') else []
            target = next(
                (library for library in libraries if str(library.get('Id')) == str(target_library_id)),
                None,
            )
            if target is None:
                raise ValueError(f'Selected Most Watched target library {target_library_id} no longer exists')
            if str(target.get('CollectionType', '')).lower() != collection_type:
                raise ValueError(f'Selected target library "{target.get("Name")}" has the wrong media type')
            locations = target.get('Locations') or []
            if len(locations) != 1 or not isinstance(locations[0], str):
                raise ValueError('The selected target library must have exactly one folder location')
            raw_path = Path(os.path.abspath(locations[0]))
            library_dir = raw_path.resolve()
            output_mount_root = self.output_mount_root
            overlaps_media = any(
                _within(library_dir, media_root) or _within(media_root, library_dir)
                for media_root in self.media_mounts
            )
            if raw_path.is_symlink() or library_dir == output_mount_root or not _within(library_dir, output_mount_root):
                raise ValueError(
                    'The selected target library folder must be a non-symlink path inside the writable export mount'
                )
            if overlaps_media:
                raise ValueError('The selected target library folder must not overlap any source media mount')
            if (library_dir / MANIFEST_NAME).is_file():
                raise ValueError('The selected target library folder is already managed by a collection export')
            if library_dir.exists() and not library_dir.is_dir():
                raise ValueError('The selected target library location is not a directory')
            return str(target.get('Name') or library_name), collection_type, library_dir, str(target_library_id)

        library_dir = self.output_root / library_name
        if not _within(library_dir, self.output_root):
            raise ValueError('Most Watched output path escaped the configured export directory')
        if library_dir.is_symlink():
            raise ValueError(f'Most Watched output directory is a symlink: {library_dir}')
        return library_name, collection_type, library_dir, None

    @staticmethod
    def _set_manifest_library(
        library_dir: Path,
        kind: str,
        library_id: Optional[str],
        active: bool,
        limit: int,
    ) -> bool:
        manifest_path = library_dir / MostWatchedLibraryExporter._manifest_name
        if manifest_path.is_symlink() or not manifest_path.is_file():
            return False
        try:
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            logger.warning("Could not add target-library metadata to Most Watched manifest %s: %s", manifest_path, exc)
            return False
        if not isinstance(manifest, dict):
            return False
        configured_limit = max(1, min(int(limit), 100))
        if (
            manifest.get('kind') == kind
            and manifest.get('library_id') == library_id
            and manifest.get('active', True) == active
            and manifest.get('limit') == configured_limit
        ):
            return False
        manifest['kind'] = kind
        manifest['library_id'] = library_id
        manifest['active'] = active
        manifest['limit'] = configured_limit
        with tempfile.NamedTemporaryFile(
            mode='w',
            encoding='utf-8',
            dir=library_dir,
            prefix='.mw-manifest-meta-',
            delete=False,
        ) as staged:
            json.dump(manifest, staged, indent=2)
            os.fchmod(staged.fileno(), 0o644)
            staged_path = Path(staged.name)
        os.replace(staged_path, manifest_path)
        return True

    def _deactivate_other_manifests(self, kind: str, active_library_dir: Optional[Path]) -> None:
        active_key = (
            os.path.normcase(os.path.abspath(str(active_library_dir)))
            if active_library_dir is not None else None
        )
        try:
            manifests = self.output_mount_root.rglob(self._manifest_name)
            for manifest_path in manifests:
                library_dir = manifest_path.parent
                if not _within(library_dir, self.output_mount_root) or library_dir.is_symlink():
                    continue
                path_key = os.path.normcase(os.path.abspath(str(library_dir)))
                if path_key == active_key:
                    continue
                try:
                    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
                    if not isinstance(manifest, dict):
                        continue
                    if (
                        manifest.get('version') != 1
                        or not isinstance(manifest.get('files'), dict)
                        or not isinstance(manifest.get('links'), dict)
                    ):
                        continue
                    old_kind = manifest.get('kind')
                    if old_kind is None:
                        old_kind = (
                            'movies' if library_dir.name == MOVIE_LIBRARY_NAME
                            else 'tv_shows' if library_dir.name == TV_LIBRARY_NAME
                            else None
                        )
                    if old_kind != kind or manifest.get('active') is False:
                        continue
                    manifest['kind'] = kind
                    manifest['active'] = False
                    with tempfile.NamedTemporaryFile(
                        mode='w',
                        encoding='utf-8',
                        dir=library_dir,
                        prefix='.mw-manifest-meta-',
                        delete=False,
                    ) as staged:
                        json.dump(manifest, staged, indent=2)
                        os.fchmod(staged.fileno(), 0o644)
                        staged_path = Path(staged.name)
                    os.replace(staged_path, manifest_path)
                except (OSError, ValueError) as exc:
                    logger.warning("Could not deactivate stale Most Watched manifest %s: %s", manifest_path, exc)
        except OSError as exc:
            logger.warning("Could not inspect old Most Watched output manifests: %s", exc)

    @staticmethod
    def _path_key(path: str) -> str:
        return os.path.normcase(os.path.abspath(path))

    def _find_library_id(self, library_name: str, library_dir: Path) -> Optional[str]:
        if not hasattr(self.emby, 'get_libraries'):
            return None
        expected = self._path_key(str(library_dir))
        for library in self.emby.get_libraries() or []:
            locations = library.get('Locations') or []
            if library.get('Name') == library_name and any(
                isinstance(location, str) and self._path_key(location) == expected
                for location in locations
            ):
                library_id = library.get('Id')
                return str(library_id) if library_id else None
        return None

    def _strm_sort_targets(
        self,
        kind: str,
        library_dir: Path,
        planned_files: Dict[str, bytes],
    ) -> Dict[str, str]:
        targets = {}
        for relative in planned_files:
            path = Path(relative)
            if kind == 'movies' and path.suffix.lower() != '.strm':
                continue
            if kind == 'tv_shows' and path.name != 'tvshow.nfo':
                continue
            rank = path.parts[0].split(' - ', 1)[0] if path.parts else ''
            if not rank.isdigit():
                continue
            item_path = library_dir / (path if kind == 'movies' else path.parent)
            targets[str(item_path)] = rank
        return targets

    def _apply_strm_sort_names(
        self,
        kind: str,
        library_id: str,
        library_dir: Path,
        planned_files: Dict[str, bytes],
    ) -> None:
        if not hasattr(self.emby, 'get_items_in_library') or not hasattr(self.emby, 'update_item_sort_name'):
            return
        targets = self._strm_sort_targets(kind, library_dir, planned_files)
        if not targets:
            return

        item_type = 'Movie' if kind == 'movies' else 'Series'
        expected_keys = {self._path_key(path): (path, sort_name) for path, sort_name in targets.items()}
        indexed = {}
        for attempt in range(5):
            items = self.emby.get_items_in_library(library_id, [item_type])
            if items is None:
                logger.warning("Could not read indexed %s STRM items from Most Watched library", kind)
                return
            indexed = {
                self._path_key(item['Path']): item
                for item in items
                if isinstance(item.get('Path'), str) and item.get('Type') == item_type
            }
            if expected_keys.keys() <= indexed.keys() or attempt == 4:
                break
            time.sleep(1)

        for path_key, (expected_path, sort_name) in expected_keys.items():
            item = indexed.get(path_key)
            if not item:
                logger.debug("Most Watched STRM item is not indexed yet: %s", expected_path)
                continue
            if item.get('SortName') == sort_name:
                continue
            if not self.emby.update_item_sort_name(str(item.get('Id') or ''), sort_name, expected_path):
                logger.warning("Could not set rank Sort Name for Most Watched STRM item %s", expected_path)

    @staticmethod
    def _load_manifest(path: Path) -> Tuple[Dict[str, str], Dict[str, str], Set[str]]:
        if path.is_symlink():
            raise ValueError('Most Watched ownership manifest must not be a symlink')
        if not path.exists():
            return {}, {}, set()
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise ValueError(f'Could not read Most Watched ownership manifest: {exc}') from exc
        files = data.get('files') if isinstance(data, dict) else None
        links = data.get('links', {}) if isinstance(data, dict) else None
        directories = data.get('directories') if isinstance(data, dict) else None
        if not isinstance(files, dict) or not isinstance(links, dict) or (
            directories is not None and not isinstance(directories, list)
        ):
            raise ValueError('Most Watched ownership manifest has an invalid format')

        def validate_paths(values: Dict[str, Any]) -> Dict[str, str]:
            result = {}
            for relative, value in values.items():
                if not isinstance(relative, str):
                    raise ValueError('Most Watched ownership manifest contains an unsafe path')
                candidate = Path(relative)
                if candidate.is_absolute() or '..' in candidate.parts or not isinstance(value, str):
                    raise ValueError('Most Watched ownership manifest contains an unsafe path')
                result[candidate.as_posix()] = value
            return result

        validated_files = validate_paths(files)
        validated_links = validate_paths(links)
        if directories is None:
            # Older manifests did not track owned directories. Their recorded media paths
            # are the only safe source from which to infer removable empty ancestors.
            owned_directories = set()
            for relative in list(validated_files) + list(validated_links):
                parent = Path(relative).parent
                while parent != Path('.'):
                    owned_directories.add(parent.as_posix())
                    parent = parent.parent
        else:
            owned_directories = set(validate_paths({relative: '' for relative in directories}))
        return validated_files, validated_links, owned_directories

    @staticmethod
    def _safe_output_path(root: Path, relative: str) -> Path:
        candidate = Path(relative)
        if candidate.is_absolute() or '..' in candidate.parts:
            raise ValueError('Refusing an unsafe Most Watched output path')
        destination = root / candidate
        if not _within(destination, root):
            raise ValueError('Most Watched output path escaped its library directory')
        parent = root
        for part in candidate.parts[:-1]:
            parent = parent / part
            if parent.is_symlink():
                raise ValueError(f'Refusing to write through a symlink in Most Watched output: {parent}')
        return destination

    @staticmethod
    def _safe_output_directory(root: Path, relative: str) -> Path:
        candidate = Path(relative)
        if candidate.is_absolute() or '..' in candidate.parts:
            raise ValueError('Refusing an unsafe Most Watched directory path')
        directory = root / candidate
        if not _within(directory, root):
            raise ValueError('Most Watched directory path escaped its library directory')
        current = root
        for part in candidate.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError(f'Refusing to traverse a symlink in Most Watched output: {current}')
        return directory

    @staticmethod
    def _create_output_parent(root: Path, destination: Path) -> Set[str]:
        missing = []
        current = destination.parent
        while current != root and not current.exists():
            missing.append(current)
            current = current.parent
        destination.parent.mkdir(parents=True, exist_ok=True)
        created = set()
        for directory in missing:
            if directory.is_dir() and not directory.is_symlink() and _within(directory, root):
                created.add(directory.relative_to(root).as_posix())
        return created

    def _reconcile_files(
        self,
        kind: str,
        planned_files: Dict[str, bytes],
        planned_links: Dict[str, str],
        library_dir: Optional[Path] = None,
    ) -> Tuple[Path, bool, bool]:
        if library_dir is None:
            _, _, library_dir, _ = self._library_dir(kind)
        manifest_path = library_dir / self._manifest_name
        old_files, old_links, old_directories = self._load_manifest(manifest_path)
        if set(planned_files).intersection(planned_links):
            raise ValueError('Most Watched plan contains a path used by both a file and a symlink')
        if not planned_files and not planned_links and not old_files and not old_links and not old_directories:
            return library_dir, False, False

        library_dir.mkdir(parents=True, exist_ok=True)
        desired_hashes = {
            relative: hashlib.sha256(content).hexdigest()
            for relative, content in planned_files.items()
        }
        created_directories: Set[str] = set()

        for relative in planned_files:
            destination = self._safe_output_path(library_dir, relative)
            if destination.is_symlink():
                if relative not in old_links or os.readlink(destination) != old_links[relative]:
                    raise ValueError(f'Refusing to overwrite an unmanaged symlink: {destination}')
            elif destination.exists():
                if relative not in old_files:
                    raise ValueError(f'Refusing to overwrite an unmanaged file: {destination}')
                if not destination.is_file():
                    raise ValueError(f'Refusing to overwrite a non-file in Most Watched output: {destination}')

        for relative, target in planned_links.items():
            destination = self._safe_output_path(library_dir, relative)
            target_path = Path(os.path.abspath(target))
            if not any(_within(target_path, root) for root in self.media_mounts) or not target_path.is_file():
                raise ValueError(f'Refusing a Most Watched symlink target outside mounted media roots: {target}')
            if destination.is_symlink():
                if relative not in old_links or os.readlink(destination) != old_links[relative]:
                    raise ValueError(f'Refusing to replace an unmanaged symlink: {destination}')
            elif destination.exists():
                if relative not in old_files or not destination.is_file():
                    raise ValueError(f'Refusing to replace an unmanaged Most Watched output: {destination}')
                if _sha256_file(destination) != old_files[relative]:
                    raise ValueError(f'Refusing to replace a modified Most Watched output: {destination}')

        changed = False
        for relative, content in planned_files.items():
            destination = self._safe_output_path(library_dir, relative)
            created_directories.update(self._create_output_parent(library_dir, destination))
            if destination.is_symlink():
                destination.unlink()
                changed = True
            if destination.is_file() and _sha256_file(destination) == desired_hashes[relative]:
                if stat.S_IMODE(destination.stat().st_mode) != 0o644:
                    os.chmod(destination, 0o644)
                    changed = True
                continue
            with tempfile.NamedTemporaryFile(dir=destination.parent, prefix='.mw-', delete=False) as staged:
                staged.write(content)
                os.fchmod(staged.fileno(), 0o644)
                staged_path = Path(staged.name)
            os.replace(staged_path, destination)
            changed = True

        for relative, target in planned_links.items():
            destination = self._safe_output_path(library_dir, relative)
            created_directories.update(self._create_output_parent(library_dir, destination))
            if destination.is_symlink() and os.readlink(destination) == target:
                continue
            if destination.is_symlink() or destination.exists():
                destination.unlink()
            os.symlink(target, destination)
            changed = True

        retained_files = dict(desired_hashes)
        for relative, old_digest in old_files.items():
            if relative in desired_hashes or relative in planned_links:
                continue
            destination = self._safe_output_path(library_dir, relative)
            if destination.is_symlink() or not destination.is_file():
                continue
            try:
                if _sha256_file(destination) == old_digest:
                    destination.unlink()
                    changed = True
            except OSError:
                logger.warning("Could not prune manager-owned Most Watched file %s", destination)

        retained_links = dict(planned_links)
        for relative, old_target in old_links.items():
            if relative in planned_links or relative in planned_files:
                continue
            destination = self._safe_output_path(library_dir, relative)
            if destination.is_symlink() and os.readlink(destination) == old_target:
                destination.unlink()
                changed = True

        desired_paths = list(planned_files) + list(planned_links)
        retained_directories = {
            directory for directory in old_directories
            if any(path.startswith(directory.rstrip('/') + '/') for path in desired_paths)
        }
        owned_directories = retained_directories.union(created_directories)
        stale_directories = old_directories.difference(retained_directories)
        for relative in sorted(stale_directories, key=lambda path: len(Path(path).parts), reverse=True):
            try:
                directory = self._safe_output_directory(library_dir, relative)
                if directory.is_symlink() or not directory.is_dir():
                    continue
                directory.rmdir()
                changed = True
            except OSError:
                # Non-empty directories may contain user-owned data; keep them untouched.
                try:
                    directory = self._safe_output_directory(library_dir, relative)
                    if directory.is_dir() and not directory.is_symlink():
                        owned_directories.add(relative)
                except (OSError, ValueError):
                    pass
                continue
            except ValueError:
                # Never traverse or remove a directory that may have been replaced by a symlink.
                continue

        manifest = {
            'version': 1,
            'updated': datetime.now(timezone.utc).isoformat(),
            'files': retained_files,
            'links': retained_links,
            'directories': sorted(owned_directories),
        }
        with tempfile.NamedTemporaryFile(
            mode='w',
            encoding='utf-8',
            dir=library_dir,
            prefix='.mw-manifest-',
            delete=False,
        ) as staged_manifest:
            json.dump(manifest, staged_manifest, indent=2)
            os.fchmod(staged_manifest.fileno(), 0o644)
            staged_manifest_path = Path(staged_manifest.name)
        os.replace(staged_manifest_path, manifest_path)
        return library_dir, changed, bool(planned_files or planned_links)

    def sync(
        self,
        kind: str,
        ranked_items: List[Dict[str, Any]],
        create_emby_library: bool,
        output_mode: str = 'symlink',
        image_variants: Optional[List[str]] = None,
        badge_style: str = 'neutral',
        target_library_id: Optional[str] = None,
        active: bool = True,
        limit: int = 10,
    ) -> Dict[str, Any]:
        limit = max(1, min(int(limit), 100))
        ranked_items = ranked_items[:limit]
        output_mode = str(output_mode).lower()
        if output_mode not in MOST_WATCHED_OUTPUT_MODES:
            raise ValueError(f'Unsupported Most Watched output mode: {output_mode}')
        image_variants = [
            value for value in dict.fromkeys(str(value).lower() for value in (image_variants or ['poster', 'thumb']))
            if value in MOST_WATCHED_IMAGE_VARIANTS
        ] or ['poster']
        badge_style = str(badge_style).lower()
        if badge_style not in MOST_WATCHED_BADGE_STYLES:
            badge_style = 'neutral'
        library_name, collection_type, library_dir, selected_library_id = self._library_dir(
            kind,
            target_library_id,
        )
        if kind == 'movies':
            planned_files, planned_links = self._movie_files(
                ranked_items, output_mode, image_variants, badge_style,
            )
        else:
            planned_files, planned_links = self._tv_files(
                ranked_items, output_mode, image_variants, badge_style,
            )
        if ranked_items and not planned_files and not planned_links:
            raise RuntimeError(f'No accessible media was found for the {kind.replace("_", " ")} Top List')
        library_dir, changed, has_files = self._reconcile_files(
            kind,
            planned_files,
            planned_links,
            library_dir=library_dir,
        )
        library_id = selected_library_id
        created = False
        if selected_library_id:
            pass
        elif has_files and create_emby_library:
            library_info = self.emby.ensure_library(library_name, str(library_dir), collection_type)
            if not library_info:
                raise RuntimeError(f'Could not create or verify Emby library "{library_name}"')
            library_id, created = library_info
        else:
            library_id = self._find_library_id(library_name, library_dir)
        manifest_changed = self._set_manifest_library(library_dir, kind, library_id, active, limit)
        self._deactivate_other_manifests(kind, library_dir if active else None)
        changed = changed or manifest_changed
        if changed and library_id and not created:
            self.emby.refresh_library(library_id)
        elif changed:
            self.emby.refresh_library_by_name(library_name, str(library_dir))
        if output_mode == 'strm' and ranked_items:
            if library_id:
                self._apply_strm_sort_names(kind, library_id, library_dir, planned_files)
        return {
            'library_name': library_name,
            'library_path': str(library_dir),
            'library_id': library_id,
            'item_count': len(ranked_items),
            'limit': limit,
            'changed': changed,
        }


def _library_roots(emby_client, kind: str, selected_ids: Optional[List[str]]) -> List[Path]:
    libraries = emby_client.get_libraries()
    collection_type = 'movies' if kind == 'movies' else 'tvshows'
    eligible = [
        library for library in libraries
        if str(library.get('CollectionType', '')).lower() == collection_type
        and library.get('Name') not in {MOVIE_LIBRARY_NAME, TV_LIBRARY_NAME}
    ]
    if selected_ids is None:
        selected = eligible
    else:
        selected_set = {str(value) for value in selected_ids}
        selected = [
            library for library in eligible
            if str(library.get('Id')) in selected_set
        ]
        missing = selected_set - {str(library.get('Id')) for library in selected}
        if missing:
            raise RuntimeError(f'Selected source library IDs are unavailable: {", ".join(sorted(missing))}')
    if not selected:
        raise RuntimeError(f'Select at least one Emby {collection_type} source library.')

    roots = []
    for library in selected:
        for location in library.get('Locations', []):
            if isinstance(location, str) and location:
                root = Path(os.path.abspath(location))
                if root not in roots:
                    roots.append(root)
    if not roots:
        raise RuntimeError(f'No filesystem paths were returned for the selected {collection_type} libraries.')
    return roots


def _path_is_in_libraries(value: Any, library_roots: List[Path]) -> bool:
    if not isinstance(value, str) or not value:
        return False
    item_path = Path(os.path.abspath(value))
    return any(_within(item_path, root) for root in library_roots)


def _aggregate_events(events: List[Dict[str, Any]], accepted_types: set) -> Dict[str, Dict[str, int]]:
    aggregated: Dict[str, Dict[str, int]] = {}
    for event in events:
        item_type = str(event.get('item_type') or event.get('ItemType') or '')
        item_id = event.get('item_id') or event.get('ItemId')
        if item_type not in accepted_types or item_id is None:
            continue
        key = str(item_id)
        stats = aggregated.setdefault(key, {'plays': 0, 'watch_seconds': 0})
        stats['plays'] += 1
        stats['watch_seconds'] += _as_int(event.get('duration') or event.get('Duration'))
    return aggregated


def _media_identity(item: Dict[str, Any], item_id: str, is_series: bool = False) -> str:
    provider_id = (
        _provider_id(item, 'Tvdb', 'Tmdb', 'Imdb')
        if is_series else _provider_id(item, 'Imdb', 'Tmdb', 'Tvdb')
    )
    return f'provider:{provider_id.casefold()}' if provider_id else f'item:{item_id}'


def _rank_movies_from_events(
    events: List[Dict[str, Any]],
    emby_client,
    library_roots: List[Path],
    limit: int,
) -> List[Dict[str, Any]]:
    stats_by_id = _aggregate_events(events, {'Movie'})
    items = emby_client.get_items_by_ids(list(stats_by_id))
    if items is None:
        raise RuntimeError('Could not resolve Playback Reporting movie item IDs in Emby.')
    grouped: Dict[str, Dict[str, Any]] = {}
    for item in items:
        item_id = str(item.get('Id') or '')
        stats = stats_by_id.get(item_id)
        if not stats or item.get('Type') != 'Movie' or not _path_is_in_libraries(item.get('Path'), library_roots):
            continue
        identity = _media_identity(item, item_id)
        group = grouped.setdefault(identity, {'item': item, 'name': item.get('Name') or '', 'plays': 0, 'watch_seconds': 0})
        group['plays'] += stats['plays']
        group['watch_seconds'] += stats['watch_seconds']
    ranked = sorted(
        grouped.values(),
        key=lambda row: (-row['plays'], -row['watch_seconds'], str(row['name']).casefold()),
    )
    return ranked[:limit]


def _rank_shows_from_events(
    events: List[Dict[str, Any]],
    emby_client,
    library_roots: List[Path],
    limit: int,
) -> List[Dict[str, Any]]:
    episode_stats = _aggregate_events(events, {'Episode'})
    episodes = emby_client.get_items_by_ids(list(episode_stats))
    if episodes is None:
        raise RuntimeError('Could not resolve Playback Reporting episode IDs in Emby.')
    stats_by_series: Dict[str, Dict[str, int]] = {}
    for episode in episodes:
        episode_id = str(episode.get('Id') or '')
        stats = episode_stats.get(episode_id)
        series_id = episode.get('SeriesId')
        if not stats or episode.get('Type') != 'Episode' or not series_id:
            continue
        if not _path_is_in_libraries(episode.get('Path'), library_roots):
            continue
        key = str(series_id)
        group = stats_by_series.setdefault(key, {'plays': 0, 'watch_seconds': 0})
        group['plays'] += stats['plays']
        group['watch_seconds'] += stats['watch_seconds']
    if not stats_by_series:
        return []

    series_items = emby_client.get_items_by_ids(list(stats_by_series))
    if series_items is None:
        raise RuntimeError('Could not resolve Playback Reporting series IDs in Emby.')
    grouped: Dict[str, Dict[str, Any]] = {}
    for series in series_items:
        series_id = str(series.get('Id') or '')
        stats = stats_by_series.get(series_id)
        if not stats or series.get('Type') != 'Series' or not _path_is_in_libraries(series.get('Path'), library_roots):
            continue
        identity = _media_identity(series, series_id, is_series=True)
        group = grouped.setdefault(identity, {'item': series, 'name': series.get('Name') or '', 'plays': 0, 'watch_seconds': 0})
        group['plays'] += stats['plays']
        group['watch_seconds'] += stats['watch_seconds']
    ranked = sorted(
        grouped.values(),
        key=lambda row: (-row['plays'], -row['watch_seconds'], str(row['name']).casefold()),
    )
    return ranked[:limit]


def load_most_watched_results(state_dir: str) -> Dict[str, Any]:
    path = Path(state_dir) / STATE_FILE_NAME
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_most_watched_results(state_dir: str, state: Dict[str, Any]) -> None:
    path = Path(state_dir) / STATE_FILE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode='w',
        encoding='utf-8',
        dir=path.parent,
        prefix='.most-watched-',
        delete=False,
    ) as staged:
        json.dump(state, staged, indent=2, default=str)
        staged_path = Path(staged.name)
    os.replace(staged_path, path)


def sync_most_watched(emby_client, config: Dict[str, Any], state_dir: str) -> Dict[str, Any]:
    settings = normalize_most_watched_settings(config.get('most_watched'))
    exports = config.get('library_exports') or {}
    auto_create = should_create_most_watched_libraries(config)
    previous = load_most_watched_results(state_dir)
    exporter = MostWatchedLibraryExporter(
        emby_client,
        media_root=exports.get('media_root', DEFAULT_MEDIA_ROOT),
        output_root=exports.get('output_root', DEFAULT_OUTPUT_ROOT),
    )
    state: Dict[str, Any] = {
        'last_sync': previous.get('last_sync'),
        'last_attempt': datetime.now(timezone.utc).isoformat(),
        'plugin_available': None,
        'movies': previous.get('movies', []),
        'tv_shows': previous.get('tv_shows', []),
        'errors': [],
        'libraries': previous.get('libraries', {}),
    }

    for kind, section in settings.items():
        if section['enabled']:
            continue
        try:
            exporter.sync(
                kind,
                [],
                create_emby_library=False,
                target_library_id=section.get('target_library_id'),
                active=False,
                limit=section['limit'],
            )
            output_key = 'tv_shows' if kind == 'tv_shows' else 'movies'
            state[output_key] = []
            state['libraries'].pop(output_key, None)
        except Exception as exc:
            state['errors'].append(f'{kind}: {exc}')

    enabled_kinds = [kind for kind, section in settings.items() if section['enabled']]
    if not enabled_kinds:
        state['last_sync'] = state['last_attempt']
        _save_most_watched_results(state_dir, state)
        return state

    reporting = PlaybackReportingClient(emby_client)
    available, availability_message = reporting.is_available()
    state['plugin_available'] = available
    if not available:
        state['errors'].append(availability_message)
        logger.warning("Most Watched sync skipped: %s", availability_message)
        _save_most_watched_results(state_dir, state)
        return state

    events_by_days: Dict[int, List[Dict[str, Any]]] = {}
    for kind in settings:
        section = settings[kind]
        output_key = 'tv_shows' if kind == 'tv_shows' else 'movies'
        if not section['enabled']:
            continue

        try:
            library_roots = _library_roots(emby_client, kind, section.get('library_ids'))
            days = section['days']
            if days not in events_by_days:
                events_by_days[days] = reporting.get_playback_events(days)
            events = events_by_days[days]
            if kind == 'movies':
                ranked = _rank_movies_from_events(events, emby_client, library_roots, section['limit'])
            else:
                ranked = _rank_shows_from_events(events, emby_client, library_roots, section['limit'])
            export_result = exporter.sync(
                kind,
                ranked,
                create_emby_library=auto_create,
                output_mode=section['output_mode'],
                image_variants=section['image_variants'],
                badge_style=section['badge_style'],
                target_library_id=section.get('target_library_id'),
                limit=section['limit'],
            )
            state[output_key] = [
                {
                    'rank': index,
                    'name': entry['name'],
                    'plays': entry['plays'],
                    'watch_seconds': entry['watch_seconds'],
                }
                for index, entry in enumerate(ranked, 1)
            ]
            state['libraries'][output_key] = export_result
        except Exception as exc:
            message = f'{output_key}: {exc}'
            state['errors'].append(message)
            logger.exception("Most Watched %s sync failed", output_key)

    if not state['errors']:
        state['last_sync'] = state['last_attempt']
    _save_most_watched_results(state_dir, state)
    return state
