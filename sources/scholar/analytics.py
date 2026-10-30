"""Psychometrics (item analysis, reliability) and standards-mastery aggregation."""
import math
from statistics import mean, median, pstdev

from .grading import counted_attempts
from .utils import thresholds


def _pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx, my = mean(xs), mean(ys)
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


def score_summary(percents):
    if not percents:
        return None
    bins = [0] * 10
    for p in percents:
        bins[min(9, max(0, int(p // 10)))] += 1
    return {
        "n": len(percents),
        "mean": mean(percents),
        "median": median(percents),
        "sd": pstdev(percents) if len(percents) > 1 else 0.0,
        "min": min(percents),
        "max": max(percents),
        "bins": bins,
        "bin_max": max(bins) or 1,
    }


def item_analysis(assessment):
    """Classical test theory statistics for every item of an assessment."""
    attempts = list(counted_attempts(assessment).values())
    items = list(assessment.items)
    n = len(attempts)
    result = {"n": n, "items": [], "summary": None, "alpha": None, "sem": None}
    if not items:
        return result

    # score matrix: rows = attempts, cols = items (raw points; ungraded counts as 0)
    resp_maps = [a.response_map() for a in attempts]
    matrix = []
    for rmap in resp_maps:
        row = []
        for item in items:
            r = rmap.get(item.id)
            row.append((r.points_awarded or 0.0) if r else 0.0)
        matrix.append(row)
    totals = [sum(row) for row in matrix]
    max_total = assessment.max_score or 1.0
    result["summary"] = score_summary([100.0 * t / max_total for t in totals])

    # reliability: Cronbach's alpha (equals KR-20 for dichotomous items)
    k = len(items)
    if n > 1 and k > 1:
        item_vars = [pstdev([row[j] for row in matrix]) ** 2 for j in range(k)]
        total_var = pstdev(totals) ** 2
        if total_var > 0:
            alpha = (k / (k - 1)) * (1 - sum(item_vars) / total_var)
            result["alpha"] = alpha
            sd_raw = math.sqrt(total_var)
            result["sem"] = sd_raw * math.sqrt(max(0.0, 1 - alpha))

    # upper / lower 27% groups
    ranked = sorted(range(n), key=lambda i: totals[i])
    group = max(1, round(0.27 * n)) if n >= 2 else 0
    lower = set(ranked[:group])
    upper = set(ranked[-group:]) if group else set()

    for j, item in enumerate(items):
        question = item.question
        pts = item.effective_points or 1.0
        # responses still waiting for a grader are left out of this item's statistics
        graded = [i for i, rmap in enumerate(resp_maps) if not (rmap.get(item.id) and rmap[item.id].needs_grading)]
        pending = n - len(graded)
        scores = [matrix[i][j] for i in graded]
        fractions = [s / pts for s in scores]
        p_value = mean(fractions) if graded else None
        rest = [totals[i] - matrix[i][j] for i in graded]
        rpb = _pearson(scores, rest)
        d_index = None
        g_upper = [i for i in graded if i in upper]
        g_lower = [i for i in graded if i in lower]
        if g_upper and g_lower:
            d_index = mean(matrix[i][j] / pts for i in g_upper) - mean(matrix[i][j] / pts for i in g_lower)

        choices = []
        omitted = 0
        if question.has_choices:
            counts = {c.id: [0, 0, 0] for c in question.choices}  # total, upper, lower
            for i, rmap in enumerate(resp_maps):
                r = rmap.get(item.id)
                answer = r.get_answer() if r else None
                picked = answer if isinstance(answer, list) else ([answer] if answer is not None else [])
                picked = [p for p in picked if p in counts]
                if not picked:
                    omitted += 1
                for p in picked:
                    counts[p][0] += 1
                    if i in upper:
                        counts[p][1] += 1
                    if i in lower:
                        counts[p][2] += 1
            for idx, c in enumerate(question.choices):
                total, up, low = counts[c.id]
                choices.append(
                    {
                        "letter": chr(65 + idx),
                        "text": c.text,
                        "correct": c.is_correct,
                        "count": total,
                        "pct": (100.0 * total / n) if n else 0.0,
                        "upper": up,
                        "lower": low,
                        # a distractor chosen more by strong students than weak ones is suspicious
                        "flag": (not c.is_correct and up > low and group > 0)
                        or (c.is_correct and low > up and group > 0),
                    }
                )
        else:
            omitted = sum(1 for rmap in resp_maps if not rmap.get(item.id) or rmap[item.id].answer is None)

        flags = []
        if pending:
            flags.append(f"{pending} awaiting grading")
        if len(graded) >= 5 and p_value is not None:
            if p_value < 0.30:
                flags.append("Very difficult")
            elif p_value > 0.90:
                flags.append("Very easy")
        if rpb is not None and len(graded) >= 5:
            if rpb < 0:
                flags.append("Negative discrimination: check the key")
            elif rpb < 0.20:
                flags.append("Low discrimination")

        result["items"].append(
            {
                "number": j + 1,
                "item": item,
                "question": question,
                "p_value": p_value,
                "rpb": rpb,
                "d_index": d_index,
                "mean_points": mean(scores) if scores else None,
                "pending": pending,
                "omitted": omitted,
                "choices": choices,
                "flags": flags,
                "standards": question.standards,
            }
        )
    return result


# --------------------------------------------------------------------------- mastery


def level_for(percent):
    if percent is None:
        return "none"
    mastery, approaching = thresholds()
    if percent >= mastery:
        return "mastered"
    if percent >= approaching:
        return "approaching"
    return "not-yet"


LEVEL_LABELS = {
    "mastered": "Mastered",
    "approaching": "Approaching",
    "not-yet": "Not yet",
    "none": "No evidence",
}


def _accumulate(totals, attempt):
    """Add an attempt's per-standard earned/possible points into ``totals``."""
    rmap = attempt.response_map()
    for item in attempt.assessment.items:
        standards = item.question.standards
        if not standards:
            continue
        r = rmap.get(item.id)
        if r is not None and r.needs_grading:
            continue  # don't count evidence that has not been graded yet
        earned = (r.points_awarded or 0.0) if r else 0.0
        possible = item.effective_points
        for std in standards:
            bucket = totals.setdefault(std.id, [0.0, 0.0, 0])
            bucket[0] += earned
            bucket[1] += possible
            bucket[2] += 1


def _finish(totals):
    out = {}
    for std_id, (earned, possible, count) in totals.items():
        pct = (100.0 * earned / possible) if possible else None
        out[std_id] = {"earned": earned, "possible": possible, "items": count, "percent": pct, "level": level_for(pct)}
    return out


def course_mastery(course, assessments=None):
    """Returns (standards, {user_id: {standard_id: stats}}) for all students in a course."""
    assessments = assessments if assessments is not None else [a for a in course.assessments if a.status != "draft"]
    per_user = {}
    standards = {}
    for assessment in assessments:
        for item in assessment.items:
            for std in item.question.standards:
                standards[std.id] = std
        for user_id, attempt in counted_attempts(assessment).items():
            _accumulate(per_user.setdefault(user_id, {}), attempt)
    ordered = sorted(standards.values(), key=lambda s: s.code)
    return ordered, {uid: _finish(t) for uid, t in per_user.items()}


def student_mastery(user, courses, assessment_filter=None):
    """Mastery for one student across the given courses → (standards, stats)."""
    totals = {}
    standards = {}
    for course in courses:
        for assessment in course.assessments:
            if assessment.status == "draft" or (assessment_filter and not assessment_filter(assessment)):
                continue
            for item in assessment.items:
                for std in item.question.standards:
                    standards[std.id] = std
            attempt = counted_attempts(assessment).get(user.id)
            if attempt is not None:
                _accumulate(totals, attempt)
    ordered = sorted(standards.values(), key=lambda s: s.code)
    return ordered, _finish(totals)


def standard_class_summary(standards, per_user, student_ids):
    """Per-standard class averages and level distributions."""
    summary = {}
    for std in standards:
        values = [per_user[u][std.id]["percent"] for u in student_ids if u in per_user and std.id in per_user[u]]
        values = [v for v in values if v is not None]
        dist = {"mastered": 0, "approaching": 0, "not-yet": 0}
        for v in values:
            dist[level_for(v)] += 1
        summary[std.id] = {
            "mean": mean(values) if values else None,
            "level": level_for(mean(values)) if values else "none",
            "n": len(values),
            "dist": dist,
        }
    return summary


# --------------------------------------------------------------------------- per-test analytics


def _quantile(sorted_values, q):
    if not sorted_values:
        return None
    pos = (len(sorted_values) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def _skewness(values):
    if len(values) < 3:
        return None
    m, sd = mean(values), pstdev(values)
    if sd == 0:
        return 0.0
    return sum(((v - m) / sd) ** 3 for v in values) / len(values)


def _bins(values, lo, hi, count):
    width = (hi - lo) / count if hi > lo else 1
    bins = [0] * count
    for v in values:
        idx = int((v - lo) // width) if width else 0
        bins[min(count - 1, max(0, idx))] += 1
    return bins, width


SIMILARITY_MAX_ATTEMPTS = 400  # pair comparison is quadratic


def answer_similarity(attempts, items):
    """Pairs of attempts sharing unusually many identical wrong answers on choice questions.

    Two students who both pick the same wrong option again and again is the classic sign of
    copying (or of a shared wrong key). Flags pairs well above the class pattern; a flag is a
    reason to look, never proof.
    """
    choice_items = [i.id for i in items if i.question.has_choices]
    if len(choice_items) < 5 or len(attempts) < 3 or len(attempts) > SIMILARITY_MAX_ATTEMPTS:
        return {"pairs": [], "checked": False if len(attempts) > SIMILARITY_MAX_ATTEMPTS else len(choice_items) >= 5, "mean": None}
    wrong = []
    for attempt in attempts:
        rmap = attempt.response_map()
        wrong.append({iid: rmap[iid].answer for iid in choice_items if iid in rmap and rmap[iid].answer and rmap[iid].is_correct is False})
    counts = []
    for i in range(len(attempts)):
        for j in range(i + 1, len(attempts)):
            a, b = wrong[i], wrong[j]
            same = sum(1 for iid, ans in a.items() if b.get(iid) == ans)
            counts.append((same, i, j))
    values = [c[0] for c in counts]
    m = mean(values)
    sd = pstdev(values) if len(values) > 1 else 0.0
    threshold = max(3, m + 3 * sd)
    pairs = []
    for same, i, j in sorted(counts, reverse=True):
        if same < threshold:
            break
        both_wrong = len(set(wrong[i]) & set(wrong[j]))
        pairs.append(
            {
                "a": attempts[i],
                "b": attempts[j],
                "same": same,
                "both_wrong": both_wrong,
                "share": same / both_wrong if both_wrong else 0.0,
            }
        )
    return {"pairs": pairs[:20], "checked": True, "mean": m, "threshold": threshold}


def assessment_insights(assessment):
    """Everything the Analytics tab shows for one assessment."""
    counted = counted_attempts(assessment)
    attempts = sorted(counted.values(), key=lambda a: -a.percent)
    items = list(assessment.items)
    n = len(attempts)
    percents = sorted(a.percent for a in attempts)
    mastery, approaching = thresholds()
    out = {"n": n, "items": len(items), "max_score": assessment.max_score}

    # who took it
    enrolled = {u.id for u in assessment.course.students}
    out["completion"] = {
        "enrolled": len(enrolled),
        "submitted": sum(1 for uid in counted if uid in enrolled),
        "in_progress": len({a.user_id for a in assessment.attempts if a.status == "in_progress"}),
        "online": sum(1 for a in attempts if a.source == "online"),
        "paper": sum(1 for a in attempts if a.source == "paper"),
    }

    # score distribution
    if percents:
        bins, _ = _bins(percents, 0, 100, 10)
        out["scores"] = {
            "mean": mean(percents),
            "sd": pstdev(percents) if n > 1 else 0.0,
            "min": percents[0],
            "p10": _quantile(percents, 0.10),
            "q1": _quantile(percents, 0.25),
            "median": _quantile(percents, 0.5),
            "q3": _quantile(percents, 0.75),
            "p90": _quantile(percents, 0.90),
            "max": percents[-1],
            "skew": _skewness(percents),
            "bins": bins,
            "bin_max": max(bins) or 1,
            "levels": {
                "mastered": sum(1 for p in percents if p >= mastery),
                "approaching": sum(1 for p in percents if approaching <= p < mastery),
                "not-yet": sum(1 for p in percents if p < approaching),
            },
        }
    else:
        out["scores"] = None
    out["thresholds"] = {"mastery": mastery, "approaching": approaching}

    # reliability (Cronbach's alpha and SEM from item analysis, plus split-half)
    ia = item_analysis(assessment)
    out["alpha"], out["sem"] = ia["alpha"], ia["sem"]
    split = None
    if n >= 3 and len(items) >= 4:
        odd, even = [], []
        for attempt in attempts:
            rmap = attempt.response_map()
            pts = [(rmap[i.id].points_awarded or 0.0) if i.id in rmap else 0.0 for i in items]
            odd.append(sum(pts[0::2]))
            even.append(sum(pts[1::2]))
        r = _pearson(odd, even)
        if r is not None:
            split = 2 * r / (1 + r) if r > -1 else None
    out["split_half"] = split

    # items: difficulty × discrimination map
    out["item_rows"] = ia["items"]
    out["item_points"] = [
        {
            "number": e["number"],
            "x": 100 * e["p_value"],
            "y": e["rpb"],
            "flagged": bool(e["flags"]),
            "label": f"Q{e['number']}",
            "tip": f"Question {e['number']}: {round(100 * e['p_value'])}% correct, discrimination {e['rpb']:.2f}"
            + (f" ({'; '.join(e['flags'])})" if e["flags"] else ""),
        }
        for e in ia["items"]
        if e["p_value"] is not None and e["rpb"] is not None
    ]
    out["hardest"] = sorted((e for e in ia["items"] if e["p_value"] is not None), key=lambda e: e["p_value"])[:5]
    out["review_items"] = [e for e in ia["items"] if any(f.startswith(("Negative", "Low discrimination")) for f in e["flags"])]

    # time on test (online attempts)
    durations = [a.duration_seconds for a in attempts if a.duration_seconds is not None]
    timing = None
    if durations:
        minutes = sorted(d / 60 for d in durations)
        top = max(minutes[-1], 1)
        if assessment.time_limit_minutes:
            top = max(top, assessment.time_limit_minutes)
        step = max(1, math.ceil(top / 12))
        bins, width = _bins(minutes, 0, step * 12, 12)
        median_min = _quantile(minutes, 0.5)
        timing = {
            "median": median_min,
            "q1": _quantile(minutes, 0.25),
            "q3": _quantile(minutes, 0.75),
            "max": minutes[-1],
            "bins": bins,
            "bin_max": max(bins) or 1,
            "step": step,
            "limit": assessment.time_limit_minutes,
            "used_all": sum(1 for a in attempts if a.duration_seconds is not None and assessment.time_limit_minutes and a.duration_seconds >= assessment.time_limit_minutes * 60 - 30),
            "points": [
                {
                    "x": a.duration_seconds / 60,
                    "y": a.percent,
                    "label": a.user.display_name,
                    "flagged": False,
                    "tip": f"{a.user.display_name}: {a.percent:.0f}% in {a.duration_seconds / 60:.0f} min",
                }
                for a in attempts
                if a.duration_seconds is not None
            ],
            "corr": _pearson([a.duration_seconds for a in attempts if a.duration_seconds is not None], [a.percent for a in attempts if a.duration_seconds is not None]),
        }
    out["timing"] = timing

    # standards on this test
    standards = {}
    for item in items:
        for std in item.question.standards:
            entry = standards.setdefault(std.id, {"standard": std, "items": 0, "points": 0.0})
            entry["items"] += 1
            entry["points"] += item.effective_points
    per_student = {}
    for attempt in attempts:
        totals = {}
        _accumulate(totals, attempt)
        per_student[attempt.user_id] = _finish(totals)
    for std_id, entry in standards.items():
        values = [stats[std_id]["percent"] for stats in per_student.values() if std_id in stats and stats[std_id]["percent"] is not None]
        entry["mean"] = mean(values) if values else None
        entry["level"] = level_for(entry["mean"])
        entry["dist"] = {k: sum(1 for v in values if level_for(v) == k) for k in ("mastered", "approaching", "not-yet")}
        entry["n"] = len(values)
    out["standards"] = sorted(standards.values(), key=lambda e: (e["mean"] is None, e["mean"] if e["mean"] is not None else 0))

    # integrity
    similarity = answer_similarity(attempts, items)
    flagged_users = {p["a"].user_id for p in similarity["pairs"]} | {p["b"].user_id for p in similarity["pairs"]}
    median_duration = _quantile(sorted(durations), 0.5) if durations else None
    class_median = _quantile(percents, 0.5) if percents else None
    sd_pct = pstdev(percents) if n > 1 else 0.0
    rows = []
    for attempt in attempts:
        events = attempt.events
        rapid = (
            attempt.duration_seconds is not None
            and median_duration
            and len(durations) >= 5
            and attempt.duration_seconds < 0.25 * median_duration
            and class_median is not None
            and attempt.percent < class_median
        )
        rows.append(
            {
                "attempt": attempt,
                "user": attempt.user,
                "percent": attempt.percent,
                "z": (attempt.percent - out["scores"]["mean"]) / sd_pct if sd_pct else 0.0,
                "rank": 100.0 * (sum(1 for p in percents if p < attempt.percent) + 0.5 * sum(1 for p in percents if p == attempt.percent)) / n,
                "minutes": attempt.duration_seconds / 60 if attempt.duration_seconds is not None else None,
                "focus_losses": attempt.focus_losses,
                "device_events": sum(1 for e in events if e.kind in ("device_change", "device_blocked", "session_hijack_blocked")),
                "ip_changes": sum(1 for e in events if e.kind == "ip_change"),
                "blocked": sum(1 for e in events if e.kind in ("copy_blocked", "paste_blocked", "print_blocked", "contextmenu_blocked")),
                "paused": any(e.kind == "locked" for e in events),
                "rapid": bool(rapid),
                "similar": attempt.user_id in flagged_users,
                "level": level_for(attempt.percent),
            }
        )
    out["students"] = rows
    out["integrity"] = {
        "left_page": sum(1 for r in rows if r["focus_losses"]),
        "exits": sum(r["focus_losses"] for r in rows),
        "paused": sum(1 for r in rows if r["paused"]),
        "devices": sum(1 for r in rows if r["device_events"]),
        "networks": sum(1 for r in rows if r["ip_changes"]),
        "blocked": sum(r["blocked"] for r in rows),
        "rapid": sum(1 for r in rows if r["rapid"]),
        "similarity": similarity,
    }
    out["by_source"] = {
        src: mean([a.percent for a in attempts if a.source == src])
        for src in ("online", "paper")
        if any(a.source == src for a in attempts)
    }
    return out
