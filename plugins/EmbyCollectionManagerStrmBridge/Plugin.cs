using System;
using MediaBrowser.Common.Configuration;
using MediaBrowser.Common.Plugins;
using MediaBrowser.Model.Plugins;
using MediaBrowser.Model.Serialization;

namespace EmbyCollectionManagerStrmBridge
{
    public sealed class Plugin : BasePlugin<PluginConfiguration>
    {
        public Plugin(IApplicationPaths applicationPaths, IXmlSerializer xmlSerializer)
            : base(applicationPaths, xmlSerializer)
        {
        }

        public override string Name => "Emby Collection Manager STRM Bridge";

        public override Guid Id => new Guid("3d9e8cda-9217-4ca9-9353-8c89adda7789");

        public override string Description =>
            "Applies Emby media information and ranked sort names to manager-owned STRM copies.";
    }

    public sealed class PluginConfiguration : BasePluginConfiguration
    {
    }
}
