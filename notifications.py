"""
notifications.py — Generiek e-mail versturen via SMTP (bijv. Outlook/Microsoft 365),
voor de dagelijkse CSV-export en meldingen bij een mislukte sync.

Benodigde omgevingsvariabelen (GitHub Actions-secrets):
    SMTP_HOST      (standaard: smtp.office365.com)
    SMTP_PORT      (standaard: 587)
    SMTP_USERNAME  (je volledige e-mailadres)
    SMTP_PASSWORD  (je wachtwoord of app-wachtwoord)
    SMTP_TO        (ontvanger; standaard hetzelfde als SMTP_USERNAME)
"""

import os
import smtplib
from email.message import EmailMessage

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.office365.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USERNAME = os.environ.get("SMTP_USERNAME")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
SMTP_TO = os.environ.get("SMTP_TO") or SMTP_USERNAME


class EmailError(Exception):
    pass


def send_email(subject, body, attachments=None):
    """
    Verstuurt een e-mail. 'attachments' is een optionele lijst van
    (bestandsnaam, bytes, mime_type)-tupels.
    """
    if not SMTP_USERNAME or not SMTP_PASSWORD or not SMTP_TO:
        raise EmailError(
            "SMTP_USERNAME, SMTP_PASSWORD en/of SMTP_TO ontbreken in de omgeving — e-mail niet verstuurd."
        )

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_USERNAME
    msg["To"] = SMTP_TO
    msg.set_content(body)

    for filename, data, mime_type in attachments or []:
        maintype, _, subtype = mime_type.partition("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype or "octet-stream", filename=filename)

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.send_message(msg)
    except smtplib.SMTPException as e:
        raise EmailError(f"Kon e-mail niet versturen: {e}")
