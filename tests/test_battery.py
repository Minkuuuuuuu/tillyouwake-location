"""Battery level and charging status: written when available, kept across polls when missing."""
import unittest

import collector


def _config():
    return {"generation": 1, "device_id": "d", "source_crs": "wgs84", "enabled_since": 0}


def _device_content(ts, level=None, status=None):
    """Device content dict with location and optional battery fields."""
    content = {
        "location": {
            "latitude": 23.15,
            "longitude": 113.33,
            "horizontalAccuracy": 10.0,
            "timeStamp": ts,
        }
    }
    if level is not None:
        content["batteryLevel"] = level
    if status is not None:
        content["batteryStatus"] = status
    return content


def _location_only(ts):
    """Old shape: pure location dict (no battery)."""
    return {
        "latitude": 23.15,
        "longitude": 113.33,
        "horizontalAccuracy": 10.0,
        "timeStamp": ts,
    }


class BatteryNormalize(unittest.TestCase):
    def test_valid_battery_returns_dict(self):
        content = {"batteryLevel": 0.42, "batteryStatus": "NotCharging"}
        result = collector.normalize_battery(content, 1000.0)
        self.assertIsNotNone(result)
        self.assertEqual(result["level"], 0.42)
        self.assertEqual(result["charging"], False)
        self.assertEqual(result["at"], 1000.0)

    def test_charging_status_variations(self):
        self.assertTrue(collector.normalize_battery({"batteryLevel": 0.5, "batteryStatus": "Charging"}, 1000.0)["charging"])
        self.assertFalse(collector.normalize_battery({"batteryLevel": 0.5, "batteryStatus": "NotCharging"}, 1000.0)["charging"])
        self.assertFalse(collector.normalize_battery({"batteryLevel": 0.5, "batteryStatus": "Discharging"}, 1000.0)["charging"])
        self.assertIsNone(collector.normalize_battery({"batteryLevel": 0.5, "batteryStatus": "Unknown"}, 1000.0)["charging"])

    def test_missing_or_invalid_level_returns_none(self):
        self.assertIsNone(collector.normalize_battery({}, 1000.0))
        self.assertIsNone(collector.normalize_battery({"batteryLevel": None}, 1000.0))
        self.assertIsNone(collector.normalize_battery({"batteryLevel": "bad"}, 1000.0))
        self.assertIsNone(collector.normalize_battery({"batteryLevel": float("inf")}, 1000.0))
        self.assertIsNone(collector.normalize_battery({"batteryLevel": -0.1}, 1000.0))
        self.assertIsNone(collector.normalize_battery({"batteryLevel": 1.5}, 1000.0))

    def test_missing_status_gives_none_charging(self):
        result = collector.normalize_battery({"batteryLevel": 0.8}, 1000.0)
        self.assertEqual(result["level"], 0.8)
        self.assertIsNone(result["charging"])


class BatteryInSnapshot(unittest.TestCase):
    def test_battery_written_when_present(self):
        snap = collector.poll_once(_config(), None, 1000.0,
                                    lambda: _device_content(999.0, level=0.42, status="NotCharging"),
                                    None, None)
        self.assertIn("battery", snap)
        self.assertEqual(snap["battery"]["level"], 0.42)
        self.assertEqual(snap["battery"]["charging"], False)
        self.assertEqual(snap["battery"]["at"], 1000.0)

    def test_battery_kept_when_missing_in_next_poll(self):
        first = collector.poll_once(_config(), None, 1000.0,
                                     lambda: _device_content(999.0, level=0.42, status="NotCharging"),
                                     None, None)
        second = collector.poll_once(_config(), first, 1100.0,
                                      lambda: _location_only(1099.0),
                                      None, None)
        self.assertEqual(second["battery"]["level"], 0.42)
        self.assertEqual(second["battery"]["at"], 1000.0)  # Kept from first poll

    def test_battery_updated_when_new_value_arrives(self):
        first = collector.poll_once(_config(), None, 1000.0,
                                     lambda: _device_content(999.0, level=0.42, status="NotCharging"),
                                     None, None)
        second = collector.poll_once(_config(), first, 1100.0,
                                      lambda: _device_content(1099.0, level=0.38, status="Charging"),
                                      None, None)
        self.assertEqual(second["battery"]["level"], 0.38)
        self.assertEqual(second["battery"]["charging"], True)
        self.assertEqual(second["battery"]["at"], 1100.0)

    def test_no_battery_field_when_never_received(self):
        snap = collector.poll_once(_config(), None, 1000.0,
                                    lambda: _location_only(999.0),
                                    None, None)
        self.assertNotIn("battery", snap)

    def test_old_location_shape_still_works(self):
        """Pure location dict (old fetch shape) continues to work without battery."""
        snap = collector.poll_once(_config(), None, 1000.0,
                                    lambda: _location_only(999.0),
                                    None, None)
        self.assertEqual(snap["status"], "ok")
        self.assertEqual(snap["latitude"], 23.15)
        self.assertNotIn("battery", snap)


if __name__ == "__main__":
    unittest.main()
