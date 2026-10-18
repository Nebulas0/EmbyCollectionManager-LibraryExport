# Emby Collection Manager STRM Bridge

The Emby-side bridge handles Manager-owned Most Watched STRM and symlink exports. It applies rank Sort Names and local images to distinct indexed copies, including Series folders backed by episode symlinks, copies source runtime/stream metadata to STRM episodes, and queues Emby's remote-content probe for STRM movies. It reconciles existing managed Series at startup. It adds or updates a per-user HomeScreen section for an existing Most Watched output library; it does not create or rename Emby libraries.

The HomeScreen row uses the selected library's name and items, with the configured Movie/Series type, sort order, and limit. The Manager records the limit in `.most-watched-manifest.json`; older manifests default to 10. The row uses the library view ID for navigation and the underlying folder item ID for its content query, with other library views excluded. In the plugin's **Most Watched HomeScreen** page, select the same Movies/TV library chosen in the Manager, choose specific users or **All users**, and set `Primary`/Poster or `Thumb`/Landscape per media type. The page reports whether an active Manager manifest is detected for the selected library. Enable the corresponding generated image file on the Manager's `/most-watched` page as well. Auto-detect follows the active manifest. Save displays the selected user counts and verifies the user IDs returned by Emby. **Revert unsaved changes** reloads the saved configuration without writing to Emby. The bridge excludes only that target library from the selected users' other HomeScreen sections, preserving those sections' sort settings and all other libraries.

For symlink output, Emby must index a distinct item under the manager-owned path. The bridge reconciles existing manager-owned Series at startup and attaches their generated `poster.jpg` as the Primary image; it checks the active manifest to distinguish those copies from source-library Series. For other symlink items, if Emby resolves the link back to the original item, the bridge skips modifying its Sort Name or image references. `.strm` remains the reliable isolated-copy mode in that case. The plugin never modifies source media files.

The Manager records the directories it creates in the Most Watched ownership manifest. On reconciliation it prunes only empty recorded directories. Legacy active TV manifests also permit pruning empty rank-prefixed top-level Series folders reserved by the old exporter; other untracked or non-empty directories are left alone. A targeted refresh is requested when files, links, or directories change.

The prebuilt DLL is in `dist/EmbyCollectionManagerStrmBridge.dll` and is built against Emby 4.10.1.0 reference assemblies. The plugin loads on the accessible 4.10.1.0 server; HomeScreen behavior and playback still need end-to-end testing with populated Most Watched libraries.

Build against the target Emby server's `system` directory:

```bash
dotnet build EmbyCollectionManagerStrmBridge.csproj -c Release \
  -p:EmbyServerPath=/path/to/Emby/system
```

The resulting DLL is in `bin/Release/netstandard2.0/`. Install it in Emby's plugin directory and restart Emby to load it. The source uses Emby's in-process plugin APIs because the public REST refresh endpoint does not expose the remote-content-probe option used for `.strm` playback.

Run the no-package XML persistence regression check (including a specific user ID and the `*` All-users marker) with:

```bash
dotnet run --project ../EmbyCollectionManagerStrmBridge.Tests/EmbyCollectionManagerStrmBridge.Tests.csproj \
  -c Release -p:EmbyServerPath=/path/to/Emby/system
```
