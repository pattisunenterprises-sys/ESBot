# app.py
"""
WhatsApp PDF Quadrant Combiner Bot (Flask + Twilio)
- Authenticated download of Twilio media URLs (uses TWILIO_API_KEY/API_SECRET or TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN)
- Collects two PDFs (each >= 2 pages), asks for confirmation, combines first 2 pages of each into 1 quadrant page PDF,
  exposes it at /download/<id> and instructs Twilio to send that URL as media back to the user.

Environment variables required (set these in Render -> Environment):
  - TWILIO_ACCOUNT_SID
  - TWILIO_AUTH_TOKEN            (optional if using API key pair)
  - TWILIO_API_KEY               (optional, preferred)
  - TWILIO_API_SECRET            (optional, preferred)
  - TWILIO_WHATSAPP_FROM         e.g. whatsapp:+14155238886
  - HOST_BASE_URL                e.g. https://your-app-name.onrender.com
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
import fitz  # PyMuPDF
from dotenv import load_dotenv

# Load .env locally (Render will use environment directly)
load_dotenv()

# Basic logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load config
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_API_KEY = os.environ.get("TWILIO_API_KEY")
TWILIO_API_SECRET = os.environ.get("TWILIO_API_SECRET")
TWILIO_WHATSAPP_FROM = os.environ.get("TWILIO_WHATSAPP_FROM")
HOST_BASE_URL = os.environ.get("HOST_BASE_URL")

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

# Health route
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

# Serve generated files
@app.route("/download/<file_id>", methods=["GET"])
def download_generated(file_id):
    temp_dir = Path(tempfile.gettempdir())
    fp = temp_dir / f"combined_{file_id}.pdf"
    if not fp.exists():
        return "Not found", 404
    return send_file(str(fp), as_attachment=True, download_name=f"combined_{file_id}.pdf")

# Combine function (from your working code)
def combine_two_pdfs_into_quad_paths(path1, path2, out_path):
    doc1 = fitz.open(path1)
    doc2 = fitz.open(path2)
    if doc1.page_count < 2 or doc2.page_count < 2:
        doc1.close()
        doc2.close()
        raise ValueError("Each PDF must have at least 2 pages")
    pages = [doc1.load_page(i) for i in range(2)] + [doc2.load_page(i) for i in range(2)]

    ref_rect = pages[0].rect
    pw, ph = ref_rect.width, ref_rect.height
    final_w, final_h = pw * 2, ph * 2

    out_doc = fitz.open()
    out_page = out_doc.new_page(width=final_w, height=final_h)

    quad_positions = [
        fitz.Rect(0, 0, final_w / 2, final_h / 2),
        fitz.Rect(final_w / 2, 0, final_w, final_h / 2),
        fitz.Rect(0, final_h / 2, final_w / 2, final_h),
        fitz.Rect(final_w / 2, final_h / 2, final_w, final_h),
    ]

    for i, src_page in enumerate(pages):
        mat = fitz.Matrix(1, 1)
        pix = src_page.get_pixmap(matrix=mat, alpha=False)
        img_bytes = pix.tobytes()
        target_rect = quad_positions[i]
        out_page.insert_image(target_rect, stream=img_bytes)

    out_doc.save(out_path)
    out_doc.close()
    doc1.close()
    doc2.close()

# Helper to send WhatsApp message via Twilio (text + optional media_url)
def send_whatsapp_message(to_whatsapp_number, body, media_url=None):
    """
    Return True on success, False on failure (Twilio error or other exception).
    Caller may fallback to TwiML if False.
    """
    try:
        if media_url:
            twilio_client.messages.create(from_=TWILIO_WHATSAPP_FROM, to=to_whatsapp_number, body=body, media_url=[media_url])
        else:
            twilio_client.messages.create(from_=TWILIO_WHATSAPP_FROM, to=to_whatsapp_number, body=body)
        logger.info("Sent message via Twilio REST API to %s", to_whatsapp_number)
        return True
    except TwilioRestException as tre:
        # Twilio-specific exceptions (e.g., 429 quota). Log and return False so caller can fallback.
        logger.warning("Twilio REST exception while sending message: %s (code=%s, status=%s)", tre.msg, getattr(tre, "code", None), getattr(tre, "status", None))
        return False
    except Exception as e:
        logger.exception("Unexpected error while sending message via Twilio: %s", e)
        return False

# Webhook: receive messages from Twilio (WhatsApp)
@app.route("/webhook", methods=["POST"])
def webhook():
    # Basic logging of incoming form for debugging
    logger.info("Incoming webhook form data: %s", dict(request.values))

    from_number = request.values.get("From")  # e.g. 'whatsapp:+9199...'
    body = (request.values.get("Body") or "").strip()
    body_lower = body.lower()
    num_media = int(request.values.get("NumMedia", "0"))

    if not from_number:
        # guard
        resp = MessagingResponse()
        resp.message("Missing From number in request.")
        return str(resp)

    sess = user_sessions.setdefault(from_number, {"files": [], "state": "collecting"})
    resp = MessagingResponse()

    # 1) Handle incoming media attachments
    if num_media > 0:
        for i in range(num_media):
            m_url = request.values.get(f"MediaUrl{i}")
            m_type = request.values.get(f"MediaContentType{i}", "")
            logger.info("Incoming media: index=%s url=%s content_type=%s", i, m_url, m_type)

            if "pdf" in m_type.lower():
                # Attempt authenticated download (preferred). Use API key pair if available, otherwise account SID+auth token
                auth_user = TWILIO_API_KEY or TWILIO_ACCOUNT_SID
                auth_pass = TWILIO_API_SECRET or TWILIO_AUTH_TOKEN

                try:
                    # Use auth by default (Twilio media URLs require it)
                    if auth_user and auth_pass:
                        r = requests.get(m_url, auth=(auth_user, auth_pass), timeout=30, allow_redirects=True)
                    else:
                        # fallback to public GET (rare)
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

        # After handling attachments, if we have two PDFs ask for confirm
        if len(sess["files"]) >= 2:
            sess["state"] = "awaiting_confirm"
            resp.message("Received two PDFs. Reply YES to confirm combine into a single page PDF, or NO to cancel.")
        return str(resp)

    # 2) Handle text commands (confirmation)
    if body_lower in ("yes", "y") and sess.get("state") == "awaiting_confirm" and len(sess["files"]) >= 2:
        try:
            path1 = sess["files"][0]["path"]
            path2 = sess["files"][1]["path"]
            file_id = uuid.uuid4().hex
            out_path = Path(tempfile.gettempdir()) / f"combined_{file_id}.pdf"
            combine_two_pdfs_into_quad_paths(path1, path2, str(out_path))
            file_url = f"{HOST_BASE_URL}/download/{file_id}"

            # Try to send via Twilio REST API first (normal path).
            sent = send_whatsapp_message(from_number, "Here is your combined PDF:", media_url=file_url)
            if not sent:
                # Fallback: reply inline with TwiML (so Twilio will send this message as response to the webhook)
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

            # If sent successfully via REST API, clean up session and return empty TwiML (ack)
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


if __name__ == "__main__":
    # Use port from environment (Render sets PORT), otherwise 5000 locally
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
