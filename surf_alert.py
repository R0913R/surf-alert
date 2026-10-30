"""Surfweer-alert: checkt de Open-Meteo voorspelling per spot en stuurt een
Telegram-bericht als er een goed surfvenster aankomt.

Gebruik:
    python surf_alert.py              # check en verstuur
    python surf_alert.py --weekly     # weekoverzicht (voor zondag)
    python surf_alert.py --dry-run    # alleen printen
"""

import argparse
import json
import os
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

MARINE_URL = "https://marine-api.open-meteo.com/v1/marine"
MARINE_VARS = ["wave_height", "wave_period", "wave_direction",
               "swell_wave_height", "swell_wave_period", "swell_wave_direction"]
# Grof maar 10 dagen vooruit; vult gaten van het fijnere kustmodel (dat ~4 dagen reikt).
FALLBACK_WAVE_MODEL = "ecmwf_wam025"
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
COMPASS = ["N", "NNO", "NO", "ONO", "O", "OZO", "ZO", "ZZO",
           "Z", "ZZW", "ZW", "WZW", "W", "WNW", "NW", "NNW"]
DAYS = ["ma", "di", "wo", "do", "vr", "za", "zo"]
MONTHS = ["jan", "feb", "mrt", "apr", "mei", "jun",
          "jul", "aug", "sep", "okt", "nov", "dec"]


def get_json(url, params, attempts=4):
    """Haal JSON op, met nieuwe pogingen bij timeouts, 429 en serverfouten."""
    full_url = f"{url}?{urllib.parse.urlencode(params)}"
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(full_url, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code != 429 and e.code < 500 or attempt == attempts - 1:
                raise
        except OSError:  # o.a. URLError, timeouts en SSL-handshakefouten
            if attempt == attempts - 1:
                raise
        time.sleep(5 * 2 ** attempt)


def fill_gaps(hourly, fallback):
    """Vul ontbrekende (None) waarden aan met die van een ander model, per tijdstip."""
    index = {t: i for i, t in enumerate(fallback["time"])}
    for var in MARINE_VARS:
        if var not in hourly or var not in fallback:
            continue
        for i, t in enumerate(hourly["time"]):
            j = index.get(t)
            if hourly[var][i] is None and j is not None:
                hourly[var][i] = fallback[var][j]


def fetch_forecast(spot, settings):
    common = {
        "latitude": spot["lat"],
        "longitude": spot["lon"],
        "forecast_days": settings["forecast_days"],
        "timezone": settings["timezone"],
    }
    marine_params = common | {"hourly": ",".join(MARINE_VARS)}
    marine = get_json(MARINE_URL, marine_params)
    if None in marine["hourly"]["wave_height"]:
        try:
            # Het grove model ziet de kust vaak als land; kijk daarom iets verder op zee.
            lat, lon = spot.get("fallback_point", [spot["lat"], spot["lon"] - 0.25])
            fallback = get_json(MARINE_URL, marine_params | {
                "latitude": lat, "longitude": lon,
                "models": FALLBACK_WAVE_MODEL, "cell_selection": "sea",
            })
            fill_gaps(marine["hourly"], fallback["hourly"])
        except Exception as e:
            print(f"Aanvullend golfmodel mislukt bij {spot['name']}: {e}", file=sys.stderr)
    weather = get_json(WEATHER_URL, common | {
        "hourly": "wind_speed_10m,wind_direction_10m,wind_gusts_10m",
        "wind_speed_unit": "kn",
    })
    return merge_hourly(marine["hourly"], weather["hourly"])


def merge_hourly(marine, weather):
    """Combineer marine- en weerdata op tijdstip tot een lijst van uren."""
    wind = {t: i for i, t in enumerate(weather["time"])}
    hours = []
    for i, t in enumerate(marine["time"]):
        j = wind.get(t)
        if j is None:
            continue
        value = lambda var: (marine.get(var) or [None] * (i + 1))[i]
        # Zonder losse swelldata (sommige modellen) valt het terug op de totale golf.
        swell_period = value("swell_wave_period")
        swell_dir = value("swell_wave_direction")
        hours.append({
            "time": datetime.fromisoformat(t),
            "wave_height": marine["wave_height"][i],
            "swell_height": value("swell_wave_height"),
            "swell_period": value("wave_period") if swell_period is None else swell_period,
            "swell_dir": value("wave_direction") if swell_dir is None else swell_dir,
            "wind_kn": weather["wind_speed_10m"][j],
            "wind_dir": weather["wind_direction_10m"][j],
            "gust_kn": weather["wind_gusts_10m"][j],
        })
    return hours


def in_range(deg, rng):
    lo, hi = rng
    deg %= 360
    return lo <= deg <= hi if lo <= hi else deg >= lo or deg <= hi


def wave_ok(h, spot):
    if None in (h["wave_height"], h["swell_period"], h["swell_dir"]):
        return False
    lo, hi = spot["wave_height"]
    return (lo <= h["wave_height"] <= hi
            and h["swell_period"] >= spot["swell_period_min"]
            and in_range(h["swell_dir"], spot["swell_direction"]))


def wind_ok(wind_kn, wind_dir, spot):
    if wind_kn is None or wind_dir is None or wind_kn > spot["wind_max_kn"]:
        return False
    return wind_kn <= spot["glassy_kn"] or in_range(wind_dir, spot["offshore_wind"])


def is_good(h, spot):
    return wave_ok(h, spot) and wind_ok(h["wind_kn"], h["wind_dir"], spot)


def good_windows(hours, spot, settings):
    """Geef aaneengesloten blokken goede daglichturen terug."""
    windows, current = [], []
    for h in hours:
        daylight = is_daylight(h, settings)
        same_block = current and (h["time"] - current[-1]["time"]).total_seconds() == 3600
        if daylight and is_good(h, spot) and (not current or same_block):
            current.append(h)
            continue
        if len(current) >= settings["min_window_hours"]:
            windows.append(current)
        current = [h] if daylight and is_good(h, spot) else []
    if len(current) >= settings["min_window_hours"]:
        windows.append(current)
    return windows


def compass(deg):
    return COMPASS[round(deg / 22.5) % 16]


def avg(values):
    return sum(values) / len(values)


def day_label(t):
    return f"{DAYS[t.weekday()]} {t.day} {MONTHS[t.month - 1]}"


def is_daylight(h, settings):
    return settings["daylight_from"] <= h["time"].hour < settings["daylight_to"]


def wind_confidence(ensemble, window, spot):
    """Aandeel van alle ensemble-runs x uren in het venster met goede wind (0-1)."""
    index = {t: i for i, t in enumerate(ensemble["time"])}
    members = [k[len("wind_speed_10m"):] for k in ensemble if k.startswith("wind_speed_10m")]
    good = total = 0
    for h in window:
        i = index.get(h["time"].isoformat(timespec="minutes"))
        if i is None:
            continue
        for m in members:
            speed = ensemble[f"wind_speed_10m{m}"][i]
            direction = (ensemble.get(f"wind_direction_10m{m}") or [None] * (i + 1))[i]
            if speed is None or direction is None:
                continue
            total += 1
            good += wind_ok(speed, direction, spot)
    return good / total if total else None


def wave_agreement(model_hours, window, spot):
    """Hoeveel golfmodellen het venster grotendeels (>= helft van de uren) goed vinden."""
    times = {h["time"] for h in window}
    agree = counted = 0
    for hours in model_hours:
        in_window = [h for h in hours if h["time"] in times and h["wave_height"] is not None]
        if not in_window:
            continue
        counted += 1
        agree += sum(wave_ok(h, spot) for h in in_window) * 2 >= len(in_window)
    return agree, counted


def confidence(wind_frac, wave):
    """Zekerheid = de zwakste schakel van wind en golven, in procent."""
    agree, counted = wave
    parts = [p for p in (wind_frac, agree / counted if counted else None) if p is not None]
    return round(100 * min(parts)) if parts else None


def fetch_confidence_data(spot, settings):
    """Haal wind-ensemble en losse golfmodellen op; wat faalt wordt overgeslagen."""
    common = {
        "latitude": spot["lat"],
        "longitude": spot["lon"],
        "forecast_days": settings["forecast_days"],
        "timezone": settings["timezone"],
    }
    ensemble = None
    try:
        ensemble = get_json(ENSEMBLE_URL, common | {
            "models": settings.get("ensemble_model", "ecmwf_ifs025"),
            "hourly": "wind_speed_10m,wind_direction_10m",
            "wind_speed_unit": "kn",
        })["hourly"]
    except Exception as e:
        print(f"Wind-ensemble mislukt bij {spot['name']}: {e}", file=sys.stderr)

    model_hours = []
    for model in settings.get("wave_models", []):
        params = common | {"hourly": ",".join(MARINE_VARS), "models": model, "cell_selection": "sea"}
        try:
            marine = get_json(MARINE_URL, params)["hourly"]
            if all(v is None for v in marine["wave_height"]):
                lat, lon = spot.get("fallback_point", [spot["lat"], spot["lon"] - 0.25])
                marine = get_json(MARINE_URL, params | {"latitude": lat, "longitude": lon})["hourly"]
        except Exception as e:
            print(f"Golfmodel {model} mislukt bij {spot['name']}: {e}", file=sys.stderr)
            continue
        model_hours.append(marine_hours(marine))
    return ensemble, model_hours


def marine_hours(marine):
    """Zet marinedata om naar uren, zonder wind (voor het vergelijken van golfmodellen)."""
    times = marine["time"]
    no_wind = {"time": times, "wind_speed_10m": [None] * len(times),
               "wind_direction_10m": [None] * len(times), "wind_gusts_10m": [None] * len(times)}
    return merge_hourly(marine, no_wind)


def describe(spot_name, window):
    start, end = window[0]["time"], window[-1]["time"]
    wave = avg([h["wave_height"] for h in window])
    period = avg([h["swell_period"] for h in window])
    swell_dir = window[len(window) // 2]["swell_dir"]
    wind = avg([h["wind_kn"] for h in window])
    wind_dir = window[len(window) // 2]["wind_dir"]
    return (f"🏄 *{spot_name}* {day_label(start)} {start:%H}:00–{end.hour + 1:02d}:00\n"
            f"   golven {wave:.1f} m, swell {period:.0f} s uit {compass(swell_dir)}, "
            f"wind {wind:.0f} kn uit {compass(wind_dir)}")


def confidence_line(pct, wind, wave):
    if pct is None:
        return "\n   zekerheid onbekend"
    agree, counted = wave
    details = []
    if wind is not None:
        details.append(f"wind {round(100 * wind)}%")
    if counted:
        details.append(f"golven {agree}/{counted} modellen")
    return f"\n   zekerheid {pct}% ({', '.join(details)})"


def week_summary(spot, hours, settings):
    """Eén regel per dag: gemiddelde condities overdag en hoeveel uur goed is."""
    days = {}
    for h in hours:
        if is_daylight(h, settings) and h["wave_height"] is not None and h["wind_kn"] is not None:
            days.setdefault(h["time"].date(), []).append(h)
    lines = [f"*{spot['name']}*"]
    for day in sorted(days):
        hs = days[day]
        good = sum(is_good(h, spot) for h in hs)
        mark = "🔥" if good >= 4 else "✅" if good >= settings["min_window_hours"] else "▫️"
        periods = [h["swell_period"] for h in hs if h["swell_period"] is not None]
        period = f", {avg(periods):.0f} s" if periods else ""
        noon = hs[len(hs) // 2]
        lines.append(
            f"{mark} {day_label(hs[0]['time'])}: {avg([h['wave_height'] for h in hs]):.1f} m{period}, "
            f"wind {avg([h['wind_kn'] for h in hs]):.0f} kn {compass(noon['wind_dir'])}"
            + (f" ({good} u goed)" if good else ""))
    return "\n".join(lines)


def send_telegram(text):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data, timeout=30)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=Path(__file__).with_name("spots.toml"))
    parser.add_argument("--weekly", action="store_true", help="weekoverzicht in plaats van alert")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    config = tomllib.loads(Path(args.config).read_text())
    settings = config["settings"]
    if args.weekly:
        settings = settings | {"forecast_days": 7}

    blocks = []
    for spot in config["spot"]:
        try:
            hours = fetch_forecast(spot, settings)
        except Exception as e:  # één spot die faalt mag de rest niet blokkeren
            print(f"Fout bij {spot['name']}: {e}", file=sys.stderr)
            continue
        if args.weekly:
            blocks.append(week_summary(spot, hours, settings))
            continue
        windows = good_windows(hours, spot, settings)
        if not windows:
            continue
        ensemble, model_hours = fetch_confidence_data(spot, settings)
        for w in windows:
            wind = wind_confidence(ensemble, w, spot) if ensemble else None
            wave = wave_agreement(model_hours, w, spot)
            pct = confidence(wind, wave)
            if pct is not None and pct < settings.get("min_confidence", 0):
                print(f"Overgeslagen ({pct}% zeker): {describe(spot['name'], w)}", file=sys.stderr)
                continue
            blocks.append(describe(spot["name"], w) + confidence_line(pct, wind, wave))

    if args.weekly:
        if not blocks:
            sys.exit("Geen voorspelling opgehaald.")
        message = ("Surfweek 🌊\n🔥 lang goed · ✅ goed venster · ▫️ niks\n\n"
                   + "\n\n".join(blocks)
                   + "\n\n_Na 3–4 dagen is de voorspelling minder betrouwbaar._")
    elif blocks:
        message = "Surf alert! 🌊\n\n" + "\n\n".join(blocks)
    else:
        print("Geen goede surfvensters gevonden.")
        return
    print(message)
    if not args.dry_run:
        send_telegram(message)


if __name__ == "__main__":
    main()
