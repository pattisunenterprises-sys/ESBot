"""
app.py

Flask app that generates a high-quality PDF containing four quadrants
each sized 99.1mm x 139mm at positions (mm, bottom-left origin):
  - Quad A: (3, 10)
  - Quad B: (105, 10)
  - Quad C: (3, 149)
  - Quad D: (105, 149)

Pre-deployment geometry checks (asserts) run at import time to ensure
the quads fit on A4 and do not overlap. No geometric fouling checks
are performed at runtime when generating PDFs (per your request).

Requires:
  pip install flask reportlab
"""

import io
import os
from flask import Flask, send_file, request, jsonify
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader

app = Flask(__name__)

# ---------------------------
# Configuration (mm units)
# ---------------------------
PAGE_W_MM = 210.0
PAGE_H_MM = 297.0

QUAD_W_MM = 99.1
QUAD_H_MM = 139.0

POSITIONS_MM = {
    'A': (3.0, 10.0),
    'B': (105.0, 10.0),
    'C': (3.0, 149.0),
    'D': (105.0, 149.0)
}

# ---------------------------
# Pre-deployment geometry checks (run once on import)
# ---------------------------
def _rect(name, x_mm, y_mm, w_mm, h_mm):
    return (name, x_mm, y_mm, w_mm, h_mm)

def _rects_from_config():
    rects = []
    for name, (x_mm, y_mm) in POSITIONS_MM.items():
        rects.append(_rect(name, x_mm, y_mm, QUAD_W_MM, QUAD_H_MM))
    return rects

def _intersect(r1, r2):
    # r: (name, x, y, w, h) in mm; origin bottom-left
    _, x1, y1, w1, h1 = r1
    _, x2, y2, w2, h2 = r2
    if (x1 + w1) <= x2 or (x2 + w2) <= x1:
        return False
    if (y1 + h1) <= y2 or (y2 + h2) <= y1:
        return False
    return True

def _run_prechecks():
    rects = _rects_from_config()
    # check bounds
    for name, x, y, w, h in rects:
        assert x >= 0 and y >= 0, f"{name} has negative origin: {(x,y)}"
        assert (x + w) <= PAGE_W_MM + 1e-6, f"{name} exceeds page width: x+w = {x+w} mm > {PAGE_W_MM} mm"
        assert (y + h) <= PAGE_H_MM + 1e-6, f"{name} exceeds page height: y+h = {y+h} mm > {PAGE_H_MM} mm"
    # check overlaps (touching edges allowed)
    n = len(rects)
    for i in range(n):
        for j in range(i+1, n):
            if _intersect(rects[i], rects[j]):
                name_i = rects[i][0]; name_j = rects[j][0]
                raise AssertionError(f"Rectangles {name_i} and {name_j} overlap (pre-deployment config error)")

# Execute pre-deployment checks now (will raise on import if invalid)
_run_prechecks()

# ---------------------------
# Utility helpers
# ---------------------------
def mm_to_pt(value_mm: float) -> float:
    """Convert mm to PDF points using reportlab's mm unit"""
    return value_mm * mm

def _draw_quadrants_on_canvas(c: canvas.Canvas, images=None, stroke_width_pt=0.5):
    """
    Draws the four quadrant frames and embeds images (if provided).
    images: optional dict mapping 'A'|'B'|'C'|'D' to a filesystem path.
    NOTE: No geometric fouling checks at runtime (prechecks already done).
    """
    # page size in pts
    page_w_pt, page_h_pt = A4

    # quad size in pts
    qw_pt = mm_to_pt(QUAD_W_MM)
    qh_pt = mm_to_pt(QUAD_H_MM)

    # draw each rect and optional image
    c.setLineWidth(stroke_width_pt)
    c.setFont("Helvetica-Bold", 10)

    for name, (px_mm, py_mm) in POSITIONS_MM.items():
        x_pt = mm_to_pt(px_mm)
        y_pt = mm_to_pt(py_mm)
        # draw border
        c.rect(x_pt, y_pt, qw_pt, qh_pt)
        # label (top-left inside rect)
        label_x = x_pt + mm_to_pt(2)
        label_y = y_pt + qh_pt - mm_to_pt(6)
        c.drawString(label_x, label_y, f"Quad {name} ({px_mm:.1f}mm, {py_mm:.1f}mm)")
        # embed image if provided
        if images and images.get(name):
            img_path = images[name]
            try:
                img = ImageReader(img_path)
                iw, ih = img.getSize()
                # compute maximum drawable area with a small margin
                margin_pt = mm_to_pt(2)
                max_w = qw_pt - 2 * margin_pt
                max_h = qh_pt - 2 * margin_pt
                scale = min(max_w / iw, max_h / ih)
                draw_w = iw * scale
                draw_h = ih * scale
                draw_x = x_pt + (qw_pt - draw_w) / 2
                draw_y = y_pt + (qh_pt - draw_h) / 2
                # preserveAspectRatio is True by default when supplying width/height that match scale
                c.drawImage(img, draw_x, draw_y, draw_w, draw_h, preserveAspectRatio=True, mask='auto')
            except Exception as e:
                # if image fails, annotate inside the quad with the error
                c.setFont("Helvetica", 8)
                err_x = x_pt + mm_to_pt(4)
                err_y = y_pt + mm_to_pt(4)
                c.drawString(err_x, err_y, f"Image load error: {e}")
                c.setFont("Helvetica-Bold", 10)  # restore for next label

# ---------------------------
# Endpoint
# ---------------------------
@app.route('/generate-quadrants', methods=['GET', 'POST'])
def generate_quadrants():
    """
    Generates and returns a PDF containing the configured four quadrants.
    Optional JSON POST body structure:
    {
      "images": {
         "A": "/absolute/or/relative/path/to/highresA.jpg",
         "B": "/path/to/highresB.png",
         "C": "/path/to/highresC.tif",
         "D": "/path/to/highresD.jpg"
      }
    }
    Notes:
      - image paths must be accessible to the running process (local filesystem).
      - This route does not perform geometry checks at runtime.
      - If an image fails to load, an annotation will appear inside the affected quad.
    """
    # Attempt to parse JSON body; allow GET as a convenience (no images)
    data = request.get_json(silent=True) or {}
    images = data.get('images') or {}

    # Build PDF in memory
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    # Draw content
    _draw_quadrants_on_canvas(c, images=images)
    c.showPage()
    c.save()
    buffer.seek(0)

    # Return PDF as attachment
    return send_file(buffer, mimetype='application/pdf', as_attachment=True, download_name='quadrants.pdf')

# ---------------------------
# Preserve existing behavior: expose root route if needed
# ---------------------------
@app.route('/')
def index():
    return jsonify({
        "service": "quadrant-pdf-generator",
        "endpoints": {
            "generate_quadrants": "/generate-quadrants (GET or POST with optional JSON body)"
        }
    })

# ---------------------------
# App entrypoint
# ---------------------------
if __name__ == '__main__':
    # Use env var PORT if set (useful for many PaaS), otherwise 5000
    port = int(os.environ.get('PORT', 5000))
    # In production, run via WSGI server (gunicorn/uwsgi). This is for local testing.
    app.run(host='0.0.0.0', port=port, debug=False)
