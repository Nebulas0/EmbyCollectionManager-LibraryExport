using System;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using MediaBrowser.Model.Entities;

namespace EmbyCollectionManagerStrmBridge
{
    public static class HomeSectionBuilder
    {
        public static ContentSection Build(
            string viewId,
            string folderItemId,
            string libraryName,
            string kind,
            string sortBy,
            string sortOrder,
            string imageType,
            int limit,
            IEnumerable<string> libraryViewIds,
            ContentSection? existing = null)
        {
            var itemType = kind == "movies"
                ? "Movie"
                : kind == "tv_shows"
                    ? "Series"
                    : throw new ArgumentOutOfRangeException(nameof(kind), kind, "Unsupported HomeScreen library kind.");
            limit = Math.Max(1, Math.Min(limit, 100));

            var section = existing ?? new ContentSection();
            var excludedViews = BuildExcludedViews(viewId, libraryViewIds, section.ExcludedFolders);
            section.SectionType = "items";
            section.ParentId = viewId;
            section.CustomName = libraryName;
            section.ItemTypes = new[] { itemType };
            section.SortBy = sortBy;
            section.SortOrder = sortOrder;
            section.ImageType = imageType;
            section.ExcludedFolders = excludedViews;

            section.Query = new HomeSectionItemsQuery
            {
                ParentId = folderItemId,
                ParentIds = long.TryParse(folderItemId, out var folderInternalId)
                    ? new[] { folderInternalId }
                    : Array.Empty<long>(),
                IncludeItemTypes = new[] { itemType },
                ExcludeUserViewIds = excludedViews,
                Limit = limit,
                IsMovie = itemType == "Movie" ? true : null,
                IsSeries = itemType == "Series" ? true : null
            };

            SetOptionalLimit(section, limit);
            return section;
        }

        private static string[] BuildExcludedViews(
            string selectedLibraryId,
            IEnumerable<string> libraryViewIds,
            IEnumerable<string>? existingExclusions)
        {
            var ids = (libraryViewIds ?? Array.Empty<string>())
                .Concat(existingExclusions ?? Array.Empty<string>())
                .Where(id => !string.IsNullOrWhiteSpace(id))
                .Select(id => id.Trim())
                .Where(id => !SameViewId(id, selectedLibraryId))
                .Distinct(StringComparer.OrdinalIgnoreCase);
            return ids.ToArray();
        }

        private static bool SameViewId(string left, string right)
        {
            return string.Equals(left, right, StringComparison.OrdinalIgnoreCase) ||
                   string.Equals(NormalizeViewId(left), NormalizeViewId(right), StringComparison.OrdinalIgnoreCase);
        }

        private static string NormalizeViewId(string value)
        {
            return (value ?? string.Empty).Replace("-", string.Empty).Trim();
        }

        private static void SetOptionalLimit(ContentSection section, int limit)
        {
            var property = typeof(ContentSection).GetProperty("MaxItems", BindingFlags.Public | BindingFlags.Instance);
            if (property == null || !property.CanWrite)
                return;

            try
            {
                property.SetValue(section, Convert.ChangeType(limit, property.PropertyType), null);
            }
            catch
            {
            }
        }
    }

    public sealed class HomeSectionItemsQuery : ItemsQuery
    {
        public string ParentId { get; set; } = string.Empty;

        public long[] ParentIds { get; set; } = Array.Empty<long>();

        public string[] IncludeItemTypes { get; set; } = Array.Empty<string>();

        public string[] ExcludeUserViewIds { get; set; } = Array.Empty<string>();

        public int Limit { get; set; }
    }
}
