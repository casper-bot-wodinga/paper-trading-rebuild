"""Tests for src/gpu_client.py's hostname resolution helper.

No real gRPC connection here — that's exercised manually against the live
worker, not in CI. Just the pure resolve-before-connect logic, since gRPC's
own channel resolver doesn't reliably handle local-network hostnames
(confirmed 2026-07-24 against legend-of-macs.local)."""
import socket
import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC_DIR))

import gpu_client  # noqa: E402


class TestResolveHostname:
    def test_bare_ip_passed_through_unchanged(self):
        assert gpu_client._resolve_hostname("192.168.1.190:5002") == "192.168.1.190:5002"

    def test_hostname_resolved_via_getaddrinfo(self, monkeypatch):
        def fake_getaddrinfo(host, port):
            assert host == "some-host.local"
            assert port == 5002
            return [(2, 1, 6, "", ("10.0.0.5", 5002))]
        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
        assert gpu_client._resolve_hostname("some-host.local:5002") == "10.0.0.5:5002"

    def test_custom_dns_suffix_also_resolved(self, monkeypatch):
        """Not just .local — any hostname suffix (e.g. .klo) goes through
        the same resolution path."""
        def fake_getaddrinfo(host, port):
            return [(2, 1, 6, "", ("10.0.0.7", 5002))]
        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
        assert gpu_client._resolve_hostname("imac.klo:5002") == "10.0.0.7:5002"

    def test_resolution_failure_falls_back_to_original_address(self, monkeypatch):
        def fake_getaddrinfo(host, port):
            raise socket.gaierror("lookup failed")
        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
        assert gpu_client._resolve_hostname("unreachable.local:5002") == "unreachable.local:5002"

    def test_empty_host_passed_through(self):
        assert gpu_client._resolve_hostname(":5002") == ":5002"
