import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import surf_alert as sa

SPOT = {
    "name": "Test", "wave_height": [0.8, 2.5], "swell_period_min": 6,
    "swell_direction": [250, 20], "wind_max_kn": 15,
    "offshore_wind": [45, 160], "glassy_kn": 6,
}
SETTINGS = {"daylight_from": 7, "daylight_to": 20, "min_window_hours": 2}


def make_hours(rows):
    times = [f"2026-10-30T{h:02d}:00" for h, *_ in rows]
    marine = {
        "time": times,
        "wave_height": [r[1] for r in rows],
        "swell_wave_height": [r[1] for r in rows],
        "swell_wave_period": [r[2] for r in rows],
        "swell_wave_direction": [r[3] for r in rows],
    }
    weather = {
        "time": times,
        "wind_speed_10m": [r[4] for r in rows],
        "wind_direction_10m": [r[5] for r in rows],
        "wind_gusts_10m": [r[4] + 5 for r in rows],
    }
    return sa.merge_hourly(marine, weather)


def test_direction_range_wraps_north():
    assert sa.in_range(350, [250, 20])
    assert sa.in_range(10, [250, 20])
    assert not sa.in_range(180, [250, 20])


def test_onshore_wind_is_bad_unless_glassy():
    good = {"wave_height": 1.2, "swell_period": 8, "swell_dir": 300, "wind_kn": 10, "wind_dir": 90}
    assert sa.is_good(good, SPOT)
    assert not sa.is_good(good | {"wind_dir": 270}, SPOT)
    assert sa.is_good(good | {"wind_dir": 270, "wind_kn": 4}, SPOT)


def test_windows_only_in_daylight_and_long_enough():
    hours = make_hours([
        (5, 1.2, 8, 300, 10, 90),   # te vroeg
        (6, 1.2, 8, 300, 10, 90),   # te vroeg
        (7, 1.2, 8, 300, 10, 90),
        (8, 1.3, 8, 300, 10, 90),
        (9, 1.3, 8, 300, 20, 90),   # te veel wind
        (10, 1.3, 8, 300, 10, 90),  # los uurtje, te kort
        (11, 0.3, 8, 300, 10, 90),  # te klein
    ])
    windows = sa.good_windows(hours, SPOT, SETTINGS)
    assert [[h["time"].hour for h in w] for w in windows] == [[7, 8]]
    text = sa.describe("Test", windows[0])
    assert "vr 30 okt 07:00–09:00" in text and "uit WNW" in text and "uit O" in text


def test_config_parses():
    import tomllib
    config = tomllib.loads((Path(sa.__file__).with_name("spots.toml")).read_text())
    assert len(config["spot"]) == 3


def test_week_summary_marks_good_days():
    hours = make_hours([(h, 1.2, 8, 300, 10, 90) for h in range(7, 12)])
    text = sa.week_summary(SPOT, hours, SETTINGS)
    assert text.splitlines()[0] == "*Test*"
    assert "🔥 vr 30 okt: 1.2 m, 8 s, wind 10 kn O (5 u goed)" in text


def test_fill_gaps_uses_fallback_model_per_hour():
    times = ["2026-11-03T12:00", "2026-11-04T12:00"]
    hourly = {"time": times, **{v: [1.0, None] for v in sa.MARINE_VARS}}
    fallback = {"time": times[::-1], **{v: [2.0, 9.0] for v in sa.MARINE_VARS}}
    sa.fill_gaps(hourly, fallback)
    assert hourly["wave_height"] == [1.0, 2.0]


def test_missing_swell_falls_back_to_total_wave():
    times = ["2026-11-02T12:00"]
    marine = {"time": times, "wave_height": [1.8], "wave_period": [7.0], "wave_direction": [320.0],
              "swell_wave_height": [None], "swell_wave_period": [None], "swell_wave_direction": [None]}
    weather = {"time": times, "wind_speed_10m": [8], "wind_direction_10m": [20], "wind_gusts_10m": [12]}
    h = sa.merge_hourly(marine, weather)[0]
    assert (h["swell_period"], h["swell_dir"]) == (7.0, 320.0)


def test_wind_confidence_counts_members_per_hour():
    window = make_hours([(7, 1.2, 8, 300, 10, 90), (8, 1.2, 8, 300, 10, 90)])
    ensemble = {
        "time": ["2026-10-30T07:00", "2026-10-30T08:00"],
        "wind_speed_10m": [10, 10], "wind_direction_10m": [90, 90],                  # goed
        "wind_speed_10m_member01": [10, 20], "wind_direction_10m_member01": [90, 90],  # 1 van 2
        "wind_speed_10m_member02": [10, 10], "wind_direction_10m_member02": [270, 270],  # aanlandig
        "wind_speed_10m_member03": [4, None], "wind_direction_10m_member03": [270, None],  # glassy
    }
    assert sa.wind_confidence(ensemble, window, SPOT) == 4 / 7


def test_wave_agreement_and_confidence():
    window = make_hours([(7, 1.2, 8, 300, 10, 90), (8, 1.2, 8, 300, 10, 90)])
    flat = make_hours([(7, 0.2, 8, 300, 10, 90), (8, 0.2, 8, 300, 10, 90)])
    wave = sa.wave_agreement([window, window, flat], window, SPOT)
    assert wave == (2, 3)
    assert sa.confidence(0.9, wave) == 67
    assert sa.confidence(None, (0, 0)) is None
    assert "zekerheid 67% (wind 90%, golven 2/3 modellen)" in sa.confidence_line(67, 0.9, wave)
