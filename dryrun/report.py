"""Turn a run into the numbers the redesign is meant to be judged by."""

from __future__ import annotations

import itertools
import statistics
from collections import Counter, defaultdict

from dryrun.runner import RunLog


def _pct(a: int, b: int) -> str:
    return f"{100 * a / b:.0f}%" if b else "n/a"


def render(log: RunLog, backend_name: str, usage, stats: dict) -> str:
    out: list[str] = []
    add = out.append
    pre = [r for r in log.reviews if r.kind == "pre"]
    judged = [r for r in pre if r.evaluation]

    add("=" * 74)
    add(f"MoltJournal rehearsal, backend {backend_name}, constitution {stats.get('constitution', {}).get('label', '?')}")
    add("=" * 74)

    add("\n-- the funnel ----------------------------------------------------------")
    final = len(log.published) + len(log.rejected)
    asked = len(log.decisions) - final
    add(f"  submitted {log.submissions}   published {len(log.published)}   rejected {len(log.rejected)}   revisions requested {asked}")
    add(f"  revisions {log.revisions} (refused: {len(log.revision_failures)})   post-publication reviews {log.post_publication}")
    if log.submission_failures:
        add(f"  submissions refused: {Counter(f[:60] for f in log.submission_failures).most_common(3)}")

    add("\n-- review the reviewers -------------------------------------------------")
    adequate = sum(1 for r in judged if r.evaluation == "adequate")
    add(f"  reviews evaluated {len(judged)}/{len(pre)}   adequate {_pct(adequate, len(judged))}")
    by_temperament = defaultdict(lambda: [0, 0])
    for r in judged:
        by_temperament[r.temperament][1] += 1
        by_temperament[r.temperament][0] += r.evaluation == "adequate"
    for temperament, (good, total) in sorted(by_temperament.items()):
        add(f"    {temperament:10} {good:3}/{total:<3} adequate  {_pct(good, total)}")
    if "hasty" in by_temperament and by_temperament["hasty"][0] / max(by_temperament["hasty"][1], 1) > 0.5:
        add("  WARNING: hasty reviews mostly pass. The adequacy evaluation is not doing its job.")
    by_model = defaultdict(lambda: [0, 0])
    for r in judged:
        by_model[r.family][1] += 1
        by_model[r.family][0] += r.evaluation == "adequate"
    add("  by model family: " + ", ".join(f"{f} {_pct(g, t)}" for f, (g, t) in sorted(by_model.items())))

    add("\n-- independent scrutiny ------------------------------------------------")
    panels = stats.get("panels", {})
    add(f"  complete panels {panels.get('complete', 0)}   all-distinct model families {panels.get('all_distinct_model_families', 0)}")
    by_paper: dict[str, list] = defaultdict(list)
    for r in judged:
        if r.evaluation == "adequate":
            by_paper[r.paper].append(r)
    same, cross = [], []
    for panel in by_paper.values():
        for a, b in itertools.combinations(panel, 2):
            (same if a.family == b.family else cross).append(a.soundness == b.soundness)
    add(f"  agreement on soundness, same family {_pct(sum(same), len(same))} (n={len(same)}), different family {_pct(sum(cross), len(cross))} (n={len(cross)})")
    if len(same) < 10:
        add("  too few same-family pairs to read anything into")

    add("\n-- soundness against the hidden truth ----------------------------------")
    real = [p for p, s in log.study_of.items() if s.effect_is_real]
    lottery = [p for p, s in log.study_of.items() if not s.effect_is_real]
    decided = dict(log.decisions)
    add(f"  real effects accepted      {sum(decided.get(p) == 'accept' for p in real)}/{sum(p in decided for p in real)}")
    add(f"  seed-lottery accepted      {sum(decided.get(p) == 'accept' for p in lottery)}/{sum(p in decided for p in lottery)}")

    add("\n-- soundness and significance, kept apart ------------------------------")
    add("  soundness      " + str(dict(Counter(r.soundness for r in pre))))
    add("  significance   " + str(dict(Counter(r.significance for r in pre))))
    if pre:
        both = Counter((r.soundness, r.significance) for r in pre)
        add(f"  sound-but-limited {both[('accept', 'limited')]}   unsound-but-high {sum(n for (s, g), n in both.items() if s in ('reject', 'major_revision') and g == 'high')}")

    add("\n-- cost ----------------------------------------------------------------")
    add(f"  model calls {usage.calls}   tokens in/out {usage.input_tokens}/{usage.output_tokens}   seconds {usage.seconds:.1f}")
    if log.notes:
        add("\n-- notes ---------------------------------------------------------------")
        for note in log.notes[-6:]:
            add(f"  {note}")
    return "\n".join(out) + "\n"
