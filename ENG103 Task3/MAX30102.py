#!/usr/bin/env python3
"""
max30102_reader.py

Reads a MAX30102 pulse-oximeter/heart-rate sensor over I2C on a Raspberry Pi,
and continuously estimates:
    - BPM   (heart rate, beats per minute)
    - SpO2  (blood oxygen saturation, percent)

These are kept up to date in a background thread and exposed as plain
attributes (`monitor.bpm`, `monitor.spo2`) so other scripts can import this
module and read them at any time.

IMPORTANT: This uses a simple peak-detection / ratio-of-ratios algorithm.
It is fine for hobby projects but is NOT a medical device and should not be
used for health/medical decisions. Thresholds (PEAK_THRESHOLD, FINGER_THRESHOLD)
may need tuning for your specific sensor, finger, and skin tone/lighting.

Wiring (3.3V logic, Pi GPIO header):
    MAX30102 VIN -> Pi 3.3V (pin 1)
    MAX30102 GND -> Pi GND  (pin 6)
    MAX30102 SDA -> Pi SDA1 (pin 3, GPIO2)
    MAX30102 SCL -> Pi SCL1 (pin 5, GPIO3)

Setup on the Pi:
    sudo raspi-config          # Interface Options -> I2C -> Enable
    sudo reboot
    sudo apt install -y python3-smbus i2c-tools
    pip3 install smbus2 --break-system-packages
    i2cdetect -y 1             # sensor should show up at address 0x57

Run standalone:
    python3 max30102_reader.py
    python3 max30102_reader.py --csv out.csv

Use from another script:
    from max30102_reader import start_monitoring
    import time

    monitor = start_monitoring()
    time.sleep(3)               # give it a few seconds to get a stable reading
    print(monitor.bpm, monitor.spo2)

    while True:
        if monitor.finger_detected:
            print(f"BPM={monitor.bpm} SpO2={monitor.spo2}")
        time.sleep(1)
"""

import argparse
import collections
import csv
import sys
import threading
import time

try:
    from smbus2 import SMBus
except ImportError:
    print("Missing dependency. Install it with:")
    print("    pip3 install smbus2 --break-system-packages")
    sys.exit(1)

# ---------------------------------------------------------------------------
# MAX30102 register map (subset needed for basic operation)
# ---------------------------------------------------------------------------
MAX30102_ADDRESS = 0x57

REG_FIFO_WR_PTR = 0x04
REG_FIFO_OVF_COUNTER = 0x05
REG_FIFO_RD_PTR = 0x06
REG_FIFO_DATA = 0x07
REG_FIFO_CONFIG = 0x08
REG_MODE_CONFIG = 0x09
REG_SPO2_CONFIG = 0x0A
REG_LED1_PA = 0x0C  # Red LED pulse amplitude
REG_LED2_PA = 0x0D  # IR LED pulse amplitude
REG_INT_ENABLE_1 = 0x02
REG_INT_ENABLE_2 = 0x03
REG_PART_ID = 0xFF

MODE_SPO2 = 0x03  # Red + IR

SAMPLE_AVG_1 = 0x00
FIFO_ROLLOVER_EN = 0x10

ADC_RANGE_4096 = 0x00
SAMPLE_RATE_100 = 0x04
PULSE_WIDTH_411 = 0x03  # 18-bit resolution


class MAX30102:
    """Low-level I2C driver: setup + raw FIFO reads."""

    def __init__(self, bus_number=1, address=MAX30102_ADDRESS):
        self.address = address
        self.bus = SMBus(bus_number)
        self._reset()
        self._setup()

    def _write(self, reg, value):
        self.bus.write_byte_data(self.address, reg, value)

    def _read(self, reg, length=1):
        if length == 1:
            return self.bus.read_byte_data(self.address, reg)
        return self.bus.read_i2c_block_data(self.address, reg, length)

    def _reset(self):
        self._write(REG_MODE_CONFIG, 0x40)  # reset bit
        time.sleep(0.1)

    def _setup(self, red_current=0x24, ir_current=0x24):
        self._write(REG_FIFO_WR_PTR, 0x00)
        self._write(REG_FIFO_OVF_COUNTER, 0x00)
        self._write(REG_FIFO_RD_PTR, 0x00)

        self._write(REG_FIFO_CONFIG, SAMPLE_AVG_1 | FIFO_ROLLOVER_EN | 0x0F)
        self._write(REG_MODE_CONFIG, MODE_SPO2)
        self._write(REG_SPO2_CONFIG, ADC_RANGE_4096 | SAMPLE_RATE_100 | PULSE_WIDTH_411)

        self._write(REG_LED1_PA, red_current)
        self._write(REG_LED2_PA, ir_current)

        self._write(REG_INT_ENABLE_1, 0x00)
        self._write(REG_INT_ENABLE_2, 0x00)

    def part_id(self):
        return self._read(REG_PART_ID)

    def available_samples(self):
        wr_ptr = self._read(REG_FIFO_WR_PTR)
        rd_ptr = self._read(REG_FIFO_RD_PTR)
        n = wr_ptr - rd_ptr
        if n < 0:
            n += 32
        return n

    def read_fifo(self):
        """Return all currently available samples as a list of (red, ir) tuples."""
        samples = []
        n = self.available_samples()
        for _ in range(n):
            data = self._read(REG_FIFO_DATA, 6)
            red = ((data[0] << 16) | (data[1] << 8) | data[2]) & 0x03FFFF
            ir = ((data[3] << 16) | (data[4] << 8) | data[5]) & 0x03FFFF
            samples.append((red, ir))
        return samples

    def close(self):
        self.bus.close()


class HeartRateSpO2Monitor:
    """
    Wraps the MAX30102 driver, runs a background polling thread, and keeps
    self.bpm / self.spo2 (and self.red / self.ir / self.finger_detected)
    continuously up to date.
    """

    SAMPLE_RATE_HZ = 100          # matches SAMPLE_RATE_100 config above
    BUFFER_SECONDS = 4            # rolling window used for SpO2 calc
    FINGER_THRESHOLD = 50000      # raw IR DC value below this => no finger present
    PEAK_THRESHOLD = 200          # min AC amplitude to count as a heartbeat pulse
    MIN_PEAK_INTERVAL = 0.3       # seconds between beats (caps BPM at 200)
    SPO2_UPDATE_INTERVAL = 1.0    # seconds between SpO2 recalculations

    def __init__(self, bus_number=1):
        self.sensor = MAX30102(bus_number=bus_number)

        maxlen = int(self.SAMPLE_RATE_HZ * self.BUFFER_SECONDS)
        self._ir_buffer = collections.deque(maxlen=maxlen)
        self._red_buffer = collections.deque(maxlen=maxlen)

        self._peak_times = collections.deque(maxlen=6)
        self._last_peak_time = 0.0
        self._running_avg_ir = None

        # Public, thread-safe-ish values other code can read directly.
        self.bpm = None            # float BPM, or None until enough data
        self.spo2 = None           # float percent, or None until enough data
        self.red = 0
        self.ir = 0
        self.finger_detected = False

        self._lock = threading.Lock()
        self._thread = None
        self._stop_flag = threading.Event()

    # -- lifecycle ----------------------------------------------------
    def start(self):
        self._stop_flag.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop_flag.set()
        if self._thread:
            self._thread.join(timeout=2)
        self.sensor.close()

    # -- background loop ------------------------------------------------
    def _run(self):
        poll_interval = 1.0 / self.SAMPLE_RATE_HZ
        last_spo2_calc = 0.0
        while not self._stop_flag.is_set():
            for red, ir in self.sensor.read_fifo():
                now = time.time()
                self._process_sample(red, ir, now)

            now = time.time()
            if now - last_spo2_calc > self.SPO2_UPDATE_INTERVAL:
                self._update_spo2()
                last_spo2_calc = now

            time.sleep(poll_interval)

    # -- per-sample processing ------------------------------------------
    def _process_sample(self, red, ir, timestamp):
        self.red = red
        self.ir = ir
        self._ir_buffer.append(ir)
        self._red_buffer.append(red)

        self.finger_detected = ir > self.FINGER_THRESHOLD
        if not self.finger_detected:
            with self._lock:
                self.bpm = None
                self.spo2 = None
            self._peak_times.clear()
            self._running_avg_ir = None
            return

        # Exponential moving average tracks the slow-changing DC baseline;
        # the difference (baseline - ir) is the AC pulsatile component,
        # which spikes upward on each heartbeat (light absorption dips).
        alpha = 0.01
        if self._running_avg_ir is None:
            self._running_avg_ir = ir
        else:
            self._running_avg_ir = alpha * ir + (1 - alpha) * self._running_avg_ir
        ac_value = self._running_avg_ir - ir

        self._detect_peak(ac_value, timestamp)

    def _detect_peak(self, ac_value, timestamp):
        if ac_value > self.PEAK_THRESHOLD and (timestamp - self._last_peak_time) > self.MIN_PEAK_INTERVAL:
            self._last_peak_time = timestamp
            self._peak_times.append(timestamp)
            if len(self._peak_times) >= 2:
                pts = list(self._peak_times)
                intervals = [t2 - t1 for t1, t2 in zip(pts, pts[1:])]
                avg_interval = sum(intervals) / len(intervals)
                if avg_interval > 0:
                    with self._lock:
                        self.bpm = round(60.0 / avg_interval, 1)

    def _update_spo2(self):
        ir_vals = list(self._ir_buffer)
        red_vals = list(self._red_buffer)

        if len(ir_vals) < self.SAMPLE_RATE_HZ or not self.finger_detected:
            return

        ir_dc = sum(ir_vals) / len(ir_vals)
        red_dc = sum(red_vals) / len(red_vals)
        ir_ac = (max(ir_vals) - min(ir_vals)) / 2.0
        red_ac = (max(red_vals) - min(red_vals)) / 2.0

        if ir_dc == 0 or red_dc == 0 or ir_ac == 0:
            return

        # Standard ratio-of-ratios approach with Maxim's empirical calibration curve.
        R = (red_ac / red_dc) / (ir_ac / ir_dc)
        spo2 = -45.060 * (R ** 2) + 30.354 * R + 94.845
        spo2 = max(0.0, min(100.0, spo2))

        with self._lock:
            self.spo2 = round(spo2, 1)

    def get_readings(self):
        """Return a snapshot dict: bpm, spo2, red, ir, finger_detected."""
        with self._lock:
            return {
                "bpm": self.bpm,
                "spo2": self.spo2,
                "red": self.red,
                "ir": self.ir,
                "finger_detected": self.finger_detected,
            }


# ---------------------------------------------------------------------------
# Module-level convenience API, so other scripts can do:
#     from max30102_reader import start_monitoring
#     monitor = start_monitoring()
#     print(monitor.bpm, monitor.spo2)
# ---------------------------------------------------------------------------
_monitor = None


def start_monitoring(bus_number=1):
    """Create (if needed) and start the global monitor. Returns the monitor instance."""
    global _monitor
    if _monitor is None:
        _monitor = HeartRateSpO2Monitor(bus_number=bus_number)
        _monitor.start()
    return _monitor


def stop_monitoring():
    global _monitor
    if _monitor is not None:
        _monitor.stop()
        _monitor = None


def get_bpm():
    """Convenience getter: current BPM, or None if not available yet / no finger."""
    return _monitor.bpm if _monitor else None


def get_spo2():
    """Convenience getter: current SpO2 percent, or None if not available yet / no finger."""
    return _monitor.spo2 if _monitor else None


# ---------------------------------------------------------------------------
# Standalone CLI usage
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Read BPM and SpO2 from a MAX30102 sensor.")
    parser.add_argument("--bus", type=int, default=1, help="I2C bus number (default: 1)")
    parser.add_argument("--interval", type=float, default=1.0, help="Console print interval in seconds (default: 1)")
    parser.add_argument("--csv", type=str, default=None, help="Optional path to log bpm,spo2,red,ir to a CSV file")
    args = parser.parse_args()

    monitor = start_monitoring(bus_number=args.bus)
    print(f"MAX30102 detected. Part ID: 0x{monitor.sensor.part_id():02X}")
    print("Place a finger gently on the sensor. Press Ctrl+C to stop.\n")

    csv_writer = None
    csv_file = None
    if args.csv:
        csv_file = open(args.csv, "w", newline="")
        csv_writer = csv.writer(csv_file)
        csv_writer.writerow(["timestamp", "bpm", "spo2", "red", "ir", "finger_detected"])

    try:
        while True:
            r = monitor.get_readings()
            if not r["finger_detected"]:
                print("No finger detected...")
            else:
                bpm_str = f"{r['bpm']:.1f}" if r["bpm"] is not None else "calculating..."
                spo2_str = f"{r['spo2']:.1f}%" if r["spo2"] is not None else "calculating..."
                print(f"BPM: {bpm_str:>10}   SpO2: {spo2_str:>10}   (raw IR={r['ir']})")

            if csv_writer:
                csv_writer.writerow([f"{time.time():.3f}", r["bpm"], r["spo2"], r["red"], r["ir"], r["finger_detected"]])

            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        if csv_file:
            csv_file.close()
        monitor.stop()


if __name__ == "__main__":
    main()