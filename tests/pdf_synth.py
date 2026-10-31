def make_pdf(pages=2, lines_per_page=12):
    """A small valid PDF with numbered questions in Helvetica (written by hand: no PDF library needed)."""
    objs = []
    def add(b): objs.append(b); return len(objs)
    add(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(pages))
    add(f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>".encode())
    font_id = 3 + 2 * pages
    q = 1
    for p in range(pages):
        add(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {4 + 2 * p} 0 R /Resources << /Font << /F1 {font_id} 0 R >> >> >>".encode())
        text = ["BT /F1 14 Tf 50 740 Td 16 TL (Sample test, page %d) Tj T* T*" % (p + 1)]
        for _ in range(lines_per_page):
            text.append("(%d. Which answer is correct?   A. one   B. two   C. three   D. four) Tj T* T*" % q); q += 1
        text.append("ET")
        stream = "\n".join(text).encode()
        add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = b"%PDF-1.4\n"; offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out)); out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets: out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return out
