from __future__ import annotations

import unittest
import os
from unittest.mock import patch
from unittest.mock import ANY

from httpx_socks import AsyncProxyTransport
from telegram.ext import Application

from bestgift_bot.network import check_network_configuration, create_request


def proxy_environment(values):
    system = {key: value for key, value in os.environ.items() if key.upper() in {"SYSTEMROOT", "WINDIR"}}
    return patch.dict("os.environ", {**system, **values}, clear=True)


class NetworkConfiguration(unittest.IsolatedAsyncioTestCase):
    async def test_socks4_system_proxy_builds_application_for_both_clients(self):
        # Точное воспроизведение ошибки на скриншоте без подключения к сети.
        with proxy_environment({"ALL_PROXY": "socks4://127.0.0.1:10808"}):
            request = create_request()
            polling_request = create_request(polling=True)
            try:
                app = (Application.builder().token("123456789:abcdefghijklmnopqrstuvwxyz123456789")
                       .request(request).get_updates_request(polling_request).build())
                self.assertIs(app.bot.request, request)
                self.assertIsInstance(request._client._transport, AsyncProxyTransport)
                self.assertIsInstance(polling_request._client._transport, AsyncProxyTransport)
                self.assertFalse(request._client_kwargs["trust_env"])
                self.assertEqual(polling_request._client_kwargs["limits"].max_connections, 1)
            finally:
                await request.shutdown()
                await polling_request.shutdown()

    async def test_socks5_and_remote_dns_variants(self):
        for scheme in ("socks5", "socks5h", "socks4a"):
            with self.subTest(scheme=scheme):
                with proxy_environment({"HTTPS_PROXY": f"{scheme}://127.0.0.1:10808"}):
                    request = create_request()
                    try:
                        self.assertIsInstance(request._client._transport, AsyncProxyTransport)
                    finally:
                        await request.shutdown()

    async def test_https_proxy_takes_precedence_over_all_proxy(self):
        with proxy_environment({
            "HTTPS_PROXY": "socks5://127.0.0.1:10809",
            "ALL_PROXY": "socks4://127.0.0.1:10808",
        }):
            with patch("bestgift_bot.network.AsyncProxyTransport.from_url", wraps=AsyncProxyTransport.from_url) as factory:
                request = create_request()
                try:
                    factory.assert_called_once_with("socks5://127.0.0.1:10809", rdns=True, verify=ANY)
                finally:
                    await request.shutdown()

    async def test_no_proxy_exclusion_is_respected(self):
        with proxy_environment({
            "ALL_PROXY": "socks4://127.0.0.1:10808", "NO_PROXY": "api.telegram.org",
        }):
            request = create_request()
            try:
                self.assertNotIsInstance(request._client._transport, AsyncProxyTransport)
                self.assertFalse(request._client_kwargs["trust_env"])
            finally:
                await request.shutdown()

    async def test_http_proxy_keeps_native_httpx_transport(self):
        with proxy_environment({
            "HTTPS_PROXY": "http://127.0.0.1:10808",
            "ALL_PROXY": "socks4://127.0.0.1:10809",
        }):
            request = create_request()
            try:
                self.assertNotIsInstance(request._client._transport, AsyncProxyTransport)
                self.assertEqual(request._client_kwargs["proxy"], "http://127.0.0.1:10808")
            finally:
                await request.shutdown()

    async def test_check_validates_socks4_without_api_calls(self):
        with proxy_environment({"ALL_PROXY": "socks4://127.0.0.1:10808"}):
            with patch("telegram.request.HTTPXRequest.do_request", side_effect=AssertionError("HTTP request attempted")):
                await check_network_configuration()


if __name__ == "__main__":
    unittest.main()
