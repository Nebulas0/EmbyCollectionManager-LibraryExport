import hashlib
import json
import logging
import os
import re
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from .library_export import (
    DEFAULT_MEDIA_ROOT,
    DEFAULT_OUTPUT_ROOT,
    MEDIA_MOUNT_ROOTS,
    OUTPUT_MOUNT_ROOT,
    validate_library_export_paths,
)

logger = logging.getLogger(__name__)

DEFAULT_MOST_WATCHED_SETTINGS = {
    'movies': {'enabled': False, 'days': 14, 'limit': 10},
    'tv_shows': {'enabled': False, 'days': 14, 'limit': 10},
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
        normalized[kind] = {
            'enabled': enabled,
            'days': max(1, min(days, 3650)),
            'limit': max(1, min(limit, 100)),
        }
    return normalized


class PlaybackReportingClient:
    """Read aggregate playback reports from the Emby Playback Reporting plugin."""

    def __init__(self, emby_client):
        self.emby = emby_client

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

    def get_report(self, media_type: str, days: int) -> List[Dict[str, Any]]:
        endpoints = {
            'movies': '/user_usage_stats/MoviesReport',
            'tv_shows': '/user_usage_stats/TvShowsReport',
        }
        if media_type not in endpoints:
            raise ValueError(f'Unsupported Most Watched type: {media_type}')
        data = self._request_json(endpoints[media_type], params={'days': int(days)})
        if not isinstance(data, list):
            raise PlaybackReportingError('Playback Reporting returned an unexpected report format.')
        return data


def _as_int(value: Any) -> int:
    try:
        return max(0, int(float(value or 0)))
    except (TypeError, ValueError):
        return 0


def _report_row(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(row, dict):
        return None
    label = row.get('label') or row.get('name') or row.get('ItemName')
    if not isinstance(label, str) or not label.strip():
        return None
    return {
        'name': label.strip(),
        'plays': _as_int(row.get('count', row.get('play_count'))),
        'watch_seconds': _as_int(row.get('time', row.get('total_duration', row.get('total_duarion')))),
    }


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
    """Create ranked .strm libraries without copying media bytes or changing source items."""

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

    def _stream_target(self, media_path: Any) -> Optional[str]:
        source = self._source_file(media_path)
        if source is None:
            return None
        if source.suffix.lower() != '.strm':
            return str(source)
        try:
            for line in source.read_text(encoding='utf-8', errors='replace').splitlines():
                target = line.strip()
                if target:
                    if urlsplit(target).scheme in {'http', 'https', 'rtmp', 'rtmps'}:
                        return target
                    target_path = Path(os.path.abspath(target))
                    if (
                        any(_within(target_path, root) for root in self.media_mounts)
                        and target_path.exists()
                    ):
                        return str(target_path)
                    return None
        except OSError:
            return None
        return None

    def _ranked_poster(self, item: Dict[str, Any], rank: int) -> bytes:
        from PIL import Image, ImageDraw, ImageFont

        image_data = None
        item_id = item.get('Id')
        if item_id and hasattr(self.emby, 'get_item_image_bytes'):
            image_data = self.emby.get_item_image_bytes(str(item_id), 'Primary')

        try:
            image = Image.open(BytesIO(image_data)).convert('RGB') if image_data else None
        except Exception:
            image = None
        if image is None:
            image = Image.new('RGB', (600, 900), (30, 37, 43))

        draw = ImageDraw.Draw(image)
        badge = max(58, min(image.width, image.height) // 5)
        margin = max(12, image.width // 28)
        left, top = margin, margin
        right, bottom = left + badge, top + badge
        draw.rounded_rectangle(
            (left, top, right, bottom),
            radius=max(8, badge // 6),
            fill=(206, 47, 47),
            outline=(255, 255, 255),
            width=max(2, badge // 24),
        )
        font_path = Path(__file__).resolve().parent.parent / 'resources' / 'fonts' / 'OpenSans-Bold.ttf'
        font_size = max(24, int(badge * (0.48 if rank >= 10 else 0.58)))
        try:
            font = ImageFont.truetype(str(font_path), font_size)
        except OSError:
            font = ImageFont.load_default()
        label = str(rank)
        bounds = draw.textbbox((0, 0), label, font=font)
        text_width, text_height = bounds[2] - bounds[0], bounds[3] - bounds[1]
        draw.text(
            (left + (badge - text_width) / 2, top + (badge - text_height) / 2 - bounds[1]),
            label,
            fill=(255, 255, 255),
            font=font,
        )
        output = BytesIO()
        image.save(output, format='JPEG', quality=92)
        return output.getvalue()

    @staticmethod
    def _item_folder_name(item: Dict[str, Any], rank: int, is_series: bool = False) -> str:
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
        return f'{rank:02d} - {_safe_component(source_name)}'

    def _movie_files(self, ranked_items: List[Dict[str, Any]]) -> Dict[str, bytes]:
        planned: Dict[str, bytes] = {}
        valid_items = []
        for entry in ranked_items:
            target = self._stream_target(entry.get('item', {}).get('Path'))
            if target:
                valid_items.append((entry, target))
        digits = max(2, len(str(max(1, len(valid_items)))))
        for index, (entry, target) in enumerate(valid_items, 1):
            item = entry['item']
            rank = index
            folder = self._item_folder_name(item, rank)
            base_name = folder
            folder_rel = Path(folder)
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
            planned[str(folder_rel / f'{base_name}.strm')] = (target + '\n').encode('utf-8')
            planned[str(folder_rel / f'{base_name}.nfo')] = _nfo_bytes('movie', fields)
            planned[str(folder_rel / 'poster.jpg')] = self._ranked_poster(item, rank)
        return planned

    def _tv_files(self, ranked_items: List[Dict[str, Any]]) -> Dict[str, bytes]:
        planned: Dict[str, bytes] = {}
        valid_items = []
        for entry in ranked_items:
            series_id = entry.get('item', {}).get('Id')
            episodes = self.emby.get_series_episodes(str(series_id)) if series_id else None
            if episodes:
                accessible = [
                    (episode, self._stream_target(episode.get('Path')))
                    for episode in episodes
                    if episode.get('ParentIndexNumber') is not None
                    and episode.get('IndexNumber') is not None
                ]
                accessible = [(episode, target) for episode, target in accessible if target]
                if accessible:
                    valid_items.append((entry, accessible))
            elif episodes is None:
                raise RuntimeError(
                    f"Could not read episodes for series '{entry.get('item', {}).get('Name', series_id)}'"
                )
        digits = max(2, len(str(max(1, len(valid_items)))))
        for index, (entry, episodes) in enumerate(valid_items, 1):
            series = entry['item']
            folder = self._item_folder_name(series, index, is_series=True)
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
            for episode, target in episodes:
                season_number = episode.get('ParentIndexNumber')
                episode_number = episode.get('IndexNumber')
                if not target or season_number is None or episode_number is None:
                    continue
                series_episodes.append((episode, target, int(season_number), int(episode_number)))
            if not series_episodes:
                logger.warning("Skipping Most Watched series without accessible episodes: %s", series.get('Name'))
                continue

            planned[str(series_rel / 'tvshow.nfo')] = _nfo_bytes('tvshow', tv_fields)
            planned[str(series_rel / 'poster.jpg')] = self._ranked_poster(series, index)
            season_numbers = sorted({season for _, _, season, _ in series_episodes})
            for season in season_numbers:
                season_dir = Path('Specials') if season == 0 else Path(f'Season {season:02d}')
                planned[str(series_rel / season_dir / 'season.nfo')] = _nfo_bytes(
                    'season',
                    [('seasonnumber', season), ('lockedfields', 'Images')],
                )
            for episode, target, season, number in series_episodes:
                season_dir = Path('Specials') if season == 0 else Path(f'Season {season:02d}')
                episode_name = _safe_component(episode.get('Name') or f'Episode {number:02d}')
                base_name = f'{_safe_component(series.get("Name") or folder)} - S{season:02d}E{number:02d} - {episode_name}'
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
                planned[str(series_rel / season_dir / f'{base_name}.strm')] = (target + '\n').encode('utf-8')
                planned[str(series_rel / season_dir / f'{base_name}.nfo')] = _nfo_bytes(
                    'episodedetails',
                    episode_fields,
                )
        return planned

    def _library_dir(self, kind: str) -> Tuple[str, str, Path]:
        library_name, collection_type = self._library_info(kind)
        library_dir = self.output_root / library_name
        if not _within(library_dir, self.output_root):
            raise ValueError('Most Watched output path escaped the configured export directory')
        if library_dir.is_symlink():
            raise ValueError(f'Most Watched output directory is a symlink: {library_dir}')
        return library_name, collection_type, library_dir

    @staticmethod
    def _load_manifest(path: Path) -> Dict[str, str]:
        if path.is_symlink():
            raise ValueError('Most Watched ownership manifest must not be a symlink')
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise ValueError(f'Could not read Most Watched ownership manifest: {exc}') from exc
        files = data.get('files') if isinstance(data, dict) else None
        if not isinstance(files, dict):
            raise ValueError('Most Watched ownership manifest has an invalid format')
        result = {}
        for relative, digest in files.items():
            candidate = Path(relative)
            if candidate.is_absolute() or '..' in candidate.parts or not isinstance(digest, str):
                raise ValueError('Most Watched ownership manifest contains an unsafe path')
            result[candidate.as_posix()] = digest
        return result

    def _reconcile_files(self, kind: str, planned: Dict[str, bytes]) -> Tuple[Path, bool, bool]:
        _, _, library_dir = self._library_dir(kind)
        manifest_path = library_dir / self._manifest_name
        old_files = self._load_manifest(manifest_path)
        if not planned and not old_files:
            return library_dir, False, False

        library_dir.mkdir(parents=True, exist_ok=True)
        desired_hashes = {
            relative: hashlib.sha256(content).hexdigest()
            for relative, content in planned.items()
        }

        for relative in planned:
            candidate = Path(relative)
            if candidate.is_absolute() or '..' in candidate.parts:
                raise ValueError('Refusing an unsafe Most Watched output path')
            destination = library_dir / candidate
            if not _within(destination, library_dir):
                raise ValueError('Most Watched output path escaped its library directory')
            parent = library_dir
            for part in candidate.parts[:-1]:
                parent = parent / part
                if parent.is_symlink():
                    raise ValueError(f'Refusing to write through a symlink in Most Watched output: {parent}')
            if destination.is_symlink():
                raise ValueError(f'Refusing to overwrite a symlink in Most Watched output: {destination}')
            if destination.exists() and relative not in old_files:
                raise ValueError(f'Refusing to overwrite an unmanaged file: {destination}')
            if destination.exists() and not destination.is_file():
                raise ValueError(f'Refusing to overwrite a non-file in Most Watched output: {destination}')

        changed = False
        for relative, content in planned.items():
            destination = library_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.is_file() and _sha256_file(destination) == desired_hashes[relative]:
                continue
            with tempfile.NamedTemporaryFile(dir=destination.parent, prefix='.mw-', delete=False) as staged:
                staged.write(content)
                staged_path = Path(staged.name)
            os.replace(staged_path, destination)
            changed = True

        retained = dict(desired_hashes)
        for relative, old_digest in old_files.items():
            if relative in desired_hashes:
                continue
            destination = library_dir / relative
            parent = library_dir
            parent_is_symlink = False
            for part in Path(relative).parts[:-1]:
                parent = parent / part
                if parent.is_symlink():
                    parent_is_symlink = True
                    break
            if parent_is_symlink or destination.is_symlink() or not destination.is_file():
                continue
            try:
                if _sha256_file(destination) == old_digest:
                    destination.unlink()
                    changed = True
            except OSError:
                logger.warning("Could not prune manager-owned Most Watched file %s", destination)

        manifest = {
            'version': 1,
            'updated': datetime.now(timezone.utc).isoformat(),
            'files': retained,
        }
        with tempfile.NamedTemporaryFile(
            mode='w',
            encoding='utf-8',
            dir=library_dir,
            prefix='.mw-manifest-',
            delete=False,
        ) as staged_manifest:
            json.dump(manifest, staged_manifest, indent=2)
            staged_manifest_path = Path(staged_manifest.name)
        os.replace(staged_manifest_path, manifest_path)
        return library_dir, changed, bool(planned)

    def sync(self, kind: str, ranked_items: List[Dict[str, Any]], create_emby_library: bool) -> Dict[str, Any]:
        library_name, collection_type, _ = self._library_dir(kind)
        planned = self._movie_files(ranked_items) if kind == 'movies' else self._tv_files(ranked_items)
        if ranked_items and not planned:
            raise RuntimeError(f'No accessible media was found for the {kind.replace("_", " ")} Top List')
        library_dir, changed, has_files = self._reconcile_files(kind, planned)
        library_id = None
        created = False
        if has_files and create_emby_library:
            library_info = self.emby.ensure_library(library_name, str(library_dir), collection_type)
            if not library_info:
                raise RuntimeError(f'Could not create or verify Emby library "{library_name}"')
            library_id, created = library_info
        if changed and library_id and not created:
            self.emby.refresh_library(library_id)
        elif changed:
            self.emby.refresh_library_by_name(library_name, str(library_dir))
        return {
            'library_name': library_name,
            'library_path': str(library_dir),
            'library_id': library_id,
            'item_count': len(ranked_items),
            'changed': changed,
        }


def _title_key(value: Any) -> str:
    return re.sub(r'\s+', ' ', str(value or '')).strip().casefold()


def _item_matches_report_name(item: Dict[str, Any], report_name: str) -> bool:
    name = str(item.get('Name') or '').strip()
    report_key = _title_key(report_name)
    name_key = _title_key(name)
    if name_key == report_key:
        return True
    name_without_year = re.sub(r'\s+\(\d{4}\)$', '', name).strip()
    report_without_year = re.sub(r'\s+\(\d{4}\)$', '', report_name).strip()
    year = item.get('ProductionYear')
    if year:
        if _title_key(f'{name} ({year})') == report_key:
            return True
        if re.search(r'\(\d{4}\)$', report_name.strip()):
            return False
    return _title_key(name_without_year) == _title_key(report_without_year)


def _find_report_item(emby_client, report_name: str, item_type: str) -> Optional[Dict[str, Any]]:
    results = emby_client.search_items_by_name(report_name, item_type)
    if results is None:
        raise RuntimeError(f"Could not search Emby for {item_type} '{report_name}'")
    matches = [
        item for item in results
        if item.get('Path') and _item_matches_report_name(item, report_name)
    ]
    return matches[0] if matches else None


def _rank_report_rows(
    report_rows: List[Dict[str, Any]],
    item_type: str,
    emby_client,
    limit: int,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    parsed = [row for row in (_report_row(entry) for entry in report_rows) if row and row['plays'] > 0]
    parsed.sort(key=lambda row: (-row['plays'], -row['watch_seconds'], row['name'].casefold()))
    ranked = []
    missing = []
    lookup_limit = max(100, limit * 10)
    for row in parsed[:lookup_limit]:
        item = _find_report_item(emby_client, row['name'], item_type)
        if item is None:
            missing.append(row['name'])
            continue
        ranked.append({**row, 'item': item})
        if len(ranked) >= limit:
            break
    return ranked, missing


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
    auto_create = exports.get('auto_create_emby_library', True) is not False
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
            exporter.sync(kind, [], create_emby_library=False)
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

    for kind in settings:
        section = settings[kind]
        output_key = 'tv_shows' if kind == 'tv_shows' else 'movies'
        if not section['enabled']:
            continue

        try:
            report_rows = reporting.get_report(kind, section['days'])
            item_type = 'Movie' if kind == 'movies' else 'Series'
            ranked, missing = _rank_report_rows(
                report_rows,
                item_type,
                emby_client,
                section['limit'],
            )
            if report_rows and not ranked:
                raise RuntimeError(
                    f'Playback Reporting returned {len(report_rows)} {output_key} entries, '
                    'but none could be matched to items in Emby; the existing export was left unchanged.'
                )
            export_result = exporter.sync(kind, ranked, create_emby_library=auto_create)
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
            if missing:
                state['errors'].append(
                    f'{output_key}: skipped {len(missing)} playback titles that were not found in Emby.'
                )
        except Exception as exc:
            message = f'{output_key}: {exc}'
            state['errors'].append(message)
            logger.exception("Most Watched %s sync failed", output_key)

    if not state['errors']:
        state['last_sync'] = state['last_attempt']
    _save_most_watched_results(state_dir, state)
    return state
