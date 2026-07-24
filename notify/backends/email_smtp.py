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
        if not to_raw:
            logger.error('email backend misconfigured: to_addresses is required.')
            return False

        if not host:
            # Fall back to app-level SMTP from SiteConfiguration
            try:
                from myapp.models import SiteConfiguration as _SC
                _cfg = _SC.load()
                if _cfg.email_host:
                    host = _cfg.email_host
                    port = _cfg.email_port or 587
                    user = _cfg.email_host_user or ''
                    password = _cfg.email_host_password or ''
                    use_tls = _cfg.email_use_tls
                    use_ssl = _cfg.email_use_ssl
                    from_addr = (_cfg.email_from_address or user or '').strip()
                else:
                    logger.error('email backend: smtp_host not set and no app SMTP configured.')
                    return False
            except Exception as exc:
                logger.error('email backend: could not load SiteConfiguration: %s', exc)
                return False
        else:
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
