import hashlib
import unittest

from primal.workload import MOD, hash_sum, normalize_params


def naive_hash_sum(seed, iterations):
    total = 0
    for index in range(iterations):
        digest = hashlib.sha256(f"{seed}:{index}".encode("utf-8")).digest()
        total = (total + int.from_bytes(digest[:8], "big")) % MOD
    return total


class WorkloadTests(unittest.TestCase):
    def test_known_vector_matches_server_implementation(self):
        result = hash_sum({"seed": "primal-test", "iterations": 100, "chunks": 4})
        self.assertEqual(result["value"], 89178844)
        self.assertEqual(
            result["chunk_values"],
            [805002654, 820370785, 130342796, 333462623],
        )

    def test_chunking_is_independent_and_consistent(self):
        for chunks in (1, 2, 3, 7, 16):
            result = hash_sum({"seed": "chunk-check", "iterations": 123, "chunks": chunks})
            self.assertEqual(result["value"], naive_hash_sum("chunk-check", 123))
            self.assertEqual(sum(result["chunk_values"]) % MOD, result["value"])

    def test_single_chunk_vector(self):
        self.assertEqual(
            hash_sum({"seed": "primal", "iterations": 10, "chunks": 1})["value"],
            430038476,
        )

    def test_invalid_parameters_are_rejected(self):
        with self.assertRaises(ValueError):
            normalize_params({"seed": "bad seed", "iterations": 10, "chunks": 1})
        with self.assertRaises(ValueError):
            normalize_params({"seed": "ok", "iterations": 0, "chunks": 1})
        with self.assertRaises(ValueError):
            normalize_params({"seed": "ok", "iterations": 10, "chunks": 65})
        with self.assertRaises(ValueError):
            normalize_params({"seed": "ok", "iterations": 500001, "chunks": 1})


if __name__ == "__main__":
    unittest.main()
