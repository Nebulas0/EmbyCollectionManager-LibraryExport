using System.Xml.Serialization;
using EmbyCollectionManagerStrmBridge;
using MediaBrowser.Controller.Entities.TV;
using MediaBrowser.Model.Entities;

var expectedMovies = new List<string> { "user-movies-123", "*" };
var expectedShows = new List<string> { "user-tv-456" };
var configuration = new PluginConfiguration
{
    MovieHomeSectionUserIds = expectedMovies,
    TvHomeSectionUserIds = expectedShows
};

var serializer = new XmlSerializer(typeof(PluginConfiguration));
string savedXml;
using (var writer = new StringWriter())
{
    serializer.Serialize(writer, configuration);
    savedXml = writer.ToString();
}

if (!savedXml.Contains("user-movies-123") || !savedXml.Contains("user-tv-456") || !savedXml.Contains("*"))
    throw new InvalidOperationException("Plugin configuration XML omitted selected user IDs.");

using var reader = new StringReader(savedXml);
var reloaded = (PluginConfiguration?)serializer.Deserialize(reader)
               ?? throw new InvalidOperationException("Plugin configuration deserialized as null.");

if (!reloaded.MovieHomeSectionUserIds.SequenceEqual(expectedMovies))
    throw new InvalidOperationException("Movie HomeScreen user IDs did not survive XML save/reload.");
if (!reloaded.TvHomeSectionUserIds.SequenceEqual(expectedShows))
    throw new InvalidOperationException("TV HomeScreen user IDs did not survive XML save/reload.");

Console.WriteLine("HomeScreen user ID and All-users ('*') configuration round-trip passed.");
RunHomeSectionProjectionTests();
RunManagedSeriesTests();

static void RunHomeSectionProjectionTests()
{
    var movieViews = new[] { "movies-view-selected", "movies-view-other", "tv-view-selected", "tv-view-other" };
    var movies = Enumerable.Range(1, 14)
        .Select(index => new TestItem("movies-view-selected", 3990052, "Movie", $"Movie {index:D2}", $"M-{index:D2}"))
        .Concat(Enumerable.Range(1, 4)
            .Select(index => new TestItem("movies-view-other", 3990053, "Movie", $"Foreign Movie {index:D2}", $"A-{index:D2}")))
        .ToList();
    var movieSection = HomeSectionBuilder.Build(
        "movies-view-selected", "3990052", "My Existing Movie Library", "movies", "SortName", "Ascending",
        "Primary", 10, movieViews);
    var movieQuery = (HomeSectionItemsQuery)movieSection.Query;
    var movieResults = Project(movieSection, movies);
    Assert(movieSection.CustomName == "My Existing Movie Library", "Movie row title must use the existing library name.");
    Assert(movieSection.ParentId == "movies-view-selected", "Movie row must retain the library view ID for navigation.");
    Assert(movieSection.ItemTypes.SequenceEqual(new[] { "Movie" }), "Movie row must filter to Movie items.");
    Assert(movieSection.ExcludedFolders.Contains("movies-view-other"), "Movie row must exclude the other movie library view.");
    Assert(!movieSection.ExcludedFolders.Contains("movies-view-selected"), "Movie row must not exclude its selected view.");
    Assert(movieSection.SortBy == "SortName" && movieSection.SortOrder == "Ascending", "Movie sort settings must be applied.");
    Assert(movieQuery.ParentId == "3990052" &&
           movieQuery.ParentIds.SequenceEqual(new[] { 3990052L }) &&
           movieQuery.IncludeItemTypes.SequenceEqual(new[] { "Movie" }) &&
           movieQuery.ExcludeUserViewIds.Contains("movies-view-other") &&
           movieQuery.Limit == 10,
        "Movie query must explicitly scope the underlying folder item, type, exclusions, and limit.");
    AssertOptionalSectionLimit(movieSection, 10);
    Assert(movieResults.Count == 10, "Movie row must respect its configured limit.");
    Assert(movieResults.All(item => item.ViewId == "movies-view-selected" && item.Type == "Movie"),
        "Movie row must contain only items from the selected movie library.");
    Assert(movieResults.Select(item => item.SortName).SequenceEqual(
        movieResults.Select(item => item.SortName).OrderBy(value => value, StringComparer.Ordinal)),
        "Movie row must use ascending SortName order.");

    var shows = Enumerable.Range(1, 13)
        .Select(index => new TestItem("tv-view-selected", 3990054, "Series", $"Show {index:D2}", $"S-{index:D2}"))
        .Concat(Enumerable.Range(1, 5)
            .Select(index => new TestItem("tv-view-other", 3990055, "Series", $"Foreign Show {index:D2}", $"Z-{index:D2}")))
        .Concat(new[] { new TestItem("tv-view-selected", 3990054, "Movie", "Wrong Type", "ZZ") })
        .ToList();
    var showSection = HomeSectionBuilder.Build(
        "tv-view-selected", "3990054", "My Existing TV Library", "tv_shows", "SortName", "Descending",
        "Thumb", 10, movieViews);
    var showQuery = (HomeSectionItemsQuery)showSection.Query;
    var showResults = Project(showSection, shows);
    Assert(showSection.CustomName == "My Existing TV Library", "TV row title must use the existing library name.");
    Assert(showSection.ParentId == "tv-view-selected", "TV row must retain the library view ID for navigation.");
    Assert(showSection.ItemTypes.SequenceEqual(new[] { "Series" }), "TV row must filter to Series items.");
    Assert(showSection.ExcludedFolders.Contains("tv-view-other"), "TV row must exclude the other TV library view.");
    Assert(!showSection.ExcludedFolders.Contains("tv-view-selected"), "TV row must not exclude its selected view.");
    Assert(showSection.SortBy == "SortName" && showSection.SortOrder == "Descending", "TV sort settings must be applied.");
    Assert(showQuery.ParentId == "3990054" &&
           showQuery.ParentIds.SequenceEqual(new[] { 3990054L }) &&
           showQuery.IncludeItemTypes.SequenceEqual(new[] { "Series" }) &&
           showQuery.ExcludeUserViewIds.Contains("tv-view-other") &&
           showQuery.Limit == 10,
        "TV query must explicitly scope the underlying folder item, type, exclusions, and limit.");
    AssertOptionalSectionLimit(showSection, 10);
    Assert(showResults.Count == 10, "TV row must respect its configured limit.");
    Assert(showResults.All(item => item.ViewId == "tv-view-selected" && item.Type == "Series"),
        "TV row must contain only Series from the selected TV library.");
    Assert(showResults.Select(item => item.SortName).SequenceEqual(
        showResults.Select(item => item.SortName).OrderByDescending(value => value, StringComparer.Ordinal)),
        "TV row must use descending SortName order.");

    Console.WriteLine("HomeScreen two-library type, scope, sorting, and limit tests passed.");
}

static List<TestItem> Project(ContentSection section, IEnumerable<TestItem> candidates)
{
    var query = section.Query as HomeSectionItemsQuery
                ?? throw new InvalidOperationException("HomeScreen query must carry the scoped projection.");
    var items = candidates
        .Where(item => query.ParentIds.Contains(item.FolderItemId))
        .Where(item => string.Equals(item.FolderItemId.ToString(), query.ParentId, StringComparison.OrdinalIgnoreCase))
        .Where(item => section.ItemTypes.Contains(item.Type, StringComparer.OrdinalIgnoreCase))
        .Where(item => query.IncludeItemTypes.Contains(item.Type, StringComparer.OrdinalIgnoreCase))
        .Where(item => !query.ExcludeUserViewIds.Contains(item.ViewId, StringComparer.OrdinalIgnoreCase));
    var ordered = string.Equals(section.SortOrder, "Descending", StringComparison.OrdinalIgnoreCase)
        ? items.OrderByDescending(item => item.SortName, StringComparer.Ordinal)
        : items.OrderBy(item => item.SortName, StringComparer.Ordinal);
    return ordered.Take(query.Limit).ToList();
}

static void Assert(bool condition, string message)
{
    if (!condition)
        throw new InvalidOperationException(message);
}

static void AssertOptionalSectionLimit(ContentSection section, int expected)
{
    var property = typeof(ContentSection).GetProperty("MaxItems");
    if (property != null)
        Assert(Convert.ToInt32(property.GetValue(section)) == expected,
            "ContentSection.MaxItems must match the manifest's configured limit when supported.");
}

static void RunManagedSeriesTests()
{
    var query = ManagedSeriesHelpers.BuildSeriesReconciliationQuery(3990052);
    Assert(query.ParentIds.SequenceEqual(new[] { 3990052L }),
        "Existing Series reconciliation must query the underlying folder item ID.");
    Assert(query.IncludeItemTypes.SequenceEqual(new[] { "Series" }),
        "Existing Series reconciliation must query Series only.");

    using var temporary = new TemporaryDirectory();
    var root = Path.Combine(temporary.Path, "Most Watched TV");
    var seriesFolder = Path.Combine(root, "01 - Managed Series");
    Directory.CreateDirectory(Path.Combine(seriesFolder, "Season 01"));
    var managedLinks = new[] { "01 - Managed Series/Season 01/Episode.mkv" };
    Assert(ManagedSeriesHelpers.IsManagerOwnedSeriesFolder(
            seriesFolder, root, managedLinks, Array.Empty<string>()),
        "Symlink-mode Series folders with manifest-owned episode links must be supported.");
    Assert(!ManagedSeriesHelpers.IsManagerOwnedSeriesFolder(
            Path.Combine(temporary.Path, "Source Series"), root, managedLinks, Array.Empty<string>()),
        "Source-library Series outside the output root must never be treated as manager-owned.");

    var posterPath = Path.Combine(seriesFolder, "poster.jpg");
    File.WriteAllBytes(posterPath, new byte[] { 1, 2, 3 });
    var series = new Series { Path = seriesFolder };
    Assert(ManagedSeriesHelpers.AttachGeneratedSeriesImages(series),
        "Generated series poster should update the indexed output item.");
    Assert(series.ImageInfos.Any(image =>
            image.Type == ImageType.Primary &&
            string.Equals(image.Path, posterPath, StringComparison.Ordinal)),
        "poster.jpg must be attached as the Series Primary image in symlink mode.");
    Assert(series.ImageInfos.All(image => image.Type != ImageType.Thumb),
        "Poster-only output must not acquire a missing landscape image.");

    Console.WriteLine("Managed Series symlink ownership, Primary artwork, and startup-query tests passed.");
}

sealed class TestItem
{
    public TestItem(string viewId, long folderItemId, string type, string name, string sortName)
    {
        ViewId = viewId;
        FolderItemId = folderItemId;
        Type = type;
        Name = name;
        SortName = sortName;
    }

    public string ViewId { get; }
    public long FolderItemId { get; }
    public string Type { get; }
    public string Name { get; }
    public string SortName { get; }
}

sealed class TemporaryDirectory : IDisposable
{
    public TemporaryDirectory()
    {
        Path = System.IO.Path.Combine(
            System.IO.Path.GetTempPath(),
            "ecm-managed-series-test-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(Path);
    }

    public string Path { get; }

    public void Dispose()
    {
        Directory.Delete(Path, true);
    }
}
