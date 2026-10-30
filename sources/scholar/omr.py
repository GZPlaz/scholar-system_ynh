"""Optical mark recognition for printed answer sheets.

A printed sheet is a frame whose corners carry four solid black squares (fiducials).
Everything else is placed at fixed millimetre offsets inside that frame, so a phone
photo can be straightened with a perspective transform and each bubble sampled where
the layout says it is. A strip of 36 small squares under the top edge encodes the sheet
id (24 bits + CRC-8 + sync bits), which identifies the assessment, student and page and
also tells us which way up the photo was taken.

The layout half of this module is pure Python (used when printing). The reader needs
OpenCV and numpy and imports them lazily, so the rest of the app works without them.
"""
import hashlib
import json
import math

# --------------------------------------------------------------------------- geometry (mm)

FRAME_W = 176.0  # distance between fiducial centres, horizontally
FRAME_H = 244.0  # ... and vertically; fits A4 and US Letter with 10 mm print margins
FID = 8.0  # fiducial side
MARGIN = 6.0  # border kept around the frame in the straightened image (> FID / 2)
PX_PER_MM = 8  # resolution of the straightened image

CODE_CELLS = 36
CODE_PITCH = 4.0
CODE_CELL = 3.0
CODE_X0 = (FRAME_W - CODE_CELLS * CODE_PITCH) / 2 + (CODE_PITCH - CODE_CELL) / 2
CODE_Y = 2.0  # top edge of the code cells

AREA_TOP = 40.0
AREA_BOTTOM = 237.0
ROW_PITCH = 6.2
BUBBLE_D = 4.6
BUBBLE_PITCH = 6.0
NUMBER_W = 8.0
NUMBER_GAP = 1.5
COL_GAP = 4.0
MAX_BUBBLES = 8

SAMPLE_R = 1.55  # radius (mm) of the disc sampled inside each bubble: well inside the 2.3 mm ring
MARK_LEVEL = 0.42  # default darkness (0 = paper, 1 = fiducial black) of a filled bubble when a sheet gives no better guide
FAINT_LEVEL = 0.22  # default darkness below which a bubble is treated as empty


# --------------------------------------------------------------------------- sheet code


def crc8(data):
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def encode_sheet_id(sheet_id):
    if not 0 < sheet_id < (1 << 24):
        raise ValueError("sheet id out of range")
    body = [(sheet_id >> (23 - i)) & 1 for i in range(24)]
    check = crc8(sheet_id.to_bytes(3, "big"))
    return [1, 0] + body + [(check >> (7 - i)) & 1 for i in range(8)] + [0, 1]


def decode_bits(bits):
    if len(bits) != CODE_CELLS or bits[:2] != [1, 0] or bits[-2:] != [0, 1]:
        return None
    sheet_id = int("".join(map(str, bits[2:26])), 2)
    check = int("".join(map(str, bits[26:34])), 2)
    if sheet_id == 0 or crc8(sheet_id.to_bytes(3, "big")) != check:
        return None
    return sheet_id


def code_cells():
    """(x, y) of the top-left corner of each code cell, frame coordinates."""
    return [(CODE_X0 + i * CODE_PITCH, CODE_Y) for i in range(CODE_CELLS)]


# --------------------------------------------------------------------------- layout


def _slot_kind(question):
    if question.qtype in ("mc", "tf", "ms") and len(question.choices) <= MAX_BUBBLES:
        return "choice"
    if question.qtype in ("short", "numeric"):
        return "write"
    return "none"  # essays, or choice questions with more options than fit


def _labels(question):
    if question.qtype == "tf":
        return ["T", "F"]
    return [chr(65 + i) for i in range(len(question.choices))]


def plan_pages(assessment):
    """Split the assessment into sheet pages. Returns a list of layout dicts (JSON-able)."""
    items = list(assessment.items)
    widest = max([len(_labels(i.question)) for i in items if _slot_kind(i.question) == "choice"] or [4])
    nch = max(4, min(MAX_BUBBLES, widest))
    col_w = NUMBER_W + NUMBER_GAP + nch * BUBBLE_PITCH + COL_GAP
    cols = max(1, min(4, int((FRAME_W - 2 + COL_GAP) // col_w)))
    rows = int((AREA_BOTTOM - AREA_TOP) // ROW_PITCH)
    per_page = rows * cols
    x_start = (FRAME_W - (cols * col_w - COL_GAP)) / 2
    pages = []
    total = max(1, math.ceil(len(items) / per_page))
    for p in range(total):
        chunk = items[p * per_page : (p + 1) * per_page]
        slots = []
        for idx, item in enumerate(chunk):
            col, row = divmod(idx, rows)
            x = x_start + col * col_w
            y = AREA_TOP + row * ROW_PITCH
            q = item.question
            kind = _slot_kind(q)
            slot = {
                "item": item.id,
                "n": p * per_page + idx + 1,
                "kind": kind,
                "qtype": q.qtype,
                "x": round(x, 2),
                "y": round(y, 2),
            }
            bx = x + NUMBER_W + NUMBER_GAP
            if kind == "choice":
                slot["labels"] = _labels(q)
                slot["bubbles"] = [
                    [round(bx + k * BUBBLE_PITCH + BUBBLE_D / 2, 2), round(y + ROW_PITCH / 2, 2)]
                    for k in range(len(slot["labels"]))
                ]
            elif kind == "write":
                slot["box"] = [round(bx, 2), round(y + 0.6, 2), round(nch * BUBBLE_PITCH - 1.4, 2), round(ROW_PITCH - 1.2, 2)]
            slots.append(slot)
        pages.append({"v": 1, "page": p + 1, "pages": total, "nch": nch, "slots": slots})
    return pages


def layout_hash(layout):
    return hashlib.sha256(json.dumps(layout, sort_keys=True).encode()).hexdigest()


# --------------------------------------------------------------------------- reading photos
#
# The pipeline, and why each step is there:
#  1. Take the brightest colour channel. The sheet is printed in light green "dropout" ink and
#     pencil is grey, so in that channel the printing nearly vanishes and only black squares and
#     pencil remain, whatever colour the lamp is.
#  2. Flatten the lighting (divide by a blurred paper estimate) and find the four black squares,
#     trying more than one way of telling them from shadow. If one is missing, say which.
#  3. Straighten the photo from the four squares, then read the sheet code.
#  4. Paper is never quite flat. The printed rings of the bubbles are located all over the page
#     and a smooth correction is fitted to them, so bubbles are sampled where they really are.
#  5. Darkness is measured inside each bubble, minus what that same printed bubble looks like when
#     empty on this very sheet, and judged against how hard *this* student presses: a light
#     pencil is read as confidently as a dark one.


class OMRError(Exception):
    """A photo that cannot be read; the message is shown to the teacher."""


def available():
    try:
        import cv2  # noqa: F401
        import numpy  # noqa: F401
    except ImportError:
        return False
    return True


WORK_SIDE = 2400  # photos are shrunk to this many pixels on the long side
MIN_PX_PER_MM = 2.8  # a sheet photographed smaller than this can't show pencil marks reliably
LOW_PX_PER_MM = 3.3  # below this the result carries a "low resolution" note
MAX_PIXELS = 80_000_000
MAX_REGISTER_MM = 1.8  # the largest paper curl correction believed
REGISTER_ORDER = 2  # 0: no correction for curled paper, 2: quadratic, 3: cubic
INK_COVERAGE = 0.08  # share of a bubble's pixels that must be truly dark for it to count as inked


def _mm_to_px(v):
    return (v + MARGIN) * PX_PER_MM


def _corner_name(point, centre):
    horizontal = "left" if point[0] < centre[0] else "right"
    vertical = "top" if point[1] < centre[1] else "bottom"
    return f"{vertical}-{horizontal}"


def _candidates(cv2, np, binary, h, w):
    """Solid, squarish, sheet-sized blobs in a binary image."""
    contours, _ = cv2.findContours(binary, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    short = min(h, w)
    min_side, max_side = 0.007 * short, 0.09 * max(h, w)
    found = []
    for c in contours:
        area = cv2.contourArea(c)
        if area < min_side * min_side or area > max_side * max_side:
            continue
        (cx, cy), (rw, rh), _ = cv2.minAreaRect(c)
        if rw == 0 or rh == 0 or not 0.6 < rw / rh < 1.66:
            continue
        if area / (rw * rh) < 0.8:  # squares fill their bounding box; filled bubbles don't
            continue
        approx = cv2.approxPolyDP(c, 0.07 * cv2.arcLength(c, True), True)
        if len(approx) != 4 or not cv2.isContourConvex(approx):
            continue
        mask = np.zeros_like(binary)
        cv2.drawContours(mask, [c], -1, 255, -1)
        if cv2.mean(binary, mask=mask)[0] < 0.85 * 255:  # must be solid, not an outline
            continue
        found.append((area, (cx, cy)))
    found.sort(key=lambda t: -t[0])
    return found[:12]


def _best_quad(cv2, np, found, h, w):
    """Choose the four squares that form the corners of a sheet. Returns (points, score) or (None, 0)."""
    from itertools import combinations

    best, best_score = None, 0.0
    for combo in combinations(found, 4):
        areas = [a for a, _ in combo]
        if max(areas) / min(areas) > 3.5:
            continue
        pts = np.array([p for _, p in combo], dtype=np.float32)
        hull = cv2.convexHull(pts)
        if len(hull) != 4:
            continue
        quad_area = cv2.contourArea(hull)
        if quad_area < 0.05 * h * w:
            continue
        order = sorted(range(4), key=lambda i: math.atan2(pts[i][1] - pts.mean(axis=0)[1], pts[i][0] - pts.mean(axis=0)[0]))
        q = pts[order]
        sides = [float(np.linalg.norm(q[i] - q[(i + 1) % 4])) for i in range(4)]
        long_side, short_side = max(sides[0] + sides[2], sides[1] + sides[3]), min(sides[0] + sides[2], sides[1] + sides[3])
        if short_side == 0 or not 1.0 < long_side / short_side < 1.95:  # a sheet is 1.39 times as tall as wide
            continue
        if quad_area > best_score:
            best, best_score = q, quad_area
    return best, best_score


def _missing_corner_message(np, found, h, w):
    # corner markers are the big squares; the small squares of the sheet code must not count as one
    big = [c for c in found if found and c[0] >= 0.4 * found[0][0]]
    pts = [p for _, p in big[:4]]
    if len(pts) == 3:
        a, b, c = [np.array(p) for p in pts]
        # the corner with the squarest angle is the one opposite the missing square
        def squareness(v, p, q):
            u1, u2 = p - v, q - v
            return abs(float(np.dot(u1, u2)) / (np.linalg.norm(u1) * np.linalg.norm(u2) + 1e-9))

        trio = [(squareness(a, b, c), a, b, c), (squareness(b, a, c), b, a, c), (squareness(c, a, b), c, a, b)]
        _, v, p, q = min(trio, key=lambda t: t[0])
        missing = p + q - v
        where = _corner_name(missing, (w / 2, h / 2))
        inside = 0 <= missing[0] < w and 0 <= missing[1] < h
        why = "hidden by a shadow, a finger or glare" if inside else "cut off by the edge of the photo"
        return f"Only 3 of the 4 black corner squares were found: the one at the {where} of the photo is {why}. Retake the photo with all four in view."
    if len(pts) == 4:
        return "The corner squares were found but don't form a sheet. Photograph one sheet at a time, flat, from straight above."
    return (
        "The sheet could not be found: the four black corner squares were not seen. Photograph the whole page, flat, "
        "with all four corners in view and no shadow or finger across them."
    )


def _find_fiducials(cv2, np, gray):
    h, w = gray.shape
    k = max(31, (max(h, w) // 12) | 1)
    background = cv2.morphologyEx(gray, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (k, k)))
    norm = cv2.divide(gray, background, scale=255)
    blurred = cv2.GaussianBlur(norm, (5, 5), 0)
    block = (max(h, w) // 8) | 1
    strategies = (
        lambda: cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1],
        lambda: cv2.threshold(blurred, 95, 255, cv2.THRESH_BINARY_INV)[1],
        lambda: cv2.adaptiveThreshold(blurred, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, block, 28),
        lambda: cv2.threshold(blurred, 55, 255, cv2.THRESH_BINARY_INV)[1],
    )
    most = []
    for make in strategies:
        found = _candidates(cv2, np, make(), h, w)
        quad, _ = _best_quad(cv2, np, found, h, w)
        if quad is not None:
            return list(quad), norm
        if len(found) > len(most):
            most = found
    raise OMRError(_missing_corner_message(np, most, h, w))


def _warp(cv2, np, image, corners):
    dst = np.array(
        [[_mm_to_px(0), _mm_to_px(0)], [_mm_to_px(FRAME_W), _mm_to_px(0)], [_mm_to_px(FRAME_W), _mm_to_px(FRAME_H)], [_mm_to_px(0), _mm_to_px(FRAME_H)]],
        dtype=np.float32,
    )
    matrix = cv2.getPerspectiveTransform(np.array(corners, dtype=np.float32), dst)
    size = (int(round((FRAME_W + 2 * MARGIN) * PX_PER_MM)), int(round((FRAME_H + 2 * MARGIN) * PX_PER_MM)))
    return cv2.warpPerspective(image, matrix, size, flags=cv2.INTER_LINEAR, borderValue=255)


def _region_mean(img, x_mm, y_mm, w_mm, h_mm):
    x0, y0 = int(_mm_to_px(x_mm)), int(_mm_to_px(y_mm))
    x1, y1 = int(_mm_to_px(x_mm + w_mm)), int(_mm_to_px(y_mm + h_mm))
    return float(img[max(0, y0) : y1, max(0, x0) : x1].mean())


def _ring_template(cv2, np):
    """What the printed ring of one bubble looks like (bright on dark so it can be matched)."""
    half = int(round(RING_R * PX_PER_MM)) + 5
    t = np.zeros((2 * half + 1, 2 * half + 1), np.float32)
    cv2.circle(t, (half, half), int(round(RING_R * PX_PER_MM)), 1.0, 2, cv2.LINE_AA)
    return cv2.GaussianBlur(t, (0, 0), 1.1), half


RING_R = BUBBLE_D / 2


class ReadSheet:
    """A straightened, identified sheet image and helpers to sample it."""

    def __init__(self, image, sheet_id):
        import numpy as np

        self.image = image
        self.sheet_id = sheet_id
        self.registered = False
        self.quality = {}
        # references: solid fiducial ink and blank paper
        self.black = min(_region_mean(image, -2, -2, 4, 4), _region_mean(image, FRAME_W - 2, FRAME_H - 2, 4, 4))
        self.white = float(np.percentile(image, 90))
        if self.white - self.black < 40:
            raise OMRError("The photo is too dark or too washed out to tell pencil from paper. Retake it in better light.")
        r = int(round(SAMPLE_R * PX_PER_MM))
        yy, xx = np.mgrid[-r : r + 1, -r : r + 1]
        self._disc = (xx * xx + yy * yy) <= r * r
        self._r = r

    # -- paper that isn't flat ------------------------------------------------------------------

    def register(self, layout):
        """Find the printed rings, fit a smooth correction to where they really are, and flatten the image."""
        import cv2
        import numpy as np

        if self.registered:
            return
        self.registered = True
        if REGISTER_ORDER == 0:
            return
        slots = [s for s in layout["slots"] if s["kind"] == "choice"]
        expected = [(x, y) for s in slots for x, y in s["bubbles"]]
        self.quality["anchors"] = 0
        if len(expected) < 24:
            return
        template, half = _ring_template(cv2, np)
        search = int(round(1.5 * PX_PER_MM))
        inverted = (255 - self.image).astype(np.float32)
        H, W = inverted.shape
        pts, shifts, conf = [], [], []
        for x_mm, y_mm in expected:
            cx, cy = int(round(_mm_to_px(x_mm))), int(round(_mm_to_px(y_mm)))
            x0, y0, x1, y1 = cx - half - search, cy - half - search, cx + half + search + 1, cy + half + search + 1
            if x0 < 0 or y0 < 0 or x1 > W or y1 > H:
                continue
            res = cv2.matchTemplate(inverted[y0:y1, x0:x1], template, cv2.TM_CCOEFF_NORMED)
            _, peak, _, loc = cv2.minMaxLoc(res)
            if peak < 0.3:
                continue
            px, py = float(loc[0]), float(loc[1])
            if 0 < loc[0] < res.shape[1] - 1 and 0 < loc[1] < res.shape[0] - 1:  # sub-pixel
                for axis in (0, 1):
                    a, b, c = (
                        (res[loc[1], loc[0] - 1], res[loc[1], loc[0]], res[loc[1], loc[0] + 1])
                        if axis == 0
                        else (res[loc[1] - 1, loc[0]], res[loc[1], loc[0]], res[loc[1] + 1, loc[0]])
                    )
                    den = a - 2 * b + c
                    if den < 0:
                        delta = 0.5 * (a - c) / den
                        if axis == 0:
                            px += max(-0.5, min(0.5, float(delta)))
                        else:
                            py += max(-0.5, min(0.5, float(delta)))
            pts.append((cx, cy))
            shifts.append((px - search, py - search))
            conf.append(peak)
        self.quality["anchors"] = len(pts)
        if len(pts) < 24:
            return
        pts, shifts, w = np.array(pts, np.float64), np.array(shifts, np.float64), np.array(conf, np.float64) ** 2
        u, v = (pts[:, 0] - W / 2) / (W / 2), (pts[:, 1] - H / 2) / (H / 2)
        def basis(uu, vv):
            cols = [np.ones_like(uu), uu, vv, uu * uu, uu * vv, vv * vv]
            if REGISTER_ORDER >= 3:
                cols += [uu**3, uu * uu * vv, uu * vv * vv, vv**3]
            return np.stack(cols, axis=1)

        A = basis(u, v)
        coef = np.zeros((A.shape[1], 2))
        weights = w.copy()
        for _ in range(5):  # Tukey's biweight: filled bubbles and smudges pull the fit, so they lose their vote
            sw = np.sqrt(weights)[:, None]
            coef, *_ = np.linalg.lstsq(A * sw, shifts * sw, rcond=None)
            resid = np.linalg.norm(shifts - A @ coef, axis=1)
            scale = 1.4826 * float(np.median(resid)) + 0.25
            t = np.clip(resid / (4.685 * scale), 0, 1)
            weights = w * (1 - t * t) ** 2
        if float(np.sum(weights > 0.05 * w.max())) < 20:
            return
        # the correction must stay believable where the bubbles are (not at the empty page corners)
        gx, gy = np.meshgrid(np.linspace(u.min(), u.max(), 12), np.linspace(v.min(), v.max(), 12))
        grid = basis(gx.ravel(), gy.ravel()) @ coef
        biggest = float(np.abs(grid).max()) / PX_PER_MM
        self.quality["max_shift_mm"] = round(biggest, 2)
        if biggest > MAX_REGISTER_MM:
            return
        self.quality["registered"] = True
        yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
        full = basis(((xx - W / 2) / (W / 2)).ravel().astype(np.float64), ((yy - H / 2) / (H / 2)).ravel().astype(np.float64)) @ coef
        mapx = xx + full[:, 0].reshape(H, W).astype(np.float32)
        mapy = yy + full[:, 1].reshape(H, W).astype(np.float32)
        self.image = cv2.remap(self.image, mapx, mapy, cv2.INTER_LINEAR, borderValue=255)

    # -- measuring ------------------------------------------------------------------------------

    def darkness(self, value):
        return max(0.0, min(1.0, (self.white - value) / (self.white - self.black)))

    def bubble(self, x_mm, y_mm):
        """(mean darkness, share of dark pixels) inside a bubble, searching ±0.4 mm for what registration left."""
        import numpy as np

        best = (0.0, 0.0)
        step = int(round(0.4 * PX_PER_MM))
        cx0, cy0 = int(round(_mm_to_px(x_mm))), int(round(_mm_to_px(y_mm)))
        r = self._r
        span = self.white - self.black
        for dy in (-step, 0, step):
            for dx in (-step, 0, step):
                cx, cy = cx0 + dx, cy0 + dy
                patch = self.image[cy - r : cy + r + 1, cx - r : cx + r + 1]
                if patch.shape != self._disc.shape:
                    continue
                d = np.clip((self.white - patch[self._disc].astype(np.float32)) / span, 0.0, 1.0)
                mean = float(d.mean())
                if mean > best[0]:
                    best = (mean, float((d >= 0.4).mean()))
        return best

    def box_ink(self, x, y, w, h):
        """Share of a write-in box (inner part) that carries ink."""
        x0, y0 = int(_mm_to_px(x + 0.8)), int(_mm_to_px(y + 0.8))
        x1, y1 = int(_mm_to_px(x + w - 0.8)), int(_mm_to_px(y + h - 0.8))
        patch = self.image[y0:y1, x0:x1]
        if patch.size == 0:
            return 0.0
        threshold = (self.white + self.black) / 2
        return float((patch < threshold).mean())

    def jpeg(self, quality=72):
        import cv2

        ok, buf = cv2.imencode(".jpg", self.image, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
        return buf.tobytes() if ok else b""


def _read_code(img):
    import numpy as np

    black = min(_region_mean(img, -2, -2, 4, 4), _region_mean(img, FRAME_W - 2, -2, 4, 4))
    white = float(np.percentile(img, 90))
    if white - black < 40:
        return None
    inset = CODE_CELL * 0.2
    means = [_region_mean(img, x + inset, y + inset, CODE_CELL - 2 * inset, CODE_CELL - 2 * inset) for x, y in code_cells()]
    for frac in (0.5, 0.38, 0.62):  # where between black and paper to draw the line
        threshold = black + frac * (white - black)
        found = decode_bits([1 if m < threshold else 0 for m in means])
        if found is not None:
            return found
    return None


def read_photo(data):
    """Find, straighten and identify an answer sheet in a photo. Returns a ReadSheet."""
    try:
        import cv2
        import numpy as np
    except ImportError as exc:  # pragma: no cover - depends on the server
        raise OMRError("Scanning is not available on this server (OpenCV is not installed).") from exc
    raw = np.frombuffer(data, dtype=np.uint8)
    colour = cv2.imdecode(raw, cv2.IMREAD_COLOR)
    if colour is None:
        raise OMRError("This file is not an image Scholar can read. Use JPEG or PNG.")
    h, w = colour.shape[:2]
    if h * w > MAX_PIXELS:
        raise OMRError("The image is too large.")
    scale = WORK_SIDE / max(h, w)
    if scale < 1:
        colour = cv2.resize(colour, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    gray = colour.max(axis=2)  # the brightest channel: printed green ink drops out, pencil and black stay
    corners, norm = _find_fiducials(cv2, np, gray)
    side = max(float(np.linalg.norm(corners[i] - corners[(i + 1) % 4])) for i in range(4))
    px_per_mm = side / FRAME_H
    if px_per_mm < MIN_PX_PER_MM:
        raise OMRError(
            f"The sheet is too small in this photo ({px_per_mm:.1f} pixels per millimetre; at least {MIN_PX_PER_MM:g} are needed to see pencil marks). "
            "Move closer so the page fills the picture, or use a higher camera resolution."
        )
    for start in range(4):  # the photo may be rotated any way round; the code tells us which
        ordered = corners[start:] + corners[:start]
        warped = _warp(cv2, np, norm, ordered)
        sheet_id = _read_code(warped)
        if sheet_id is not None:
            found = ReadSheet(warped, sheet_id)
            found.quality["px_per_mm"] = round(px_per_mm, 1)
            return found
    raise OMRError(
        "The sheet code (the row of small squares along the top edge) could not be read. "
        "Make sure it is in the photo, in focus, and not covered."
    )


# --------------------------------------------------------------------------- judging the marks


def _typical_levels(rows):
    """How hard does this student press, and how dirty is this sheet? → (mark level, noise level)."""
    import numpy as np

    tops, others = [], []
    for row in rows:
        ranked = sorted(row, reverse=True)
        if not ranked:
            continue
        if ranked[0] >= 0.10:
            tops.append(ranked[0])
        others.extend(ranked[1:])
    mark = float(np.median(tops)) if len(tops) >= 6 else 0.6
    noise = float(np.percentile(others, 90)) if len(others) >= 12 else 0.05
    return mark, noise


def read_answers(sheet, layout):
    """Interpret the marks for one printed page layout.

    Returns {item_id: {"value": str, "fills": [...], "states": [...], "flag": str, "suggest": str}}
    where value is what a teacher would type on the paper-entry form ('B', 'AC', 'T', or '').
    flag is '' when the reading is certain, otherwise one of: multiple, faint, write. For a
    flagged answer, value holds the best guess so a human only has to confirm it.
    """
    sheet.register(layout)
    slots = [s for s in layout["slots"] if s["kind"] == "choice"]
    raw = {s["item"]: [sheet.bubble(x, y) for x, y in s["bubbles"]] for s in slots}

    # what an empty bubble looks like on this sheet, per printed letter (letters and printing add a little ink)
    by_label = {}
    for s in slots:
        for k, label in enumerate(s["labels"]):
            by_label.setdefault(label, []).append(raw[s["item"]][k][0])
    import numpy as np

    everything = [v for vals in by_label.values() for v in vals]
    # a low percentile, not the median: for letters used in few rows (T, F) half the bubbles may be marked
    overall = float(np.percentile(everything, 30)) if everything else 0.0
    base = {label: (float(np.percentile(vals, 30)) if len(vals) >= 8 else overall) for label, vals in by_label.items()}

    eff, ink = {}, {}  # item -> [score] after taking the empty bubble away; item -> [is there real ink?]
    for s in slots:
        eff[s["item"]], ink[s["item"]] = [], []
        for k, (mean, cov) in enumerate(raw[s["item"]]):
            m = max(0.0, mean - base[s["labels"][k]])
            eff[s["item"]].append(m)
            # real ink has truly dark pixels (a pencil stroke, a dot); the ghost of an erasure and a
            # smudge are grey all over and never are
            ink[s["item"]].append(m >= 0.30 or cov >= INK_COVERAGE)
    single = [eff[s["item"]] for s in slots if s["qtype"] != "ms"]
    mark, noise = _typical_levels(single)
    threshold = min(0.4, max(0.5 * mark, 2.5 * noise, 0.10))
    sheet.quality.update({"mark_level": round(mark, 3), "noise": round(noise, 3), "threshold": round(threshold, 3)})

    out = {}
    answered = 0
    for slot in layout["slots"]:
        entry = {"value": "", "fills": [], "states": [], "flag": "", "suggest": "", "kind": slot["kind"]}
        if slot["kind"] == "choice":
            f = eff[slot["item"]]
            has_ink = ink[slot["item"]]
            labels = slot["labels"]
            entry["fills"] = [round(v, 3) for v in f]
            states = [""] * len(f)
            inked = sorted((i for i in range(len(f)) if has_ink[i]), key=lambda i: -f[i])
            # grey with no truly dark pixel: a very light pencil mark, the ghost of an erasure, or a smudge
            grey = sorted((i for i in range(len(f)) if not has_ink[i] and f[i] >= max(0.07, 2.5 * noise)), key=lambda i: -f[i])
            usual = 0.6 * threshold  # how dark a mark may be before it is "lighter than usual"
            if slot["qtype"] == "ms":
                for i in inked:
                    states[i] = "marked" if f[i] >= usual else "faint"
                for i in grey:
                    states[i] = "faint"
                entry["value"] = "".join(labels[i] for i in sorted(inked))
                if grey or any(f[i] < usual for i in inked):
                    entry["flag"] = "faint"  # a light mark among others: an erasure, or just light pencil?
                    entry["suggest"] = "".join(labels[i] for i in sorted(inked + grey))
            elif inked:
                first = inked[0]
                states[first] = "marked" if f[first] >= usual else "faint"
                entry["value"] = labels[first]
                if len(inked) > 1:
                    second = inked[1]
                    ratio = f[second] / max(f[first], 1e-6)
                    if ratio >= 0.6 and f[second] >= usual * 0.6:
                        entry["flag"] = "multiple"
                        entry["suggest"] = labels[first]
                        entry["value"] = ""
                        states[second] = "marked"
                    elif ratio > 0.4:
                        entry["flag"] = "faint"  # a darker second bubble: an erasure that didn't come out?
                        states[second] = "faint"
                elif f[first] < max(0.10, 3 * noise):
                    entry["flag"] = "faint"  # alone, but barely there
            elif grey:
                states[grey[0]] = "faint"
                entry["value"] = labels[grey[0]]
                entry["flag"] = "faint"
            if entry["value"] or entry["flag"]:
                answered += 1
            entry["states"] = states
        elif slot["kind"] == "write":
            x, y, w, h = slot["box"]
            if sheet.box_ink(x, y, w, h) > 0.01:
                entry["flag"] = "write"
        out[slot["item"]] = entry
    sheet.quality["answered"] = answered
    sheet.quality["rows"] = len(slots)
    sheet.quality["blank"] = [s["n"] for s in slots if not out[s["item"]]["value"] and not out[s["item"]]["flag"]]
    return out
