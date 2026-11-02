"""The command line: read paths, print findings, and answer with an exit code."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional, Sequence

from . import __version__
from .analyze import analyze_files

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_USAGE = 2

SEVERITIES = ("high", "medium", "low")
SEVERITY_RANK = {name: index for index, name in enumerate(SEVERITIES)}

FIX_HINT = (
    "read an Idempotency-Key and store it before the first write, or make the write\n"
    "         itself unique so a second delivery cannot land twice"
)


def render(findings: List[dict], files: int, routes: int) -> str:
    if not findings:
        return ("No issues found by idemcheck v%s across %d mutating route(s) in %d file(s).\n"
                "That means no unguarded write was recognised, not that every handler is safe."
                % (__version__, routes, files))

    worst = min((f["severity"] for f in findings), key=lambda s: SEVERITY_RANK[s])
    lines = ["idemcheck: %d finding(s) across %d mutating route(s) in %d file(s), worst %s"
             % (len(findings), routes, files, worst), ""]
    for f in findings:
        lines.append("[%s] %s:%d (%s)" % (f["severity"].upper(), f["path"], f["line"], f["rule"]))
        lines.append("  route: %s" % f["route"])
        lines.append("  why:   %s" % f["detail"])
        if f["writes"]:
            lines.append("  calls: %s" % ", ".join(f["writes"]))
        lines.append("  fix:   %s" % FIX_HINT)
        lines.append("")
    return "\n".join(lines).rstrip()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="idemcheck",
        description="Find the HTTP handlers that will double-charge when a client retries.",
    )
    parser.add_argument("paths", nargs="*", default=["."],
                        help="files or directories to scan (default: .)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--fail-on", choices=("high", "medium", "low", "none"), default="high",
                        help="lowest severity that fails the run (default: high)")
    parser.add_argument("--include-tests", action="store_true",
                        help="also scan tests/ and test_*.py")
    parser.add_argument("--version", action="version", version="idemcheck %s" % __version__)
    args = parser.parse_args(argv)

    targets = [Path(p) for p in args.paths]
    missing = [str(p) for p in targets if not p.exists()]
    if missing:
        print("no such path: %s" % ", ".join(missing), file=sys.stderr)
        return EXIT_USAGE

    report = analyze_files(targets, include_tests=args.include_tests)
    findings = [f.to_dict() for f in report.findings]

    if args.json:
        print(json.dumps({
            "findings": findings,
            "summary": {"files": report.files, "routes": report.routes,
                        "findings": len(findings), "fail_on": args.fail_on},
        }, indent=2))
    else:
        print(render(findings, report.files, report.routes))

    if args.fail_on == "none" or not findings:
        return EXIT_OK
    limit = SEVERITY_RANK[args.fail_on]
    return EXIT_FINDINGS if any(SEVERITY_RANK[f["severity"]] <= limit for f in findings) \
        else EXIT_OK


if __name__ == "__main__":        # so `python -m idemcheck.cli` works too
    raise SystemExit(main())
