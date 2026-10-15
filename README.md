# Emby Collection Manager — Library Export

This is a customized Emby Collection Manager based on [Nebulas0/EmbyCollectionManager](https://github.com/Nebulas0/EmbyCollectionManager). It keeps the existing collection synchronization and adds optional, symlink-backed Emby libraries.

## Features

- Keeps the existing TMDb, Trakt, and MDBList collection workflows.
- Opt individual recipe, Trakt list, or MDBList collections into a symlink-folder export.
- Choose the Emby media path root and writable export directory in Settings.
- Choose globally whether opted-in exports are automatically registered as Emby libraries. With automatic creation off, the symlink folders are still maintained for manual library setup.
- Creates movie links to the movie’s containing directory, including for `.mkv` and `.strm` media.
- Links a TV series at its Series folder, preserving all seasons and episodes. If a collection contains episodes or seasons, the manager resolves their Emby `SeriesId` and links the series root once.
- Reconciles symlinks on the normal sync interval and removes only links recorded in its own manifest. It never deletes the source media or automatically deletes an Emby library entry.

The current Trakt and MDBList importers process movie lists. The exporter can handle Series/Episode/Season items already present in an Emby collection, but does not add TV-show list ingestion.

## Requirements

- Emby with an administrator API key for automatic library creation.
- Docker and Docker Compose.
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

## Configuration and privacy

`config/config.yaml`, list contents, UI state, recipe overrides, and sync history are runtime data and are excluded from Git. The checked-in configuration is only a placeholder template. Do not commit API keys, personal list files, exported settings, or a real server hostname.
