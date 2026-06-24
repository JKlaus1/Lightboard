"""
Enttec Open DMX USB driver for Raspberry Pi.
Generates proper DMX512 break/MAB timing via pyserial.
Runs a continuous 40fps output thread.
"""

import serial
import threading
import time
import logging

log = logging.getLogger(__name__)


class EnttecOpenDMX:
    BAUD_RATE  = 250000
    REFRESH_HZ = 40
    BREAK_US   = 0.0001
    MAB_US     = 0.000012

    def __init__(self, port="/dev/ttyUSB0"):
        self.port       = port
        self._universe  = bytearray(513)
        self._lock      = threading.Lock()
        self._running   = False
        self._connected = False
        self._ser       = None
        self._thread    = None

    def connect(self):
        try:
            self._ser = serial.Serial(
                self.port,
                baudrate=self.BAUD_RATE,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_TWO,
                timeout=1,
            )
            self._connected = True
            self._running   = True
            self._thread    = threading.Thread(target=self._run, daemon=True, name="dmx-output")
            self._thread.start()
            log.info(f"Open DMX connected on {self.port}")
        except serial.SerialException as exc:
            log.error(f"Could not open DMX port {self.port}: {exc}")
            self._connected = False

    @property
    def connected(self):
        return self._connected

    def set_channel(self, channel, value):
        if 1 <= channel <= 512:
            with self._lock:
                self._universe[channel] = max(0, min(255, int(value)))

    def set_channels(self, values):
        """Accepts either:
          - Flat {channel: value}  → goes to universe 0 (legacy)
          - Nested {universe: {channel: value}} → only universe 0 sent over USB
        """
        if not values:
            return
        # Detect format by inspecting first value
        sample = next(iter(values.values()))
        if isinstance(sample, dict):
            # Nested form: extract universe 0 (Enttec is single-universe)
            uni0 = values.get(0, {})
            with self._lock:
                for ch, val in uni0.items():
                    ch = int(ch)
                    if 1 <= ch <= 512:
                        self._universe[ch] = max(0, min(255, int(val)))
        else:
            # Legacy flat form
            with self._lock:
                for ch, val in values.items():
                    ch = int(ch)
                    if 1 <= ch <= 512:
                        self._universe[ch] = max(0, min(255, int(val)))

    def get_channel(self, channel):
        with self._lock:
            return self._universe[channel] if 1 <= channel <= 512 else 0

    def get_universe_snapshot(self, universe=0):
        """List of 512 ints (index 0 = channel 1) for the given universe.
        Enttec only outputs universe 0, so other universes return zeros."""
        if int(universe) != 0:
            return [0] * 512
        with self._lock:
            return list(self._universe[1:513])

    def get_all_universes_snapshot(self):
        """{universe_num: [512 ints]}. Enttec is single-universe so only u0."""
        return {0: self.get_universe_snapshot(0)}

    def blackout(self):
        with self._lock:
            for i in range(1, 513):
                self._universe[i] = 0

    def stop(self):
        self._running = False
        if self._ser and self._ser.is_open:
            try:
                self._ser.close()
            except Exception:
                pass

    def _send_frame(self):
        try:
            self._ser.break_condition = True
            time.sleep(self.BREAK_US)
            self._ser.break_condition = False
            time.sleep(self.MAB_US)
            with self._lock:
                data = bytes(self._universe)
            self._ser.write(data)
        except serial.SerialException as exc:
            log.warning(f"DMX send error: {exc}")
            self._connected = False
            self._try_reconnect()

    def _try_reconnect(self):
        log.info("Attempting DMX reconnect in 3s...")
        time.sleep(3)
        try:
            if self._ser:
                self._ser.close()
            self._ser = serial.Serial(
                self.port, baudrate=self.BAUD_RATE,
                bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_TWO, timeout=1,
            )
            self._connected = True
            log.info("DMX reconnected")
        except Exception as exc:
            log.error(f"Reconnect failed: {exc}")

    def _run(self):
        interval = 1.0 / self.REFRESH_HZ
        while self._running:
            t0 = time.time()
            if self._connected:
                self._send_frame()
            sleep = max(0.0, interval - (time.time() - t0))
            time.sleep(sleep)
