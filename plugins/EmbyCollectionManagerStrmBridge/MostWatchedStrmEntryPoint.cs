using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Runtime.Serialization;
using System.Runtime.Serialization.Json;
using System.Text;
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
using MediaBrowser.Model.Querying;
using MediaBrowser.Model.Tasks;
using MediaBrowser.Model.Users;

namespace EmbyCollectionManagerStrmBridge
{
    public sealed class MostWatchedStrmEntryPoint : IServerEntryPoint
    {
        private const string ManifestName = ".most-watched-manifest.json";
        private const string HomeSectionPrefix = "ECM Top Played - ";
        private static MostWatchedStrmEntryPoint? _instance;
        private readonly ILibraryManager _libraryManager;
        private readonly IUserManager _userManager;
        private readonly IProviderManager _providerManager;
        private readonly IItemRepository _itemRepository;
        private readonly IMediaSourceManager _mediaSourceManager;
        private readonly IFileSystem _fileSystem;
        private readonly ILogger _logger;
        private readonly object _gate = new object();
        private readonly HashSet<long> _inFlight = new HashSet<long>();
        private readonly HashSet<long> _probeQueued = new HashSet<long>();
        private bool _homeSyncInFlight;
        private DateTime _lastHomeSyncUtc = DateTime.MinValue;

        public MostWatchedStrmEntryPoint(
            ILibraryManager libraryManager,
            IUserManager userManager,
            IProviderManager providerManager,
            IItemRepository itemRepository,
            IMediaSourceManager mediaSourceManager,
            IFileSystem fileSystem,
            ILogManager logManager)
        {
            _libraryManager = libraryManager;
            _userManager = userManager;
            _providerManager = providerManager;
            _itemRepository = itemRepository;
            _mediaSourceManager = mediaSourceManager;
            _fileSystem = fileSystem;
            _logger = logManager.GetLogger("EmbyCollectionManagerStrmBridge");
        }

        public void Run()
        {
            _instance = this;
            _libraryManager.ItemAdded += OnItemChanged;
            _libraryManager.ItemUpdated += OnItemChanged;
            _libraryManager.ItemRemoved += OnItemRemoved;
            QueueHomeSectionSync(true);
            Task.Run(ReconcileManagedSeries);
        }

        public static void RequestHomeSectionSync()
        {
            _instance?.QueueHomeSectionSync(true);
        }

        private void OnItemChanged(object sender, ItemChangeEventArgs args)
        {
            var item = args.Item;
            if (item == null || !IsSupportedCopy(item) || !TryFindLibraryRoot(item.Path, out _))
                return;

            QueueItemProcessing(item.InternalId);
        }

        private void QueueItemProcessing(long internalId)
        {
            lock (_gate)
            {
                if (!_inFlight.Add(internalId))
                    return;
            }

            Task.Run(() => ProcessItem(internalId));
        }

        private void ReconcileManagedSeries()
        {
            try
            {
                foreach (var library in GetManagedLibraries().Where(entry => entry.Kind == "tv_shows"))
                {
                    if (!long.TryParse(library.FolderItemId, out var folderItemId))
                    {
                        _logger.Warn("[Most Watched] Cannot reconcile Series for " + library.Name +
                                     ": the underlying library folder item ID could not be resolved.");
                        continue;
                    }

                    var seriesItems = _libraryManager.GetItemList(
                        ManagedSeriesHelpers.BuildSeriesReconciliationQuery(folderItemId));
                    if (seriesItems == null)
                        continue;

                    foreach (var series in seriesItems.OfType<Series>())
                    {
                        if (IsSupportedCopy(series))
                            QueueItemProcessing(series.InternalId);
                    }
                }
            }
            catch (Exception ex)
            {
                _logger.Warn("[Most Watched] Could not reconcile existing managed Series: " + ex.Message);
            }
        }

        private void OnItemRemoved(object sender, ItemChangeEventArgs args)
        {
            QueueHomeSectionSync(false);
        }

        private void ProcessItem(long internalId)
        {
            try
            {
                var item = _libraryManager.GetItemById(internalId);
                if (item == null || string.IsNullOrEmpty(item.Path) || !TryFindLibraryRoot(item.Path, out var libraryRoot))
                    return;

                var isStrm = item.Path.EndsWith(".strm", StringComparison.OrdinalIgnoreCase);
                if (!isStrm && !IsDistinctSymlinkCopy(item, libraryRoot))
                {
                    _logger.Warn("[Most Watched] Skipping symlink item that Emby resolves to its source: " + item.Path);
                    return;
                }

                var needsProbe = isStrm && item is Movie &&
                                 ((item.RunTimeTicks ?? 0) <= 0 || !HasMediaStreams(item));
                var changed = SetRankAndImages(item, libraryRoot);
                var sourceFound = false;
                if (isStrm && (item is Movie || item is Episode))
                    changed |= SyncSourceVideoInfo(item, out sourceFound);
                if (isStrm && item is Episode && (!sourceFound || !HasMediaStreams(item)))
                    needsProbe = true;

                if (changed)
                    _libraryManager.UpdateItem(item, item.GetParent(), ItemUpdateType.MetadataEdit, null);

                if (needsProbe)
                    QueueMediaProbe(item);
                QueueHomeSectionSync(false);
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
                    var isStrm = item.Path.EndsWith(".strm", StringComparison.OrdinalIgnoreCase);
                    changed |= ManagedSeriesHelpers.SetLocalImage(
                        item,
                        ImageType.Primary,
                        Path.Combine(directory, isStrm ? baseName + ".jpg" : "poster.jpg"));
                    changed |= ManagedSeriesHelpers.SetLocalImage(
                        item,
                        ImageType.Thumb,
                        Path.Combine(directory, isStrm ? baseName + "-thumb.jpg" : "landscape.jpg"));
                }
            }
            else if (item is Series)
            {
                changed |= ManagedSeriesHelpers.AttachGeneratedSeriesImages((Series)item);
            }

            return changed;
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
                       TryFindLibraryRoot(item.Path, out _);
            if (item is Series &&
                !string.IsNullOrEmpty(item.Path) &&
                TryFindLibraryRoot(item.Path, out var libraryRoot) &&
                IsManagerOwnedSeriesFolder(item.Path, libraryRoot))
            {
                return true;
            }
            return false;
        }

        private bool IsDistinctSymlinkCopy(BaseItem item, string libraryRoot)
        {
            if (item is Series)
                return IsManagerOwnedSeriesFolder(item.Path, libraryRoot);

            var targetPath = ReadManagedLinkTarget(item.Path, libraryRoot);
            if (string.IsNullOrEmpty(targetPath))
                return false;

            var source = _libraryManager.FindByPath(targetPath, false);
            return source == null || source.InternalId != item.InternalId;
        }

        private static bool IsManagerOwnedSeriesFolder(string seriesPath, string libraryRoot)
        {
            var manifest = ReadManagedManifest(Path.Combine(libraryRoot, ManifestName));
            return ManagedSeriesHelpers.IsManagerOwnedSeriesFolder(
                seriesPath,
                libraryRoot,
                manifest?.Links?.Keys ?? Enumerable.Empty<string>(),
                manifest?.Files?.Keys ?? Enumerable.Empty<string>());
        }

        private static string ReadManagedLinkTarget(string itemPath, string libraryRoot)
        {
            try
            {
                var root = Path.GetFullPath(libraryRoot).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
                var fullPath = Path.GetFullPath(itemPath);
                if (!fullPath.StartsWith(root, StringComparison.OrdinalIgnoreCase))
                    return string.Empty;
                var relative = fullPath.Substring(root.Length).Replace('\\', '/');
                var manifestPath = Path.Combine(libraryRoot, ManifestName);
                using (var stream = File.OpenRead(manifestPath))
                {
                    var serializer = new DataContractJsonSerializer(typeof(ManagedManifest));
                    var manifest = serializer.ReadObject(stream) as ManagedManifest;
                    if (manifest != null && manifest.Links != null &&
                        manifest.Links.TryGetValue(relative, out var target))
                        return target;
                }
            }
            catch
            {
            }
            return string.Empty;
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
                var manifestPath = Path.Combine(directory.FullName, ManifestName);
                if (File.Exists(manifestPath) && ReadManagedManifest(manifestPath)?.Active != false)
                {
                    libraryRoot = directory.FullName;
                    return true;
                }
                directory = directory.Parent;
            }
            return false;
        }

        [DataContract]
        private sealed class ManagedManifest
        {
            [DataMember(Name = "kind")]
            public string? Kind { get; set; }

            [DataMember(Name = "library_id")]
            public string? LibraryId { get; set; }

            [DataMember(Name = "active")]
            public bool? Active { get; set; }

            [DataMember(Name = "limit")]
            public int? Limit { get; set; }

            [DataMember(Name = "links")]
            public Dictionary<string, string>? Links { get; set; }

            [DataMember(Name = "files")]
            public Dictionary<string, string>? Files { get; set; }
        }

        private static string StreamSignature(List<MediaStream> streams)
        {
            return string.Join("|", streams.OrderBy(stream => stream.Index).Select(stream =>
                $"{stream.Index}:{stream.Type}:{stream.Codec}:{stream.Language}:{stream.Width}x{stream.Height}:{stream.Channels}:{stream.IsExternal}:{stream.Path}"));
        }

        private void QueueHomeSectionSync(bool force)
        {
            lock (_gate)
            {
                if (_homeSyncInFlight)
                    return;
                if (!force && DateTime.UtcNow - _lastHomeSyncUtc < TimeSpan.FromSeconds(20))
                    return;
                _homeSyncInFlight = true;
            }

            Task.Run(async () =>
            {
                try
                {
                    await Task.Delay(1500).ConfigureAwait(false);
                    SyncHomeSections();
                    _lastHomeSyncUtc = DateTime.UtcNow;
                }
                catch (Exception ex)
                {
                    _logger.Warn("[Most Watched] Could not synchronize HomeScreen sections: " + ex.Message);
                    var config = Plugin.Instance?.Configuration;
                    if (config != null)
                    {
                        config.MovieHomeSectionStatus = "HomeScreen sync failed; check the Emby STRM Bridge log.";
                        config.TvHomeSectionStatus = "HomeScreen sync failed; check the Emby STRM Bridge log.";
                        Plugin.Instance?.SaveConfiguration();
                    }
                }
                finally
                {
                    lock (_gate)
                        _homeSyncInFlight = false;
                }
            });
        }

        private void SyncHomeSections()
        {
            var plugin = Plugin.Instance;
            var config = plugin?.Configuration;
            if (config == null)
                return;

            config.ManagedHomeSections = config.ManagedHomeSections ?? new List<HomeSectionTracking>();
            var managedLibraries = GetManagedLibraries();
            var movieLibraries = managedLibraries.Where(library => library.Kind == "movies").ToList();
            var tvLibraries = managedLibraries.Where(library => library.Kind == "tv_shows").ToList();
            config.ActiveMovieOutputLibrary = string.Join(", ", movieLibraries.Select(library => library.Name).Distinct());
            config.ActiveTvOutputLibrary = string.Join(", ", tvLibraries.Select(library => library.Name).Distinct());
            config.MovieHomeSectionStatus = HomeSectionStatus(
                movieLibraries,
                config.MovieHomeSectionLibraryId,
                config.MovieHomeSectionUserIds);
            config.TvHomeSectionStatus = HomeSectionStatus(
                tvLibraries,
                config.TvHomeSectionLibraryId,
                config.TvHomeSectionUserIds);
            var desired = new List<DesiredHomeSection>();
            var unresolvedFolderKinds = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach (var library in managedLibraries)
            {
                if (string.IsNullOrWhiteSpace(library.FolderItemId))
                {
                    unresolvedFolderKinds.Add(library.Kind);
                    _logger.Warn("[Most Watched] Could not resolve the underlying folder item for library " + library.Name);
                    continue;
                }
                var configuredLibraryId = library.Kind == "movies"
                    ? config.MovieHomeSectionLibraryId
                    : config.TvHomeSectionLibraryId;
                if (!string.IsNullOrEmpty(configuredLibraryId) &&
                    !string.Equals(configuredLibraryId, library.ViewId, StringComparison.OrdinalIgnoreCase))
                    continue;
                var users = library.Kind == "movies"
                    ? config.MovieHomeSectionUserIds
                    : config.TvHomeSectionUserIds;
                foreach (var userId in ResolveConfiguredUsers(users))
                {
                    if (string.IsNullOrWhiteSpace(userId))
                        continue;
                    desired.Add(new DesiredHomeSection
                    {
                        UserId = userId,
                        LibraryId = library.ViewId,
                        FolderItemId = library.FolderItemId,
                        Kind = library.Kind,
                        LibraryName = library.Name,
                        Limit = library.Limit
                    });
                }
            }

            var desiredKeys = new HashSet<string>(desired.Select(section => TrackingKey(
                section.UserId,
                section.Kind,
                section.LibraryId)), StringComparer.OrdinalIgnoreCase);
            var failedKinds = new HashSet<string>(StringComparer.OrdinalIgnoreCase);

            foreach (var stale in config.ManagedHomeSections.ToList())
            {
                if (unresolvedFolderKinds.Contains(stale.Kind))
                    continue;
                if (desiredKeys.Contains(TrackingKey(stale.UserId, stale.Kind, stale.LibraryId)))
                    continue;
                var removed = false;
                try
                {
                    var internalUserId = _userManager.GetInternalId(stale.UserId);
                    if (!string.IsNullOrEmpty(stale.SectionId))
                        _userManager.DeleteHomeSections(internalUserId, new[] { stale.SectionId }, CancellationToken.None);
                    removed = true;
                }
                catch (Exception ex)
                {
                    failedKinds.Add(stale.Kind);
                    _logger.Warn("[Most Watched] Could not remove an obsolete HomeScreen section: " + ex.Message);
                }
                if (removed)
                    config.ManagedHomeSections.Remove(stale);
            }

            foreach (var wanted in desired)
            {
                try
                {
                    SyncHomeSection(config, wanted);
                }
                catch (Exception ex)
                {
                    failedKinds.Add(wanted.Kind);
                    _logger.Warn("[Most Watched] HomeScreen section for " + wanted.LibraryName + " failed: " + ex.Message);
                }
            }

            try
            {
                SyncHomeSectionExclusions(config, desired);
            }
            catch (Exception ex)
            {
                foreach (var kind in desired.Select(section => section.Kind).Distinct())
                    failedKinds.Add(kind);
                _logger.Warn("[Most Watched] Could not update section exclusions: " + ex.Message);
            }
            if (failedKinds.Contains("movies"))
                config.MovieHomeSectionStatus = "HomeScreen sync failed; check the Emby STRM Bridge log.";
            if (failedKinds.Contains("tv_shows"))
                config.TvHomeSectionStatus = "HomeScreen sync failed; check the Emby STRM Bridge log.";
            plugin?.SaveConfiguration();
        }

        private static string HomeSectionStatus(
            List<ManagedLibrary> detectedLibraries,
            string configuredLibraryId,
            List<string> configuredUsers)
        {
            if (detectedLibraries.Count == 0)
                return "No active Manager output manifest found. Enable the list and run a Manager sync.";
            if (!string.IsNullOrEmpty(configuredLibraryId) &&
                !detectedLibraries.Any(library => string.Equals(
                    library.ViewId,
                    configuredLibraryId,
                    StringComparison.OrdinalIgnoreCase)))
                return "Selected library does not match an active Manager output. Select the same library on both pages or use Auto.";
            var selectedLibraries = string.IsNullOrEmpty(configuredLibraryId)
                ? detectedLibraries
                : detectedLibraries.Where(library => string.Equals(
                    library.ViewId,
                    configuredLibraryId,
                    StringComparison.OrdinalIgnoreCase)).ToList();
            if (selectedLibraries.Any(library => string.IsNullOrWhiteSpace(library.FolderItemId)))
                return "Could not resolve the selected library's underlying folder item for its HomeScreen query.";
            if (detectedLibraries.Count > 1 && string.IsNullOrEmpty(configuredLibraryId))
                return "Multiple active output libraries found. Select the intended one explicitly.";
            if (configuredUsers == null || configuredUsers.Count == 0)
                return "Output detected. Select HomeScreen users or All users.";
            return "Output and HomeScreen settings are configured.";
        }

        private List<string> ResolveConfiguredUsers(List<string> configuredUsers)
        {
            configuredUsers = configuredUsers ?? new List<string>();
            if (!configuredUsers.Any(user => string.Equals(user, "*", StringComparison.Ordinal)))
                return configuredUsers.Where(user => !string.IsNullOrWhiteSpace(user)).Distinct(StringComparer.OrdinalIgnoreCase).ToList();

            return (_userManager.GetUsers(new UserQuery { Limit = 10000 }, CancellationToken.None)?.Items ?? Array.Empty<User>())
                .Select(user => Convert.ToString(user.Id))
                .Where(userId => !string.IsNullOrWhiteSpace(userId))
                .Select(userId => userId!)
                .Distinct(StringComparer.OrdinalIgnoreCase)
                .ToList();
        }

        private void SyncHomeSection(PluginConfiguration config, DesiredHomeSection wanted)
        {
            var internalUserId = _userManager.GetInternalId(wanted.UserId);
            var currentSections = _userManager.GetHomeSections(internalUserId, CancellationToken.None)?.Sections
                                  ?? Array.Empty<ContentSection>();
            var tracked = config.ManagedHomeSections.FirstOrDefault(entry =>
                string.Equals(entry.UserId, wanted.UserId, StringComparison.OrdinalIgnoreCase) &&
                string.Equals(entry.Kind, wanted.Kind, StringComparison.OrdinalIgnoreCase));
            var displayName = wanted.LibraryName;
            var existing = tracked == null
                ? null
                : currentSections.FirstOrDefault(section => GetStringProperty(section, "Id") == tracked.SectionId);
            var legacyName = wanted.Kind == "movies"
                ? HomeSectionPrefix + "Movies"
                : HomeSectionPrefix + "TV Shows";
            existing = existing ?? currentSections.FirstOrDefault(section =>
                string.Equals(GetStringProperty(section, "ParentId"), wanted.LibraryId, StringComparison.OrdinalIgnoreCase) &&
                string.Equals(GetStringProperty(section, "CustomName"), legacyName, StringComparison.OrdinalIgnoreCase));

            var updated = BuildHomeSection(config, wanted, displayName, existing);
            if (existing != null)
            {
                var existingId = GetProperty(existing, "Id");
                if (existingId != null)
                    SetProperty(updated, "Id", existingId);
                _userManager.UpdateHomeSection(internalUserId, updated, CancellationToken.None);
                if (tracked == null)
                {
                    tracked = new HomeSectionTracking
                    {
                        UserId = wanted.UserId,
                        Kind = wanted.Kind
                    };
                    config.ManagedHomeSections.Add(tracked);
                }
                tracked.SectionId = GetStringProperty(existing, "Id") ?? string.Empty;
                tracked.LibraryId = wanted.LibraryId;
                return;
            }

            var oldIds = new HashSet<string>(
                currentSections.Select(section => GetStringProperty(section, "Id"))
                    .Where(id => !string.IsNullOrEmpty(id))
                    .Select(id => id!),
                StringComparer.OrdinalIgnoreCase);
            _userManager.AddHomeSection(internalUserId, updated, CancellationToken.None);
            var after = _userManager.GetHomeSections(internalUserId, CancellationToken.None)?.Sections
                        ?? Array.Empty<ContentSection>();
            var created = after.FirstOrDefault(section =>
            {
                var sectionId = GetStringProperty(section, "Id");
                return !string.IsNullOrEmpty(sectionId) && !oldIds.Contains(sectionId!);
            });
            if (created == null)
                created = after.FirstOrDefault(section =>
                    string.Equals(GetStringProperty(section, "CustomName"), displayName, StringComparison.OrdinalIgnoreCase));

            if (tracked == null)
            {
                tracked = new HomeSectionTracking
                {
                    UserId = wanted.UserId,
                    Kind = wanted.Kind
                };
                config.ManagedHomeSections.Add(tracked);
            }
            tracked.SectionId = GetStringProperty(created, "Id") ?? string.Empty;
            tracked.LibraryId = wanted.LibraryId;
        }

        private void SyncHomeSectionExclusions(PluginConfiguration config, List<DesiredHomeSection> desired)
        {
            config.ManagedHomeSectionExclusions = config.ManagedHomeSectionExclusions
                                                  ?? new List<HomeSectionExclusionTracking>();
            var targetsByUser = desired
                .GroupBy(section => section.UserId, StringComparer.OrdinalIgnoreCase)
                .ToDictionary(
                    group => group.Key,
                    group => new HashSet<string>(group.Select(section => section.LibraryId), StringComparer.OrdinalIgnoreCase),
                    StringComparer.OrdinalIgnoreCase);

            foreach (var stale in config.ManagedHomeSectionExclusions.ToList())
            {
                if (targetsByUser.TryGetValue(stale.UserId, out var libraries) && libraries.Contains(stale.LibraryId))
                    continue;

                var restored = false;
                try
                {
                    var internalUserId = _userManager.GetInternalId(stale.UserId);
                    var sections = _userManager.GetHomeSections(internalUserId, CancellationToken.None)?.Sections
                                   ?? Array.Empty<ContentSection>();
                    var section = sections.FirstOrDefault(candidate =>
                        GetStringProperty(candidate, "Id") == stale.SectionId);
                    if (section == null)
                    {
                        restored = true;
                    }
                    else
                    {
                        var changed = false;
                        if (stale.AddedToExcludedFolders)
                            changed |= UpdateStringArrayProperty(section, "ExcludedFolders", stale.LibraryId, false, out _);
                        if (stale.AddedToQuery)
                            changed |= UpdateQueryExcludedViews(section, stale.LibraryId, false, out _);
                        if (changed)
                            _userManager.UpdateHomeSection(internalUserId, section, CancellationToken.None);
                        restored = true;
                    }
                }
                catch (Exception ex)
                {
                    _logger.Warn("[Most Watched] Could not restore a HomeScreen section exclusion: " + ex.Message);
                }

                if (restored)
                    config.ManagedHomeSectionExclusions.Remove(stale);
            }

            foreach (var userGroup in targetsByUser)
            {
                long internalUserId;
                try
                {
                    internalUserId = _userManager.GetInternalId(userGroup.Key);
                }
                catch (Exception ex)
                {
                    _logger.Warn("[Most Watched] Could not resolve user for HomeScreen filtering: " + ex.Message);
                    continue;
                }

                var sections = _userManager.GetHomeSections(internalUserId, CancellationToken.None)?.Sections
                               ?? Array.Empty<ContentSection>();
                var ownedSectionIds = new HashSet<string>(
                    config.ManagedHomeSections
                        .Where(entry => string.Equals(entry.UserId, userGroup.Key, StringComparison.OrdinalIgnoreCase))
                        .Select(entry => entry.SectionId)
                        .Where(id => !string.IsNullOrEmpty(id)),
                    StringComparer.OrdinalIgnoreCase);

                foreach (var section in sections)
                {
                    var sectionId = GetStringProperty(section, "Id");
                    if (string.IsNullOrEmpty(sectionId) || ownedSectionIds.Contains(sectionId!) ||
                        (GetStringProperty(section, "CustomName") ?? string.Empty)
                        .StartsWith(HomeSectionPrefix, StringComparison.OrdinalIgnoreCase))
                        continue;

                    var changed = false;
                    foreach (var libraryId in userGroup.Value)
                    {
                        var tracking = config.ManagedHomeSectionExclusions.FirstOrDefault(entry =>
                            string.Equals(entry.UserId, userGroup.Key, StringComparison.OrdinalIgnoreCase) &&
                            string.Equals(entry.SectionId, sectionId, StringComparison.OrdinalIgnoreCase) &&
                            string.Equals(entry.LibraryId, libraryId, StringComparison.OrdinalIgnoreCase));

                        var folderChanged = UpdateStringArrayProperty(section, "ExcludedFolders", libraryId, true, out var addedFolder);
                        var queryChanged = UpdateQueryExcludedViews(section, libraryId, true, out var addedQuery);
                        changed |= folderChanged || queryChanged;

                        if (tracking == null && (addedFolder || addedQuery))
                        {
                            tracking = new HomeSectionExclusionTracking
                            {
                                UserId = userGroup.Key,
                                SectionId = sectionId!,
                                LibraryId = libraryId
                            };
                            config.ManagedHomeSectionExclusions.Add(tracking);
                        }
                        if (tracking != null)
                        {
                            tracking.AddedToExcludedFolders |= addedFolder;
                            tracking.AddedToQuery |= addedQuery;
                        }
                    }
                    if (changed)
                        _userManager.UpdateHomeSection(internalUserId, section, CancellationToken.None);
                }
            }
        }

        private static bool UpdateStringArrayProperty(
            object instance,
            string propertyName,
            string value,
            bool add,
            out bool changedValue)
        {
            changedValue = false;
            var property = instance.GetType().GetProperty(propertyName, BindingFlags.Public | BindingFlags.Instance);
            if (property == null || !property.CanWrite || property.PropertyType != typeof(string[]))
                return false;
            var current = property.GetValue(instance, null) as string[] ?? Array.Empty<string>();
            if (add)
            {
                if (current.Contains(value, StringComparer.OrdinalIgnoreCase))
                    return false;
                property.SetValue(instance, current.Concat(new[] { value }).ToArray(), null);
                changedValue = true;
                return true;
            }

            var filtered = current.Where(item => !string.Equals(item, value, StringComparison.OrdinalIgnoreCase)).ToArray();
            if (filtered.Length == current.Length)
                return false;
            property.SetValue(instance, filtered, null);
            changedValue = true;
            return true;
        }

        private static bool UpdateQueryExcludedViews(
            object section,
            string libraryId,
            bool add,
            out bool changedValue)
        {
            changedValue = false;
            var queryProperty = section.GetType().GetProperty("Query", BindingFlags.Public | BindingFlags.Instance);
            if (queryProperty == null || !queryProperty.CanWrite)
                return false;
            var query = queryProperty.GetValue(section, null);
            if (query == null)
            {
                try
                {
                    query = Activator.CreateInstance(queryProperty.PropertyType);
                    queryProperty.SetValue(section, query, null);
                }
                catch
                {
                    return false;
                }
            }
            if (query == null)
                return false;

            var excludedProperty = query.GetType().GetProperty("ExcludeUserViewIds", BindingFlags.Public | BindingFlags.Instance);
            if (excludedProperty == null || !excludedProperty.CanWrite)
                return false;
            if (excludedProperty.PropertyType == typeof(string[]))
                return UpdateStringArrayProperty(query, "ExcludeUserViewIds", libraryId, add, out changedValue);
            if (excludedProperty.PropertyType == typeof(Guid[]))
            {
                if (!Guid.TryParse(libraryId, out var libraryGuid))
                    return false;
                var current = excludedProperty.GetValue(query, null) as Guid[] ?? Array.Empty<Guid>();
                if (add)
                {
                    if (current.Contains(libraryGuid))
                        return false;
                    excludedProperty.SetValue(query, current.Concat(new[] { libraryGuid }).ToArray(), null);
                    changedValue = true;
                    return true;
                }

                var filtered = current.Where(id => id != libraryGuid).ToArray();
                if (filtered.Length == current.Length)
                    return false;
                excludedProperty.SetValue(query, filtered, null);
                changedValue = true;
                return true;
            }
            return false;
        }

        private ContentSection BuildHomeSection(
            PluginConfiguration config,
            DesiredHomeSection wanted,
            string displayName,
            ContentSection existing)
        {
            return HomeSectionBuilder.Build(
                wanted.LibraryId,
                wanted.FolderItemId,
                displayName,
                wanted.Kind,
                wanted.Kind == "movies" ? config.MovieHomeSectionSortBy : config.TvHomeSectionSortBy,
                wanted.Kind == "movies" ? config.MovieHomeSectionSortOrder : config.TvHomeSectionSortOrder,
                wanted.Kind == "movies" ? config.MovieHomeSectionImageType : config.TvHomeSectionImageType,
                wanted.Limit,
                GetLibraryViewIds(),
                existing);
        }

        private IEnumerable<string> GetLibraryViewIds()
        {
            var folders = _libraryManager.GetVirtualFolders();
            if (folders == null)
                return Array.Empty<string>();
            return folders
                .Select(folder => Convert.ToString(GetProperty(folder, "ItemId")))
                .Where(id => !string.IsNullOrWhiteSpace(id))
                .Select(id => id!);
        }

        private List<ManagedLibrary> GetManagedLibraries()
        {
            var result = new List<ManagedLibrary>();
            var folders = _libraryManager.GetVirtualFolders();
            if (folders == null)
                return result;
            foreach (var folder in folders)
            {
                var libraryId = Convert.ToString(GetProperty(folder, "ItemId"));
                var name = Convert.ToString(GetProperty(folder, "Name")) ?? string.Empty;
                var collectionType = Convert.ToString(GetProperty(folder, "CollectionType")) ?? string.Empty;
                var locations = GetProperty(folder, "Locations") as IEnumerable<string> ?? Array.Empty<string>();
                foreach (var location in locations)
                {
                    if (string.IsNullOrEmpty(location))
                        continue;
                    var manifestPath = Path.Combine(location, ManifestName);
                    if (!File.Exists(manifestPath))
                        continue;
                    var manifest = ReadManagedManifest(manifestPath);
                    if (manifest == null || manifest.Active == false)
                        continue;
                    if (!string.IsNullOrEmpty(manifest.LibraryId) &&
                        !string.Equals(manifest.LibraryId, libraryId, StringComparison.OrdinalIgnoreCase))
                        continue;

                    var kind = manifest.Kind;
                    if (string.IsNullOrEmpty(kind))
                    {
                        kind = collectionType.Equals("movies", StringComparison.OrdinalIgnoreCase)
                            ? "movies"
                            : collectionType.Equals("tvshows", StringComparison.OrdinalIgnoreCase)
                                ? "tv_shows" : string.Empty;
                    }
                    if (kind != "movies" && kind != "tv_shows")
                        continue;
                    var underlyingFolder = _libraryManager.FindByPath(location, false);
                    result.Add(new ManagedLibrary
                    {
                        ViewId = libraryId ?? string.Empty,
                        FolderItemId = underlyingFolder?.InternalId > 0
                            ? underlyingFolder.InternalId.ToString()
                            : string.Empty,
                        Name = name,
                        Kind = kind,
                        Limit = Math.Max(1, Math.Min(manifest.Limit ?? 10, 100))
                    });
                }
            }
            return result;
        }

        private static ManagedManifest? ReadManagedManifest(string manifestPath)
        {
            try
            {
                using (var stream = File.OpenRead(manifestPath))
                {
                    var serializer = new DataContractJsonSerializer(typeof(ManagedManifest));
                    return serializer.ReadObject(stream) as ManagedManifest;
                }
            }
            catch
            {
                return null;
            }
        }

        private static object? GetProperty(object? instance, string name)
        {
            return instance?.GetType().GetProperty(name, BindingFlags.Public | BindingFlags.Instance)?.GetValue(instance, null);
        }

        private static string? GetStringProperty(object? instance, string name)
        {
            return Convert.ToString(GetProperty(instance, name));
        }

        private static void SetProperty(object instance, string name, object value)
        {
            if (instance == null)
                return;
            var property = instance.GetType().GetProperty(name, BindingFlags.Public | BindingFlags.Instance);
            if (property == null || !property.CanWrite)
                return;
            if (value == null)
            {
                property.SetValue(instance, null, null);
                return;
            }

            var destinationType = Nullable.GetUnderlyingType(property.PropertyType) ?? property.PropertyType;
            object converted = value;
            if (!destinationType.IsInstanceOfType(value))
            {
                if (destinationType.IsEnum)
                {
                    converted = Enum.Parse(destinationType, Convert.ToString(value), true);
                }
                else if (destinationType == typeof(string))
                {
                    converted = Convert.ToString(value);
                }
                else
                {
                    converted = Convert.ChangeType(value, destinationType);
                }
            }
            property.SetValue(instance, converted, null);
        }

        private static string TrackingKey(string userId, string kind, string libraryId)
        {
            return (userId ?? string.Empty) + "|" + (kind ?? string.Empty) + "|" + (libraryId ?? string.Empty);
        }

        private sealed class ManagedLibrary
        {
            public string ViewId { get; set; } = string.Empty;
            public string FolderItemId { get; set; } = string.Empty;
            public string Name { get; set; } = string.Empty;
            public string Kind { get; set; } = string.Empty;
            public int Limit { get; set; } = 10;
        }

        private sealed class DesiredHomeSection
        {
            public string UserId { get; set; } = string.Empty;
            public string LibraryId { get; set; } = string.Empty;
            public string FolderItemId { get; set; } = string.Empty;
            public string LibraryName { get; set; } = string.Empty;
            public string Kind { get; set; } = string.Empty;
            public int Limit { get; set; } = 10;
        }

        public void Dispose()
        {
            _libraryManager.ItemAdded -= OnItemChanged;
            _libraryManager.ItemUpdated -= OnItemChanged;
            _libraryManager.ItemRemoved -= OnItemRemoved;
        }
    }
}
