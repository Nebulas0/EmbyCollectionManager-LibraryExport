"""
Recipe override and duplicate management.
Allows customizing built-in recipes (library selection, extra URLs) and
creating duplicates (e.g. one for 4K, one for 1080p).
"""
import os
import json
import logging
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)


class RecipeOverrideManager:
    """Manages recipe overrides and duplicates stored in JSON."""

    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        self.override_path = os.path.join(base_dir, 'config', 'recipe_overrides.json')
        self._cache = None

    def _load(self) -> Dict[str, Any]:
        if self._cache is not None:
            return self._cache
        try:
            with open(self.override_path, 'r', encoding='utf-8') as f:
                self._cache = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            self._cache = {'overrides': {}, 'duplicates': []}
        # Ensure structure
        if 'overrides' not in self._cache:
            self._cache['overrides'] = {}
        if 'duplicates' not in self._cache:
            self._cache['duplicates'] = []
        return self._cache

    def _save(self):
        os.makedirs(os.path.dirname(self.override_path), exist_ok=True)
        with open(self.override_path, 'w', encoding='utf-8') as f:
            json.dump(self._cache, f, indent=2)

    # === Overrides (modify existing recipe behavior) ===

    def get_override(self, recipe_name: str) -> Dict[str, Any]:
        """Get override config for a recipe."""
        data = self._load()
        return data['overrides'].get(recipe_name, {})

    def set_override(self, recipe_name: str, override: Dict[str, Any]):
        """Set override config for a recipe."""
        data = self._load()
        data['overrides'][recipe_name] = override
        self._save()

    def delete_override(self, recipe_name: str):
        """Delete override for a recipe."""
        data = self._load()
        if recipe_name in data['overrides']:
            del data['overrides'][recipe_name]
            self._save()

    def get_all_overrides(self) -> Dict[str, Dict[str, Any]]:
        """Get all overrides."""
        return self._load()['overrides']

    # === Duplicates (create new collections based on existing recipes) ===

    def get_duplicates(self) -> List[Dict[str, Any]]:
        """Get all duplicate definitions."""
        return self._load()['duplicates']

    def add_duplicate(self, duplicate: Dict[str, Any]) -> int:
        """Add a duplicate. Returns its index."""
        data = self._load()
        data['duplicates'].append(duplicate)
        self._save()
        return len(data['duplicates']) - 1

    def update_duplicate(self, index: int, duplicate: Dict[str, Any]):
        """Update a duplicate by index."""
        data = self._load()
        if 0 <= index < len(data['duplicates']):
            data['duplicates'][index] = duplicate
            self._save()

    def delete_duplicate(self, index: int):
        """Delete a duplicate by index."""
        data = self._load()
        if 0 <= index < len(data['duplicates']):
            del data['duplicates'][index]
            self._save()

    # === Helpers for app_logic ===

    def get_library_ids_for_recipe(self, recipe_name: str) -> Optional[List[str]]:
        """Get per-recipe library_ids override, or None."""
        ov = self.get_override(recipe_name)
        ids = ov.get('library_ids')
        if ids is None or ids == []:
            return None
        return ids

    def get_extra_items_for_recipe(self, recipe_name: str) -> Dict[str, List]:
        """Get extra MDBList/Trakt URLs and TMDb IDs to merge into a recipe."""
        ov = self.get_override(recipe_name)
        return {
            'mdblist_urls': ov.get('extra_mdblist_urls', []),
            'trakt_urls': ov.get('extra_trakt_urls', []),
            'tmdb_ids': ov.get('extra_tmdb_ids', []),
        }

    def get_custom_artwork_for_recipe(self, recipe_name: str) -> Dict[str, Optional[str]]:
        """Get custom poster/backdrop URLs for a recipe."""
        ov = self.get_override(recipe_name)
        return {
            'poster_url': ov.get('custom_poster_url'),
            'backdrop_url': ov.get('custom_backdrop_url'),
        }
