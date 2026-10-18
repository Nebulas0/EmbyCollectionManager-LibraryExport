using System.Xml.Serialization;
using EmbyCollectionManagerStrmBridge;
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

static void RunHomeSectionProjectionTests()
{
    var movieViews = new[] { "movies-selected", "movies-other", "tv-selected", "tv-other" };
    var movies = Enumerable.Range(1, 14)
        .Select(index => new TestItem("movies-selected", "Movie", $"Movie {index:D2}", $"M-{index:D2}"))
        .Concat(Enumerable.Range(1, 4)
            .Select(index => new TestItem("movies-other", "Movie", $"Foreign Movie {index:D2}", $"A-{index:D2}")))
        .ToList();
    var movieSection = HomeSectionBuilder.Build(
        "movies-selected", "My Existing Movie Library", "movies", "SortName", "Ascending",
        "Primary", 10, movieViews);
    var movieQuery = (HomeSectionItemsQuery)movieSection.Query;
    var movieResults = Project(movieSection, movies);
    Assert(movieSection.CustomName == "My Existing Movie Library", "Movie row title must use the existing library name.");
    Assert(movieSection.ParentId == "movies-selected", "Movie row must retain its selected library.");
    Assert(movieSection.ItemTypes.SequenceEqual(new[] { "Movie" }), "Movie row must filter to Movie items.");
    Assert(movieSection.ExcludedFolders.Contains("movies-other"), "Movie row must exclude the other movie library.");
    Assert(!movieSection.ExcludedFolders.Contains("movies-selected"), "Movie row must not exclude its selected library.");
    Assert(movieSection.SortBy == "SortName" && movieSection.SortOrder == "Ascending", "Movie sort settings must be applied.");
    Assert(movieQuery.ParentId == "movies-selected" &&
           movieQuery.IncludeItemTypes.SequenceEqual(new[] { "Movie" }) &&
           movieQuery.ExcludeUserViewIds.Contains("movies-other") &&
           movieQuery.Limit == 10,
        "Movie query must explicitly scope the selected view, type, exclusions, and limit.");
    AssertOptionalSectionLimit(movieSection, 10);
    Assert(movieResults.Count == 10, "Movie row must respect its configured limit.");
    Assert(movieResults.All(item => item.LibraryId == "movies-selected" && item.Type == "Movie"),
        "Movie row must contain only items from the selected movie library.");
    Assert(movieResults.Select(item => item.SortName).SequenceEqual(
        movieResults.Select(item => item.SortName).OrderBy(value => value, StringComparer.Ordinal)),
        "Movie row must use ascending SortName order.");

    var shows = Enumerable.Range(1, 13)
        .Select(index => new TestItem("tv-selected", "Series", $"Show {index:D2}", $"S-{index:D2}"))
        .Concat(Enumerable.Range(1, 5)
            .Select(index => new TestItem("tv-other", "Series", $"Foreign Show {index:D2}", $"Z-{index:D2}")))
        .Concat(new[] { new TestItem("tv-selected", "Movie", "Wrong Type", "ZZ") })
        .ToList();
    var showSection = HomeSectionBuilder.Build(
        "tv-selected", "My Existing TV Library", "tv_shows", "SortName", "Descending",
        "Thumb", 10, movieViews);
    var showQuery = (HomeSectionItemsQuery)showSection.Query;
    var showResults = Project(showSection, shows);
    Assert(showSection.CustomName == "My Existing TV Library", "TV row title must use the existing library name.");
    Assert(showSection.ParentId == "tv-selected", "TV row must retain its selected library.");
    Assert(showSection.ItemTypes.SequenceEqual(new[] { "Series" }), "TV row must filter to Series items.");
    Assert(showSection.ExcludedFolders.Contains("tv-other"), "TV row must exclude the other TV library.");
    Assert(!showSection.ExcludedFolders.Contains("tv-selected"), "TV row must not exclude its selected library.");
    Assert(showSection.SortBy == "SortName" && showSection.SortOrder == "Descending", "TV sort settings must be applied.");
    Assert(showQuery.ParentId == "tv-selected" &&
           showQuery.IncludeItemTypes.SequenceEqual(new[] { "Series" }) &&
           showQuery.ExcludeUserViewIds.Contains("tv-other") &&
           showQuery.Limit == 10,
        "TV query must explicitly scope the selected view, type, exclusions, and limit.");
    AssertOptionalSectionLimit(showSection, 10);
    Assert(showResults.Count == 10, "TV row must respect its configured limit.");
    Assert(showResults.All(item => item.LibraryId == "tv-selected" && item.Type == "Series"),
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
        .Where(item => string.Equals(item.LibraryId, query.ParentId, StringComparison.OrdinalIgnoreCase))
        .Where(item => section.ItemTypes.Contains(item.Type, StringComparer.OrdinalIgnoreCase))
        .Where(item => query.IncludeItemTypes.Contains(item.Type, StringComparer.OrdinalIgnoreCase))
        .Where(item => !query.ExcludeUserViewIds.Contains(item.LibraryId, StringComparer.OrdinalIgnoreCase));
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

sealed class TestItem
{
    public TestItem(string libraryId, string type, string name, string sortName)
    {
        LibraryId = libraryId;
        Type = type;
        Name = name;
        SortName = sortName;
    }

    public string LibraryId { get; }
    public string Type { get; }
    public string Name { get; }
    public string SortName { get; }
}
