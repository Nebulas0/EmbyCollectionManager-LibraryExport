# Emby Collection Manager — Library Export

This is a customized Emby Collection Manager based on [Nebulas0/EmbyCollectionManager](https://github.com/Nebulas0/EmbyCollectionManager). It keeps the existing collection synchronization and adds optional library exports, including Most Watched rankings.

## Features

- Keeps the existing TMDb, Trakt, and MDBList collection workflows.
- Opt individual recipe, Trakt list, or MDBList collections into a symlink-folder export.
- Choose the Emby media path root and writable export directory in Settings.
- Choose globally whether opted-in exports are automatically registered as Emby libraries. With automatic creation off, the symlink folders are still maintained for manual library setup.
- Creates movie links to the movie’s containing directory without assuming a file extension, including `.mkv`, `.mp4`, and `.strm`.
- Links a TV series at its Series folder, preserving all seasons and episodes. If a collection contains episodes or seasons, the manager resolves their Emby `SeriesId` and links the series root once.
- Reconciles symlinks on the normal sync interval and removes only links recorded in its own manifest. It never deletes the source media or automatically deletes an Emby library entry.
- Optional Most Watched libraries use Playback Reporting data, with independent lookback days and top-item limits for movies and TV shows. Choose symlinks or `.strm` pointers and select ranked poster/thumb images.

The current Trakt and MDBList importers process movie lists. The exporter can handle Series/Episode/Season items already present in an Emby collection, but does not add TV-show list ingestion.

## Requirements

- Emby with an administrator API key for automatic library creation.
- Docker and Docker Compose.
- The Emby [Playback Reporting plugin](https://github.com/faush01/playback_reporting) is required only for Most Watched libraries.
- The Emby Collection Manager container must see the media paths exactly as Emby reports them.
- A local writable output directory that is also visible to Emby at the same container path. Do not use a remote/rclone-only mount as the writable export directory.

## Saltbox deployment

Saltbox’s [your own containers](https://docs.saltbox.dev/advanced/your-own-containers/) guide recommends Docker Compose for custom containers. The [Traefik template](https://docs.saltbox.dev/reference/modules/traefik_template/) generates a Compose file with Saltbox’s Traefik labels.

1. Clone this repository to a host directory:

   ```sh
   git clone https://github.com/Nebulas0/EmbyCollectionManager-LibraryExport.git /opt/EmbyCollectionManager-LibraryExport
   cd /opt/EmbyCollectionManager-LibraryExport
   ```

2. Create local configuration files:

   ```sh
   cp .env.example .env
   cp config/config.yaml.example config/config.yaml
   ```

   Set `DOMAIN` and `EMBYCOLLECTIONMANAGER_HOST` in `.env`; enter API keys, the Emby URL, and user ID in `config/config.yaml`.

3. For a Saltbox-generated Traefik template, run this on the Saltbox host:

   ```sh
   sb install generate-traefik-template
   ```

   Enter `embycollectionmanager` as the service name, your chosen Emby Collection Manager hostname, and port `8282`. Saltbox writes the generated template to `/tmp/docker-compose.yml`. Use its Traefik labels and `saltbox` network in the checked-in `compose.yaml`; keep this app’s `build: .`, environment, and media/config/output volume mounts. The repository Compose file already includes a generic Traefik template setup, so you can use it directly after configuring `.env`.

4. Create the local output directory and ensure it is writable by the container:

   ```sh
   mkdir -p /mnt/local/embycollectionmanager-library-exports
   ```

5. Mount each Emby media root into the manager **read-only at the same absolute path Emby uses**. The example Compose file defaults to `/mnt/unionfs`. If you use multiple roots, add a read-only volume for each root and list their in-container paths in `LIBRARY_EXPORT_MEDIA_MOUNTS`, separated by colons.
6. Mount `LIBRARY_EXPORT_OUTPUT_MOUNT` read-write into the manager and ensure Emby can see that same path. If Emby does not already mount its parent path, add a matching volume to the Emby container.
7. Validate, build, and start the service:

   ```sh
   docker compose config --quiet
   docker compose build embycollectionmanager
   docker compose up -d embycollectionmanager
   ```

For a main server using `/mnt/unionfs`, set both `LIBRARY_EXPORT_MEDIA_MOUNTS` and `LIBRARY_EXPORT_DEFAULT_MEDIA_ROOT` to `/mnt/unionfs`. The manager’s bind mount must expose `/mnt/unionfs:/mnt/unionfs:ro,rslave`.

## Using library export

1. In Settings, verify **Emby media path root** and **Symlink output folder**. Media roots must be mounted read-only in the manager; the output folder must be inside the configured writable export mount.
2. Choose whether **Automatically create the Emby library for opted-in collections** is on. If off, the folder export still updates and can be added manually as an Emby library.
3. In Collections, Trakt Lists, or MDBLists, edit the desired recipe/list and enable its symlink-library export option.
4. The next sync updates the Emby collection first, then reconciles the folder and, when enabled, creates or refreshes the corresponding Emby library named `<collection name>-library`.

When items disappear from a collection, the next sync removes only the corresponding manager-owned symlinks. Turning off a collection’s export also clears its managed links on the next sync; the Emby library entry itself is left in place.

## Trakt and MDBList list editor

The Trakt Lists and MDBLists tabs share a single edit dialog for list content, collection name, per-list item limit, source libraries, symlink export, seasonal window, and poster/backdrop URLs. The standalone Libraries button is removed; choose source libraries in Edit instead. These options are stored per list and applied on the next sync. A per-list item limit overrides the global limit; `0` means unlimited and a blank value uses the global setting.

## Seasonal collections and ordering

Edit a recipe in the Collections tab and enable **Seasonal collection** to give it a recurring annual window using `MM-DD` dates. Suggested windows are Halloween `10-01`–`11-01`, Christmas `12-01`–`01-02`, Valentine's Day `02-01`–`02-15`, and Thanksgiving `11-01`–`12-01`; edit these or set a custom window on any recipe. Windows can cross New Year. Keep the recipe enabled for the scheduler to enforce the window.

Outside an enabled season, the next manager sync removes the matching Emby BoxSet collection and clears only its manifest-owned symlink exports. The manager verifies that the Emby item is a pathless BoxSet before deleting it; it does not delete movie files, other media, or any Emby library entry. Seasonal scheduling is off by default.

On each sync, the manager applies the configured user's **Emby Web** display preferences to every accessible BoxSet: collection items use Production Year descending and the outer collection list uses Sort Name ascending. Existing custom view preferences are preserved. Manager-synced memberships are also submitted in descending release-year order, using premiere date to break ties. Non-seasonal collections get a Sort Name matching their name; active seasonal recipe collections use a hidden, timestamp-based Sort Name so the most recently updated seasonal collection sorts first. Other Emby clients/users have separate display preferences and may need the same settings selected in their client.

## Most Watched libraries

The Most Watched tab reads per-item playback events from Playback Reporting's `UserPlaylist` API using the configured Emby administrator token. It filters ignored users, resolves the event item IDs through Emby, and counts only items whose paths are inside the selected source libraries. Movie plays are grouped by movie identity; episode plays are grouped by Emby `SeriesId`.

Movies and TV shows each have their own enable switch, library selection, lookback period, and item limit. Both periods default to 14 days and both limits to 10 items; exports are disabled until opted in. If no source library selection has been saved yet, all matching movie or TV libraries are selected. The normal manager sync refreshes enabled lists; the tab's manual button runs the full sync.

For Movies and TV Shows independently, choose either real media-file symlinks or HomeScreenCompanion-style `.strm` pointers. A pointer contains the original media path; it does not copy the video. TV output remains one ranked Series folder containing Season folders and episode files/pointers. The manager generates local NFOs and ranked poster and thumb/landscape images without changing source media or artwork. In STRM mode, indexed Movie and Series copies also receive a rank Sort Name update through the Emby API. Sort Name ascending is still required in the Emby client. Regular collection exports keep their existing directory-symlink behavior.

When a title is no longer in the selected libraries' ranked results, the next sync prunes only its manifest-owned links and sidecars. Original media and Emby library entries are left untouched.

The Collections editor, Trakt Lists editor, and MDBLists editor each let you choose whether that item automatically registers an Emby library, disables registration, or inherits the global Settings default. This is separate from the per-item symlink-folder export toggle: when export is enabled but registration is off, the folder is still maintained for manual library setup. Existing Emby library entries are never removed automatically. Most Watched retains its own automatic-library checkbox. These are ordinary Emby libraries; the manager does not create HomeScreenCompanion-style per-user Home Screen sections.

### STRM playback helper plugin

The optional `plugins/EmbyCollectionManagerStrmBridge` project is a lightweight Emby-side helper for `.strm` exports. It watches only Manager-owned Most Watched `.strm` items (symlink-only exports are ignored), associates the generated rank images and Sort Names, copies source episode runtime/stream metadata, and queues Emby's remote-content probe for movie `.strm` files. It does not modify source media.

The helper uses Emby in-process plugin APIs because the public REST refresh endpoint does not expose the remote-content-probe option. A prebuilt DLL is included at `plugins/EmbyCollectionManagerStrmBridge/dist/EmbyCollectionManagerStrmBridge.dll`. It was built against Emby 4.10.1.0 reference assemblies and its entry point loaded successfully on the accessible Emby 4.10.1.0 server. End-to-end STRM playback has not yet been tested because there are currently no generated Most Watched STRM items on that server.

To rebuild against another Emby version, use that server's `system` assemblies:

```bash
dotnet build plugins/EmbyCollectionManagerStrmBridge/EmbyCollectionManagerStrmBridge.csproj \
  -c Release -p:EmbyServerPath=/path/to/Emby/system
```

Copy the resulting `bin/Release/netstandard2.0/EmbyCollectionManagerStrmBridge.dll` into Emby's plugin directory and restart Emby to load it. The plugin only processes STRM-mode copies and leaves symlink-only exports alone.

## Configuration and privacy

`config/config.yaml`, list contents, UI state, recipe overrides, and sync history are runtime data and are excluded from Git. The checked-in configuration is only a placeholder template. Do not commit API keys, personal list files, exported settings, or a real server hostname.
