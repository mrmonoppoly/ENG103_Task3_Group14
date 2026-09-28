"""

Reads a MAX30102 pulse-oximeter/heart-rate sensor over I2C on a Raspberry Pi,
and continuously estimates:
    - BPM   (heart rate, beats per minute)
    - SpO2  (blood oxygen saturation, percent)

These are kept up to date in a background thread and exposed as plain
attributes (`monitor.bpm`, `monitor.spo2`) so other scripts can import this
module and read them at any time. Both are None when there is no finger, no
detectable pulse, or the sensor can't be read.

IMPORTANT: This uses a simple peak-detection / ratio-of-ratios algorithm.
It is fine for hobby projects but is NOT a medical device and should not be
used for health/medical decisions.

Tuning: run this file directly and watch "pulse amp". With a good finger
placement it should be a few hundred counts or more. If it is tiny, raise the
LED current (led_current in MAX30102._setup) or press slightly firmer/lighter.
MIN_PEAK_AMPLITUDE should sit well above the amp you see with the finger on
but no pulse (noise) and well below the amp with a good pulse.

Wiring (3.3V logic, Pi GPIO header):
    MAX30102 VIN -> Pi 3.3V (pin 1)
    MAX30102 GND -> Pi GND  (pin 6)
    MAX30102 SDA -> Pi SDA1 (pin 3, GPIO2)
    MAX30102 SCL -> Pi SCL1 (pin 5, GPIO3)
    (INT is not used)

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
    time.sleep(5)               # give it a few seconds to get a stable reading
    print(monitor.bpm, monitor.spo2)
"""

import argparse
import collections
import csv
import math
import statistics
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

    def _setup(self, red_current=0x10, ir_current=0x10):
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

    def reinit(self):
        """Reset and reconfigure the sensor (used to recover after an I2C error)."""
        self._reset()
        self._setup()

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

    SAMPLE_RATE_HZ = 100            # matches SAMPLE_RATE_100 config above
    BUFFER_SECONDS = 4              # rolling window used for SpO2 calc
    FINGER_THRESHOLD = 50000        # raw IR DC value below this => no finger present
    BASELINE_ALPHA = 0.02           # DC baseline tracking speed
    SMOOTH_SAMPLES = 5              # moving-average length (~50 ms) to reduce noise
    SETTLE_SECONDS = 3.0            # ignore the signal this long after a finger appears

    MIN_PEAK_AMPLITUDE = 100        # floor for the peak threshold (raw ADC counts)
    PEAK_THRESHOLD_FRACTION = 0.5   # peaks must reach this fraction of the recent max
    MIN_PEAK_INTERVAL = 0.3         # seconds between beats (caps BPM at 200)
    MAX_PEAK_INTERVAL = 1.5         # longer gap = missed beat (below 40 BPM); restart averaging
    MIN_INTERVALS = 3               # beats needed before a BPM is reported
    PULSE_TIMEOUT = 3.0             # seconds without a beat before BPM/SpO2 are cleared
    SPO2_TIMEOUT = 5.0              # seconds without a valid SpO2 update before it is cleared

    SPO2_UPDATE_INTERVAL = 1.0      # seconds between SpO2 recalculations
    SPO2_MIN_R = 0.2                # plausible ratio-of-ratios range; outside = artifact
    SPO2_MAX_R = 1.0

    def __init__(self, bus_number=1, sensor=None):
        self.sensor = sensor if sensor is not None else MAX30102(bus_number=bus_number)

        maxlen = int(self.SAMPLE_RATE_HZ * self.BUFFER_SECONDS)
        self._ir_buffer = collections.deque(maxlen=maxlen)
        self._red_buffer = collections.deque(maxlen=maxlen)
        self._ir_ac_buffer = collections.deque(maxlen=maxlen)
        self._red_ac_buffer = collections.deque(maxlen=maxlen)
        self._recent_ac = collections.deque(maxlen=self.SAMPLE_RATE_HZ * 2)
        self._ir_smooth = collections.deque(maxlen=self.SMOOTH_SAMPLES)
        self._red_smooth = collections.deque(maxlen=self.SMOOTH_SAMPLES)
        self._ac_hist = collections.deque(maxlen=3)
        self._intervals = collections.deque(maxlen=5)
        self._spo2_history = collections.deque(maxlen=5)

        self._sample_count = 0
        self._last_peak_sample = None
        self._last_spo2_sample = None
        self._ir_baseline = None
        self._red_baseline = None
        self._settle_until = 0

        # Public, thread-safe-ish values other code can read directly.
        self.bpm = None            # float BPM, or None until enough data
        self.spo2 = None           # float percent, or None until enough data
        self.red = 0
        self.ir = 0
        self.ac_amp = 0.0          # recent pulse amplitude (raw counts), useful for tuning
        self.finger_detected = False
        self.last_error = None     # set if the I2C read fails

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
            try:
                for red, ir in self.sensor.read_fifo():
                    self._process_sample(red, ir)
                self.last_error = None
            except OSError as e:
                # I2C hiccup: don't die silently and leave old readings frozen.
                self.last_error = str(e)
                self.finger_detected = False
                self._clear_readings()
                self._reset_tracking()
                time.sleep(0.5)
                try:
                    self.sensor.reinit()  # sensor may have lost power/config
                except (OSError, AttributeError):
                    pass  # still unreachable; try again on the next loop
                continue

            self._expire_stale()

            now = time.time()
            if now - last_spo2_calc > self.SPO2_UPDATE_INTERVAL:
                self._update_spo2()
                last_spo2_calc = now

            time.sleep(poll_interval)

    # -- state helpers ------------------------------------------------
    def _clear_readings(self):
        with self._lock:
            self.bpm = None
            self.spo2 = None

    def _reset_tracking(self):
        """Forget all signal history (finger removed or sensor error)."""
        for d in (self._ir_buffer, self._red_buffer, self._ir_ac_buffer,
                  self._red_ac_buffer, self._recent_ac, self._ir_smooth,
                  self._red_smooth, self._ac_hist, self._intervals,
                  self._spo2_history):
            d.clear()
        self._ir_baseline = None
        self._red_baseline = None
        self._last_peak_sample = None
        self._last_spo2_sample = None
        self.ac_amp = 0.0

    def _expire_stale(self):
        """Clear readings that haven't been refreshed, so old values aren't repeated forever."""
        sr = self.SAMPLE_RATE_HZ
        if self.bpm is not None and self._last_peak_sample is not None:
            if (self._sample_count - self._last_peak_sample) / sr > self.PULSE_TIMEOUT:
                self._clear_readings()
                self._intervals.clear()
                self._spo2_history.clear()
                return
        if self.spo2 is not None and self._last_spo2_sample is not None:
            if (self._sample_count - self._last_spo2_sample) / sr > self.SPO2_TIMEOUT:
                with self._lock:
                    self.spo2 = None
                self._spo2_history.clear()

    # -- per-sample processing ------------------------------------------
    def _process_sample(self, red, ir):
        self._sample_count += 1
        self.red = red
        self.ir = ir

        self.finger_detected = ir > self.FINGER_THRESHOLD
        if not self.finger_detected:
            self._clear_readings()
            self._reset_tracking()
            return

        # Exponential moving averages track the slow-changing DC baselines.
        # (baseline - value) is the AC pulsatile component, which rises on
        # each heartbeat (more blood = more absorption = less reflected light).
        alpha = self.BASELINE_ALPHA
        if self._ir_baseline is None:
            self._ir_baseline = ir
            self._red_baseline = red
            self._settle_until = self._sample_count + int(self.SETTLE_SECONDS * self.SAMPLE_RATE_HZ)
        else:
            self._ir_baseline = alpha * ir + (1 - alpha) * self._ir_baseline
            self._red_baseline = alpha * red + (1 - alpha) * self._red_baseline

        self._ir_smooth.append(self._ir_baseline - ir)
        self._red_smooth.append(self._red_baseline - red)
        ir_ac = sum(self._ir_smooth) / len(self._ir_smooth)
        red_ac = sum(self._red_smooth) / len(self._red_smooth)

        if self._sample_count < self._settle_until:
            return  # still settling after the finger was placed

        self._ir_buffer.append(ir)
        self._red_buffer.append(red)
        self._ir_ac_buffer.append(ir_ac)
        self._red_ac_buffer.append(red_ac)
        self._recent_ac.append(ir_ac)
        self.ac_amp = max(self._recent_ac) - min(self._recent_ac)

        self._detect_peak(ir_ac)

    def _detect_peak(self, value):
        """Detect local maxima that clear an adaptive threshold; time them by sample count."""
        self._ac_hist.append(value)
        if len(self._ac_hist) < 3:
            return
        a, b, c = self._ac_hist  # b is the middle (previous) sample
        threshold = max(self.MIN_PEAK_AMPLITUDE,
                        self.PEAK_THRESHOLD_FRACTION * max(self._recent_ac))
        if not (b > a and b >= c and b > threshold):
            return

        peak_sample = self._sample_count - 1
        if self._last_peak_sample is None:
            self._last_peak_sample = peak_sample
            return

        interval = (peak_sample - self._last_peak_sample) / self.SAMPLE_RATE_HZ
        if interval < self.MIN_PEAK_INTERVAL:
            return  # too soon: noise or dicrotic notch, keep the earlier beat
        self._last_peak_sample = peak_sample
        if interval > self.MAX_PEAK_INTERVAL:
            self._intervals.clear()  # missed a beat; start averaging again
            return

        self._intervals.append(interval)
        if len(self._intervals) >= self.MIN_INTERVALS:
            median_interval = statistics.median(self._intervals)
            with self._lock:
                self.bpm = round(60.0 / median_interval, 1)

    def _update_spo2(self):
        # Only trust SpO2 while a pulse is actually being tracked.
        if not self.finger_detected or self.bpm is None:
            return
        n = len(self._ir_ac_buffer)
        if n < self.SAMPLE_RATE_HZ * 2:
            return

        ir_ac_vals = list(self._ir_ac_buffer)
        red_ac_vals = list(self._red_ac_buffer)
        ir_rms = math.sqrt(sum(x * x for x in ir_ac_vals) / len(ir_ac_vals))
        red_rms = math.sqrt(sum(x * x for x in red_ac_vals) / len(red_ac_vals))
        ir_dc = sum(self._ir_buffer) / len(self._ir_buffer)
        red_dc = sum(self._red_buffer) / len(self._red_buffer)

        if ir_dc == 0 or red_dc == 0 or ir_rms == 0:
            return

        # Standard ratio-of-ratios approach with Maxim's empirical calibration curve.
        R = (red_rms / red_dc) / (ir_rms / ir_dc)
        if not (self.SPO2_MIN_R <= R <= self.SPO2_MAX_R):
            return  # implausible ratio = motion/noise artifact, skip this update

        spo2 = -45.060 * (R ** 2) + 30.354 * R + 94.845
        spo2 = max(0.0, min(100.0, spo2))

        self._spo2_history.append(spo2)
        self._last_spo2_sample = self._sample_count
        with self._lock:
            self.spo2 = round(sum(self._spo2_history) / len(self._spo2_history), 1)

    def get_readings(self):
        """Return a snapshot dict: bpm, spo2, red, ir, ac_amp, finger_detected."""
        with self._lock:
            return {
                "bpm": self.bpm,
                "spo2": self.spo2,
                "red": self.red,
                "ir": self.ir,
                "ac_amp": self.ac_amp,
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
            extra = f"(IR={r['ir']}, pulse amp={r['ac_amp']:.0f})"
            if monitor.last_error:
                print(f"Sensor read error: {monitor.last_error}")
            elif not r["finger_detected"]:
                print(f"No finger detected...   {extra}")
            elif r["bpm"] is None:
                print(f"Finger detected, waiting for a pulse...   {extra}")
            else:
                spo2_str = f"{r['spo2']:.1f}%" if r["spo2"] is not None else "calculating..."
                print(f"BPM: {r['bpm']:>6.1f}   SpO2: {spo2_str:>14}   {extra}")

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