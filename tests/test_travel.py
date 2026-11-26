"""在路上: two fast samples put her on the road, the address names the city she is in, standing still
half an hour takes her off quietly, the App's 【到了】 ends it out loud."""
import unittest

import collector


def _config():
    return {"generation": 1, "device_id": "d", "source_crs": "wgs84", "enabled_since": 0}


def _fix(ts, lat=23.15, lon=113.33, acc=10.0):
    return {"latitude": lat, "longitude": lon, "horizontalAccuracy": acc, "timeStamp": ts}


class Speed(unittest.TestCase):
    def test_speed_and_heading_between_two_samples(self):
        before = {"latitude": 23.15, "longitude": 113.33, "accuracy_m": 10.0, "sampled_at": 1000.0}
        # ~5 km north in 60 s ≈ 300 km/h minus the two error radii
        speed, heading = collector.speed_between(before, {"latitude": 23.195, "longitude": 113.33,
                                                          "accuracy_m": 10.0, "sampled_at": 1060.0})
        self.assertGreater(speed, 280)
        self.assertLess(speed, 310)
        self.assertEqual(heading, "北")
        self.assertEqual(collector.bearing_text(23.15, 113.33, 23.16, 113.34), "东北")
        self.assertEqual(collector.bearing_text(23.15, 113.33, 23.15, 113.30), "西")

    def test_noise_inside_the_error_radii_is_standing_still(self):
        before = {"latitude": 23.15, "longitude": 113.33, "accuracy_m": 500.0, "sampled_at": 1000.0}
        speed, heading = collector.speed_between(before, {"latitude": 23.153, "longitude": 113.33,
                                                          "accuracy_m": 500.0, "sampled_at": 1060.0})
        self.assertEqual((speed, heading), (0.0, None))

    def test_same_sample_or_too_far_apart_says_nothing(self):
        before = {"latitude": 23.15, "longitude": 113.33, "accuracy_m": 10.0, "sampled_at": 1000.0}
        self.assertEqual(collector.speed_between(before, {**before}), (None, None))
        self.assertEqual(collector.speed_between(before, {**before, "latitude": 24.0, "sampled_at": 1000.0 + 601}),
                         (None, None))
        self.assertEqual(collector.speed_between(None, before), (None, None))


class Train(unittest.TestCase):
    """A ride: every minute a sample ~5 km further north; the geocoder names the city of the latitude."""

    def setUp(self):
        self.cities = []

        def geocode(lat, lon, crs):
            city = "广州市" if lat < 23.5 else "清远市" if lat < 24.0 else "韶关市"
            geocode.last_adcode = "440100" if city == "广州市" else "441800" if city == "清远市" else "440200"
            geocode.last_district = None
            geocode.last_city = city
            self.cities.append(city)
            return f"广东省{city}某区某路"
        self.geocode = geocode

    def ride(self, snap, t0, minutes, lat0=23.15, step=0.045, acc=10.0, switch=None, stop_after=None):
        """Advance ``minutes`` polls; the train stops (same spot) from ``stop_after`` on."""
        for i in range(1, minutes + 1):
            t = t0 + 60 * i
            lat = lat0 + step * (i if stop_after is None else min(i, stop_after))
            snap = collector.poll_once(_config(), snap, t, lambda: _fix(t - 1, lat=lat, acc=acc), self.geocode, None,
                                       travel_switch=switch)
        return snap, t

    def test_two_fast_samples_put_her_on_the_road_and_cities_follow(self):
        snap = collector.poll_once(_config(), None, 1000.0, lambda: _fix(999.0), self.geocode, None)
        self.assertFalse(snap["travel"]["active"])
        self.assertEqual(snap["address"]["city"], "广州市")
        snap, t = self.ride(snap, 1000.0, 1)
        self.assertFalse(snap["travel"]["active"])  # one fast reading is not a trip
        self.assertEqual(snap["travel"]["fast_streak"], 1)
        snap, t = self.ride(snap, t, 1, lat0=23.15 + 0.045)
        self.assertTrue(snap["travel"]["active"])
        self.assertEqual(snap["travel"]["mode"], "auto")
        self.assertEqual(snap["travel"]["since"], t)
        self.assertEqual(snap["travel"]["start_city"], "广州市")
        self.assertEqual(snap["travel"]["entered_city"], "广州市")
        self.assertIsNone(snap["travel"]["left_city"])
        self.assertGreater(snap["travel"]["speed_kmh"], 250)
        self.assertEqual(snap["travel"]["heading"], "北")
        # north past 23.5: a new city, the old one is the one she just left
        snap, t = self.ride(snap, t, 8, lat0=23.15 + 0.09)
        self.assertEqual(snap["travel"]["entered_city"], "清远市")
        self.assertEqual(snap["travel"]["left_city"], "广州市")
        self.assertTrue(snap["travel"]["active"])
        self.assertIsNone(snap["travel"]["ended_at"])

    def test_poor_accuracy_still_gets_an_address_on_the_road(self):
        snap = collector.poll_once(_config(), None, 1000.0, lambda: _fix(999.0), self.geocode, None)
        snap, t = self.ride(snap, 1000.0, 3)
        self.assertTrue(snap["travel"]["active"])
        asked = len(self.cities)
        # a 900 m fix: no address when walking, but on the road the city is still wanted
        snap, t = self.ride(snap, t, 1, lat0=23.15 + 0.135, acc=900.0)
        self.assertEqual(len(self.cities), asked + 1)
        self.assertEqual(snap["address"]["accuracy_m"] if "accuracy_m" in snap["address"] else snap["accuracy_m"], 900.0)
        off = collector.poll_once(_config(), None, 5000.0, lambda: _fix(4999.0, acc=900.0), self.geocode, None)
        self.assertNotIn("address", off)  # not travelling: the old rule stands

    def test_station_stop_does_not_end_it_but_half_an_hour_still_does_quietly(self):
        snap = collector.poll_once(_config(), None, 1000.0, lambda: _fix(999.0), self.geocode, None)
        snap, t = self.ride(snap, 1000.0, 3)
        self.assertTrue(snap["travel"]["active"])
        # three minutes at a station
        snap, t = self.ride(snap, t, 3, lat0=23.15 + 0.135, step=0.0)
        self.assertTrue(snap["travel"]["active"])
        self.assertIsNotNone(snap["travel"]["still_since"])
        # moving again clears the stillness
        snap, t = self.ride(snap, t, 2, lat0=23.15 + 0.135)
        self.assertIsNone(snap["travel"]["still_since"])
        # thirty minutes still: off the road, nothing to announce
        snap, t = self.ride(snap, t, 31, lat0=23.15 + 0.225, step=0.0)
        self.assertFalse(snap["travel"]["active"])
        self.assertEqual(snap["travel"]["ended_by"], "still")
        self.assertEqual(snap["travel"]["ended_at"], t)
        self.assertEqual(snap["travel"]["fast_streak"], 0)

    def test_the_switch_starts_and_the_button_ends_once(self):
        snap = collector.poll_once(_config(), None, 1000.0, lambda: _fix(999.0), self.geocode, None)
        on = {"state": "on", "at": 1010.0}
        snap = collector.poll_once(_config(), snap, 1060.0, lambda: _fix(1059.0), self.geocode, None, travel_switch=on)
        self.assertTrue(snap["travel"]["active"])
        self.assertEqual(snap["travel"]["mode"], "manual")
        self.assertEqual(snap["travel"]["switch_at"], 1010.0)
        snap, t = self.ride(snap, 1060.0, 2, switch=on)  # the same write is not consumed again
        self.assertEqual(snap["travel"]["since"], 1060.0)
        arrived = {"state": "arrived", "at": t + 5}
        snap = collector.poll_once(_config(), snap, t + 60, lambda: _fix(t + 59, lat=23.15 + 0.09), self.geocode, None,
                                   travel_switch=arrived)
        self.assertFalse(snap["travel"]["active"])
        self.assertEqual(snap["travel"]["ended_by"], "button")
        self.assertEqual(snap["travel"]["ended_at"], t + 60)
        self.assertEqual(snap["travel"]["end_city"], "广州市")
        self.assertTrue(snap["travel"]["end_text"].startswith("广东省广州市"))
        # the button also resets the fast streak so a fast reading right after does not re-enter alone
        self.assertEqual(snap["travel"]["fast_streak"], 0)
        # an old switch value never acts again
        snap = collector.poll_once(_config(), snap, t + 120, lambda: _fix(t + 119, lat=23.15 + 0.09), self.geocode, None,
                                   travel_switch=on)
        self.assertFalse(snap["travel"]["active"])

    def test_travel_state_survives_a_poll_without_a_new_sample(self):
        snap = collector.poll_once(_config(), None, 1000.0, lambda: _fix(999.0), self.geocode, None)
        snap, t = self.ride(snap, 1000.0, 3)
        again = collector.poll_once(_config(), snap, t + 60, lambda: (_ for _ in ()).throw(RuntimeError("x")),
                                    self.geocode, None)
        self.assertEqual(again["travel"], snap["travel"])

    def test_snapshot_never_logs_text(self):
        snap = collector.poll_once(_config(), None, 1000.0, lambda: _fix(999.0), self.geocode, None)
        self.assertNotIn("secret", str(snap["travel"]))


if __name__ == "__main__":
    unittest.main()
