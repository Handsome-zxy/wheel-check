#!/usr/bin/env python3
"""wheel-check data layer: GitHub / HN / HuggingFace retrieval. Hard facts only."""
import argparse
import json
import sys
import urllib.parse
import urllib.request
from datetime import date, timedelta

EXIT_OK = 0
EXIT_DEGRADED = 2
EXIT_BAD_ARGS = 3

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

def build_query(terms, suffix):
    """Merge every term into a single OR query so one tier costs one request.

    Unauthenticated GitHub search allows only 10 requests/minute, so one request
    per term is not affordable.
    """
    quoted = " OR ".join('"%s"' % t for t in terms)
    return ("(%s)" % quoted if len(terms) > 1 else quoted) + suffix

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
               + "&sort=stars&order=desc&per_page=%d" % limit)
        data = fetch(url)
        items = data.get("items") or []
        if items:
            return {"tier": tier, "total": data.get("total_count", 0),
                    "results": [normalize_repo(i) for i in items]}
    return {"tier": "none", "total": 0, "results": []}

class _ArgParser(argparse.ArgumentParser):
    """ArgumentParser whose usage errors exit with EXIT_BAD_ARGS, not argparse's 2.

    argparse's default error() calls sys.exit(2), and 2 means EXIT_DEGRADED in
    this project: a malformed call would be reported as a retrieval failure.
    """

    def error(self, message):
        self.print_usage(sys.stderr)
        print("%s: error: %s" % (self.prog, message), file=sys.stderr)
        raise SystemExit(EXIT_BAD_ARGS)

def build_parser():
    ap = _ArgParser(prog="probe.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("health")
    s = sub.add_parser("search")
    s.add_argument("--terms", required=True)
    s.add_argument("--source", default="auto", choices=["auto", "github", "hn", "hf"])
    s.add_argument("--strict", action="store_true")
    s.add_argument("--limit", type=int, default=8)
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
            validate_terms(args.terms)
        except ValueError as e:
            print("error: %s" % e, file=sys.stderr)
            return EXIT_BAD_ARGS
    return EXIT_OK

if __name__ == "__main__":
    sys.exit(main())
