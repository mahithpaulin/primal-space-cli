import unittest

from primal.workload import WORKLOADS, normalize_http_params, normalize_python_params, normalize_shell_params


class WorkloadTests(unittest.TestCase):
    def test_supported_workloads(self):
        self.assertEqual(WORKLOADS, ("python", "shell", "http"))

    def test_python_params_normalization(self):
        params = normalize_python_params({"code": "def main(args):\n    return args\n", "items": [1, 2]})
        self.assertEqual(params["args"], [1, 2])
        self.assertEqual(params["items"], [1, 2])
        with self.assertRaises(ValueError):
            normalize_python_params({"code": ""})
        with self.assertRaises(ValueError):
            normalize_python_params({"code": "x = 1", "items": "nope"})

    def test_python_explicit_args_override_items(self):
        params = normalize_python_params({"code": "def main(args):\n    return args\n", "items": [1], "args": {"seed": 4}})
        self.assertEqual(params["args"], {"seed": 4})

    def test_shell_params_normalization(self):
        params = normalize_shell_params({"command": "echo hi"})
        self.assertEqual(params["command"], "echo hi")
        with self.assertRaises(ValueError):
            normalize_shell_params({"command": "   "})

    def test_http_params_normalization(self):
        params = normalize_http_params({"url": "https://example.com"})
        self.assertEqual(params["method"], "GET")
        self.assertEqual(params["timeout_s"], 20)
        params = normalize_http_params({"url": "https://example.com", "method": "post", "timeout_s": 90})
        self.assertEqual(params["method"], "POST")
        self.assertEqual(params["timeout_s"], 30)
        with self.assertRaises(ValueError):
            normalize_http_params({"url": "ftp://example.com"})


if __name__ == "__main__":
    unittest.main()
