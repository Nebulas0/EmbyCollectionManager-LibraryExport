using System;
using System.Collections.Generic;
using System.Linq;
using MediaBrowser.Common.Configuration;
using MediaBrowser.Common.Plugins;
using MediaBrowser.Model.Plugins;
using MediaBrowser.Model.Serialization;

namespace EmbyCollectionManagerStrmBridge
{
    public sealed class Plugin : BasePlugin<PluginConfiguration>, IHasWebPages
    {
        public Plugin(IApplicationPaths applicationPaths, IXmlSerializer xmlSerializer)
            : base(applicationPaths, xmlSerializer)
        {
            Instance = this;
        }

        public static Plugin? Instance { get; private set; }

        public override string Name => "Emby Collection Manager STRM Bridge";

        public override Guid Id => new Guid("3d9e8cda-9217-4ca9-9353-8c89adda7789");

        public override string Description =>
            "Maintains ranked Most Watched metadata and per-user HomeScreen sections.";

        public override void UpdateConfiguration(BasePluginConfiguration configuration)
        {
            base.UpdateConfiguration(configuration);
            MostWatchedStrmEntryPoint.RequestHomeSectionSync();
        }

        public IEnumerable<PluginPageInfo> GetPages()
        {
            var assembly = GetType().Assembly;
            var page = assembly.GetManifestResourceNames()
                .FirstOrDefault(name => name.EndsWith("configurationPage.html", StringComparison.OrdinalIgnoreCase));
            var script = assembly.GetManifestResourceNames()
                .FirstOrDefault(name => name.EndsWith("configurationPage.js", StringComparison.OrdinalIgnoreCase));
            if (page == null || script == null)
                return Array.Empty<PluginPageInfo>();

            return new[]
            {
                new PluginPageInfo
                {
                    Name = "EmbyCollectionManagerStrmBridge",
                    EmbeddedResourcePath = page,
                    EnableInMainMenu = true,
                    DisplayName = "Most Watched HomeScreen",
                    MenuIcon = "sort"
                },
                new PluginPageInfo
                {
                    Name = "EmbyCollectionManagerStrmBridgeJS",
                    EmbeddedResourcePath = script
                }
            };
        }
    }

    public sealed class PluginConfiguration : BasePluginConfiguration
    {
        public string MovieHomeSectionLibraryId { get; set; } = string.Empty;

        public string TvHomeSectionLibraryId { get; set; } = string.Empty;

        public List<string> MovieHomeSectionUserIds { get; set; } = new List<string>();

        public List<string> TvHomeSectionUserIds { get; set; } = new List<string>();

        public string MovieHomeSectionImageType { get; set; } = "Primary";

        public string TvHomeSectionImageType { get; set; } = "Primary";

        public string MovieHomeSectionSortBy { get; set; } = "SortName";

        public string TvHomeSectionSortBy { get; set; } = "SortName";

        public string MovieHomeSectionSortOrder { get; set; } = "Ascending";

        public string TvHomeSectionSortOrder { get; set; } = "Ascending";

        public List<HomeSectionTracking> ManagedHomeSections { get; set; } = new List<HomeSectionTracking>();

        public List<HomeSectionExclusionTracking> ManagedHomeSectionExclusions { get; set; } =
            new List<HomeSectionExclusionTracking>();
    }

    public sealed class HomeSectionTracking
    {
        public string UserId { get; set; } = string.Empty;

        public string SectionId { get; set; } = string.Empty;

        public string LibraryId { get; set; } = string.Empty;

        public string Kind { get; set; } = string.Empty;
    }

    public sealed class HomeSectionExclusionTracking
    {
        public string UserId { get; set; } = string.Empty;

        public string SectionId { get; set; } = string.Empty;

        public string LibraryId { get; set; } = string.Empty;

        public bool AddedToExcludedFolders { get; set; }

        public bool AddedToQuery { get; set; }
    }
}
