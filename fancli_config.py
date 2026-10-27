"""Configuration and curve logic shared by the daemon and CLI."""

import json
import math

DEFAULT_CURVE = [[0, 10], [50, 30], [65, 60], [75, 80], [85, 100]]


def is_number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def load_config(path):
    """Load a valid config, or return None so callers can use their fallback."""
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None

    if not isinstance(data, dict) or data.get('mode') not in ('auto', 'manual'):
        return None
    if data['mode'] == 'manual':
        speed = data.get('speed')
        if not is_number(speed) or not 0 <= speed <= 100:
            return None

    curve = data.get('curve', DEFAULT_CURVE)
    if not isinstance(curve, list) or not curve:
        return None
    previous_temp = None
    for point in curve:
        if not isinstance(point, list) or len(point) != 2:
            return None
        temp, speed = point
        if not is_number(temp) or not is_number(speed) or not 0 <= speed <= 100:
            return None
        if previous_temp is not None and temp <= previous_temp:
            return None
        previous_temp = temp
    data['curve'] = curve
    return data


def interpolate_curve(curve, temp):
    """Interpolate a validated curve, using its endpoints outside its range."""
    if temp <= curve[0][0]:
        return curve[0][1]
    for (t0, s0), (t1, s1) in zip(curve, curve[1:]):
        if temp <= t1:
            return s0 + (temp - t0) / (t1 - t0) * (s1 - s0)
    return curve[-1][1]
