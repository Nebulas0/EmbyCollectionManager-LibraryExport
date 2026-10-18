using System.Xml.Serialization;
using EmbyCollectionManagerStrmBridge;

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
