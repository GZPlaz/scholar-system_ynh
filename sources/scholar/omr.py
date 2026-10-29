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

SAMPLE_R = 1.35  # radius of the disc sampled inside each bubble
MARK_LEVEL = 0.42  # darkness (0 = paper, 1 = fiducial black) at which a bubble counts as filled
FAINT_LEVEL = 0.22  # between FAINT and MARK: probably an erasure or a light mark, ask a human


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


class OMRError(Exception):
    """A photo that cannot be read; the message is shown to the teacher."""


def available():
    try:
        import cv2  # noqa: F401
        import numpy  # noqa: F401
    except ImportError:
        return False
    return True


def _mm_to_px(v):
    return (v + MARGIN) * PX_PER_MM


def _find_fiducials(cv2, np, gray):
    h, w = gray.shape
    # Flatten uneven lighting: divide by a blurred "paper" estimate, then threshold globally.
    k = max(31, (max(h, w) // 12) | 1)
    background = cv2.morphologyEx(gray, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (k, k)))
    norm = cv2.divide(gray, background, scale=255)
    _, binary = cv2.threshold(cv2.GaussianBlur(norm, (5, 5), 0), 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    contours, _ = cv2.findContours(binary, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    short = min(h, w)
    min_side, max_side = 0.008 * short, 0.09 * max(h, w)
    candidates = []
    for c in contours:
        area = cv2.contourArea(c)
        if area < min_side * min_side or area > max_side * max_side:
            continue
        (cx, cy), (rw, rh), _ = cv2.minAreaRect(c)
        if rw == 0 or rh == 0 or not 0.6 < rw / rh < 1.66:
            continue
        if area / (rw * rh) < 0.82:  # squares fill their bounding box; filled bubbles don't
            continue
        approx = cv2.approxPolyDP(c, 0.06 * cv2.arcLength(c, True), True)
        if len(approx) != 4 or not cv2.isContourConvex(approx):
            continue
        mask = np.zeros_like(binary)
        cv2.drawContours(mask, [c], -1, 255, -1)
        if cv2.mean(binary, mask=mask)[0] < 0.85 * 255:  # must be solid, not an outline
            continue
        candidates.append((area, (cx, cy)))
    candidates.sort(key=lambda t: -t[0])
    candidates = candidates[:10]
    if len(candidates) < 4:
        raise OMRError(
            "The four black corner squares were not all found. Photograph the whole sheet, flat, "
            "with all four corners in view and no shadow across them."
        )
    best, best_area = None, 0.0
    from itertools import combinations

    for combo in combinations(candidates, 4):
        areas = [a for a, _ in combo]
        if max(areas) / min(areas) > 3.0:
            continue
        pts = np.array([p for _, p in combo], dtype=np.float32)
        hull = cv2.convexHull(pts)
        if len(hull) != 4:
            continue
        quad_area = cv2.contourArea(hull)
        if quad_area > best_area:
            best, best_area = pts, quad_area
    if best is None or best_area < 0.08 * h * w:
        raise OMRError("The corner squares could not be matched up. Retake the photo closer to the sheet, with all four corners visible.")
    centre = best.mean(axis=0)
    order = sorted(range(4), key=lambda i: math.atan2(best[i][1] - centre[1], best[i][0] - centre[0]))
    return [best[i] for i in order], norm  # clockwise in image coordinates


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


class ReadSheet:
    """A straightened, identified sheet image and helpers to sample it."""

    def __init__(self, image, sheet_id):
        import numpy as np

        self.image = image
        self.sheet_id = sheet_id
        # references: solid fiducial ink and blank paper
        self.black = min(_region_mean(image, -2, -2, 4, 4), _region_mean(image, FRAME_W - 2, FRAME_H - 2, 4, 4))
        self.white = float(np.percentile(image, 90))
        if self.white - self.black < 40:
            raise OMRError("The photo is too dark or too washed out to tell pencil from paper. Retake it in better light.")
        r = int(round(SAMPLE_R * PX_PER_MM))
        yy, xx = np.mgrid[-r : r + 1, -r : r + 1]
        self._disc = (xx * xx + yy * yy) <= r * r
        self._r = r

    def darkness(self, value):
        return max(0.0, min(1.0, (self.white - value) / (self.white - self.black)))

    def bubble(self, x_mm, y_mm):
        """Darkness of a bubble, searching ±0.6 mm for paper curl and printer offset."""
        best = 0.0
        step = int(round(0.6 * PX_PER_MM))
        cx0, cy0 = int(round(_mm_to_px(x_mm))), int(round(_mm_to_px(y_mm)))
        r = self._r
        for dy in (-step, 0, step):
            for dx in (-step, 0, step):
                cx, cy = cx0 + dx, cy0 + dy
                patch = self.image[cy - r : cy + r + 1, cx - r : cx + r + 1]
                if patch.shape != self._disc.shape:
                    continue
                best = max(best, self.darkness(float(patch[self._disc].mean())))
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
    black = min(_region_mean(img, -2, -2, 4, 4), _region_mean(img, FRAME_W - 2, -2, 4, 4))
    white = float(__import__("numpy").percentile(img, 90))
    if white - black < 40:
        return None
    threshold = (white + black) / 2
    inset = CODE_CELL * 0.2
    bits = [1 if _region_mean(img, x + inset, y + inset, CODE_CELL - 2 * inset, CODE_CELL - 2 * inset) < threshold else 0 for x, y in code_cells()]
    return decode_bits(bits)


MAX_PIXELS = 50_000_000


def read_photo(data):
    """Find, straighten and identify an answer sheet in a photo. Returns a ReadSheet."""
    try:
        import cv2
        import numpy as np
    except ImportError as exc:  # pragma: no cover - depends on the server
        raise OMRError("Scanning is not available on this server (OpenCV is not installed).") from exc
    raw = np.frombuffer(data, dtype=np.uint8)
    gray = cv2.imdecode(raw, cv2.IMREAD_GRAYSCALE)
    if gray is None:
        raise OMRError("This file is not an image Scholar can read. Use JPEG or PNG.")
    h, w = gray.shape
    if h * w > MAX_PIXELS:
        raise OMRError("The image is too large.")
    scale = 1800.0 / max(h, w)
    if scale < 1:
        gray = cv2.resize(gray, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    corners, norm = _find_fiducials(cv2, np, gray)
    for start in range(4):  # the photo may be rotated any way round; the code tells us which
        ordered = corners[start:] + corners[:start]
        warped = _warp(cv2, np, norm, ordered)
        sheet_id = _read_code(warped)
        if sheet_id is not None:
            return ReadSheet(warped, sheet_id)
    raise OMRError(
        "The sheet code (the row of small squares along the top edge) could not be read. "
        "Make sure it is in the photo, in focus, and not covered."
    )


def read_answers(sheet, layout):
    """Interpret the marks for one printed page layout.

    Returns {item_id: {"value": str, "fills": [...], "flag": str}} where value is what a
    teacher would type on the paper-entry form ('B', 'AC', 'T', or '').
    flag is '' when the reading is certain, otherwise one of: multiple, faint, write.
    """
    out = {}
    for slot in layout["slots"]:
        entry = {"value": "", "fills": [], "flag": "", "kind": slot["kind"]}
        if slot["kind"] == "choice":
            fills = [round(sheet.bubble(x, y), 3) for x, y in slot["bubbles"]]
            entry["fills"] = fills
            labels = slot["labels"]
            marked = [i for i, f in enumerate(fills) if f >= MARK_LEVEL]
            faint = [i for i, f in enumerate(fills) if FAINT_LEVEL <= f < MARK_LEVEL]
            if slot["qtype"] == "ms":
                entry["value"] = "".join(labels[i] for i in marked)
                if faint:
                    entry["flag"] = "faint"
            else:
                if len(marked) == 1:
                    entry["value"] = labels[marked[0]]
                elif len(marked) > 1:
                    ranked = sorted(marked, key=lambda i: -fills[i])
                    if fills[ranked[0]] - fills[ranked[1]] >= 0.3:
                        entry["value"] = labels[ranked[0]]  # a clear mark plus a half-erased one
                        entry["flag"] = "faint"
                    else:
                        entry["flag"] = "multiple"
                if faint and not entry["flag"]:
                    entry["flag"] = "faint"
        elif slot["kind"] == "write":
            x, y, w, h = slot["box"]
            if sheet.box_ink(x, y, w, h) > 0.01:
                entry["flag"] = "write"
        out[slot["item"]] = entry
    return out
