"""
Email utility — sends transactional emails via Gmail SMTP (or any SMTP server).

Configure via environment variables:
  SMTP_HOST     — default smtp.gmail.com
  SMTP_PORT     — default 587 (STARTTLS)
  SMTP_USER     — sender Gmail address
  SMTP_PASSWORD — Gmail App Password
  EMAIL_FROM_NAME — display name (default: Apex Trading)
"""

import os
import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

logger = logging.getLogger(__name__)

def _send(to_email: str, subject: str, html_body: str) -> bool:
    """Low-level send. Returns True on success, False on failure."""
    smtp_host  = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port  = int(os.getenv("SMTP_PORT", "587"))
    smtp_user  = os.getenv("SMTP_USER", "")
    smtp_pass  = os.getenv("SMTP_PASSWORD", "")
    from_name  = os.getenv("EMAIL_FROM_NAME", "TradeGenie")

    if not smtp_user or not smtp_pass:
        logger.warning("Email not configured — skipping send to %s", to_email)
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"{from_name} <{smtp_user}>"
    msg["To"]      = to_email
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
            server.ehlo()
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, to_email, msg.as_string())
        logger.info("Email sent to %s — %s", to_email, subject)
        return True
    except Exception as e:
        logger.error("Failed to send email to %s: %s", to_email, e)
        return False


# ── Templates ─────────────────────────────────────────────────────────────────

def send_welcome_email(
    to_email: str,
    username: str,
    password: str,
    plan_type: str,
    plan_expiry: str,
    mobile: str = None,
) -> bool:
    """Send welcome / account-created email to a newly created user."""

    plan_labels = {"1m": "1 Month", "3m": "3 Months", "6m": "6 Months", "12m": "12 Months"}
    plan_label  = plan_labels.get(plan_type, plan_type)

    mobile_row = f"""
        <tr>
          <td style="padding:6px 0;color:#6b6b6b;font-size:13px;">Mobile</td>
          <td style="padding:6px 0;font-weight:600;font-size:13px;">{mobile}</td>
        </tr>""" if mobile else ""

    html = f"""
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"/></head>
<body style="margin:0;padding:0;background:#f5f5f4;font-family:'Inter',Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f5f4;padding:32px 16px;">
    <tr><td align="center">
      <table width="520" cellpadding="0" cellspacing="0"
             style="background:#ffffff;border-radius:16px;overflow:hidden;border:1px solid #e5e5e5;">

        <!-- Header -->
        <tr>
          <td style="background:#534AB7;padding:28px 32px;">
            <div style="font-size:22px;font-weight:800;color:#ffffff;letter-spacing:-0.5px;">
              ⚡ Apex Trading
            </div>
            <div style="color:rgba(255,255,255,0.75);font-size:13px;margin-top:4px;">
              Your account is ready
            </div>
          </td>
        </tr>

        <!-- Body -->
        <tr>
          <td style="padding:32px;">
            <p style="font-size:15px;color:#1a1a1a;margin:0 0 20px;">
              Hi <strong>{username}</strong>,
            </p>
            <p style="font-size:14px;color:#6b6b6b;line-height:1.6;margin:0 0 24px;">
              Your Apex Trading account has been created. Here are your login details:
            </p>

            <!-- Credentials box -->
            <table width="100%" cellpadding="0" cellspacing="0"
                   style="background:#fafafa;border:1px solid #eeede9;border-radius:10px;padding:16px 20px;margin-bottom:24px;">
              <tr>
                <td style="padding:6px 0;color:#6b6b6b;font-size:13px;">Username</td>
                <td style="padding:6px 0;font-weight:600;font-size:13px;font-family:monospace;">{username}</td>
              </tr>
              <tr>
                <td style="padding:6px 0;color:#6b6b6b;font-size:13px;">Password</td>
                <td style="padding:6px 0;font-weight:600;font-size:13px;font-family:monospace;">{password}</td>
              </tr>
              <tr>
                <td style="padding:6px 0;color:#6b6b6b;font-size:13px;">Plan</td>
                <td style="padding:6px 0;font-weight:600;font-size:13px;">{plan_label}</td>
              </tr>
              <tr>
                <td style="padding:6px 0;color:#6b6b6b;font-size:13px;">Valid until</td>
                <td style="padding:6px 0;font-weight:600;font-size:13px;color:#3B6D11;">{plan_expiry}</td>
              </tr>
              {mobile_row}
            </table>

            <p style="font-size:12px;color:#a0a0a0;line-height:1.6;margin:0 0 24px;">
              For security, please change your password after your first login.
              Keep these credentials safe — do not share them with anyone.
            </p>

            <!-- CTA -->
            <a href="#"
               style="display:inline-block;background:#534AB7;color:#ffffff;padding:12px 28px;
                      border-radius:10px;font-weight:700;font-size:14px;text-decoration:none;">
              Log In to Dashboard →
            </a>
          </td>
        </tr>

        <!-- Footer -->
        <tr>
          <td style="padding:20px 32px;border-top:1px solid #eeede9;">
            <p style="font-size:11px;color:#a0a0a0;margin:0;line-height:1.6;">
              This email was sent by Apex Trading. If you did not expect this account,
              please contact your administrator.
            </p>
          </td>
        </tr>

      </table>
    </td></tr>
  </table>
</body>
</html>
"""
    return _send(to_email, "Your Apex Trading account is ready", html)
