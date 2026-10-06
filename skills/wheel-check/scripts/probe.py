#!/usr/bin/env python3
"""wheel-check data layer: GitHub / HN / HuggingFace retrieval. Hard facts only."""
import argparse
import json
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta

EXIT_OK = 0
EXIT_DEGRADED = 2
EXIT_BAD_ARGS = 3

RETRY_BACKOFF = 1.0   # seconds between the original request and its one retry (§6)
RETRY_BUDGET = 1      # retries allowed for a WHOLE search: 3 tier requests + 1 retry

UA = {"User-Agent": "wheel-check", "Accept": "application/vnd.github+json"}

def force_utf8():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

def validate_terms(raw):
    terms = [t.strip() for t in (raw or "").split("|")]
    terms = [t for t in terms if t]
    if not terms:
        raise ValueError("--terms must contain at least one non-empty term")
    return terms

def _pushed_floor(months=18):
    """Oldest pushed_at the strict tier accepts, as YYYY-MM-DD."""
    return (date.today() - timedelta(days=30 * months)).isoformat()

def github_tiers():
    """(tier_name, filter_suffix) in fallback order. Each tier costs ONE request."""
    return [
        ("strict", " stars:>50 pushed:>%s fork:false" % _pushed_floor()),
        ("relaxed", " stars:>50 fork:false"),
        ("plain", ""),
    ]

def urlopen_json(url, headers=None, timeout=15):
    """Default `fetch`: one GET, JSON body. Callers may inject their own."""
    req = urllib.request.Request(url, headers=headers or UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)

class RateLimited(Exception):
    """403/429: the GitHub quota is exhausted (unauthenticated: 10 requests/minute)."""

class Timeout(Exception):
    """A request that ran out of time: socket timeout, or an HTTP timeout response."""

class NetworkError(Exception):
    """The request never got through: DNS, refused/reset connection, proxy, no route."""

class InternalError(Exception):
    """An unexpected failure: a probe.py bug, or a response shape we cannot read.

    Kept apart from the transport failures on purpose. The envelope must not offer a
    network-shaped `reason` for a bug — it reports `degraded` with `reason: null` and
    puts the real exception type on stderr (see `_warn_internal`).
    """

    def __init__(self, cause):
        Exception.__init__(self, "%s: %s" % (type(cause).__name__, cause))
        self.cause = cause

def _is_timeout(exc):
    """True for a genuine timeout, in either the 3.8 or the 3.10+ spelling."""
    return isinstance(exc, (socket.timeout, TimeoutError))

def _classify(exc):
    """Map a fetch failure onto the reason vocabulary §4.3 / §6 defines.

    403/429 -> rate-limited, a dead network -> network, a request that timed out ->
    timeout. `timeout` and `network` are kept apart deliberately: §6 gives them
    different remedies, so telling the user "timeout" when DNS never resolved would
    point them at the wrong fix.

    Anything we cannot recognise as a transport failure becomes InternalError rather
    than being relabelled as `timeout`: a bug must not be reported as a network
    condition (the controller's ruling on fix round 2).

    Our own exception types pass straight through: an injected `fetch` (tests, future
    sources) raises them directly, and re-classifying those would turn a quota error
    into a generic Timeout and silently lose the retry.
    """
    if isinstance(exc, (RateLimited, Timeout, NetworkError, InternalError)):
        return exc
    if isinstance(exc, urllib.error.HTTPError):     # also a URLError: check first
        if exc.code in (403, 429):
            return RateLimited(str(exc.code))
        return Timeout("http-%s" % exc.code)
    if isinstance(exc, urllib.error.URLError):
        # urlopen wraps socket-level failures in URLError. A wrapped socket.timeout
        # is what the 15s request timeout looks like; anything else (gaierror,
        # refused, reset, proxy) never reached the server at all.
        if _is_timeout(getattr(exc, "reason", None)):
            return Timeout("socket-timeout")
        return NetworkError(str(getattr(exc, "reason", None) or exc))
    if _is_timeout(exc):
        return Timeout(type(exc).__name__)
    if isinstance(exc, OSError):                    # bare socket/SSL failure
        return NetworkError(type(exc).__name__)
    return InternalError(exc)                       # not a transport failure at all

def _warn_internal(problem):
    """Report an unexpected failure on stderr; stdout stays pure JSON (§4.3).

    Without this line a bug inside probe.py would reach the caller as an unexplained
    `degraded` and nobody would ever notice it. The global constraint sends error text
    to stderr, which is exactly what this is.
    """
    cause = problem.cause
    message = str(cause)
    detail = type(cause).__name__
    if message and len(message) <= 200:             # long tracebacks are unreadable
        detail = "%s: %s" % (detail, message)
    print("error: probe.py unexpected failure: %s (reported as degraded, reason null)"
          % detail, file=sys.stderr)

def _with_retry(fn, attempts=2, backoff=RETRY_BACKOFF):
    """Call fn(); on a rate limit sleep `backoff` and try again, then re-raise.

    Only quota errors are worth retrying — a 5xx or a dead socket is raised as-is
    after being classified. `attempts=2` means the original request plus one retry.
    """
    last = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:          # noqa: BLE001 - classify, then re-raise
            last = _classify(e)
            if isinstance(last, RateLimited) and i + 1 < attempts:
                time.sleep(backoff)
                continue
            raise last
    raise last

def _retrying_fetch(fetch, backoff=RETRY_BACKOFF):
    """Wrap `fetch` with the 403/429 retry, charged to the search as a whole.

    The retry belongs to the REQUEST, not to the tier loop: `github_search` spends one
    request per tier, and a persistent 403 has to cost exactly two requests (original
    + retry) and then raise, so the loop never advances into relaxed/plain against an
    API that is already refusing. Retrying per call instead would let one search cost
    up to six requests; one retry for the whole search keeps the ceiling at three tier
    requests + one retry = four (plan budget: <=3 searches, <=45s).
    """
    budget = {"retries": RETRY_BUDGET}
    def wrapped(url, **kw):
        made = []
        def call():
            made.append(1)
            return fetch(url, **kw)
        try:
            return _with_retry(call, attempts=1 + budget["retries"], backoff=backoff)
        finally:
            if len(made) > 1:           # a retry was really spent, charge it
                budget["retries"] -= 1
    return wrapped

def build_query(terms, suffix):
    """Plain space-joined concept nouns (GitHub ANDs them) plus the tier suffix.

    One request per tier: unauthenticated GitHub search allows only 10/minute, so
    every term is folded into a single query instead of one request per term.

    Unquoted, measured live (2026-10-06). Quoting forces an exact-phrase match
    against name/description and collapsed `test selection` 11 -> 1 and a quoted
    pair -> 0 — a silent skip. GitHub *does* honour a parenthesised OR, but over
    generic concept nouns it degenerates into near-synonym noise (11108), so we
    do not promise a union: multi-term input is space-joined AND and the caller
    should send ONE concept noun per request.
    """
    if isinstance(terms, str):
        raise ValueError(
            "build_query expects a list of terms, got the raw string %r; run "
            "validate_terms() first (a string would be joined per character)" % terms)
    return " ".join(terms) + suffix

def normalize_repo(item):
    """Reduce a GitHub repo payload to the hard facts the caller may render."""
    return {
        "kind": "repo",
        "full_name": item.get("full_name"),
        "url": item.get("html_url"),
        "stars": item.get("stargazers_count"),
        "pushed_at": (item.get("pushed_at") or "")[:10],
        "license": (item.get("license") or {}).get("spdx_id"),
        "archived": bool(item.get("archived")),
        "language": item.get("language"),
        "description": item.get("description"),
    }

def _per_page(limit):
    """Clamp limit for programmatic callers: GitHub accepts per_page 1..100 only.

    Measured: per_page=0 and per_page=101 both return HTTP 200 with the value
    silently normalised (per_page=0 came back with MORE items than requested), so
    an out-of-range value must never reach the API.
    """
    return max(1, min(100, int(limit)))

def github_search(terms, limit, fetch=urlopen_json, strict_only=False):
    """Try the tiers in order; first tier with hits wins.

    Returns {"tier": str, "total": int, "results": [repo_dict]}. "none" means
    every tier tried came back empty. strict_only=True (degraded path) runs the
    strict tier alone.
    """
    tiers = github_tiers()[:1] if strict_only else github_tiers()
    for tier, suffix in tiers:
        url = ("https://api.github.com/search/repositories?q="
               + urllib.parse.quote(build_query(terms, suffix))
               + "&sort=stars&order=desc&per_page=%d" % _per_page(limit))
        data = fetch(url)
        items = data.get("items") or []
        if items:
            return {"tier": tier, "total": data.get("total_count", 0),
                    "results": [normalize_repo(i) for i in items]}
    return {"tier": "none", "total": 0, "results": []}

def _source_legs():
    """source name -> retriever, or None when this build has no retriever for it.

    Task 5 replaces the "hn" None with its retriever and Task 6 does "hf"; this is the
    one place a source is registered, so the honest-degradation path below stops
    firing for a source the moment its retriever exists.
    """
    return {"github": github_search, "hn": None, "hf": None}

def _degraded_reason(problem):
    """Map a classified failure onto the §4.3 `reason` vocabulary.

    Returns None when §4.3 cannot name the cause (an InternalError, or a source this
    build has no retriever for). The envelope still says `degraded` — it just does not
    invent a cause; the real one is on stderr. Claiming `timeout` for a probe.py bug
    would report a false cause to the user.
    """
    for exc_type, reason in ((RateLimited, "rate-limited"),
                             (NetworkError, "network"),
                             (Timeout, "timeout")):
        if isinstance(problem, exc_type):
            return reason
    return None

def _degraded(reason, source="github"):
    """Whole-operation failure. `reason` is reserved for this, never per-source.

    Per-source trouble (Task 5+) sets that entry's `status`; only a failure that
    stops the operation as a whole may set the envelope's `reason`. `source` names the
    leg that failed, so a degraded envelope never blames a source the caller did not
    ask for.
    """
    return {"status": "degraded", "reason": reason,
            "sources": [{"source": source, "status": "degraded"}],
            "results": []}

def search_envelope(terms, source="auto", limit=8, fetch=urlopen_json, strict_only=False):
    """Run the source legs; return (exit_code, envelope) in the §4.3 shape.

    `status: "ok"` with `results: []` is the ONLY "there is genuinely nothing"
    signal. Every other combination has to be spoken aloud, so a rate limit, a dead
    network or a bug returns `degraded` with an empty `results` — never `ok` + `[]`,
    which would make the skill silently skip a precedent that does exist.

    `terms` is the CLEANED list from validate_terms(); `total` in each source entry
    is the API's `total_count`, not `len(results)` — the two legitimately disagree
    because `per_page` caps the returned array (measured: total 5260, 8 returned).

    `source` selects which legs run (`auto` = the default set, today GitHub only).
    A source with no retriever in this build is a whole-operation degradation, never a
    silent answer with another source's data; see `_source_legs`.

    `reason` is None when the cause is outside §4.3's vocabulary (internal error,
    unimplemented source): the envelope is still `degraded`, and stderr carries the
    real exception type. stdout is always pure JSON.
    """
    wanted = "github" if source == "auto" else source
    leg = _source_legs().get(wanted)
    if leg is None:
        print("error: --source %s has no retriever in this build; no query was sent "
              "and GitHub was NOT queried in its place" % wanted, file=sys.stderr)
        return EXIT_DEGRADED, _degraded(None, source=wanted)
    retrying = _retrying_fetch(fetch)
    try:
        found = leg(terms, limit, fetch=retrying, strict_only=strict_only)
    except Exception as exc:        # noqa: BLE001 - any retrieval failure degrades
        # Classify whatever came out of the retrieval: transport failures become
        # rate-limited / network / timeout, anything else is an InternalError that is
        # reported as-is on stderr rather than relabelled. Degrading is mandatory here
        # — reporting a failure as `ok` + `[]` would be the one lie this contract
        # forbids.
        problem = _classify(exc)
        if isinstance(problem, InternalError):
            _warn_internal(problem)
        return EXIT_DEGRADED, _degraded(_degraded_reason(problem), source=wanted)
    return EXIT_OK, {"status": "ok", "reason": None,
                     "sources": [{"source": wanted, "tier": found["tier"],
                                  "total": found["total"], "status": "ok"}],
                     "results": found["results"]}

class _ArgParser(argparse.ArgumentParser):
    """ArgumentParser whose usage errors exit with EXIT_BAD_ARGS, not argparse's 2.

    argparse's default error() calls sys.exit(2), and 2 means EXIT_DEGRADED in
    this project: a malformed call would be reported as a retrieval failure.
    """

    def error(self, message):
        self.print_usage(sys.stderr)
        print("%s: error: %s" % (self.prog, message), file=sys.stderr)
        raise SystemExit(EXIT_BAD_ARGS)

def _limit_arg(raw):
    """argparse `type` for --limit: an out-of-range value is a bad-args call (exit 3)."""
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError("--limit must be an integer, got %r" % raw)
    if not 1 <= value <= 100:
        raise argparse.ArgumentTypeError("--limit must be between 1 and 100, got %d" % value)
    return value

def build_parser():
    ap = _ArgParser(prog="probe.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("health")
    s = sub.add_parser("search")
    s.add_argument("--terms", required=True)
    s.add_argument("--source", default="auto", choices=["auto", "github", "hn", "hf"])
    s.add_argument("--strict", action="store_true")
    s.add_argument("--limit", type=_limit_arg, default=8)
    r = sub.add_parser("repo")
    r.add_argument("full_name")
    return ap

def main(argv=None):
    force_utf8()
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as e:
        # Usage errors raise SystemExit(EXIT_BAD_ARGS); -h raises SystemExit(0).
        return e.code if isinstance(e.code, int) else EXIT_BAD_ARGS
    if args.cmd == "search":
        try:
            terms = validate_terms(args.terms)
        except ValueError as e:
            print("error: %s" % e, file=sys.stderr)
            return EXIT_BAD_ARGS
        # Hand the search path the CLEANED list: raw `--terms " a |  | b | "` must
        # never reach build_query with empty groups or surrounding whitespace. Never
        # validate_terms(args.terms) a second time — it splits on "|" and would raise
        # AttributeError on the list it is handed.
        args.terms = terms
        code, env = search_envelope(args.terms, source=args.source, limit=args.limit,
                                    strict_only=args.strict)
        print(json.dumps(env, ensure_ascii=False))
        return code
    return EXIT_OK

if __name__ == "__main__":
    sys.exit(main())
