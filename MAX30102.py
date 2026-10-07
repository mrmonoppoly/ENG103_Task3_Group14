#!/usr/bin/env python3
"""
max30102_reader.py

Reads a MAX30102 pulse-oximeter/heart-rate sensor over I2C on a Raspberry Pi,
and continuously estimates:
    - BPM   (heart rate, beats per minute)
    - SpO2  (blood oxygen saturation, percent)

These are kept up to date in a background thread and exposed as plain
attributes (`monitor.bpm`, `monitor.spo2`) so other scripts can import this
module and read them at any time. Both are None when there is no finger, no
detectable pulse, or the sensor can't be read.

ALGORITHM: this uses Maxim's reference valley-detection / ratio-of-ratios
approach (the same method used in Maxim's app note and in widely-shared
MAX30102 Python drivers), ported in from a standalone hrcalc.py-style
implementation. It needs the sensor's FIFO averaging set to 4 samples
(SMP_AVE=4) so the effective sample rate feeding the algorithm is 25 Hz,
which is what the algorithm's constants assume. This is NOT a medical
device and should not be used for health/medical decisions.

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
    pip3 install smbus2 numpy --break-system-packages
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
import sys
import threading
import time

import numpy as np

try:
    from smbus2 import SMBus
except ImportError:
    print("Missing dependency. Install it with:")
    print("    pip3 install smbus2 numpy --break-system-packages")
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

# FIFO_CONFIG = 0x4f: SMP_AVE=4 (sample averaging), FIFO rollover disabled,
# FIFO almost-full flag at 17. The SMP_AVE=4 part matters: it makes the chip
# average every 4 raw 100 Hz samples into 1 FIFO sample, so samples arrive
# at an effective 25 Hz. The HR/SpO2 algorithm's SAMPLE_FREQ constant below
# assumes exactly this effective rate.
FIFO_CONFIG_VALUE = 0x4F

# SPO2_CONFIG = 0x27: ADC range 4096 nA, sample rate 100 Hz (before
# averaging), pulse width 411 us / 18-bit resolution.
SPO2_CONFIG_VALUE = 0x27


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

        self._write(REG_FIFO_CONFIG, FIFO_CONFIG_VALUE)
        self._write(REG_MODE_CONFIG, MODE_SPO2)
        self._write(REG_SPO2_CONFIG, SPO2_CONFIG_VALUE)

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


# ---------------------------------------------------------------------------
# HR / SpO2 calculation — ported from Maxim's reference algorithm
# (valley detection + per-beat AC/DC ratio, median across beats).
# Operates on fixed-size buffers of raw ir/red samples.
# ---------------------------------------------------------------------------
HR_SAMPLE_FREQ = 25     # effective sample rate feeding this algorithm (after SMP_AVE=4)
HR_MA_SIZE = 4          # moving-average window used when finding valleys ("DO NOT CHANGE" per Maxim)
HR_BUFFER_SIZE = 100    # samples per calculation (4 seconds at 25 Hz)


def calc_hr_and_spo2(ir_data, red_data):
    """
    By detecting peaks of the PPG cycle and the corresponding AC/DC of the
    red/infra-red signal, the ratio for SpO2 is computed.
    Returns (hr, hr_valid, spo2, spo2_valid).
    """
    ir_mean = int(np.mean(ir_data))

    # Remove DC mean and invert so the peak finder (which looks for maxima)
    # actually finds the pulse valleys in the raw (uninverted) signal.
    x = -1 * (np.array(ir_data) - ir_mean)

    # 4-point moving average
    for i in range(x.shape[0] - HR_MA_SIZE):
        x[i] = np.sum(x[i:i + HR_MA_SIZE]) / HR_MA_SIZE

    n_th = int(np.mean(x))
    n_th = 30 if n_th < 30 else n_th
    n_th = 60 if n_th > 60 else n_th

    ir_valley_locs, n_peaks = _find_peaks(x, HR_BUFFER_SIZE, n_th, 4, 15)

    peak_interval_sum = 0
    if n_peaks >= 2:
        for i in range(1, n_peaks):
            peak_interval_sum += (ir_valley_locs[i] - ir_valley_locs[i - 1])
        peak_interval_sum = int(peak_interval_sum / (n_peaks - 1))
        hr = int(HR_SAMPLE_FREQ * 60 / peak_interval_sum)
        hr_valid = True
    else:
        hr = -999
        hr_valid = False

    # ---------- SpO2 ----------
    exact_ir_valley_locs_count = n_peaks

    for i in range(exact_ir_valley_locs_count):
        if ir_valley_locs[i] > HR_BUFFER_SIZE:
            return hr, hr_valid, -999, False

    i_ratio_count = 0
    ratio = []
    red_dc_max_index = -1
    ir_dc_max_index = -1

    for k in range(exact_ir_valley_locs_count - 1):
        red_dc_max = -16777216
        ir_dc_max = -16777216
        if ir_valley_locs[k + 1] - ir_valley_locs[k] > 3:
            for i in range(ir_valley_locs[k], ir_valley_locs[k + 1]):
                if ir_data[i] > ir_dc_max:
                    ir_dc_max = ir_data[i]
                    ir_dc_max_index = i
                if red_data[i] > red_dc_max:
                    red_dc_max = red_data[i]
                    red_dc_max_index = i

            red_ac = int((red_data[ir_valley_locs[k + 1]] - red_data[ir_valley_locs[k]])
                         * (red_dc_max_index - ir_valley_locs[k]))
            red_ac = red_data[ir_valley_locs[k]] + int(red_ac / (ir_valley_locs[k + 1] - ir_valley_locs[k]))
            red_ac = red_data[red_dc_max_index] - red_ac

            ir_ac = int((ir_data[ir_valley_locs[k + 1]] - ir_data[ir_valley_locs[k]])
                        * (ir_dc_max_index - ir_valley_locs[k]))
            ir_ac = ir_data[ir_valley_locs[k]] + int(ir_ac / (ir_valley_locs[k + 1] - ir_valley_locs[k]))
            ir_ac = ir_data[ir_dc_max_index] - ir_ac

            nume = red_ac * ir_dc_max
            denom = ir_ac * red_dc_max
            if (denom > 0 and i_ratio_count < 5) and nume != 0:
                ratio.append(int(((nume * 100) & 0xFFFFFFFF) / denom))
                i_ratio_count += 1

    ratio = sorted(ratio)
    mid_index = int(i_ratio_count / 2)

    ratio_ave = 0
    if mid_index > 1:
        ratio_ave = int((ratio[mid_index - 1] + ratio[mid_index]) / 2)
    elif len(ratio) != 0:
        ratio_ave = ratio[mid_index]

    if 2 < ratio_ave < 184:
        spo2 = -45.060 * (ratio_ave ** 2) / 10000.0 + 30.054 * ratio_ave / 100.0 + 94.845
        spo2_valid = True
    else:
        spo2 = -999
        spo2_valid = False

    return hr, hr_valid, spo2, spo2_valid


def _find_peaks(x, size, min_height, min_dist, max_num):
    ir_valley_locs, n_peaks = _find_peaks_above_min_height(x, size, min_height, max_num)
    ir_valley_locs, n_peaks = _remove_close_peaks(n_peaks, ir_valley_locs, x, min_dist)
    n_peaks = min([n_peaks, max_num])
    return ir_valley_locs, n_peaks


def _find_peaks_above_min_height(x, size, min_height, max_num):
    i = 0
    n_peaks = 0
    ir_valley_locs = []
    while i < size - 1:
        if x[i] > min_height and x[i] > x[i - 1]:
            n_width = 1
            while i + n_width < size - 1 and x[i] == x[i + n_width]:
                n_width += 1
            if x[i] > x[i + n_width] and n_peaks < max_num:
                ir_valley_locs.append(i)
                n_peaks += 1
                i += n_width + 1
            else:
                i += n_width
        else:
            i += 1
    return ir_valley_locs, n_peaks


def _remove_close_peaks(n_peaks, ir_valley_locs, x, min_dist):
    sorted_indices = sorted(ir_valley_locs, key=lambda i: x[i])
    sorted_indices.reverse()

    i = -1
    while i < n_peaks:
        old_n_peaks = n_peaks
        n_peaks = i + 1
        j = i + 1
        while j < old_n_peaks:
            n_dist = (sorted_indices[j] - sorted_indices[i]) if i != -1 else (sorted_indices[j] + 1)
            if n_dist > min_dist or n_dist < -1 * min_dist:
                sorted_indices[n_peaks] = sorted_indices[j]
                n_peaks += 1
            j += 1
        i += 1

    sorted_indices[:n_peaks] = sorted(sorted_indices[:n_peaks])
    return sorted_indices, n_peaks


class HeartRateSpO2Monitor:
    """
    Wraps the MAX30102 driver, runs a background polling thread, and keeps
    self.bpm / self.spo2 (and self.red / self.ir / self.finger_detected)
    continuously up to date using Maxim's reference HR/SpO2 algorithm.
    """

    FINGER_THRESHOLD = 50000   # raw IR/red DC mean below this => no finger present
    RECALC_EVERY = 25          # recalculate after this many new samples (~1s at 25Hz)
    BPM_SMOOTH_N = 4           # rolling average length for displayed BPM
    PULSE_TIMEOUT = 3.0        # seconds without a valid calc before BPM/SpO2 clear
    SPO2_TIMEOUT = 5.0

    def __init__(self, bus_number=1, sensor=None):
        self.sensor = sensor if sensor is not None else MAX30102(bus_number=bus_number)

        self._ir_data = []
        self._red_data = []
        self._samples_since_calc = 0
        self._bpm_history = collections.deque(maxlen=self.BPM_SMOOTH_N)

        self._last_valid_bpm_time = 0.0
        self._last_valid_spo2_time = 0.0

        self.bpm = None
        self.spo2 = None
        self.red = 0
        self.ir = 0
        self.ac_amp = 0.0          # rough signal-strength indicator, for tuning
        self.finger_detected = False
        self.last_error = None

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
        poll_interval = 0.01
        while not self._stop_flag.is_set():
            try:
                for red, ir in self.sensor.read_fifo():
                    self._process_sample(red, ir)
                self.last_error = None
            except OSError as e:
                self.last_error = str(e)
                self.finger_detected = False
                self._clear_readings()
                self._reset_buffers()
                time.sleep(0.5)
                try:
                    self.sensor.reinit()
                except (OSError, AttributeError):
                    pass
                continue

            self._expire_stale()
            time.sleep(poll_interval)

    # -- state helpers ------------------------------------------------
    def _clear_readings(self):
        with self._lock:
            self.bpm = None
            self.spo2 = None

    def _reset_buffers(self):
        self._ir_data.clear()
        self._red_data.clear()
        self._samples_since_calc = 0
        self._bpm_history.clear()
        self.ac_amp = 0.0

    def _expire_stale(self):
        now = time.time()
        if self.bpm is not None and now - self._last_valid_bpm_time > self.PULSE_TIMEOUT:
            with self._lock:
                self.bpm = None
            self._bpm_history.clear()
        if self.spo2 is not None and now - self._last_valid_spo2_time > self.SPO2_TIMEOUT:
            with self._lock:
                self.spo2 = None

    # -- per-sample processing ------------------------------------------
    def _process_sample(self, red, ir):
        self.red = red
        self.ir = ir

        self.finger_detected = ir > self.FINGER_THRESHOLD
        if not self.finger_detected:
            self._clear_readings()
            self._reset_buffers()
            return

        self._ir_data.append(ir)
        self._red_data.append(red)
        while len(self._ir_data) > HR_BUFFER_SIZE:
            self._ir_data.pop(0)
            self._red_data.pop(0)

        self._samples_since_calc += 1
        if len(self._ir_data) == HR_BUFFER_SIZE:
            self.ac_amp = max(self._ir_data) - min(self._ir_data)
            if self._samples_since_calc >= self.RECALC_EVERY:
                self._samples_since_calc = 0
                self._recalculate()

    def _recalculate(self):
        hr, hr_valid, spo2, spo2_valid = calc_hr_and_spo2(self._ir_data, self._red_data)

        if hr_valid:
            self._bpm_history.append(hr)
            now = time.time()
            with self._lock:
                self.bpm = round(float(np.mean(self._bpm_history)), 1)
            self._last_valid_bpm_time = now

        if spo2_valid:
            now = time.time()
            with self._lock:
                self.spo2 = round(float(spo2), 1)
            self._last_valid_spo2_time = now

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
            extra = f"(IR={r['ir']}, amp={r['ac_amp']:.0f})"
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