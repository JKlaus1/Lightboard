"""Art-Net DMX driver: sends DMX universe data over UDP to an Art-Net node
such as the PKNight EN-3P, an Eltrum, an ENTTEC ODE, etc.

Implements the same public interface as EnttecOpenDMX (connect / disconnect /
set_channels / blackout / connected) so LightingEngine can swap between them
without code changes.
"""

import socket
import threading
import logging
import time

log = logging.getLogger(__name__)


class ArtNetDMX:
    ARTNET_PORT = 6454

    def __init__(self, targets, universe=0):
        """targets: a single string, comma-separated string, or list of strings.
        Each entry can be a plain IP (receives all universes) or 'IP:universe'
        to route only that universe to that target. Examples:
            "192.168.1.182"                    → receives every universe
            "192.168.1.182:0, 192.168.1.183:1" → 182 only gets u0, 183 only gets u1
            ["192.168.1.182:0", "192.168.1.183"] → 182 only gets u0, 183 gets all
        universe: default universe for legacy flat data."""
        if isinstance(targets, str):
            targets = [t.strip() for t in targets.split(",") if t.strip()]
        # Parse each "IP" or "IP:universe" entry → (ip, universe_or_None)
        parsed = []
        for entry in targets:
            entry = entry.strip()
            if ":" in entry:
                ip_part, uni_part = entry.rsplit(":", 1)
                try:
                    parsed.append((ip_part.strip(), int(uni_part.strip())))
                except ValueError:
                    parsed.append((entry, None))  # malformed, treat as all
            else:
                parsed.append((entry, None))
        self.targets       = parsed   # list of (ip, universe|None)
        self.default_uni   = int(universe)
        self.connected     = False
        self._socket       = None
        # Multi-universe state: {universe_num: bytearray(512)}
        self._universes    = {self.default_uni: bytearray(512)}
        self._lock         = threading.Lock()
        self._sequence     = 1
        self._last_send    = 0.0
        self._running      = False
        self._thread       = None

    def connect(self):
        try:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            # Non-blocking: a real-time DMX sender must never stall on the
            # network. If a target is unreachable (powered off, or not on this
            # network), sendto() fails instantly instead of blocking the output
            # thread on ARP resolution — which would otherwise freeze the whole
            # engine, the visualizer, and the DMX monitor ~once a second.
            try:
                self._socket.setblocking(False)
            except Exception:
                pass
            self.connected = True
            if not self.targets:
                log.warning("Art-Net: no targets configured")
            else:
                labels = [f"{ip}:u{u}" if u is not None else f"{ip}:all"
                          for ip, u in self.targets]
                log.info(f"Art-Net output: {', '.join(labels)} "
                         f"(default universe {self.default_uni})")
            self._running = True
            self._thread = threading.Thread(target=self._keepalive_loop,
                                            daemon=True, name="artnet-keepalive")
            self._thread.start()
        except Exception as e:
            log.error(f"Art-Net setup failed: {e}")
            self.connected = False

    def disconnect(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None
        if self._socket:
            try:
                self._socket.close()
            except Exception:
                pass
            self._socket = None
        self.connected = False

    def set_channels(self, values):
        """Accepts either:
          - Flat {channel: value}             → default universe (legacy)
          - Nested {universe: {channel: value}} → multi-universe
        """
        if not values:
            return
        sample = next(iter(values.values()))
        with self._lock:
            if isinstance(sample, dict):
                # Multi-universe form
                for uni, channels in values.items():
                    uni = int(uni)
                    if uni not in self._universes:
                        self._universes[uni] = bytearray(512)
                    buf = self._universes[uni]
                    for ch, val in channels.items():
                        ch = int(ch)
                        if 1 <= ch <= 512:
                            buf[ch - 1] = max(0, min(255, int(val)))
            else:
                # Legacy flat form → default universe
                buf = self._universes.setdefault(self.default_uni, bytearray(512))
                for ch, val in values.items():
                    ch = int(ch)
                    if 1 <= ch <= 512:
                        buf[ch - 1] = max(0, min(255, int(val)))
        self._send()

    def blackout(self):
        with self._lock:
            for uni in self._universes:
                buf = self._universes[uni]
                for i in range(512):
                    buf[i] = 0
        self._send()

    def get_universe_snapshot(self, universe=None):
        """Return current universe data as a list of 512 ints.
        If universe is None, returns the default universe (for the DMX monitor).
        """
        if universe is None:
            universe = self.default_uni
        with self._lock:
            buf = self._universes.get(int(universe))
            return list(buf) if buf else [0] * 512

    def get_all_universes_snapshot(self):
        """Returns {universe_num: [512 ints]} for all universes that have data."""
        with self._lock:
            return {u: list(b) for u, b in self._universes.items()}

    # ── internal ──────────────────────────────────────────────────────────

    def _send(self):
        if not self.connected or self._socket is None or not self.targets:
            return
        with self._lock:
            universes = {u: bytes(b) for u, b in self._universes.items()}
            seq = self._sequence
            self._sequence = 1 if self._sequence >= 255 else self._sequence + 1
        # One UDP packet per universe per matching target. A target whose
        # universe is None receives every universe; otherwise only the
        # matching one.
        for uni, data in universes.items():
            packet = self._build_packet(seq, uni, data)
            for ip, target_uni in self.targets:
                if target_uni is not None and target_uni != uni:
                    continue
                try:
                    self._socket.sendto(packet, (ip, self.ARTNET_PORT))
                except Exception as e:
                    log.debug(f"Art-Net send to {ip} u{uni} failed: {e}")
        self._last_send = time.time()

    def _build_packet(self, seq, universe, dmx_data):
        return (
            b"Art-Net\x00"
            + b"\x00\x50"
            + b"\x00\x0e"
            + bytes([seq])
            + b"\x00"
            + bytes([universe & 0xff, (universe >> 8) & 0x7f])
            + bytes([(512 >> 8) & 0xff, 512 & 0xff])
            + dmx_data
        )

    def _keepalive_loop(self):
        """Refresh the node if no frame has been sent recently."""
        while self._running:
            time.sleep(0.05)
            if not self._running:
                return
            if time.time() - self._last_send > 0.15:
                self._send()
