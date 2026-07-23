import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from django.template.loader import render_to_string

from .base import NotificationBackend

logger = logging.getLogger(__name__)


class EmailBackend(NotificationBackend):
    """
    Config:
        smtp_host:      SMTP server hostname
        smtp_port:      port (default 587)
        smtp_user:      login username
        smtp_password:  login password
        use_tls:        true/false (default true — uses STARTTLS)
        use_ssl:        true/false (default false — use for port 465)
        from_address:   sender address (defaults to smtp_user)
        to_addresses:   comma-separated recipient addresses
    """

    def send(self, title: str, message: str, item=None, transaction=None) -> bool:
        host = (self.config.get('smtp_host') or '').strip()
        to_raw = (self.config.get('to_addresses') or '').strip()
        if not host or not to_raw:
            logger.error('email backend misconfigured: smtp_host and to_addresses are required.')
            return False

        port = int(self.config.get('smtp_port') or 587)
        user = self.config.get('smtp_user', '')
        password = self.config.get('smtp_password', '')
        use_tls = str(self.config.get('use_tls', 'true')).lower() not in ('false', '0', 'no')
        use_ssl = str(self.config.get('use_ssl', 'false')).lower() in ('true', '1', 'yes')
        from_addr = (self.config.get('from_address') or user).strip()
        to_addrs = [a.strip() for a in to_raw.split(',') if a.strip()]

        # Plain-text fallback
        body_lines = [message]
        if item:
            body_lines += [
                '',
                f'Item:     {item.name}',
                f'Type:     {item.type}',
                f'Value:    {item.value} {item.currency}',
            ]
            if item.expiry_date:
                body_lines.append(f'Expires:  {item.expiry_date}')
        plain_text = '\n'.join(body_lines)

        # HTML body via branded template
        try:
            html_body = render_to_string('email/notification.html', {
                'title': title,
                'message': message,
                'item': item,
            })
        except Exception:
            html_body = None

        msg = MIMEMultipart('alternative')
        msg['Subject'] = title
        msg['From'] = from_addr
        msg['To'] = ', '.join(to_addrs)
        msg.attach(MIMEText(plain_text, 'plain', 'utf-8'))
        if html_body:
            msg.attach(MIMEText(html_body, 'html', 'utf-8'))

        try:
            if use_ssl:
                conn = smtplib.SMTP_SSL(host, port, timeout=15)
            else:
                conn = smtplib.SMTP(host, port, timeout=15)
                if use_tls:
                    conn.starttls()
            if user and password:
                conn.login(user, password)
            conn.sendmail(from_addr, to_addrs, msg.as_string())
            conn.quit()
            return True
        except (smtplib.SMTPException, OSError) as exc:
            logger.warning('email notification failed: %s', exc)
            return False
