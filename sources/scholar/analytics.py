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
                flags.append("Negative discrimination — check the key")
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
