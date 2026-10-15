"""
List metadata management for per-collection settings.
Stores library selection, custom names, and other per-list config.
"""
import os
import json
import logging
from typing import Dict, Any, List, Optional
from pathlib import Path

logger = logging.getLogger(__name__)


class ListMetadataManager:
    """Manages per-collection metadata (library IDs, custom names, etc)."""

    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        self.metadata_path = os.path.join(base_dir, 'config', 'list_metadata.json')
        self._cache = None

    def _load(self) -> Dict[str, Any]:
        """Load metadata from disk."""
        if self._cache is not None:
            return self._cache
        try:
            with open(self.metadata_path, 'r', encoding='utf-8') as f:
                self._cache = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            self._cache = {}
        return self._cache

    def _save(self, data: Dict[str, Any]):
        """Save metadata to disk."""
        os.makedirs(os.path.dirname(self.metadata_path), exist_ok=True)
        with open(self.metadata_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
        self._cache = data

    def get_list_config(self, list_type: str, filename: str) -> Dict[str, Any]:
        """
        Get config for a specific list file.
        
        Args:
            list_type: 'traktlists' or 'mdblists'
            filename: The filename (e.g., 'My Collection.txt')
            
        Returns:
            Dict with optional keys:
                - collection_name: Custom collection name (overrides filename)
                - library_ids: List of Emby library IDs to scan
                - poster_url: Custom poster URL
                - backdrop_url: Custom backdrop URL
                - category_id: Custom category ID for poster template
        """
        data = self._load()
        return data.get(list_type, {}).get(filename, {})

    def set_list_config(self, list_type: str, filename: str, config: Dict[str, Any]):
        """Set config for a specific list file."""
        data = self._load()
        if list_type not in data:
            data[list_type] = {}
        data[list_type][filename] = config
        self._save(data)

    def delete_list_config(self, list_type: str, filename: str):
        """Delete config for a list file."""
        data = self._load()
        if list_type in data and filename in data[list_type]:
            del data[list_type][filename]
            self._save(data)

    def get_all_configs(self, list_type: str) -> Dict[str, Dict[str, Any]]:
        """Get all configs for a list type."""
        data = self._load()
        return data.get(list_type, {})

    def get_library_ids_for_list(self, list_type: str, filename: str) -> Optional[List[str]]:
        """Get library IDs for a specific list, or None if not set."""
        cfg = self.get_list_config(list_type, filename)
        ids = cfg.get('library_ids')
        if ids is None or ids == []:
            return None
        return ids

    def get_collection_name_for_list(self, list_type: str, filename: str) -> Optional[str]:
        """Get custom collection name for a list, or None to use filename."""
        cfg = self.get_list_config(list_type, filename)
        return cfg.get('collection_name')
