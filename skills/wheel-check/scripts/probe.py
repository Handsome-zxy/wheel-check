#!/usr/bin/env python3
"""wheel-check data layer: GitHub / HN / HuggingFace retrieval. Hard facts only."""
import argparse
import sys

EXIT_OK = 0
EXIT_DEGRADED = 2
EXIT_BAD_ARGS = 3

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
