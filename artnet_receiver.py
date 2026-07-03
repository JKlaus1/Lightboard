"""Art-Net receiver: always-on UDP 6454 listener for remote (master/slave)
mode — Phase 2 of the venue-install plan.

A master Pi (the mixer-rack Lightboard) patches this venue Pi's fixtures as
extra universes and unicasts Art-Net at it. This module listens on port 6454
at all times (no toggle), parses ArtDmx packets, and hands each frame to a
callback — in practice LightingEngine.handle_remote_frame(), which engages
remote mode and pipes the frame straight to the local output nodes.

Safety guards:
  * Only opcode OpDmx (0x5000) is acted on. ArtPoll / ArtPollReply chatter
    from the output nodes on the same LAN is ignored.
  * Packets sourced from one of this Pi's OWN addresses are dropped, so the
    local ArtNetDMX sender can never feedback-loop into remote mode (e.g. if
    the Pi's own IP ever ends up in artnet_target).
  * Malformed / truncated packets are dropped silently.
"""

import socket
import struct
import threading
import logging
import time

log = logging.getLogger(__name__)

ARTNET_PORT = 6454
_HEADER     = b"Art-Net\x00"
OP_DMX      = 0x5000


def parse_artdmx(pkt):
    """Parse a raw UDP payload as an ArtDmx packet.

    Returns (universe, data_bytes) or None if the packet is not a valid
    ArtDmx frame. universe is the full 15-bit port-address
    (Net << 8 | SubUni); data_bytes is 2-512 bytes of channel data.
    """
    if len(pkt) < 20 or not pkt.startswith(_HEADER):
        return None
    opcode = struct.unpack("<H", pkt[8:10])[0]
    if opcode != OP_DMX:
        return None
    # [10:12] ProtVer (>=14), [12] Sequence, [13] Physical,
    # [14] SubUni, [15] Net, [16:18] Length (big-endian)
    universe = pkt[14] | (pkt[15] << 8)
    length   = struct.unpack(">H", pkt[16:18])[0]
    if length < 1 or length > 512:
        return None
    data = pkt[18:18 + length]
    if len(data) < length:
        return None            # truncated
    return universe, data


def build_artdmx(universe, data, sequence=0):
    """Build a raw ArtDmx packet (used by tests and the manual blast tool)."""
    data = bytes(data)
    if len(data) % 2:           # spec: even length
        data += b"\x00"
    return (_HEADER
            + struct.pack("<H", OP_DMX)
            + struct.pack(">H", 14)              # ProtVer
            + bytes([sequence & 0xFF, 0])        # Sequence, Physical
            + bytes([universe & 0xFF, (universe >> 8) & 0x7F])
            + struct.pack(">H", len(data))
            + data)


def _local_ipv4s():
    """All IPv4 addresses assigned to this host's interfaces (Linux)."""
    ips = {"127.0.0.1"}
    try:
        import fcntl
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            for _idx, name in socket.if_nameindex():
                try:
                    ip = socket.inet_ntoa(fcntl.ioctl(
                        s.fileno(), 0x8915,          # SIOCGIFADDR
                        struct.pack("256s", name[:15].encode()))[20:24])
                    ips.add(ip)
                except OSError:
                    pass                             # iface has no IPv4
        finally:
            s.close()
    except Exception:
        pass
    return ips


class ArtNetReceiver:
    """Background UDP listener. on_frame(universe, data, src_ip) is called
    for every valid ArtDmx packet from a non-local source."""

    LOCAL_IP_REFRESH_S = 60.0    # interfaces change (venue AP up/down)

    def __init__(self, on_frame, port=ARTNET_PORT):
        self.on_frame  = on_frame
        self.port      = port
        self._sock     = None
        self._thread   = None
        self._running  = False
        self._local    = set()
        self._local_ts = 0.0

    def start(self):
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind(("0.0.0.0", self.port))
            self._sock.settimeout(1.0)   # so stop() can exit the loop
        except OSError as e:
            log.error("Art-Net receiver: cannot bind :%d (%s) — "
                      "remote mode unavailable", self.port, e)
            self._sock = None
            return False
        self._refresh_local()
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="artnet-rx")
        self._thread.start()
        log.info("Art-Net receiver listening on :%d (remote mode armed)",
                 self.port)
        return True

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    # ── internal ──────────────────────────────────────────────────────────

    def _refresh_local(self):
        self._local    = _local_ipv4s()
        self._local_ts = time.time()

    def _run(self):
        while self._running:
            try:
                pkt, addr = self._sock.recvfrom(1024)
            except socket.timeout:
                continue
            except OSError:
                if self._running:
                    log.warning("Art-Net receiver: socket error, retrying")
                    time.sleep(0.5)
                continue
            src_ip = addr[0]
            if time.time() - self._local_ts > self.LOCAL_IP_REFRESH_S:
                self._refresh_local()
            if src_ip in self._local:
                continue                     # our own sender — never loop back
            parsed = parse_artdmx(pkt)
            if parsed is None:
                continue                     # ArtPoll / junk — ignore
            universe, data = parsed
            try:
                self.on_frame(universe, data, src_ip)
            except Exception:
                log.exception("Art-Net receiver: on_frame handler failed")
