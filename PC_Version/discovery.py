"""
Arc Flash LAN Discovery & QR Pairing Payload Generator for PC.

Protocol parity with Android's NsdDiscovery.kt & QrPayloadCodec.kt:
- UDP Broadcast & Listener on port 45890 every 2.0s (TTL 12.0s)
  Beacon JSON: {"v":1,"did":"<uuid4>","name":"<deviceName>","port":<53317>}
- Self-device filtering & cross-transport peer deduplication (PeerFilter)
- Optional mDNS (_arcflash._tcp.local.) when `zeroconf` is installed; zero external dependency required
- Compact QR pairing payload generator ({"v":1,"did":"...","tok":"...","exp":...}) — zero IP/port/secrets
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import secrets
import selectors
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, replace
from typing import Callable, Optional

from arc_protocol import SERVICE_PREFIX, SERVICE_TYPE, UDP_DISCOVERY_PORT

QR_TTL_MS = 60_000
UDP_BEACON_INTERVAL_S = 2.0
UDP_PEER_TTL_S = 12.0

_UUID_V4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")


@dataclass(frozen=True)
class DiscoveredPeer:
    device_id: str
    display_name: str
    service_name: str
    host_address: str | None = None
    port: int | None = None
    last_seen: float = 0.0

    @property
    def is_resolved(self) -> bool:
        return bool(self.host_address and self.host_address.strip()) and (self.port or 0) > 0


@dataclass(frozen=True)
class LanStatus:
    is_lan_connected: bool = False
    transport_label: str = "Offline"
    local_ip_address: str | None = None
    is_advertising: bool = False
    is_discovering: bool = False


class QrPayloadCodec:
    """Exact parity with Android QrPayloadCodec.kt for Show QR pairing."""

    @staticmethod
    def new_device_id() -> str:
        return str(uuid.uuid4()).lower()

    @staticmethod
    def generate_token_hex32() -> str:
        return secrets.token_hex(16)

    @staticmethod
    def create_payload(device_id: str, now_ms: int | None = None) -> tuple[str, int]:
        did = device_id.strip().lower()
        if not _UUID_V4_RE.match(did):
            did = str(uuid.uuid4()).lower()
        now = int(time.time() * 1000) if now_ms is None else now_ms
        expires_at = now + QR_TTL_MS
        token = QrPayloadCodec.generate_token_hex32()
        payload = f'{{"v":1,"did":"{did}","tok":"{token}","exp":{expires_at}}}'
        return payload, expires_at

    @staticmethod
    def validate_payload(raw: str, now_ms: int | None = None) -> dict | None:
        try:
            obj = json.loads(raw.strip())
            if not isinstance(obj, dict) or set(obj.keys()) != {"v", "did", "tok", "exp"}:
                return None
            if obj.get("v") != 1:
                return None
            did = str(obj.get("did", "")).lower()
            tok = str(obj.get("tok", ""))
            exp = int(obj.get("exp", 0))
            if not _UUID_V4_RE.match(did) or not _HEX32_RE.match(tok):
                return None
            now = int(time.time() * 1000) if now_ms is None else now_ms
            if now >= exp:
                return None
            return {"v": 1, "did": did, "tok": tok, "exp": exp}
        except Exception:
            return None


class PeerFilter:
    """Pure, deterministic self-device filter & peer deduplicator matching NsdDiscovery.kt."""

    @staticmethod
    def normalize_ip(raw: str | None) -> str | None:
        if not raw:
            return None
        trimmed = raw.strip()
        if not trimmed:
            return None
        return trimmed.split("%", 1)[0].lower()

    @staticmethod
    def is_loopback_or_invalid(raw_ip: str | None) -> bool:
        ip = PeerFilter.normalize_ip(raw_ip)
        if not ip or ip in ("localhost", "::1", "0:0:0:0:0:0:0:1", "0.0.0.0", "::"):
            return True
        if ip.startswith("127."):
            return True
        try:
            addr = ipaddress.ip_address(ip)
            return addr.is_loopback or addr.is_unspecified or addr.is_multicast
        except ValueError:
            return True

    @staticmethod
    def is_self_service_name(
        service_name: str | None,
        local_device_id: str,
        registered_service_name: str | None,
    ) -> bool:
        candidate = (service_name or "").strip().lower()
        if not candidate:
            return False
        reg = (registered_service_name or "").strip().lower()
        if reg and (candidate == reg or candidate.startswith(reg + " ")):
            return True
        clean_did = local_device_id.strip().lower()
        if not clean_did:
            return False
        short_did = clean_did[:8]
        base = f"{SERVICE_PREFIX}{short_did}"
        return (
            candidate == base
            or candidate.startswith(base + " ")
            or candidate.startswith(base + "(")
            or candidate.startswith(base + "\\032")
        )

    @staticmethod
    def is_remote_lan_address(raw_ip: str | None, local_addresses: set[str]) -> bool:
        norm = PeerFilter.normalize_ip(raw_ip)
        if not norm or PeerFilter.is_loopback_or_invalid(norm):
            return False
        norm_locals = {x for x in (PeerFilter.normalize_ip(a) for a in local_addresses) if x}
        return norm not in norm_locals

    @staticmethod
    def is_self_peer(
        peer: DiscoveredPeer,
        local_device_id: str,
        registered_service_name: str | None,
        local_addresses: set[str],
    ) -> bool:
        clean_did = local_device_id.strip().lower()
        peer_did = peer.device_id.strip().lower()
        if clean_did and (peer_did == clean_did or peer_did == clean_did[:8]):
            return True
        if PeerFilter.is_self_service_name(peer.service_name, local_device_id, registered_service_name):
            return True
        peer_ip = PeerFilter.normalize_ip(peer.host_address)
        if peer_ip:
            if PeerFilter.is_loopback_or_invalid(peer_ip):
                return True
            norm_locals = {x for x in (PeerFilter.normalize_ip(a) for a in local_addresses) if x}
            if peer_ip in norm_locals:
                return True
        return False

    @staticmethod
    def _is_same_peer(a: DiscoveredPeer, b: DiscoveredPeer) -> bool:
        a_did = a.device_id.strip().lower()
        b_did = b.device_id.strip().lower()
        if a_did and b_did:
            if a_did == b_did:
                return True
            if (len(a_did) == 8 and b_did.startswith(a_did)) or (len(b_did) == 8 and a_did.startswith(b_did)):
                return True
        a_svc = a.service_name.strip().lower()
        b_svc = b.service_name.strip().lower()
        if a_svc and a_svc == b_svc:
            return True
        a_host = PeerFilter.normalize_ip(a.host_address)
        b_host = PeerFilter.normalize_ip(b.host_address)
        if a_host and b_host and a_host == b_host:
            if a.port == b.port or a.port is None or b.port is None:
                return True
        return False

    @staticmethod
    def _merge_peer(existing: DiscoveredPeer, incoming: DiscoveredPeer) -> DiscoveredPeer:
        best_did = incoming.device_id if len(incoming.device_id) >= len(existing.device_id) else existing.device_id
        best_name = (
            incoming.display_name
            if incoming.display_name.strip() and not incoming.display_name.startswith("Device ")
            else (existing.display_name or incoming.display_name)
        )
        a_host = PeerFilter.normalize_ip(existing.host_address)
        b_host = PeerFilter.normalize_ip(incoming.host_address)
        if not a_host:
            best_host = b_host
        elif not b_host:
            best_host = a_host
        elif ("." in b_host and ":" not in b_host) and not ("." in a_host and ":" not in a_host):
            best_host = b_host
        else:
            best_host = b_host
        best_port = incoming.port if (incoming.port or 0) > 0 else existing.port
        return DiscoveredPeer(
            device_id=best_did,
            display_name=best_name,
            service_name=incoming.service_name or existing.service_name,
            host_address=best_host,
            port=best_port,
            last_seen=max(existing.last_seen, incoming.last_seen),
        )

    @staticmethod
    def filter_and_deduplicate(
        peers: list[DiscoveredPeer],
        local_device_id: str,
        registered_service_name: str | None,
        local_addresses: set[str],
        require_remote_address: bool = False,
    ) -> list[DiscoveredPeer]:
        result: list[DiscoveredPeer] = []
        for raw in peers:
            norm_host = PeerFilter.normalize_ip(raw.host_address)
            peer = replace(raw, host_address=norm_host)
            if PeerFilter.is_self_peer(peer, local_device_id, registered_service_name, local_addresses):
                continue
            if peer.host_address and not PeerFilter.is_remote_lan_address(peer.host_address, local_addresses):
                continue
            if require_remote_address and not peer.is_resolved:
                continue
            matched_idx = next((i for i, e in enumerate(result) if PeerFilter._is_same_peer(e, peer)), -1)
            if matched_idx >= 0:
                result[matched_idx] = PeerFilter._merge_peer(result[matched_idx], peer)
            else:
                result.append(peer)
        return result


def collect_local_ipv4_and_broadcasts() -> tuple[set[str], list[str], str | None]:
    """
    Enumerates local non-loopback IPv4 addresses and subnet broadcast targets cleanly
    using Python stdlib + OS network interface commands (zero hard external deps).
    """
    local_ips: set[str] = {"127.0.0.1", "::1"}
    broadcasts: set[str] = {"255.255.255.255"}
    primary_ipv4: str | None = None

    # Probe active outbound interface via UDP connect (sends 0 packets)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 80))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                local_ips.add(ip)
                primary_ipv4 = ip
    except Exception:
        pass

    # Inspect host addresses from getaddrinfo
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            addr = info[4][0].split("%", 1)[0]
            if addr:
                local_ips.add(addr.lower())
                if primary_ipv4 is None and "." in addr and not addr.startswith("127."):
                    primary_ipv4 = addr
    except Exception:
        pass

    # Parse OS subnet masks/broadcasts where available for reliable hotspot / multi-NIC discovery
    try:
        if sys.platform.startswith("linux"):
            out = subprocess.check_output(["ip", "-4", "addr", "show"], text=True, timeout=1.5)
            for match in re.finditer(r"inet\s+(\d+\.\d+\.\d+\.\d+)/(\d+)(?:.*?brd\s+(\d+\.\d+\.\d+\.\d+))?", out):
                ip, prefix, brd = match.group(1), int(match.group(2)), match.group(3)
                if not ip.startswith("127."):
                    local_ips.add(ip)
                    if primary_ipv4 is None:
                        primary_ipv4 = ip
                    if brd:
                        broadcasts.add(brd)
                    else:
                        net = ipaddress.IPv4Network(f"{ip}/{prefix}", strict=False)
                        broadcasts.add(str(net.broadcast_address))
        elif sys.platform == "darwin":
            out = subprocess.check_output(["ifconfig"], text=True, timeout=1.5)
            for match in re.finditer(
                r"inet\s+(\d+\.\d+\.\d+\.\d+)\s+netmask\s+0x([0-9a-fA-F]+)(?:\s+broadcast\s+(\d+\.\d+\.\d+\.\d+))?",
                out,
            ):
                ip, mask_hex, brd = match.group(1), match.group(2), match.group(3)
                if not ip.startswith("127."):
                    local_ips.add(ip)
                    if primary_ipv4 is None:
                        primary_ipv4 = ip
                    if brd:
                        broadcasts.add(brd)
                    else:
                        mask_ip = str(ipaddress.IPv4Address(int(mask_hex, 16)))
                        net = ipaddress.IPv4Network(f"{ip}/{mask_ip}", strict=False)
                        broadcasts.add(str(net.broadcast_address))
    except Exception:
        pass

    # Fallback /24 subnet broadcast for detected primary IPv4
    if primary_ipv4:
        try:
            net = ipaddress.IPv4Network(f"{primary_ipv4}/24", strict=False)
            broadcasts.add(str(net.broadcast_address))
        except Exception:
            pass

    return local_ips, sorted(broadcasts), primary_ipv4


class LanDiscoveryService:
    """
    Runs UDP LAN discovery (45890) + optional mDNS (_arcflash._tcp.local.) to find both
    Android Arc Flash devices and other PC instances automatically.
    """

    def __init__(
        self,
        device_id_provider: Callable[[], str],
        device_name_provider: Callable[[], str],
        listen_port_provider: Callable[[], int],
        on_peers_changed: Callable[[list[DiscoveredPeer]], None] | None = None,
    ) -> None:
        self._did_provider = device_id_provider
        self._name_provider = device_name_provider
        self._port_provider = listen_port_provider
        self._on_peers_changed = on_peers_changed

        self._lock = threading.RLock()
        self._running = False
        self._udp_sock: socket.socket | None = None
        self._udp_wake_r: socket.socket | None = None
        self._udp_wake_w: socket.socket | None = None
        self._threads: list[threading.Thread] = []
        self._peers: dict[str, DiscoveredPeer] = {}
        self._registered_service_name: str | None = None

        self._zeroconf = None
        self._zc_info = None
        self._zc_browser = None

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True

        did = self._did_provider()
        self._registered_service_name = f"{SERVICE_PREFIX}{did[:8]}" if did else None

        self._open_udp_socket()
        wake_r, wake_w = socket.socketpair()
        wake_r.setblocking(False)
        wake_w.setblocking(False)
        self._udp_wake_r = wake_r
        self._udp_wake_w = wake_w
        t_rx = threading.Thread(target=self._udp_listen_loop, name="arc-udp-rx", daemon=True)
        t_tx = threading.Thread(target=self._udp_beacon_loop, name="arc-udp-tx", daemon=True)
        t_rx.start()
        t_tx.start()
        self._threads = [t_rx, t_tx]
        self._try_start_zeroconf()

    def stop(self) -> None:
        with self._lock:
            self._running = False
            sock = self._udp_sock
            self._udp_sock = None
            threads = list(self._threads)
            self._threads.clear()
            wake_r = self._udp_wake_r
            wake_w = self._udp_wake_w
            self._udp_wake_r = None
            self._udp_wake_w = None
        if wake_w is not None:
            try:
                wake_w.send(b"\x01")
            except OSError:
                pass
        for t in threads:
            if t.is_alive():
                t.join(timeout=1.5)
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        for w in (wake_r, wake_w):
            if w is not None:
                try:
                    w.close()
                except OSError:
                    pass
        self._stop_zeroconf()

    def get_lan_status(self) -> LanStatus:
        local_ips, _, primary_ipv4 = collect_local_ipv4_and_broadcasts()
        has_lan = primary_ipv4 is not None or any(
            not PeerFilter.is_loopback_or_invalid(ip) for ip in local_ips
        )
        return LanStatus(
            is_lan_connected=has_lan,
            transport_label="Wi-Fi / LAN" if has_lan else "Offline",
            local_ip_address=primary_ipv4,
            is_advertising=self._running and has_lan,
            is_discovering=self._running and has_lan,
        )

    def get_peers(self) -> list[DiscoveredPeer]:
        now = time.monotonic()
        local_ips, _, _ = collect_local_ipv4_and_broadcasts()
        did = self._did_provider()
        with self._lock:
            alive = [
                p
                for p in self._peers.values()
                if (now - p.last_seen) <= UDP_PEER_TTL_S or p.last_seen == 0.0
            ]
            filtered = PeerFilter.filter_and_deduplicate(
                peers=alive,
                local_device_id=did,
                registered_service_name=self._registered_service_name,
                local_addresses=local_ips,
                require_remote_address=True,
            )
            return filtered

    def find_peer(self, device_id: str) -> Optional[DiscoveredPeer]:
        target = device_id.strip().lower()
        for p in self.get_peers():
            if p.device_id.strip().lower() == target:
                return p
        return None

    def register_manual_or_incoming_peer(self, peer_id: str, name: str, host: str, port: int) -> None:
        if not peer_id or not host or port <= 0:
            return
        local_ips, _, _ = collect_local_ipv4_and_broadcasts()
        peer = DiscoveredPeer(
            device_id=peer_id.strip(),
            display_name=name.strip() or f"Device {peer_id[:8]}",
            service_name=f"{SERVICE_PREFIX}{peer_id[:8]}",
            host_address=host,
            port=port,
            last_seen=time.monotonic(),
        )
        if PeerFilter.is_self_peer(peer, self._did_provider(), self._registered_service_name, local_ips):
            return
        with self._lock:
            self._peers[peer.device_id.lower()] = peer
        if self._on_peers_changed:
            self._on_peers_changed(self.get_peers())

    def force_refresh(self) -> None:
        self._send_beacon_once()

    def _open_udp_socket(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(1.0)
        try:
            sock.bind(("", UDP_DISCOVERY_PORT))
        except OSError:
            # Fallback to ephemeral UDP port for sending beacons if 45890 is held by another local instance
            sock.bind(("", 0))
        with self._lock:
            self._udp_sock = sock

    def _send_beacon_once(self) -> None:
        port = self._port_provider()
        did = self._did_provider()
        name = self._name_provider()
        if port <= 0 or not did:
            return
        sock = self._udp_sock
        if sock is None:
            return
        payload = json.dumps(
            {"v": 1, "did": did, "name": name, "port": port},
            separators=(",", ":"),
        ).encode("utf-8")
        _, broadcasts, _ = collect_local_ipv4_and_broadcasts()
        for target_ip in broadcasts:
            try:
                sock.sendto(payload, (target_ip, UDP_DISCOVERY_PORT))
            except OSError:
                pass

    def _udp_beacon_loop(self) -> None:
        while self._running:
            self._send_beacon_once()
            self._prune_expired()
            deadline = time.monotonic() + UDP_BEACON_INTERVAL_S
            while self._running and time.monotonic() < deadline:
                time.sleep(0.25)

    def _udp_listen_loop(self) -> None:
        sock = self._udp_sock
        wake_r = self._udp_wake_r
        if sock is None:
            return
        sel = selectors.DefaultSelector()
        try:
            sock.setblocking(False)
            sel.register(sock, selectors.EVENT_READ, data="udp")
            if wake_r is not None:
                sel.register(wake_r, selectors.EVENT_READ, data="wake")
            while self._running:
                try:
                    events = sel.select(timeout=0.5)
                except OSError:
                    break
                if not self._running:
                    break
                for key, _ in events:
                    if key.data == "wake":
                        return
                    if key.data == "udp":
                        while self._running:
                            try:
                                data, addr = sock.recvfrom(2048)
                            except BlockingIOError:
                                break
                            except OSError:
                                return
                            self._handle_udp_packet(data, addr[0])
        finally:
            try:
                sel.close()
            except Exception:
                pass

    def _handle_udp_packet(self, data: bytes, sender_ip: str) -> None:
        try:
            obj = json.loads(data.decode("utf-8", errors="ignore"))
        except Exception:
            return
        if not isinstance(obj, dict):
            return
        peer_did = str(obj.get("did", "")).strip()
        peer_name = str(obj.get("name", "")).strip() or f"Device {peer_did[:8]}"
        peer_port = int(obj.get("port", 0) or 0)
        if not peer_did or peer_port <= 0:
            return

        local_ips, _, _ = collect_local_ipv4_and_broadcasts()
        if not PeerFilter.is_remote_lan_address(sender_ip, local_ips):
            return

        peer = DiscoveredPeer(
            device_id=peer_did,
            display_name=peer_name,
            service_name=f"{SERVICE_PREFIX}{peer_did[:8]}",
            host_address=sender_ip,
            port=peer_port,
            last_seen=time.monotonic(),
        )
        if PeerFilter.is_self_peer(peer, self._did_provider(), self._registered_service_name, local_ips):
            return

        with self._lock:
            self._peers[peer_did.lower()] = peer
        if self._on_peers_changed:
            self._on_peers_changed(self.get_peers())

    def _prune_expired(self) -> None:
        now = time.monotonic()
        removed = False
        with self._lock:
            stale = [
                k for k, v in self._peers.items()
                if v.last_seen > 0.0 and (now - v.last_seen) > UDP_PEER_TTL_S
            ]
            for k in stale:
                del self._peers[k]
                removed = True
        if removed and self._on_peers_changed:
            self._on_peers_changed(self.get_peers())

    def _try_start_zeroconf(self) -> None:
        """Optional mDNS enhancement if `zeroconf` library is installed."""
        try:
            from zeroconf import ServiceBrowser, ServiceInfo, Zeroconf  # type: ignore
        except ImportError:
            return

        port = self._port_provider()
        did = self._did_provider()
        name = self._name_provider()
        _, _, primary_ip = collect_local_ipv4_and_broadcasts()
        if port <= 0 or not did or not primary_ip:
            return

        try:
            zc = Zeroconf()
            svc_name = f"{SERVICE_PREFIX}{did[:8]}.{SERVICE_TYPE}"
            info = ServiceInfo(
                SERVICE_TYPE,
                svc_name,
                addresses=[socket.inet_aton(primary_ip)],
                port=port,
                properties={"did": did.encode("utf-8"), "name": name[:24].encode("utf-8")},
            )
            zc.register_service(info, allow_name_change=True)
            self._registered_service_name = info.name.split(".", 1)[0]

            class _Listener:
                def __init__(self, parent: LanDiscoveryService) -> None:
                    self.parent = parent

                def add_service(self, zc_inst, svc_type, full_name) -> None:
                    self._resolve(zc_inst, svc_type, full_name)

                def update_service(self, zc_inst, svc_type, full_name) -> None:
                    self._resolve(zc_inst, svc_type, full_name)

                def remove_service(self, zc_inst, svc_type, full_name) -> None:
                    pass

                def _resolve(self, zc_inst, svc_type, full_name) -> None:
                    si = zc_inst.get_service_info(svc_type, full_name, timeout=1500)
                    if not si:
                        return
                    addrs = si.parsed_scoped_addresses()
                    if not addrs:
                        return
                    props = si.properties or {}
                    p_did = (props.get(b"did") or b"").decode("utf-8", errors="ignore").strip()
                    p_name = (props.get(b"name") or b"").decode("utf-8", errors="ignore").strip()
                    short = full_name.split(".", 1)[0]
                    if not p_did:
                        p_did = short.removeprefix(SERVICE_PREFIX)
                    self.parent.register_manual_or_incoming_peer(
                        p_did,
                        p_name or f"Device {p_did[:8]}",
                        addrs[0],
                        si.port,
                    )

            self._zeroconf = zc
            self._zc_info = info
            self._zc_browser = ServiceBrowser(zc, SERVICE_TYPE, _Listener(self))
        except Exception:
            self._stop_zeroconf()

    def _stop_zeroconf(self) -> None:
        zc = self._zeroconf
        info = self._zc_info
        self._zeroconf = None
        self._zc_info = None
        self._zc_browser = None
        if zc is not None:
            try:
                if info is not None:
                    zc.unregister_service(info)
            except Exception:
                pass
            try:
                zc.close()
            except Exception:
                pass
