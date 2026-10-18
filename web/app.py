"""
Web UI for Emby Collection Manager
Provides a Flask-based web interface to manage all configuration,
toggle collection recipes, select libraries, and trigger syncs.
"""

import os
import sys
import json
import yaml
import threading
import time
import logging
import re
from datetime import datetime
from pathlib import Path
from flask import Flask, Response, render_template, request, jsonify, redirect, url_for, send_file

# Add parent directory to path so we can import src modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config_loader import ConfigLoader
from src.collection_recipes import RECIPES, CATEGORY_CONFIG
from src.library_export import (
    DEFAULT_MEDIA_ROOT,
    DEFAULT_OUTPUT_ROOT,
    MANIFEST_NAME,
    MEDIA_MOUNT_ROOTS,
    OUTPUT_MOUNT_ROOT,
    CollectionLibraryExporter,
    validate_library_export_paths,
)

logger = logging.getLogger("WebUI")


class _SuccessfulLogPollFilter(logging.Filter):
    def filter(self, record):
        if record.name != 'werkzeug':
            return True
        message = record.getMessage()
        return not ('"GET /api/logs' in message and '" 200 ' in message)


# Provider hooks set by main.py
_log_provider = None
_sync_function = None
_next_sync_provider = None

def set_log_provider(fn):
    global _log_provider
    _log_provider = fn

def set_sync_functions(sync_fn, next_sync_fn):
    global _sync_function, _next_sync_provider
    _sync_function = sync_fn
    _next_sync_provider = next_sync_fn

app = Flask(__name__)

# Paths
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, 'config', 'config.yaml')
STATE_PATH = os.path.join(BASE_DIR, 'config', 'webui_state.json')
MOST_WATCHED_STATE_PATH = os.path.join(BASE_DIR, 'config', 'most_watched_results.json')

# Sync state tracking
sync_state = {
    'running': False,
    'last_run': None,
    'last_status': None,
    'last_error': None,
    'thread': None
}


def load_config():
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        logger.error(f"Failed to load config: {e}")
        return {}


def save_config(config):
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            yaml.safe_dump(config, f, default_flow_style=False, sort_keys=False)
        return True
    except Exception as e:
        logger.error(f"Failed to save config: {e}")
        return False


def load_state():
    try:
        with open(STATE_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        with open(STATE_PATH, 'w', encoding='utf-8') as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save state: {e}")


def get_enabled_recipes():
    state = load_state()
    enabled = state.get('enabled_recipes', None)
    if enabled is None:
        return [r['name'] for r in RECIPES if 'name' in r]
    return enabled


def get_filtered_recipes():
    enabled_set = set(get_enabled_recipes())
    result = []
    for recipe in RECIPES:
        if 'name' not in recipe:
            continue
        r = dict(recipe)
        r['enabled'] = recipe['name'] in enabled_set
        cat_id = recipe.get('category_id', 0)
        r['category_name'] = CATEGORY_CONFIG.get(cat_id, {}).get('name', 'Unknown')
        result.append(r)
    return result


def get_emby_libraries():
    config = load_config()
    emby_cfg = config.get('emby', {})
    if not emby_cfg.get('server_url') or not emby_cfg.get('api_key'):
        return []
    try:
        from src.emby_client import EmbyClient
        emby = EmbyClient(
            server_url=emby_cfg['server_url'],
            api_key=emby_cfg['api_key'],
            user_id=emby_cfg.get('user_id', ''),
            config=config
        )
        return emby.get_libraries()
    except Exception as e:
        logger.error(f"Failed to get Emby libraries: {e}")
        return []


def run_sync_background():
    global sync_state
    sync_state['running'] = True
    sync_state['last_error'] = None
    sync_state['last_status'] = 'running'
    sync_state['last_run'] = datetime.now().isoformat()
    try:
        from src.app_logic import main as app_main
        import src.app_logic as app_logic_module
        state = load_state()
        enabled = state.get('enabled_recipes', None)
        original_recipes = app_logic_module.RECIPES
        if enabled is not None:
            app_logic_module.RECIPES = [r for r in original_recipes if r.get('name') in enabled]
        old_argv = sys.argv
        sys.argv = ['app_logic', '--config', CONFIG_PATH, '--targets', 'auto']
        try:
            app_main()
        finally:
            sys.argv = old_argv
            app_logic_module.RECIPES = original_recipes
        sync_state['last_status'] = 'success'
    except Exception as e:
        sync_state['last_status'] = 'error'
        sync_state['last_error'] = str(e)
        logger.error(f"Sync error: {e}")
    finally:
        sync_state['running'] = False


# === Routes ===

@app.route('/')
def index():
    return redirect(url_for('dashboard'))


@app.route('/dashboard')
def dashboard():
    config = load_config()
    state = load_state()
    recipes = get_filtered_recipes()
    enabled_count = sum(1 for r in recipes if r['enabled'])
    total_count = len(recipes)
    return render_template('dashboard.html', config=config, state=state,
                           sync_state=sync_state, enabled_count=enabled_count, total_count=total_count)


@app.route('/most-watched', methods=['GET', 'POST'])
def most_watched():
    from src.emby_client import EmbyClient
    from src.most_watched import (
        PlaybackReportingClient,
        load_most_watched_results,
        normalize_most_watched_settings,
        should_create_most_watched_libraries,
    )

    config = load_config()
    settings = normalize_most_watched_settings(config.get('most_watched'))
    all_libraries = get_emby_libraries()
    output_mount_root = Path(OUTPUT_MOUNT_ROOT).resolve()

    def target_library_status(library):
        locations = library.get('Locations') or []
        if len(locations) != 1 or not isinstance(locations[0], str):
            return False, 'requires exactly one folder location'
        raw_location = Path(locations[0])
        if raw_location.is_symlink():
            return False, 'folder is a symlink'
        try:
            location = raw_location.resolve()
        except (OSError, RuntimeError):
            return False, 'folder path cannot be resolved'
        for media_root in MEDIA_MOUNT_ROOTS:
            root = Path(media_root).resolve()
            try:
                location.relative_to(root)
                return False, 'folder overlaps a source media mount'
            except ValueError:
                try:
                    root.relative_to(location)
                    return False, 'folder overlaps a source media mount'
                except ValueError:
                    pass
        if (location / MANIFEST_NAME).is_file() and not (location / '.most-watched-manifest.json').is_file():
            return False, 'folder is already managed by a collection export'
        try:
            location.relative_to(output_mount_root)
            return (location != output_mount_root), (
                '' if location != output_mount_root else 'folder is the whole output mount'
            )
        except ValueError:
            return False, 'folder is outside the Manager writable output mount'

    def target_library_options(collection_type):
        options = []
        for library in all_libraries:
            if str(library.get('CollectionType', '')).lower() != collection_type:
                continue
            eligible, reason = target_library_status(library)
            option = dict(library)
            option['_target_eligible'] = eligible
            option['_target_reason'] = reason
            options.append(option)
        return options

    target_movie_libraries = target_library_options('movies')
    target_tv_libraries = target_library_options('tvshows')

    def _path_is_in_output_mount(value, root):
        try:
            path = Path(value).resolve()
            path.relative_to(root)
            return path != root
        except (OSError, RuntimeError, ValueError):
            return False

    output_library_ids = {
        str(library.get('Id'))
        for library in all_libraries
        if library.get('Name') in {'Most Watched Movies-library', 'Most Watched TV Shows-library'}
        or any(
            isinstance(location, str)
            and _path_is_in_output_mount(location, output_mount_root)
            for location in (library.get('Locations') or [])
        )
    }
    available_libraries = [
        library for library in all_libraries
        if str(library.get('Id')) not in output_library_ids
    ]
    movie_libraries = [
        library for library in available_libraries
        if str(library.get('CollectionType', '')).lower() == 'movies'
    ]
    tv_libraries = [
        library for library in available_libraries
        if str(library.get('CollectionType', '')).lower() == 'tvshows'
    ]
    saved = False
    error = None
    most_watched_auto_create = should_create_most_watched_libraries(config)

    if request.method == 'POST':
        most_watched_auto_create = request.form.get('most_watched_auto_create_emby_library') == 'on'
        submitted = {
            'movies': {
                'enabled': request.form.get('movies_enabled') == 'on',
                'days': request.form.get('movies_days', 14),
                'limit': request.form.get('movies_limit', 10),
                'library_ids': request.form.getlist('movies_library_ids'),
                'target_library_id': request.form.get('movies_target_library_id') or None,
                'output_mode': request.form.get('movies_output_mode', 'symlink'),
                'image_variants': request.form.getlist('movies_image_variants'),
                'badge_style': request.form.get('movies_badge_style', 'neutral'),
            },
            'tv_shows': {
                'enabled': request.form.get('tv_shows_enabled') == 'on',
                'days': request.form.get('tv_shows_days', 14),
                'limit': request.form.get('tv_shows_limit', 10),
                'library_ids': request.form.getlist('tv_shows_library_ids'),
                'target_library_id': request.form.get('tv_shows_target_library_id') or None,
                'output_mode': request.form.get('tv_shows_output_mode', 'symlink'),
                'image_variants': request.form.getlist('tv_shows_image_variants'),
                'badge_style': request.form.get('tv_shows_badge_style', 'neutral'),
            },
        }
        settings = normalize_most_watched_settings(submitted)
        valid_ids = {
            'movies': {
                str(library.get('Id')) for library in all_libraries
                if str(library.get('CollectionType', '')).lower() == 'movies'
                and str(library.get('Id')) not in output_library_ids
            },
            'tv_shows': {
                str(library.get('Id')) for library in all_libraries
                if str(library.get('CollectionType', '')).lower() == 'tvshows'
                and str(library.get('Id')) not in output_library_ids
            },
        }
        valid_target_ids = {
            'movies': {
                str(library.get('Id')) for library in target_movie_libraries
                if library.get('_target_eligible')
            },
            'tv_shows': {
                str(library.get('Id')) for library in target_tv_libraries
                if library.get('_target_eligible')
            },
        }
        invalid_selection = any(
            section['enabled'] and (
                not section['library_ids']
                or not set(section['library_ids']).issubset(valid_ids[kind])
            )
            for kind, section in settings.items()
        ) or any(
            section.get('target_library_id')
            and str(section['target_library_id']) not in valid_target_ids[kind]
            for kind, section in settings.items()
        ) or any(
            section.get('target_library_id')
            and str(section['target_library_id']) in set(section.get('library_ids') or [])
            for section in settings.values()
        )
        if invalid_selection:
            error = 'Choose valid source libraries and an existing output library folder inside the writable export mount.'
        else:
            most_watched_config = config.get('most_watched')
            most_watched_config = dict(most_watched_config) if isinstance(most_watched_config, dict) else {}
            most_watched_config.update(settings)
            most_watched_config['auto_create_emby_library'] = most_watched_auto_create
            config['most_watched'] = most_watched_config
            if save_config(config):
                saved = True
            else:
                error = 'Failed to save Most Watched settings.'

    emby_config = config.get('emby') or {}
    plugin_available = False
    plugin_message = 'Configure an Emby server and administrator API key to check Playback Reporting.'
    if emby_config.get('server_url') and emby_config.get('api_key'):
        try:
            emby = EmbyClient(
                server_url=emby_config['server_url'],
                api_key=emby_config['api_key'],
                user_id=emby_config.get('user_id', ''),
                config=config,
            )
            plugin_available, plugin_message = PlaybackReportingClient(emby).is_available()
        except Exception as exc:
            plugin_message = f'Could not check Playback Reporting: {exc}'

    results = load_most_watched_results(os.path.dirname(MOST_WATCHED_STATE_PATH))
    return render_template(
        'most_watched.html',
        settings=settings,
        results=results,
        movie_libraries=movie_libraries,
        tv_libraries=tv_libraries,
        target_movie_libraries=target_movie_libraries,
        target_tv_libraries=target_tv_libraries,
        has_target_movie_libraries=any(library.get('_target_eligible') for library in target_movie_libraries),
        has_target_tv_libraries=any(library.get('_target_eligible') for library in target_tv_libraries),
        plugin_available=plugin_available,
        plugin_message=plugin_message,
        auto_create_emby_library=most_watched_auto_create,
        saved=saved,
        error=error,
    )


@app.route('/settings', methods=['GET', 'POST'])
def settings():
    if request.method == 'POST':
        config = load_config()
        state = load_state()
        export_settings = config.get('library_exports') or {}
        media_root = request.form.get(
            'library_export_media_root',
            export_settings.get('media_root', DEFAULT_MEDIA_ROOT),
        ).strip()
        output_root = request.form.get(
            'library_export_output_root',
            export_settings.get('output_root', DEFAULT_OUTPUT_ROOT),
        ).strip()
        try:
            media_root, output_root = validate_library_export_paths(media_root, output_root)
        except ValueError as e:
            return render_template(
                'settings.html',
                config=config,
                state=state,
                default_library_export_media_root=DEFAULT_MEDIA_ROOT,
                default_library_export_output_root=DEFAULT_OUTPUT_ROOT,
                error=str(e),
            )
        export_settings['media_root'] = media_root
        export_settings['output_root'] = output_root
        export_settings['auto_create_emby_library'] = request.form.get('library_export_auto_create_emby_library') == 'on'
        config['library_exports'] = export_settings
        config.setdefault('tmdb', {})['api_key'] = request.form.get('tmdb_api_key', '')
        config.setdefault('emby', {})['server_url'] = request.form.get('emby_server_url', '')
        config.setdefault('emby', {})['api_key'] = request.form.get('emby_api_key', '')
        config.setdefault('emby', {})['user_id'] = request.form.get('emby_user_id', '')
        config.setdefault('trakt', {})['client_id'] = request.form.get('trakt_client_id', '')
        config.setdefault('trakt', {})['client_secret'] = request.form.get('trakt_client_secret', '')
        config.setdefault('trakt', {})['access_token'] = request.form.get('trakt_access_token', '')
        config.setdefault('trakt', {})['username'] = request.form.get('trakt_username', 'me')
        config.setdefault('mdblist', {})['api_key'] = request.form.get('mdblist_api_key', '')
        config.setdefault('traktlists', {})['enabled'] = request.form.get('traktlists_enabled') == 'on'
        config['traktlists']['directory'] = request.form.get('traktlists_directory', 'traktlists')
        max_trakt = request.form.get('traktlists_max_items', '0')
        config['traktlists']['max_items_per_collection'] = int(max_trakt) if max_trakt.isdigit() else 0
        config.setdefault('mdblists', {})['enabled'] = request.form.get('mdblists_enabled') == 'on'
        config['mdblists']['directory'] = request.form.get('mdblists_directory', 'mdblists')
        max_mdb = request.form.get('mdblists_max_items', '0')
        config['mdblists']['max_items_per_collection'] = int(max_mdb) if max_mdb.isdigit() else 0
        config.setdefault('poster_settings', {})['enable_custom_posters'] = request.form.get('poster_enable') == 'on'
        config['poster_settings']['template_name'] = request.form.get('poster_template', 'default.png')
        tp = request.form.get('poster_text_position', '0.5')
        config['poster_settings']['text_position'] = float(tp) if tp else 0.5
        config['poster_settings']['text_color'] = [
            int(request.form.get('text_color_r', 255)),
            int(request.form.get('text_color_g', 255)),
            int(request.form.get('text_color_b', 255))
        ]
        config['poster_settings']['bg_color'] = [
            int(request.form.get('bg_color_r', 0)),
            int(request.form.get('bg_color_g', 0)),
            int(request.form.get('bg_color_b', 0)),
            int(request.form.get('bg_color_a', 128))
        ]
        si = request.form.get('sync_interval', '24')
        state['sync_interval_hours'] = int(si) if si.isdigit() else 24
        save_state(state)
        if save_config(config):
            return render_template(
                'settings.html',
                config=config,
                state=state,
                default_library_export_media_root=DEFAULT_MEDIA_ROOT,
                default_library_export_output_root=DEFAULT_OUTPUT_ROOT,
                saved=True,
            )
        return render_template(
            'settings.html',
            config=config,
            state=state,
            default_library_export_media_root=DEFAULT_MEDIA_ROOT,
            default_library_export_output_root=DEFAULT_OUTPUT_ROOT,
            error="Failed to save config",
        )
    config = load_config()
    state = load_state()
    return render_template(
        'settings.html',
        config=config,
        state=state,
        default_library_export_media_root=DEFAULT_MEDIA_ROOT,
        default_library_export_output_root=DEFAULT_OUTPUT_ROOT,
    )


@app.route('/libraries', methods=['GET', 'POST'])
def libraries():
    if request.method == 'POST':
        selected = request.form.getlist('library_ids')
        config = load_config()
        config.setdefault('emby', {})['library_ids'] = selected
        save_config(config)
        libs = get_emby_libraries()
        return render_template('libraries.html', libraries=libs, selected=selected, saved=True)
    config = load_config()
    selected = config.get('emby', {}).get('library_ids', [])
    libs = get_emby_libraries()
    return render_template('libraries.html', libraries=libs, selected=selected)


@app.route('/collections')
def collections():
    recipes = get_filtered_recipes()
    categories = {}
    for r in recipes:
        cat_id = r.get('category_id', 0)
        cat_name = r.get('category_name', 'Unknown')
        if cat_id not in categories:
            categories[cat_id] = {'name': cat_name, 'recipes': []}
        categories[cat_id]['recipes'].append(r)
    categories = sorted(categories.items())
    return render_template('collections.html', categories=categories)


@app.route('/api/toggle_recipe', methods=['POST'])
def toggle_recipe():
    data = request.json
    name = data.get('name')
    enabled = data.get('enabled')
    if not name:
        return jsonify({'error': 'No recipe name'}), 400
    state = load_state()
    cur = set(get_enabled_recipes())
    if enabled:
        cur.add(name)
    else:
        cur.discard(name)
    state['enabled_recipes'] = list(cur)
    save_state(state)
    return jsonify({'success': True, 'enabled_count': len(cur)})


@app.route('/api/toggle_all', methods=['POST'])
def toggle_all():
    data = request.json
    enable = data.get('enable', True)
    state = load_state()
    if enable:
        state['enabled_recipes'] = [r['name'] for r in RECIPES if 'name' in r]
    else:
        state['enabled_recipes'] = []
    save_state(state)
    return jsonify({'success': True, 'enabled_count': len(state['enabled_recipes'])})


@app.route('/api/toggle_category', methods=['POST'])
def toggle_category():
    data = request.json
    cat_id = data.get('category_id')
    enabled = data.get('enabled')
    if cat_id is None:
        return jsonify({'error': 'No category_id'}), 400
    state = load_state()
    cur = set(get_enabled_recipes())
    for r in RECIPES:
        if r.get('category_id') == cat_id and 'name' in r:
            if enabled:
                cur.add(r['name'])
            else:
                cur.discard(r['name'])
    state['enabled_recipes'] = list(cur)
    save_state(state)
    return jsonify({'success': True, 'enabled_count': len(cur)})


@app.route('/api/sync', methods=['POST'])
def trigger_sync():
    if sync_state['running']:
        return jsonify({'error': 'Sync already running'}), 409
    thread = threading.Thread(target=run_sync_background, daemon=True)
    sync_state['thread'] = thread
    thread.start()
    return jsonify({'success': True, 'message': 'Sync started'})


@app.route('/api/sync_status')
def sync_status():
    return jsonify({
        'running': sync_state['running'],
        'last_run': sync_state['last_run'],
        'last_status': sync_state['last_status'],
        'last_error': sync_state['last_error']
    })


@app.route('/api/test_emby', methods=['POST'])
def test_emby():
    data = request.json
    try:
        from src.emby_client import EmbyClient
        emby = EmbyClient(server_url=data.get('server_url', ''), api_key=data.get('api_key', ''), user_id=data.get('user_id', ''), config={})
        libs = emby.get_libraries()
        return jsonify({'success': True, 'libraries': libs, 'count': len(libs)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/test_tmdb', methods=['POST'])
def test_tmdb():
    data = request.json
    try:
        from src.tmdb_client import TmdbClient
        tmdb = TmdbClient(api_key=data.get('api_key', ''))
        movies = tmdb.discover_movies({'sort_by': 'popularity.desc'}, page_limit=1)
        return jsonify({'success': True, 'movie_count': len(movies), 'sample': movies[0]['title'] if movies else None})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/test_trakt', methods=['POST'])
def test_trakt():
    data = request.json
    try:
        from src.trakt_client import TraktClient
        trakt = TraktClient(client_id=data.get('client_id', ''))
        trending = trakt.get_trending_lists(1)
        return jsonify({'success': True, 'list_count': len(trending) if trending else 0})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/test_mdblist', methods=['POST'])
def test_mdblist():
    data = request.json
    try:
        from src.mdblist_client import MDBListClient
        client = MDBListClient(api_key=data.get('api_key', ''))
        result = client._make_request('/user/')
        return jsonify({'success': True, 'data': result is not None})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


def _safe_filename(name):
    if not name:
        return None
    # Allow .txt, .yaml, .yml
    valid_exts = ('.txt', '.yaml', '.yml')
    if not any(name.endswith(ext) for ext in valid_exts):
        return None
    if not re.match(r'^[\w\-\s]+\.(txt|yaml|yml)$', name):
        return None
    return name


def _list_file_settings(filename, content):
    parsed = None
    if filename.endswith(('.yaml', '.yml')):
        try:
            parsed = yaml.safe_load(content)
        except yaml.YAMLError:
            parsed = None
    elif content.strip().startswith('---') and '\n---\n' in content:
        try:
            frontmatter = content.split('\n---\n', 1)[0].strip().lstrip('-').strip()
            parsed = yaml.safe_load(frontmatter)
        except yaml.YAMLError:
            parsed = None
    if not isinstance(parsed, dict):
        return {}
    supported = {
        'collection_name',
        'library_ids',
        'item_limit',
        'poster_url',
        'backdrop_url',
        'category_id',
        'create_library',
        'auto_create_emby_library',
        'seasonal',
    }
    return {key: parsed[key] for key in supported if key in parsed}


@app.route('/api/list_editor')
def list_editor_data():
    list_type = request.args.get('type', '')
    filename = _safe_filename(request.args.get('filename', ''))
    if list_type not in {'traktlists', 'mdblists'} or not filename:
        return jsonify({'error': 'Invalid list type or filename'}), 400
    config = load_config()
    directory = config.get(list_type, {}).get('directory', list_type)
    file_path = os.path.join(BASE_DIR, directory, filename)
    try:
        with open(file_path, 'r', encoding='utf-8') as source:
            content = source.read()
    except OSError:
        return jsonify({'error': 'List file not found'}), 404

    from src.list_metadata import ListMetadataManager
    metadata = ListMetadataManager(BASE_DIR).get_list_config(list_type, filename)
    effective = _list_file_settings(filename, content)
    if isinstance(metadata, dict):
        effective.update(metadata)

    from src.seasonal import normalize_seasonal_rule, seasonal_defaults_for_recipe
    collection_name = effective.get('collection_name') or Path(filename).stem
    try:
        effective['seasonal'] = normalize_seasonal_rule(
            collection_name,
            effective.get('seasonal'),
        )
    except ValueError:
        effective['seasonal'] = normalize_seasonal_rule(collection_name, None)

    return jsonify({
        'content': content,
        'config': effective,
        'seasonal_defaults': seasonal_defaults_for_recipe(collection_name),
    })


@app.route('/traktlists')
def traktlists():
    config = load_config()
    d = config.get('traktlists', {}).get('directory', 'traktlists')
    p = os.path.join(BASE_DIR, d)
    files = []
    if os.path.isdir(p):
        for f in sorted(os.listdir(p)):
            if f.endswith(('.txt', '.yaml', '.yml')):
                with open(os.path.join(p, f), 'r', encoding='utf-8') as fh:
                    c = fh.read()
                files.append({'name': f, 'content': c, 'lines': len(c.splitlines())})
    return render_template(
        'list_manager.html',
        files=files,
        directory=d,
        list_type='traktlists',
        list_label='Trakt',
    )


@app.route('/traktlists/save', methods=['POST'])
def save_traktlist():
    data = request.json
    fn = _safe_filename(data.get('name', ''))
    if not fn:
        return jsonify({'error': 'Invalid filename'}), 400
    config = load_config()
    d = config.get('traktlists', {}).get('directory', 'traktlists')
    p = os.path.join(BASE_DIR, d)
    os.makedirs(p, exist_ok=True)
    with open(os.path.join(p, fn), 'w', encoding='utf-8') as f:
        f.write(data.get('content', ''))
    return jsonify({'success': True})


@app.route('/traktlists/delete', methods=['POST'])
def delete_traktlist():
    data = request.json
    fn = _safe_filename(data.get('name', ''))
    if not fn:
        return jsonify({'error': 'Invalid filename'}), 400
    config = load_config()
    d = config.get('traktlists', {}).get('directory', 'traktlists')
    p = os.path.join(BASE_DIR, d)
    fp = os.path.join(p, fn)
    if os.path.exists(fp):
        try:
            with open(fp, 'r', encoding='utf-8') as f:
                file_content = f.read()
        except OSError:
            file_content = ''
        os.remove(fp)
        _remove_list_library_export('traktlists', fn, file_content)
        return jsonify({'success': True})
    return jsonify({'error': 'File not found'}), 404


@app.route('/mdblists')
def mdblists():
    config = load_config()
    d = config.get('mdblists', {}).get('directory', 'mdblists')
    p = os.path.join(BASE_DIR, d)
    files = []
    if os.path.isdir(p):
        for f in sorted(os.listdir(p)):
            if f.endswith(('.txt', '.yaml', '.yml')):
                with open(os.path.join(p, f), 'r', encoding='utf-8') as fh:
                    c = fh.read()
                files.append({'name': f, 'content': c, 'lines': len(c.splitlines())})
    return render_template(
        'list_manager.html',
        files=files,
        directory=d,
        list_type='mdblists',
        list_label='MDBList',
    )


@app.route('/mdblists/save', methods=['POST'])
def save_mdblist():
    data = request.json
    fn = _safe_filename(data.get('name', ''))
    if not fn:
        return jsonify({'error': 'Invalid filename'}), 400
    config = load_config()
    d = config.get('mdblists', {}).get('directory', 'mdblists')
    p = os.path.join(BASE_DIR, d)
    os.makedirs(p, exist_ok=True)
    with open(os.path.join(p, fn), 'w', encoding='utf-8') as f:
        f.write(data.get('content', ''))
    return jsonify({'success': True})


@app.route('/mdblists/delete', methods=['POST'])
def delete_mdblist():
    data = request.json
    fn = _safe_filename(data.get('name', ''))
    if not fn:
        return jsonify({'error': 'Invalid filename'}), 400
    config = load_config()
    d = config.get('mdblists', {}).get('directory', 'mdblists')
    p = os.path.join(BASE_DIR, d)
    fp = os.path.join(p, fn)
    if os.path.exists(fp):
        try:
            with open(fp, 'r', encoding='utf-8') as f:
                file_content = f.read()
        except OSError:
            file_content = ''
        os.remove(fp)
        _remove_list_library_export('mdblists', fn, file_content)
        return jsonify({'success': True})
    return jsonify({'error': 'File not found'}), 404




# === Recipe Detail / Override / Duplicate ===

@app.route('/api/recipe_detail/<path:recipe_name>')
def recipe_detail(recipe_name):
    """Get full details of a recipe including override config."""
    from src.collection_recipes import RECIPES, CATEGORY_CONFIG
    from src.seasonal import seasonal_defaults_for_recipe
    recipe = None
    for r in RECIPES:
        if r.get('name') == recipe_name:
            recipe = r
            break
    if not recipe:
        return jsonify({'error': 'Recipe not found'}), 404
    
    # Get override
    from src.recipe_override import RecipeOverrideManager
    mgr = RecipeOverrideManager(BASE_DIR)
    override = mgr.get_override(recipe_name)
    
    # Get category name
    cat_id = recipe.get('category_id', 0)
    cat_name = CATEGORY_CONFIG.get(cat_id, {}).get('name', 'Unknown')
    
    # Build detail dict (exclude non-serializable stuff)
    detail = {
        'name': recipe.get('name'),
        'source_type': recipe.get('source_type'),
        'category_id': cat_id,
        'category_name': cat_name,
        'item_limit': recipe.get('item_limit'),
        'tmdb_discover_params': recipe.get('tmdb_discover_params'),
        'tmdb_collection_id': recipe.get('tmdb_collection_id'),
        'trakt_list_params': recipe.get('trakt_list_params'),
        'sort_by': recipe.get('sort_by'),
        'seasonal_defaults': seasonal_defaults_for_recipe(recipe_name),
        'override': override,
    }
    return jsonify(detail)


@app.route('/api/recipe_override', methods=['POST'])
def save_recipe_override():
    """Save override config for a recipe."""
    data = request.json
    recipe_name = data.get('recipe_name')
    override = data.get('override', {})
    if not recipe_name:
        return jsonify({'error': 'No recipe_name'}), 400
    if not isinstance(override, dict):
        return jsonify({'error': 'Override must be an object'}), 400
    if 'auto_create_emby_library' in override:
        from src.list_metadata import parse_optional_bool
        raw_value = override.get('auto_create_emby_library')
        parsed_value = parse_optional_bool(raw_value)
        if raw_value is not None and parsed_value is None:
            return jsonify({'error': 'Automatic library creation must be true, false, or inherited'}), 400
        override['auto_create_emby_library'] = parsed_value
    from src.seasonal import normalize_seasonal_rule
    try:
        override['seasonal'] = normalize_seasonal_rule(
            recipe_name,
            override.get('seasonal'),
        )
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    from src.recipe_override import RecipeOverrideManager
    mgr = RecipeOverrideManager(BASE_DIR)
    mgr.set_override(recipe_name, override)
    return jsonify({'success': True})


@app.route('/api/recipe_override_delete', methods=['POST'])
def delete_recipe_override():
    """Delete override for a recipe."""
    data = request.json
    recipe_name = data.get('recipe_name')
    if not recipe_name:
        return jsonify({'error': 'No recipe_name'}), 400
    from src.recipe_override import RecipeOverrideManager
    mgr = RecipeOverrideManager(BASE_DIR)
    mgr.delete_override(recipe_name)
    return jsonify({'success': True})


@app.route('/api/recipe_duplicate', methods=['POST'])
def duplicate_recipe():
    """Create a duplicate of a recipe."""
    data = request.json
    original_name = data.get('original_name')
    new_name = data.get('new_name')
    if not original_name or not new_name:
        return jsonify({'error': 'Missing original_name or new_name'}), 400
    from src.recipe_override import RecipeOverrideManager
    from src.list_metadata import parse_optional_bool
    raw_auto_create = data.get('auto_create_emby_library')
    auto_create = parse_optional_bool(raw_auto_create)
    if raw_auto_create is not None and auto_create is None:
        return jsonify({'error': 'Automatic library creation must be true, false, or inherited'}), 400
    mgr = RecipeOverrideManager(BASE_DIR)
    duplicate = {
        'original_name': original_name,
        'new_name': new_name,
        'library_ids': data.get('library_ids', []),
        'extra_mdblist_urls': data.get('extra_mdblist_urls', []),
        'extra_trakt_urls': data.get('extra_trakt_urls', []),
        'extra_tmdb_ids': data.get('extra_tmdb_ids', []),
        'custom_poster_url': data.get('custom_poster_url'),
        'custom_backdrop_url': data.get('custom_backdrop_url'),
        'create_library': bool(data.get('create_library', False)),
        'auto_create_emby_library': auto_create,
    }
    idx = mgr.add_duplicate(duplicate)
    return jsonify({'success': True, 'index': idx})


@app.route('/api/recipe_duplicates')
def list_duplicates():
    """List all duplicate recipes."""
    from src.recipe_override import RecipeOverrideManager
    mgr = RecipeOverrideManager(BASE_DIR)
    return jsonify(mgr.get_duplicates())


@app.route('/api/recipe_duplicate_delete', methods=['POST'])
def delete_duplicate():
    """Delete a duplicate recipe by index."""
    data = request.json
    index = data.get('index')
    if index is None:
        return jsonify({'error': 'No index'}), 400
    from src.recipe_override import RecipeOverrideManager
    mgr = RecipeOverrideManager(BASE_DIR)
    mgr.delete_duplicate(int(index))
    return jsonify({'success': True})


# === Artwork Management ===

@app.route('/artwork')
def artwork():
    """Show all Emby collections with artwork management."""
    config = load_config()
    emby_cfg = config.get('emby', {})
    if not emby_cfg.get('server_url') or not emby_cfg.get('api_key'):
        return render_template('artwork.html', collections=[], error="Emby not configured")
    try:
        from src.emby_client import EmbyClient
        emby = EmbyClient(
            server_url=emby_cfg['server_url'],
            api_key=emby_cfg['api_key'],
            user_id=emby_cfg.get('user_id', ''),
            config=config
        )
        collections = emby.get_all_collections()
        # Proxy images through this app so browsers do not need Emby's internal URL or API key.
        for col in collections:
            col_id = col.get('Id', '')
            col['poster_url'] = url_for(
                'collection_image_proxy',
                collection_id=col_id,
                image_type='Primary',
            )
            col['backdrop_url'] = url_for(
                'collection_image_proxy',
                collection_id=col_id,
                image_type='Backdrop',
            )
        return render_template('artwork.html', collections=collections, error=None)
    except Exception as e:
        return render_template('artwork.html', collections=[], error=str(e))


@app.route('/api/collection_image/<collection_id>/<image_type>')
def collection_image_proxy(collection_id, image_type):
    """Serve Emby artwork through the manager so browser clients can use internal Emby URLs."""
    if not re.fullmatch(r'[A-Za-z0-9-]{1,64}', collection_id or ''):
        return Response(status=400)
    if image_type not in {'Primary', 'Backdrop'}:
        return Response(status=400)

    config = load_config()
    emby_cfg = config.get('emby', {})
    if not emby_cfg.get('server_url') or not emby_cfg.get('api_key'):
        return Response(status=503)
    try:
        from src.emby_client import EmbyClient
        emby = EmbyClient(
            server_url=emby_cfg['server_url'],
            api_key=emby_cfg['api_key'],
            user_id=emby_cfg.get('user_id', ''),
            config=config,
        )
        response = emby.session.get(
            f"{emby.server_url}/Items/{collection_id}/Images/{image_type}",
            timeout=20,
        )
        if response.status_code != 200 or not response.content:
            return Response(status=404 if response.status_code == 404 else 502)
        content_type = response.headers.get('Content-Type', 'image/jpeg').split(';', 1)[0]
        if not content_type.lower().startswith('image/'):
            return Response(status=502)
        result = Response(response.content, mimetype=content_type)
        result.headers['Cache-Control'] = 'private, max-age=300'
        result.headers['X-Content-Type-Options'] = 'nosniff'
        return result
    except Exception as exc:
        logger.warning("Could not proxy collection artwork for %s: %s", collection_id, exc)
        return Response(status=502)


@app.route('/api/collection_image', methods=['POST'])
def get_collection_image():
    """Get current collection image as base64 for preview."""
    data = request.json
    collection_id = data.get('collection_id')
    image_type = data.get('image_type', 'Primary')
    if not collection_id:
        return jsonify({'error': 'No collection_id'}), 400
    try:
        config = load_config()
        emby_cfg = config.get('emby', {})
        from src.emby_client import EmbyClient
        emby = EmbyClient(
            server_url=emby_cfg['server_url'],
            api_key=emby_cfg['api_key'],
            user_id=emby_cfg.get('user_id', ''),
            config=config
        )
        result = emby.get_collection_image(collection_id, image_type)
        return jsonify(result or {'has_image': False})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/upload_image', methods=['POST'])
def upload_image():
    """Upload a custom image for a collection."""
    collection_id = request.form.get('collection_id')
    image_type = request.form.get('image_type', 'Primary')
    if 'file' not in request.files or not collection_id:
        return jsonify({'error': 'No file or collection_id'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    # Validate file type
    allowed = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
    ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else ''
    if ext not in allowed:
        return jsonify({'error': f'Invalid file type. Allowed: {allowed}'}), 400
    content_types = {
        'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg',
        'gif': 'image/gif', 'webp': 'image/webp'
    }
    content_type = content_types.get(ext, 'image/jpeg')
    image_data = file.read()
    # Limit to 10MB
    if len(image_data) > 10 * 1024 * 1024:
        return jsonify({'error': 'File too large (max 10MB)'}), 400
    try:
        config = load_config()
        emby_cfg = config.get('emby', {})
        from src.emby_client import EmbyClient
        emby = EmbyClient(
            server_url=emby_cfg['server_url'],
            api_key=emby_cfg['api_key'],
            user_id=emby_cfg.get('user_id', ''),
            config=config
        )
        success = emby.upload_collection_image_from_data(collection_id, image_data, image_type, content_type)
        if success:
            return jsonify({'success': True})
        return jsonify({'error': 'Upload failed'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/reset_image', methods=['POST'])
def reset_image():
    """Reset a collection image to default (delete custom image)."""
    data = request.json
    collection_id = data.get('collection_id')
    image_type = data.get('image_type', 'Primary')
    if not collection_id:
        return jsonify({'error': 'No collection_id'}), 400
    try:
        config = load_config()
        emby_cfg = config.get('emby', {})
        from src.emby_client import EmbyClient
        emby = EmbyClient(
            server_url=emby_cfg['server_url'],
            api_key=emby_cfg['api_key'],
            user_id=emby_cfg.get('user_id', ''),
            config=config
        )
        success = emby.delete_collection_image(collection_id, image_type)
        if success:
            return jsonify({'success': True})
        return jsonify({'error': 'Reset failed'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# === Per-Collection Library Selection ===

@app.route('/api/list_config', methods=['GET', 'POST'])
def list_config():
    """Get or set per-collection config (library_ids, custom name, etc)."""
    if request.method == 'GET':
        list_type = request.args.get('type', 'traktlists')
        from src.list_metadata import ListMetadataManager
        mgr = ListMetadataManager(BASE_DIR)
        configs = mgr.get_all_configs(list_type)
        return jsonify(configs)
    else:
        data = request.json
        list_type = data.get('type')
        filename = _safe_filename(data.get('filename', ''))
        config = data.get('config', {})
        if list_type not in {'traktlists', 'mdblists'} or not filename:
            return jsonify({'error': 'Missing type or filename'}), 400
        if not isinstance(config, dict):
            return jsonify({'error': 'List config must be an object'}), 400
        if 'auto_create_emby_library' in config:
            from src.list_metadata import parse_optional_bool
            raw_value = config.get('auto_create_emby_library')
            parsed_value = parse_optional_bool(raw_value)
            if raw_value is not None and parsed_value is None:
                return jsonify({'error': 'Automatic library creation must be true, false, or inherited'}), 400
            config['auto_create_emby_library'] = parsed_value
        if config.get('item_limit') is not None:
            try:
                item_limit = int(config['item_limit'])
            except (TypeError, ValueError):
                return jsonify({'error': 'Item limit must be a number'}), 400
            if item_limit < 0 or item_limit > 10000:
                return jsonify({'error': 'Item limit must be between 0 and 10000'}), 400
            config['item_limit'] = item_limit
        if 'seasonal' in config:
            from src.seasonal import normalize_seasonal_rule
            try:
                config['seasonal'] = normalize_seasonal_rule(
                    config.get('collection_name') or Path(filename).stem,
                    config['seasonal'],
                )
            except ValueError as exc:
                return jsonify({'error': str(exc)}), 400
        from src.list_metadata import ListMetadataManager
        mgr = ListMetadataManager(BASE_DIR)
        current_config = mgr.get_list_config(list_type, filename)
        if not isinstance(current_config, dict):
            current_config = {}
        if isinstance(config, dict):
            current_config.update(config)
        mgr.set_list_config(list_type, filename, current_config)
        return jsonify({'success': True})


def _remove_list_library_export(list_type, filename, file_content=''):
    try:
        from src.emby_client import EmbyClient
        from src.list_metadata import ListMetadataManager
        config = load_config()
        metadata = ListMetadataManager(BASE_DIR)
        list_config = metadata.get_list_config(list_type, filename)
        collection_name = list_config.get('collection_name')
        if not collection_name and file_content:
            if filename.endswith(('.yaml', '.yml')):
                yaml_content = file_content
            elif file_content.strip().startswith('---') and '\n---\n' in file_content:
                yaml_content = file_content.split('\n---\n', 1)[0].strip().lstrip('-').strip()
            else:
                yaml_content = ''
            if yaml_content:
                try:
                    data = yaml.safe_load(yaml_content)
                    if isinstance(data, dict):
                        collection_name = data.get('collection_name')
                except yaml.YAMLError:
                    pass
        collection_name = collection_name or Path(filename).stem
        emby_config = config.get('emby', {})
        if emby_config.get('server_url') and emby_config.get('api_key'):
            emby = EmbyClient(
                server_url=emby_config['server_url'],
                api_key=emby_config['api_key'],
                user_id=emby_config.get('user_id', ''),
                config=config,
            )
            export_config = config.get('library_exports') or {}
            exporter = CollectionLibraryExporter(
                emby,
                media_root=export_config.get('media_root'),
                output_root=export_config.get('output_root'),
            )
            exporter.remove_collection_export(collection_name)
        metadata.delete_list_config(list_type, filename)
    except Exception as e:
        logger.warning("Could not clean library export for %s: %s", filename, e)


@app.route('/api/list_libraries')
def list_libraries():
    """Get available Emby libraries (for per-collection selection)."""
    libs = get_emby_libraries()
    return jsonify(libs)



# === Logs Viewer ===

@app.route('/logs')
def logs_page():
    return render_template('logs.html')


@app.route('/api/logs')
def api_logs():
    n = request.args.get('lines', '100', type=int)
    if _log_provider:
        return jsonify({'lines': _log_provider(n)})
    return jsonify({'lines': ''})


# === Sync History ===

@app.route('/api/sync_history')
def sync_history():
    from src.sync_history import SyncHistory
    h = SyncHistory(BASE_DIR)
    return jsonify(h.get_history(20))


@app.route('/api/sync_history_clear', methods=['POST'])
def sync_history_clear():
    from src.sync_history import SyncHistory
    h = SyncHistory(BASE_DIR)
    h.clear()
    return jsonify({'success': True})


# === Single Recipe Sync ===

@app.route('/api/sync_single', methods=['POST'])
def sync_single():
    data = request.json
    recipe_name = data.get('recipe_name')
    if not recipe_name:
        return jsonify({'error': 'No recipe_name'}), 400
    if sync_state['running']:
        return jsonify({'error': 'A sync is already running'}), 409

    def run_single():
        global sync_state
        sync_state['running'] = True
        sync_state['last_error'] = None
        sync_state['last_status'] = 'running'
        sync_state['last_run'] = datetime.now().isoformat()
        try:
            if _sync_function:
                _sync_function(single_recipe=recipe_name)
            else:
                # Fallback: use the web app's own sync
                run_sync_background()
            sync_state['last_status'] = 'success'
        except Exception as e:
            sync_state['last_status'] = 'error'
            sync_state['last_error'] = str(e)
        finally:
            sync_state['running'] = False

    thread = threading.Thread(target=run_single, daemon=True)
    thread.start()
    return jsonify({'success': True, 'message': f'Syncing {recipe_name}'})


# === Next Sync Time ===

@app.route('/api/next_sync')
def next_sync():
    if _next_sync_provider:
        t = _next_sync_provider()
        if t:
            return jsonify({'next_sync': t.isoformat(), 'next_sync_display': t.strftime('%Y-%m-%d %H:%M:%S')})
    return jsonify({'next_sync': None})


# === Export/Import Config ===

@app.route('/api/export_config')
def export_config():
    """Export all config as a single JSON file."""
    import zipfile
    import tempfile
    config = load_config()
    state = load_state()
    # Load list metadata
    list_meta = {}
    meta_path = os.path.join(BASE_DIR, 'config', 'list_metadata.json')
    try:
        with open(meta_path, 'r') as f:
            list_meta = json.load(f)
    except Exception:
        pass
    # Load recipe overrides
    recipe_ov = {}
    ov_path = os.path.join(BASE_DIR, 'config', 'recipe_overrides.json')
    try:
        with open(ov_path, 'r') as f:
            recipe_ov = json.load(f)
    except Exception:
        pass
    export = {
        'config': config,
        'state': state,
        'list_metadata': list_meta,
        'recipe_overrides': recipe_ov,
        'exported_at': datetime.now().isoformat(),
        'version': 1,
    }
    tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
    json.dump(export, tmp, indent=2)
    tmp.close()
    return send_file(tmp.name, as_attachment=True, download_name='ecm_config_backup.json')


@app.route('/api/import_config', methods=['POST'])
def import_config():
    """Import config from a JSON file."""
    if 'file' not in request.files:
        return jsonify({'error': 'No file'}), 400
    file = request.files['file']
    try:
        data = json.load(file)
        if 'config' in data:
            save_config(data['config'])
        if 'state' in data:
            save_state(data['state'])
        if 'list_metadata' in data:
            meta_path = os.path.join(BASE_DIR, 'config', 'list_metadata.json')
            with open(meta_path, 'w') as f:
                json.dump(data['list_metadata'], f, indent=2)
        if 'recipe_overrides' in data:
            ov_path = os.path.join(BASE_DIR, 'config', 'recipe_overrides.json')
            with open(ov_path, 'w') as f:
                json.dump(data['recipe_overrides'], f, indent=2)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# === Delete Emby Collection ===

@app.route('/api/delete_collection', methods=['POST'])
def delete_collection():
    """Delete a collection from Emby."""
    data = request.json
    collection_id = data.get('collection_id')
    if not collection_id:
        return jsonify({'error': 'No collection_id'}), 400
    try:
        config = load_config()
        emby_cfg = config.get('emby', {})
        from src.emby_client import EmbyClient
        emby = EmbyClient(
            server_url=emby_cfg['server_url'],
            api_key=emby_cfg['api_key'],
            user_id=emby_cfg.get('user_id', ''),
            config=config
        )
        url = f"{emby.server_url}/Items/{collection_id}?api_key={emby.api_key}"
        resp = emby.session.delete(url, timeout=15)
        if resp.status_code in [200, 204]:
            return jsonify({'success': True})
        return jsonify({'error': f'Delete failed: {resp.status_code}'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# === Collection Preview (TMDb IDs before sync) ===

@app.route('/api/preview_collection/<path:recipe_name>')
def preview_collection(recipe_name):
    """Preview what TMDb IDs a recipe would produce without syncing."""
    from src.collection_recipes import RECIPES
    recipe = None
    for r in RECIPES:
        if r.get('name') == recipe_name:
            recipe = r
            break
    if not recipe:
        return jsonify({'error': 'Recipe not found'}), 404

    config = load_config()
    source_type = recipe.get('source_type')
    item_limit = recipe.get('item_limit', 50)
    tmdb_ids = []
    movie_titles = []

    try:
        from src.tmdb_client import TmdbClient
        tmdb = TmdbClient(api_key=config.get('tmdb', {}).get('api_key', ''))

        if source_type in ('tmdb_discover', 'tmdb_discover_individual_movies'):
            params = recipe.get('tmdb_discover_params', {})
            movies = tmdb.discover_movies(params, item_limit)
            tmdb_ids = [m['id'] for m in movies]
            movie_titles = [{'id': m['id'], 'title': m.get('title', ''), 'year': m.get('release_date', '')[:4]} for m in movies]
        elif source_type in ('tmdb_collection', 'tmdb_series_collection'):
            col_id = recipe.get('tmdb_collection_id')
            if col_id:
                movies = tmdb.get_collection_movies(col_id, item_limit)
                tmdb_ids = [m['id'] for m in movies]
                movie_titles = [{'id': m['id'], 'title': m.get('title', ''), 'year': m.get('release_date', '')[:4]} for m in movies]
        else:
            return jsonify({'error': f'Preview not supported for source type: {source_type}. Use sync to test Trakt-based recipes.'})

        return jsonify({
            'recipe_name': recipe_name,
            'source_type': source_type,
            'count': len(tmdb_ids),
            'movies': movie_titles[:50],  # Limit preview to 50
            'total': len(tmdb_ids),
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# === Notifications Settings ===

@app.route('/api/notifications', methods=['GET', 'POST'])
def notifications():
    if request.method == 'POST':
        data = request.json
        config = load_config()
        config.setdefault('notifications', {})['enabled'] = data.get('enabled', False)
        config['notifications']['webhook_url'] = data.get('webhook_url', '')
        config['notifications']['notify_on_success'] = data.get('notify_on_success', True)
        config['notifications']['notify_on_error'] = data.get('notify_on_error', True)
        save_config(config)
        return jsonify({'success': True})
    else:
        config = load_config()
        return jsonify(config.get('notifications', {'enabled': False, 'webhook_url': '', 'notify_on_success': True, 'notify_on_error': True}))


def start_webui(host='0.0.0.0', port=8282):
    logging.basicConfig(level=logging.INFO)
    werkzeug_logger = logging.getLogger('werkzeug')
    if not any(isinstance(log_filter, _SuccessfulLogPollFilter) for log_filter in werkzeug_logger.filters):
        werkzeug_logger.addFilter(_SuccessfulLogPollFilter())
    print(f"Starting Web UI on http://{host}:{port}")
    app.run(host=host, port=port, debug=False, use_reloader=False)


if __name__ == '__main__':
    port = int(os.environ.get('WEBUI_PORT', '8282'))
    start_webui(port=port)
