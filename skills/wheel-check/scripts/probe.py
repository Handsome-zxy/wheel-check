#!/usr/bin/env python3
"""wheel-check data layer: GitHub / HN / HuggingFace retrieval. Hard facts only."""
import argparse
import json
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
    """Any other failure that stopped the request: timeout, 5xx, DNS, bad body."""

def _classify(exc):
    """Map a fetch failure onto the two degraded reasons the envelope can carry.

    Our own exception types pass straight through: an injected `fetch` (tests, future
    sources) raises RateLimited/Timeout directly, and re-classifying those would turn
    a quota error into a generic Timeout and silently lose the retry.
    """
    if isinstance(exc, (RateLimited, Timeout)):
        return exc
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (403, 429):
            return RateLimited(str(exc.code))
        return Timeout("http-%s" % exc.code)
    return Timeout(type(exc).__name__)

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

def _degraded(reason):
    """Whole-operation failure. `reason` is reserved for this, never per-source.

    Per-source trouble (Task 5+) sets that entry's `status`; only a failure that
    stops the operation as a whole may set the envelope's `reason`.
    """
    return {"status": "degraded", "reason": reason,
            "sources": [{"source": "github", "status": "degraded"}],
            "results": []}

def search_envelope(terms, source="auto", limit=8, fetch=urlopen_json, strict_only=False):
    """Run the source legs; return (exit_code, envelope) in the §4.3 shape.

    `status: "ok"` with `results: []` is the ONLY "there is genuinely nothing"
    signal. Every other combination has to be spoken aloud, so a rate limit or a
    dead network returns `degraded` with an empty `results` — never `ok` + `[]`,
    which would make the skill silently skip a precedent that does exist.

    `terms` is the CLEANED list from validate_terms(); `total` in each source entry
    is the API's `total_count`, not `len(results)` — the two legitimately disagree
    because `per_page` caps the returned array (measured: total 5260, 8 returned).

    `source` selects which legs run. Tasks 5/6 add the HN and HuggingFace legs;
    until then only GitHub is wired, so `auto` means GitHub-only.
    """
    retrying = _retrying_fetch(fetch)
    try:
        gh = github_search(terms, limit, fetch=retrying, strict_only=strict_only)
    except Exception as exc:        # noqa: BLE001 - any retrieval failure degrades
        # Classify whatever came out of the retrieval: our own types pass through,
        # everything else (5xx, dead socket, unreadable body) becomes `timeout`. A
        # body we could not read must degrade — reporting it as `ok` + `[]` would be
        # the one lie this contract forbids.
        problem = _classify(exc)
        return EXIT_DEGRADED, _degraded(
            "rate-limited" if isinstance(problem, RateLimited) else "timeout")
    return EXIT_OK, {"status": "ok", "reason": None,
                     "sources": [{"source": "github", "tier": gh["tier"],
                                  "total": gh["total"], "status": "ok"}],
                     "results": gh["results"]}

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
