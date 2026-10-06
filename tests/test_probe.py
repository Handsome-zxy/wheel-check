# tests/test_probe.py
import contextlib, importlib.util, io, json, pathlib, re, socket, sys, unittest, unittest.mock, urllib.error, urllib.parse
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
        # §4.3 keeps `timeout` and `network` apart, and §6 gives them different
        # remedies, so a connection failure must not be reported as a timeout.
        p = load_probe()
        def fetch(url, **kw):
            raise urllib.error.URLError("no route to host")
        code, env = p.search_envelope(["x"], source="github", fetch=fetch)
        self.assertEqual(code, p.EXIT_DEGRADED)
        self.assertEqual(env["status"], "degraded")
        self.assertEqual(env["results"], [])
        self.assertEqual(env["reason"], "network")

    def test_dns_failure_is_network(self):
        p = load_probe()
        def fetch(url, **kw):
            raise socket.gaierror(11001, "getaddrinfo failed")
        code, env = p.search_envelope(["x"], source="github", fetch=fetch)
        self.assertEqual((code, env["reason"]), (p.EXIT_DEGRADED, "network"))

    def test_urlerror_wrapping_a_socket_timeout_is_a_timeout(self):
        # This is what urlopen actually raises when the 15s request timeout fires:
        # URLError whose `reason` is the socket timeout, not a connection failure.
        p = load_probe()
        def fetch(url, **kw):
            raise urllib.error.URLError(socket.timeout("timed out"))
        code, env = p.search_envelope(["x"], source="github", fetch=fetch)
        self.assertEqual((code, env["reason"]), (p.EXIT_DEGRADED, "timeout"))

    def test_bare_socket_timeout_is_a_timeout(self):
        p = load_probe()
        def fetch(url, **kw):
            raise socket.timeout("timed out")
        code, env = p.search_envelope(["x"], source="github", fetch=fetch)
        self.assertEqual((code, env["reason"]), (p.EXIT_DEGRADED, "timeout"))

    def test_http_504_gateway_timeout_is_a_timeout(self):
        p = load_probe()
        def fetch(url, **kw):
            raise self._http_error(504)
        code, env = p.search_envelope(["x"], source="github", fetch=fetch)
        self.assertEqual((code, env["reason"]), (p.EXIT_DEGRADED, "timeout"))

    def test_our_own_network_error_passes_straight_through(self):
        p = load_probe()
        def fetch(url, **kw):
            raise p.NetworkError("proxy refused")
        code, env = p.search_envelope(["x"], source="github", fetch=fetch)
        self.assertEqual((code, env["reason"]), (p.EXIT_DEGRADED, "network"))

class TestInternalFailure(unittest.TestCase):
    """A bug inside probe.py must degrade loudly, not masquerade as a network condition.

    `{"items": [None]}` raises AttributeError inside normalize_repo. The envelope has
    to say `degraded` (never the forbidden `ok` + []), must NOT claim a cause it does
    not have, and the real exception type has to reach stderr — stdout is reserved for
    the JSON contract, and a silent wrong label is exactly what this project forbids.
    """

    def _run(self, p, fetch):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code, env = p.search_envelope(["x"], source="github", fetch=fetch)
        return code, env, err.getvalue()

    def _payload(self, payload):
        return lambda url, **kw: payload

    def test_items_containing_none_is_degraded_and_never_ok_empty(self):
        p = load_probe()
        code, env, _ = self._run(p, self._payload({"total_count": 1, "items": [None]}))
        self.assertEqual(code, p.EXIT_DEGRADED)
        self.assertEqual(env["status"], "degraded")
        self.assertNotEqual((env["status"], env["results"]), ("ok", []),
                            "a crash must never look like a genuine empty search")
        self.assertEqual(env["results"], [])

    def test_an_internal_failure_claims_no_cause(self):
        p = load_probe()
        _, env, _ = self._run(p, self._payload({"total_count": 1, "items": [None]}))
        self.assertIsNone(env["reason"],
                          "an internal bug must not be reported as timeout/network")

    def test_the_real_exception_type_reaches_stderr(self):
        p = load_probe()
        _, _, err = self._run(p, self._payload({"total_count": 1, "items": [None]}))
        self.assertIn("AttributeError", err)
        self.assertIn("unexpected", err)

    def test_a_non_dict_body_is_degraded_and_reported(self):
        p = load_probe()
        code, env, err = self._run(p, self._payload(None))
        self.assertEqual((code, env["status"], env["results"]), (p.EXIT_DEGRADED, "degraded", []))
        self.assertIsNone(env["reason"])
        self.assertIn("AttributeError", err)

    def test_a_long_exception_message_is_truncated_on_stderr(self):
        p = load_probe()
        def fetch(url, **kw):
            raise RuntimeError("x" * 5000)
        code, env, err = self._run(p, fetch)
        self.assertEqual(code, p.EXIT_DEGRADED)
        self.assertIn("RuntimeError", err)
        self.assertLess(len(err), 400, "the diagnostic must stay one readable line")

class TestSourceDispatch(unittest.TestCase):
    """An explicit --source must never be answered with a different source's data.

    Silently returning GitHub repos for `--source hn` is how an eval records the wrong
    source's data as the source's result. Until a retriever exists, asking for it
    degrades the whole operation (exit 2) with the reason on stderr.
    """

    def _unimplemented(self, p):
        source = "hf"           # Task 6 registers it; "hn" was covered the same way
        if p._source_legs().get(source) is not None:
            self.skipTest("%s now has a retriever" % source)
        return source

    def test_a_source_without_a_retriever_degrades_instead_of_answering_with_github(self):
        p = load_probe()
        source = self._unimplemented(p)
        calls = []
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code, env = p.search_envelope(["x"], source=source,
                                          fetch=lambda url, **kw: calls.append(url))
        self.assertEqual(code, p.EXIT_DEGRADED)
        self.assertEqual(env["status"], "degraded")
        self.assertEqual(env["results"], [])
        self.assertEqual(calls, [], "--source %s must not be answered with GitHub data" % source)
        self.assertEqual(env["sources"], [{"source": source, "status": "degraded"}])
        self.assertIn(source, err.getvalue())

    def test_auto_and_explicit_github_still_query_github(self):
        p = load_probe()
        for source in ("auto", "github"):
            calls = []
            def fetch(url, **kw):
                calls.append(url)
                return {"total_count": 1, "items": [FAKE_REPO]}
            code, env = p.search_envelope(["x"], source=source, fetch=fetch)
            self.assertEqual(code, p.EXIT_OK, source)
            self.assertEqual(len(calls), 1, source)
            self.assertEqual(env["sources"][0]["source"], "github", source)

FAKE_HN = {"nbHits": 1, "hits": [{
    "title": "Show HN: Testrim – Intelligent Test Impact Analysis",
    "objectID": "47382910", "points": 42, "created_at": "2026-03-11T10:00:00Z",
    "url": "https://github.com/example/testrim",
}]}

class TestHackerNews(unittest.TestCase):
    def test_hn_item_is_normalized_as_discussion(self):
        p = load_probe()
        out = p.hn_search(["incremental test selection"], 5, fetch=lambda url, **kw: FAKE_HN)
        self.assertEqual(len(out), 1)
        item = out[0]
        self.assertEqual(item["kind"], "discussion")
        self.assertEqual(item["points"], 42)
        self.assertEqual(item["created_at"], "2026-03-11")
        self.assertEqual(item["url"], "https://news.ycombinator.com/item?id=47382910")
        self.assertEqual(item["external_url"], "https://github.com/example/testrim")
        self.assertNotIn("stars", item)          # HN 不提供硬事实
        self.assertNotIn("license", item)

    def test_hn_used_when_github_is_empty(self):
        p = load_probe()
        def fetch(url, **kw):
            if "hn.algolia.com" in url:
                return FAKE_HN
            return {"total_count": 0, "items": []}
        code, env = p.search_envelope(["incremental test selection"], source="auto", fetch=fetch)
        self.assertEqual(code, 0)
        self.assertEqual(env["status"], "ok")
        self.assertEqual([r["kind"] for r in env["results"]], ["discussion"])

    def test_hn_not_called_when_github_has_hits(self):
        p = load_probe()
        seen = []
        def fetch(url, **kw):
            seen.append(url)
            if "hn.algolia.com" in url:
                return FAKE_HN
            return {"total_count": 1, "items": [FAKE_REPO]}
        p.search_envelope(["test selection"], source="auto", fetch=fetch)
        self.assertFalse(any("hn.algolia.com" in u for u in seen))

class TestHnNormalization(unittest.TestCase):
    """§4.3: a discussion entry carries ONLY what HN can supply.

    HN has titles, points, a creation date, the item URL and the external link. It has
    no stars, no license and no archived flag, so a discussion entry must never borrow
    a repo-shaped field — that would present HN's popularity as repository maturity
    (§9, controller ruling).
    """

    def test_discussion_entry_has_exactly_the_hn_field_set(self):
        p = load_probe()
        item = p.hn_search(["x"], 5, fetch=lambda url, **kw: FAKE_HN)[0]
        self.assertEqual(sorted(item),
                         ["created_at", "external_url", "kind", "points", "title", "url"])
        for repo_field in ("stars", "license", "archived", "pushed_at", "language",
                           "description", "full_name"):
            self.assertNotIn(repo_field, item)

    def test_query_url_is_the_keyless_algolia_story_endpoint(self):
        p = load_probe()
        seen = []
        def fetch(url, **kw):
            seen.append(url)
            return FAKE_HN
        p.hn_search(["incremental test selection"], 5, fetch=fetch)
        u = urllib.parse.urlparse(seen[0])
        params = urllib.parse.parse_qs(u.query)
        self.assertEqual((u.scheme, u.netloc, u.path),
                         ("https", "hn.algolia.com", "/api/v1/search"))
        self.assertEqual(params["query"], ["incremental test selection"])
        self.assertEqual(params["tags"], ["story"], "comments and polls are not prior art")
        self.assertEqual(params["hitsPerPage"], ["5"])
        self.assertNotIn("apiKey", seen[0], "the Algolia endpoint is keyless")

    def test_created_at_without_a_timestamp_is_empty_not_a_crash(self):
        p = load_probe()
        item = p.hn_search(["x"], 5,
                           fetch=lambda url, **kw: {"hits": [{"objectID": "1"}]})[0]
        self.assertEqual(item["kind"], "discussion")
        self.assertEqual(item["created_at"], "")
        self.assertIsNone(item["points"])
        self.assertIsNone(item["title"])
        self.assertIsNone(item["external_url"])
        self.assertEqual(item["url"], "https://news.ycombinator.com/item?id=1")

    def test_a_hit_without_an_object_id_has_no_url_rather_than_id_none(self):
        p = load_probe()
        item = p.hn_search(["x"], 5, fetch=lambda url, **kw: {"hits": [{}]})[0]
        self.assertIsNone(item["url"])

class TestHnSourceDispatch(unittest.TestCase):
    """`--source` picks the legs: auto follows §5.5, `hn`/`github` are forced single sources."""

    def test_explicit_hn_queries_only_hn(self):
        p = load_probe()
        seen = []
        def fetch(url, **kw):
            seen.append(url)
            return FAKE_HN
        code, env = p.search_envelope(["incremental test selection"], source="hn", fetch=fetch)
        self.assertEqual(code, p.EXIT_OK)
        self.assertEqual(env["status"], "ok")
        self.assertIsNone(env["reason"])
        self.assertEqual([r["kind"] for r in env["results"]], ["discussion"])
        self.assertEqual(len(seen), 1)
        self.assertIn("hn.algolia.com", seen[0])

    def test_hn_source_entry_is_plain_tier_with_the_nbhits_total(self):
        # §4.3's HN example: tier "plain" (HN has no filter ladder) and `total` = the
        # source's own match count (nbHits), never len(results), which hitsPerPage caps.
        p = load_probe()
        payload = {"nbHits": 42, "hits": FAKE_HN["hits"]}
        env = p.search_envelope(["x"], source="hn", fetch=lambda url, **kw: payload)[1]
        self.assertEqual(env["sources"],
                         [{"source": "hn", "tier": "plain", "total": 42, "status": "ok"}])
        self.assertEqual(len(env["results"]), 1)

    def test_explicit_hn_failure_degrades_because_hn_was_the_whole_operation(self):
        p = load_probe()
        def fetch(url, **kw):
            raise p.Timeout("boom")
        code, env = p.search_envelope(["x"], source="hn", fetch=fetch)
        self.assertEqual(code, p.EXIT_DEGRADED)
        self.assertEqual(env["status"], "degraded")
        self.assertEqual(env["reason"], "timeout")
        self.assertEqual(env["sources"], [{"source": "hn", "status": "degraded"}],
                         "_degraded must name the leg that actually failed")
        self.assertEqual(env["results"], [])

    def test_explicit_github_never_calls_hn(self):
        p = load_probe()
        seen = []
        def fetch(url, **kw):
            seen.append(url)
            return {"total_count": 0, "items": []}
        env = p.search_envelope(["x"], source="github", fetch=fetch)[1]
        self.assertFalse(any("hn.algolia.com" in u for u in seen))
        self.assertEqual([s["source"] for s in env["sources"]], ["github"])

    def test_hn_failure_marks_the_source_failed_without_failing_the_operation(self):
        # Controller ruling: `reason` is reserved for whole-operation degradation. HN is
        # a supplement, so its failure is partial — the envelope stays ok / exit 0, and
        # the source list is what tells the caller HN did not answer.
        p = load_probe()
        def fetch(url, **kw):
            if "hn.algolia.com" in url:
                raise p.NetworkError("proxy refused")
            return {"total_count": 0, "items": []}
        code, env = p.search_envelope(["x"], source="auto", fetch=fetch)
        self.assertEqual((code, env["status"], env["reason"], env["results"]),
                         (p.EXIT_OK, "ok", None, []))
        self.assertEqual(env["sources"],
                         [{"source": "github", "tier": "none", "total": 0, "status": "ok"},
                          {"source": "hn", "status": "failed"}])

    def test_hn_consulted_but_empty_is_still_the_true_nothing_signal(self):
        p = load_probe()
        def fetch(url, **kw):
            if "hn.algolia.com" in url:
                return {"nbHits": 0, "hits": []}
            return {"total_count": 0, "items": []}
        code, env = p.search_envelope(["x"], source="auto", fetch=fetch)
        self.assertEqual((code, env["status"], env["reason"], env["results"]),
                         (p.EXIT_OK, "ok", None, []))
        self.assertEqual(env["sources"][1],
                         {"source": "hn", "tier": "plain", "total": 0, "status": "ok"})

    def test_github_failure_degrades_without_asking_hn(self):
        # §5.3 puts HN in the retry path for an EMPTY GitHub result, not for a failed
        # one: a failed primary is the whole-operation degradation, not a cue to guess.
        p = load_probe()
        seen = []
        def fetch(url, **kw):
            seen.append(url)
            raise urllib.error.URLError("no route to host")
        code, env = p.search_envelope(["x"], source="auto", fetch=fetch)
        self.assertEqual((code, env["reason"]), (p.EXIT_DEGRADED, "network"))
        self.assertFalse(any("hn.algolia.com" in u for u in seen))

    def test_hn_may_spend_the_searchs_one_retry(self):
        # The retry budget belongs to the search, not to GitHub: when GitHub came back
        # empty without spending it, HN gets the same one-retry treatment on a 429.
        p = load_probe()
        hn_calls = []
        def fetch(url, **kw):
            if "hn.algolia.com" in url:
                hn_calls.append(url)
                if len(hn_calls) == 1:
                    raise p.RateLimited("429")
                return FAKE_HN
            return {"total_count": 0, "items": []}
        with unittest.mock.patch.object(p.time, "sleep", lambda seconds: None):
            env = p.search_envelope(["x"], source="auto", fetch=fetch)[1]
        self.assertEqual(len(hn_calls), 2)
        self.assertEqual(env["sources"][1],
                         {"source": "hn", "tier": "plain", "total": 1, "status": "ok"})

    def test_an_internal_error_in_hn_reaches_stderr(self):
        # A malformed HN body is a probe.py-side surprise, not a network condition; the
        # envelope keeps its partial-failure shape but the bug must still be diagnosable.
        p = load_probe()
        def fetch(url, **kw):
            if "hn.algolia.com" in url:
                return None                      # AttributeError inside _hn_hits
            return {"total_count": 0, "items": []}
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code, env = p.search_envelope(["x"], source="auto", fetch=fetch)
        self.assertEqual((code, env["status"], env["reason"]), (p.EXIT_OK, "ok", None))
        self.assertEqual(env["sources"][1], {"source": "hn", "status": "failed"})
        self.assertIn("AttributeError", err.getvalue())

class TestMainHnSource(unittest.TestCase):
    """§10.1 evals run `--source hn`: the flag must really reach Algolia, not GitHub."""

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def test_source_hn_runs_the_real_pipeline_against_algolia_only(self):
        p = load_probe()
        seen = []
        def urlopen(req, timeout=None):
            seen.append(req.full_url)
            if "hn.algolia.com" not in req.full_url:
                raise AssertionError("--source hn must not call GitHub: %s" % req.full_url)
            return self._Resp(json.dumps(FAKE_HN).encode("utf-8"))
        out = io.StringIO()
        with unittest.mock.patch.object(p.urllib.request, "urlopen", urlopen), \
                contextlib.redirect_stdout(out):
            code = p.main(["search", "--terms", "incremental test selection",
                           "--source", "hn"])
        self.assertEqual(code, 0)
        env = json.loads(out.getvalue())
        self.assertEqual([s["source"] for s in env["sources"]], ["hn"])
        self.assertEqual(env["results"][0]["kind"], "discussion")
        self.assertEqual(env["results"][0]["url"],
                         "https://news.ycombinator.com/item?id=47382910")

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

    def test_cli_source_without_a_retriever_exits_2_and_never_calls_the_wire(self):
        p = load_probe()
        source = "hf"
        if p._source_legs().get(source) is not None:
            self.skipTest("%s now has a retriever" % source)
        def boom(req, timeout=None):
            raise AssertionError("a source with no retriever must not query anything")
        out, err = io.StringIO(), io.StringIO()
        with unittest.mock.patch.object(p.urllib.request, "urlopen", boom), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = p.main(["search", "--terms", "x", "--source", source])
        self.assertEqual(code, p.EXIT_DEGRADED)
        self.assertEqual(json.loads(out.getvalue())["status"], "degraded")
        self.assertIn(source, err.getvalue())

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
        self.assertEqual((env["status"], env["reason"], env["results"]),
                         ("degraded", "network", []))

    def test_an_unreadable_body_is_degraded_not_ok_empty(self):
        def urlopen(req, timeout=None):
            return self._Resp(b"<html>gateway error</html>")
        code, out, _ = self._run(urlopen)
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)["status"], "degraded")

    def test_an_internal_failure_stays_off_stdout_and_reports_on_stderr(self):
        # stdout must remain parseable JSON for the model; the diagnosis goes to stderr.
        def urlopen(req, timeout=None):
            body = json.dumps({"total_count": 1, "items": [None]}).encode("utf-8")
            return self._Resp(body)
        code, out, err = self._run(urlopen)
        self.assertEqual(code, 2)
        env = json.loads(out)
        self.assertEqual(env["status"], "degraded")
        self.assertEqual(env["results"], [])
        self.assertIn("AttributeError", err)

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

FAKE_HF = [{"modelId": "openai/whisper-large-v3", "downloads": 2400000, "likes": 4100}]
FAKE_RATE_LIMIT = {"resources": {
    "search": {"limit": 10, "remaining": 9, "reset": 1770000000},
    "core": {"limit": 60, "remaining": 42, "reset": 1770000000}}}

class _Resp(io.BytesIO):
    """The context-manager response the wire-level tests below feed to urlopen.

    The older wire tests each define this nested; Task 6's four wire tests share one.
    """

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

class TestHuggingFaceGate(unittest.TestCase):
    """§5.5 + Review Focus #5: HF is a CONDITIONAL source, decided by is_ai_requirement."""

    def test_ai_requirement_detected(self):
        p = load_probe()
        self.assertTrue(p.is_ai_requirement(["local speech to text"]))
        self.assertFalse(p.is_ai_requirement(["incremental test selection"]))

    def test_ai_terms_is_exactly_the_documented_set(self):
        p = load_probe()
        self.assertEqual(p.AI_TERMS,
                         {"ai", "llm", "model", "embedding", "diffusion", "speech",
                          "vision", "transcribe", "whisper", "fine-tune", "finetune",
                          "rag", "agent"})

    def test_matching_is_word_wise_so_a_substring_never_fires(self):
        # "chair" and "e-mail" contain the letters of "ai" but are not the word: a
        # substring match would drag the Hub's model noise into a generic requirement.
        p = load_probe()
        self.assertFalse(p.is_ai_requirement(["chair"]))
        self.assertFalse(p.is_ai_requirement(["e-mail"]))
        self.assertTrue(p.is_ai_requirement(["An LLM based helper"]))   # case-insensitive
        self.assertTrue(p.is_ai_requirement(["(embedding)"]))           # punctuation-tolerant
        self.assertFalse(p.is_ai_requirement([]))

    def test_hf_queried_only_for_ai_terms(self):
        p = load_probe()
        seen = []
        def fetch(url, **kw):
            seen.append(url)
            if "huggingface.co" in url:
                return FAKE_HF
            return {"total_count": 0, "items": []}
        p.search_envelope(["incremental test selection"], source="auto", fetch=fetch)
        self.assertFalse(any("huggingface.co" in u for u in seen))
        seen.clear()
        p.search_envelope(["local speech to text"], source="auto", fetch=fetch)
        self.assertTrue(any("huggingface.co" in u for u in seen))

    def test_hf_item_is_normalized_as_model(self):
        p = load_probe()
        out = p.hf_search(["speech to text"], 5, fetch=lambda url, **kw: FAKE_HF)
        self.assertEqual(out[0]["kind"], "model")
        self.assertEqual(out[0]["model_id"], "openai/whisper-large-v3")
        self.assertEqual(out[0]["url"], "https://huggingface.co/openai/whisper-large-v3")
        self.assertEqual(out[0]["downloads"], 2400000)

    def test_repo_detail_returns_hard_facts(self):
        p = load_probe()
        out = p.repo_detail("Tinder/bazel-diff", fetch=lambda url, **kw: FAKE_REPO)
        self.assertEqual(out["kind"], "repo")
        self.assertEqual(out["stars"], 526)
        self.assertEqual(out["license"], "Apache-2.0")

    def test_repo_detail_rejects_bad_full_name(self):
        p = load_probe()
        # stderr is redirected only so the expected diagnostic does not clutter the run;
        # the assertion is the brief's, unchanged.
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(p.main(["repo", "not-a-full-name"]), 3)

class TestHuggingFaceNormalization(unittest.TestCase):
    """§4.3: a `model` entry carries what the Hub supplies — never a repo-shaped field.

    Models are not code libraries (§5.5): stars/license/archived would present Hub
    downloads as repository maturity, which is the confusion §9 warns about.
    """

    def test_model_entry_has_exactly_the_hub_field_set(self):
        p = load_probe()
        item = p.hf_search(["speech to text"], 5, fetch=lambda url, **kw: FAKE_HF)[0]
        self.assertEqual(sorted(item), ["downloads", "kind", "likes", "model_id", "url"])
        for repo_field in ("stars", "license", "archived", "pushed_at", "language",
                           "description", "full_name"):
            self.assertNotIn(repo_field, item)

    def test_query_url_is_the_keyless_hub_endpoint_sorted_by_downloads(self):
        p = load_probe()
        seen = []
        p.hf_search(["speech to text"], 5,
                    fetch=lambda url, **kw: seen.append(url) or FAKE_HF)
        u = urllib.parse.urlparse(seen[0])
        params = urllib.parse.parse_qs(u.query)
        self.assertEqual((u.scheme, u.netloc, u.path),
                         ("https", "huggingface.co", "/api/models"))
        self.assertEqual(params["search"], ["speech to text"])
        self.assertEqual(params["limit"], ["5"])
        self.assertEqual(params["sort"], ["downloads"])
        self.assertEqual(params["direction"], ["-1"])
        self.assertNotIn("token", seen[0].lower(), "the Hub endpoint is keyless")

    def test_missing_id_yields_no_url_rather_than_a_fabricated_one(self):
        # Same rule HN's hits already follow: no identifier means no URL, never "/None".
        p = load_probe()
        item = p.hf_search(["x"], 5, fetch=lambda url, **kw: [{"downloads": 1}])[0]
        self.assertEqual(item["kind"], "model")
        self.assertIsNone(item["model_id"])
        self.assertIsNone(item["url"])
        self.assertIsNone(item["likes"])

    def test_an_empty_hub_answer_is_an_empty_list(self):
        p = load_probe()
        self.assertEqual(p.hf_search(["x"], 5, fetch=lambda url, **kw: []), [])

class TestHuggingFaceSourceDispatch(unittest.TestCase):
    """§5.5 / Review Focus #5: `auto` gates on the AI check, `--source hf` is forced."""

    def test_hf_is_registered_in_the_dispatch_table(self):
        p = load_probe()
        self.assertIsNotNone(p._source_legs().get("hf"),
                             "Task 6 replaces the None: the unimplemented-source path "
                             "must stop firing for hf")

    def test_explicit_hf_queries_only_the_hub(self):
        p = load_probe()
        seen = []
        def fetch(url, **kw):
            seen.append(url)
            return FAKE_HF
        code, env = p.search_envelope(["speech to text"], source="hf", fetch=fetch)
        self.assertEqual(code, p.EXIT_OK)
        self.assertEqual(env["status"], "ok")
        self.assertEqual([s["source"] for s in env["sources"]], ["hf"])
        self.assertEqual(env["sources"][0]["tier"], "plain")
        self.assertEqual(env["sources"][0]["total"], 1)
        self.assertEqual([r["kind"] for r in env["results"]], ["model"])
        self.assertEqual(len(seen), 1)
        self.assertIn("huggingface.co", seen[0])

    def test_explicit_hf_is_forced_even_for_a_non_ai_requirement(self):
        # Review Focus #5: the AI gate belongs to `auto`; `--source hf` is the forced
        # single source §5.5 reserves for debugging and evals. Skipping the query here
        # could only be reported as ok + [] — the one lie this contract forbids.
        p = load_probe()
        seen = []
        def fetch(url, **kw):
            seen.append(url)
            return FAKE_HF
        code, env = p.search_envelope(["incremental test selection"], source="hf", fetch=fetch)
        self.assertEqual(code, p.EXIT_OK)
        self.assertIn("huggingface.co", seen[0])
        self.assertEqual([r["kind"] for r in env["results"]], ["model"])

    def test_explicit_hf_failure_degrades_because_hf_was_the_whole_operation(self):
        p = load_probe()
        def fetch(url, **kw):
            raise p.NetworkError("proxy refused")
        code, env = p.search_envelope(["speech to text"], source="hf", fetch=fetch)
        self.assertEqual(code, p.EXIT_DEGRADED)
        self.assertEqual(env["status"], "degraded")
        self.assertEqual(env["reason"], "network")
        self.assertEqual(env["sources"], [{"source": "hf", "status": "degraded"}])
        self.assertEqual(env["results"], [])

    def test_explicit_hf_never_calls_github(self):
        p = load_probe()
        seen = []
        def fetch(url, **kw):
            seen.append(url)
            return FAKE_HF
        p.search_envelope(["speech to text"], source="hf", fetch=fetch)
        self.assertFalse(any("api.github.com" in u for u in seen))

    def test_source_hf_runs_the_real_pipeline_against_the_hub_only(self):
        # §10.1 evals run a forced single source: the flag must reach the Hub's API.
        p = load_probe()
        seen = []
        def urlopen(req, timeout=None):
            seen.append(req.full_url)
            if "huggingface.co" not in req.full_url:
                raise AssertionError("--source hf must not call %s" % req.full_url)
            return _Resp(json.dumps(FAKE_HF).encode("utf-8"))
        out = io.StringIO()
        with unittest.mock.patch.object(p.urllib.request, "urlopen", urlopen), \
                contextlib.redirect_stdout(out):
            code = p.main(["search", "--terms", "speech to text", "--source", "hf"])
        self.assertEqual(code, 0)
        env = json.loads(out.getvalue())
        self.assertEqual([s["source"] for s in env["sources"]], ["hf"])
        self.assertEqual(env["results"][0]["model_id"], "openai/whisper-large-v3")

class TestHuggingFaceIsAppended(unittest.TestCase):
    """Models are not code libraries: HF is APPENDED to the repo answers, never instead."""

    def test_auto_appends_models_after_repo_hits_without_dropping_them(self):
        p = load_probe()
        def fetch(url, **kw):
            if "huggingface.co" in url:
                return FAKE_HF
            return {"total_count": 1, "items": [FAKE_REPO]}
        code, env = p.search_envelope(["local speech to text"], source="auto", fetch=fetch)
        self.assertEqual(code, p.EXIT_OK)
        self.assertEqual([r["kind"] for r in env["results"]], ["repo", "model"],
                         "HF results are appended AFTER the repo hits, never in place of them")
        self.assertEqual([s["source"] for s in env["sources"]], ["github", "hf"])
        self.assertEqual(env["results"][0]["full_name"], "Tinder/bazel-diff")
        self.assertEqual(env["results"][1]["model_id"], "openai/whisper-large-v3")

    def test_a_non_ai_requirement_never_reaches_the_hub_in_auto(self):
        p = load_probe()
        seen = []
        def fetch(url, **kw):
            seen.append(url)
            return {"total_count": 1, "items": [FAKE_REPO]}
        env = p.search_envelope(["incremental test selection"], source="auto", fetch=fetch)[1]
        self.assertFalse(any("huggingface.co" in u for u in seen))
        self.assertEqual([r["kind"] for r in env["results"]], ["repo"])

    def test_a_failed_hf_supplement_is_named_in_sources_and_does_not_kill_the_answer(self):
        # The controller's per-source ruling (the shape HN already follows): a supplement
        # failure is PARTIAL — envelope ok, reason null, and `sources` carries the news.
        p = load_probe()
        def fetch(url, **kw):
            if "huggingface.co" in url:
                raise p.NetworkError("proxy refused")
            return {"total_count": 1, "items": [FAKE_REPO]}
        code, env = p.search_envelope(["local speech to text"], source="auto", fetch=fetch)
        self.assertEqual((code, env["status"], env["reason"]), (p.EXIT_OK, "ok", None))
        self.assertEqual(env["sources"][1], {"source": "hf", "status": "failed"})
        self.assertEqual([r["kind"] for r in env["results"]], ["repo"],
                         "github's answer survives a failed model search")

    def test_an_internal_error_in_hf_reaches_stderr(self):
        # A body that is not the documented array is a probe.py-side surprise, not "no
        # models": the envelope keeps its partial-failure shape and the bug is diagnosable.
        p = load_probe()
        def fetch(url, **kw):
            if "huggingface.co" in url:
                return None                      # hf_search refuses a non-list body
            return {"total_count": 1, "items": [FAKE_REPO]}
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code, env = p.search_envelope(["local speech to text"], source="auto", fetch=fetch)
        self.assertEqual((code, env["status"]), (p.EXIT_OK, "ok"))
        self.assertEqual(env["sources"][1], {"source": "hf", "status": "failed"})
        self.assertIn("ValueError", err.getvalue())
        self.assertIn("did not return a list", err.getvalue())

    def test_hf_consulted_but_empty_is_still_the_true_nothing_signal(self):
        p = load_probe()
        def fetch(url, **kw):
            if "huggingface.co" in url:
                return []
            return {"total_count": 0, "items": []}
        code, env = p.search_envelope(["local speech to text"], source="auto", fetch=fetch)
        self.assertEqual((code, env["status"], env["reason"], env["results"]),
                         (p.EXIT_OK, "ok", None, []))
        self.assertEqual([s["source"] for s in env["sources"]], ["github", "hn", "hf"])

class TestRepoCommand(unittest.TestCase):
    """§4.2: `repo <owner/name>` is the hard-fact lookup that backfills an HN hit.

    Before Task 6 the subcommand returned exit 0 with no output at all: a caller asking
    for one repository's facts got silence and no way to tell success from failure.
    """

    def _run(self, p, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = p.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_repo_prints_an_envelope_with_the_single_repo_entry(self):
        p = load_probe()
        seen = []
        def urlopen(req, timeout=None):
            seen.append(req.full_url)
            return _Resp(json.dumps(FAKE_REPO).encode("utf-8"))
        with unittest.mock.patch.object(p.urllib.request, "urlopen", urlopen):
            code, out, err = self._run(p, ["repo", "Tinder/bazel-diff"])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        env = json.loads(out)
        self.assertEqual(env["status"], "ok")
        self.assertIsNone(env["reason"])
        self.assertEqual(env["sources"], [{"source": "github", "status": "ok"}])
        self.assertEqual(len(env["results"]), 1)
        self.assertEqual(env["results"][0]["kind"], "repo")
        self.assertEqual(env["results"][0]["full_name"], "Tinder/bazel-diff")
        self.assertEqual(env["results"][0]["stars"], 526)
        self.assertEqual(env["results"][0]["pushed_at"], "2026-10-06")
        self.assertEqual(seen, ["https://api.github.com/repos/Tinder/bazel-diff"])

    def test_a_full_name_without_a_slash_exits_3_with_stderr_and_no_stdout(self):
        p = load_probe()
        def boom(req, timeout=None):
            raise AssertionError("a malformed full_name must not query anything")
        with unittest.mock.patch.object(p.urllib.request, "urlopen", boom):
            code, out, err = self._run(p, ["repo", "not-a-full-name"])
        self.assertEqual(code, 3)
        self.assertEqual(out, "", "a bad call must not print an envelope on stdout")
        self.assertIn("error", err)
        self.assertIn("/", err, "stderr must say what shape was expected")

    def test_extra_slashes_or_an_empty_half_are_bad_args_too(self):
        p = load_probe()
        for bad in ("a/b/c", "/name", "owner/", ""):
            code, out, err = self._run(p, ["repo", bad])
            self.assertEqual(code, 3, bad)
            self.assertEqual(out, "", bad)
            self.assertIn("error", err, bad)

    def test_a_failed_lookup_degrades_and_explains_itself_on_stderr(self):
        p = load_probe()
        calls = []
        def urlopen(req, timeout=None):
            calls.append(req.full_url)
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)
        with unittest.mock.patch.object(p.urllib.request, "urlopen", urlopen):
            code, out, err = self._run(p, ["repo", "Tinder/bazel-diff"])
        self.assertEqual(code, 2)
        env = json.loads(out)
        self.assertEqual(env["status"], "degraded")
        self.assertEqual(env["results"], [])
        self.assertNotEqual((env["status"], env["results"]), ("ok", []),
                            "a failed lookup must never look like a repository with no facts")
        self.assertIsNone(env["reason"],
                          "a 404 was delivered and rejected: it is not a timeout (§4.3)")
        self.assertIn("404", err, "the raw HTTP status is the diagnosable fact")
        self.assertEqual(len(calls), 1, "a rejected status is not a quota error: no retry")

    def test_repo_detail_uses_the_repos_endpoint_verbatim(self):
        p = load_probe()
        seen = []
        p.repo_detail("owner/name", fetch=lambda url, **kw: seen.append(url) or FAKE_REPO)
        self.assertEqual(seen, ["https://api.github.com/repos/owner/name"])

class TestHealthReport(unittest.TestCase):
    """§4.2 + §6: `health` = liveness + auth + quota, per source, and it must SPEAK.

    §6 puts the dead-network detection in this stage ("网络完全不通 | health 阶段即发现
    → degraded / network"). A health command that exits 0 with no output cannot make
    that call, so the report is asserted here field by field.
    """

    def test_all_legs_reachable_reports_ok_with_the_github_search_quota(self):
        p = load_probe()
        code, rep = p.health_report(fetch=lambda url, **kw: FAKE_RATE_LIMIT)
        self.assertEqual(code, p.EXIT_OK)
        self.assertEqual(rep["status"], "ok")
        self.assertIsNone(rep["reason"])
        self.assertEqual(rep["results"], [])
        self.assertEqual([s["source"] for s in rep["sources"]], ["github", "hn", "hf"])
        gh = rep["sources"][0]
        self.assertEqual(gh["status"], "ok")
        self.assertEqual(gh["auth"], "anonymous",
                         "the anonymous search quota (10/min) is what this process gets")
        self.assertEqual(gh["quota"],
                         {"search": {"limit": 10, "remaining": 9, "reset": 1770000000},
                          "core": {"limit": 60, "remaining": 42, "reset": 1770000000}})

    def test_a_token_raised_quota_is_reported_as_authenticated(self):
        p = load_probe()
        body = {"resources": {"search": {"limit": 30, "remaining": 29, "reset": 1},
                              "core": {"limit": 5000, "remaining": 4999, "reset": 1}}}
        rep = p.health_report(fetch=lambda url, **kw: body)[1]
        self.assertEqual(rep["sources"][0]["auth"], "token")
        self.assertEqual(rep["sources"][0]["quota"]["search"]["limit"], 30)

    def test_a_dead_leg_degrades_with_the_network_reason_and_names_the_source(self):
        p = load_probe()
        def fetch(url, **kw):
            if "hn.algolia.com" in url:
                raise urllib.error.URLError("no route to host")
            return FAKE_RATE_LIMIT
        code, rep = p.health_report(fetch=fetch)
        self.assertEqual(code, p.EXIT_DEGRADED)
        self.assertEqual(rep["status"], "degraded")
        self.assertEqual(rep["reason"], "network")
        self.assertEqual(rep["sources"][1]["status"], "failed")
        self.assertEqual(rep["sources"][1]["reason"], "network")
        self.assertEqual(rep["sources"][0]["status"], "ok",
                         "one dead leg must not erase the other legs' facts")

    def test_an_exhausted_search_quota_is_reported_as_rate_limited(self):
        # "Can we search right now" is the question health answers: a reachable API with a
        # spent SEARCH quota is a degraded answer, and §6's remedy (GITHUB_TOKEN) applies.
        p = load_probe()
        body = {"resources": {"search": {"limit": 10, "remaining": 0, "reset": 1770000000},
                              "core": {"limit": 60, "remaining": 60, "reset": 1}}}
        code, rep = p.health_report(fetch=lambda url, **kw: body)
        self.assertEqual((code, rep["status"], rep["reason"]),
                         (p.EXIT_DEGRADED, "degraded", "rate-limited"))
        self.assertEqual(rep["sources"][0]["status"], "ok", "the API did answer")

    def test_an_exhausted_core_quota_is_reported_without_flipping_the_verdict(self):
        # Measured live: core 60/60 used while search still had 10/10 — `repo` was already
        # 403ing. The facts must be in the report, but `core` only limits the repo backfill:
        # a non-zero health code would send the skill to L2/L3 (§6) for no gain.
        p = load_probe()
        body = {"resources": {"search": {"limit": 10, "remaining": 10, "reset": 1},
                              "core": {"limit": 60, "remaining": 0, "reset": 1791296439}}}
        code, rep = p.health_report(fetch=lambda url, **kw: body)
        self.assertEqual((code, rep["status"], rep["reason"]), (p.EXIT_OK, "ok", None))
        self.assertEqual(rep["sources"][0]["quota"]["core"]["remaining"], 0,
                         "the spent bucket is a reported fact, not a hidden one")

    def test_health_never_spends_the_search_quota(self):
        p = load_probe()
        seen = []
        p.health_report(fetch=lambda url, **kw: seen.append(url) or FAKE_RATE_LIMIT)
        self.assertEqual(len(seen), 3, "three legs, one request each")
        self.assertIn("api.github.com/rate_limit", seen[0])
        self.assertFalse(any("/search/repositories" in u for u in seen),
                         "health must not spend one of the 10 search requests/minute")
        self.assertIn("hn.algolia.com", seen[1])
        self.assertIn("huggingface.co", seen[2])

    def test_an_unreadable_rate_limit_body_is_reported_not_smoothed(self):
        p = load_probe()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code, rep = p.health_report(fetch=lambda url, **kw: {"resources": {}})
        self.assertEqual(code, p.EXIT_DEGRADED)
        self.assertEqual(rep["sources"][0]["status"], "failed")
        self.assertIsNone(rep["sources"][0]["reason"])
        self.assertIn("resources.search", err.getvalue())

    def test_a_core_bucket_missing_from_the_body_fails_loudly(self):
        # Both buckets are consumed by this build, so a body missing one is a shape we
        # cannot read; reporting a quota we could not read as "ok" is the silent answer.
        p = load_probe()
        body = {"resources": {"search": {"limit": 10, "remaining": 9, "reset": 1}}}
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code, rep = p.health_report(fetch=lambda url, **kw: body)
        self.assertEqual(code, p.EXIT_DEGRADED)
        self.assertEqual(rep["sources"][0]["status"], "failed")
        self.assertIn("resources.core", err.getvalue())

    def test_health_prints_the_report_on_stdout_and_exits_0_on_a_live_wire(self):
        p = load_probe()
        def urlopen(req, timeout=None):
            body = FAKE_RATE_LIMIT if "api.github.com" in req.full_url else {}
            return _Resp(json.dumps(body).encode("utf-8"))
        out, err = io.StringIO(), io.StringIO()
        with unittest.mock.patch.object(p.urllib.request, "urlopen", urlopen), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = p.main(["health"])
        self.assertEqual(code, 0)
        rep = json.loads(out.getvalue())
        self.assertEqual(rep["status"], "ok")
        self.assertEqual([s["source"] for s in rep["sources"]], ["github", "hn", "hf"])
        self.assertEqual(rep["sources"][0]["quota"]["search"]["remaining"], 9)

    def test_a_dead_wire_makes_health_exit_2_with_a_report_not_a_traceback(self):
        p = load_probe()
        def urlopen(req, timeout=None):
            raise urllib.error.URLError("no route to host")
        out, err = io.StringIO(), io.StringIO()
        with unittest.mock.patch.object(p.urllib.request, "urlopen", urlopen), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = p.main(["health"])
        self.assertEqual(code, 2)
        rep = json.loads(out.getvalue())
        self.assertEqual((rep["status"], rep["reason"]), ("degraded", "network"))
        self.assertEqual([s["status"] for s in rep["sources"]], ["failed"] * 3)

class TestAmbiguousGithubBody(unittest.TestCase):
    """A body that is recognisably NOT a GitHub search response must degrade.

    Task 4 review: `{}` flowed through the tier loop as "zero results", so a broken or
    ambiguous API response was indistinguishable from a genuine "nothing exists" —
    `ok` + `[]`, the one signal the skill reads as "stay silent" (§4.3).
    """

    def _run(self, p, payload):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code, env = p.search_envelope(["x"], source="github",
                                          fetch=lambda url, **kw: payload)
        return code, env, err.getvalue()

    def test_an_empty_dict_body_is_degraded_not_ok_empty(self):
        p = load_probe()
        code, env, err = self._run(p, {})
        self.assertEqual(code, p.EXIT_DEGRADED)
        self.assertEqual(env["status"], "degraded")
        self.assertEqual(env["results"], [])
        self.assertNotEqual((env["status"], env["results"]), ("ok", []),
                            "an unreadable body must never look like a genuine empty search")
        self.assertIsNone(env["reason"], "the cause is outside §4.3's reason vocabulary")
        self.assertIn("total_count", err, "stderr must name what the body was missing")

    def test_a_body_with_only_a_message_key_is_degraded(self):
        p = load_probe()
        code, env, _ = self._run(p, {"message": "Not Found"})
        self.assertEqual((code, env["status"], env["results"]),
                         (p.EXIT_DEGRADED, "degraded", []))

    def test_a_body_without_items_is_degraded(self):
        p = load_probe()
        code, env, _ = self._run(p, {"total_count": 3})
        self.assertEqual((code, env["status"], env["results"]),
                         (p.EXIT_DEGRADED, "degraded", []))

    def test_a_contradictory_total_with_no_items_is_degraded(self):
        # The API says matches exist but hands over none: reporting "none" here would
        # claim "nothing exists" against the API's own count.
        p = load_probe()
        code, env, _ = self._run(p, {"total_count": 5, "items": []})
        self.assertEqual((code, env["status"], env["results"]),
                         (p.EXIT_DEGRADED, "degraded", []))

    def test_a_genuine_empty_search_response_is_still_ok_empty(self):
        p = load_probe()
        code, env, err = self._run(p, {"total_count": 0, "items": []})
        self.assertEqual((code, env["status"], env["reason"], env["results"]),
                         (p.EXIT_OK, "ok", None, []))
        self.assertEqual(err, "")

    def test_the_guard_does_not_advance_to_the_next_tier(self):
        # A body we cannot read is not evidence that the strict tier found nothing, so
        # the relaxed/plain requests must not be spent on an API that is answering garbage.
        p = load_probe()
        calls = []
        def fetch(url, **kw):
            calls.append(url)
            return {}
        with contextlib.redirect_stderr(io.StringIO()):
            p.search_envelope(["x"], source="github", fetch=fetch)
        self.assertEqual(len(calls), 1)

class TestHonestSourceDegradation(unittest.TestCase):
    """A source with no leg at all still degrades honestly (the path hf just left)."""

    def test_an_unknown_source_degrades_with_zero_requests(self):
        p = load_probe()
        calls = []
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code, env = p.search_envelope(["x"], source="gitlab",
                                          fetch=lambda url, **kw: calls.append(url))
        self.assertEqual(code, p.EXIT_DEGRADED)
        self.assertEqual(env["status"], "degraded")
        self.assertEqual(env["sources"], [{"source": "gitlab", "status": "degraded"}])
        self.assertEqual(env["results"], [])
        self.assertEqual(calls, [], "no retriever means no request, and no GitHub substitute")
        self.assertIn("gitlab", err.getvalue())

class TestUnwritableStderrNeverChangesTheContract(unittest.TestCase):
    """Gap 1 (Task 4 re-review): a diagnostic must not be able to break the contract.

    On a host where stderr cannot be written (a closed fd 2, a broken pipe) print() raises
    OSError. The diagnostic is a courtesy; the envelope and the exit code ARE the contract,
    so a degraded answer has to survive a dead stderr instead of becoming a traceback with
    exit 1 — the very outcome the degraded path exists to prevent.
    """

    class _DeadStream(object):
        def write(self, text):
            raise OSError(9, "Bad file descriptor")

        def flush(self):
            raise OSError(9, "Bad file descriptor")

    def _with_dead_stderr(self, fn):
        real = sys.stderr
        sys.stderr = self._DeadStream()
        try:
            return fn()
        finally:
            sys.stderr = real

    def test_a_degraded_search_survives_a_dead_stderr(self):
        p = load_probe()
        def run():
            return p.search_envelope(["x"], source="github",
                                     fetch=lambda url, **kw: {"total_count": 1,
                                                              "items": [None]})
        code, env = self._with_dead_stderr(run)
        self.assertEqual((code, env["status"], env["results"]),
                         (p.EXIT_DEGRADED, "degraded", []))

    def test_a_missing_leg_survives_a_dead_stderr(self):
        p = load_probe()
        code, env = self._with_dead_stderr(
            lambda: p.search_envelope(["x"], source="gitlab", fetch=lambda url, **kw: {}))
        self.assertEqual((code, env["status"]), (p.EXIT_DEGRADED, "degraded"))

    def test_bad_args_still_exit_3_with_a_dead_stderr(self):
        p = load_probe()
        out = io.StringIO()
        def run():
            with contextlib.redirect_stdout(out):
                return p.main(["search", "--terms", ""])
        self.assertEqual(self._with_dead_stderr(run), 3)
        self.assertEqual(out.getvalue(), "")

    def test_a_usage_error_still_exits_3_with_a_dead_stderr(self):
        p = load_probe()
        self.assertEqual(
            self._with_dead_stderr(lambda: p.main(["search", "--terms", "x",
                                                   "--source", "nope"])), 3)

class TestMalformedLegPayloads(unittest.TestCase):
    """Gap 2 (Task 4 re-review): a leg is an outside shape, so a shape bug must degrade.

    `found["tier"]`/`["total"]`/`["results"]` used to be read outside the try. With only
    github_search and _hn_leg producing a payload that hole was invisible; hf is a third
    producer, so a leg returning something unexpected has to degrade with a diagnostic
    instead of escaping main() as a traceback and exit 1.
    """

    def _malformed(self, payload):
        def leg(terms, limit, fetch=None, strict_only=False):
            return payload
        return leg

    def test_a_github_leg_with_an_unexpected_shape_degrades_loudly(self):
        p = load_probe()
        p._source_legs = lambda: {"github": self._malformed({"nope": 1})}
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code, env = p.search_envelope(["x"], source="github",
                                          fetch=lambda url, **kw: {})
        self.assertEqual(code, p.EXIT_DEGRADED)
        self.assertEqual(env["status"], "degraded")
        self.assertEqual(env["results"], [])
        self.assertIn("KeyError", err.getvalue())

    def test_a_malformed_hn_leg_is_a_partial_failure_not_a_crash(self):
        p = load_probe()
        legs = p._source_legs()
        legs["hn"] = self._malformed([])
        p._source_legs = lambda: legs
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code, env = p.search_envelope(["x"], source="auto",
                                          fetch=lambda url, **kw: {"total_count": 0,
                                                                   "items": []})
        self.assertEqual((code, env["status"]), (p.EXIT_OK, "ok"))
        self.assertEqual(env["sources"][-1], {"source": "hn", "status": "failed"})
        self.assertIn("TypeError", err.getvalue())

    def test_a_malformed_hf_leg_is_a_partial_failure_not_a_crash(self):
        p = load_probe()
        legs = p._source_legs()
        legs["hf"] = self._malformed([])
        p._source_legs = lambda: legs
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code, env = p.search_envelope(["local speech to text"], source="auto",
                                          fetch=lambda url, **kw: {"total_count": 0,
                                                                   "items": []})
        self.assertEqual((code, env["status"]), (p.EXIT_OK, "ok"))
        self.assertEqual(env["sources"][-1], {"source": "hf", "status": "failed"})
        self.assertIn("TypeError", err.getvalue())

class TestRejectedHttpStatusIsNotATransportFailure(unittest.TestCase):
    """A delivered-and-rejected 4xx is not a transport condition (controller ruling).

    §6 hands `timeout` / `network` / `rate-limited` different remedies, so a 404 from a typo
    must not borrow `timeout`: sending the user to check their network when the real answer
    is "that repository does not exist" points at the wrong fix. Such a status is an
    internal/unexpected condition — degraded with `reason: null` — while 403/429 stay
    `rate-limited` and 5xx keeps `timeout` (a 504 genuinely is one).
    """

    def _http_error(self, code):
        return urllib.error.HTTPError("https://api.github.com/repos/o/n", code,
                                      "boom", {}, None)

    def test_404_is_an_internal_condition_and_claims_no_reason(self):
        p = load_probe()
        problem = p._classify(self._http_error(404))
        self.assertIsInstance(problem, p.InternalError)
        self.assertIsNone(p._degraded_reason(problem),
                          "a rejected request is not a cause §4.3 can name")

    def test_400_is_an_internal_condition_and_claims_no_reason(self):
        p = load_probe()
        problem = p._classify(self._http_error(400))
        self.assertIsInstance(problem, p.InternalError)
        self.assertIsNone(p._degraded_reason(problem))

    def test_403_and_429_keep_their_rate_limited_label(self):
        p = load_probe()
        for code in (403, 429):
            self.assertIsInstance(p._classify(self._http_error(code)), p.RateLimited, code)

    def test_5xx_keeps_its_timeout_label(self):
        p = load_probe()
        for code in (500, 503, 504):
            self.assertIsInstance(p._classify(self._http_error(code)), p.Timeout, code)

    def test_a_search_404_degrades_with_a_null_reason_and_the_status_on_stderr(self):
        p = load_probe()
        def fetch(url, **kw):
            raise self._http_error(404)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code, env = p.search_envelope(["x"], source="github", fetch=fetch)
        self.assertEqual((code, env["status"], env["reason"], env["results"]),
                         (p.EXIT_DEGRADED, "degraded", None, []))
        self.assertIn("404", err.getvalue())

    def test_a_rejected_status_is_not_retried(self):
        # Only 403/429 are quota errors worth a retry; a rejection is final.
        p = load_probe()
        calls = []
        def fetch(url, **kw):
            calls.append(url)
            raise self._http_error(400)
        with unittest.mock.patch.object(p.time, "sleep", lambda seconds: None):
            p.search_envelope(["x"], source="github", fetch=fetch)
        self.assertEqual(len(calls), 1)

if __name__ == "__main__":
    unittest.main()
