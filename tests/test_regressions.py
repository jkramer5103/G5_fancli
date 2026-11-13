"""Regression coverage with no real fan, process or service operations."""

import json
import subprocess
from unittest.mock import Mock

import pytest

from fancli_config import DEFAULT_CURVE, load_config
from tests.test_cli_display import cli, reload, strip_ansi
from tests.test_daemon_logic import daemon, reload as reload_daemon


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    reload()
    reload_daemon()
    for module in (cli, daemon):
        for name, filename in [('STATUS_PATH', 'status.json'),
                               ('OVERRIDE_PATH', 'override.json'),
                               ('CONFIG_PATH', 'config.json')]:
            monkeypatch.setattr(module, name, str(tmp_path / filename))
    monkeypatch.setattr(cli, 'RUN_DIR', str(tmp_path))
    # A missed mock must fail the test rather than run a command.
    monkeypatch.setattr(subprocess, 'run', Mock(side_effect=AssertionError('unmocked command')))


def write_status(mode, speed_pct, cpu_temp, gpu_temp, timestamp=None):
    daemon.write_status(cli.STATUS_PATH, mode, speed_pct, cpu_temp,
                        gpu_temp if gpu_temp is not None else 0, gpu_temp is not None)
    if timestamp is not None:
        with open(cli.STATUS_PATH) as f:
            data = json.load(f)
        data['timestamp'] = timestamp
        with open(cli.STATUS_PATH, 'w') as f:
            json.dump(data, f)


@pytest.mark.parametrize('data', [
    None, [], 'mode', {'mode': 'typo'},
    {'mode': 'manual'}, {'mode': 'manual', 'speed': '70'},
    {'mode': 'manual', 'speed': True}, {'mode': 'manual', 'speed': -1},
    {'mode': 'manual', 'speed': 101}, {'mode': 'manual', 'speed': float('nan')},
    {'mode': 'auto', 'curve': []}, {'mode': 'auto', 'curve': None},
    {'mode': 'auto', 'curve': [[0, 10], [0, 50]]},
    {'mode': 'auto', 'curve': [[50, 10], [0, 50]]},
    {'mode': 'auto', 'curve': [[0, 101]]},
    {'mode': 'auto', 'curve': [[0, '50']]},
    {'mode': 'auto', 'curve': [[0, True]]},
    {'mode': 'auto', 'curve': [[0, float('inf')]]},
    {'mode': 'auto', 'curve': [[10**400, 50]]},
    {'mode': 'auto', 'curve': [[0, 10, 20]]},
])
def test_invalid_configs_fall_back_consistently(data):
    with open(cli.OVERRIDE_PATH, 'w') as f:
        json.dump(data, f)
    with open(cli.CONFIG_PATH, 'w') as f:
        json.dump({'mode': 'auto', 'curve': [[0, 20], [100, 90]]}, f)
    assert load_config(cli.OVERRIDE_PATH) is None
    config, source = daemon.get_active_config()
    curve, manual_speed, _ = cli.get_active_curve_info()
    assert source == 'config'
    assert config['curve'] == curve == [[0, 20], [100, 90]]
    assert manual_speed is None


def test_invalid_persistent_config_uses_default():
    with open(cli.CONFIG_PATH, 'w') as f:
        f.write('{"mode": "auto", "curve": []}')
    config, source = daemon.get_active_config()
    assert source == 'default'
    assert config['curve'] == DEFAULT_CURVE
    assert cli.get_active_curve_info()[0] == DEFAULT_CURVE


@pytest.mark.parametrize('mode', ['auto', 'manual', 'max'])
def test_stale_status_is_reported_in_every_mode(mode, capsys):
    write_status(mode, 100, 80, 75, timestamp=0)
    cli.cmd_status(None)
    assert 'STALE DATA' in capsys.readouterr().out


@pytest.mark.parametrize('data', [None, [], {}, {'mode': 'max'},
                                {'mode': 'max', 'speed_pct': '100'}])
def test_invalid_status_is_unavailable(data):
    with open(cli.STATUS_PATH, 'w') as f:
        json.dump(data, f)
    assert cli.read_status() is None


@pytest.mark.parametrize('speed', [0, 99, 100])
def test_status_box_fits_full_bar(speed):
    write_status('auto', speed, 100, None)
    lines = [strip_ansi(line) for line in cli.render_status(cli.read_status())]
    assert len({len(line) for line in lines}) == 1
    assert 'Fan Target:' in '\n'.join(lines)


def test_max_is_a_full_speed_override():
    cli.cmd_max(None)
    assert load_config(cli.OVERRIDE_PATH)['speed'] == 100
    cli.cmd_max_stop(None)
    assert load_config(cli.OVERRIDE_PATH) is None


class FakeEc:
    def __init__(self):
        self.commands = []

    def command(self, cmd, *data):
        self.commands.append((cmd, *data))

    def read(self, address):
        return {0xD0: 0x01, 0xD1: 0xD1, 0xD2: 0x00, 0xD3: 0x00}[address]


def test_both_fans_get_the_duty():
    ec = FakeEc()
    daemon.send_fan_speeds(ec, 0xAB)
    assert ec.commands == [(0x99, 1, 0xAB), (0x99, 2, 0xAB)]


def test_tach_converts_to_rpm():
    assert daemon.read_fan_rpms(FakeEc()) == [2156220 // 0x01D1, 0]


def test_ec_handshake_times_out_instead_of_hanging(monkeypatch):
    ec = daemon.EmbeddedController.__new__(daemon.EmbeddedController)
    monkeypatch.setattr(ec, '_status', lambda: daemon.EC_IBF)  # never drains
    monkeypatch.setattr(daemon, 'EC_TIMEOUT', 0.01)
    with pytest.raises(daemon.EcError):
        ec.command(0x99, 1, 0xFF)


def test_watch_restores_cursor_on_system_exit(monkeypatch, capsys):
    monkeypatch.setattr(cli, 'read_status', Mock(side_effect=SystemExit(1)))
    with pytest.raises(SystemExit):
        cli.cmd_watch(None)
    assert capsys.readouterr().out.endswith('\033[?25h')


def test_decimal_curve_labels_match_domain():
    chart = cli.render_curve_chart([[12.5, 20], [62.5, 80]])
    assert chart[-2].split() == ['12.5', '22.5', '32.5', '42.5', '52.5', '62.5']
