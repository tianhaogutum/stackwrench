import unittest

from stackwrench.context_budget.ignore import InputError, decide, parse_rules


class IgnoreConformanceTests(unittest.TestCase):
    def decision(self, text, path):
        return decide(path, parse_rules(text, "rules.ignore"))

    def test_golden_rooted_and_unrooted(self):
        for pattern in ("cache", "/cache", "cache/", "/cache/", " /cache/  "):
            for path, included in (("cache", False), ("cache/a/b", False),
                                   ("nested/cache", True), ("cache.txt", True)):
                with self.subTest(pattern=pattern, path=path):
                    self.assertEqual(self.decision(pattern, path)[0], included)

    def test_golden_star_and_question(self):
        cases = [
            ("*.log", "root.log", False), ("*.log", "deep/a.log", True),
            ("*/junk?", "a/junk1", False), ("*/junk?", "a/b/junk1", True),
            ("*/junk?", "a/junk12", True), ("*", ".hidden", False),
            ("*", "a/b/c", False), ("a?c", "aéc", False),
            ("a?c", "ac", True), ("a?c", "a/c", True),
        ]
        for pattern, path, expected in cases:
            with self.subTest(pattern=pattern, path=path):
                self.assertEqual(self.decision(pattern, path)[0], expected)

    def test_golden_double_star(self):
        cases = [
            ("**/*.tmp", "a.tmp", False),
            ("**/*.tmp", "a/b/c.tmp", False),
            ("**/*.tmp", "a/b/c.txt", True),
            ("x/**/a", "x/a", False),
            ("x/**/a", "x/b/c/a", False),
            ("x/**/a", "y/x/a", True),
            ("x/**", "x", True),
            ("x/**", "x/file", False),
            ("**/cache", "cache/a", False),
            ("**/cache", "one/two/cache/a", False),
            ("**/**/a", "a", False),
            ("**/**/a", "b/c/a", False),
            ("**", "anything/deep", False),
        ]
        for pattern, path, expected in cases:
            with self.subTest(pattern=pattern, path=path):
                self.assertEqual(self.decision(pattern, path)[0], expected)

    def test_golden_character_classes(self):
        for pattern, path, expected in [
            ("v[1-3].dat", "v2.dat", False), ("v[1-3].dat", "v4.dat", True),
            ("v[^1-3].dat", "v4.dat", False), ("v[^1-3].dat", "v2.dat", True),
            ("[ab]", "a", False), ("[!a]", "!", False),
            (r"[\-]", "-", False), (r"[\]]", "]", False),
            ("[α-γ]", "β", False), ("x[^a]y", "x/y", True),
        ]:
            with self.subTest(pattern=pattern, path=path):
                self.assertEqual(self.decision(pattern, path)[0], expected)

    def test_golden_comments_bom_whitespace_clean_and_dot(self):
        text = "\ufeff# comment\r\n\n  \n.\n./tmp/../keep\n  #literal  \nfoo #bar\n"
        rules = parse_rules(text, "test")
        self.assertEqual([rule.pattern for rule in rules], ["keep", "#literal", "foo #bar"])
        self.assertEqual([rule.line for rule in rules], [5, 6, 7])
        for path in ("keep", "#literal", "foo #bar"):
            self.assertFalse(decide(path, rules)[0])
        self.assertTrue(decide("else", rules)[0])

    def test_golden_escape_literals(self):
        for pattern, path in [(r"\#name", "#name"), (r"\!name", "!name"),
                              (r"a\*b", "a*b"), (r"a\?b", "a?b"),
                              (r"a\[b\]", "a[b]"), (r"a\\b", "a\\b")]:
            with self.subTest(pattern=pattern):
                self.assertFalse(self.decision(pattern, path)[0])

    def test_negation_reincludes_under_ignored_parent(self):
        text = "out\n!out/keep.txt\n"
        included, last = self.decision(text, "out/keep.txt")
        self.assertTrue(included)
        self.assertEqual(last, {"source": "rules.ignore", "line": 2,
                               "raw": "!out/keep.txt", "pattern": "out/keep.txt",
                               "negated": True, "matched_path": "out/keep.txt"})
        self.assertFalse(self.decision(text, "out/no.txt")[0])

    def test_negation_of_parent_inherits_to_descendants(self):
        self.assertTrue(self.decision("**\n!out/keep\n", "out/keep/deep/file")[0])
        included, last = self.decision("**\n!out/keep\nout/keep/deep\n", "out/keep/deep/file")
        self.assertFalse(included)
        self.assertEqual(last["matched_path"], "out/keep/deep")
        self.assertEqual(last["line"], 3)

    def test_last_matching_line_including_redundant_rule(self):
        included, last = self.decision("*.txt\nroot.txt\n!root*\n", "root.txt")
        self.assertTrue(included)
        self.assertEqual(last["line"], 3)
        included, last = self.decision("out\nout/file\n", "out/file")
        self.assertFalse(included)
        self.assertEqual(last["line"], 2)

    def test_invalid_patterns_rejected_even_with_empty_context(self):
        patterns = ["!", "!   ", "[", "[]", "[^]", "[z-a]", "[a-]", "[-a]", "x]",
                    "abc\\", "x\x00y", "[a/b]", "../escape", "/../escape"]
        for pattern in patterns:
            with self.subTest(pattern=pattern):
                with self.assertRaises(InputError):
                    parse_rules("# comment\n" + pattern, "bad.ignore")

    def test_unsupported_patterns_explicitly_rejected(self):
        for pattern in ("a**b", "**name", "***", "foo/**bar", r"name\z", "dir/../!name", "a\x1cb"):
            with self.subTest(pattern=pattern):
                with self.assertRaisesRegex(InputError, "bad.ignore:2:"):
                    parse_rules("# comment\n" + pattern, "bad.ignore")

    def test_oversized_line_rejected(self):
        with self.assertRaisesRegex(InputError, "65535"):
            parse_rules("a" * 65536, "long")


if __name__ == "__main__":
    unittest.main()
