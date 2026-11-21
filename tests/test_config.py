import unittest

from primal.config import normalize_server, parse_value


class ConfigTests(unittest.TestCase):
    def test_boolean_parsing(self):
        self.assertTrue(parse_value("compute_enabled", "true"))

    def test_integer_parsing(self):
        self.assertEqual(parse_value("maximum_cpu_usage", "20"), 20)
        with self.assertRaises(ValueError):
            parse_value("minimum_battery", "half")

    def test_server_normalization(self):
        self.assertEqual(normalize_server("https://example.com/"), "https://example.com")
        self.assertEqual(normalize_server("http://localhost:3000/"), "http://localhost:3000")
        with self.assertRaises(ValueError):
            normalize_server("http://example.com")


if __name__ == "__main__":
    unittest.main()
