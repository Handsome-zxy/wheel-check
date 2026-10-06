# tests/test_probe.py
import contextlib, importlib.util, io, json, pathlib, re, sys, unittest, unittest.mock, urllib.error, urllib.parse
from datetime import date

PROBE = pathlib.Path(__file__).resolve().parents[1] / "skills" / "wheel-check" / "scripts" / "probe.py"

def load_probe():
    spec = importlib.util.spec_from_file_location("probe", PROBE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

def stub_search(p, code=0, env=None):
    """Replace the network boundary of main()'s search path; return what it received.

    Global constraint: no test may hit api.github.com (unauthenticated search allows
    10 requests/minute, and a shared IP would make the suite flaky). load_probe()
    builds a fresh module per call, so the swap cannot leak between tests.
    """
    if env is None:
        env = {"status": "ok", "reason": None, "sources": [], "results": []}
    seen = {}
    def stub(terms, source="auto", limit=8, fetch=None, strict_only=False):
        seen.update(terms=terms, source=source, limit=limit, strict_only=strict_only)
        return code, env
    p.search_envelope = stub
    return seen

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

class TestForceUtf8(unittest.TestCase):
    """Review Focus #3: a GBK console must not corrupt UTF-8 output.

    force_utf8() is the only thing standing between a Windows GBK console and
    mojibake, so the byte-level contract is asserted here rather than by hand.
    """

    def test_stdout_emits_utf8_bytes_even_when_the_stream_started_as_gbk(self):
        p = load_probe()
        buf = io.BytesIO()
        # newline="\n" keeps the expectation independent of os.linesep.
        fake = io.TextIOWrapper(buf, encoding="gbk", newline="\n")
        real_stdout, real_stderr = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = fake
        try:
            p.force_utf8()
            print("测试")
            fake.flush()
        finally:
            sys.stdout, sys.stderr = real_stdout, real_stderr
        self.assertEqual(buf.getvalue(), "测试\n".encode("utf-8"))

class TestCliBadArgs(unittest.TestCase):
    """Review Focus #1: a bad call exits 3, explains itself on stderr, prints no stdout."""

    def _run(self, argv):
        p = load_probe()
        stub_search(p)  # a bad call must never become a retrieval; and never a real one
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = p.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_empty_terms_exits_3(self):
        code, out, err = self._run(["search", "--terms", ""])
        self.assertEqual(code, 3)
        self.assertEqual(out, "", "bad args must not print a results envelope on stdout")
        self.assertIn("error", err)

    def test_bad_source_exits_3(self):
        code, out, err = self._run(["search", "--terms", "x", "--source", "nope"])
        self.assertEqual(code, 3)
        self.assertEqual(out, "")
        self.assertIn("error", err)

    def test_help_exits_0(self):
        code, out, err = self._run(["-h"])
        self.assertEqual(code, 0)
        self.assertIn("usage", out)
        self.assertEqual(err, "")

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

        seen = stub_search(p)                  # search really retrieves now: mock the network
        p.build_parser = lambda: StubParser()  # fresh module per test; no leakage
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(p.main([]), 0)
        self.assertEqual(ns.terms, ["a", "b"])
        self.assertEqual(seen["terms"], ["a", "b"],
                         "the retrieval layer must get the cleaned list, not the raw string")

    def test_whitespace_padded_terms_still_exit_ok_on_the_real_cli(self):
        p = load_probe()
        stub_search(p)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(p.main(["search", "--terms", " a |  | b | "]), 0)

class TestCliLimitBounds(unittest.TestCase):
    """An out-of-range --limit is a malformed call: exit 3, not a silent clamp."""

    def _run(self, argv):
        p = load_probe()
        stub_search(p)  # bounded values still retrieve; keep the quota untouched
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = p.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_limit_zero_exits_3(self):
        code, out, err = self._run(["search", "--terms", "x", "--limit", "0"])
        self.assertEqual(code, 3)
        self.assertEqual(out, "")
        self.assertIn("--limit", err)

    def test_limit_101_exits_3(self):
        code, out, err = self._run(["search", "--terms", "x", "--limit", "101"])
        self.assertEqual(code, 3)
        self.assertEqual(out, "")
        self.assertIn("--limit", err)

    def test_non_integer_limit_exits_3(self):
        self.assertEqual(self._run(["search", "--terms", "x", "--limit", "abc"])[0], 3)

    def test_negative_limit_exits_3(self):
        self.assertEqual(self._run(["search", "--terms", "x", "--limit", "-5"])[0], 3)

    def test_boundary_values_are_accepted(self):
        self.assertEqual(self._run(["search", "--terms", "x", "--limit", "1"])[0], 0)
        self.assertEqual(self._run(["search", "--terms", "x", "--limit", "100"])[0], 0)

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

    Quoting forces an exact-phrase match against name/description: measured
    `test selection` 11 -> 1 and a quoted pair -> 0 (a silent skip). A
    parenthesised OR *is* honoured, but over generic concept nouns it degenerates
    into near-synonym noise (11108), so multi-term input is space-joined AND
    instead of promising a union.
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

class TestBuildQueryRejectsRawString(unittest.TestCase):
    """`main()` hands over a cleaned list; a raw `--terms` string must never iterate chars."""

    def test_raw_string_raises_a_clear_value_error(self):
        p = load_probe()
        with self.assertRaises(ValueError) as ctx:
            p.build_query("test selection", " stars:>50")
        self.assertIn("list", str(ctx.exception))

    def test_the_validated_list_flows_straight_in(self):
        p = load_probe()
        self.assertEqual(p.build_query(p.validate_terms(" test selection | affected tests | "), ""),
                         "test selection affected tests")

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

class TestLimitBounds(unittest.TestCase):
    """GitHub accepts per_page 1..100 only and silently normalises anything else.

    Measured: per_page=0 and per_page=101 both return HTTP 200, and per_page=0 came
    back with more items than the requested limit — never send an out-of-range value.
    """

    def _per_page(self, p, limit):
        calls = []
        def fetch(url, **kw):
            calls.append(url)
            return {"total_count": 1, "items": [FAKE_REPO]}
        p.github_search(["test selection"], limit, fetch=fetch)
        return urllib.parse.parse_qs(urllib.parse.urlparse(calls[0]).query)["per_page"][0]

    def test_in_range_limits_are_sent_verbatim(self):
        p = load_probe()
        self.assertEqual(self._per_page(p, 8), "8")
        self.assertEqual(self._per_page(p, 1), "1")
        self.assertEqual(self._per_page(p, 100), "100")

    def test_zero_is_clamped_up_to_one(self):
        p = load_probe()
        self.assertEqual(self._per_page(p, 0), "1")

    def test_above_max_is_clamped_down_to_one_hundred(self):
        p = load_probe()
        self.assertEqual(self._per_page(p, 101), "100")

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

class TestDegraded(unittest.TestCase):
    """Review Focus #4: a rate-limited search must never be reported as an empty one.

    The envelope is the only channel the model reads, and `ok` + `results: []` is the
    one signal that means "nothing exists" (the skill then stays silent). A 403 that
    came back as `ok`/`[]` would make the skill silently skip a real precedent.
    """

    def test_rate_limited_after_retry_is_degraded(self):
        p = load_probe()
        calls = []
        def fetch(url, **kw):
            calls.append(url)
            raise p.RateLimited("403")
        code, env = p.search_envelope(["x"], source="github", fetch=fetch)
        self.assertEqual(code, 2)
        self.assertEqual(env["status"], "degraded")
        self.assertEqual(env["reason"], "rate-limited")
        self.assertEqual(env["results"], [])
        self.assertEqual(len(calls), 2)   # original + 1 retry, never more

    def test_transient_then_ok(self):
        p = load_probe()
        state = {"n": 0}
        def fetch(url, **kw):
            state["n"] += 1
            if state["n"] == 1:
                raise p.RateLimited("403")
            return {"total_count": 1, "items": [FAKE_REPO]}
        code, env = p.search_envelope(["x"], source="github", fetch=fetch)
        self.assertEqual(code, 0)
        self.assertEqual(env["status"], "ok")
        self.assertEqual(len(env["results"]), 1)

    def test_empty_is_ok_and_not_degraded(self):
        p = load_probe()
        code, env = p.search_envelope(["x"], source="github",
                                      fetch=lambda url, **kw: {"total_count": 0, "items": []})
        self.assertEqual(code, 0)
        self.assertEqual(env["status"], "ok")
        self.assertEqual(env["results"], [])

class TestRetryPlacement(unittest.TestCase):
    """The retry belongs to the request, not to the tier loop (controller ruling).

    `github_search` spends one request per tier; retrying around the loop would burn
    the quota of an already-exhausted API on the relaxed/plain tiers. With the retry
    on the request, a persistent 403 costs exactly 2 requests and then aborts.
    """

    def _no_sleep(self, p):
        return unittest.mock.patch.object(p.time, "sleep", lambda seconds: None)

    def test_the_retry_repeats_the_same_tier_and_never_advances(self):
        p = load_probe()
        calls = []
        def fetch(url, **kw):
            calls.append(url)
            raise p.RateLimited("403")
        with self._no_sleep(p):
            code, env = p.search_envelope(["x"], source="github", fetch=fetch)
        self.assertEqual(code, p.EXIT_DEGRADED)
        self.assertEqual(len(set(calls)), 1,
                         "a rate-limited search must abort, not fall through to relaxed/plain")

    def test_one_retry_per_search_caps_requests_at_three_tiers_plus_one(self):
        p = load_probe()
        calls = []
        def fetch(url, **kw):
            calls.append(url)
            if len(calls) in (1, 3):   # tier1: 403 then empty; tier2: 403 again
                raise p.RateLimited("403")
            return {"total_count": 0, "items": []}
        with self._no_sleep(p):
            code, env = p.search_envelope(["x"], source="github", fetch=fetch)
        self.assertEqual(code, p.EXIT_DEGRADED)
        self.assertEqual(env["reason"], "rate-limited")
        self.assertEqual(len(calls), 3,
                         "budget is 3 tier requests + 1 retry: the second 403 must abort")

    def test_backoff_is_waited_exactly_once_per_retry(self):
        p = load_probe()
        slept = []
        def fetch(url, **kw):
            raise p.RateLimited("429")
        with unittest.mock.patch.object(p.time, "sleep", slept.append):
            p.search_envelope(["x"], source="github", fetch=fetch)
        self.assertEqual(slept, [1.0])   # spec 6: back off once, never busy-wait

class TestFetchFailureClassification(unittest.TestCase):
    """Every way a fetch can fail must land in `degraded`, never in a traceback."""

    def _http_error(self, code):
        return urllib.error.HTTPError("https://api.github.com/x", code, "boom", {}, None)

    def test_http_403_is_rate_limited_and_retried(self):
        p = load_probe()
        calls = []
        def fetch(url, **kw):
            calls.append(url)
            raise self._http_error(403)
        with unittest.mock.patch.object(p.time, "sleep", lambda seconds: None):
            code, env = p.search_envelope(["x"], source="github", fetch=fetch)
        self.assertEqual((code, env["reason"]), (p.EXIT_DEGRADED, "rate-limited"))
        self.assertEqual(len(calls), 2)

    def test_http_429_is_rate_limited_too(self):
        p = load_probe()
        def fetch(url, **kw):
            raise self._http_error(429)
        with unittest.mock.patch.object(p.time, "sleep", lambda seconds: None):
            code, env = p.search_envelope(["x"], source="github", fetch=fetch)
        self.assertEqual((code, env["reason"]), (p.EXIT_DEGRADED, "rate-limited"))

    def test_http_500_degrades_without_spending_a_retry(self):
        p = load_probe()
        calls = []
        def fetch(url, **kw):
            calls.append(url)
            raise self._http_error(500)
        code, env = p.search_envelope(["x"], source="github", fetch=fetch)
        self.assertEqual((code, env["reason"]), (p.EXIT_DEGRADED, "timeout"))
        self.assertEqual(len(calls), 1, "only 403/429 are quota errors worth a retry")

    def test_network_error_degrades_instead_of_looking_empty(self):
        # Spec 4.3 also lists a `network` reason; the brief's _classify folds every
        # non-quota failure into `timeout`, so that is what is pinned here.
        p = load_probe()
        def fetch(url, **kw):
            raise urllib.error.URLError("no route to host")
        code, env = p.search_envelope(["x"], source="github", fetch=fetch)
        self.assertEqual(code, p.EXIT_DEGRADED)
        self.assertEqual(env["status"], "degraded")
        self.assertEqual(env["results"], [])
        self.assertIsNotNone(env["reason"])

    def test_a_malformed_body_degrades_instead_of_crashing(self):
        p = load_probe()
        code, env = p.search_envelope(["x"], source="github", fetch=lambda url, **kw: None)
        self.assertEqual((code, env["status"], env["results"]), (p.EXIT_DEGRADED, "degraded", []))

class TestSearchEnvelope(unittest.TestCase):
    """Spec 4.3: status / reason / sources / results are all required, always."""

    KEYS = ["reason", "results", "sources", "status"]

    def _env(self, p, items=None, total=None):
        items = [FAKE_REPO] if items is None else items
        total = len(items) if total is None else total
        def fetch(url, **kw):
            return {"total_count": total, "items": items}
        return p.search_envelope(["x"], source="github", fetch=fetch)[1]

    def test_ok_envelope_carries_the_four_required_keys(self):
        p = load_probe()
        env = self._env(p)
        self.assertEqual(sorted(env), self.KEYS)
        self.assertEqual(env["status"], "ok")
        self.assertIsNone(env["reason"])
        self.assertEqual(sorted(env["sources"][0]), ["source", "status", "tier", "total"])
        self.assertEqual(env["sources"][0]["source"], "github")
        self.assertEqual(env["sources"][0]["status"], "ok")
        self.assertEqual(env["sources"][0]["tier"], "strict")
        self.assertEqual(env["results"][0]["kind"], "repo")

    def test_total_is_the_api_total_count_not_the_capped_len_of_results(self):
        # `results` is capped by per_page; `total` is what the API says exists. The
        # two legitimately disagree and must not be conflated by the caller.
        p = load_probe()
        env = self._env(p, items=[FAKE_REPO], total=5260)
        self.assertEqual(env["sources"][0]["total"], 5260)
        self.assertEqual(len(env["results"]), 1)

    def test_empty_ok_envelope_reports_the_none_tier(self):
        p = load_probe()
        env = self._env(p, items=[], total=0)
        self.assertEqual((env["status"], env["reason"], env["results"]), ("ok", None, []))
        self.assertEqual(env["sources"][0]["tier"], "none")
        self.assertEqual(env["sources"][0]["total"], 0)

    def test_degraded_envelope_carries_the_four_required_keys(self):
        p = load_probe()
        def fetch(url, **kw):
            raise p.Timeout("boom")
        code, env = p.search_envelope(["x"], source="github", fetch=fetch)
        self.assertEqual(code, p.EXIT_DEGRADED)
        self.assertEqual(sorted(env), self.KEYS)
        self.assertEqual(env["status"], "degraded")
        self.assertEqual(env["reason"], "timeout")
        self.assertEqual(env["results"], [])
        self.assertEqual(env["sources"], [{"source": "github", "status": "degraded"}])

    def test_strict_only_reaches_the_github_tier_loop(self):
        p = load_probe()
        calls = []
        def fetch(url, **kw):
            calls.append(url)
            return {"total_count": 0, "items": []}
        env = p.search_envelope(["x"], source="github", fetch=fetch, strict_only=True)[1]
        self.assertEqual(len(calls), 1)
        self.assertEqual(env["sources"][0]["tier"], "none")

class TestMainSearchOutput(unittest.TestCase):
    """main() must print the envelope as JSON on stdout and return its exit code."""

    def _run(self, p, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = p.main(argv)
        return code, out.getvalue()

    def test_ok_search_prints_the_envelope_and_exits_0(self):
        p = load_probe()
        env = {"status": "ok", "reason": None,
               "sources": [{"source": "github", "tier": "plain", "total": 1, "status": "ok"}],
               "results": [p.normalize_repo(FAKE_REPO)]}
        stub_search(p, code=p.EXIT_OK, env=env)
        code, out = self._run(p, ["search", "--terms", "test selection"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), env)

    def test_degraded_search_prints_the_envelope_and_exits_2(self):
        p = load_probe()
        env = {"status": "degraded", "reason": "rate-limited",
               "sources": [{"source": "github", "status": "degraded"}], "results": []}
        stub_search(p, code=p.EXIT_DEGRADED, env=env)
        code, out = self._run(p, ["search", "--terms", "test selection"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)["status"], "degraded")

    def test_cleaned_terms_and_flags_reach_the_retrieval_layer(self):
        p = load_probe()
        seen = stub_search(p)
        self._run(p, ["search", "--terms", " a |  | b | ", "--source", "github",
                      "--limit", "3", "--strict"])
        self.assertEqual(seen["terms"], ["a", "b"])
        self.assertIsInstance(seen["terms"], list,
                              "validate_terms() must not run a second time on a list")
        self.assertEqual(seen["source"], "github")
        self.assertEqual(seen["limit"], 3)
        self.assertIs(seen["strict_only"], True)

    def test_bad_terms_never_reach_the_retrieval_layer(self):
        p = load_probe()
        seen = stub_search(p)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = p.main(["search", "--terms", "|  |"])
        self.assertEqual(code, p.EXIT_BAD_ARGS)
        self.assertEqual(seen, {}, "a malformed call must not become a degraded retrieval")
        self.assertEqual(out.getvalue(), "")

class TestMainEndToEnd(unittest.TestCase):
    """main() is the composition root: every failure path must RETURN an exit code.

    These run the real pipeline (main -> search_envelope -> github_search ->
    urlopen_json) with only `urllib.request.urlopen` replaced, so they also prove the
    default `fetch` is wired in. An exception from the network layer must surface as
    EXIT_DEGRADED + a reason, never as a traceback out of main() and never as a
    silent `ok` + empty results.
    """

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _run(self, urlopen):
        p = load_probe()
        out, err = io.StringIO(), io.StringIO()
        with unittest.mock.patch.object(p.urllib.request, "urlopen", urlopen), \
                unittest.mock.patch.object(p.time, "sleep", lambda seconds: None), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = p.main(["search", "--terms", "test selection"])
        return code, out.getvalue(), err.getvalue()

    def test_a_real_wire_response_becomes_an_ok_envelope(self):
        def urlopen(req, timeout=None):
            body = json.dumps({"total_count": 1, "items": [FAKE_REPO]}).encode("utf-8")
            return self._Resp(body)
        code, out, _ = self._run(urlopen)
        self.assertEqual(code, 0)
        env = json.loads(out)
        self.assertEqual(env["status"], "ok")
        self.assertEqual(env["sources"], [{"source": "github", "tier": "strict",
                                           "total": 1, "status": "ok"}])
        self.assertEqual(env["results"][0]["full_name"], "Tinder/bazel-diff")

    def test_http_403_from_the_wire_is_degraded_not_a_traceback(self):
        def urlopen(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 403, "rate limit exceeded", {}, None)
        code, out, _ = self._run(urlopen)
        self.assertEqual(code, 2)
        env = json.loads(out)
        self.assertEqual((env["status"], env["reason"], env["results"]),
                         ("degraded", "rate-limited", []))

    def test_a_dead_socket_is_degraded_not_a_traceback(self):
        def urlopen(req, timeout=None):
            raise urllib.error.URLError("no route to host")
        code, out, _ = self._run(urlopen)
        self.assertEqual(code, 2)
        env = json.loads(out)
        self.assertEqual((env["status"], env["results"]), ("degraded", []))
        self.assertIsNotNone(env["reason"])

    def test_an_unreadable_body_is_degraded_not_ok_empty(self):
        def urlopen(req, timeout=None):
            return self._Resp(b"<html>gateway error</html>")
        code, out, _ = self._run(urlopen)
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)["status"], "degraded")

class TestEnvelopeOutputEncoding(unittest.TestCase):
    """Review Focus #3: the envelope is printed with ensure_ascii=False, so a GBK
    console must still get UTF-8 bytes: mojibake here corrupts the one contract the
    model parses, and a UnicodeEncodeError would kill the search entirely."""

    def test_chinese_description_survives_a_gbk_stdout(self):
        p = load_probe()
        env = {"status": "ok", "reason": None,
               "sources": [{"source": "github", "tier": "plain", "total": 1, "status": "ok"}],
               "results": [{"kind": "repo", "full_name": "o/n", "description": "增量测试选择"}]}
        stub_search(p, env=env)
        buf = io.BytesIO()
        fake = io.TextIOWrapper(buf, encoding="gbk", newline="\n")
        real_stdout = sys.stdout
        sys.stdout = fake
        try:
            code = p.main(["search", "--terms", "test selection"])
            fake.flush()
        finally:
            sys.stdout = real_stdout
        self.assertEqual(code, 0)
        text = buf.getvalue().decode("utf-8")
        self.assertIn("增量测试选择", text)
        self.assertEqual(json.loads(text), env)

if __name__ == "__main__":
    unittest.main()
