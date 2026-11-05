#!/usr/bin/env python
"""Rehearse the journal with a cast of agents before anything is preregistered.

    .venv/bin/uvicorn app.main:app --port 8000
    .venv/bin/python scripts/dry_run.py                       # free, stub models
    ANTHROPIC_API_KEY=... .venv/bin/python scripts/dry_run.py \
        --backend anthropic --models claude-opus-5 --max-calls 120 --admin-token ...

Pass several models, from different families, to exercise the panel-diversity rule.

Nothing here has privileged access to the journal: the harness registers
accounts and drives them over the public HTTP API, which is also a test of
whether that API is enough to participate.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dryrun.backends import make_backend  # noqa: E402
from dryrun.personas import build_cast  # noqa: E402
from dryrun.report import render  # noqa: E402
from dryrun.runner import DryRun, Journal  # noqa: E402
from dryrun.world import make_studies  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://localhost:8000")
    parser.add_argument("--backend", default="stub", choices=["stub", "anthropic", "openai"])
    parser.add_argument(
        "--models",
        default="claude-opus-5,gpt-5,gemini-2.5-pro",
        help="Comma-separated roster, dealt round-robin across the cast. "
        "Pass several to get a first look at cross-model agreement.",
    )
    parser.add_argument("--agents", type=int, default=6)
    parser.add_argument("--papers", type=int, default=6)
    parser.add_argument("--rounds", type=int, default=8)
    parser.add_argument("--max-calls", type=int, default=None, help="Hard cap on model calls.")
    parser.add_argument("--admin-token", default=None, help="Needed to designate founding editors.")
    parser.add_argument("--editors", type=int, default=2, help="How many of the cast are founding editors.")
    parser.add_argument("--effort", default=None, choices=["low", "medium", "high", "xhigh", "max"])
    parser.add_argument("--real-fraction", type=float, default=0.5)
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Seeds the synthetic world. Defaults to something new each run, because "
        "manuscripts are content-addressed and a repeated world is refused as duplicate.",
    )
    parser.add_argument("--out", default=None, help="Write the report to this file as well.")
    parser.add_argument(
        "--invite",
        default=None,
        help="Invite code, when the journal is gated. Needs enough uses for the whole cast.",
    )
    args = parser.parse_args()

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    journal = Journal(args.base)
    health = journal.call("GET", "/api/health")
    if not health or "_error" in health:
        raise SystemExit(f"no journal at {args.base} — start uvicorn first")

    backend = make_backend(args.backend, args.max_calls, args.effort)
    cast = build_cast(models, args.agents)
    seed = args.seed if args.seed is not None else int(time.time()) % 1_000_000
    studies = make_studies(args.papers, real_fraction=args.real_fraction, seed=seed)

    print(
        f"  cast of {len(cast)} across {models}, {len(studies)} studies, "
        f"backend {backend.name}, world seed {seed}"
    )
    if backend.name in ("anthropic", "openai"):
        print("  this spends real inference on your account; --max-calls caps it\n")

    log = DryRun(
        journal, backend, cast, studies,
        invite_code=args.invite, admin_token=args.admin_token, editors=args.editors,
    ).run(args.rounds)

    report = render(log, backend.name, backend.usage, journal.call("GET", "/api/statistics") or {})
    print(report)
    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")
        print(f"  written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
