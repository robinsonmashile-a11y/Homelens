"""
Homelens — Alert system.

Sends an email via Gmail when a pipeline step fails.

Required .env keys:
  ALERT_FROM_EMAIL    your Gmail address
  ALERT_TO_EMAIL      destination address (can be same)
  GMAIL_APP_PASSWORD  16-char app password from
                      Google Account → Security → App passwords

Usage:
  from scripts.alerts import send_alert
  send_alert('Scrapers', 'Connection timeout after 3600s')
"""
from __future__ import annotations

import logging
import os
import smtplib
from email.mime.text import MIMEText
from datetime import datetime

logger = logging.getLogger('homelens.alerts')


def send_alert(step_name: str, error_detail: str) -> None:
    """
    Send a failure alert email. Silently swallows exceptions so a broken
    alert system never crashes the pipeline itself.
    """
    from_email = os.getenv('ALERT_FROM_EMAIL', '')
    to_email   = os.getenv('ALERT_TO_EMAIL', '')
    app_pass   = os.getenv('GMAIL_APP_PASSWORD', '')

    if not all([from_email, to_email, app_pass]):
        logger.warning(
            'Alert skipped — ALERT_FROM_EMAIL / ALERT_TO_EMAIL / '
            'GMAIL_APP_PASSWORD not set in .env'
        )
        return

    body = (
        f'Homelens Pipeline Alert\n'
        f'Time: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")} SAST\n'
        f'Step that failed: {step_name}\n\n'
        f'Error detail:\n{error_detail}\n\n'
        f'Check logs/scheduler.log for the full trace.'
    )

    msg = MIMEText(body)
    msg['Subject'] = f'[Homelens] Pipeline failure: {step_name}'
    msg['From']    = from_email
    msg['To']      = to_email

    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(from_email, app_pass)
            server.send_message(msg)
        logger.info('Alert sent for step: %s', step_name)
    except Exception as e:
        # Never let alert failure crash the pipeline
        logger.error('Failed to send alert email: %s', e)
