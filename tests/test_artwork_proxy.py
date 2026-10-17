import unittest
from unittest.mock import Mock, patch

from web import app as web_app


class ArtworkProxyTests(unittest.TestCase):
    def setUp(self):
        web_app.app.config['TESTING'] = True
        self.client = web_app.app.test_client()
        self.config = {
            'emby': {
                'server_url': 'http://emby.internal:8096',
                'api_key': 'admin-token',
                'user_id': 'admin-user',
            },
        }

    def test_artwork_grid_uses_same_origin_image_proxy(self):
        emby = Mock()
        emby.get_all_collections.return_value = [{
            'Id': '12345',
            'Name': 'Test Collection',
            'ChildCount': 2,
        }]
        with patch.object(web_app, 'load_config', return_value=self.config), patch(
            'src.emby_client.EmbyClient',
            return_value=emby,
        ):
            response = self.client.get('/artwork')

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'/api/collection_image/12345/Primary', response.data)
        self.assertIn(b'2 items', response.data)
        self.assertNotIn(b'admin-token', response.data)
        self.assertNotIn(b'http://emby.internal:8096', response.data)

    def test_proxy_streams_image_with_server_side_auth(self):
        emby = Mock()
        emby.server_url = 'http://emby.internal:8096'
        emby.session.get.return_value = Mock(
            status_code=200,
            content=b'image-bytes',
            headers={'Content-Type': 'image/jpeg'},
        )
        with patch.object(web_app, 'load_config', return_value=self.config), patch(
            'src.emby_client.EmbyClient',
            return_value=emby,
        ):
            response = self.client.get('/api/collection_image/12345/Primary')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, 'image/jpeg')
        self.assertEqual(response.data, b'image-bytes')
        emby.session.get.assert_called_once_with(
            'http://emby.internal:8096/Items/12345/Images/Primary',
            timeout=20,
        )

    def test_proxy_rejects_invalid_image_types_and_ids(self):
        self.assertEqual(self.client.get('/api/collection_image/12345/Chapter').status_code, 400)
        self.assertEqual(self.client.get('/api/collection_image/not%21valid/Primary').status_code, 400)
        self.assertEqual(self.client.get('/api/collection_image/../../etc/Primary').status_code, 404)


if __name__ == '__main__':
    unittest.main()
