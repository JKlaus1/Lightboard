"""
sACN (ANSI E1.31) DMX-over-IP driver.

Sends E1.31 DATA packets either:
  - unicast to a list of target IPs (similar workflow to the Art-Net driver), or
  - multicast to the standard sACN multicast group for each universe
    (239.255.UH.UL where UH:UL = universe number, big-endian).

Multicast mode is ideal for WLED and other E1.31 receivers — just point them
at a universe number and they pick the data out of the multicast stream.

Note on universe numbering:
  - The sACN spec defines universes 1-63999 as valid (0 and 64000+ are out of
    spec). Many receivers will accept 0, but for portability prefer assigning
    fixtures to universes >= 1 when using sACN.
"""

import socket
import struct
import threading
import time
import uuid
import logging

log = logging.getLogger(__name__)


class SacnDMX:
    SACN_PORT        = 5568
    DEFAULT_PRIORITY = 100
    SOURCE_NAME      = "Lightboard"

    def __init__(self, targets="", universe=0, priority=100, multicast=False, cid=None):
        """
        targets:   comma-separated string or list of "IP" or "IP:universe" entries.
                   Ignored when multicast=True.
        universe:  default universe for legacy flat data (and for the entry that
                   the snapshot APIs return when no specific universe is asked).
        priority:  0-200 sACN priority (default 100). Receivers use this for HTP
                   merging when multiple senders are present.
        multicast: if True, every universe is sent to its standard sACN multicast
                   address. If False, each packet is unicast to all matching
                   targets (per-target universe filtering supported).
        cid:       optional 16-byte component identifier. If None, a fresh UUID
                   is generated at construction time. (Many receivers don't care
                   that this changes across restarts.)
        """
        if isinstance(targets, str):
            targets = [t.strip() for t in targets.split(",") if t.strip()]

        parsed = []
        for entry in targets:
            entry = entry.strip()
            if ":" in entry:
                ip_part, uni_part = entry.rsplit(":", 1)
                try:
                    parsed.append((ip_part.strip(), int(uni_part.strip())))
                except ValueError:
                    parsed.append((entry, None))
            else:
                parsed.append((entry, None))

        self.targets     = parsed
        self.default_uni = int(universe)
        self.priority    = max(0, min(200, int(priority)))
        self.multicast   = bool(multicast)
        self.cid         = cid if cid is not None else uuid.uuid4().bytes

        self.connected   = False
        self._socket     = None
        # Per-universe DMX buffer (each is 512 bytes; channel 1 → index 0)
        self._universes  = {self.default_uni: bytearray(512)}
        # Per-universe sequence number (rolling 0-255 per E1.31 spec)
        self._sequences  = {}
        self._lock       = threading.Lock()
        self._last_send  = 0.0
        self._running    = False
        self._thread     = None

    def connect(self):
        try:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._socket.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 8)
            # Allow multicast on all interfaces by default; receivers join the
            # group themselves so we don't need to bind to a specific one.
            self.connected = True
            if self.multicast:
                log.info(f"sACN multicast mode (priority {self.priority}, "
                         f"default universe {self.default_uni})")
            else:
                labels = [f"{ip}:u{u}" if u is not None else f"{ip}:all"
                          for ip, u in self.targets]
                if labels:
                    log.info(f"sACN unicast: {', '.join(labels)} "
                             f"(priority {self.priority})")
                else:
                    log.warning("sACN unicast mode with no targets configured")
            self._running = True
            self._thread = threading.Thread(target=self._keepalive_loop,
                                            daemon=True, name="sacn-keepalive")
            self._thread.start()
        except Exception as e:
            log.error(f"sACN setup failed: {e}")
            self.connected = False

    def _keepalive_loop(self):
        """Re-send the current frame periodically so receivers know we're alive
        and don't time out into their 'no signal' state."""
        while self._running:
            if time.time() - self._last_send > 1.0:
                self._send()
            time.sleep(0.1)

    # ── Public API (matches dmx.py / artnet.py shape) ─────────────────────

    def set_channels(self, values):
        """Accepts either flat {channel: value} (legacy → default universe) or
        nested {universe: {channel: value}}."""
        if not values:
            return
        sample = next(iter(values.values()))
        with self._lock:
            if isinstance(sample, dict):
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
        if universe is None:
            universe = self.default_uni
        with self._lock:
            buf = self._universes.get(int(universe))
            return list(buf) if buf else [0] * 512

    def get_all_universes_snapshot(self):
        with self._lock:
            return {u: list(b) for u, b in self._universes.items()}

    def stop(self):
        self._running = False
        if self._socket:
            try:
                self._socket.close()
            except Exception:
                pass
        self.connected = False

    # ── Internal ──────────────────────────────────────────────────────────

    def _send(self):
        if not self.connected or self._socket is None:
            return
        with self._lock:
            universes = {u: bytes(b) for u, b in self._universes.items()}

        for uni, data in universes.items():
            seq = self._sequences.get(uni, 0)
            self._sequences[uni] = (seq + 1) % 256
            packet = self._build_packet(uni, seq, data)

            if self.multicast:
                # E1.31 multicast: 239.255.{universe_high}.{universe_low}
                hi = (uni >> 8) & 0xff
                lo = uni & 0xff
                mc_addr = f"239.255.{hi}.{lo}"
                try:
                    self._socket.sendto(packet, (mc_addr, self.SACN_PORT))
                except Exception as e:
                    log.debug(f"sACN multicast u{uni} failed: {e}")
            else:
                for ip, target_uni in self.targets:
                    if target_uni is not None and target_uni != uni:
                        continue
                    try:
                        self._socket.sendto(packet, (ip, self.SACN_PORT))
                    except Exception as e:
                        log.debug(f"sACN unicast to {ip} u{uni} failed: {e}")
        self._last_send = time.time()

    def _build_packet(self, universe, sequence, dmx_data):
        """Build a 638-byte E1.31 DATA packet (Root + Framing + DMP layers)."""
        # ── DMP layer (523 bytes) ──
        # 2 flags+len + 1 vector + 1 type + 2 start-addr + 2 increment +
        # 2 value-count + 513 properties (1 start-code + 512 channels) = 523
        dmp = (
            b"\x72\x0b"                            # Flags+Length: 0x720b (PDU=523)
            + b"\x02"                              # Vector: SET_PROPERTY
            + b"\xa1"                              # Address+Data type
            + b"\x00\x00"                          # First property address
            + b"\x00\x01"                          # Address increment
            + b"\x02\x01"                          # Property value count: 513
            + b"\x00"                              # DMX start code
            + dmx_data                             # 512 DMX bytes
        )
        # ── Framing layer (600 bytes) ──
        source_name = self.SOURCE_NAME.encode("utf-8")[:63].ljust(64, b"\x00")
        framing = (
            b"\x72\x58"                            # Flags+Length: 0x7258 (PDU=600)
            + b"\x00\x00\x00\x02"                  # Vector: VECTOR_E131_DATA_PACKET
            + source_name                          # 64 bytes
            + bytes([self.priority])               # Priority (0-200)
            + b"\x00\x00"                          # Synchronization address: 0
            + bytes([sequence])                    # Sequence number
            + b"\x00"                              # Options
            + struct.pack(">H", universe)          # Universe (big-endian)
            + dmp
        )
        # ── Root layer (638 bytes) ──
        root = (
            b"\x00\x10"                            # Preamble size: 16
            + b"\x00\x00"                          # Post-amble size: 0
            + b"ASC-E1.17\x00\x00\x00"             # ACN Packet Identifier
            + b"\x72\x6e"                          # Flags+Length: 0x726e (PDU=622)
            + b"\x00\x00\x00\x04"                  # Vector: VECTOR_ROOT_E131_DATA
            + self.cid                             # CID (16 bytes)
            + framing
        )
        return root
