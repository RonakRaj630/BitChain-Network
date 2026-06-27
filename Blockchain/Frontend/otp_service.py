"""
otp_service.py — OTP generation, storage and email delivery for BitChain
"""

import random
import string
import smtplib
import time
import os
from dotenv import load_dotenv
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

load_dotenv()

# ── Config ──────────────────────────────────────────────
SMTP_HOST     = 'smtp.gmail.com'
SMTP_PORT     = 587
SMTP_USER     = os.environ.get('BITCHAIN_EMAIL', '')        # your gmail
SMTP_PASSWORD = os.environ.get('BITCHAIN_PASSWORD', '')   # gmail app password
OTP_EXPIRY    = 300   # 5 minutes

# In-memory store: email -> {otp, expires, purpose, address}
OTP_STORE = {}

# ── OTP Generation ───────────────────────────────────────
def generate_otp(length=8):
    """8-char alphanumeric OTP — letters + digits, mixed case."""
    chars = string.ascii_letters + string.digits
    return ''.join(random.SystemRandom().choice(chars) for _ in range(length))

# ── OTP Storage ──────────────────────────────────────────
def store_otp(email, otp, purpose, address=None):
    """Store OTP with expiry. purpose = 'login' | 'reset'"""
    OTP_STORE[email.lower()] = {
        'otp':     otp,
        'expires': time.time() + OTP_EXPIRY,
        'purpose': purpose,
        'address': address,
    }

def verify_otp(email, submitted_otp, purpose):
    """
    Verify OTP. Returns (True, 'OK') or (False, reason).
    OTP is deleted after first successful use.
    """
    key   = email.lower()
    entry = OTP_STORE.get(key)

    if not entry:
        return False, 'No OTP found. Please request a new one.'
    if entry['purpose'] != purpose:
        return False, 'Invalid OTP type.'
    if time.time() > entry['expires']:
        OTP_STORE.pop(key, None)
        return False, 'OTP expired. Please request a new one.'
    if entry['otp'] != submitted_otp.strip():
        return False, 'Incorrect OTP. Please try again.'

    OTP_STORE.pop(key, None)   # one-time use
    return True, 'OK'

# ── Email Delivery ───────────────────────────────────────
def send_otp_email(to_email, otp, purpose='login'):
    """
    Send OTP via Gmail SMTP.
    Falls back to console print if SMTP not configured (dev mode).
    Returns True on success, False on failure.
    """
    if not SMTP_USER or not SMTP_PASSWORD:
        print(f"\n{'='*50}")
        print(f"DEV MODE — OTP for {to_email} [{purpose}]: {otp}")
        print(f"{'='*50}\n")
        return True

    subject = {
        'login': '🔐 BitChain Login OTP',
        'reset': '🔑 BitChain Password Reset OTP',
    }.get(purpose, '🔐 BitChain OTP')

    body = f"""
<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#030712;font-family:Arial,sans-serif;">
  <div style="max-width:480px;margin:40px auto;background:#0f172a;border:1px solid #1e293b;border-radius:16px;overflow:hidden;">

    <!-- Header -->
    <div style="background:linear-gradient(135deg,#3b82f6,#a855f7);padding:2rem;text-align:center;">
      <div style="font-size:2rem;margin-bottom:0.5rem;">⛏️</div>
      <h1 style="margin:0;color:white;font-size:1.4rem;font-weight:700;">BitChain Network</h1>
      <p style="margin:0.25rem 0 0;color:rgba(255,255,255,0.8);font-size:13px;">
        {"Login Verification" if purpose == "login" else "Password Reset"}
      </p>
    </div>

    <!-- Body -->
    <div style="padding:2rem;">
      <p style="color:#94a3b8;font-size:14px;margin:0 0 1.5rem;">
        {"Use the code below to complete your login." if purpose == "login" else "Use the code below to reset your password."}
      </p>

      <!-- OTP Box -->
      <div style="background:#1e293b;border:1px solid #334155;border-radius:12px;padding:1.5rem;text-align:center;margin-bottom:1.5rem;">
        <p style="margin:0 0 0.5rem;color:#64748b;font-size:11px;letter-spacing:3px;text-transform:uppercase;">Your OTP Code</p>
        <div style="font-family:monospace;font-size:2.2rem;font-weight:700;letter-spacing:8px;color:#a855f7;">{otp}</div>
      </div>

      <p style="color:#64748b;font-size:13px;margin:0 0 0.5rem;">
        ⏱ This code expires in <strong style="color:#94a3b8;">5 minutes</strong>.
      </p>
      <p style="color:#475569;font-size:12px;margin:0;">
        If you did not request this, you can safely ignore this email.
      </p>
    </div>

    <!-- Footer -->
    <div style="padding:1rem 2rem;border-top:1px solid #1e293b;text-align:center;">
      <p style="margin:0;color:#334155;font-size:11px;">BitChain Network · Built in Python · secp256k1 ECC</p>
    </div>
  </div>
</body>
</html>
"""

    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From']    = f'BitChain Network <{SMTP_USER}>'
        msg['To']      = to_email

        msg.attach(MIMEText(body, 'html'))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_USER, to_email, msg.as_string())

        print(f"✅ OTP email sent to {to_email} [{purpose}]")
        return True

    except smtplib.SMTPAuthenticationError:
        print("❌ Gmail auth failed — check BITCHAIN_EMAIL and BITCHAIN_EMAIL_PASS")
        return False
    except Exception as e:
        print(f"❌ Email send error: {e}")
        return False
