"""
WhatsApp PDF Quadrant Combiner Bot (Flask + Twilio)

See the chat for a short summary of what changed. This file is the updated, drop-in Flask app that
implements precise A4 quadrant placement, DPI-configurable rendering to PNG streams, Twilio REST send
with TwiML fallback, authenticated media download, in-memory sessions, and /download/<id> serving.

Set environment variables on Render as described in the file header and in the conversation.
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

# Helper mm -> points (reportlab uses points)
def mm_to_pt(value_mm):
    return value_mm * mm

# Combine function: takes two bytes objects (PDF content) and returns bytes of the combined A4 PDF
def combine_pdfs_to_quadrant_pdf(pdf_bytes1: bytes, pdf_bytes2: bytes, dpi: int = DPI) -> bytes:
    """Render first two pages of each PDF at `dpi`, place them as PNGs into a single A4 page
    at the exact mm positions and sizes specified.

    Quadrant placement coordinates are interpreted as TOP-LEFT-origin (x_mm, y_mm).
    """
    # Import ImageReader here to avoid top-level import issues in some environments
    from io import BytesIO
    from reportlab.lib.utils import ImageReader

    # Render first two pages of each PDF
    imgs1 = convert_from_bytes(pdf_bytes1, dpi=dpi, first_page=1, last_page=2)
    imgs2 = convert_from_bytes(pdf_bytes2, dpi=dpi, first_page=1, last_page=2)

    if len(imgs1) < 2 or len(imgs2) < 2:
        raise ValueError("Each PDF must have at least 2 pages")

    pages = [imgs1[0], imgs1[1], imgs2[0], imgs2[1]]  # A, B, C, D order

    # Target sizes and positions in mm (from spec)
    quad_w_mm = 99.1
    quad_h_mm = 139.0
    positions_mm = [
        (3.0, 10.0),    # A (top-left)
        (105.0, 10.0),  # B (top-right)
        (3.0, 149.0),   # C (bottom-left)
        (105.0, 149.0), # D (bottom-right)
    ]

    # Create PDF in memory
    out_io = tempfile.SpooledTemporaryFile()
    c = canvas.Canvas(out_io, pagesize=A4)
    page_w_pt, page_h_pt = A4

    # For each page image, convert to PNG bytes (rendering already at DPI), then draw at location
    for img, (x_mm, y_mm) in zip(pages, positions_mm):
        # Ensure image is RGB
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")

        # Save PNG to bytes
        img_buf = BytesIO()
        img.save(img_buf, format="PNG")
        img_buf.seek(0)

        # Wrap bytes into an ImageReader which ReportLab accepts
        img_reader = ImageReader(img_buf)

        # Compute target dimensions in points
        target_w_pt = mm_to_pt(quad_w_mm)
        target_h_pt = mm_to_pt(quad_h_mm)

        # Compute x in points from left
        x_pt = mm_to_pt(x_mm)
        # Convert y from TOP-LEFT reference to bottom-left (reportlab):
        y_pt = page_h_pt - mm_to_pt(y_mm) - target_h_pt

        # draw the PNG, scale to target width/height (in points)
        c.drawImage(img_reader, x_pt, y_pt, width=target_w_pt, height=target_h_pt, preserveAspectRatio=True, anchor='sw')

        img_buf.close()

    c.showPage()
    c.save()

    out_io.seek(0)
    pdf_bytes = out_io.read()
    out_io.close()
    return pdf_bytes


# Helper to send WhatsApp message via Twilio REST API
def send_whatsapp_message(to_whatsapp_number, body, media_url=None):
    try:
        if media_url:
            twilio_client.messages.create(from_=TWILIO_WHATSAPP_FROM, to=to_whatsapp_number, body=body, media_url=[media_url])
        else:
            twilio_client.messages.create(from_=TWILIO_WHATSAPP_FROM, to=to_whatsapp_number, body=body)
        logger.info("Sent message via Twilio REST API to %s", to_whatsapp_number)
        return True
    except TwilioRestException as tre:
        logger.warning("Twilio REST exception: %s (code=%s)", str(tre), getattr(tre, 'code', None))
        return False
    except Exception as e:
        logger.exception("Unexpected Twilio send error: %s", e)
        return False


# Routes
@app.route('/', methods=['GET'])
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


@app.route('/health', methods=['GET'])
def health():
    return 'OK', 200


@app.route('/download/<file_id>', methods=['GET'])
def download_generated(file_id):
    tmpdir = Path(tempfile.gettempdir())
    fp = tmpdir / f"combined_{file_id}.pdf"
    if not fp.exists():
        return "Not found", 404
    return send_file(str(fp), as_attachment=True, download_name=f"combined_{file_id}.pdf")


@app.route('/webhook', methods=['POST'])
def webhook():
    logger.info('Incoming webhook: %s', dict(request.values))
    from_number = request.values.get('From')
    body = (request.values.get('Body') or '').strip()
    body_lower = body.lower()
    num_media = int(request.values.get('NumMedia', '0'))

    if not from_number:
        resp = MessagingResponse()
        resp.message('Missing From number in request.')
        return str(resp)

    sess = user_sessions.setdefault(from_number, {'files': [], 'state': 'collecting'})
    resp = MessagingResponse()

    # Handle incoming media
    if num_media > 0:
        for i in range(num_media):
            m_url = request.values.get(f'MediaUrl{i}')
            m_type = request.values.get(f'MediaContentType{i}', '')
            logger.info('Media %s type=%s url=%s', i, m_type, m_url)

            if 'pdf' in m_type.lower():
                # Authenticated download
                auth_user = TWILIO_API_KEY or TWILIO_ACCOUNT_SID
                auth_pass = TWILIO_API_SECRET or TWILIO_AUTH_TOKEN
                try:
                    if auth_user and auth_pass:
                        r = requests.get(m_url, auth=(auth_user, auth_pass), timeout=30)
                    else:
                        r = requests.get(m_url, timeout=30)

                    if r.status_code == 200 and r.content and len(r.content) > 10:
                        # store bytes in temp file
                        tmpf = tempfile.NamedTemporaryFile(delete=False, suffix='.pdf')
                        tmpf.write(r.content)
                        tmpf.flush()
                        tmpf.close()
                        sess['files'].append({'path': tmpf.name, 'orig_name': f'file_{len(sess['files'])+1}.pdf'})
                        resp.message(f'Received PDF #{len(sess['files'])}.')
                    else:
                        logger.error('Failed media download status=%s', getattr(r, 'status_code', None))
                        resp.message('Failed to download attached file. Please try again.')
                        return str(resp)
                except requests.RequestException as ex:
                    logger.exception('Exception downloading media: %s', ex)
                    resp.message('Network error when downloading file. Please try again.')
                    return str(resp)
            else:
                resp.message('Please send PDF files only.')

        if len(sess['files']) >= 2:
            sess['state'] = 'awaiting_confirm'
            resp.message('Received two PDFs. Reply YES to confirm combine into a single A4 quadrant PDF, or NO to cancel.')
        return str(resp)

    # Handle confirmation
    if body_lower in ('yes', 'y') and sess.get('state') == 'awaiting_confirm' and len(sess['files']) >= 2:
        try:
            # read the first two files' bytes
            with open(sess['files'][0]['path'], 'rb') as f1, open(sess['files'][1]['path'], 'rb') as f2:
                b1 = f1.read()
                b2 = f2.read()

            pdf_bytes = combine_pdfs_to_quadrant_pdf(b1, b2, dpi=DPI)

            file_id = uuid.uuid4().hex
            tmpdir = Path(tempfile.gettempdir())
            out_path = tmpdir / f'combined_{file_id}.pdf'
            with open(out_path, 'wb') as outf:
                outf.write(pdf_bytes)

            file_url = f"{HOST_BASE_URL}/download/{file_id}"

            sent = send_whatsapp_message(from_number, 'Here is your combined PDF:', media_url=file_url)
            if not sent:
                # Fallback to TwiML inline media
                logger.info('Falling back to TwiML inline media for %s', from_number)
                tw = MessagingResponse()
                m = tw.message('Here is your combined PDF (link):')
                m.media(file_url)

                # cleanup
                for f in sess['files']:
                    try:
                        Path(f['path']).unlink(missing_ok=True)
                    except Exception:
                        pass
                user_sessions.pop(from_number, None)
                return str(tw)

            # success -> cleanup session files and return empty TwiML ack
            for f in sess['files']:
                try:
                    Path(f['path']).unlink(missing_ok=True)
                except Exception:
                    pass
            user_sessions.pop(from_number, None)
            return str(MessagingResponse())
        except Exception as e:
            logger.exception('Combine/send failed: %s', e)
            resp.message(f'Failed to combine PDFs: {e}')
            return str(resp)

    if body_lower in ('no', 'n') and sess.get('state') == 'awaiting_confirm':
        for f in sess['files']:
            try:
                Path(f['path']).unlink(missing_ok=True)
            except Exception:
                pass
        user_sessions.pop(from_number, None)
        resp.message('Cancelled — uploaded files removed. Send PDFs to start again.')
        return str(resp)

    # default
    resp.message('Hi — send me two PDFs (each with 2 pages). After I receive two, I\'ll ask you to confirm. Reply YES to proceed or NO to cancel.')
    return str(resp)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
