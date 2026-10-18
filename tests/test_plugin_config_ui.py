import unittest
from pathlib import Path


class PluginConfigurationPageTests(unittest.TestCase):
    def test_multi_select_uses_emby_values_and_checks_server_readback(self):
        script = (
            Path(__file__).resolve().parents[1]
            / 'plugins'
            / 'EmbyCollectionManagerStrmBridge'
            / 'configurationPage.js'
        ).read_text(encoding='utf-8')

        self.assertIn("typeof select.getValues === 'function'", script)
        self.assertIn("select.setValues(selected, false)", script)
        self.assertIn("selected.indexOf('*') >= 0 ? ['*'] : selected", script)
        self.assertIn("ApiClient.updatePluginConfiguration(pluginId, config)", script)
        self.assertIn("ApiClient.getPluginConfiguration(pluginId)", script)
        self.assertIn("sameValues(saved.MovieHomeSectionUserIds, movieUserIds)", script)
        self.assertIn("sameValues(saved.TvHomeSectionUserIds, tvUserIds)", script)


if __name__ == '__main__':
    unittest.main()
