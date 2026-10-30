"""A benchmark for the answer-sheet reader: realistic phone photos of marked sheets.

Run it directly to see the numbers:

    python tests/omr_bench.py --n 60 --seed 1

Sheets are drawn in colour the way they print (green rings and letters, black corner squares),
marked the way students mark (full, light, sloppy, scribbled, ticked, spilling over the ring,
ghosts of erasures), then photographed badly: uneven light, shadows, perspective, paper curl,
blur, noise and JPEG compression, at several camera resolutions and in any rotation.

What matters, in this order:
  silent errors  answers read wrongly and not flagged: a teacher would never see them
  flagged        answers sent to a human for a look (costs time, never marks)
  unreadable     photos that could not be read at all
"""
import argparse
import collections
import math
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "sources"))

from scholar import omr  # noqa: E402

GREEN = (32, 102, 59)  # BGR of the dropout ring ink (#3b6620)
PALE = (135, 180, 157)  # BGR of the letters inside bubbles (#9db487)


class _Choice:
    pass


class _Question:
    def __init__(self, qtype, n):
        self.qtype = qtype
        self.choices = [_Choice() for _ in range(2 if qtype == "tf" else n)]


class _Item:
    def __init__(self, i, question):
        self.id = i
        self.question = question


class _Assessment:
    def __init__(self, items):
        self.items = items


def make_assessment(rng, n_questions):
    items = []
    for i in range(1, n_questions + 1):
        r = rng.random()
        if r < 0.62:
            q = _Question("mc", int(rng.choice([4, 4, 5])))
        elif r < 0.78:
            q = _Question("tf", 2)
        elif r < 0.93:
            q = _Question("ms", int(rng.choice([4, 5])))
        else:
            q = _Question("short", 0)
        items.append(_Item(i, q))
    return _Assessment(items)


STYLES = {  # relative frequency of each way of marking a bubble
    "full": 0.52,
    "light": 0.14,
    "sloppy": 0.10,  # fills most of the bubble, leaves gaps
    "scribble": 0.08,
    "spill": 0.08,  # dark, runs over the ring
    "tick": 0.04,
    "dot": 0.04,
}
HARD_STYLES = {"full": 0.34, "light": 0.26, "sloppy": 0.12, "scribble": 0.10, "spill": 0.08, "tick": 0.05, "dot": 0.05}
PROFILE = {"profile": "normal"}  # set by evaluate()


def pick_style(rng):
    table = HARD_STYLES if PROFILE["profile"] == "hard" else STYLES
    return rng.choice(list(table), p=np.array(list(table.values())) / sum(table.values()))


def draw_mark(img, cx, cy, style, ppm, rng):
    r = omr.BUBBLE_D / 2 * ppm
    c = (int(cx), int(cy))
    if style == "full":
        shade = int(rng.uniform(30, 85))
        cv2.circle(img, c, int(r - 0.25 * ppm), (shade,) * 3, -1, cv2.LINE_AA)
    elif style == "light":
        shade = int(rng.uniform(115, 165))
        cv2.circle(img, c, int(r - 0.3 * ppm), (shade,) * 3, -1, cv2.LINE_AA)
    elif style == "sloppy":
        shade = int(rng.uniform(40, 90))
        overlay = img.copy()
        cv2.circle(overlay, c, int(r - 0.3 * ppm), (shade,) * 3, -1, cv2.LINE_AA)
        for _ in range(int(rng.integers(3, 6))):  # paper showing through
            gx, gy = cx + rng.uniform(-r * 0.7, r * 0.7), cy + rng.uniform(-r * 0.7, r * 0.7)
            cv2.circle(overlay, (int(gx), int(gy)), max(1, int(rng.uniform(0.25, 0.6) * ppm)), (235,) * 3, -1)
        img[:] = overlay
    elif style == "scribble":
        shade = int(rng.uniform(45, 100))
        for k in range(int(rng.integers(6, 10))):
            off = -r + k * (2 * r / 8)
            cv2.line(img, (int(cx + off - r * 0.6), int(cy + r * 0.7)), (int(cx + off + r * 0.6), int(cy - r * 0.7)), (shade,) * 3, max(2, int(0.45 * ppm)), cv2.LINE_AA)
    elif style == "spill":
        shade = int(rng.uniform(35, 80))
        cv2.circle(img, c, int(r + rng.uniform(0.2, 0.7) * ppm), (shade,) * 3, -1, cv2.LINE_AA)
    elif style == "tick":
        shade = int(rng.uniform(50, 100))
        pts = np.array([[cx - r * 0.55, cy + r * 0.0], [cx - r * 0.1, cy + r * 0.55], [cx + r * 0.8, cy - r * 0.7]], np.int32)
        cv2.polylines(img, [pts], False, (shade,) * 3, max(2, int(0.5 * ppm)), cv2.LINE_AA)
    elif style == "dot":
        shade = int(rng.uniform(50, 100))
        cv2.circle(img, c, int(0.9 * ppm), (shade,) * 3, -1, cv2.LINE_AA)


def render_sheet(layout, sheet_id, truth, styles, rng, ppm=7):
    """The printed page in colour with marks in it. truth: {item: [idx]}, styles: {(item, idx): style}."""
    off = omr.FID / 2 + 6
    W, H = int((omr.FRAME_W + 2 * off) * ppm), int((omr.FRAME_H + 2 * off) * ppm)
    img = np.full((H, W, 3), 255, np.uint8)
    P = lambda v: int(round((v + off) * ppm))  # noqa: E731
    for fx, fy in ((0, 0), (omr.FRAME_W, 0), (omr.FRAME_W, omr.FRAME_H), (0, omr.FRAME_H)):
        cv2.rectangle(img, (P(fx - omr.FID / 2), P(fy - omr.FID / 2)), (P(fx + omr.FID / 2), P(fy + omr.FID / 2)), (0, 0, 0), -1)
    for bit, (x, y) in zip(omr.encode_sheet_id(sheet_id), omr.code_cells()):
        if bit:
            cv2.rectangle(img, (P(x), P(y)), (P(x + omr.CODE_CELL), P(y + omr.CODE_CELL)), (0, 0, 0), -1)
    cv2.putText(img, "Unit 1 Check-in", (P(8), P(14)), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (25, 25, 25), 2, cv2.LINE_AA)
    cv2.putText(img, "Name: Amara Okafor    Student ID: MA2026001", (P(8), P(24)), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (40, 40, 40), 1, cv2.LINE_AA)
    cv2.line(img, (P(8), P(33)), (P(omr.FRAME_W - 8), P(33)), GREEN, 2)
    for slot in layout["slots"]:
        cv2.putText(img, str(slot["n"]), (P(slot["x"] + 2), P(slot["y"] + 4.2)), cv2.FONT_HERSHEY_SIMPLEX, 0.38, GREEN, 1, cv2.LINE_AA)
        if slot["kind"] == "choice":
            for k, (bx, by) in enumerate(slot["bubbles"]):
                cv2.circle(img, (P(bx), P(by)), int(omr.BUBBLE_D / 2 * ppm), GREEN, 1, cv2.LINE_AA)
                cv2.putText(img, slot["labels"][k], (P(bx - 0.9), P(by + 0.85)), cv2.FONT_HERSHEY_SIMPLEX, 0.26, PALE, 1, cv2.LINE_AA)
        elif slot["kind"] == "write":
            x, y, w, h = slot["box"]
            cv2.rectangle(img, (P(x), P(y)), (P(x + w), P(y + h)), GREEN, 1)
    # ghosts of erased marks, then the marks themselves
    for slot in layout["slots"]:
        if slot["kind"] != "choice":
            continue
        for k, (bx, by) in enumerate(slot["bubbles"]):
            if k not in truth.get(slot["item"], []) and rng.random() < 0.025:
                shade = int(rng.uniform(205, 232))
                cv2.circle(img, (P(bx), P(by)), int((omr.BUBBLE_D / 2 - 0.4) * ppm), (shade,) * 3, -1, cv2.LINE_AA)
    for slot in layout["slots"]:
        if slot["kind"] != "choice":
            continue
        for k in truth.get(slot["item"], []):
            bx, by = slot["bubbles"][k]
            draw_mark(img, P(bx), P(by), styles[(slot["item"], k)], ppm, rng)
    # a smudge or two
    for _ in range(int(rng.integers(0, 3))):
        sx, sy = rng.uniform(10, omr.FRAME_W - 10), rng.uniform(omr.AREA_TOP, omr.AREA_BOTTOM)
        cv2.ellipse(img, (P(sx), P(sy)), (int(rng.uniform(1, 3) * ppm), int(rng.uniform(0.5, 1.2) * ppm)), rng.uniform(0, 180), 0, 360, (int(rng.uniform(190, 225)),) * 3, -1, cv2.LINE_AA)
    return img


def photograph(sheet, rng, rotation=0, curl=True, shadow=True, harsh=False):
    hard = PROFILE["profile"] == "hard"
    h, w = sheet.shape[:2]
    pw, ph = [(4000, 3000), (3000, 4000), (2560, 1440), (1440, 2560), (1920, 1080), (4032, 3024)][int(rng.choice([0, 1, 1, 2, 3, 4, 5], p=[0.12, 0.3, 0.12, 0.2, 0.1, 0.16, 0.0]))]
    if harsh:
        pw, ph = 1600, 1200
    if curl:
        amp = rng.uniform(0.5, 2.6 if hard else 1.6) * 7  # px at 7 px/mm: up to 1.6 mm (2.6 when hard)
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        dx = amp * np.sin(np.pi * yy / h) * (2 * xx / w - 1)
        dy = amp * np.sin(np.pi * xx / w) * (2 * yy / h - 1) * 0.6
        sheet = cv2.remap(sheet, xx + dx, yy + dy, cv2.INTER_LINEAR, borderValue=(255, 255, 255))
    fill = rng.uniform(0.62, 0.9)
    scale = fill * min(pw / w, ph / h)
    cw, ch = w * scale, h * scale
    ox, oy = (pw - cw) / 2 + rng.uniform(-0.04, 0.04) * pw, (ph - ch) / 2 + rng.uniform(-0.03, 0.03) * ph
    j = lambda: rng.uniform(-0.07, 0.07) * cw  # noqa: E731
    dst = np.float32([[ox + j(), oy + j()], [ox + cw + j(), oy + j()], [ox + cw + j(), oy + ch + j()], [ox + j(), oy + ch + j()]])
    src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    M = cv2.getPerspectiveTransform(src, dst)
    desk = np.full((ph, pw, 3), (int(rng.uniform(70, 150)),) * 3, np.uint8)
    photo = cv2.warpPerspective(sheet, M, (pw, ph), borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0))
    mask = cv2.warpPerspective(np.full((h, w), 255, np.uint8), M, (pw, ph))
    photo = np.where(mask[..., None] > 0, photo, desk).astype(np.float32)
    yy, xx = np.mgrid[0:ph, 0:pw].astype(np.float32)
    a = rng.uniform(0.5, 0.85)
    light = a + (1 - a) * (rng.uniform(0, 1) * xx / pw + rng.uniform(0, 1) * yy / ph) / 1.4
    if shadow and rng.random() < (0.85 if hard else 0.4):  # a hand or a lamp casting a soft diagonal shadow
        theta = rng.uniform(0, math.pi)
        d = (xx - pw / 2) * math.cos(theta) + (yy - ph / 2) * math.sin(theta)
        centre, width = rng.uniform(-0.3, 0.3) * min(pw, ph), rng.uniform(0.08, 0.25) * min(pw, ph)
        light *= 1 - rng.uniform(0.3 if hard else 0.2, 0.65 if hard else 0.5) / (1 + np.exp(-(d - centre) / (width * 0.25))) * (d < centre + 2 * width)
    cast = np.array([rng.uniform(0.75, 1.0), rng.uniform(0.9, 1.0), 1.0])  # warm lamp: less blue
    photo = photo * light[..., None] * cast[None, None, :] + rng.normal(0, rng.uniform(2, 9), photo.shape)
    photo = np.clip(photo, 0, 255).astype(np.uint8)
    k = int(rng.choice([3, 5, 7, 9, 9] if hard else [1, 3, 3, 5, 5, 7]))
    if k > 1:
        photo = cv2.GaussianBlur(photo, (k, k), 0)
    for _ in range(rotation):
        photo = cv2.rotate(photo, cv2.ROTATE_90_CLOCKWISE)
    ok, buf = cv2.imencode(".jpg", photo, [int(cv2.IMWRITE_JPEG_QUALITY), int(rng.integers(38, 80) if hard else rng.integers(55, 92))])
    return buf.tobytes()


def make_case(seed, n_questions=48, harsh=False):
    rng = np.random.default_rng(seed)
    assessment = make_assessment(rng, n_questions)
    layout = omr.plan_pages(assessment)[0]
    truth, styles = {}, {}
    for slot in layout["slots"]:
        if slot["kind"] != "choice":
            continue
        n = len(slot["bubbles"])
        if rng.random() < 0.05:
            truth[slot["item"]] = []  # skipped
            continue
        if slot["qtype"] == "ms":
            pick = sorted(rng.choice(n, size=int(rng.integers(1, 4)), replace=False).tolist())
        else:
            pick = [int(rng.integers(0, n))]
        truth[slot["item"]] = pick
        for k in pick:
            styles[(slot["item"], k)] = pick_style(rng)
    sheet = render_sheet(layout, int(rng.integers(1, 5000)), truth, styles, rng)
    photo = photograph(sheet, rng, rotation=int(rng.integers(0, 4)), harsh=harsh)
    return layout, truth, styles, photo


def expected_value(slot, truth):
    return "".join(slot["labels"][k] for k in sorted(truth.get(slot["item"], [])))


def evaluate(n=40, seed=1, harsh=False, reader=omr, profile="normal"):
    PROFILE["profile"] = profile
    stats = collections.Counter()
    by_style = collections.defaultdict(collections.Counter)
    failures = []
    for i in range(n):
        layout, truth, styles, photo = make_case(seed * 1000 + i, harsh=harsh)
        stats["sheets"] += 1
        try:
            sheet = reader.read_photo(photo)
            read = reader.read_answers(sheet, layout)
        except omr.OMRError as exc:
            stats["unreadable"] += 1
            failures.append((i, str(exc)[:70]))
            continue
        clean = True
        for slot in layout["slots"]:
            if slot["kind"] != "choice":
                continue
            stats["answers"] += 1
            got = read[slot["item"]]
            want = expected_value(slot, truth)
            kinds = {styles[(slot["item"], k)] for k in truth.get(slot["item"], [])}
            if got["flag"]:
                stats["flagged"] += 1
                stats["flag:" + got["flag"]] += 1
                clean = False
                for s in kinds:
                    by_style[s]["flagged"] += 1
            if got["value"] != want and not got["flag"]:
                stats["silent_errors"] += 1
                clean = False
                for s in kinds or {"blank"}:
                    by_style[s]["silent"] += 1
            if got["flag"] and got["value"] == want:
                stats["flagged_but_right"] += 1
            for s in kinds:
                by_style[s]["marks"] += 1
        stats["clean_sheets"] += clean
    return stats, by_style, failures


def report(stats, by_style, failures):
    a = max(1, stats["answers"])
    ok = max(1, stats["sheets"] - stats["unreadable"])
    print(f"sheets {stats['sheets']}   unreadable {stats['unreadable']}   clean (no flag, no error) {stats['clean_sheets']}/{ok}")
    print(f"answers {stats['answers']}   silent errors {stats['silent_errors']} ({100 * stats['silent_errors'] / a:.2f}%)   "
          f"flagged {stats['flagged']} ({100 * stats['flagged'] / a:.1f}%, of which already right {stats['flagged_but_right']})")
    print("   flag reasons: " + ", ".join(f"{k[5:]} {v}" for k, v in sorted(stats.items()) if k.startswith("flag:")))
    for style, c in sorted(by_style.items()):
        print(f"   {style:9s} marks {c['marks']:4d}   flagged {c['flagged']:4d}   silent errors {c['silent']:3d}")
    for i, why in failures[:6]:
        print(f"   unreadable #{i}: {why}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--harsh", action="store_true", help="low-resolution cameras")
    ap.add_argument("--hard", action="store_true", help="deep shadows, heavy curl and blur, lots of light pencil")
    args = ap.parse_args()
    report(*evaluate(args.n, args.seed, args.harsh, profile="hard" if args.hard else "normal"))
