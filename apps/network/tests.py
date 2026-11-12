import base64
import json
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, override_settings

from apps.network.onboarding import install_script
from apps.network.tunguard import register_router, server_public_key
from core.crypto import encrypt

TEST_KEY = "ZMD6DO1R8ZmXwA-qxTiemkgDNI3ZqSWbaSyoZWCSe5Q="


@override_settings(TUNGARD_API_URL="http://tunguard.test:9000", TUNGARD_API_KEY="test-api-key")
class TunGuardClientTests(SimpleTestCase):
    @override_settings(
        FIELD_ENCRYPTION_KEY=TEST_KEY,
        ROUTER_TUNNEL_HOST="vpn.pingota.example",
        ROUTER_TUNNEL_PORT=13231,
        ROUTER_TUNNEL_SUBNET="10.70.0.0/16",
        PUBLIC_BASE_URL="https://pingota.example",
    )
    @mock.patch("apps.network.tunguard.server_public_key", return_value="AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=")
    def test_install_script_uses_registered_peer_keys_and_verifies_tunnel(self, _server_key):
        router = SimpleNamespace(
            tunnel_public_key=base64.b64encode(bytes(range(32))).decode("ascii"),
            tunnel_private_key=encrypt("router-private-key"),
            tunnel_address="10.70.0.2",
            install_token="one-time-token",
            api_user="pingota-ab12",
            api_password=encrypt("router-api-password"),
            name="AP North",
            isp=SimpleNamespace(name="North ISP"),
        )

        script = install_script(router)

        self.assertIn('private-key="router-private-key"', script)
        self.assertIn('public-key="AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="', script)
        self.assertIn('allowed-address="10.70.0.1/32"', script)
        self.assertIn("/ping address=\"10.70.0.1\"", script)
        self.assertIn("one-time-token", script)

    @mock.patch("apps.network.tunguard.urllib.request.urlopen")
    def test_register_router_uses_hex_key_and_single_host_route(self, urlopen):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"success":true}'
        urlopen.return_value = response
        public_key = base64.b64encode(bytes(range(32))).decode("ascii")
        router = SimpleNamespace(
            tunnel_public_key=public_key,
            tunnel_address="10.70.0.2",
            name="AP North",
            isp=SimpleNamespace(name="North ISP"),
        )

        register_router(router)

        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://tunguard.test:9000/api/peer/add")
        self.assertEqual(request.get_header("X-api-key"), "test-api-key")
        self.assertEqual(
            json.loads(request.data),
            {
                "public_key": bytes(range(32)).hex(),
                "allowed_ip": "10.70.0.2/32",
                "device_name": "Pingota: North ISP / AP North",
            },
        )

    @mock.patch("apps.network.tunguard.urllib.request.urlopen")
    def test_server_key_is_converted_from_tunguard_hex_to_routeros_base64(self, urlopen):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = (
            b'{"public_key":"000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"}'
        )
        urlopen.return_value = response

        self.assertEqual(server_public_key(), base64.b64encode(bytes(range(32))).decode("ascii"))
