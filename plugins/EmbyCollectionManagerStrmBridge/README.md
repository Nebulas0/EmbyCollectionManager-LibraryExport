# Emby Collection Manager STRM Bridge

This lightweight Emby plugin applies rank names and the generated local poster/thumb images to manager-owned Most Watched `.strm` copies. It also copies an indexed source item's runtime and media streams to episode copies, and queues Emby's remote-content probe for movie `.strm` items. Symlink-only exports are ignored.

It handles Movie/Episode `.strm` items and Series folders containing `.strm` episodes beneath a library root containing `.most-watched-manifest.json`. It does not modify source media files.

Build against the target Emby server's `system` directory:

```bash
dotnet build EmbyCollectionManagerStrmBridge.csproj -c Release \
  -p:EmbyServerPath=/path/to/Emby/system
```

The resulting DLL is in `bin/Release/netstandard2.0/`. Install it in Emby's plugin directory and restart Emby only when explicitly approved. The source uses Emby's in-process plugin APIs because the public REST refresh endpoint does not expose the remote-content-probe option used for `.strm` playback.
