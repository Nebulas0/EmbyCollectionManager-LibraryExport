from typing import List, Optional, Dict, Any, Tuple
import uuid
import logging
import requests
import os
import sys
from typing import List, Dict, Any, Optional
from urllib.parse import quote

from .base_media_server_client import MediaServerClient
from .poster_generator import generate_custom_poster, file_to_url
from .collection_poster_mapper import get_poster_template_for_collection, load_category_config

logger = logging.getLogger(__name__)

class EmbyClient(MediaServerClient):
    """
    Client for interacting with the Emby server API.
    Inherits from MediaServerClient.
    """
    def get_collection_id(self, collection_name: str) -> Optional[str]:
        data = self._make_api_request(
            'GET',
            f"/Users/{self.user_id}/Items",
            params={
                'IncludeItemTypes': 'BoxSet',
                'Recursive': 'true',
                'SearchTerm': collection_name,
                'Fields': 'Name',
            },
        )
        if not isinstance(data, dict) or not isinstance(data.get('Items'), list):
            raise RuntimeError(f"Could not search Emby collections for '{collection_name}'")
        for item in data['Items']:
            if item.get('Name', '').lower() == collection_name.lower():
                return item.get('Id')
        return None

    def get_or_create_collection(self, collection_name: str) -> Optional[str]:
        """
        Get the Emby collection ID by name, or create it if it does not exist.
        Args:
            collection_name: Name of the collection.
        Returns:
            The collection ID (str) or None if not found/created.
        """
        # Search for the collection by name - try several different search approaches
        # First try exact match
        params = {
            'IncludeItemTypes': 'BoxSet',
            'Recursive': 'true',
            'SearchTerm': collection_name,
            'Fields': 'Name'
        }
        endpoint = f"/Users/{self.user_id}/Items"
        logger.info(f"Searching for collection: '{collection_name}'")
        data = self._make_api_request('GET', endpoint, params=params)
        if data and 'Items' in data:
            for item in data['Items']:
                if item.get('Name', '').lower() == collection_name.lower():
                    logger.info(f"Found existing collection: {item['Name']} (ID: {item['Id']})")
                    return item['Id']
        
        # Collection doesn't exist, create it using a sample item ID (required by Emby)
        logger.info(f"Collection '{collection_name}' not found. Creating new collection...")
        
        # First get a movie or TV show from the library to use as a starting point
        # IMPORTANT: Emby requires that we include at least one item when creating a collection
        params = {
            'IncludeItemTypes': 'Movie',
            'Recursive': 'true',
            'Limit': 1
        }
        endpoint = f"/Users/{self.user_id}/Items"
        item_data = self._make_api_request('GET', endpoint, params=params)
        
        try:
            # Find a movie to use as a starting point for the collection
            if item_data and 'Items' in item_data and item_data['Items']:
                sample_item_id = item_data['Items'][0]['Id']
                logger.info(f"Found sample item with ID: {sample_item_id} to use for collection creation")
                
                # Create the collection using the sample item (this is the key insight from the other code)
                try:
                    # Method 1: Use the direct format demonstrated in the other code
                    # Format: /Collections?api_key=XXX&IsLocked=true&Name=CollectionName&Ids=123456
                    encoded_name = quote(collection_name)
                    # Ensure IsLocked=false if you want to edit it easily later, or true if you want to protect it.
                    # Let's default to false for easier management initially.
                    full_url = f"{self.server_url}/Collections?api_key={self.api_key}&IsLocked=false&Name={encoded_name}&Ids={sample_item_id}"
                    logger.info(f"Creating collection with URL-encoded name and sample item...")
                    
                    # Don't print the full URL with API key
                    logger.info(f"URL (API key hidden): {full_url.replace(self.api_key, 'API_KEY_HIDDEN')}")
                    
                    response = self.session.post(full_url, timeout=15)
                    
                    if response.status_code == 200 and response.text: # Emby usually returns 200 OK with collection details
                        try:
                            data = response.json()
                            if data and 'Id' in data:
                                new_collection_id = data['Id']
                                logger.info(f"Successfully created collection '{collection_name}' with ID: {new_collection_id}")
                                
                                # Remove this temporary item from the collection immediately
                                try:
                                    # The endpoint for removing items from a collection is /Collections/{CollectionId}/Items
                                    # The method is DELETE, not POST, and IDs are passed in query string.
                                    remove_url = f"{self.server_url}/Collections/{new_collection_id}/Items?api_key={self.api_key}&Ids={sample_item_id}"
                                    logger.info(f"Removing temporary item {sample_item_id} from collection {new_collection_id}...")
                                    remove_response = self.session.delete(remove_url, timeout=15) # Correct method is DELETE

                                    # Successful deletion usually returns 204 No Content
                                    if remove_response.status_code == 204:
                                        logger.info(f"Successfully removed temporary item from collection")
                                    else:
                                        logger.warning(f"Failed to remove temporary item from collection: {remove_response.status_code} - {remove_response.text}")
                                except Exception as e:
                                    logger.error(f"Error removing temporary item from collection: {e}")
                                
                                return new_collection_id
                            else:
                                logger.error(f"Invalid response data when creating collection: {data}")
                        except Exception as e:
                            logger.error(f"Error parsing collection creation response: {e} - Response text: {response.text}")
                    else:
                        logger.error(f"Collection creation failed: {response.status_code}")
                        if response.text:
                            logger.error(f"Response: {response.text}")
                except Exception as e:
                    logger.error(f"Error creating collection: {e}")
            else:
                logger.warning("No sample item found to use for collection creation")
                
            # If we get here, collection creation failed
            # Create a fake ID and remember the collection name for later reporting
            # This is just to allow the process to continue for other collections
            if not hasattr(self, '_temp_collections'):
                self._temp_collections = {}
                
            fake_id = str(uuid.uuid4())
            self._temp_collections[fake_id] = collection_name
            logger.info(f"Created temporary placeholder ID for collection '{collection_name}': {fake_id}")
            logger.info(f"Please create this collection manually in your Emby web interface.")
            return fake_id
        except Exception as e:
            logger.error(f"Error during collection creation: {e}")
            return None
            
    def get_libraries(self) -> List[Dict[str, Any]]:
        """
        Get all libraries (virtual folders) from the Emby server.
        Returns a list of dicts with Id, Name, CollectionType.
        """
        try:
            endpoint = "/Library/VirtualFolders"
            data = self._make_api_request('GET', endpoint)
            libraries = []
            if data and isinstance(data, list):
                for folder in data:
                    libraries.append({
                        'Id': folder.get('ItemId') or folder.get('Id'),
                        'Name': folder.get('Name', 'Unknown'),
                        'CollectionType': folder.get('CollectionType', ''),
                        'Locations': folder.get('Locations', [])
                    })
            return libraries
        except Exception as e:
            logger.error(f"Error getting libraries: {e}")
            return []

    def get_collection_items(self, collection_id: str) -> Optional[List[Dict[str, Any]]]:
        endpoint = f"/Users/{self.user_id}/Items"
        items = []
        start_index = 0
        limit = 500

        while True:
            data = self._make_api_request(
                'GET',
                endpoint,
                params={
                    'ParentId': collection_id,
                    'Recursive': 'false',
                    'Fields': 'Path',
                    'StartIndex': start_index,
                    'Limit': limit,
                },
            )
            if not isinstance(data, dict) or not isinstance(data.get('Items'), list):
                return None

            page = data['Items']
            items.extend(page)
            start_index += len(page)
            total = data.get('TotalRecordCount')
            if not page or (total is not None and start_index >= total) or (total is None and len(page) < limit):
                return self._expand_series_items(items)

    def _expand_series_items(self, items: List[Dict[str, Any]]) -> Optional[List[Dict[str, Any]]]:
        series_items = {}
        expanded = []
        for item in items:
            if item.get('Type') not in {'Episode', 'Season'}:
                expanded.append(item)
                continue

            series_id = item.get('SeriesId')
            if not series_id:
                logger.warning("An Emby episode or season did not include its SeriesId")
                return None
            if series_id not in series_items:
                series = self._make_api_request(
                    'GET',
                    f"/Users/{self.user_id}/Items/{series_id}",
                    params={'Fields': 'Path'},
                )
                if not isinstance(series, dict) or series.get('Type') != 'Series' or not series.get('Path'):
                    logger.warning("Could not resolve Emby series path for SeriesId %s", series_id)
                    return None
                series_items[series_id] = series
            expanded.append(series_items[series_id])
        return expanded

    def search_items_by_name(self, name: str, item_type: str) -> Optional[List[Dict[str, Any]]]:
        endpoint = f"/Users/{self.user_id}/Items"
        items = []
        start_index = 0
        limit = 100

        while True:
            data = self._make_api_request(
                'GET',
                endpoint,
                params={
                    'SearchTerm': name,
                    'IncludeItemTypes': item_type,
                    'Recursive': 'true',
                    'Fields': 'Path,ProviderIds,ProductionYear,Overview,Genres,Studios,PremiereDate,OfficialRating,CommunityRating,RunTimeTicks',
                    'StartIndex': start_index,
                    'Limit': limit,
                },
            )
            if not isinstance(data, dict) or not isinstance(data.get('Items'), list):
                return None

            page = data['Items']
            items.extend(page)
            start_index += len(page)
            total = data.get('TotalRecordCount')
            if not page or (total is not None and start_index >= total) or (total is None and len(page) < limit):
                return items

    def get_items_by_ids(self, item_ids: List[str]) -> Optional[List[Dict[str, Any]]]:
        if not item_ids:
            return []
        endpoint = f"/Users/{self.user_id}/Items"
        items = []
        ids = list(dict.fromkeys(str(item_id) for item_id in item_ids if item_id))
        for offset in range(0, len(ids), 100):
            data = self._make_api_request(
                'GET',
                endpoint,
                params={
                    'Ids': ','.join(ids[offset:offset + 100]),
                    'Fields': 'Path,SeriesId,ProviderIds,ProductionYear,Overview,Genres,Studios,PremiereDate,OfficialRating,CommunityRating,RunTimeTicks',
                    'Limit': 100,
                },
            )
            if not isinstance(data, dict) or not isinstance(data.get('Items'), list):
                return None
            items.extend(data['Items'])
        return items

    def get_series_episodes(self, series_id: str) -> Optional[List[Dict[str, Any]]]:
        endpoint = f"/Users/{self.user_id}/Items"
        episodes = []
        start_index = 0
        limit = 500

        while True:
            data = self._make_api_request(
                'GET',
                endpoint,
                params={
                    'ParentId': series_id,
                    'IncludeItemTypes': 'Episode',
                    'Recursive': 'true',
                    'Fields': 'Path,ProviderIds,ProductionYear,Overview,PremiereDate,IndexNumber,ParentIndexNumber,RunTimeTicks',
                    'StartIndex': start_index,
                    'Limit': limit,
                },
            )
            if not isinstance(data, dict) or not isinstance(data.get('Items'), list):
                return None

            page = data['Items']
            episodes.extend(page)
            start_index += len(page)
            total = data.get('TotalRecordCount')
            if not page or (total is not None and start_index >= total) or (total is None and len(page) < limit):
                return episodes

    def get_item_image_bytes(self, item_id: str, image_type: str = 'Primary') -> Optional[bytes]:
        try:
            response = self.session.get(
                f"{self.server_url}/Items/{item_id}/Images/{image_type}",
                timeout=20,
            )
            if response.status_code == 200 and response.content:
                return response.content
        except requests.RequestException as e:
            logger.debug("Could not load %s image for item %s: %s", image_type, item_id, e)
        return None

    def ensure_library(self, name: str, path: str, collection_type: str) -> Optional[Tuple[str, bool]]:
        folders = self._make_api_request('GET', '/Library/VirtualFolders')
        if not isinstance(folders, list):
            return None

        normalized_path = os.path.normpath(path)
        for folder in folders:
            if folder.get('Name') != name:
                continue
            locations = [os.path.normpath(location) for location in folder.get('Locations', []) if isinstance(location, str)]
            if normalized_path not in locations:
                logger.warning("An Emby library named '%s' already exists at a different path", name)
                return None
            folder_id = folder.get('ItemId') or folder.get('Id')
            if folder.get('CollectionType') != collection_type:
                logger.warning("Emby library '%s' already exists with a different content type", name)
            return (folder_id, False) if folder_id else None

        try:
            response = self.session.post(
                f"{self.server_url}/Library/VirtualFolders",
                json={
                    'Name': name,
                    'CollectionType': collection_type,
                    'Paths': [path],
                    'RefreshLibrary': True,
                },
                timeout=30,
            )
            if response.status_code not in (200, 204):
                logger.error("Emby could not create library '%s' (HTTP %s)", name, response.status_code)
                return None
        except requests.RequestException as e:
            logger.error("Emby could not create library '%s': %s", name, e)
            return None

        folders = self._make_api_request('GET', '/Library/VirtualFolders')
        if isinstance(folders, list):
            for folder in folders:
                locations = [os.path.normpath(location) for location in folder.get('Locations', []) if isinstance(location, str)]
                if folder.get('Name') == name and normalized_path in locations:
                    folder_id = folder.get('ItemId') or folder.get('Id')
                    if folder_id:
                        return folder_id, True

        logger.warning("Emby accepted creation of library '%s' but its ID could not be verified", name)
        return None

    def refresh_library(self, library_id: str) -> bool:
        try:
            response = self.session.post(
                f"{self.server_url}/Items/{library_id}/Refresh",
                timeout=30,
            )
            if response.status_code in (200, 204):
                return True
            logger.warning("Emby could not refresh library %s (HTTP %s)", library_id, response.status_code)
        except requests.RequestException as e:
            logger.warning("Emby could not refresh library %s: %s", library_id, e)
        return False

    def refresh_library_by_name(self, name: str, path: str) -> bool:
        folders = self._make_api_request('GET', '/Library/VirtualFolders')
        if not isinstance(folders, list):
            return False

        normalized_path = os.path.normpath(path)
        for folder in folders:
            locations = [os.path.normpath(location) for location in folder.get('Locations', []) if isinstance(location, str)]
            if folder.get('Name') == name and normalized_path in locations:
                folder_id = folder.get('ItemId') or folder.get('Id')
                return self.refresh_library(folder_id) if folder_id else False
        return True

    def get_library_item_ids_by_tmdb_ids(self, tmdb_ids: List[int], library_ids: List[str] = None) -> List[str]:
        """
        Given a list of TMDb IDs, return the Emby server's internal item IDs for owned movies.
        Uses direct provider ID lookup only for optimal performance.
        
        Args:
            tmdb_ids: List of TMDb movie IDs.
            library_ids: Optional list of Emby library IDs to restrict the search to.
                         If None or empty, searches all libraries.
        Returns:
            List of Emby item IDs (str).
        """
        if not tmdb_ids:
            return []
        
        # Convert all IDs to strings for comparison and lookup
        tmdb_ids_str = [str(id) for id in tmdb_ids]
        found_item_ids = []
        
        # Use a set to track which TMDb IDs we've already found to avoid duplicates
        found_tmdb_ids = set()
        total_to_find = len(tmdb_ids_str)
        
        # Determine which libraries to search
        search_library_ids = None
        if library_ids:
            search_library_ids = [lid for lid in library_ids if lid]
        
        if search_library_ids:
            logger.info(f"Searching for {total_to_find} TMDb movies in {len(search_library_ids)} selected libraries...")
        else:
            logger.info(f"Searching for {total_to_find} TMDb movies using direct ID lookup...")
        
        try:
            # Use Emby's AnyProviderIdEquals parameter to directly search for TMDb IDs
            # Process in batches to avoid overloading the server with too many IDs at once
            batch_size = 50  # Size of each TMDb ID batch
            total_found = 0
            batch_counter = 0
            
            for i in range(0, len(tmdb_ids_str), batch_size):
                batch_counter += 1
                batch = tmdb_ids_str[i:i+batch_size]
                
                # Generate a string of TMDb IDs in the format needed by Emby's API
                # Format: "tmdb.12345,tmdb.67890,..." (each must be prefixed with tmdb.)
                tmdb_id_query = ','.join([f"tmdb.{tmdb_id}" for tmdb_id in batch])
                
                params = {
                    'IncludeItemTypes': 'Movie',
                    'Recursive': 'true',
                    'Fields': 'ProviderIds',
                    'AnyProviderIdEquals': tmdb_id_query,
                    # Critical: Set Limit to total_to_find to ensure we get all matches
                    # Setting it to batch_size would limit results per batch
                    'Limit': total_to_find,
                    # Add a cache-busting parameter to avoid stale results
                    '_cb': str(uuid.uuid4().hex),
                }
                
                # If specific libraries are selected, search each one separately
                # because Emby's ParentId parameter only accepts a single ID
                if search_library_ids:
                    for lib_id in search_library_ids:
                        params_copy = dict(params)
                        params_copy['ParentId'] = lib_id
                        endpoint = f"/Users/{self.user_id}/Items"
                        data = self._make_api_request('GET', endpoint, params=params_copy)
                        if not data:
                            continue
                        items = data.get('Items', [])
                        if not items:
                            continue
                        for item in items:
                            provider_ids = item.get('ProviderIds', {})
                            if not provider_ids:
                                continue
                            tmdb_id = None
                            if 'Tmdb' in provider_ids:
                                tmdb_id = provider_ids['Tmdb']
                            elif 'TMDb' in provider_ids:
                                tmdb_id = provider_ids['TMDb']
                            elif 'tmdb' in provider_ids:
                                tmdb_id = provider_ids['tmdb']
                            if tmdb_id and tmdb_id in tmdb_ids_str and tmdb_id not in found_tmdb_ids:
                                found_item_ids.append(item['Id'])
                                found_tmdb_ids.add(tmdb_id)
                    if batch_counter % 5 == 0 or len(found_tmdb_ids) > 0:
                        logger.info(f"Found {len(found_item_ids)} of {total_to_find} TMDb movies so far...")
                    if len(found_tmdb_ids) >= len(tmdb_ids_str):
                        logger.info("Found all requested TMDb movies!")
                        break
                    continue
                
                endpoint = f"/Users/{self.user_id}/Items"
                
                # Only log every 5th batch to reduce log spam
                if batch_counter % 5 == 1:
                    logger.info(f"Fetching batch {batch_counter} ({len(batch)} TMDb IDs)...")
                
                data = self._make_api_request('GET', endpoint, params=params)
                if not data:
                    logger.warning(f"No data returned for batch {batch_counter}")
                    continue
                
                items = data.get('Items', [])
                if not items:
                    continue
                
                # Track items found in this batch
                batch_found = 0
                
                # Extract Emby item IDs and store matching TMDb IDs as found
                for item in items:
                    provider_ids = item.get('ProviderIds', {})
                    if not provider_ids:
                        continue
                    
                    # Check for any recognized TMDb ID format
                    tmdb_id = None
                    if 'Tmdb' in provider_ids:
                        tmdb_id = provider_ids['Tmdb']
                    elif 'TMDb' in provider_ids:
                        tmdb_id = provider_ids['TMDb']
                    elif 'tmdb' in provider_ids:
                        tmdb_id = provider_ids['tmdb']
                    
                    # Only add items that match our search criteria and haven't been found before
                    if tmdb_id and tmdb_id in tmdb_ids_str and tmdb_id not in found_tmdb_ids:
                        found_item_ids.append(item['Id'])
                        found_tmdb_ids.add(tmdb_id)
                        batch_found += 1
                
                total_found += batch_found
                
                # Log progress but only every 5th batch or if this batch had significant finds
                if batch_counter % 5 == 0 or batch_found > 0:
                    logger.info(f"Found {total_found} of {total_to_find} TMDb movies so far...")
                
                # If we found everything, we can stop
                if len(found_tmdb_ids) >= len(tmdb_ids_str):
                    logger.info("Found all requested TMDb movies!")
                    break
                    
            # Final summary
            total_found = len(found_item_ids)
            logger.info(f"Found {total_found} of {total_to_find} TMDb movies ({(total_found/total_to_find)*100:.1f}% match rate).")
            
        except Exception as e:
            logger.error(f"Error searching library for TMDb IDs: {e}")
            
        return found_item_ids

    
    
    

    def get_item_names_by_ids(self, item_ids: List[str]) -> dict:
        """
        Get movie/item names by their IDs to provide better logging.
        
        Args:
            item_ids: List of Emby item IDs
            
        Returns:
            Dictionary mapping item IDs to their names
        """
        result = {}
        
        if not item_ids:
            return result
            
        # Process items in batches to avoid making too many individual API calls
        batch_size = 25 # Emby's Ids parameter can usually take more, but 25 is safe.
        for i in range(0, len(item_ids), batch_size):
            batch = item_ids[i:i+batch_size]
            
            try:
                # Use comma-separated list of IDs to get details for multiple items at once
                ids_param = ",".join(batch)
                # Fetching from /Items requires user_id to get full editable metadata
                endpoint = f"/Users/{self.user_id}/Items?Ids={ids_param}&Fields=Name,SortName" # Added SortName for debugging
                data = self._make_api_request('GET', endpoint) # _make_api_request should handle self.user_id if needed by endpoint
                
                if data and 'Items' in data:
                    for item in data['Items']:
                        if 'Id' in item and 'Name' in item:
                            result[item['Id']] = item['Name']
            except Exception as e:
                logger.error(f"Error fetching names for batch of items: {e}")
        
        return result


    def update_collection_items(self, collection_id: str, item_ids: List[str]) -> bool:
        """
        Set the items for a given Emby collection.
        Args:
            collection_id: The Emby collection ID.
            item_ids: List of Emby item IDs to include in the collection.
        Returns:
            True if successful, False otherwise.
        """
        if not collection_id: # item_ids can be empty if we want to clear a collection
            logger.error("Error: Invalid collection_id")
            return False
        
        if hasattr(self, '_temp_collections') and collection_id in self._temp_collections:
            collection_name = self._temp_collections[collection_id]
            logger.info(f"Cannot update items for pseudo-collection '{collection_name}'")
            return True # Pretend success
        
        # 1. Add/Update items in the collection
        # First, ensure we have no duplicate IDs which could cause issues
        unique_item_ids = list(dict.fromkeys(item_ids))  # Remove duplicates
        
        if len(unique_item_ids) < len(item_ids):
            logger.info(f"Removed {len(item_ids) - len(unique_item_ids)} duplicate item IDs from collection update")
        
        # Emby's /Collections/{Id}/Items endpoint replaces all items with the provided list.
        # However, URLs have length limits (Error 414). For large collections, we need to batch the requests.
        batch_size = 500  # Process items in batches to avoid URL length limits
        
        try:
            logger.info(f"Setting {len(unique_item_ids)} items for collection {collection_id}...")
            
            if len(unique_item_ids) <= batch_size:
                # Small collection - use single request
                items_to_set_str = ",".join(unique_item_ids) if unique_item_ids else ""
                add_items_url = f"{self.server_url}/Collections/{collection_id}/Items?api_key={self.api_key}&Ids={items_to_set_str}"
                response = self.session.post(add_items_url, timeout=30)
            else:
                # Large collection - clear first, then add in batches
                logger.info(f"Large collection detected ({len(unique_item_ids)} items). Using batch processing...")
                
                # First, clear the collection
                clear_url = f"{self.server_url}/Collections/{collection_id}/Items?api_key={self.api_key}&Ids="
                response = self.session.post(clear_url, timeout=30)
                
                if response.status_code != 204:
                    logger.error(f"Failed to clear collection before batch update: {response.status_code}")
                    return False
                
                # Then add items in batches using the add endpoint (not replace)
                add_endpoint = f"{self.server_url}/Collections/{collection_id}/Items?api_key={self.api_key}"
                
                for i in range(0, len(unique_item_ids), batch_size):
                    batch = unique_item_ids[i:i+batch_size]
                    batch_str = ",".join(batch)
                    batch_url = f"{add_endpoint}&Ids={batch_str}"
                    
                    logger.info(f"Adding batch {i//batch_size + 1}: items {i+1}-{min(i+len(batch), len(unique_item_ids))}")
                    batch_response = self.session.post(batch_url, timeout=30)
                    
                    if batch_response.status_code != 204:
                        logger.error(f"Failed to add batch {i//batch_size + 1}: {batch_response.status_code}")
                        return False
                
                # Use the last response for the final status check
                response = batch_response 
            
            if response.status_code == 204: # 204 No Content is success
                logger.info(f"Successfully set items in collection {collection_id}.")

                # Optional: Trigger a refresh on the collection
                try:
                    refresh_url = f"{self.server_url}/Items/{collection_id}/Refresh?api_key={self.api_key}"
                    refresh_response = self.session.post(refresh_url, timeout=30)
                    if refresh_response.status_code in [200, 204]:
                        logger.info(f"Successfully sent refresh command for collection {collection_id}.")
                    else:
                        logger.warning(f"Failed to send refresh command for collection {collection_id}: {refresh_response.status_code}")
                except Exception as e_refresh:
                    logger.warning(f"Error sending refresh command: {e_refresh}")
                
                return True # Overall success if items were added, even if metadata tweaks had issues
            else:
                logger.error(f"Failed to set items in collection: {response.status_code} - {response.text[:200]}")
                return False
        except Exception as e:
            logger.error(f"Error updating collection items: {e}")
            return False

    
    def update_collection_artwork(self, collection_id: str, poster_url: Optional[str]=None, backdrop_url: Optional[str]=None, category_id: Optional[int]=None) -> bool:
        """
        Update artwork for an Emby collection using external image URLs.
        Order of poster selection:
        1. Use provided poster_url if any
        2. If it's a franchise collection (based on category_id), try to fetch from TMDb
        3. For non-franchise collections, generate a custom poster based on category_id
        4. Fall back to first movie's poster as last resort
        
        Args:
            collection_id: The Emby collection ID
            poster_url: URL to collection poster image
            backdrop_url: URL to collection backdrop/fanart image
            category_id: Optional category ID to determine poster template (overrides recipe lookup)
            
        Returns:
            True if at least one image was successfully updated, False otherwise
        """
        # Check if this is a pseudo-ID for a collection we couldn't create
        if hasattr(self, '_temp_collections') and collection_id in self._temp_collections:
            collection_name = self._temp_collections[collection_id]
            logger.info(f"Cannot update artwork for pseudo-collection '{collection_name}'")
            logger.info(f"To use artwork, create the collection manually in your Emby web interface.")
            
            # For placeholder collections, try to generate a custom poster for future use
            if hasattr(self, 'config') and self.config.get('poster_settings', {}).get('enable_custom_posters', True):
                try:
                    # Get poster settings from config
                    poster_settings = self.config.get('poster_settings', {})
                    template_name = poster_settings.get('template_name')
                    text_color = poster_settings.get('text_color')
                    text_position = poster_settings.get('text_position')
                    
                    logger.info(f"Generating custom poster for future use with collection '{collection_name}'")
                    custom_poster_path = generate_custom_poster(
                        collection_name,
                        template_name=template_name,
                        text_color=text_color,
                        text_position=text_position
                    )
                    
                    if custom_poster_path:
                        logger.info(f"Generated custom poster for '{collection_name}' at: {custom_poster_path}")
                        logger.info(f"This poster will be available for manual assignment after creating the collection.")
                    else:
                        logger.warning(f"Failed to generate custom poster for '{collection_name}'")
                except Exception as e:
                    logger.error(f"Error generating custom poster for placeholder collection: {e}")
            
            # We'll pretend it succeeded since we can't do anything about it
            return True
            
        success = False
        collection_data = None
        
        if not collection_id:
            return False
        
        # If no poster_url is provided, try to get one using our priority order
        if not poster_url:
            logger.info("No poster URL provided, attempting to find a suitable poster")
            
            # First, get the collection details (we'll need this in multiple steps)
            try:
                # Get collection data through the user context path which works elsewhere in the code
                collection_endpoint = f"/Users/{self.user_id}/Items/{collection_id}?api_key={self.api_key}"
                collection_data = self._make_api_request('GET', collection_endpoint)
            except Exception as e:
                logger.error(f"Error fetching collection data: {e}")
                collection_data = None
                
            # First, determine the collection's category_id and check if it's a franchise collection
            collection_name = None
            # Note: category_id parameter might be provided, don't reset it to None
            received_category_id = category_id  # Save the parameter value
            is_franchise = False
            template_name = None
            logger.info(f"update_collection_artwork called with category_id: {received_category_id}")
            
            # Make sure we have collection data with name
            if not collection_data or 'Name' not in collection_data:
                collection_endpoint = f"/Users/{self.user_id}/Items/{collection_id}?api_key={self.api_key}"
                collection_data = self._make_api_request('GET', collection_endpoint)
                
            if collection_data and 'Name' in collection_data:
                collection_name = collection_data['Name']
                logger.info(f"Processing collection: '{collection_name}'")
                
                # Look up category_id for this collection from collection_recipes.py
                # Unless category_id was provided as parameter
                try:
                    # Get the path to collection_recipes.py
                    if hasattr(self, 'config') and 'script_dir' in self.config:
                        script_dir = self.config['script_dir']
                    else:
                        # Default to a relative path from current directory
                        script_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                        
                    recipes_file_path = os.path.join(script_dir, 'src', 'collection_recipes.py')
                    
                    # Use provided category_id if available, otherwise look it up
                    if received_category_id is not None:
                        category_id = received_category_id
                        logger.info(f"Using provided category_id {category_id} for collection '{collection_name}'")
                    elif category_id is None:
                        # First try to import directly
                        if script_dir not in sys.path:
                            sys.path.append(script_dir)
                            
                        try:
                            from src.collection_recipes import COLLECTION_RECIPES
                            
                            # Find this collection in the COLLECTION_RECIPES list
                            for recipe in COLLECTION_RECIPES:
                                if recipe.get('name') == collection_name and 'category_id' in recipe:
                                    category_id = recipe['category_id']
                                    logger.info(f"Found category_id {category_id} for collection '{collection_name}'")
                                    break
                        except (ImportError, ModuleNotFoundError) as e:
                            logger.warning(f"Could not import COLLECTION_RECIPES directly: {e}")
                    
                    # If we found a category_id, check if it's a franchise collection
                    if category_id is not None:
                        # Load the category config mapping
                        try:
                            from src.collection_poster_mapper import load_category_config, is_franchise_collection
                            
                            category_map = load_category_config(recipes_file_path)
                            
                            # Check if this is a franchise collection
                            is_franchise = is_franchise_collection(category_id, category_map)
                            logger.debug(f"Collection '{collection_name}' franchise status: {is_franchise} (category_id: {category_id})")
                            
                            # Get template name if not a franchise collection
                            if not is_franchise:
                                from src.collection_poster_mapper import get_poster_template_for_collection
                                template_name = get_poster_template_for_collection(
                                    collection_name=collection_name,
                                    category_poster_map=category_map,
                                    recipes_file_path=recipes_file_path,
                                    category_id=category_id
                                )
                                logger.info(f"Using template '{template_name}' for collection '{collection_name}' (category {category_id})")
                        except Exception as e:
                            logger.error(f"Error determining franchise status: {e}")
                except Exception as e:
                    logger.error(f"Error finding category_id for collection '{collection_name}': {e}")
            else:
                logger.warning("Could not determine collection name for poster selection")
            
            # STEP 1: For franchise collections, try to fetch poster from TMDb
            if is_franchise and not poster_url and collection_data and 'ProviderIds' in collection_data:
                try:
                    tmdb_id = collection_data['ProviderIds'].get('Tmdb')
                    
                    if tmdb_id:
                        logger.info(f"Fetching poster from TMDb for franchise collection '{collection_name}' (ID: {tmdb_id})")
                        # Use uppercase /Items/ for remote images as confirmed working
                        remote_images_endpoint = f"/Items/{collection_id}/RemoteImages?api_key={self.api_key}"
                        remote_images_data = self._make_api_request('GET', remote_images_endpoint)
                        
                        # Look for collection poster in remote images
                        if remote_images_data and 'Images' in remote_images_data:
                            collection_posters = [img for img in remote_images_data['Images'] 
                                                if img.get('Type') == 'Primary' and 
                                                img.get('ProviderName') == 'TheMovieDb']
                            
                            if collection_posters:
                                # Sort by vote average to get the best poster
                                collection_posters.sort(key=lambda x: x.get('CommunityRating', 0), reverse=True)
                                poster_url = collection_posters[0].get('Url')
                                logger.info(f"Found collection poster from TMDb: {poster_url}")
                            else:
                                logger.info(f"No suitable TMDb posters found for franchise collection '{collection_name}'")
                        else:
                            logger.info(f"No remote images available for franchise collection '{collection_name}'")
                    else:
                        logger.info(f"No TMDb ID found for franchise collection '{collection_name}'")
                except Exception as e:
                    logger.error(f"Error fetching TMDb poster: {e}")
            
            # STEP 2: For non-franchise collections, generate a custom poster if enabled
            if not poster_url and not is_franchise and hasattr(self, 'config') and self.config.get('poster_settings', {}).get('enable_custom_posters', True):
                try:
                    if collection_name:
                        # Get poster settings from config
                        poster_settings = self.config.get('poster_settings', {})
                        text_color = poster_settings.get('text_color')
                        text_position = poster_settings.get('text_position')
                        
                        # If we still don't have a template_name from category lookup, use default from config
                        if template_name is None:
                            template_name = poster_settings.get('template_name')
                            logger.info(f"Using default template '{template_name}' from config for collection '{collection_name}'")
                        
                        # Generate custom poster with the determined template
                        logger.info(f"Generating custom poster for non-franchise collection '{collection_name}'")
                        custom_poster_path = generate_custom_poster(
                            collection_name,
                            template_name=template_name,
                            text_color=text_color,
                            text_position=text_position
                        )
                        
                        if custom_poster_path:
                            # Convert file path to URL
                            poster_url = file_to_url(custom_poster_path)
                            logger.info(f"Generated custom poster at: {custom_poster_path}")
                        else:
                            logger.warning(f"Failed to generate custom poster for '{collection_name}'")
                    else:
                        logger.warning("Could not determine collection name for custom poster generation")
                except Exception as e:
                    logger.error(f"Error generating custom poster: {e}")
            elif not poster_url and not (hasattr(self, 'config') and self.config.get('poster_settings', {}).get('enable_custom_posters', True)):
                logger.info("Custom poster generation is disabled in config or config not available")
            
            # STEP 3: Last resort - If still no poster, try using first movie's poster as fallback
            if not poster_url:
                try:
                    logger.info("Falling back to first movie poster in the collection")
                    # Get items in the collection
                    collection_items_endpoint = f"/Items?ParentId={collection_id}&api_key={self.api_key}"
                    items_data = self._make_api_request('GET', collection_items_endpoint)
                    
                    if items_data and 'Items' in items_data and items_data['Items']:
                        first_item = items_data['Items'][0]
                        first_item_id = first_item.get('Id')
                        
                        if first_item_id:
                            # Get remote images for the first item
                            item_images_endpoint = f"/Items/{first_item_id}/RemoteImages?api_key={self.api_key}"
                            item_images_data = self._make_api_request('GET', item_images_endpoint)
                            
                            if item_images_data and 'Images' in item_images_data:
                                movie_posters = [img for img in item_images_data['Images'] 
                                                if img.get('Type') == 'Primary' and 
                                                img.get('ProviderName') == 'TheMovieDb']
                                
                                if movie_posters:
                                    # Sort by vote average to get the best poster
                                    movie_posters.sort(key=lambda x: x.get('CommunityRating', 0), reverse=True)
                                    poster_url = movie_posters[0].get('Url')
                                    logger.info(f"Using first movie's poster as fallback: {poster_url}")
                                else:
                                    logger.info("No movie poster found in TMDb remote images for first item")
                            else:
                                logger.info("No remote images data available for first item")
                        else:
                            logger.info("Could not get ID for the first item in collection")
                    else:
                        logger.info("No items found in collection")
                except Exception as e:
                    logger.error(f"Error trying to fetch first movie poster: {e}")
        
        # Update poster if available - using direct binary upload like Posterizarr
        if poster_url:
            logger.info(f"Attempting to set poster for {collection_id} with URL: {poster_url}")
            try:
                # Download image from URL first
                try:
                    # Handle local file URLs differently from HTTP URLs
                    if poster_url.startswith('file://'):
                        # For local files, read the file directly instead of using requests
                        try:
                            file_path = poster_url[7:]  # Remove 'file://' prefix
                            with open(file_path, 'rb') as f:
                                image_data = f.read()
                            logger.debug(f"Successfully read local file: {file_path}")
                        except Exception as e:
                            logger.error(f"Error reading local file {file_path}: {e}")
                            raise
                    else:
                        # For remote URLs, use requests as normal
                        image_response = requests.get(poster_url, timeout=15)
                        image_response.raise_for_status()
                        image_data = image_response.content
                    
                    # Determine content type based on URL
                    if poster_url.lower().endswith('.jpg') or poster_url.lower().endswith('.jpeg'):
                        content_type = 'image/jpeg'
                    elif poster_url.lower().endswith('.png'):
                        content_type = 'image/png'
                    else:
                        content_type = 'image/jpeg'  # Default to JPEG
                    
                    # Convert image data to Base64 string - THIS IS CRITICAL
                    import base64
                    image_data_base64 = base64.b64encode(image_data).decode('utf-8')
                    
                    # Use the working endpoint pattern that was confirmed to work
                    url = f"{self.server_url}/Items/{collection_id}/Images/Primary?api_key={self.api_key}"
                    logger.info(f"Updating poster for collection {collection_id}")
                    
                    # Send the Base64-encoded image data with content type header
                    response = self.session.post(url, data=image_data_base64, 
                                                headers={'Content-Type': content_type}, 
                                                timeout=15)
                    
                    if response.status_code in [200, 204]:
                        success = True
                        logger.info(f"Poster update successful (status: {response.status_code})")
                    else:
                        logger.error(f"Failed to update poster (status: {response.status_code}) - {response.text}")
                        
                except requests.RequestException as e:
                    logger.error(f"Error downloading/uploading image from URL {poster_url}: {e}")
            except Exception as e:
                logger.error(f"Error updating collection poster: {e}")
            
        # Update backdrop if provided - using direct binary upload like Posterizarr
        if backdrop_url:
            logger.info(f"Attempting to set backdrop for {collection_id} with URL: {backdrop_url}")
            try:
                # Download image from URL first
                try:
                    # Handle local file URLs differently from HTTP URLs
                    if backdrop_url.startswith('file://'):
                        # For local files, read the file directly instead of using requests
                        try:
                            file_path = backdrop_url[7:]  # Remove 'file://' prefix
                            with open(file_path, 'rb') as f:
                                image_data = f.read()
                            logger.debug(f"Successfully read local file: {file_path}")
                        except Exception as e:
                            logger.error(f"Error reading local file {file_path}: {e}")
                            raise
                    else:
                        # For remote URLs, use requests as normal
                        image_response = requests.get(backdrop_url, timeout=15)
                        image_response.raise_for_status()
                        image_data = image_response.content
                    
                    # Determine content type based on URL
                    if backdrop_url.lower().endswith('.jpg') or backdrop_url.lower().endswith('.jpeg'):
                        content_type = 'image/jpeg'
                    elif backdrop_url.lower().endswith('.png'):
                        content_type = 'image/png'
                    else:
                        content_type = 'image/jpeg'  # Default to JPEG
                    
                    # Convert image data to Base64 string - THIS IS CRITICAL
                    import base64
                    image_data_base64 = base64.b64encode(image_data).decode('utf-8')
                    
                    # Use the working endpoint pattern that was confirmed to work
                    url = f"{self.server_url}/Items/{collection_id}/Images/Backdrop?api_key={self.api_key}"
                    logger.info(f"Updating backdrop for collection {collection_id}")
                    
                    # Send the Base64-encoded image data with content type header
                    response = self.session.post(url, data=image_data_base64, 
                                                headers={'Content-Type': content_type}, 
                                                timeout=15)
                    
                    if response.status_code in [200, 204]:
                        success = True
                        logger.info(f"Backdrop update successful (status: {response.status_code})")
                    else:
                        logger.error(f"Failed to update backdrop (status: {response.status_code}) - {response.text}")
                        
                except requests.RequestException as e:
                    logger.error(f"Error downloading/uploading image from URL {backdrop_url}: {e}")
            except Exception as e:
                logger.error(f"Error updating collection backdrop: {e}")
            
        return success

    def get_collection_image(self, collection_id: str, image_type: str = 'Primary') -> Optional[Dict[str, Any]]:
        """
        Get the current image for a collection.
        
        Args:
            collection_id: The Emby collection ID
            image_type: 'Primary' for poster, 'Backdrop' for fanart
            
        Returns:
            Dict with image info or None
        """
        try:
            url = f"{self.server_url}/Items/{collection_id}/Images/{image_type}?api_key={self.api_key}"
            response = self.session.get(url, timeout=10)
            if response.status_code == 200 and response.content:
                import base64
                return {
                    'has_image': True,
                    'content_type': response.headers.get('Content-Type', 'image/jpeg'),
                    'size': len(response.content),
                    'base64': base64.b64encode(response.content).decode('utf-8')
                }
            return {'has_image': False}
        except Exception as e:
            logger.error(f"Error getting collection image: {e}")
            return None

    def delete_collection_image(self, collection_id: str, image_type: str = 'Primary') -> bool:
        """
        Delete/reset a collection image back to default.
        
        Args:
            collection_id: The Emby collection ID
            image_type: 'Primary' for poster, 'Backdrop' for fanart
            
        Returns:
            True if successful, False otherwise
        """
        try:
            url = f"{self.server_url}/Items/{collection_id}/Images/{image_type}?api_key={self.api_key}"
            response = self.session.delete(url, timeout=15)
            if response.status_code in [200, 204]:
                logger.info(f"Deleted {image_type} image for collection {collection_id}")
                return True
            else:
                logger.error(f"Failed to delete image (status: {response.status_code}) - {response.text}")
                return False
        except Exception as e:
            logger.error(f"Error deleting collection image: {e}")
            return False

    def upload_collection_image_from_data(self, collection_id: str, image_data: bytes, 
                                           image_type: str = 'Primary', content_type: str = 'image/jpeg') -> bool:
        """
        Upload an image to a collection from raw bytes.
        
        Args:
            collection_id: The Emby collection ID
            image_data: Raw image bytes
            image_type: 'Primary' for poster, 'Backdrop' for fanart
            content_type: MIME type of the image
            
        Returns:
            True if successful, False otherwise
        """
        try:
            import base64
            image_data_base64 = base64.b64encode(image_data).decode('utf-8')
            url = f"{self.server_url}/Items/{collection_id}/Images/{image_type}?api_key={self.api_key}"
            response = self.session.post(url, data=image_data_base64,
                                         headers={'Content-Type': content_type}, timeout=15)
            if response.status_code in [200, 204]:
                logger.info(f"Uploaded {image_type} image for collection {collection_id}")
                return True
            else:
                logger.error(f"Failed to upload image (status: {response.status_code}) - {response.text}")
                return False
        except Exception as e:
            logger.error(f"Error uploading collection image: {e}")
            return False

    def get_all_collections(self) -> List[Dict[str, Any]]:
        """
        Get all collections from Emby.
        
        Returns:
            List of collection dicts with Id, Name, etc.
        """
        try:
            endpoint = f"/Users/{self.user_id}/Items?api_key={self.api_key}&IncludeItemTypes=BoxSet&Recursive=true"
            data = self._make_api_request('GET', endpoint)
            if data and 'Items' in data:
                return data['Items']
            return []
        except Exception as e:
            logger.error(f"Error getting all collections: {e}")
            return []
