# Emby Collection Manager STRM Bridge

The Emby-side bridge handles Manager-owned Most Watched STRM and symlink exports. It applies rank Sort Names and local images to distinct indexed copies, copies source runtime/stream metadata to STRM episodes, and queues Emby's remote-content probe for STRM movies. It also creates a dedicated per-user HomeScreen section for each active Most Watched output library.

The HomeScreen section defaults to `SortName` ascending. In the plugin's **Most Watched HomeScreen** page, select users and choose `Primary`/Poster or `Thumb`/Landscape per media type. Enable the corresponding generated image file on the Manager's `/most-watched` page as well. The Manager's output-library selector determines which existing library is scanned; the bridge discovers its active `.most-watched-manifest.json`. It excludes only that target library from the selected users' other HomeScreen sections, preserving those sections' sort settings and all other libraries.

For symlink output, Emby must index a distinct item under the manager-owned path. If Emby resolves it back to the original item, the bridge deliberately skips modifying its Sort Name or image references to avoid changing source-library metadata/artwork. `.strm` remains the reliable isolated-copy mode in that case. The plugin never modifies source media files.

The prebuilt DLL is in `dist/EmbyCollectionManagerStrmBridge.dll` and is built against Emby 4.10.1.0 reference assemblies. The plugin loads on the accessible 4.10.1.0 server; HomeScreen behavior and playback still need end-to-end testing with populated Most Watched libraries.

Build against the target Emby server's `system` directory:

```bash
dotnet build EmbyCollectionManagerStrmBridge.csproj -c Release \
  -p:EmbyServerPath=/path/to/Emby/system
```

The resulting DLL is in `bin/Release/netstandard2.0/`. Install it in Emby's plugin directory and restart Emby to load it. The source uses Emby's in-process plugin APIs because the public REST refresh endpoint does not expose the remote-content-probe option used for `.strm` playback.
