"""Draw answer sheets and fake phone photos of them, for testing the OMR reader."""
import cv2
import numpy as np

from scholar import omr


def render_sheet(layout, sheet_id, marks, ppm=6, partial=None):
    """marks: {item_id: [bubble indexes to fill]}; partial: {item_id: {idx: fill_fraction}}."""
    off = omr.FID / 2 + 6
    W = int((omr.FRAME_W + 2 * off) * ppm)
    H = int((omr.FRAME_H + 2 * off) * ppm)
    img = np.full((H, W), 255, np.uint8)
    P = lambda v: int(round((v + off) * ppm))  # noqa: E731
    for fx, fy in ((0, 0), (omr.FRAME_W, 0), (omr.FRAME_W, omr.FRAME_H), (0, omr.FRAME_H)):
        cv2.rectangle(img, (P(fx - omr.FID / 2), P(fy - omr.FID / 2)), (P(fx + omr.FID / 2), P(fy + omr.FID / 2)), 0, -1)
    for bit, (x, y) in zip(omr.encode_sheet_id(sheet_id), omr.code_cells()):
        if bit:
            cv2.rectangle(img, (P(x), P(y)), (P(x + omr.CODE_CELL), P(y + omr.CODE_CELL)), 0, -1)
    cv2.putText(img, "Unit 1 Check-in  answer sheet", (P(10), P(20)), cv2.FONT_HERSHEY_SIMPLEX, 0.9, 30, 2)
    for slot in layout["slots"]:
        cv2.putText(img, str(slot["n"]), (P(slot["x"]), P(slot["y"] + 4.5)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, 60, 1)
        if slot["kind"] == "choice":
            for k, (bx, by) in enumerate(slot["bubbles"]):
                cv2.circle(img, (P(bx), P(by)), int(omr.BUBBLE_D / 2 * ppm), 90, 1)
                cv2.putText(img, slot["labels"][k], (P(bx - 0.8), P(by + 0.9)), cv2.FONT_HERSHEY_SIMPLEX, 0.25, 170, 1)
                frac = (partial or {}).get(slot["item"], {}).get(k)
                if k in marks.get(slot["item"], []) or frac:
                    shade = 45 if not frac else int(255 - 210 * frac)
                    cv2.circle(img, (P(bx), P(by)), int((omr.BUBBLE_D / 2 - 0.3) * ppm), shade, -1)
        elif slot["kind"] == "write":
            x, y, w, h = slot["box"]
            cv2.rectangle(img, (P(x), P(y)), (P(x + w), P(y + h)), 90, 1)
            if slot["item"] in marks:
                cv2.putText(img, "42", (P(x + 1), P(y + 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, 30, 2)
    return img


def fake_photo(sheet, rotation=0, seed=0, size=(3000, 4000), quality=80):
    """Put the sheet on a desk, skewed, unevenly lit, noisy, blurred, JPEG-compressed."""
    rng = np.random.default_rng(seed)
    h, w = sheet.shape
    pw, ph = size
    scale = 0.75 * min(pw / w, ph / h)
    cw, ch = w * scale, h * scale
    ox, oy = (pw - cw) / 2, (ph - ch) / 2
    jitter = lambda: rng.uniform(-0.06, 0.06) * cw  # noqa: E731
    dst = np.float32([[ox + jitter(), oy + jitter()], [ox + cw + jitter(), oy + jitter()], [ox + cw + jitter(), oy + ch + jitter()], [ox + jitter(), oy + ch + jitter()]])
    src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    M = cv2.getPerspectiveTransform(src, dst)
    desk = np.full((ph, pw), 110, np.uint8)
    photo = cv2.warpPerspective(sheet, M, (pw, ph), borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    mask = cv2.warpPerspective(np.full_like(sheet, 255), M, (pw, ph))
    photo = np.where(mask > 0, photo, desk).astype(np.float32)
    yy, xx = np.mgrid[0:ph, 0:pw]
    light = 0.62 + 0.38 * (xx / pw) * (0.6 + 0.4 * yy / ph)  # a lamp off to one side
    photo = photo * light + rng.normal(0, 6, photo.shape)
    photo = cv2.GaussianBlur(np.clip(photo, 0, 255).astype(np.uint8), (5, 5), 0)
    for _ in range(rotation):
        photo = cv2.rotate(photo, cv2.ROTATE_90_CLOCKWISE)
    ok, buf = cv2.imencode(".jpg", photo, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    return buf.tobytes()
