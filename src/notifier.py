"""Webhook/Discord notifications for sync events."""
import logging
import requests
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


class Notifier:
    """Sends notifications to Discord/webhook."""

    def __init__(self, config: Dict[str, Any]):
        notif_cfg = config.get('notifications', {})
        self.webhook_url = notif_cfg.get('webhook_url', '')
        self.enabled = notif_cfg.get('enabled', False)
        self.notify_on_success = notif_cfg.get('notify_on_success', True)
        self.notify_on_error = notif_cfg.get('notify_on_error', True)

    def _send_discord(self, title: str, description: str, color: int):
        """Send a Discord webhook message."""
        if not self.webhook_url:
            return
        payload = {
            'embeds': [{
                'title': title,
                'description': description,
                'color': color,
                'footer': {'text': 'Emby Collection Manager'}
            }]
        }
        try:
            resp = requests.post(self.webhook_url, json=payload, timeout=10)
            if resp.status_code in [200, 204]:
                logger.info(f"Notification sent: {title}")
            else:
                logger.warning(f"Notification failed: {resp.status_code}")
        except Exception as e:
            logger.warning(f"Notification error: {e}")

    def notify_sync_start(self, collection_count: int = 0):
        if not self.enabled:
            return
        desc = f"Starting sync with {collection_count} collections" if collection_count else "Starting sync"
        self._send_discord("Sync Started", desc, 0x3498db)

    def notify_sync_success(self, duration: str, collections: int = 0, errors: int = 0):
        if not self.enabled or not self.notify_on_success:
            return
        desc = f"Completed in {duration}"
        if collections:
            desc += f"\nCollections processed: {collections}"
        if errors:
            desc += f"\nWarnings/errors: {errors}"
        self._send_discord("Sync Complete", desc, 0x2ecc71)

    def notify_sync_error(self, error: str, duration: str = ""):
        if not self.enabled or not self.notify_on_error:
            return
        desc = f"Error: {error}"
        if duration:
            desc += f"\nDuration: {duration}"
        self._send_discord("Sync Failed", desc, 0xe74c3c)

    def notify_single_collection(self, collection_name: str, success: bool, item_count: int = 0, error: str = ""):
        if not self.enabled:
            return
        if success:
            desc = f"Collection '{collection_name}' synced with {item_count} items"
            self._send_discord("Collection Synced", desc, 0x2ecc71)
        else:
            desc = f"Failed to sync '{collection_name}': {error}"
            self._send_discord("Collection Sync Failed", desc, 0xe74c3c)
