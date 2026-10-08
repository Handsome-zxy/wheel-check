#!/usr/bin/env python3
"""wheel-check data layer: GitHub / HN / HuggingFace retrieval. Hard facts only."""
import argparse
import json
import os
import socket
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta

EXIT_OK = 0
EXIT_DEGRADED = 2
EXIT_BAD_ARGS = 3

RETRY_BACKOFF = 1.0   # the fallback wait when a rate limit names no reset (§6, Item 6a)
RETRY_BUDGET = 1      # retries allowed for a WHOLE `search` INVOCATION (Item 2): the
                      # invocation ceiling is 3 tier requests + 1 retry = 4, and the
                      # ladder stops at the first tier that returns hits (typically 1-2)
SEARCH_BUDGET_SECONDS = 45.0   # §4.2's whole-invocation time budget, and therefore the
                               # longest wait a rate-limited retry may ever serve: GitHub's
                               # search window is 60s, so a reset further out than this is
                               # not worth a second request (Item 6a)
REQUEST_TIMEOUT = 15           # seconds allowed per HTTP request (urlopen_json's default)
INVOCATION_REQUEST_CEILING = 4 # 3 filter tiers + 1 retry: the most one `search` invocation
                               # ever sends. The budget above caps the WAIT, not elapsed time,
                               # so the documented hard ceiling for one invocation is
                               # 4 * 15 + 45 = 105s (the typical rate-limit path is
                               # 1 request + <=45s wait + 1 retry = ~75s), and
                               # `test_the_worst_case_wall_clock_is_the_documented_105_seconds`
                               # is what keeps this comment and the docs honest.

UA = {"User-Agent": "wheel-check", "Accept": "application/vnd.github+json"}

GITHUB_HOST = "api.github.com"                    # the ONE host a token may travel to
GITHUB_API = "https://" + GITHUB_HOST
GITHUB_TOKEN_ENV = ("GITHUB_TOKEN", "GH_TOKEN")   # GH_TOKEN is the name the gh CLI uses

def github_token(environ=None):
    """The GitHub credential from the environment, or None when none is set (§6).

    `GITHUB_TOKEN` first, then `GH_TOKEN`: the second is the name the gh CLI already
    exports, so an authenticated shell works without exporting anything twice. A blank
    value counts as absent. The value is never logged, echoed, or put in a URL —
    `request_headers` is its only reader and all it does is add one request header.
    """
    env = os.environ if environ is None else environ
    for name in GITHUB_TOKEN_ENV:
        value = (env.get(name) or "").strip()
        if value:
            return value
    return None

def _is_github_api(url):
    """True only for an api.github.com URL — an exact host compare, not a prefix test."""
    return urllib.parse.urlsplit(url).netloc.lower() == GITHUB_HOST

def request_headers(url, environ=None):
    """Headers for one GET: the UA, plus GitHub's bearer token when we have one (§6).

    Unauthenticated GitHub search allows 10 requests/minute and 30 with a token (§2.3).
    The credential is OPTIONAL — without one this returns exactly the headers this file
    has always sent — and it goes to `api.github.com` requests ONLY: Algolia and the
    Hugging Face Hub are keyless third parties, and a GitHub credential has no business
    travelling to them (§5.5 keeps the sources apart).
    """
    headers = dict(UA)
    token = github_token(environ)
    if token and _is_github_api(url):
        headers["Authorization"] = "Bearer " + token
    return headers

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

def _same_host(first, second):
    """True when two URLs share a netloc — host AND port, compared case-insensitively.

    The port is part of the comparison on purpose: `127.0.0.1:8000 -> 127.0.0.1:8001` is a
    different origin by every browser's rule, and the loopback test can prove exactly that
    case. `api.github.com -> api.github.com:8443` is therefore cross-host too.
    """
    return (urllib.parse.urlsplit(first).netloc.lower()
            == urllib.parse.urlsplit(second).netloc.lower())

class _SameHostAuthRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but never carry `Authorization` off the original netloc (Item 6b).

    CPython's `HTTPRedirectHandler.redirect_request` rebuilds the outgoing request from
    `req.headers` minus the content headers — and keeps `Authorization` (verified on 3.13;
    every earlier 3.x behaves the same). A GitHub response that redirected elsewhere would
    therefore hand the `Bearer` token to that host. This handler keeps the header while the
    redirect stays inside the netloc the credential was issued for, and drops it — from
    both header stores urllib sends — the moment the redirect leaves.

    A cross-host redirect is still FOLLOWED, unauthenticated. Returning `None` here would
    abort the redirect and turn a redirect into a failure, which is a different behaviour
    than the one being fixed.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = urllib.request.HTTPRedirectHandler.redirect_request(
            self, req, fp, code, msg, headers, newurl)
        if new is None or _same_host(req.full_url, new.full_url):
            return new
        for store in (new.headers, new.unredirected_hdrs):
            for name in [key for key in store if key.lower() == "authorization"]:
                del store[name]
        return new

def _carries_authorization(req):
    """True when this request has a credential the guard exists to protect."""
    names = list(req.headers) + list(getattr(req, "unredirected_hdrs", {}))
    return any(name.lower() == "authorization" for name in names)

_GUARDED_OPENER = None

def _guarded_opener():
    """The opener for credential-bearing requests: one redirect guard, built once.

    `build_opener` skips its own `HTTPRedirectHandler` when handed an instance of a
    subclass, so this opener follows redirects through `_SameHostAuthRedirect` and carries
    every other default handler unchanged.
    """
    global _GUARDED_OPENER
    if _GUARDED_OPENER is None:
        _GUARDED_OPENER = urllib.request.build_opener(_SameHostAuthRedirect())
    return _GUARDED_OPENER

def _open(req, timeout):
    """Send one prepared request: the guarded opener when a credential is attached (6b).

    `request_headers` attaches `Authorization` to `api.github.com` requests only (§6), so
    in this build "carries a credential" and "is a GitHub request" are the same set, and a
    request without a credential is sent exactly as it always was — `urllib.request.urlopen`,
    the seam every transport test in the suite patches. The guard keys on the credential
    rather than on the host so that it also covers a caller's own headers and any future
    authenticated source; a host test would miss both.
    """
    if _carries_authorization(req):
        return _guarded_opener().open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)

def urlopen_json(url, headers=None, timeout=REQUEST_TIMEOUT):
    """Default `fetch`: one GET, JSON body. Callers may inject their own.

    `headers=None` means this file's own headers, which carry `GITHUB_TOKEN` when the
    environment sets one (see `request_headers`). A caller passing its own headers owns
    the request completely, credential included — `_open` is what keeps that credential
    from following a redirect to another host (Item 6b).
    """
    req = urllib.request.Request(url, headers=headers or request_headers(url))
    with _open(req, timeout) as r:
        return json.load(r)

class RateLimited(Exception):
    """403/429: the GitHub quota is exhausted (unauthenticated: 10 requests/minute).

    Carries `reset` — the epoch second the window reopens — whenever the response said so,
    which is what turns the retry from a blind 1.0s re-confirmation into a decision
    (Item 6a): inside the budget we wait for the reset, beyond it we do not retry at all.
    A rate limit with no reset in hand keeps the old fixed backoff.
    """

    def __init__(self, message="", reset=None):
        Exception.__init__(self, message)
        self.reset = reset

def _rate_limit_reset(exc):
    """The epoch second the quota window reopens, read off the 403/429 response itself.

    GitHub sends `X-RateLimit-Reset` as an epoch second on a quota rejection, so the reset
    is already in hand and reading it costs no request. `Retry-After` is deliberately NOT
    consulted: it is a delay in seconds, not a timestamp, and adding the two together would
    invent a reset time. Anything unreadable is `None` — "no reset in hand", never zero.
    """
    headers = getattr(exc, "headers", None)
    raw = headers.get("X-RateLimit-Reset") if hasattr(headers, "get") else None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None

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

    A 4xx that is not 403/429 (404, 400, 422 …) is neither: the request WAS delivered and
    the server rejected it, so calling it `timeout` would send the caller to check a
    network that is working (controller ruling). It becomes an InternalError — an
    unexpected condition with no §4.3 name — and the raw status travels in the cause for
    the caller's diagnostic. 5xx keeps `timeout`: a 504 genuinely is one.

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
            return RateLimited(str(exc.code), reset=_rate_limit_reset(exc))
        if 400 <= exc.code < 500:
            return InternalError(exc)               # delivered and rejected, not transport
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

def _warn(message):
    """Write one diagnostic line to stderr — never letting that write change the outcome.

    A diagnostic exists so a degraded answer is diagnosable, but on a host where stderr
    is unwritable (a closed fd 2, a broken pipe) print() raises OSError: the carefully
    preserved `degraded` + exit 2 would become a traceback and exit 1, which is exactly
    the outcome the degraded path exists to prevent. The envelope and the exit code are
    the contract; stderr is a courtesy, so a failed write here is swallowed on purpose.
    """
    try:
        print(message, file=sys.stderr)
    except Exception:           # noqa: BLE001 - a diagnostic is never load-bearing
        pass

def _warn_internal(problem, outcome="reported as degraded, reason null"):
    """Report an unexpected failure on stderr; stdout stays pure JSON (§4.3).

    Without this line a bug inside probe.py would reach the caller as an unexplained
    `degraded` and nobody would ever notice it. The global constraint sends error text
    to stderr, which is exactly what this is. `outcome` names what the envelope did
    about it, so the same diagnostic stays truthful for the HN supplement, whose
    failure leaves the envelope `ok` with that source marked `failed`.
    """
    cause = problem.cause
    message = str(cause)
    detail = type(cause).__name__
    if message and len(message) <= 200:             # long tracebacks are unreadable
        detail = "%s: %s" % (detail, message)
    _warn("error: probe.py unexpected failure: %s (%s)" % (detail, outcome))

def _warn_supplement_failed(source, problem):
    """One stderr line for ANY failure of a supplement, whatever the cause (§4.3 v2.2).

    A supplement is consulted exactly when the primary source had nothing to show, so
    its failure always lands on the one envelope that also means "genuinely nothing" —
    `ok` + `results: []`. stdout cannot carry the difference (`reason` is reserved for a
    failure of the whole operation) and the `sources` entry alone is easy to miss, so
    stderr is the channel that keeps an empty answer from being read as an authoritative
    "nothing exists" — the Testrim case HN exists for. An InternalError keeps its own
    diagnostic, where the exception type is the actionable part.
    """
    if isinstance(problem, InternalError):
        _warn_internal(problem, "%s failed; the envelope stays ok" % source)
        return
    _warn("error: %s supplement failed (%s); the empty result is not evidence of "
          "absence, and sources marks %s as failed"
          % (source, _degraded_reason(problem), source))

def _retry_wait(problem, backoff, now, budget=SEARCH_BUDGET_SECONDS):
    """How long to wait before retrying this rate limit; `None` means: do NOT retry.

    GitHub's search window is 60s while this invocation's whole budget is 45s, so the old
    blind 1.0s retry mostly re-confirmed the 403 and spent one of the 10 requests doing it.
    The response's own reset decides instead:

    - **inside the budget** -> sleep exactly until the window reopens (capped at the
      budget), which is the one retry that can actually succeed;
    - **beyond the budget** -> no retry at all: the caller gets `degraded` +
      `rate-limited` one request cheaper, and stderr names the reset.

    With no reset in hand the pre-existing fixed `backoff` stands: a rate limit whose window
    we cannot read is still worth one cheap confirmation (§6). A window that has already
    reopened waits 0, never a negative number.
    """
    reset = getattr(problem, "reset", None)
    if reset is None:
        return backoff
    delay = reset - now
    if delay > budget:
        return None
    return max(0.0, min(delay, budget))

def _warn_retry_abandoned(problem, now):
    """One stderr line when the reset is too far away to be worth a second request.

    The envelope already says `degraded` / `rate-limited`; what it cannot say is WHEN the
    window reopens, because `reason` is a fixed vocabulary with no room for a timestamp.
    This is what makes the refused retry actionable instead of mysterious.
    """
    reset = problem.reset
    delay = max(0, int(reset - now))
    _warn("error: GitHub quota is rate limited and resets at %s (%ds away), beyond the "
          "%ds budget; not retrying (a retry would only spend another request; set "
          "GITHUB_TOKEN for 30 requests/minute)"
          % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(reset)), delay,
             int(SEARCH_BUDGET_SECONDS)))

def _with_retry(fn, attempts=2, backoff=RETRY_BACKOFF, clock=None, sleep=None):
    """Call fn(); on a rate limit wait for the right moment and try again, then re-raise.

    Only quota errors are worth retrying — a 5xx or a dead socket is raised as-is
    after being classified. `attempts=2` means the original request plus one retry.

    The wait is `_retry_wait`'s decision: until the reset when the response named one inside
    the budget, the fixed `backoff` when it did not, and no retry at all when the reset is
    further out than the budget. `clock`/`sleep` are injectable so a test can prove the
    arithmetic without ever serving the wait (Item 6a).
    """
    now_fn = time.time if clock is None else clock
    sleep_fn = time.sleep if sleep is None else sleep
    last = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:          # noqa: BLE001 - classify, then re-raise
            last = _classify(e)
            if isinstance(last, RateLimited) and i + 1 < attempts:
                wait = _retry_wait(last, backoff, now_fn())
                if wait is None:
                    _warn_retry_abandoned(last, now_fn())
                    raise last
                sleep_fn(wait)
                continue
            raise last
    raise last

def _retrying_fetch(fetch, backoff=RETRY_BACKOFF, clock=None, sleep=None):
    """Wrap `fetch` with the 403/429 retry, charged to the search as a whole.

    The retry belongs to the REQUEST, not to the tier loop: `github_search` spends one
    request per tier, and a persistent 403 has to cost exactly two requests (original
    + retry) and then raise, so the loop never advances into relaxed/plain against an
    API that is already refusing. Retrying per call instead would let one search cost
    up to six requests; one retry for the whole search keeps the ceiling at three tier
    requests + one retry = four. The plan's budget is stated in INVOCATIONS, not requests
    (§4.2 for the caller, Item 2): <=3 `probe.py search` calls and <=45s in total, which is
    what makes four requests per call fit inside an unauthenticated 10/minute window.

    `clock`/`sleep` reach `_with_retry` unchanged: they exist so a caller (a test, an
    embedding program) owns the wait without patching the `time` module.
    """
    budget = {"retries": RETRY_BUDGET}
    def wrapped(url, **kw):
        made = []
        def call():
            made.append(1)
            return fetch(url, **kw)
        try:
            return _with_retry(call, attempts=1 + budget["retries"], backoff=backoff,
                               clock=clock, sleep=sleep)
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

UNTRUSTED_TEXT_MAX = 200        # §4.3: how much third-party free text may reach the envelope
TRUNCATION_MARK = "\u2026"      # a truncated field must LOOK truncated

# What counts as hidden is decided by Unicode's own categories, not by a hand-copied range
# list (review fix). The first version enumerated ranges and missed 144 code points —
# including U+061C ARABIC LETTER MARK (itself a bidi control), U+00AD, U+180E, the invisible
# operators, the interlinear annotation block and the whole deprecated TAG block
# (U+E0001, U+E0020–U+E007F), which is precisely the shape text-hiding takes. `unicodedata`
# is stdlib, so the rule is version-proof and 3.8-safe, and a code point the interpreter
# knows about cannot be forgotten the way a hand-written range can.
#
# Two documented exceptions:
#   * the C0 whitespace controls are SEPARATORS, not glue: "a\nb" must stay two words, so
#     they become a space before this check (see `sanitize_text`);
#   * a handful of Cf code points are genuinely VISIBLE (prepended concatenation marks such
#     as the Arabic number signs). They render a glyph, and removing them would alter text a
#     reader can see — which this function must never do.
_VISIBLE_FORMAT = frozenset((
    0x0600, 0x0601, 0x0602, 0x0603, 0x0604, 0x0605,   # Arabic number signs
    0x06DD, 0x070F, 0x0890, 0x0891, 0x08E2,           # end of ayah, Syriac/Arabic marks
    0x110BD, 0x110CD,                                 # Kaithi number signs
))
_WHITESPACE_CONTROLS = "\t\n\r\v\f"

def _is_invisible(ch):
    """True for a control/format character a human reader cannot see."""
    if unicodedata.category(ch) not in ("Cc", "Cf"):
        return False
    return ord(ch) not in _VISIBLE_FORMAT

def sanitize_text(value):
    """Third-party free text, made safe to RENDER while staying a faithful quote (§4.3).

    `description`, `title` and `model_id` are written by strangers and are the only
    channel by which third-party content reaches the model's decision path. This makes
    three guarantees about what the envelope carries:

    1. **Visible.** Every Cc/Cf character the interpreter knows about is dropped (bidi
       controls and isolates, zero-width characters, invisible operators, tag characters,
       the BOM, DEL/C1 …) except the visible prepended concatenation marks listed in
       `_VISIBLE_FORMAT`. Pairing a benign-looking sentence with invisible text is the cheap
       way to show the human one thing and the model another; a quote the reader cannot
       fully see is not evidence. Newlines and tabs become a space so that "a\\nb" stays two
       words rather than fusing into one.
    2. **Bounded.** At most `UNTRUSTED_TEXT_MAX` characters, and a cut field ends with
       `TRUNCATION_MARK` so it is visibly cut instead of silently amputated.
    3. **Uncensored.** Words are never removed or reworded — an instruction aimed at the
       agent is still quoted in full (up to the cap) and reported to the user AS CONTENT.
       Sanitizing is not judging; that is the contract layer's `SKILL.md` rule.

    Only free text goes through here. URLs, numbers, counts, dates, license ids and
    owner/name are facts and are passed through exactly as the source reported them. The
    function is the identity on well-formed third-party text.

    `None` for anything that is not a string, and for a string with no visible content
    left — an empty field is "nothing here", the same `None` the other absent fields use,
    never `""` that a caller could render as a fact.
    """
    if not isinstance(value, str):
        return None
    kept = []
    for ch in value:
        if ch in _WHITESPACE_CONTROLS:
            kept.append(" ")
        elif not _is_invisible(ch):
            kept.append(ch)
    text = " ".join("".join(kept).split())
    if not text:
        return None
    if len(text) > UNTRUSTED_TEXT_MAX:
        text = text[:UNTRUSTED_TEXT_MAX - 1].rstrip() + TRUNCATION_MARK
    return text

def normalize_repo(item):
    """Reduce a GitHub repo payload to the hard facts the caller may render.

    `pushed_at` is `None` when the payload does not carry one — never `""`. An absent
    field has no value, and `""` is a value a model can render as a fact (§4.1 forbids
    that); `None` is the same "there is nothing here" the other absent fields use.
    Note it is the last PUSH, not the last commit: a tag or a push to a stale branch
    moves it forward, which is why the callers' evidence label says 最后一次推送.
    """
    pushed = item.get("pushed_at")
    return {
        "kind": "repo",
        "full_name": item.get("full_name"),
        "url": item.get("html_url"),
        "stars": item.get("stargazers_count"),
        "pushed_at": pushed[:10] if pushed else None,
        "license": (item.get("license") or {}).get("spdx_id"),
        "archived": bool(item.get("archived")),
        "language": item.get("language"),
        "description": sanitize_text(item.get("description")),
    }

def _per_page(limit):
    """Clamp limit for programmatic callers: both sources take 1..100 (or nothing).

    GitHub accepts per_page 1..100 only. Measured: per_page=0 and per_page=101 both
    return HTTP 200 with the value silently normalised (per_page=0 came back with MORE
    items than requested), so an out-of-range value must never reach the API. HN reuses
    the same bound for `hitsPerPage`: an out-of-range value is rejected rather than
    normalised, and a rejected request is a failed HN search caused by nothing but the
    number we sent — the CLI is already bounded at 1..100, this covers callers of
    `hn_search()`/`search_envelope()`.
    """
    return max(1, min(100, int(limit)))

def _github_page(data):
    """Read one /search/repositories body, or refuse to read it as "no results".

    A 200 whose body is not a GitHub search response (`{}`, `{"message": ...}`, a partial
    object) used to fall through the tier loop as "zero results", so a broken or ambiguous
    API answer was indistinguishable from a genuine "nothing exists" — `ok` + `[]`, the one
    signal §4.3 lets the skill read as "stay silent" (Task 4 review).

    A contradictory body (matches counted but no items handed over) is refused for the same
    reason: the API's own count would be denied by a report that says nothing exists.

    The refusal is an InternalError, which is this file's existing class for "a response
    shape we cannot read"; search_envelope degrades on it and puts the detail on stderr.
    A genuine `{"total_count": 0, "items": []}` passes through untouched.

    Only a MAPPING is inspected. A non-mapping body already fails loudly on its own first
    read (it cannot be smoothed into "no results"), so it is left to raise there; the
    dangerous body is the well-formed mapping whose missing keys `.get()` would quietly
    turn into an empty hit list.
    """
    if not isinstance(data, dict):
        return data
    if "total_count" not in data or "items" not in data:
        raise InternalError(ValueError(
            "not a GitHub search response (expected total_count + items): %r" % (data,)))
    items = data["items"]
    if not isinstance(items, list):
        raise InternalError(ValueError(
            "GitHub search response carries a non-list items field: %r" % (items,)))
    total = data["total_count"]
    if isinstance(total, int) and total > 0 and not items:
        raise InternalError(ValueError(
            "GitHub search response contradicts itself: total_count=%d with no items"
            % total))
    return data

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
        data = _github_page(fetch(url))
        items = data.get("items") or []
        if items:
            return {"tier": tier, "total": data.get("total_count", 0),
                    "results": [normalize_repo(i) for i in items]}
    return {"tier": "none", "total": 0, "results": []}

def _repo_page(data):
    """Read one /repos/{owner}/{repo} body, or refuse to read it as a repository.

    `normalize_repo` maps every absent field to `None` by design — right for a real payload,
    catastrophic for a body that is not a repository at all: `{}` or `{"message": ...}`
    would become an `ok` envelope holding a `kind: "repo"` entry with every fact null, and
    §4.1 forbids the caller ever rendering those nulls as facts (绝不编造 star / 日期). This
    is the same defect class `_github_page` closes on the search path, applied to the
    single-repo path the reviewer reproduced.

    A repository response is recognisable by `full_name`. Without a non-empty one the body
    is not a repository, so the lookup degrades with a diagnostic instead of inventing one.
    """
    if (not isinstance(data, dict) or not isinstance(data.get("full_name"), str)
            or not data["full_name"].strip()):
        raise InternalError(ValueError(
            "not a GitHub repository response (expected full_name): %r" % (data,)))
    return data

def repo_detail(full_name, fetch=urlopen_json):
    """Hard facts for ONE repository (`kind: "repo"`), §4.2's `repo <owner/name>`.

    §4.3 forces the caller to backfill an HN hit: a discussion entry carries a title and
    a link and nothing else, and HN's points must never stand in for repository maturity.
    This is the lookup that replaces that guess with a fact, through the same
    `normalize_repo` the search path uses so both shapes stay identical — and through
    `_repo_page` first, so a 200 that is not a repository never becomes a null-filled fact.

    `full_name` is validated HERE too, not only in `main()` (Item 3): this is the
    programmatic entry point, and without the check a caller could send a request for a
    name that cannot name a repository and then read the API's rejection as a retrieval
    outcome. The CLI keeps its own check, so a bad name is still exit 3 with no envelope;
    a library caller gets the clear `ValueError` `build_query` already raises. Nothing is
    fetched on the rejecting path.
    """
    if not valid_full_name(full_name):
        raise ValueError(
            "repo_detail needs a full_name of the form owner/name, got %r" % (full_name,))
    return normalize_repo(_repo_page(fetch(GITHUB_API + "/repos/" + full_name)))

HN_ENDPOINT = "https://hn.algolia.com/api/v1/search"
HN_ITEM = "https://news.ycombinator.com/item?id=%s"

def _hn_url(terms, limit):
    """Algolia's keyless story search: one request, no quota (§2.2b).

    `tags=story` keeps the recall on submissions — a comment hit is not a project.
    Measured win over GitHub (§2.2b): `incremental test selection` returns total=0 on
    GitHub's filtered index and surfaces `Show HN: Testrim` here, because HN titles are
    natural-language concepts while GitHub only indexes repo descriptions.
    """
    return (HN_ENDPOINT + "?query=" + urllib.parse.quote(" ".join(terms))
            + "&tags=story&hitsPerPage=%d" % _per_page(limit))

def _hn_entry(hit):
    """One Algolia hit -> a `discussion` entry (§4.3).

    HN supplies a title, points, a creation date, the item URL and an external link —
    and nothing else. No stars, no license, no archived flag: borrowing a repo-shaped
    field here would present HN's popularity as repository maturity (§4.3, §9). A hit
    without an objectID has no item URL at all; `None` beats `?id=None`.
    """
    oid = hit.get("objectID")
    return {
        "kind": "discussion",
        "title": sanitize_text(hit.get("title")),
        "url": (HN_ITEM % oid) if oid else None,
        "points": hit.get("points"),
        "created_at": (hit.get("created_at") or "")[:10],
        "external_url": hit.get("url"),
    }

def _hn_hits(terms, limit, fetch):
    """(total, [entry]) for `terms`; the count travels separately from the entries.

    `total` is the source's own match count, never `len(results)`: Algolia reports
    `nbHits` for the whole result set while `hitsPerPage` caps what comes back, exactly
    like GitHub's `total_count` against `per_page`.

    The body goes through `_hn_page` FIRST — the same check `health` runs on the same
    endpoint — and only then is `hits` read as a required key. Reading it with
    `data.get("hits") or []` made a 200 that is not an Algolia story response
    indistinguishable from a genuine empty result, and because HN is consulted exactly
    when GitHub returned nothing, every such body landed on `ok` + `results: []`: the one
    envelope §4.3 declares authoritative for "真的没有", with no stderr line to tell them
    apart. Now the unreadable body raises, the caller classifies it, and the supplement
    (or the explicitly requested source) says so on stderr instead of going quiet.
    """
    data = _hn_page(fetch(_hn_url(terms, limit)))
    hits = data["hits"]
    total = data.get("nbHits")
    return (len(hits) if total is None else total), [_hn_entry(h) for h in hits]

def hn_search(terms, limit, fetch=urlopen_json):
    """Discussion entries for `terms`, in Algolia's relevance order (Task 5 interface)."""
    return _hn_hits(terms, limit, fetch)[1]

def _hn_leg(terms, limit, fetch=urlopen_json, strict_only=False):
    """`hn_search` in the leg shape the source dispatch expects: {"tier","total","results"}.

    HN has no filter ladder, so the tier is always `plain` (§4.3's HN example) and
    `--strict` — a GitHub filter concept — is deliberately ignored.
    """
    total, results = _hn_hits(terms, limit, fetch)
    return {"tier": "plain", "total": total, "results": results}

AI_TERMS = {"ai", "llm", "embedding", "diffusion", "speech", "transcribe", "whisper",
            "fine-tune", "finetune", "rag"}
HF_ENDPOINT = "https://huggingface.co/api/models"
HF_MODEL = "https://huggingface.co/%s"

def is_ai_requirement(terms):
    """True when the requirement carries an AI/ML concept noun — §5.5's gate for HF.

    HF is a CONDITIONAL source: §2.2b measured the Hub as noise for anything that is not
    an AI/ML need, so only an AI-shaped requirement is allowed to reach it. The match is
    word-wise and case-insensitive: `chair` contains the letters of `ai` and must not turn
    a generic requirement into a model search.

    The set is deliberately NARROW (Item 4). Every member names an AI/ML concept that a
    requirement either has or does not have. `model`, `vision` and `agent` were dropped:
    they are ordinary English words, and the measured cost of keeping them was a
    speech-transcription eval run whose list came back with speech-emotion-recognition
    models appended. There is deliberately NO "two terms must match" rule either: that
    would make this conditional source miss a legitimate AI requirement, and an explicit
    `--source hf` bypasses the gate entirely.
    """
    words = set()
    for t in terms:
        words.update(w.strip(".,()[]").lower() for w in t.split())
    return bool(words & AI_TERMS)

def _hf_url(terms, limit):
    """The Hub's keyless model search, most-downloaded first (§2.2b).

    `sort=downloads&direction=-1` is what makes the answer usable: the Hub has no stars
    and no license, and its relevance order puts 4-download toys at the top. `limit` goes
    through the same `_per_page` clamp as GitHub and HN: out-of-range values are only
    reachable programmatically today, and one source silently accepting what the other two
    refuse is how a bound stops being a bound.
    """
    return (HF_ENDPOINT + "?search=" + urllib.parse.quote(" ".join(terms))
            + "&limit=%d&sort=downloads&direction=-1" % _per_page(limit))

def _hf_entry(model):
    """One Hub model -> a `model` entry (§4.3): model_id / pipeline_tag / downloads / likes / url.

    Models are not code libraries (§5.5), so no repo-shaped field is borrowed here: doing
    so would present Hub downloads as repository maturity, the confusion §9 warns about.
    A model with no id gets no URL at all — `None` beats the fabricated
    "huggingface.co/None" a straight string format would hand the caller, the same rule
    `_hn_entry` already follows for a hit without an objectID.

    `pipeline_tag` (Item 5) is the Hub's own label for what the model DOES —
    `automatic-speech-recognition`, `text-generation` — so a candidate becomes decidable by
    use rather than by download count, which is the number §9 warns against reading as
    maturity. It is reported as the Hub wrote it, and `None` when the body has none (or has
    a non-string there), never `""`.

    `model_id` and `pipeline_tag` are third-party text and go through `sanitize_text`
    (`title`/`description` precedent). The URL is derived from the SANITIZED id on purpose:
    a link that disagreed with the id rendered next to it would be a link the caller cannot
    check.
    """
    model_id = sanitize_text(model.get("modelId") or model.get("id"))
    return {"kind": "model",
            "model_id": model_id,
            "pipeline_tag": sanitize_text(model.get("pipeline_tag")),
            "downloads": model.get("downloads"),
            "likes": model.get("likes"),
            "url": (HF_MODEL % model_id) if model_id else None}

def _hf_models(body):
    """The Hub body `hf_search` consumes: a bare array of models, and nothing else.

    One shape check shared by the retriever and by `health`'s liveness reading, so the two
    cannot disagree about what "the Hub answered" means.
    """
    if not isinstance(body, list):
        raise InternalError(ValueError(
            "Hugging Face model search did not return a list: %r" % (body,)))
    return body

def hf_search(terms, limit, fetch=urlopen_json):
    """Model entries for `terms`, most-downloaded first (Task 6 interface).

    A body that is not the bare array this endpoint documents raises instead of being
    smoothed into an empty hit list — the rule `_hn_hits` already follows. "The Hub was
    asked and had nothing" and "the Hub's answer was unreadable" must never collapse into
    the same envelope, because the first one is allowed to mean "nothing exists".
    """
    return [_hf_entry(m) for m in _hf_models(fetch(_hf_url(terms, limit)))]

def _hf_leg(terms, limit, fetch=urlopen_json, strict_only=False):
    """`hf_search` in the leg shape the source dispatch expects: {"tier","total","results"}.

    The Hub has no filter ladder, so the tier is always `plain` (§4.3's HN precedent) and
    `--strict` — a GitHub filter concept — is deliberately ignored.

    `total` IS `len(results)`, and that is the one place it differs from GitHub's
    `total_count` and Algolia's `nbHits`: this endpoint answers with a bare array and
    exposes no match count at all, so there is no honest source-side number to pass through
    and inventing one would be a made-up figure. A caller must therefore read an `hf`
    entry's `total` as "the Hub returned N models" — never as "the Hub has exactly N".
    """
    results = hf_search(terms, limit, fetch)
    return {"tier": "plain", "total": len(results), "results": results}

def _source_legs():
    """source name -> retriever, or None when this build has no retriever for it.

    Task 5 registered "hn" (§5.5's supplement) and Task 6 registers "hf" (the AI-only
    conditional source); this is the one place a source is registered, so the
    honest-degradation path below stops firing for a source the moment its retriever
    exists. Every leg takes `(terms, limit, fetch=..., strict_only=...)` and returns a
    `{"tier", "total", "results"}` payload.
    """
    return {"github": github_search, "hn": _hn_leg, "hf": _hf_leg}

def _leg_payload(source, found):
    """Validate one leg's payload and return its (source_entry, results).

    A leg is an outside shape: `github_search`, `_hn_leg`, `_hf_leg`, a test double, or a
    source added later. Reading `found["tier"]`/`["total"]`/`["results"]` inside the try
    was Gap 2's first half; this is the second, because those reads still let a non-list
    `results` through. That poisons the append below (`list + str` raises a TypeError no
    `except` covers, so `main()` exits 1 with a traceback) or ships a non-array `results`
    in a single-source envelope. Both violate §4.3, so the payload is checked before use
    and a bad one degrades loudly instead.
    """
    entry = {"source": source, "tier": found["tier"], "total": found["total"],
             "status": "ok"}
    results = found["results"]
    if not isinstance(results, list):
        raise InternalError(ValueError(
            "%s leg carried a non-list results field: %r" % (source, results)))
    return entry, results

def _degraded_reason(problem):
    """Map a classified failure onto the §4.3 `reason` vocabulary.

    THE RULE for `reason`: it names a cause the CALLER CAN ACT ON — `rate-limited` (wait
    for the window, or set GITHUB_TOKEN), `network` (fix connectivity, or fall back to
    L2/L3), `timeout` (retry later) — and it is `null` when the cause is internal,
    unexpected, or an unimplemented source. A `null` reason never means "nothing was
    wrong": the envelope is still `degraded` and stderr carries the real cause
    (`_warn_internal`). Naming a cause that does not exist, such as `timeout` for a
    probe.py bug or for an HTTP status the server delivered and rejected, would send the
    caller after a remedy that cannot work.
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

def search_envelope(terms, source="auto", limit=8, fetch=urlopen_json, strict_only=False,
                    clock=None, sleep=None):
    """Run the source legs; return (exit_code, envelope) in the §4.3 shape.

    `status: "ok"` with `results: []` is the ONLY "there is genuinely nothing"
    signal. Every other combination has to be spoken aloud, so a rate limit, a dead
    network or a bug returns `degraded` with an empty `results` — never `ok` + `[]`,
    which would make the skill silently skip a precedent that does exist.

    `terms` is the CLEANED list from validate_terms(); `total` in a source entry is the
    source's OWN match count — GitHub's `total_count`, Algolia's `nbHits` — which may
    exceed `len(results)` because the page size caps the array (measured: total 5260, 8
    returned). The Hub is the documented exception: its models endpoint exposes no match
    count at all, so an `hf` entry's `total` IS the number of models returned and must be
    read as "the Hub returned N", never as "the Hub has exactly N" (see `_hf_leg`).

    `source` selects which legs run. `auto` is the §5.5 strategy: GitHub first, and HN
    only when GitHub came back EMPTY (a hit, however noisy, is the caller's to judge —
    probe.py does not rank). `github`/`hn`/`hf` force a single source, and a source with
    no retriever in this build is a whole-operation degradation, never a silent answer
    with another source's data; see `_source_legs`.

    A failed HN supplement is PARTIAL: the envelope stays ok/exit 0, `reason` stays
    null and `sources` gains `{"source": "hn", "status": "failed"}`. The source list is
    how the caller learns a source did not answer; `reason` is reserved for a failure
    of the operation as a whole (§4.3).

    `reason` is None when the cause is outside §4.3's vocabulary (internal error,
    unimplemented source): the envelope is still `degraded`, and stderr carries the
    real exception type. stdout is always pure JSON.

    `clock`/`sleep` are the retry's time seam (Item 6a): they default to `time.time` /
    `time.sleep`, and a caller that passes its own owns the wait — a test proves the
    reset arithmetic without serving it. A rate limit whose reset is further away than
    `SEARCH_BUDGET_SECONDS` is not retried at all.
    """
    wanted = "github" if source == "auto" else source
    leg = _source_legs().get(wanted)
    if leg is None:
        _warn("error: --source %s has no retriever in this build; no query was sent "
              "and GitHub was NOT queried in its place" % wanted)
        return EXIT_DEGRADED, _degraded(None, source=wanted)
    retrying = _retrying_fetch(fetch, clock=clock, sleep=sleep)
    try:
        found = leg(terms, limit, fetch=retrying, strict_only=strict_only)
        # Read and validate the leg's payload INSIDE the protected region: a leg is an
        # outside shape (github_search, _hn_leg, _hf_leg, a test double), and a shape bug in
        # any of them must degrade with a diagnostic instead of escaping main() as a
        # traceback — including a non-list `results`, which `_leg_payload` refuses.
        entry, results = _leg_payload(wanted, found)
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
    sources = [entry]
    if source == "auto" and not results:
        # §5.5: HN is the retry path for GitHub's semantic gap, not a parallel leg — it
        # is asked only when the primary source came back with nothing to show. The
        # payload is read INSIDE the try for the same reason as the primary leg's: a
        # supplement returning an unexpected shape must degrade like any other failure of
        # the supplement instead of escaping as a traceback.
        try:
            hn = _source_legs()["hn"](terms, limit, fetch=retrying,
                                      strict_only=strict_only)
            hn_entry, hn_results = _leg_payload("hn", hn)
        except Exception as exc:    # noqa: BLE001 - a supplement failure is partial
            # stdout keeps the ruled shape (ok / reason null), so the failure has to be
            # spoken on stderr or an empty answer reads as "nothing exists" (§4.3 v2.2).
            _warn_supplement_failed("hn", _classify(exc))
            sources.append({"source": "hn", "status": "failed"})
        else:
            sources.append(hn_entry)
            results = hn_results
    if source == "auto" and is_ai_requirement(terms):
        # §5.5's conditional source: an AI-shaped requirement ALSO asks the Hub, and its
        # models are APPENDED to whatever the other legs found — never substituted, because
        # a model is not a code library the user can drop in. A failed Hub search is PARTIAL
        # exactly like a failed HN supplement: the envelope stays `ok`, `reason` stays null
        # and the `sources` entry is what tells the caller the model search did not answer.
        try:
            hf = _source_legs()["hf"](terms, limit, fetch=retrying,
                                      strict_only=strict_only)
            hf_entry, hf_results = _leg_payload("hf", hf)
        except Exception as exc:    # noqa: BLE001 - a supplement failure is partial
            problem = _classify(exc)
            if isinstance(problem, InternalError):
                _warn_internal(problem, "hf failed; the envelope stays ok")
            else:
                # Spec v2.2: a failed source must be visible to a HUMAN too, not only as a
                # `sources` entry on stdout. This matters most exactly when GitHub and HN
                # answered nothing: `ok` + `[]` is then also the shape of "there genuinely
                # is no precedent", and the caller must not read a dead Hub as that.
                _warn("error: hf supplement failed (%s); the envelope stays ok, so this "
                      "search is incomplete" % _degraded_reason(problem))
            sources.append({"source": "hf", "status": "failed"})
        else:
            sources.append(hf_entry)
            results = results + hf_results
    return EXIT_OK, {"status": "ok", "reason": None, "sources": sources,
                     "results": results}

HEALTH_QUERY = "wheel-check"        # a liveness probe, never a retrieval
GITHUB_RATE_LIMIT = GITHUB_API + "/rate_limit"
GITHUB_ANON_SEARCH_LIMIT = 10       # §2.3/§6: anonymous search quota; 30 with a token
GITHUB_BUCKETS = ("search", "core")  # the two buckets this build spends (§2.3, §6)

def github_quota(body):
    """The quota facts GitHub reports for THIS process, per bucket (§2.3, §6).

    Both buckets this build consumes come from the one /rate_limit body: `search`
    (10/min anonymous) is what `search` spends, and `core` (60/h) is what `repo` spends.
    Reporting only `search` was measured to be a live false ok — health said healthy while
    `probe.py repo owner/name` was already 403ing on a spent core bucket (60/60 used,
    search 10/10 remaining) — so both are reported. Reading them costs no request from
    either bucket. An unreadable body raises InternalError: a quota nobody can read is
    not a quota of zero.
    """
    resources = (body or {}).get("resources")
    if not isinstance(resources, dict):
        raise InternalError(ValueError("rate_limit body has no resources: %r" % (body,)))
    quota = {}
    for bucket in GITHUB_BUCKETS:
        facts = resources.get(bucket)
        if not isinstance(facts, dict):
            raise InternalError(ValueError(
                "rate_limit body has no resources.%s: %r" % (bucket, body)))
        quota[bucket] = {"limit": facts.get("limit"), "remaining": facts.get("remaining"),
                         "reset": facts.get("reset")}
    return quota

def _hn_page(body):
    """The Algolia body `_hn_hits` consumes: an object carrying a `hits` list."""
    if not isinstance(body, dict) or not isinstance(body.get("hits"), list):
        raise InternalError(ValueError(
            "not an Algolia story response (expected hits): %r" % (body,)))
    return body

# source -> the shape check that decides whether that leg answered with something usable.
# `github` is handled separately below because its check also yields the quota facts.
HEALTH_SHAPES = {"hn": _hn_page, "hf": _hf_models}

def _health_entry(name, url, fetch):
    """One source's liveness entry; a failure is classified, never raised (§4.3 vocab).

    Liveness means "this source answered with a body the retriever can actually use", not
    merely "a 200 arrived": `{}` from the Hub leaves `hf_search` raising, and a body without
    `hits` leaves `_hn_hits` raising, so certifying either `ok` would send the caller to a
    leg that cannot serve a single request. For those two the check is literally the
    retriever's own — `HEALTH_SHAPES` maps a source to the very function its retriever calls
    first (`_hn_page`, `_hf_models`) — so health and search cannot disagree about the same
    body. GitHub is the deliberate exception: its liveness comes from /rate_limit, which
    spends none of the 10 search requests/minute, so `github_quota` is not `_github_page`'s
    twin and the two are not meant to be compared.
    """
    try:
        body = fetch(url)
        if name == "github":
            quota = github_quota(body)
            entry = {"source": name, "status": "ok", "quota": quota,
                     # The API's own limit IS the observed auth state (10 anonymous, 30
                     # with a token). `request_headers` really does attach GITHUB_TOKEN to
                     # this request, so the reported limit describes what was SENT rather
                     # than what the environment claims.
                     "auth": ("token" if quota["search"]["limit"] > GITHUB_ANON_SEARCH_LIMIT
                              else "anonymous")}
        else:
            HEALTH_SHAPES[name](body)
            entry = {"source": name, "status": "ok"}
    except Exception as exc:        # noqa: BLE001 - health reports, it never raises
        problem = _classify(exc)
        if isinstance(problem, InternalError):
            _warn_internal(problem, "health: %s is reported failed" % name)
        return {"source": name, "status": "failed", "reason": _degraded_reason(problem)}
    return entry

def health_report(fetch=urlopen_json):
    """(exit_code, report): per-source liveness + auth + quota, in the §4.3 envelope shape.

    §4.2 defines `health` as liveness + auth + quota reported per source, and §6 makes it
    the stage that detects a dead network ("网络完全不通 | health 阶段即发现 → degraded /
    network"). A health command that prints nothing and exits 0 cannot make that call: the
    caller could not tell a live network from a dead one, so `status: ok` here is asserted
    about every leg, and any failed leg returns EXIT_DEGRADED with that source named.

    The GitHub leg reads /rate_limit, so health never spends one of the 10 search
    requests/minute. An exhausted SEARCH quota is a degraded answer — `search`, the
    primary capability, cannot run — and §6's remedy (set GITHUB_TOKEN) applies. The
    `core` bucket is reported but does not flip the verdict on its own: it only limits the
    `repo` backfill, and a non-zero health code sends the skill to L2/L3 (§6), which would
    trade real precision for nothing. Its facts are in `quota` for the caller to see.

    Every failure lands in `sources` with `reason` from §4.3's vocabulary, or `null` when
    the vocabulary has no word for it (a bug, an unreadable body), with the detail on
    stderr. stdout stays pure JSON.
    """
    legs = (("github", GITHUB_RATE_LIMIT),
            ("hn", _hn_url([HEALTH_QUERY], 1)),
            ("hf", _hf_url([HEALTH_QUERY], 1)))
    sources = [_health_entry(name, url, fetch) for name, url in legs]
    failures = [s for s in sources if s["status"] != "ok"]
    quota = sources[0].get("quota") or {}
    if failures:
        status, reason = "degraded", failures[0]["reason"]
    elif (quota.get("search") or {}).get("remaining") == 0:
        status, reason = "degraded", "rate-limited"
    else:
        status, reason = "ok", None
    report = {"status": status, "reason": reason, "sources": sources, "results": []}
    return (EXIT_OK if status == "ok" else EXIT_DEGRADED), report

def valid_full_name(full_name):
    """True for `owner/name`: exactly one slash, both halves non-empty, no whitespace.

    §4.2's `repo` takes a full_name, and anything else is a malformed CALL (exit 3), not a
    lookup that found nothing: degraded would read as "the network/API is unhappy" when
    the real answer is "that is not a repository name" (Review Focus #1).

    A non-string is malformed too, and says so as a clean False rather than dying on
    `.count` inside a callers' `except Exception` (Item 3: `repo_detail` is a library
    boundary, and a boundary that raises AttributeError for `None` is not a boundary).
    """
    return (isinstance(full_name, str) and bool(full_name)
            and full_name.count("/") == 1
            and not any(c.isspace() for c in full_name)
            and all(full_name.split("/")))

def repo_envelope(full_name, fetch=urlopen_json, clock=None, sleep=None):
    """(exit_code, envelope) for ONE repository: §4.2's hard-fact lookup, single result.

    A direct lookup runs no filter ladder, so its source entry carries no `tier` and no
    `total`: neither fact exists for "tell me about this exact repository", and inventing
    them would be the kind of made-up number this contract bans.

    `clock`/`sleep` are the same retry time seam `search_envelope` takes (Item 6a).
    """
    try:
        entry = repo_detail(full_name, fetch=_retrying_fetch(fetch, clock=clock, sleep=sleep))
    except Exception as exc:        # noqa: BLE001 - any lookup failure degrades
        problem = _classify(exc)
        if isinstance(problem, InternalError) and isinstance(problem.cause,
                                                            urllib.error.HTTPError):
            # A 4xx that is not a quota error: a typo'd name (404) or a bad lookup (400)
            # was delivered and rejected. §4.3 has no word for it, so the envelope reports
            # `degraded` with `reason: null` and this line is the whole diagnosis — the raw
            # status, which is exactly what the caller has to see.
            _warn("error: repo lookup failed for %s: HTTP %s"
                  % (full_name, problem.cause.code))
        elif isinstance(problem, InternalError):
            _warn_internal(problem)
        else:
            # A transport failure does have a §4.3 name (it is in the envelope); the detail
            # is echoed here so the `repo` call is diagnosable on its own.
            _warn("error: repo lookup failed for %s: %s" % (full_name, problem))
        return EXIT_DEGRADED, _degraded(_degraded_reason(problem), source="github")
    return EXIT_OK, {"status": "ok", "reason": None,
                     "sources": [{"source": "github", "status": "ok"}],
                     "results": [entry]}

class _ArgParser(argparse.ArgumentParser):
    """ArgumentParser whose usage errors exit with EXIT_BAD_ARGS, not argparse's 2.

    argparse's default error() calls sys.exit(2), and 2 means EXIT_DEGRADED in
    this project: a malformed call would be reported as a retrieval failure.
    """

    def error(self, message):
        # A usage error must exit 3 even when stderr cannot be written; the message is a
        # diagnostic, the exit code is the contract (see `_warn`).
        try:
            self.print_usage(sys.stderr)
        except Exception:           # noqa: BLE001 - a diagnostic is never load-bearing
            pass
        _warn("%s: error: %s" % (self.prog, message))
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
            _warn("error: %s" % e)
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
    if args.cmd == "health":
        # §6: the dead network is discovered HERE, so the stage has to speak — a silent
        # exit 0 would let a dead network look like a healthy one all the way to the user.
        code, report = health_report()
        print(json.dumps(report, ensure_ascii=False))
        return code
    if args.cmd == "repo":
        if not valid_full_name(args.full_name):
            _warn("error: repo needs a full_name of the form owner/name, got %r"
                  % (args.full_name,))
            return EXIT_BAD_ARGS
        code, env = repo_envelope(args.full_name)
        print(json.dumps(env, ensure_ascii=False))
        return code
    return EXIT_OK

if __name__ == "__main__":
    sys.exit(main())
