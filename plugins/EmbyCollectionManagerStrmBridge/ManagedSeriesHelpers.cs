using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Entities.TV;
using MediaBrowser.Model.Entities;

namespace EmbyCollectionManagerStrmBridge
{
    public static class ManagedSeriesHelpers
    {
        public static bool IsManagerOwnedSeriesFolder(
            string seriesPath,
            string libraryRoot,
            IEnumerable<string> managedLinks,
            IEnumerable<string> managedFiles)
        {
            try
            {
                if (string.IsNullOrWhiteSpace(seriesPath) || string.IsNullOrWhiteSpace(libraryRoot) ||
                    !Directory.Exists(seriesPath))
                    return false;

                var root = Path.GetFullPath(libraryRoot).TrimEnd(Path.DirectorySeparatorChar) +
                           Path.DirectorySeparatorChar;
                var fullSeriesPath = Path.GetFullPath(seriesPath).TrimEnd(Path.DirectorySeparatorChar);
                if (!fullSeriesPath.StartsWith(root, StringComparison.OrdinalIgnoreCase))
                    return false;
                if ((new DirectoryInfo(fullSeriesPath).Attributes & FileAttributes.ReparsePoint) != 0)
                    return false;

                var relative = fullSeriesPath.Substring(root.Length).Replace('\\', '/').Trim('/');
                if (string.IsNullOrEmpty(relative))
                    return false;
                var prefix = relative + "/";
                return (managedLinks ?? Enumerable.Empty<string>())
                           .Any(path => path.StartsWith(prefix, StringComparison.OrdinalIgnoreCase)) ||
                       (managedFiles ?? Enumerable.Empty<string>())
                           .Any(path => path.StartsWith(prefix, StringComparison.OrdinalIgnoreCase) &&
                                        path.EndsWith(".strm", StringComparison.OrdinalIgnoreCase));
            }
            catch
            {
                return false;
            }
        }

        public static InternalItemsQuery BuildSeriesReconciliationQuery(long folderItemId)
        {
            return new InternalItemsQuery
            {
                ParentIds = new[] { folderItemId },
                Recursive = true,
                IncludeItemTypes = new[] { "Series" }
            };
        }

        public static bool AttachGeneratedSeriesImages(Series series)
        {
            if (series == null || string.IsNullOrWhiteSpace(series.Path))
                return false;

            return SetLocalImage(series, ImageType.Primary, Path.Combine(series.Path, "poster.jpg")) |
                   SetLocalImage(series, ImageType.Thumb, Path.Combine(series.Path, "landscape.jpg"));
        }

        public static bool SetLocalImage(BaseItem item, ImageType imageType, string imagePath)
        {
            if (item == null || !File.Exists(imagePath))
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
    }
}
