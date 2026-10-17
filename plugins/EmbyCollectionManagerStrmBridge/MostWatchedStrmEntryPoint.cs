using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Entities.Movies;
using MediaBrowser.Controller.Entities.TV;
using MediaBrowser.Controller.Library;
using MediaBrowser.Controller.Persistence;
using MediaBrowser.Controller.Plugins;
using MediaBrowser.Controller.Providers;
using MediaBrowser.Model.Entities;
using MediaBrowser.Model.IO;
using MediaBrowser.Model.Logging;
using MediaBrowser.Model.Tasks;

namespace EmbyCollectionManagerStrmBridge
{
    public sealed class MostWatchedStrmEntryPoint : IServerEntryPoint
    {
        private const string ManifestName = ".most-watched-manifest.json";
        private readonly ILibraryManager _libraryManager;
        private readonly IProviderManager _providerManager;
        private readonly IItemRepository _itemRepository;
        private readonly IMediaSourceManager _mediaSourceManager;
        private readonly IFileSystem _fileSystem;
        private readonly ILogger _logger;
        private readonly object _gate = new object();
        private readonly HashSet<long> _inFlight = new HashSet<long>();
        private readonly HashSet<long> _probeQueued = new HashSet<long>();

        public MostWatchedStrmEntryPoint(
            ILibraryManager libraryManager,
            IProviderManager providerManager,
            IItemRepository itemRepository,
            IMediaSourceManager mediaSourceManager,
            IFileSystem fileSystem,
            ILogManager logManager)
        {
            _libraryManager = libraryManager;
            _providerManager = providerManager;
            _itemRepository = itemRepository;
            _mediaSourceManager = mediaSourceManager;
            _fileSystem = fileSystem;
            _logger = logManager.GetLogger("EmbyCollectionManagerStrmBridge");
        }

        public void Run()
        {
            _libraryManager.ItemAdded += OnItemChanged;
            _libraryManager.ItemUpdated += OnItemChanged;
        }

        private void OnItemChanged(object sender, ItemChangeEventArgs args)
        {
            var item = args.Item;
            if (item == null || !IsSupportedCopy(item) || !TryFindLibraryRoot(item.Path, out _))
                return;

            lock (_gate)
            {
                if (!_inFlight.Add(item.InternalId))
                    return;
            }

            Task.Run(() => ProcessItem(item.InternalId));
        }

        private void ProcessItem(long internalId)
        {
            try
            {
                var item = _libraryManager.GetItemById(internalId);
                if (item == null || string.IsNullOrEmpty(item.Path) || !TryFindLibraryRoot(item.Path, out var libraryRoot))
                    return;

                var needsProbe = item is Movie &&
                                 ((item.RunTimeTicks ?? 0) <= 0 || !HasMediaStreams(item));
                var changed = SetRankAndImages(item, libraryRoot);
                var sourceFound = false;
                if (item is Movie || item is Episode)
                    changed |= SyncSourceVideoInfo(item, out sourceFound);
                if (item is Episode && (!sourceFound || !HasMediaStreams(item)))
                    needsProbe = true;

                if (changed)
                    _libraryManager.UpdateItem(item, item.GetParent(), ItemUpdateType.MetadataEdit, null);

                if (needsProbe)
                    QueueMediaProbe(item);
            }
            catch (Exception ex)
            {
                _logger.Warn("[Most Watched] Could not finish STRM metadata for item " + internalId + ": " + ex.Message);
            }
            finally
            {
                lock (_gate)
                    _inFlight.Remove(internalId);
            }
        }

        private bool SyncSourceVideoInfo(BaseItem copy, out bool sourceFound)
        {
            sourceFound = false;
            var targetPath = ReadStrmTarget(copy.Path);
            if (string.IsNullOrEmpty(targetPath) || !File.Exists(targetPath))
                return false;

            var original = _libraryManager.FindByPath(targetPath, false);
            if (original == null || original.InternalId == copy.InternalId || IsManagerStrmCopy(original))
                return false;
            sourceFound = true;

            var changed = false;
            if (!string.IsNullOrEmpty(original.Container) && copy.Container != original.Container)
            {
                copy.Container = original.Container;
                changed = true;
            }
            if (copy.Width != original.Width)
            {
                copy.Width = original.Width;
                changed = true;
            }
            if (copy.Height != original.Height)
            {
                copy.Height = original.Height;
                changed = true;
            }
            if (copy.TotalBitrate != original.TotalBitrate)
            {
                copy.TotalBitrate = original.TotalBitrate;
                changed = true;
            }
            if (copy.Size != original.Size)
            {
                copy.Size = original.Size;
                changed = true;
            }
            if (original.RunTimeTicks.HasValue && copy.RunTimeTicks != original.RunTimeTicks)
            {
                copy.RunTimeTicks = original.RunTimeTicks;
                changed = true;
            }

            var sourceStreams = _mediaSourceManager.GetMediaStreams(original) ?? new List<MediaStream>();
            var copyStreams = _mediaSourceManager.GetMediaStreams(copy) ?? new List<MediaStream>();
            if (sourceStreams.Count > 0 && StreamSignature(sourceStreams) != StreamSignature(copyStreams))
            {
                _itemRepository.SaveMediaStreams(copy.InternalId, sourceStreams, CancellationToken.None);
                changed = true;
            }

            return changed;
        }

        private void QueueMediaProbe(BaseItem item)
        {
            if (string.IsNullOrEmpty(item.Path) ||
                !item.Path.EndsWith(".strm", StringComparison.OrdinalIgnoreCase))
                return;
            lock (_gate)
            {
                if (!_probeQueued.Add(item.InternalId))
                    return;
            }

            try
            {
                _providerManager.QueueRefresh(item.InternalId, new MetadataRefreshOptions(_fileSystem)
                {
                    MetadataRefreshMode = MetadataRefreshMode.FullRefresh,
                    ImageRefreshMode = MetadataRefreshMode.ValidationOnly,
                    ReplaceAllImages = false,
                    ForceSave = true,
                    EnableRemoteContentProbe = true
                }, RefreshPriority.High);
                _logger.Info("[Most Watched] Queued Emby probe for " + item.Name);
            }
            catch (Exception ex)
            {
                _logger.Warn("[Most Watched] Could not queue Emby probe for " + item.Name + ": " + ex.Message);
            }
        }

        private bool HasMediaStreams(BaseItem item)
        {
            try
            {
                return (_mediaSourceManager.GetMediaStreams(item) ?? new List<MediaStream>()).Count > 0;
            }
            catch
            {
                return false;
            }
        }

        private bool SetRankAndImages(BaseItem item, string libraryRoot)
        {
            var rank = GetRankPrefix(item.Path, libraryRoot);
            var changed = false;
            if (!string.IsNullOrEmpty(rank) && (item is Movie || item is Series) && item.SortName != rank)
            {
                item.SortName = rank;
                changed = true;
            }

            if (item is Movie)
            {
                var directory = Path.GetDirectoryName(item.Path);
                var baseName = Path.GetFileNameWithoutExtension(item.Path);
                if (!string.IsNullOrEmpty(directory))
                {
                    changed |= SetLocalImage(item, ImageType.Primary, Path.Combine(directory, baseName + ".jpg"));
                    changed |= SetLocalImage(item, ImageType.Thumb, Path.Combine(directory, baseName + "-thumb.jpg"));
                }
            }
            else if (item is Series)
            {
                changed |= SetLocalImage(item, ImageType.Primary, Path.Combine(item.Path, "poster.jpg"));
                changed |= SetLocalImage(item, ImageType.Thumb, Path.Combine(item.Path, "landscape.jpg"));
            }

            return changed;
        }

        private static bool SetLocalImage(BaseItem item, ImageType imageType, string imagePath)
        {
            if (!File.Exists(imagePath))
                return false;

            var images = (item.ImageInfos ?? Array.Empty<ItemImageInfo>()).ToList();
            var current = images.Where(image => image.Type == imageType).ToList();
            var modified = File.GetLastWriteTimeUtc(imagePath);
            if (current.Count == 1 &&
                string.Equals(current[0].Path, imagePath, StringComparison.OrdinalIgnoreCase) &&
                current[0].DateModified == modified)
                return false;

            images.RemoveAll(image => image.Type == imageType);
            images.Add(new ItemImageInfo
            {
                Path = imagePath,
                Type = imageType,
                DateModified = modified
            });
            item.ImageInfos = images.ToArray();
            return true;
        }

        private static string GetRankPrefix(string itemPath, string libraryRoot)
        {
            var relative = itemPath.Substring(libraryRoot.TrimEnd(Path.DirectorySeparatorChar).Length)
                .TrimStart(Path.DirectorySeparatorChar);
            if (string.IsNullOrEmpty(relative))
                return string.Empty;
            var first = relative.Split(Path.DirectorySeparatorChar)[0];
            var separator = first.IndexOf(" - ", StringComparison.Ordinal);
            if (separator <= 0)
                return string.Empty;
            var rank = first.Substring(0, separator);
            return rank.All(char.IsDigit) ? rank : string.Empty;
        }

        private static string? ReadStrmTarget(string strmPath)
        {
            try
            {
                return File.ReadAllLines(strmPath)
                    .Select(line => line.Trim())
                    .FirstOrDefault(line => line.Length > 0 && !line.StartsWith("#", StringComparison.Ordinal));
            }
            catch
            {
                return null;
            }
        }

        private static bool IsSupportedCopy(BaseItem item)
        {
            if (item is Movie || item is Episode)
                return !string.IsNullOrEmpty(item.Path) &&
                       item.Path.EndsWith(".strm", StringComparison.OrdinalIgnoreCase);
            if (item is Series &&
                !string.IsNullOrEmpty(item.Path) &&
                TryFindLibraryRoot(item.Path, out _))
            {
                try
                {
                    return Directory.EnumerateFiles(item.Path, "*.strm", SearchOption.AllDirectories).Any();
                }
                catch
                {
                    return false;
                }
            }
            return false;
        }

        private static bool IsManagerStrmCopy(BaseItem item)
        {
            return !string.IsNullOrEmpty(item.Path) &&
                   item.Path.EndsWith(".strm", StringComparison.OrdinalIgnoreCase) &&
                   TryFindLibraryRoot(item.Path, out _);
        }

        private static bool TryFindLibraryRoot(string itemPath, out string libraryRoot)
        {
            libraryRoot = string.Empty;
            if (string.IsNullOrEmpty(itemPath))
                return false;

            var directory = Directory.Exists(itemPath)
                ? new DirectoryInfo(itemPath)
                : new DirectoryInfo(Path.GetDirectoryName(itemPath) ?? string.Empty);
            while (directory != null)
            {
                if (File.Exists(Path.Combine(directory.FullName, ManifestName)))
                {
                    libraryRoot = directory.FullName;
                    return true;
                }
                directory = directory.Parent;
            }
            return false;
        }

        private static string StreamSignature(List<MediaStream> streams)
        {
            return string.Join("|", streams.OrderBy(stream => stream.Index).Select(stream =>
                $"{stream.Index}:{stream.Type}:{stream.Codec}:{stream.Language}:{stream.Width}x{stream.Height}:{stream.Channels}:{stream.IsExternal}:{stream.Path}"));
        }

        public void Dispose()
        {
            _libraryManager.ItemAdded -= OnItemChanged;
            _libraryManager.ItemUpdated -= OnItemChanged;
        }
    }
}
