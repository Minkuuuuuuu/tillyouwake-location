"""The sky beside the address (Open-Meteo since 2026-11-26): the live reading every 20 min, the days ahead
riding on it, both at once when the district changed; failures keep the last one; the district name comes
from the address."""
import unittest

import collector


def _config():
    return {"generation": 1, "device_id": "d", "source_crs": "wgs84", "enabled_since": 0}


def _fix(ts):
    return {"latitude": 23.15, "longitude": 113.33, "horizontalAccuracy": 10.0, "timeStamp": ts}


# A real answer from 2026-11-26 03:45 over 天河 (clear night, 12.3°, light north wind), trimmed.
OPEN_METEO_SAMPLE = {
    "latitude": 23.163443, "longitude": 113.37173, "utc_offset_seconds": 28800, "timezone": "Asia/Shanghai",
    "current": {"time": "2026-11-26T03:45", "interval": 900, "temperature_2m": 12.3, "relative_humidity_2m": 78,
                "weather_code": 0, "wind_speed_10m": 2.91, "wind_direction_10m": 356},
    "daily": {"time": ["2026-11-26", "2026-11-27", "2026-11-28", "2026-11-29"],
              "weather_code": [3, 2, 61, 0],
              "temperature_2m_max": [19.8, 24.4, 25.0, 23.9], "temperature_2m_min": [12.1, 14.1, 15.0, 11.9],
              "wind_speed_10m_max": [2.9, 3.86, 9.0, 2.89], "wind_direction_10m_dominant": [0, 357, 13, 54]},
}


class OpenMeteoParsing(unittest.TestCase):
    def test_sample_becomes_the_snapshot_shape_in_amap_words(self):
        sky = collector.parse_open_meteo(OPEN_METEO_SAMPLE)
        self.assertEqual(sky["text"], "晴")
        self.assertEqual(sky["temp"], 12.3)
        self.assertEqual(sky["humidity"], 78.0)
        self.assertEqual(sky["wind_dir"], "北")
        self.assertEqual(sky["wind_power"], "≤3")
        self.assertNotIn("city", sky)  # the address names the district, not the weather service
        # 2026-11-26 03:45 Asia/Shanghai = 2026-11-25 19:45 UTC
        self.assertEqual(sky["reported_at"], 1795635900.0)
        self.assertEqual(len(sky["days"]), 4)
        self.assertEqual(sky["days"][0], {"date": "2026-11-26", "day": "阴", "night": "阴", "high": 19.8, "low": 12.1,
                                          "wind": "北", "power": "≤3"})
        self.assertEqual(sky["days"][2]["day"], "小雨")
        self.assertEqual(sky["days"][2]["power"], "5")

    def test_no_live_code_is_an_empty_answer(self):
        with self.assertRaises(ValueError):
            collector.parse_open_meteo({"current": {"temperature_2m": 12.0}, "daily": {}})
        with self.assertRaises(ValueError):
            collector.parse_open_meteo({})

    def test_wet_words_survive_for_the_rain_start_check(self):
        # the self-wake looks for 雨/雪/雹 in the text; every precipitating WMO code must carry one
        wet_codes = [51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 71, 73, 75, 77, 80, 81, 82, 85, 86, 95, 96, 99]
        for code in wet_codes:
            self.assertTrue(any(mark in collector.wmo_text(code) for mark in ("雨", "雪", "雹")), code)
        for code in (0, 1, 2, 3, 45, 48):
            self.assertFalse(any(mark in collector.wmo_text(code) for mark in ("雨", "雪", "雹")), code)
        self.assertEqual(collector.wmo_text("x"), "")
        self.assertEqual(collector.wmo_text(None), "")

    def test_wind_helpers(self):
        self.assertEqual([collector.wind_dir_text(d) for d in (0, 45, 90, 135, 180, 225, 270, 315, 359)],
                         ["北", "东北", "东", "东南", "南", "西南", "西", "西北", "北"])
        self.assertEqual(collector.wind_dir_text(None), "")
        self.assertEqual(collector.wind_power_text(0.0), "≤3")
        self.assertEqual(collector.wind_power_text(5.4), "≤3")
        self.assertEqual(collector.wind_power_text(5.5), "4")
        self.assertEqual(collector.wind_power_text(20.8), "9")
        self.assertEqual(collector.wind_power_text("bad"), "")

    def test_district_fallback_from_the_address_text(self):
        self.assertEqual(collector._district_of("广东省广州市天河区五山街道广东省农业科学院"), "天河区")
        self.assertEqual(collector._district_of("北京市朝阳区建国路"), "朝阳区")
        self.assertEqual(collector._district_of("广东省东莞市松山湖"), "")
        self.assertEqual(collector._district_of(None), "")


class WeatherStep(unittest.TestCase):
    def setUp(self):
        self.asked = []
        self.adcode = "440106"
        self.district = "天河区"

        def geocode(lat, lon, crs):
            geocode.last_adcode = self.adcode
            geocode.last_district = self.district
            return "广东省广州市天河区五山街道"
        self.geocode = geocode

        def weather(lat, lon, forecast=True):
            self.asked.append((round(lat, 2), round(lon, 2), forecast))
            sky = {"text": "晴", "temp": 28.0, "humidity": 61.0, "wind_dir": "北",
                   "wind_power": "≤3", "reported_at": 1000.0}
            if forecast:
                sky["days"] = [{"date": "2026-11-26", "day": "晴", "night": "晴", "high": 27.0, "low": 19.0,
                                "wind": "北", "power": "≤3"}]
            return sky
        self.weather = weather

    def test_first_poll_asks_the_point_and_names_the_district_from_the_address(self):
        snap = collector.poll_once(_config(), None, 1000.0, lambda: _fix(999.0), self.geocode, self.weather)
        self.assertEqual(snap["address"]["adcode"], "440106")
        self.assertEqual(snap["address"]["district"], "天河区")
        self.assertEqual(snap["weather"]["text"], "晴")
        self.assertEqual(snap["weather"]["city"], "天河区")
        self.assertEqual(snap["weather"]["adcode"], "440106")
        self.assertEqual(snap["weather"]["fetched_at"], 1000.0)
        self.assertEqual(snap["weather"]["forecast_at"], 1000.0)
        self.assertEqual(len(snap["weather"]["days"]), 1)
        self.assertEqual(self.asked, [(23.15, 113.33, True)])

    def test_district_name_falls_back_to_the_address_text_for_old_addresses(self):
        self.district = None  # a geocoder that leaves no name, like the stored addresses from before
        snap = collector.poll_once(_config(), None, 1000.0, lambda: _fix(999.0), self.geocode, self.weather)
        self.assertEqual(snap["weather"]["city"], "天河区")

    def test_within_twenty_minutes_nothing_is_asked_then_live_only(self):
        first = collector.poll_once(_config(), None, 1000.0, lambda: _fix(999.0), self.geocode, self.weather)
        again = collector.poll_once(_config(), first, 1000.0 + 1199, lambda: _fix(2198.0), self.geocode, self.weather)
        self.assertEqual(again["weather"], first["weather"])
        self.assertEqual(self.asked, [(23.15, 113.33, True)])
        later = collector.poll_once(_config(), again, 1000.0 + 1201, lambda: _fix(2200.0), self.geocode, self.weather)
        self.assertEqual(later["weather"]["fetched_at"], 2201.0)
        self.assertEqual(self.asked, [(23.15, 113.33, True), (23.15, 113.33, False)])
        # an answer without days keeps the days from the first forecast, not dropped
        self.assertEqual(later["weather"]["days"], first["weather"]["days"])
        self.assertEqual(later["weather"]["forecast_at"], 1000.0)

    def test_days_that_come_along_anyway_are_taken(self):
        # Open-Meteo sends the days with every answer; the step keeps them and moves forecast_at
        def always(lat, lon, forecast=True):
            return self.weather(lat, lon, forecast=True)
        first = collector.poll_once(_config(), None, 1000.0, lambda: _fix(999.0), self.geocode, always)
        later = collector.poll_once(_config(), first, 1000.0 + 1201, lambda: _fix(2200.0), self.geocode, always)
        self.assertEqual(later["weather"]["forecast_at"], 2201.0)
        self.assertEqual(len(later["weather"]["days"]), 1)

    def test_forecast_rides_on_the_first_live_ask_after_three_hours(self):
        snap = collector.poll_once(_config(), None, 1000.0, lambda: _fix(999.0), self.geocode, self.weather)
        t = 1000.0
        while t < 1000.0 + collector.FORECAST_SECONDS:
            t += collector.WEATHER_SECONDS
            snap = collector.poll_once(_config(), snap, t, lambda: _fix(t - 1), self.geocode, self.weather)
        forecasts = [a for a in self.asked if a[2]]
        self.assertEqual(len(forecasts), 2)  # the first poll and one three hours later
        self.assertEqual(snap["weather"]["forecast_at"], t)
        self.assertLessEqual(len(self.asked), 11)  # 1 + 9 live asks; the forecast never adds a round of its own

    def test_new_district_asks_both_at_once_and_renames(self):
        first = collector.poll_once(_config(), None, 1000.0, lambda: _fix(999.0), self.geocode, self.weather)
        self.adcode, self.district = "440104", "越秀区"
        far = {**_fix(1060.0), "latitude": 23.05}  # another district, well past the address-reuse radius
        moved = collector.poll_once(_config(), first, 1000.0 + collector.ADDRESS_MIN_GAP + 1,
                                    lambda: far, self.geocode, self.weather)
        self.assertEqual(moved["weather"]["adcode"], "440104")
        self.assertEqual(moved["weather"]["city"], "越秀区")
        self.assertEqual(self.asked[-1], (23.05, 113.33, True))
        self.assertEqual(moved["weather"]["forecast_at"], 1000.0 + collector.ADDRESS_MIN_GAP + 1)

    def test_failure_keeps_last_sky_and_waits(self):
        first = collector.poll_once(_config(), None, 1000.0, lambda: _fix(999.0), self.geocode, self.weather)

        def broken(lat, lon, forecast=True):
            raise RuntimeError("open_meteo_error")
        failed = collector.poll_once(_config(), first, 3000.0, lambda: _fix(2999.0), self.geocode, broken)
        self.assertEqual(failed["weather"]["text"], "晴")
        self.assertEqual(failed["weather"]["city"], "天河区")
        self.assertEqual(failed["weather"]["days"], first["weather"]["days"])
        self.assertEqual(failed["weather"]["error"], "weather_failed:RuntimeError")
        self.assertEqual(failed["weather"]["retry_at"], 3000.0 + collector.WEATHER_RETRY)
        calm = collector.poll_once(_config(), failed, 3100.0, lambda: _fix(3099.0), self.geocode, broken)
        self.assertEqual(calm["weather"]["retry_at"], 3000.0 + collector.WEATHER_RETRY)  # not asked again yet

    def test_no_district_means_no_ask_and_no_weather_without_provider(self):
        snap = collector.poll_once(_config(), None, 1000.0, lambda: _fix(999.0), None, self.weather)
        self.assertNotIn("weather", snap)
        self.assertEqual(self.asked, [])
        plain = collector.poll_once(_config(), None, 1000.0, lambda: _fix(999.0), self.geocode, None)
        self.assertNotIn("weather", plain)

    def test_snapshot_never_logs_text(self):
        # kinds only in errors, as the rest of the collector promises
        def broken(lat, lon, forecast=True):
            raise RuntimeError("secret 12345")
        snap = collector.poll_once(_config(), None, 1000.0, lambda: _fix(999.0), self.geocode, broken)
        self.assertNotIn("12345", str(snap["weather"]))

    def test_day_budget(self):
        # the contract behind the numbers: a quiet day in one district stays far below Open-Meteo's 10,000/day,
        # and well under what AMap's 5,000/month ever allowed (≈160/day)
        per_day = 86400 / collector.WEATHER_SECONDS
        self.assertLess(per_day, 100)


if __name__ == "__main__":
    unittest.main()
