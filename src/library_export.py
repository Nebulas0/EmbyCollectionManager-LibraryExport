import json
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple, Union

logger = logging.getLogger(__name__)

def _media_mount_roots_from_environment():
    roots = os.environ.get('LIBRARY_EXPORT_MEDIA_MOUNTS')
    if roots:
        parsed = tuple(Path(root) for root in roots.split(os.pathsep) if root)
        if parsed:
            return parsed
    return (Path(os.environ.get('LIBRARY_EXPORT_MEDIA_MOUNT', '/mnt/local/strm/library')),)


MEDIA_MOUNT_ROOTS = _media_mount_roots_from_environment()
MEDIA_MOUNT_ROOT = MEDIA_MOUNT_ROOTS[0]
OUTPUT_MOUNT_ROOT = Path(os.environ.get('LIBRARY_EXPORT_OUTPUT_MOUNT', '/mnt/local/embycollectionmanager-library-exports'))
DEFAULT_MEDIA_ROOT = os.environ.get('LIBRARY_EXPORT_DEFAULT_MEDIA_ROOT', str(MEDIA_MOUNT_ROOT))
DEFAULT_OUTPUT_ROOT = str(OUTPUT_MOUNT_ROOT)
MANIFEST_NAME = '.embycollectionmanager.json'


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def validate_library_export_paths(
    media_root: str,
    output_root: str,
    media_mount_root: Union[Path, str, Sequence[Path]] = MEDIA_MOUNT_ROOTS,
    output_mount_root: Path = OUTPUT_MOUNT_ROOT,
) -> Tuple[str, str]:
    if isinstance(media_mount_root, (str, os.PathLike)):
        media_mounts = [Path(media_mount_root)]
    else:
        media_mounts = [Path(root) for root in media_mount_root]
    media_mounts = [root.resolve() for root in media_mounts]
    output_mount = Path(output_mount_root).resolve()
    media = Path(media_root).resolve()
    output = Path(output_root).resolve()

    if not any(_is_within(media, mount) for mount in media_mounts):
        mounted_roots = ', '.join(str(mount) for mount in media_mounts)
        raise ValueError(f'Media root must be inside a mounted media directory: {mounted_roots}')
    if not _is_within(output, output_mount):
        raise ValueError(f'Output directory must be inside the mounted export directory: {output_mount}')
    if not media.is_dir():
        raise ValueError(f'Media root is not an accessible directory: {media}')
    if output.exists() and not output.is_dir():
        raise ValueError(f'Output path is not a directory: {output}')
    if _is_within(media, output) or _is_within(output, media):
        raise ValueError('Media root and output directory must not overlap')

    return str(media), str(output)


def library_name_for_collection(collection_name: str) -> str:
    value = re.sub(r'[^\w .()-]', '_', str(collection_name), flags=re.UNICODE).strip(' .')
    value = re.sub(r'\s+', ' ', value)
    if not value or value in {'.', '..'}:
        raise ValueError('Collection name cannot be used as a library name')
    return value if value.lower().endswith('-library') else f'{value}-library'


class CollectionLibraryExporter:
    def __init__(
        self,
        emby_client,
        media_root: Optional[str] = DEFAULT_MEDIA_ROOT,
        output_root: Optional[str] = DEFAULT_OUTPUT_ROOT,
        media_mount_root: Union[Path, str, Sequence[Path]] = MEDIA_MOUNT_ROOTS,
        output_mount_root: Path = OUTPUT_MOUNT_ROOT,
    ):
        self.emby = emby_client
        self.media_root, self.output_root = validate_library_export_paths(
            media_root or DEFAULT_MEDIA_ROOT,
            output_root or DEFAULT_OUTPUT_ROOT,
            media_mount_root,
            output_mount_root,
        )
        self.media_root = Path(self.media_root)
        self.output_root = Path(self.output_root)

    def export_collection(
        self,
        collection_id: str,
        collection_name: str,
        create_emby_library: bool = True,
    ) -> bool:
        items = self.emby.get_collection_items(collection_id)
        if items is None:
            logger.warning("Could not read Emby collection '%s'; leaving its library export unchanged", collection_name)
            return False

        planned = self._plan_links(items, collection_name)
        if planned is None:
            return False

        library_name = library_name_for_collection(collection_name)
        library_dir = self.output_root / library_name
        changed = self._reconcile_links(library_dir, planned)
        if changed is None:
            return False

        types = {entry['type'] for entry in planned.values()}
        if not types:
            if changed:
                self.emby.refresh_library_by_name(library_name, str(library_dir))
            return True

        if not create_emby_library:
            if changed:
                self.emby.refresh_library_by_name(library_name, str(library_dir))
            return True

        collection_type = 'mixed' if len(types) > 1 else ('movies' if 'Movie' in types else 'tvshows')
        library = self.emby.ensure_library(library_name, str(library_dir), collection_type)
        if not library:
            logger.warning("Could not create or verify Emby library '%s'", library_name)
            return False

        library_id, created = library
        if changed and not created:
            self.emby.refresh_library(library_id)
        return True

    def remove_collection_export(self, collection_name: str) -> bool:
        try:
            library_name = library_name_for_collection(collection_name)
            library_dir = self.output_root / library_name
            if not library_dir.is_dir() or not (library_dir / MANIFEST_NAME).is_file():
                return True
            changed = self._reconcile_links(library_dir, {})
            if changed is None:
                return False
            if changed:
                self.emby.refresh_library_by_name(library_name, str(library_dir))
            return True
        except (OSError, ValueError) as e:
            logger.error("Could not remove managed links for '%s': %s", collection_name, e)
            return False

    def _plan_links(self, items: list, collection_name: str) -> Optional[Dict[str, Dict[str, Any]]]:
        planned: Dict[str, Dict[str, Any]] = {}
        media_root = self.media_root.resolve()

        for item in items:
            item_type = item.get('Type')
            if item_type not in {'Movie', 'Series'}:
                logger.warning(
                    "Unsupported item type '%s' in collection '%s'; leaving its library export unchanged",
                    item_type,
                    collection_name,
                )
                return None

            raw_path = item.get('Path')
            if not raw_path or not Path(raw_path).is_absolute():
                logger.warning("An item in collection '%s' has no usable Emby path", collection_name)
                return None

            item_path = Path(os.path.abspath(os.path.normpath(raw_path)))
            if not _is_within(item_path, media_root):
                logger.warning(
                    "Item path for collection '%s' is outside the configured media root",
                    collection_name,
                )
                return None

            if not item_path.exists():
                logger.info("Skipping missing source path for an item in collection '%s'", collection_name)
                continue

            if item_type == 'Series':
                if not item_path.is_dir():
                    logger.warning(
                        "Series path is not a directory for collection '%s'; leaving its library export unchanged",
                        collection_name,
                    )
                    return None
                source_path = item_path
            elif item_path.is_dir():
                source_path = item_path
            elif item_path.parent == media_root:
                source_path = item_path
            else:
                source_path = item_path.parent

            if source_path == media_root or not source_path.exists():
                logger.warning("Unsafe or missing source directory in collection '%s'", collection_name)
                return None

            link_name = source_path.name
            entry = {
                'source': str(source_path),
                'item_id': str(item.get('Id', '')),
                'type': item_type,
                'is_directory': source_path.is_dir(),
            }
            existing = planned.get(link_name)
            if existing and existing['source'] != entry['source']:
                logger.warning(
                    "Two items in collection '%s' have the same source folder name '%s'; leaving its library export unchanged",
                    collection_name,
                    link_name,
                )
                return None
            planned[link_name] = entry

        return planned

    def _reconcile_links(self, library_dir: Path, planned: Dict[str, Dict[str, Any]]) -> Optional[bool]:
        try:
            self.output_root.mkdir(parents=True, exist_ok=True)
            if library_dir.is_symlink():
                logger.error("Refusing to use a symlink as a library export directory: %s", library_dir)
                return None
            library_dir.mkdir(parents=True, exist_ok=True)
            resolved_dir = library_dir.resolve()
            if not _is_within(resolved_dir, self.output_root.resolve()):
                logger.error("Refusing to write outside the configured library export directory")
                return None

            manifest_path = library_dir / MANIFEST_NAME
            manifest = self._load_manifest(manifest_path)
            if manifest is None:
                logger.error("Invalid library export manifest at %s; leaving the directory unchanged", manifest_path)
                return None

            old_entries = manifest.get('items', {})
            new_entries: Dict[str, Dict[str, Any]] = {}
            changed = False

            for name, entry in planned.items():
                if Path(name).name != name or name in {'', '.', '..'}:
                    logger.error("Refusing unsafe symlink name in library export")
                    return None
                link_path = library_dir / name
                previous = old_entries.get(name, {})

                if link_path.is_symlink():
                    current_target = os.readlink(link_path)
                    if current_target == entry['source']:
                        new_entries[name] = {**entry, 'owned': bool(previous.get('owned'))}
                        continue
                    if not previous.get('owned') or current_target != previous.get('source'):
                        logger.error("Refusing to replace an unmanaged symlink at %s", link_path)
                        return None
                if link_path.exists() and not link_path.is_symlink():
                    logger.error("Refusing to replace an unmanaged file at %s", link_path)
                    return None

                temp_link = library_dir / f'.ecm-{uuid.uuid4().hex}'
                try:
                    os.symlink(entry['source'], temp_link, target_is_directory=entry['is_directory'])
                    os.replace(temp_link, link_path)
                finally:
                    if temp_link.is_symlink():
                        temp_link.unlink()
                new_entries[name] = {**entry, 'owned': True}
                changed = True

            for name, previous in old_entries.items():
                if name in planned or not previous.get('owned'):
                    continue
                link_path = library_dir / name
                if Path(name).name == name and link_path.is_symlink() and os.readlink(link_path) == previous.get('source'):
                    link_path.unlink()
                    changed = True

            new_manifest = {'version': 1, 'items': new_entries}
            temp_manifest = library_dir / f'.ecm-manifest-{uuid.uuid4().hex}'
            try:
                temp_manifest.write_text(json.dumps(new_manifest, indent=2), encoding='utf-8')
                os.replace(temp_manifest, manifest_path)
            finally:
                if temp_manifest.exists():
                    temp_manifest.unlink()

            return changed
        except OSError as e:
            logger.error("Could not reconcile library symlinks in %s: %s", library_dir, e)
            return None

    @staticmethod
    def _load_manifest(path: Path) -> Optional[Dict[str, Any]]:
        if not path.exists():
            return {'version': 1, 'items': {}}
        try:
            manifest = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(manifest, dict) or not isinstance(manifest.get('items'), dict):
            return None
        for name, item in manifest['items'].items():
            if (
                not isinstance(name, str)
                or Path(name).name != name
                or name in {'', '.', '..'}
                or not isinstance(item, dict)
                or not isinstance(item.get('source'), str)
            ):
                return None
        return manifest
