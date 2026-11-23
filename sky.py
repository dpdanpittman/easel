"""The sky at the configured place (settings `sky`: latitude, longitude), computed locally: sunrise, sunset, daylight, sun altitude, moon phase.

No network, standard library only. Sun times use the NOAA solar position equations (good to about a minute);
the moon phase uses the mean synodic month from a known new moon (good to a few hours, plenty for a picture).
"""
import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import config  # noqa: E402

LAT = float(config.get()["sky"]["latitude"])      # settings `sky`
LON = float(config.get()["sky"]["longitude"])
TZ = ZoneInfo(config.get()["time"]["timezone"])
SYNODIC = 29.530588853                 # days
NEW_MOON_EPOCH = datetime(2000, 1, 6, 18, 14, tzinfo=timezone.utc)
PHASE_NAMES = ["New moon", "Waxing crescent", "First quarter", "Waxing gibbous",
               "Full moon", "Waning gibbous", "Last quarter", "Waning crescent"]


def _julian_day(dt_utc):
    return dt_utc.timestamp() / 86400.0 + 2440587.5


def _solar(jd):
    """(declination radians, equation of time minutes) for a Julian day."""
    t = (jd - 2451545.0) / 36525.0
    l0 = math.radians((280.46646 + t * (36000.76983 + t * 0.0003032)) % 360)
    m = math.radians(357.52911 + t * (35999.05029 - 0.0001537 * t))
    e = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
    c = (math.sin(m) * (1.914602 - t * (0.004817 + 0.000014 * t)) + math.sin(2 * m) * (0.019993 - 0.000101 * t)
         + math.sin(3 * m) * 0.000289)
    true_long = l0 + math.radians(c)
    omega = math.radians(125.04 - 1934.136 * t)
    app_long = true_long - math.radians(0.00569 + 0.00478 * math.sin(omega))
    eps0 = 23 + (26 + (21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))) / 60) / 60
    eps = math.radians(eps0 + 0.00256 * math.cos(omega))
    decl = math.asin(math.sin(eps) * math.sin(app_long))
    y = math.tan(eps / 2) ** 2
    eot = 4 * math.degrees(y * math.sin(2 * l0) - 2 * e * math.sin(m) + 4 * e * y * math.sin(m) * math.cos(2 * l0)
                           - 0.5 * y * y * math.sin(4 * l0) - 1.25 * e * e * math.sin(2 * m))
    return decl, eot


def sun_altitude(dt):
    """Sun altitude in degrees at an aware datetime."""
    utc = dt.astimezone(timezone.utc)
    decl, eot = _solar(_julian_day(utc))
    minutes = utc.hour * 60 + utc.minute + utc.second / 60
    tst = (minutes + eot + 4 * LON) % 1440
    ha = math.radians(tst / 4 - 180)
    lat = math.radians(LAT)
    cos_z = math.sin(lat) * math.sin(decl) + math.cos(lat) * math.cos(decl) * math.cos(ha)
    return 90 - math.degrees(math.acos(max(-1.0, min(1.0, cos_z))))


def sun_times(day):
    """(sunrise, sunset) as aware local datetimes, rounded to the minute, or (None, None) in polar day/night.

    The standard sunrise equation. sun_altitude() keeps the NOAA equations."""
    n = day.toordinal() + 1721425 - 2451545               # days since J2000, at noon
    j_star = n - LON / 360.0
    m = math.radians((357.5291 + 0.98560028 * j_star) % 360)
    c = 1.9148 * math.sin(m) + 0.02 * math.sin(2 * m) + 0.0003 * math.sin(3 * m)
    lam = math.radians((math.degrees(m) + c + 180 + 102.9372) % 360)
    j_transit = 2451545.0 + j_star + 0.0053 * math.sin(m) - 0.0069 * math.sin(2 * lam)
    sin_dec = math.sin(lam) * math.sin(math.radians(23.4397))
    cos_dec = math.cos(math.asin(sin_dec))
    phi = math.radians(LAT)
    cos_w = (math.sin(math.radians(-0.833)) - math.sin(phi) * sin_dec) / (math.cos(phi) * cos_dec)
    if not -1 <= cos_w <= 1:
        return None, None
    w = math.degrees(math.acos(cos_w))

    def to_dt(jd):
        secs = round((jd - 2440587.5) * 86400 / 60) * 60       # nearest minute
        return datetime.fromtimestamp(secs, timezone.utc).astimezone(TZ)

    return to_dt(j_transit - w / 360.0), to_dt(j_transit + w / 360.0)


def moon(dt):
    """Moon age (days), illuminated fraction, phase name and the next full and new moon dates."""
    age = ((dt.astimezone(timezone.utc) - NEW_MOON_EPOCH).total_seconds() / 86400.0) % SYNODIC
    frac = (1 - math.cos(2 * math.pi * age / SYNODIC)) / 2
    idx = int((age / SYNODIC) * 8 + 0.5) % 8
    to_full = (SYNODIC / 2 - age) % SYNODIC
    to_new = (SYNODIC - age) % SYNODIC
    return {"age": round(age, 2), "illum": round(frac, 3), "phase": PHASE_NAMES[idx], "waxing": age < SYNODIC / 2,
            "next_full": (dt + timedelta(days=to_full)).astimezone(TZ).date().isoformat(),
            "next_new": (dt + timedelta(days=to_new)).astimezone(TZ).date().isoformat()}


def _hm(dt):
    return dt.strftime("%-I:%M %p") if dt else ""


def _len(rise, sset):
    return (sset - rise).total_seconds() if rise and sset else 0


def snapshot(now=None):
    """Everything the page draws, for one moment."""
    now = (now or datetime.now(TZ)).astimezone(TZ)
    rise, sset = sun_times(now.date())
    y_rise, y_set = sun_times(now.date() - timedelta(days=1))
    day_s, yday_s = _len(rise, sset), _len(y_rise, y_set)
    delta_min = int(day_s // 60) - int(yday_s // 60)
    solstice = _next_solstice(now.date())
    return {
        "sunrise": _hm(rise), "sunset": _hm(sset),
        "sunrise_ts": int(rise.timestamp()) if rise else None, "sunset_ts": int(sset.timestamp()) if sset else None,
        "daylight": f"{int(day_s // 3600)}h {int(day_s % 3600 // 60):02d}m",
        "daylight_delta_min": delta_min,
        "sun_alt": round(sun_altitude(now), 1),
        "moon": moon(now),
        "solstice": solstice,
    }


def _next_solstice(d):
    """The next solstice date and its name (fixed civil dates, close enough for a caption)."""
    for year in (d.year, d.year + 1):
        for month, day, name in ((6, 21, "summer"), (12, 21, "winter")):
            s = d.replace(year=year, month=month, day=day)
            if s >= d:
                return {"date": s.isoformat(), "name": name, "days": (s - d).days}
    return None
