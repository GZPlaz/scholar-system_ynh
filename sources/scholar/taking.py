"""Presenting and recording an online attempt (shared by the test portal and the staff preview)."""
from .grading import effective_deadline, save_answer
from .models import utcnow

MAX_TEXT_ANSWER = 20000


def ordered_entries(attempt):
    """Items and choices in the order this attempt presents them."""
    items = {item.id: item for item in attempt.assessment.items}
    order = [i for i in attempt.get_item_order() if i in items]
    order += [i.id for i in attempt.assessment.items if i.id not in order]
    choice_order = attempt.get_choice_order()
    rmap = attempt.response_map()
    entries = []
    for number, item_id in enumerate(order, start=1):
        item = items[item_id]
        choices = list(item.question.choices)
        if item_id in choice_order:
            by_id = {c.id: c for c in choices}
            ordered = [by_id[c] for c in choice_order[item_id] if c in by_id]
            choices = ordered + [c for c in choices if c not in ordered]
        response = rmap.get(item_id)
        entries.append(
            {
                "number": number,
                "item": item,
                "question": item.question,
                "choices": choices,
                "response": response,
                "answer": response.get_answer() if response else None,
            }
        )
    return entries


def review_entries(attempt, staff=False):
    entries = ordered_entries(attempt)
    show_key = staff or attempt.assessment.show_correct_answers
    for entry in entries:
        question = entry["question"]
        answer = entry["answer"]
        selected = set(answer if isinstance(answer, list) else ([answer] if answer is not None else []))
        entry["selected"] = selected
        entry["show_key"] = show_key
        if question.qtype == "short":
            entry["key_text"] = ", ".join(c.text for c in question.choices)
        elif question.qtype == "numeric" and question.numeric_answer is not None:
            tol = question.numeric_tolerance
            entry["key_text"] = f"{question.numeric_answer:g}" + (f" (± {tol:g})" if tol else "")
        else:
            entry["key_text"] = ""
    return entries


def clean_answer(item, values):
    """Validate a raw list of submitted values for one item into the stored answer format."""
    question = item.question
    values = [v for v in values if v is not None]
    if question.qtype in ("mc", "tf"):
        valid = {c.id for c in question.choices}
        for v in values:
            try:
                cid = int(v)
            except (TypeError, ValueError):
                continue
            if cid in valid:
                return cid
        return None
    if question.qtype == "ms":
        valid = {c.id for c in question.choices}
        picked = set()
        for v in values:
            try:
                cid = int(v)
            except (TypeError, ValueError):
                continue
            if cid in valid:
                picked.add(cid)
        return sorted(picked) or None
    text = (values[0] if values else "") or ""
    text = str(text)[:MAX_TEXT_ANSWER]
    return text if text.strip() else None


def store_form_answers(attempt, form):
    for item in attempt.assessment.items:
        key = f"q_{item.id}"
        if key not in form and f"present_{item.id}" not in form:
            continue
        save_answer(attempt, item, clean_answer(item, form.getlist(key)))


def remaining_seconds(attempt):
    deadline = effective_deadline(attempt)
    if deadline is None:
        return None
    return max(0, int((deadline - utcnow()).total_seconds()))
