"""
MDBList Processor for Emby Collection Manager

This module processes user-created text files in the mdblists directory
and converts them into collections with TMDb movie IDs.
"""

import os
import re
import logging
import yaml
from typing import List, Dict, Any, Set, Optional
from pathlib import Path
from src.list_metadata import ListMetadataManager, parse_optional_bool

logger = logging.getLogger(__name__)

class MDBListProcessor:
    """
    Processes text files containing MDBList URLs, TMDb IDs, and movie titles
    to create collections for media servers.
    """
    
    def __init__(self, tmdb_client, mdblist_client=None, config=None):
        """
        Initialize the processor with required clients.
        
        Args:
            tmdb_client: TMDb client for movie lookups
            mdblist_client: Optional MDBList client for list processing
            config: Configuration dictionary
        """
        self.tmdb_client = tmdb_client
        self.mdblist_client = mdblist_client
        self.config = config or {}
        
        # Configuration options
        mdblists_config = self.config.get('mdblists', {})
        self.enabled = mdblists_config.get('enabled', True)
        self.directory = mdblists_config.get('directory', 'mdblists')
        try:
            self.max_items = max(0, int(mdblists_config.get('max_items_per_collection', 0) or 0))
        except (TypeError, ValueError):
            self.max_items = 0
        base_dir = self.config.get('script_dir') or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.list_metadata = ListMetadataManager(base_dir)
        
    def is_enabled(self) -> bool:
        """Check if MDBList processing is enabled."""
        return self.enabled
    
    def get_mdblists_directory(self) -> Path:
        """Get the path to the mdblists directory."""
        return Path(self.directory)
    
    def scan_mdblists_directory(self) -> List[Dict[str, Any]]:
        """
        Scan the mdblists directory for .txt files and process them.
        
        Returns:
            List of collection dictionaries ready for processing
        """
        if not self.is_enabled():
            logger.info("MDBList processing is disabled")
            return []
            
        mdblists_dir = self.get_mdblists_directory()
        
        if not mdblists_dir.exists():
            logger.info(f"MDBList directory '{mdblists_dir}' does not exist")
            return []
            
        logger.info(f"Scanning MDBList directory: {mdblists_dir}")
        collections = []
        
        # Find all .txt and .yaml/.yml files in the directory
        list_files = list(mdblists_dir.glob("*.txt")) + list(mdblists_dir.glob("*.yaml")) + list(mdblists_dir.glob("*.yml"))
        
        if not list_files:
            logger.info("No list files found in mdblists directory")
            return []
            
        logger.info(f"Found {len(list_files)} MDBList files to process")
        
        for list_file in list_files:
            try:
                collection = self.process_mdblist_file(list_file)
                if collection:
                    collections.append(collection)
            except Exception as e:
                logger.error(f"Error processing file '{list_file.name}': {e}")
                
        return collections
    
    def process_mdblist_file(self, file_path: Path) -> Dict[str, Any]:
        """
        Process a single mdblist file and extract movie IDs.
        Supports .txt (one item per line) and .yaml/.yml (structured) formats.
        
        YAML format supports:
          - collection_name: "Custom Name"  # overrides filename
          - library_ids: ["lib1", "lib2"]   # per-collection library selection
          - auto_create_emby_library: false # override global library registration setting
          - category_id: 13                  # custom category for poster template
          - poster_url: "https://..."        # custom poster URL
          - backdrop_url: "https://..."      # custom backdrop URL
          - items:                           # list of MDBList URLs, TMDb IDs, or titles
              - https://mdblist.com/lists/...
              - https://mdblist.com/lists/...
              - 12345
              - "Movie Title"
        
        Args:
            file_path: Path to the .txt or .yaml file
            
        Returns:
            Dictionary containing collection info and TMDb IDs
        """
        collection_name = file_path.stem  # Filename without extension
        custom_name = None
        library_ids = None
        category_id = 13  # Default MDBList category
        poster_url = None
        backdrop_url = None
        item_limit = None
        create_library = False
        auto_create_emby_library = None
        seasonal = None
        
        is_yaml = file_path.suffix in ('.yaml', '.yml')
        
        logger.info(f"Processing MDBList file: {file_path.name} -> Collection: '{collection_name}'")
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except Exception as e:
            logger.error(f"Failed to read file '{file_path}': {e}")
            return None
        
        if is_yaml:
            try:
                yaml_data = yaml.safe_load(content)
                if isinstance(yaml_data, dict):
                    custom_name = yaml_data.get('collection_name')
                    library_ids = yaml_data.get('library_ids')
                    cat = yaml_data.get('category_id')
                    if cat is not None:
                        category_id = cat
                    poster_url = yaml_data.get('poster_url')
                    backdrop_url = yaml_data.get('backdrop_url')
                    item_limit = yaml_data.get('item_limit')
                    create_library = bool(yaml_data.get('create_library', False))
                    auto_create_emby_library = parse_optional_bool(yaml_data.get('auto_create_emby_library'))
                    seasonal = yaml_data.get('seasonal')
                    items = yaml_data.get('items', [])
                    # Convert items to text format for processing
                    content = '\n'.join(str(i) for i in items) if items else ''
                elif isinstance(yaml_data, list):
                    content = '\n'.join(str(i) for i in yaml_data)
            except yaml.YAMLError as e:
                logger.warning(f"Failed to parse YAML in '{file_path.name}', treating as text: {e}")
        elif content.strip().startswith('---'):
            # YAML frontmatter in a .txt file
            try:
                if '\n---\n' in content:
                    frontmatter, content = content.split('\n---\n', 1)
                    frontmatter = frontmatter.strip().lstrip('-').strip()
                    yaml_data = yaml.safe_load(frontmatter)
                    if isinstance(yaml_data, dict):
                        custom_name = yaml_data.get('collection_name')
                        library_ids = yaml_data.get('library_ids')
                        cat = yaml_data.get('category_id')
                        if cat is not None:
                            category_id = cat
                        poster_url = yaml_data.get('poster_url')
                        backdrop_url = yaml_data.get('backdrop_url')
                        item_limit = yaml_data.get('item_limit')
                        create_library = bool(yaml_data.get('create_library', False))
                        auto_create_emby_library = parse_optional_bool(yaml_data.get('auto_create_emby_library'))
                        seasonal = yaml_data.get('seasonal')
            except yaml.YAMLError:
                pass

        try:
            metadata = self.list_metadata.get_list_config('mdblists', file_path.name)
        except Exception as exc:
            logger.warning("Could not load saved settings for %s: %s", file_path.name, exc)
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        if 'collection_name' in metadata:
            custom_name = metadata.get('collection_name')
        if 'library_ids' in metadata:
            library_ids = metadata.get('library_ids')
        if 'category_id' in metadata:
            category_id = metadata.get('category_id') or category_id
        if 'poster_url' in metadata:
            poster_url = metadata.get('poster_url')
        if 'backdrop_url' in metadata:
            backdrop_url = metadata.get('backdrop_url')
        if 'item_limit' in metadata:
            item_limit = metadata.get('item_limit')
        if 'create_library' in metadata:
            create_library = bool(metadata['create_library'])
        if 'auto_create_emby_library' in metadata:
            auto_create_emby_library = parse_optional_bool(metadata.get('auto_create_emby_library'))
        if 'seasonal' in metadata:
            seasonal = metadata['seasonal']
        
        if custom_name:
            collection_name = custom_name
            logger.info(f"Using custom collection name: '{collection_name}'")
        
        tmdb_ids = self.extract_tmdb_ids_from_content(content, collection_name)
        
        if not tmdb_ids:
            logger.warning(f"No valid movies found in '{file_path.name}'")
            
        raw_limit = self.max_items if item_limit is None else item_limit
        try:
            effective_limit = max(0, int(raw_limit or 0))
        except (TypeError, ValueError):
            effective_limit = self.max_items
        if effective_limit and len(tmdb_ids) > effective_limit:
            logger.info(f"Limiting collection '{collection_name}' to {effective_limit} items (was {len(tmdb_ids)})")
            tmdb_ids = tmdb_ids[:effective_limit]
            
        logger.info(f"Found {len(tmdb_ids)} movies for collection '{collection_name}'")
        
        return {
            'name': collection_name,
            'source_type': 'mdblist_file',
            'tmdb_ids': tmdb_ids,
            'target_servers': ['emby'],
            'category_id': category_id,
            'file_path': str(file_path),
            'library_ids': library_ids,
            'poster_url': poster_url,
            'backdrop_url': backdrop_url,
            'item_limit': effective_limit,
            'create_library': create_library,
            'auto_create_emby_library': auto_create_emby_library,
            'seasonal': seasonal,
        }
    
    def extract_tmdb_ids_from_content(self, content: str, collection_name: str) -> List[int]:
        """
        Extract TMDb IDs from file content supporting multiple formats.
        
        Args:
            content: File content as string
            collection_name: Name for logging purposes
            
        Returns:
            List of TMDb movie IDs
        """
        lines = content.strip().split('\n')
        tmdb_ids = []
        seen_ids = set()  # Prevent duplicates
        
        for line_num, line in enumerate(lines, 1):
            line = line.strip()
            
            # Skip empty lines and comments
            if not line or line.startswith('#'):
                continue
                
            try:
                # Try to extract TMDb IDs from this line
                extracted_ids = self.parse_line(line, collection_name, line_num)
                
                # Add unique IDs only
                for tmdb_id in extracted_ids:
                    if tmdb_id not in seen_ids:
                        tmdb_ids.append(tmdb_id)
                        seen_ids.add(tmdb_id)
                        
            except Exception as e:
                logger.warning(f"Error processing line {line_num} in '{collection_name}': {line[:50]}... - {e}")
                
        return tmdb_ids
    
    def parse_line(self, line: str, collection_name: str, line_num: int) -> List[int]:
        """
        Parse a single line and extract TMDb IDs.
        
        Args:
            line: Line content
            collection_name: Collection name for logging
            line_num: Line number for logging
            
        Returns:
            List of TMDb IDs found on this line
        """
        # Remove inline comments
        if '#' in line:
            line = line.split('#')[0].strip()
            
        if not line:
            return []
            
        # 1. Check for MDBList URL
        mdblist_url_match = re.match(r'https://mdblist\.com/lists/([^/?]+)(?:/([^/?]+))?', line)
        if mdblist_url_match:
            return self.process_mdblist_url(line, collection_name)
            
        # 2. Check for direct TMDb ID (just a number)
        if line.isdigit():
            tmdb_id = int(line)
            if self.validate_tmdb_id(tmdb_id):
                return [tmdb_id]
            else:
                logger.warning(f"Invalid TMDb ID {tmdb_id} in '{collection_name}' line {line_num}")
                return []
                
        # 3. Treat as movie title - search TMDb
        return self.search_movie_by_title(line, collection_name, line_num)
    
    def process_mdblist_url(self, url: str, collection_name: str) -> List[int]:
        """
        Process an MDBList URL and extract TMDb IDs.
        
        Args:
            url: MDBList URL
            collection_name: Collection name for logging
            
        Returns:
            List of TMDb IDs from the MDBList
        """
        if not self.mdblist_client:
            logger.error(f"MDBList client not available for processing URL in '{collection_name}'")
            return []
            
        try:
            logger.info(f"Fetching MDBList: {url}")
            mdblist_items = self.mdblist_client.get_list_items(url)
            tmdb_ids = self.mdblist_client.extract_tmdb_ids(mdblist_items, 'movie')
            logger.info(f"Found {len(tmdb_ids)} movies from MDBList: {url}")
            return tmdb_ids
        except Exception as e:
            logger.error(f"Failed to process MDBList URL {url}: {e}")
            return []
    
    def search_movie_by_title(self, title: str, collection_name: str, line_num: int) -> List[int]:
        """
        Search for a movie by title using TMDb.
        
        Args:
            title: Movie title to search
            collection_name: Collection name for logging
            line_num: Line number for logging
            
        Returns:
            List containing the TMDb ID if found, empty list otherwise
        """
        try:
            logger.debug(f"Searching TMDb for movie title: '{title}'")
            search_results = self.tmdb_client.search_movies(title)
            
            if search_results and len(search_results) > 0:
                # Take the first (most relevant) result
                movie = search_results[0]
                tmdb_id = movie['id']
                movie_title = movie.get('title', 'Unknown')
                year = movie.get('release_date', '')[:4] if movie.get('release_date') else ''
                logger.info(f"Found movie: '{title}' -> '{movie_title}' ({year}) [TMDb ID: {tmdb_id}]")
                return [tmdb_id]
            else:
                logger.warning(f"No TMDb results found for title: '{title}' in '{collection_name}' line {line_num}")
                return []
                
        except Exception as e:
            logger.error(f"Error searching for movie '{title}': {e}")
            return []
    
    def validate_tmdb_id(self, tmdb_id: int) -> bool:
        """
        Validate that a TMDb ID exists by checking the movie details.
        
        Args:
            tmdb_id: TMDb movie ID
            
        Returns:
            True if the ID is valid, False otherwise
        """
        try:
            movie_details = self.tmdb_client.get_movie_details(tmdb_id)
            return movie_details is not None
        except Exception:
            return False