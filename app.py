"""
WhatsApp PDF Quadrant Combiner Bot (Flask + Twilio)

Single-file app:
- /webhook (POST) Twilio WhatsApp webhook
- /download/<file_id> serves generated PDF from tempdir
- /health for readiness
- In-memory sessions
- Authenticated media download (prefers TWILIO_API_KEY + secret)
- Twilio REST send with TwiML fallback
- combine_pdfs_to_quadrant_pdf uses helper functions so you can place each quadrant independently

Environment variables:
- TWILIO_ACCOUNT_SID
- TWILIO_AUTH_TOKEN (or TWILIO_API_KEY + TWILIO_API_SECRET)
- TWILIO_WHATSAPP_FROM (e.g. "whatsapp:+1415...")
- HOST_BASE_URL (public URL for /download links)
- DPI (optional, default 300)
"""
import os
import tempfile
import uuid
import logging
from pathlib import Path
from flask import Flask, request, send_file, jsonify
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
from twilio.base.exceptions import TwilioRestException
import requests
from pdf2image import convert_from_bytes
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from PIL import Image
from dotenv import load_dotenv

# Load local .env for development
load_dotenv()

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Config / env
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_API_KEY = os.environ.get("TWILIO_API_KEY")
TWILIO_API_SECRET = os.environ.get("TWILIO_API_SECRET")
TWILIO_WHATSAPP_FROM = os.environ.get("TWILIO_WHATSAPP_FROM")
HOST_BASE_URL = os.environ.get("HOST_BASE_URL")
DPI = int(os.environ.get("DPI", "300"))

if not (TWILIO_WHATSAPP_FROM and HOST_BASE_URL and TWILIO_ACCOUNT_SID):
    raise RuntimeError("Set TWILIO_ACCOUNT_SID, TWILIO_WHATSAPP_FROM and HOST_BASE_URL in env")

# Twilio client: prefer API key pair
if TWILIO_API_KEY and TWILIO_API_SECRET and TWILIO_ACCOUNT_SID:
    twilio_client = Client(TWILIO_API_KEY, TWILIO_API_SECRET, TWILIO_ACCOUNT_SID)
    logger.info("Using Twilio API Key auth")
else:
    twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    logger.info("Using Twilio Account SID + Auth Token auth")

app = Flask(__name__)

# In-memory sessions
user_sessions = {}

# Geometry helpers
def mm_to_pt(value_mm):
    return value_mm * mm

# --- Image rendering & placement helpers ---

def render_page_images(pdf_bytes: bytes, dpi: int = DPI):
    imgs = convert_from_bytes(pdf_bytes, dpi=dpi, first_page=1, last_page=2)
    if len(imgs) < 2:
        raise ValueError("Each PDF must have at least 2 pages")
    return imgs

def place_image_on_canvas(cnv: canvas.Canvas, pil_img: Image.Image,
                          quadrant_index: int,
                          zoom: float = 1.0,
                          anchor_top_left: bool = True):
    quad_w_mm = 99.1
    quad_h_mm = 139.0
    positions_mm = [
        (7.0, 12.0),    # A (top-left)
        (100.0, 14.0),  # B (top-right)
        (7.0, 150.0),   # C (bottom-left)
        (100.0, 152.0), # D (bottom-right)
    ]
    if quadrant_index < 0 or quadrant_index > 3:
        raise ValueError("quadrant_index must be 0..3")

    x_mm, y_mm = positions_mm[quadrant_index]
    base_w_pt = mm_to_pt(quad_w_mm)
    base_h_pt = mm_to_pt(quad_h_mm)
    target_w_pt = base_w_pt * zoom
    target_h_pt = base_h_pt * zoom

    page_w_pt, page_h_pt = A4

    if not anchor_top_left:
        center_x_mm = x_mm + (quad_w_mm / 2.0)
        center_y_mm = y_mm + (quad_h_mm / 2.0)
        center_x_pt = mm_to_pt(center_x_mm)
        x_pt = center_x_pt - (target_w_pt / 2.0)
        y_pt = page_h_pt - mm_to_pt(center_y_mm) - (target_h_pt / 2.0)
    else:
        x_pt = mm_to_pt(x_mm)
        y_pt = page_h_pt - mm_to_pt(y_mm) - target_h_pt

    if pil_img.mode not in ("RGB", "RGBA"):
        pil_img = pil_img.convert("RGB")

    from io import BytesIO
    buf = BytesIO()
    pil_img.save(buf, format="PNG")
    buf.seek(0)
    img_reader = ImageReader(buf)

    cnv.drawImage(img_reader, x_pt, y_pt,
                  width=target_w_pt, height=target_h_pt,
                  preserveAspectRatio=True, anchor='sw')
    buf.close()

def combine_pdfs_to_quadrant_pdf(pdf_bytes1: bytes, pdf_bytes2: bytes, dpi: int = DPI) -> bytes:
    imgs1 = render_page_images(pdf_bytes1, dpi=dpi)
    imgs2 = render_page_images(pdf_bytes2, dpi=dpi)
    pages = [imgs1[0], imgs1[1], imgs2[0], imgs2[1]]  # A,B,C,D

    zoom_factors = [1.0, 1.125, 1.0, 1.125]  # B and D zoomed

    out_io = tempfile.SpooledTemporaryFile()
    cnv = canvas.Canvas(out_io, pagesize=A4)

    for idx, pil_img in enumerate(pages):
        place_image_on_canvas(cnv, pil_img, quadrant_index=idx,
                              zoom=zoom_factors[idx],
                              anchor_top_left=True)

    cnv.showPage()
    cnv.save()
    out_io.seek(0)
    result = out_io.read()
    out_io.close()
    return result

# --- Twilio helper ---
def send_whatsapp_message(to_whatsapp_number, body, media_url=None):
    try:
        if media_url:
            twilio_client.messages.create(from_=TWILIO_WHATSAPP_FROM, to=to_whatsapp_number,
                                          body=body, media_url=[media_url])
        else:
            twilio_client.messages.create(from_=TWILIO_WHATSAPP_FROM, to=to_whatsapp_number,
                                          body=body)
        logger.info("Sent message via Twilio REST API to %s", to_whatsapp_number)
        return True
    except TwilioRestException as tre:
        logger.warning("Twilio REST exception: %s (code=%s)", str(tre), getattr(tre, 'code', None))
        return False
    except Exception as e:
        logger.exception("Unexpected Twilio send error: %s", e)
        return False

# --- Routes ---
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
def download_generated(file_id):
    tmpdir = Path(tempfile.gettempdir())
    fp = tmpdir / f"combined_{file_id}.pdf"
    if not fp.exists():
        return "Not found", 404
    return send_file(str(fp), as_attachment=True,
                     download_name=f"combined_{file_id}.pdf")

@app.route("/webhook", methods=["POST"])
def webhook():
    logger.info("Incoming webhook: %s", dict(request.values))
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

    if num_media > 0:
        for i in range(num_media):
            m_url = request.values.get(f"MediaUrl{i}")
            m_type = request.values.get(f"MediaContentType{i}", "")
            logger.info("Media %s type=%s url=%s", i, m_type, m_url)

            if "pdf" in m_type.lower():
                auth_user = TWILIO_API_KEY or TWILIO_ACCOUNT_SID
                auth_pass = TWILIO_API_SECRET or TWILIO_AUTH_TOKEN
                try:
                    if auth_user and auth_pass:
                        r = requests.get(m_url, auth=(auth_user, auth_pass), timeout=30)
                    else:
                        r = requests.get(m_url, timeout=30)
                    if r.status_code == 200 and r.content and len(r.content) > 10:
                        tmpf = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
                        tmpf.write(r.content)
                        tmpf.flush()
                        tmpf.close()
                        sess["files"].append({"path": tmpf.name,
                                              "orig_name": f"file_{len(sess['files'])+1}.pdf"})
                        resp.message(f"Received PDF #{len(sess['files'])}.")
                    else:
                        resp.message("Failed to download attached file. Please try again.")
                        return str(resp)
                except requests.RequestException as ex:
                    logger.exception("Exception downloading media: %s", ex)
                    resp.message("Network error when downloading file. Please try again.")
                    return str(resp)
            else:
                resp.message("Please send PDF files only.")

        if len(sess["files"]) >= 2:
            sess["state"] = "awaiting_confirm"
            resp.message("Received two PDFs. Reply YES to confirm combine into a single A4 quadrant PDF, or NO to cancel.")
        return str(resp)

    if body_lower in ("yes", "y") and sess.get("state") == "awaiting_confirm" and len(sess["files"]) >= 2:
        try:
            with open(sess["files"][0]["path"], "rb") as f1, open(sess["files"][1]["path"], "rb") as f2:
                b1 = f1.read()
                b2 = f2.read()
            pdf_bytes = combine_pdfs_to_quadrant_pdf(b1, b2, dpi=DPI)
            file_id = uuid.uuid4().hex
            tmpdir = Path(tempfile.gettempdir())
            out_path = tmpdir / f"combined_{file_id}.pdf"
            with open(out_path, "wb") as outf:
                outf.write(pdf_bytes)
            file_url = f"{HOST_BASE_URL}/download/{file_id}"
            sent = send_whatsapp_message(from_number, "Here is your combined PDF:", media_url=file_url)
            if not sent:
                tw = MessagingResponse()
                m = tw.message("Here is your combined PDF (link):")
                m.media(file_url)
                for f in sess["files"]:
                    try:
                        Path(f["path"]).unlink(missing_ok=True)
                    except Exception:
                        pass
                user_sessions.pop(from_number, None)
                return str(tw)
            for f in sess["files"]:
                try:
                    Path(f["path"]).unlink(missing_ok=True)
                except Exception:
                    pass
            user_sessions.pop(from_number, None)
            return str(MessagingResponse())
        except Exception as e:
            logger.exception("Combine/send failed: %s", e)
            resp.message(f"Failed to combine PDFs: {e}")
            return str(resp)

    if body_lower in ("no", "n") and sess.get("state") == "awaiting_confirm":
        for f in sess["files"]:
            try:
                Path(f["path"]).unlink(missing_ok=True)
            except Exception:
                pass
        user_sessions.pop(from_number, None)
        resp.message("Cancelled — uploaded files removed. Send PDFs to start again.")
        return str(resp)

    resp.message("Hi — send me two PDFs (each with 2 pages). After I receive two, I'll ask you to confirm. Reply YES to proceed or NO to cancel.")
    return str(resp)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
