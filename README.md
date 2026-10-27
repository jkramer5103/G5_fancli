# fancli

Linux fan control using a hardware-specific embedded-controller IO sequence.

The repository and service names are historical. They do not establish compatibility with any particular manufacturer or model.

## Hardware

- Hardware compatibility must be verified for the actual machine; this repository does not identify a confirmed target.
- NVIDIA GPU temperature is read through `nvidia-smi`
- Fan control uses `outb`, provided by the `ioport` package on Debian/Ubuntu

This is hardware-specific low-level control. Use it only on compatible machines and keep an eye on temperatures after installing or changing the curve.

## Install

```bash
sudo apt install ioport
sudo ./install.sh
```

The installer copies the daemon and CLI to `/usr/local/bin`, installs the systemd service, creates `/etc/g5ge-fan/config.json` if missing, and starts `g5ge-fancontrol`.

## Usage

```bash
fancli status
fancli watch
sudo fancli set 70
sudo fancli auto
fancli curve
sudo fancli max
sudo fancli max-stop
```

`set` writes a temporary manual override under `/run/g5ge-fan`. `auto` clears the override and returns to the configured fan curve. `max` stops the daemon and keeps the fan at full speed until `max-stop` restarts automatic control.

## Modes

`status` and `watch` show the requested fan target, not a measured speed or RPM. CPU temperature is currently the hottest readable thermal zone, which may include sensors other than the CPU. A GPU sensor error shows the last known temperature (initially zero).

MAX mode uses a separate background process. Its process lifecycle and transitions still need testing on the target hardware.

## Configuration

Default config path:

```text
/etc/g5ge-fan/config.json
```

Automatic mode with the default curve:

```json
{"mode": "auto"}
```

Custom curve:

```json
{
  "mode": "auto",
  "curve": [[0, 10], [50, 30], [65, 60], [75, 80], [85, 100]]
}
```

Manual config:

```json
{"mode": "manual", "speed": 60}
```

Restart the service after changing persistent config:

```bash
sudo systemctl restart g5ge-fancontrol
```

The daemon also rereads configuration each cycle. Modes must be `auto` or `manual`; manual speeds must be finite numbers from 0 to 100. Curves must contain at least one `[temperature, percentage]` pair, with finite numbers, strictly increasing temperatures, and percentages from 0 to 100. Invalid overrides fall back to persistent config; invalid persistent config falls back to the default curve.

## Development

```bash
python3 -m pytest -q
```
