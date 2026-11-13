#!/usr/bin/env python3
"""Fan control daemon using a hardware-specific EC IO sequence."""

import json
import logging
import os
import signal
import subprocess
import sys
import time
import glob

from fancli_config import DEFAULT_CURVE, interpolate_curve, load_config

CONFIG_PATH = '/etc/g5ge-fan/config.json'
OVERRIDE_PATH = '/run/g5ge-fan/override.json'
STATUS_PATH = '/run/g5ge-fan/status.json'
PORT_PATH = '/dev/port'
CYCLE_SECONDS = 2
# Above this temperature a manual target is raised to full speed, so a low
# manual setting (from the CLI or the bar widget) can never cook the machine.
SAFETY_TEMP = 90


def pct_to_raw(percent):
    """Convert 0-100% to 0-255 raw EC value."""
    return round(percent * 255 / 100)


def read_cpu_temp(glob_pattern=None):
    """Return max CPU temp in °C across all thermal zones, or None."""
    if glob_pattern is None:
        glob_pattern = '/sys/class/thermal/thermal_zone*/temp'
    paths = glob.glob(glob_pattern)
    if not paths:
        return None
    temps = []
    for p in paths:
        try:
            with open(p) as f:
                temps.append(int(f.read().strip()) / 1000.0)
        except (OSError, ValueError):
            continue
    return max(temps) if temps else None


def read_gpu_temp():
    """Run nvidia-smi and return GPU temp as int, or None on failure."""
    try:
        result = subprocess.run(
            ['nvidia-smi', '--query-gpu=temperature.gpu', '--format=csv,noheader'],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return None
        return int(result.stdout.strip())
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


class GpuState:
    """Stateful tracker for GPU temperature with fallback."""
    def __init__(self):
        self.last_temp = 0
        self.sensor_ok = True
        self.error_logged = False


def read_gpu_with_fallback(state):
    """Read GPU temp, falling back to last known value on failure.
    Returns (temp, sensor_ok). Updates state in-place."""
    temp = read_gpu_temp()
    if temp is not None:
        state.last_temp = temp
        state.sensor_ok = True
        state.error_logged = False
        return temp, True
    else:
        state.sensor_ok = False
        return state.last_temp, False


EC_DATA = 0x62
EC_CMD = 0x66          # command (write) / status (read)
EC_IBF = 0x02          # input buffer full: EC hasn't taken the last byte yet
EC_OBF = 0x01          # output buffer full: a byte is ready to read
EC_SET_FAN = 0x99      # Clevo-style "set fan duty": 0x99, fan index, duty
EC_READ = 0x80         # ACPI "read EC RAM": 0x80, address -> byte
FANS = (1, 2)          # fan 1 (CPU) and fan 2 (GPU) are driven separately
FAN_DUTY_REGS = (0xCE, 0xCF)
FAN_TACH_REGS = (0xD0, 0xD2)   # 16-bit big endian, rpm = TACH_CONSTANT / value
TACH_CONSTANT = 2156220
EC_TIMEOUT = 0.1


class EcError(OSError):
    pass


class EmbeddedController:
    """Minimal EC port protocol over /dev/port.

    Every byte must wait for the EC to drain its input buffer first; without
    that handshake the EC silently drops bytes and keeps its own fan curve.
    """

    def __init__(self, port_path=None):
        self.fd = os.open(port_path or PORT_PATH, os.O_RDWR)

    def close(self):
        os.close(self.fd)

    def _status(self):
        return os.pread(self.fd, 1, EC_CMD)[0]

    def _wait(self, ready):
        deadline = time.monotonic() + EC_TIMEOUT
        while not ready(self._status()):
            if time.monotonic() > deadline:
                raise EcError("EC handshake timed out")
            time.sleep(0.0002)

    def _write(self, port, value):
        self._wait(lambda st: not st & EC_IBF)
        os.pwrite(self.fd, bytes([value]), port)

    def command(self, cmd, *data):
        self._write(EC_CMD, cmd)
        for value in data:
            self._write(EC_DATA, value)
        self._wait(lambda st: not st & EC_IBF)

    def read(self, address):
        self.command(EC_READ, address)
        self._wait(lambda st: st & EC_OBF)
        return os.pread(self.fd, 1, EC_DATA)[0]


def send_fan_speeds(ec, raw):
    """Set every fan to the same raw duty (0-255)."""
    for fan in FANS:
        ec.command(EC_SET_FAN, fan, raw)


def read_fan_rpms(ec):
    """Return measured RPM per fan (None when unreadable)."""
    rpms = []
    for reg in FAN_TACH_REGS:
        try:
            value = ec.read(reg) << 8 | ec.read(reg + 1)
            rpms.append(TACH_CONSTANT // value if value else 0)
        except OSError:
            rpms.append(None)
    return rpms


def write_status(path, mode, speed_pct, cpu_temp, gpu_temp, gpu_sensor_ok, rpms=None):
    """Atomically write status.json."""
    data = {
        'mode': mode,
        'speed_pct': round(speed_pct),
        'cpu_temp': round(cpu_temp) if cpu_temp is not None else 0,
        'gpu_temp': round(gpu_temp),
        'gpu_sensor_ok': gpu_sensor_ok,
        'fan_rpm': list(rpms or []),
        'timestamp': time.time(),
    }
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    os.rename(tmp, path)
    os.chmod(path, 0o644)


def get_active_config():
    """Load override if present, else load config. Returns (config_dict, source)."""
    override = load_config(OVERRIDE_PATH)
    if override is not None:
        return override, 'override'
    config = load_config(CONFIG_PATH)
    if config is not None:
        return config, 'config'
    logging.warning("No valid config found; using auto mode with default curve")
    return {'mode': 'auto', 'curve': DEFAULT_CURVE}, 'default'


def run_daemon():
    """Main daemon loop."""
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s %(levelname)s %(message)s',
        stream=sys.stdout,
    )
    logging.info("g5ge-fandaemon starting")

    gpu_state = GpuState()
    ec = None
    running = True

    def handle_sigterm(signum, frame):
        nonlocal running
        logging.info("Received SIGTERM, shutting down")
        running = False

    signal.signal(signal.SIGTERM, handle_sigterm)

    while running:
        cycle_start = time.monotonic()

        config, source = get_active_config()
        mode = config.get('mode', 'auto')

        cpu_temp = read_cpu_temp()
        if cpu_temp is None:
            logging.error("Could not read CPU temperature")
            cpu_temp = 0

        gpu_temp, gpu_ok = read_gpu_with_fallback(gpu_state)
        if not gpu_ok and not gpu_state.error_logged:
            logging.error("nvidia-smi failed; using last known GPU temp=%d", gpu_temp)
            gpu_state.error_logged = True
        elif not gpu_ok:
            logging.debug("nvidia-smi still failing; gpu_temp=%d (cached)", gpu_temp)

        temp_input = max(cpu_temp, gpu_temp)
        if mode == 'manual':
            speed_pct = float(config.get('speed', 50))
            if temp_input >= SAFETY_TEMP and speed_pct < 100:
                logging.warning("%.0f°C >= %d°C: overriding manual %.0f%% to 100%%",
                                temp_input, SAFETY_TEMP, speed_pct)
                speed_pct = 100.0
        else:
            curve = config.get('curve', DEFAULT_CURVE)
            speed_pct = interpolate_curve(curve, temp_input)

        speed_pct = max(0.0, min(100.0, speed_pct))
        raw = pct_to_raw(speed_pct)


        rpms = []
        try:
            if ec is None:
                ec = EmbeddedController()
            send_fan_speeds(ec, raw)
            rpms = read_fan_rpms(ec)
        except OSError as e:
            logging.error("EC access via %s failed: %s", PORT_PATH, e)

        logging.debug(
            "mode=%s source=%s cpu=%.1f gpu=%d speed=%.1f%% raw=%d rpm=%s",
            mode, source, cpu_temp, gpu_temp, speed_pct, raw, rpms
        )

        try:
            write_status(STATUS_PATH, mode, speed_pct, cpu_temp, gpu_temp, gpu_ok, rpms)
        except OSError as e:
            logging.error("Failed to write status: %s", e)

        elapsed = time.monotonic() - cycle_start
        sleep_for = max(0, CYCLE_SECONDS - elapsed)
        if running:
            time.sleep(sleep_for)

    logging.info("g5ge-fandaemon stopped")


if __name__ == '__main__':
    run_daemon()
