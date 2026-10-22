"""The sky beside the address: asked only when stale or the district changed; failures keep the last one."""
import unittest

import collector


def _config():
    return {"generation": 1, "device_id": "d", "source_crs": "wgs84", "enabled_since": 0}


def _fix(ts):
    return {"latitude": 23.15, "longitude": 113.33, "horizontalAccuracy": 10.0, "timeStamp": ts}


class WeatherStep(unittest.TestCase):
    def setUp(self):
        self.asked = []

        def geocode(lat, lon, crs):
            geocode.last_adcode = "440106"
            return "广州市天河区"
        self.geocode = geocode

        def weather(adcode):
            self.asked.append(adcode)
            return {"city": "天河区", "text": "晴", "temp": 28.0, "humidity": 61.0, "wind_dir": "北",
                    "wind_power": "4", "reported_at": 1000.0, "days": []}
        self.weather = weather

    def test_first_poll_asks_once_and_keeps_the_district(self):
        snap = collector.poll_once(_config(), None, 1000.0, lambda: _fix(999.0), self.geocode, self.weather)
        self.assertEqual(snap["address"]["adcode"], "440106")
        self.assertEqual(snap["weather"]["text"], "晴")
        self.assertEqual(snap["weather"]["adcode"], "440106")
        self.assertEqual(snap["weather"]["fetched_at"], 1000.0)
        self.assertEqual(self.asked, ["440106"])

    def test_within_ten_minutes_nothing_is_asked(self):
        first = collector.poll_once(_config(), None, 1000.0, lambda: _fix(999.0), self.geocode, self.weather)
        again = collector.poll_once(_config(), first, 1000.0 + 300, lambda: _fix(1299.0), self.geocode, self.weather)
        self.assertEqual(again["weather"], first["weather"])
        self.assertEqual(self.asked, ["440106"])
        later = collector.poll_once(_config(), again, 1000.0 + 601, lambda: _fix(1600.0), self.geocode, self.weather)
        self.assertEqual(later["weather"]["fetched_at"], 1601.0)
        self.assertEqual(self.asked, ["440106", "440106"])

    def test_failure_keeps_last_sky_and_waits(self):
        first = collector.poll_once(_config(), None, 1000.0, lambda: _fix(999.0), self.geocode, self.weather)

        def broken(adcode):
            raise RuntimeError("amap_10003")
        failed = collector.poll_once(_config(), first, 2000.0, lambda: _fix(1999.0), self.geocode, broken)
        self.assertEqual(failed["weather"]["text"], "晴")
        self.assertEqual(failed["weather"]["error"], "weather_failed:RuntimeError")
        self.assertEqual(failed["weather"]["retry_at"], 2000.0 + collector.WEATHER_RETRY)
        calm = collector.poll_once(_config(), failed, 2100.0, lambda: _fix(2099.0), self.geocode, broken)
        self.assertEqual(calm["weather"]["retry_at"], 2000.0 + collector.WEATHER_RETRY)  # not asked again yet

    def test_no_district_means_no_ask_and_no_weather_without_provider(self):
        snap = collector.poll_once(_config(), None, 1000.0, lambda: _fix(999.0), None, self.weather)
        self.assertNotIn("weather", snap)
        self.assertEqual(self.asked, [])
        plain = collector.poll_once(_config(), None, 1000.0, lambda: _fix(999.0), self.geocode, None)
        self.assertNotIn("weather", plain)

    def test_snapshot_never_logs_text(self):
        # kinds only in errors, as the rest of the collector promises
        def broken(adcode):
            raise RuntimeError("secret 12345")
        snap = collector.poll_once(_config(), None, 1000.0, lambda: _fix(999.0), self.geocode, broken)
        self.assertNotIn("12345", str(snap["weather"]))


if __name__ == "__main__":
    unittest.main()
