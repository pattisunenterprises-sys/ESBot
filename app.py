# app.py
"""
WhatsApp PDF Quadrant Combiner Bot (Flask + Twilio)
- Authenticated download of Twilio media URLs (uses TWILIO_API_KEY/API_SECRET or TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN)
- Collects two PDFs (each >= 2 pages), asks for confirmation, combines first 2 pages of each into 1 quadrant page PDF,
  places them precisely on an A4 canvas at the mm positions you specified, renders at high DPI (configurable),
  exposes it at /download/<id> and instructs Twilio to send that URL as media back to the user.

Drop-in replacement for your existing app.py. Retains all prior behavior:
 - /webhook POST (Twilio)
 - /download/<file_id> GET
 - / and /health endpoints
 - Twilio REST send with fallback to TwiML inline reply when REST send fails (e.g., quota 429)
 - In-memory sessions (same as before) — replace with persistent store for production if needed.

Environment variables required (set these in Render -> Environment):
  - TWILIO_ACCOUNT_SID
  - TWILIO_AUTH_TOKEN            (optional if using API key pair)
  - TWILIO_API_KEY               (optional, preferred)
  - TWILIO_API_SECRET            (optional, preferred)
  - TWILIO_WHATSAPP_FROM         e.g. whatsapp:+14155238886
  - HOST_BASE_URL                e.g. https://your-app-name.onrender.com
Optional:
  - COMBINE_DPI                  DPI for rendering source pages into quadrants (default 300)
"""
import os
import tempfile
import uuid
import logging
from pathlib import Path
from typing import Optional

from flask import Flask, request, send_file, jsonify
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
from twilio.base.exceptions import TwilioRestException
import requests
import fitz  # PyMuPDF
from dotenv import load_dotenv
from reportlab.lib.units import mm as _reportlab_mm  # used for mm->pt conversion

# Load .env locally (Render will use environment directly)
load_dotenv()

# Basic logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration / env
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_API_KEY = os.environ.get("TWILIO_API_KEY")
TWILIO_API_SECRET = os.environ.get("TWILIO_API_SECRET")
TWILIO_WHATSAPP_FROM = os.environ.get("TWILIO_WHATSAPP_FROM")
HOST_BASE_URL = os.environ.get("HOST_BASE_URL")
COMBINE_DPI = int(os.environ.get("COMBINE_DPI", "300"))  # default 300 DPI

# Validate critical envs (fail early if missing)
if not (TWILIO_WHATSAPP_FROM and HOST_BASE_URL and TWILIO_ACCOUNT_SID):
    raise RuntimeError("Set TWILIO_ACCOUNT_SID, TWILIO_WHATSAPP_FROM and HOST_BASE_URL in env")

# Create Twilio client: prefer API key pair, else fallback to account auth token
if TWILIO_API_KEY and TWILIO_API_SECRET and TWILIO_ACCOUNT_SID:
    twilio_client = Client(TWILIO_API_KEY, TWILIO_API_SECRET, TWILIO_ACCOUNT_SID)
    logger.info("Using Twilio API Key authentication")
elif TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    logger.info("Using Twilio Account SID + Auth Token authentication")
else:
    raise RuntimeError("Set TWILIO_ACCOUNT_SID and (TWILIO_API_KEY + TWILIO_API_SECRET) or TWILIO_AUTH_TOKEN in env")

app = Flask(__name__)

# In-memory user sessions (for production use a persistent store like Redis)
user_sessions = {}

# ----------------------------
# Helpers: mm <-> points
# ----------------------------
def mm_to_pt(mm_val: float) -> float:
    """
    Convert millimetres to PDF points (1 point = 1/72 inch).
    Using reportlab mm constant to avoid manual ratio.
    """
    return mm_val * _reportlab_mm  # reportlab.mm is points per mm

# ----------------------------
# High-quality combine function
# ----------------------------
def combine_two_pdfs_into_quad_paths(path1: str, path2: str, out_path: str, dpi: Optional[int] = None):
    """
    Combine first 2 pages of each input PDF into a single A4 PDF with four fixed quadrants.
    Quadrant size and positions (mm) are exact:
      - Quad A: (3mm, 10mm)
      - Quad B: (105mm, 10mm)
      - Quad C: (3mm, 149mm)
      - Quad D: (105mm, 149mm)
    Each quadrant size: 99.1mm x 139mm

    Renders source pages at `dpi` (default COMBINE_DPI env or 300) for high quality, converts to PNG stream,
    and inserts into the target rectangle preserving aspect ratio and centering.
    """
    dpi = dpi or COMBINE_DPI

    doc1 = fitz.open(path1)
    doc2 = fitz.open(path2)
    out_doc = None
    try:
        if doc1.page_count < 2 or doc2.page_count < 2:
            raise ValueError("Each PDF must have at least 2 pages")

        # A4 page in points
        PAGE_W_MM = 210.0
        PAGE_H_MM = 297.0
        page_w_pt = mm_to_pt(PAGE_W_MM)
        page_h_pt = mm_to_pt(PAGE_H_MM)

        # quadrant geometry
        QUAD_W_MM = 99.1
        QUAD_H_MM = 139.0
        POSITIONS_MM = {
            'A': (3.0, 10.0),
            'B': (105.0, 10.0),
            'C': (3.0, 149.0),
            'D': (105.0, 149.0)
        }

        # compute fitz.Rects in points (x0,y0,x1,y1). PyMuPDF uses a coordinate system where y increases downwards for insert_image
        quad_rects = []
        for k in ('A', 'B', 'C', 'D'):
            px_mm, py_mm = POSITIONS_MM[k]
            x0 = mm_to_pt(px_mm)
            y0 = mm_to_pt(py_mm)
            x1 = x0 + mm_to_pt(QUAD_W_MM)
            y1 = y0 + mm_to_pt(QUAD_H_MM)
            quad_rects.append(fitz.Rect(x0, y0, x1, y1))

        # create output doc and page
        out_doc = fitz.open()
        out_page = out_doc.new_page(width=page_w_pt, height=page_h_pt)

        # source pages: first 2 from doc1 then first 2 from doc2
        src_pages = [doc1.load_page(i) for i in range(2)] + [doc2.load_page(i) for i in range(2)]

        # matrix for rasterization based on DPI: scale = dpi / 72 (72 points per inch)
        scale = dpi / 72.0
        mat = fitz.Matrix(scale, scale)

        for i, src_page in enumerate(src_pages):
            pix = src_page.get_pixmap(matrix=mat, alpha=False)  # RGB no alpha
            img_bytes = pix.tobytes("png")  # lossless PNG

            # image pixel dims -> convert to points: pixels * (72/dpi)
            img_w_pts = pix.width * (72.0 / dpi)
            img_h_pts = pix.height * (72.0 / dpi)

            target = quad_rects[i]
            target_w = target.width
            target_h = target.height

            # scale to fit while preserving aspect ratio
            fit_scale = min(target_w / img_w_pts, target_h / img_h_pts)
            draw_w = img_w_pts * fit_scale
            draw_h = img_h_pts * fit_scale

            # center within target
            draw_x = target.x0 + (target_w - draw_w) / 2.0
            draw_y = target.y0 + (target_h - draw_h) / 2.0
            draw_rect = fitz.Rect(draw_x, draw_y, draw_x + draw_w, draw_y + draw_h)

            out_page.insert_image(draw_rect, stream=img_bytes, keep_proportion=True, overlay=False)

            # free pixmap
            pix = None

        out_doc.save(out_path)
    finally:
        try:
            doc1.close()
        except Exception:
            pass
        try:
            doc2.close()
        except Exception:
            pass
        if out_doc is not None:
            try:
                out_doc.close()
            except Exception:
                pass

# ----------------------------
# Twilio send helper with fallback support
# ----------------------------
def send_whatsapp_message(to_whatsapp_number: str, body: str, media_url: Optional[str] = None) -> bool:
    """
    Try to send via Twilio REST API. Return True on success, False on failure (caller may fallback to TwiML).
    """
    try:
        if media_url:
            twilio_client.messages.create(from_=TWILIO_WHATSAPP_FROM, to=to_whatsapp_number, body=body, media_url=[media_url])
        else:
            twilio_client.messages.create(from_=TWILIO_WHATSAPP_FROM, to=to_whatsapp_number, body=body)
        logger.info("Sent message via Twilio REST API to %s", to_whatsapp_number)
        return True
    except TwilioRestException as tre:
        logger.warning("Twilio REST exception while sending message: %s (code=%s, status=%s)", tre.msg, getattr(tre, "code", None), getattr(tre, "status", None))
        return False
    except Exception as e:
        logger.exception("Unexpected error while sending message via Twilio: %s", e)
        return False

# ----------------------------
# Routes
# ----------------------------
@app.route("/", methods=["GET"])
def root():
    return jsonify({
        "service": "pdf-whatsapp-quadrant-combiner",
        "status": "ok",
        "endpoints": {
            "webhook": "/webhook (POST)",
            "download": "/download/<file_id> (GET)",
            "health": "/health (GET)"
        }
    }), 200

@app.route("/health", methods=["GET"])
def health():
    return "OK", 200

@app.route("/download/<file_id>", methods=["GET"])
def download_generated(file_id: str):
    temp_dir = Path(tempfile.gettempdir())
    fp = temp_dir / f"combined_{file_id}.pdf"
    if not fp.exists():
        return "Not found", 404
    return send_file(str(fp), as_attachment=True, download_name=f"combined_{file_id}.pdf")

@app.route("/webhook", methods=["POST"])
def webhook():
    logger.info("Incoming webhook form data: %s", dict(request.values))

    from_number = request.values.get("From")
    body = (request.values.get("Body") or "").strip()
    body_lower = body.lower()
    num_media = int(request.values.get("NumMedia", "0"))

    if not from_number:
        resp = MessagingResponse()
        resp.message("Missing From number in request.")
        return str(resp)

    sess = user_sessions.setdefault(from_number, {"files": [], "state": "collecting"})
    resp = MessagingResponse()

    # Handle incoming media
    if num_media > 0:
        for i in range(num_media):
            m_url = request.values.get(f"MediaUrl{i}")
            m_type = request.values.get(f"MediaContentType{i}", "")
            logger.info("Incoming media: index=%s url=%s content_type=%s", i, m_url, m_type)

            if "pdf" in m_type.lower():
                auth_user = TWILIO_API_KEY or TWILIO_ACCOUNT_SID
                auth_pass = TWILIO_API_SECRET or TWILIO_AUTH_TOKEN
                try:
                    if auth_user and auth_pass:
                        r = requests.get(m_url, auth=(auth_user, auth_pass), timeout=30, allow_redirects=True)
                    else:
                        r = requests.get(m_url, timeout=30, allow_redirects=True)

                    logger.info("Media GET: status=%s len=%s", getattr(r, "status_code", None), len(getattr(r, "content", b"")))
                    if r.status_code == 200 and r.content and len(r.content) > 10:
                        tmpf = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
                        tmpf.write(r.content)
                        tmpf.flush()
                        tmpf.close()
                        sess["files"].append({"path": tmpf.name, "orig_name": f"file_{len(sess['files'])+1}.pdf"})
                        resp.message(f"Received PDF #{len(sess['files'])}.")
                    else:
                        logger.error("Failed to download media: status=%s headers=%s", getattr(r, "status_code", None), getattr(r, "headers", {}))
                        resp.message("Failed to download attached file. Please try again.")
                        return str(resp)
                except requests.exceptions.RequestException as ex:
                    logger.exception("Exception while downloading media: %s", ex)
                    resp.message("Failed to download attached file due to network error. Please try again.")
                    return str(resp)
            else:
                resp.message("Received a non-PDF attachment — please send PDF files only.")

        if len(sess["files"]) >= 2:
            sess["state"] = "awaiting_confirm"
            resp.message("Received two PDFs. Reply YES to confirm combine into a single page PDF, or NO to cancel.")
        return str(resp)

    # Handle confirmations YES / NO
    if body_lower in ("yes", "y") and sess.get("state") == "awaiting_confirm" and len(sess["files"]) >= 2:
        try:
            path1 = sess["files"][0]["path"]
            path2 = sess["files"][1]["path"]
            file_id = uuid.uuid4().hex
            out_path = Path(tempfile.gettempdir()) / f"combined_{file_id}.pdf"

            # Use the new high-quality combine (exact positions, DPI-controlled)
            combine_two_pdfs_into_quad_paths(path1, path2, str(out_path), dpi=COMBINE_DPI)

            file_url = f"{HOST_BASE_URL}/download/{file_id}"

            # Try REST API send first
            sent = send_whatsapp_message(from_number, "Here is your combined PDF:", media_url=file_url)
            if not sent:
                # Fallback: inline TwiML response with media
                logger.info("Falling back to TwiML inline response with media URL for %s", from_number)
                tw = MessagingResponse()
                m = tw.message("Here is your combined PDF (direct link):")
                m.media(file_url)
                # cleanup session files
                for f in sess["files"]:
                    try:
                        Path(f["path"]).unlink(missing_ok=True)
                    except Exception:
                        pass
                sess.clear()
                user_sessions.pop(from_number, None)
                return str(tw)

            # REST send succeeded - cleanup and return empty TwiML ack
            for f in sess["files"]:
                try:
                    Path(f["path"]).unlink(missing_ok=True)
                except Exception:
                    pass
            sess.clear()
            user_sessions.pop(from_number, None)
            return str(MessagingResponse())
        except Exception as e:
            logger.exception("Failed to combine/send PDFs: %s", e)
            resp.message(f"Failed to combine PDFs: {e}")
            return str(resp)

    if body_lower in ("no", "n") and sess.get("state") == "awaiting_confirm":
        for f in sess["files"]:
            try:
                Path(f["path"]).unlink(missing_ok=True)
            except Exception:
                pass
        sess.clear()
        user_sessions.pop(from_number, None)
        resp.message("Cancelled. Your uploaded files were removed. Send PDFs again to start over.")
        return str(resp)

    # default help message
    resp.message("Hi — send me two PDFs (each with 2 pages). After I receive two, I'll ask you to confirm. Reply YES to proceed or NO to cancel.")
    return str(resp)

# ----------------------------
# Entrypoint
# ----------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    # Run only for local testing; in production use gunicorn/uwsgi
    app.run(host="0.0.0.0", port=port, debug=False)
