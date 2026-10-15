"""Sync history tracking."""
import os
import json
import logging
from datetime import datetime
from typing import Dict, Any, List

logger = logging.getLogger(__name__)


class SyncHistory:
    """Tracks sync run history in JSON."""

    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        self.path = os.path.join(base_dir, 'config', 'sync_history.json')
        self.max_entries = 50

    def _load(self) -> List[Dict[str, Any]]:
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def _save(self, data: List[Dict[str, Any]]):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, 'w', encoding='utf-8') as f:
            json.dump(data[-self.max_entries:], f, indent=2)

    def add_entry(self, entry: Dict[str, Any]):
        """Add a sync history entry."""
        data = self._load()
        data.append(entry)
        self._save(data)

    def get_history(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Get recent sync history."""
        data = self._load()
        return data[-limit:][::-1]  # Most recent first

    def clear(self):
        """Clear all history."""
        self._save([])
