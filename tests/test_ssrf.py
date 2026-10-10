import ipaddress
import socket
import unittest
from unittest.mock import AsyncMock, patch
import aiohttp
from tests.support import webcheck as w


def address(ip):
    return dict(hostname="example.com", host=ip, port=443,
                family=socket.AF_INET6 if ":" in ip else socket.AF_INET,
                proto=0, flags=0)


class AddressTests(unittest.TestCase):
    def test_shared_and_mapped_ranges_rejected(self):
        for value in ("100.64.0.1", "127.0.0.1", "192.168.0.1", "224.0.0.1",
                      "::1", "fe80::1", "::ffff:127.0.0.1", "::ffff:100.64.0.1"):
            with self.subTest(value=value):
                self.assertTrue(w._is_reserved_ip(ipaddress.ip_address(value)))
        self.assertFalse(w._is_reserved_ip(ipaddress.ip_address("8.8.8.8")))


class ConnectionAddressTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.plugin = w.WebCheckPlugin(None, {})
        self.session = await self.plugin._get_session()

    async def asyncTearDown(self):
        await self.plugin.terminate()

    async def test_literal_is_checked_by_actual_connector(self):
        with self.assertRaises(aiohttp.ClientError):
            await self.session.connector._resolve_host("127.0.0.1", 80)

    async def test_mixed_dns_addresses_rejected(self):
        with patch.object(aiohttp.TCPConnector, "_resolve_host", AsyncMock(
                return_value=[address("8.8.8.8"), address("127.0.0.1")])):
            with self.assertRaises(aiohttp.ClientError):
                await self.session.connector._resolve_host("example.com", 443)

    async def test_changed_or_cached_resolution_cannot_skip_check(self):
        with patch.object(aiohttp.TCPConnector, "_resolve_host", AsyncMock(
                side_effect=[[address("8.8.8.8")], [address("192.168.1.1")]])):
            result = await self.session.connector._resolve_host("example.com", 443)
            self.assertEqual(result[0]["hostname"], "example.com")
            self.assertEqual(result[0]["host"], "8.8.8.8")
            with self.assertRaises(aiohttp.ClientError):
                await self.session.connector._resolve_host("example.com", 443)

    async def test_dns_failure_and_empty_results_are_errors(self):
        with patch.object(aiohttp.TCPConnector, "_resolve_host", AsyncMock(side_effect=OSError("DNS"))):
            with self.assertRaises(OSError):
                await self.session.connector._resolve_host("example.com", 443)
        with patch.object(aiohttp.TCPConnector, "_resolve_host", AsyncMock(return_value=[])):
            with self.assertRaises(OSError):
                await self.session.connector._resolve_host("example.com", 443)
