"""Unit tests for the pure-stdlib core. Run: python3 -m unittest discover -s memory-bus/tests -v"""
import hashlib
import math
import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from memory_bus import textops as T  # noqa: E402


def toy_embed(texts):
    """Deterministic bag-of-words hashing embedder (64-d), L2-normalized."""
    out = []
    for t in texts:
        v = [0.0] * 64
        for w in t.lower().split():
            h = int(hashlib.md5(w.encode()).hexdigest(), 16)
            v[h % 64] += 1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        out.append([x / n for x in v])
    return out


class Tokens(unittest.TestCase):
    def test_estimate(self):
        self.assertEqual(T.est_tokens(""), 0)
        self.assertEqual(T.est_tokens("abcd"), 1)
        self.assertEqual(T.est_tokens("a" * 4001), 1001)

    def test_truncate(self):
        s = "word " * 100
        out = T.truncate_to_tokens(s, 10)
        self.assertLessEqual(len(out), 10 * 4 + 2)
        self.assertTrue(out.endswith("…"))


class Hashing(unittest.TestCase):
    def test_idempotent_whitespace(self):
        self.assertEqual(T.content_hash("a  b\n c"), T.content_hash("a b c"))
        self.assertNotEqual(T.content_hash("a b c"), T.content_hash("a b d"))


class Chunking(unittest.TestCase):
    def test_small_verbatim(self):
        self.assertEqual(T.chunk_text("hello world"), ["hello world"])
        self.assertEqual(T.chunk_text("   "), [])

    def test_paragraph_packing(self):
        paras = ["para %d " % i + "x" * 300 for i in range(10)]  # ~78 tokens each
        chunks = T.chunk_text("\n\n".join(paras), chunk_tokens=200)
        self.assertGreater(len(chunks), 3)
        for c in chunks:
            self.assertLessEqual(T.est_tokens(c), 200)
        self.assertEqual("\n\n".join(chunks), "\n\n".join(paras))  # nothing lost

    def test_oversized_paragraph_kept_whole_below_hard_max(self):
        big = "Sentence number one is here. " * 60  # ~430 tokens, > 400
        chunks = T.chunk_text(big, chunk_tokens=400, hard_max_tokens=1500)
        self.assertEqual(len(chunks), 1)
        self.assertGreater(T.est_tokens(chunks[0]), 400)

    def test_hard_max_split(self):
        huge = "This is a sentence. " * 800  # ~4000 tokens
        chunks = T.chunk_text(huge, chunk_tokens=400, hard_max_tokens=1500)
        self.assertGreater(len(chunks), 2)
        for c in chunks:
            self.assertLessEqual(T.est_tokens(c), 1500)


class Mmr(unittest.TestCase):
    def test_diversity(self):
        q = T.unit([1.0, 0.0, 0.0])
        cands = [T.unit([1.0, 0.0, 0.0]), T.unit([0.99, 0.01, 0.0]), T.unit([0.6, 0.8, 0.0])]
        order = T.mmr(q, cands, k=2, lambda_=0.3)
        self.assertEqual(order[0], 0)
        self.assertEqual(order[1], 2)  # near-duplicate 1 is skipped for the diverse 2

    def test_pure_relevance(self):
        q = T.unit([1.0, 0.0])
        cands = [T.unit([0.0, 1.0]), T.unit([1.0, 0.0])]
        self.assertEqual(T.mmr(q, cands, k=1, lambda_=1.0), [1])
        self.assertEqual(T.mmr(q, [], k=3), [])


class MmrLarge(unittest.TestCase):
    """2026-09-16 regression: MMR must scale to compress-sized pools and both paths must agree."""

    def _rand_vecs(self, n, dim=32, seed=7):
        import random
        rnd = random.Random(seed)
        return [T.unit([rnd.gauss(0, 1) for _ in range(dim)]) for _ in range(n)]

    def test_pure_and_numpy_agree(self):
        if T._np is None:
            self.skipTest("numpy not importable")
        cands = self._rand_vecs(150)
        q = cands[0]
        a = T.mmr(q, cands, k=40, lambda_=0.6, use_numpy=False)
        b = T.mmr(q, cands, k=40, lambda_=0.6, use_numpy=True)
        self.assertEqual(a, b)
        self.assertEqual(len(set(a)), 40)

    def test_pure_path_is_incremental_fast(self):
        import time
        cands = self._rand_vecs(600)
        t0 = time.time()
        order = T.mmr(cands[0], cands, k=600, lambda_=0.8, use_numpy=False)
        self.assertLess(time.time() - t0, 10.0)
        self.assertEqual(sorted(order), list(range(600)))


class Packing(unittest.TestCase):
    def test_budget(self):
        hits = [{"source": "s%d" % i, "text": "t" * 400} for i in range(10)]  # ~101 tokens each
        kept, packed, used = T.pack_hits(hits, max_tokens=350)
        self.assertLessEqual(used, 350)
        self.assertEqual(len(kept), 4)  # 3 full + 1 truncated tail (>=40 tokens left)
        self.assertTrue(kept[-1].get("truncated"))
        self.assertLessEqual(T.est_tokens(packed), 350)

    def test_stop_when_tail_too_small(self):
        hits = [{"source": "a", "text": "t" * 1180}, {"source": "b", "text": "t" * 400}]
        kept, _, used = T.pack_hits(hits, max_tokens=300)
        self.assertEqual(len(kept), 1)
        self.assertLessEqual(used, 300)


class Compress(unittest.TestCase):
    def test_noop_under_target(self):
        out = T.extractive_compress("short text", toy_embed, 100)
        self.assertFalse(out["changed"])

    def test_tool_output_keeps_head_tail(self):
        lines = ["$ pytest -q"] + ["test_%d PASSED noise noise noise noise noise" % i for i in range(200)] + \
                ["FAILED tests/test_x.py::test_y - AssertionError", "1 failed, 199 passed in 3.2s", "exit code 1"]
        text = "\n".join(lines)
        out = T.extractive_compress(text, toy_embed, 200, mode="tool_output", keep_head=1, keep_tail=3)
        self.assertTrue(out["changed"])
        self.assertLessEqual(out["tokens_out"], 200 + 8)
        self.assertIn("$ pytest -q", out["text"])
        self.assertIn("exit code 1", out["text"])
        self.assertIn("FAILED tests/test_x.py", out["text"])
        self.assertIn("[…]", out["text"])

    def test_3000_lines_completes_quickly(self):
        """The §10 acceptance probe: seq 1 3000 | sed 's/^/line /' | compress --target 300.
        Hung the service for >300 s before the incremental/pooled MMR."""
        import time
        text = "\n".join("line %d" % i for i in range(1, 3001))
        for use_numpy in (True, False):
            t0 = time.time()
            with unittest.mock.patch.object(T, "_np", T._np if use_numpy else None):
                out = T.extractive_compress(text, toy_embed, 300, mode="tool_output")
            self.assertLess(time.time() - t0, 15.0, "use_numpy=%s" % use_numpy)
            self.assertTrue(out["changed"])
            self.assertEqual(out["total"], 3000)
            self.assertLessEqual(out["tokens_out"], 300 + 8)
            self.assertTrue(out["text"].startswith("line 1\nline 2\nline 3\n"))
            self.assertTrue(out["text"].endswith("line 2998\nline 2999\nline 3000"))
            self.assertIn("[…]", out["text"])

    def test_unit_cap_coalesces(self):
        text = "\n".join("l%d" % i for i in range(T.MAX_COMPRESS_UNITS * 2 + 5))
        out = T.extractive_compress(text, toy_embed, 100, mode="tool_output")
        self.assertLessEqual(out["total"], T.MAX_COMPRESS_UNITS)
        self.assertTrue(out["text"].startswith("l0\nl1\nl2"))

    def test_query_steers(self):
        text = ("The deployment uses port 8791 on Kali. " * 3 + "Cats like tuna and naps. " * 3 +
                "The qdrant collection engrams uses int8 quantization. " * 3) * 4
        out = T.extractive_compress(text, toy_embed, 60, query_vec=toy_embed(["qdrant int8 quantization"])[0], mode="prose")
        self.assertIn("quantization", out["text"])
        self.assertLessEqual(out["tokens_out"], 70)


class Scrub(unittest.TestCase):
    def test_secret_lines_withheld(self):
        s = T.scrub("normal line\nAPI_KEY=abc123\nxoxb-1234-5678\nstill fine")
        self.assertIn("normal line", s)
        self.assertIn("still fine", s)
        self.assertNotIn("abc123", s)
        self.assertNotIn("xoxb", s)


if __name__ == "__main__":
    unittest.main()
