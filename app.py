# app.py
"""
WhatsApp PDF Quadrant Combiner Bot using Twilio + Flask.

Flow:
- User sends PDFs to your Twilio WhatsApp number.
- Bot collects PDFs per user (expects 2 PDFs, each with 2 pages).
- When two PDFs are collected, bot sends a confirmation request.
- If user replies "YES", bot generates a single-page PDF with 4 quadrants and sends it back.
- If "NO", cancels and clears user state.

Requirements:
- Twilio account + WhatsApp sandbox or WhatsApp Business with a number.
- Publicly accessible webhook (ngrok for local testing).
- Environment variables in a .env file:
    TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_FROM, HOST_BASE_URL
"""

import os
import tempfile
import uuid
from pathlib import Path
from flask import Flask, request, send_file
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
import requests
import fitz  # PyMuPDF
from dotenv import load_dotenv

load_dotenv()

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_FROM = os.environ.get("TWILIO_WHATSAPP_FROM")  # e.g. 'whatsapp:+1415XXXXXXX'
HOST_BASE_URL = os.environ.get("HOST_BASE_URL")  # e.g. https://<your-ngrok-id>.ngrok.io

if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_FROM, HOST_BASE_URL]):
    raise RuntimeError("Set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_FROM, HOST_BASE_URL in env")

twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

app = Flask(__name__)

# In-memory user session store
# For production, replace with persistent DB (Redis, DynamoDB, etc.)
user_sessions = {}
# Structure:
# user_sessions[from_number] = {
#    "files": [ { "path": "/tmp/..", "orig_name": "a.pdf" }, ... ],
#    "state": "collecting" | "awaiting_confirm"
# }

# ---- helper: combine function (4 pages -> 1 quadrant page) ----
def combine_two_pdfs_into_quad_paths(path1, path2, out_path):
    """Open path1 and path2, each expected to have >=2 pages, take first 2 pages from each,
    place in quadrants on single page and save to out_path"""
    doc1 = fitz.open(path1)
    doc2 = fitz.open(path2)
    if doc1.page_count < 2 or doc2.page_count < 2:
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

# ---- helper: send message back (text + optional media) ----
def send_whatsapp_message(to_whatsapp_number, body, media_url=None):
    if media_url:
        twilio_client.messages.create(
            from_=TWILIO_WHATSAPP_FROM,
            to=to_whatsapp_number,
            body=body,
            media_url=[media_url],
        )
    else:
        twilio_client.messages.create(from_=TWILIO_WHATSAPP_FROM, to=to_whatsapp_number, body=body)

# ---- route: serve generated files publicly (Twilio will fetch this URL to upload to user) ----
@app.route("/download/<file_id>", methods=["GET"])
def download_generated(file_id):
    temp_dir = Path(tempfile.gettempdir())
    fp = temp_dir / f"combined_{file_id}.pdf"
    if not fp.exists():
        return "Not found", 404
    return send_file(str(fp), as_attachment=True, download_name=f"combined_{file_id}.pdf")

# ---- Twilio webhook to receive incoming messages ----
@app.route("/webhook", methods=["POST"])
def webhook():
    # Parse Twilio form data
    from_number = request.values.get("From")  # e.g. 'whatsapp:+9199...'
    body = (request.values.get("Body") or "").strip()
    body_lower = body.lower()
    num_media = int(request.values.get("NumMedia", "0"))

    # Ensure session exists
    sess = user_sessions.setdefault(from_number, {"files": [], "state": "collecting"})

    resp = MessagingResponse()

    if num_media > 0:
        # Twilio provides media URLs as MediaUrl0, MediaContentType0, etc.
        for i in range(num_media):
            m_url = request.values.get(f"MediaUrl{i}")
            m_type = request.values.get(f"MediaContentType{i}", "")
            if "pdf" in m_type.lower():
                # download media to temp file
                r = requests.get(m_url)
                if r.status_code != 200:
                    resp.message("Failed to download attached file. Please try again.")
                    return str(resp)
                tmpf = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
                tmpf.write(r.content)
                tmpf.flush()
                tmpf.close()
                sess["files"].append({"path": tmpf.name, "orig_name": f"file_{len(sess['files'])+1}.pdf"})
                resp.message(f"Received PDF #{len(sess['files'])}.")
            else:
                resp.message("Received a non-PDF attachment — please send PDF files only.")
        # If two PDFs collected, ask for confirmation
        if len(sess["files"]) >= 2:
            sess["state"] = "awaiting_confirm"
            resp.message("Received two PDFs. Reply YES to confirm combine into a single page PDF, or NO to cancel.")
        return str(resp)

    # No media — treat as text commands
    if body_lower in ("yes", "y") and sess.get("state") == "awaiting_confirm" and len(sess["files"]) >= 2:
        try:
            path1 = sess["files"][0]["path"]
            path2 = sess["files"][1]["path"]
            file_id = uuid.uuid4().hex
            out_path = Path(tempfile.gettempdir()) / f"combined_{file_id}.pdf"
            combine_two_pdfs_into_quad_paths(path1, path2, str(out_path))
            file_url = f"{HOST_BASE_URL}/download/{file_id}"
            send_whatsapp_message(from_number, "Here is your combined PDF:", media_url=file_url)
            for f in sess["files"]:
                try:
                    Path(f["path"]).unlink(missing_ok=True)
                except Exception:
                    pass
            sess.clear()
            user_sessions.pop(from_number, None)
            return str(MessagingResponse())
        except Exception as e:
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

    # default reply/help
    resp.message("Hi — send me two PDFs (each with 2 pages). After I receive two, I'll ask you to confirm. Reply YES to proceed or NO to cancel.")
    return str(resp)

if __name__ == "__main__":
    # Run Flask dev server
    app.run(port=5000, debug=True)
