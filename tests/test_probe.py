# tests/test_probe.py
import importlib.util, io, pathlib, re, unittest, unittest.mock, urllib.parse
from datetime import date

PROBE = pathlib.Path(__file__).resolve().parents[1] / "skills" / "wheel-check" / "scripts" / "probe.py"

def load_probe():
    spec = importlib.util.spec_from_file_location("probe", PROBE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

class TestContract(unittest.TestCase):
    def test_module_exposes_exit_codes(self):
        p = load_probe()
        self.assertEqual((p.EXIT_OK, p.EXIT_DEGRADED, p.EXIT_BAD_ARGS), (0, 2, 3))

class TestValidateTerms(unittest.TestCase):
    def test_splits_and_strips_pipe_separated_terms(self):
        p = load_probe()
        self.assertEqual(p.validate_terms("test selection| affected tests "),
                         ["test selection", "affected tests"])

    def test_empty_string_raises(self):
        p = load_probe()
        with self.assertRaises(ValueError):
            p.validate_terms("")

    def test_only_separators_raises(self):
        p = load_probe()
        with self.assertRaises(ValueError):
            p.validate_terms("|  |")

class TestCliBadArgs(unittest.TestCase):
    def test_empty_terms_exits_3(self):
        p = load_probe()
        self.assertEqual(p.main(["search", "--terms", ""]), 3)

    def test_bad_source_exits_3(self):
        p = load_probe()
        self.assertEqual(p.main(["search", "--terms", "x", "--source", "nope"]), 3)

class TestMainSearchTerms(unittest.TestCase):
    """Task 2 review minor: main() must not discard validate_terms()'s cleaned list.

    Raw `--terms " a |  | b | "` must never reach the query builder: empty groups and
    surrounding whitespace have to be gone by the time the search path reads them.
    """

    def test_main_replaces_raw_terms_with_the_cleaned_list(self):
        p = load_probe()
        ns = p.build_parser().parse_args(["search", "--terms", " a |  | b | "])
        self.assertEqual(ns.terms, " a |  | b | ")  # argparse hands over the raw string

        class StubParser(object):
            def parse_args(self, argv):
                return ns

        p.build_parser = lambda: StubParser()  # fresh module per test; no leakage
        self.assertEqual(p.main([]), 0)
        self.assertEqual(ns.terms, ["a", "b"])

    def test_whitespace_padded_terms_still_exit_ok_on_the_real_cli(self):
        p = load_probe()
        self.assertEqual(p.main(["search", "--terms", " a |  | b | "]), 0)

FAKE_REPO = {
    "full_name": "Tinder/bazel-diff", "html_url": "https://github.com/Tinder/bazel-diff",
    "stargazers_count": 526, "pushed_at": "2026-10-06T04:00:00Z",
    "license": {"spdx_id": "Apache-2.0"}, "archived": False,
    "language": "Java", "description": "Incremental build/test selection",
}

class TestGithubTiers(unittest.TestCase):
    def test_strict_tier_hit_wins(self):
        p = load_probe()
        calls = []
        def fetch(url, **kw):
            calls.append(url)
            return {"total_count": 1, "items": [FAKE_REPO]}
        out = p.github_search(["test selection"], 8, fetch=fetch)
        self.assertEqual(out["tier"], "strict")
        self.assertEqual(len(calls), 1)
        self.assertIn("stars%3A%3E50", calls[0].replace(":", "%3A"))
        self.assertEqual(out["results"][0]["full_name"], "Tinder/bazel-diff")
        self.assertEqual(out["results"][0]["kind"], "repo")

    def test_falls_through_to_relaxed_then_plain(self):
        p = load_probe()
        seen = []
        def fetch(url, **kw):
            seen.append(url)
            if len(seen) == 1:
                return {"total_count": 0, "items": []}
            if len(seen) == 2:
                return {"total_count": 0, "items": []}
            return {"total_count": 1, "items": [FAKE_REPO]}
        out = p.github_search(["test selection"], 8, fetch=fetch)
        self.assertEqual(out["tier"], "plain")
        self.assertEqual(len(seen), 3)

    def test_all_tiers_empty_returns_ok_empty(self):
        p = load_probe()
        out = p.github_search(["nonsense"], 8, fetch=lambda url, **kw: {"total_count": 0, "items": []})
        self.assertEqual(out["tier"], "none")
        self.assertEqual(out["results"], [])

class TestTierDefinitions(unittest.TestCase):
    def test_tiers_are_ordered_strict_relaxed_plain(self):
        p = load_probe()
        self.assertEqual([name for name, _ in p.github_tiers()],
                         ["strict", "relaxed", "plain"])

    def test_relaxed_and_plain_suffixes_match_the_contract(self):
        p = load_probe()
        tiers = dict(p.github_tiers())
        self.assertEqual(tiers["relaxed"], " stars:>50 fork:false")
        self.assertEqual(tiers["plain"], "")

    def test_strict_suffix_carries_stars_push_floor_and_fork_filters(self):
        p = load_probe()
        strict = dict(p.github_tiers())["strict"]
        self.assertIn("stars:>50", strict)
        self.assertIn("fork:false", strict)
        m = re.search(r"pushed:>(\d{4}-\d{2}-\d{2})", strict)
        self.assertIsNotNone(m, "strict tier needs a pushed:> date floor: %r" % strict)
        self.assertLessEqual(abs((date.today() - date.fromisoformat(m.group(1))).days - 540), 1)

class TestBuildQuery(unittest.TestCase):
    """Contract (controller ruling): plain terms, space-joined. No quotes, no parens.

    Quoting a concept noun forced an exact-phrase match (measured 11 -> 1 hits);
    a parenthesised OR is not honoured by GitHub (measured 11108 noise hits).
    """

    def test_multiple_terms_are_space_joined_as_plain_terms(self):
        p = load_probe()
        self.assertEqual(p.build_query(["test selection", "affected tests"], " stars:>50"),
                         "test selection affected tests stars:>50")

    def test_single_term_passes_through_unquoted(self):
        p = load_probe()
        self.assertEqual(p.build_query(["test selection"], ""), "test selection")

    def test_never_emits_quotes_or_parentheses(self):
        p = load_probe()
        for terms in (["a"], ["a", "b"], ["test selection", "affected tests", "impacted tests"]):
            q = p.build_query(terms, " stars:>50")
            for ch in ('"', "'", "(", ")"):
                self.assertNotIn(ch, q, "quotes/parens change GitHub's query semantics: %r" % q)

    def test_build_query_appends_the_tier_suffix_verbatim(self):
        p = load_probe()
        self.assertEqual(p.build_query(["a"], " fork:false"), "a fork:false")

class TestNormalizeRepo(unittest.TestCase):
    def test_normalizes_hard_facts_to_a_kind_repo_entry(self):
        p = load_probe()
        out = p.normalize_repo(FAKE_REPO)
        self.assertEqual(out["kind"], "repo")
        self.assertEqual(out["full_name"], "Tinder/bazel-diff")
        self.assertEqual(out["url"], "https://github.com/Tinder/bazel-diff")
        self.assertEqual(out["stars"], 526)
        self.assertEqual(out["pushed_at"], "2026-10-06")
        self.assertEqual(out["license"], "Apache-2.0")
        self.assertIs(out["archived"], False)
        self.assertEqual(out["language"], "Java")
        self.assertEqual(out["description"], "Incremental build/test selection")

    def test_missing_fields_degrade_to_none_not_crash(self):
        p = load_probe()
        out = p.normalize_repo({})
        self.assertEqual(out["kind"], "repo")
        self.assertIsNone(out["full_name"])
        self.assertIsNone(out["url"])
        self.assertIsNone(out["stars"])
        self.assertEqual(out["pushed_at"], "")
        self.assertIsNone(out["license"])
        self.assertIs(out["archived"], False)
        self.assertIsNone(out["language"])
        self.assertIsNone(out["description"])

class TestGithubRequestBudget(unittest.TestCase):
    def test_terms_are_merged_into_one_request_per_tier(self):
        p = load_probe()
        calls = []
        def fetch(url, **kw):
            calls.append(url)
            return {"total_count": 1, "items": [FAKE_REPO]}
        p.github_search(["test selection", "affected tests", "impacted tests"], 8, fetch=fetch)
        self.assertEqual(len(calls), 1,
                         "terms must merge into one query, not one request each (quota 10/min)")
        q = urllib.parse.parse_qs(urllib.parse.urlparse(calls[0]).query)["q"][0]
        self.assertIn("test selection affected tests impacted tests", q)
        self.assertNotIn('"', q)

    def test_three_terms_exhaust_three_tiers_in_three_requests(self):
        p = load_probe()
        calls = []
        def fetch(url, **kw):
            calls.append(url)
            return {"total_count": 0, "items": []}
        out = p.github_search(["test selection", "affected tests", "impacted tests"], 8, fetch=fetch)
        self.assertEqual(len(calls), 3)
        self.assertEqual(out["tier"], "none")

    def test_url_carries_star_sort_and_the_requested_limit(self):
        p = load_probe()
        calls = []
        def fetch(url, **kw):
            calls.append(url)
            return {"total_count": 1, "items": [FAKE_REPO]}
        p.github_search(["test selection"], 8, fetch=fetch)
        params = urllib.parse.parse_qs(urllib.parse.urlparse(calls[0]).query)
        self.assertEqual(params["sort"], ["stars"])
        self.assertEqual(params["order"], ["desc"])
        self.assertEqual(params["per_page"], ["8"])

    def test_strict_tier_url_really_applies_the_strict_filter(self):
        p = load_probe()
        calls = []
        def fetch(url, **kw):
            calls.append(url)
            return {"total_count": 1, "items": [FAKE_REPO]}
        p.github_search(["test selection"], 8, fetch=fetch)
        q = urllib.parse.parse_qs(urllib.parse.urlparse(calls[0]).query)["q"][0]
        self.assertTrue(q.endswith(dict(p.github_tiers())["strict"]),
                        "strict tier query missing its filters: %r" % q)

class TestGithubSearchStrictOnly(unittest.TestCase):
    def test_strict_only_sends_one_request_and_never_falls_through(self):
        p = load_probe()
        calls = []
        def fetch(url, **kw):
            calls.append(url)
            return {"total_count": 0, "items": []}
        out = p.github_search(["test selection"], 8, fetch=fetch, strict_only=True)
        self.assertEqual(len(calls), 1)
        self.assertEqual(out, {"tier": "none", "total": 0, "results": []})

    def test_total_count_is_passed_through(self):
        p = load_probe()
        out = p.github_search(["test selection"], 8,
                              fetch=lambda url, **kw: {"total_count": 42, "items": [FAKE_REPO]})
        self.assertEqual(out["total"], 42)
        self.assertEqual(len(out["results"]), 1)

class TestUrlopenJson(unittest.TestCase):
    """The default `fetch` is the code that runs for real; every other test injects a fake."""

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def test_parses_json_body_and_sends_ua_and_15s_timeout(self):
        p = load_probe()
        seen = {}
        def fake_urlopen(req, timeout=None):
            # header_items() is what urllib actually puts on the wire; get_header()
            # is case-sensitive against the stored "User-agent" key.
            sent = {k.lower(): v for k, v in req.header_items()}
            seen["url"] = req.full_url
            seen["ua"] = sent.get("user-agent")
            seen["accept"] = sent.get("accept")
            seen["timeout"] = timeout
            return self._Resp(b'{"total_count": 7, "items": []}')
        with unittest.mock.patch.object(p.urllib.request, "urlopen", fake_urlopen):
            out = p.urlopen_json("https://api.github.com/search/repositories?q=x")
        self.assertEqual(out, {"total_count": 7, "items": []})
        self.assertEqual(seen["timeout"], 15)
        self.assertEqual(seen["ua"], "wheel-check")
        self.assertEqual(seen["accept"], "application/vnd.github+json")

if __name__ == "__main__":
    unittest.main()
