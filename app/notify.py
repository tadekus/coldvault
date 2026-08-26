"""Email notifications via Resend (https://resend.com). Uses the stdlib only.

Configured via .env:
  RESEND_API_KEY        re_...
  COLDVAULT_EMAIL_FROM  "ColdVault <coldvault@your-verified-domain>"
  COLDVAULT_EMAIL_TO    you@example.com[,someone@else]
  COLDVAULT_NOTIFY      true  -> run audit + email after each canary upload
"""
import json
import os
import urllib.error
import urllib.request

import config
import db
import version
from logs import log_event

RESEND_URL = os.environ.get("RESEND_URL", "https://api.resend.com/emails")
USER_AGENT = f"ColdVault/{version.VERSION}"


def _fmt_bytes(n):
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB", "TB", "PB"):
        if n < 1024 or u == "PB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024


def enabled():
    return bool(config.NOTIFY and config.RESEND_API_KEY and config.EMAIL_FROM and config.EMAIL_TO)


def config_problem():
    """Human-readable reason notifications can't send, or None if ok."""
    if not config.RESEND_API_KEY:
        return "RESEND_API_KEY is not set"
    if not config.EMAIL_FROM:
        return "COLDVAULT_EMAIL_FROM is not set"
    if not config.EMAIL_TO:
        return "COLDVAULT_EMAIL_TO is not set"
    return None


def send_email(subject, html):
    """Send one email via Resend. Returns (ok, detail). Never raises."""
    problem = config_problem()
    if problem:
        return False, problem
    payload = json.dumps({
        "from": config.EMAIL_FROM,
        "to": config.EMAIL_TO,
        "subject": subject,
        "html": html,
    }).encode()
    req = urllib.request.Request(RESEND_URL, data=payload, method="POST", headers={
        "Authorization": f"Bearer {config.RESEND_API_KEY}",
        "Content-Type": "application/json",
        # Resend is behind Cloudflare, which blocks the default urllib
        # User-Agent (seen as Cloudflare error 1010). Send a normal one.
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode()
        mid = ""
        try:
            mid = json.loads(body).get("id", "")
        except ValueError:
            pass
        log_event("INFO", "notify", f"email sent to {', '.join(config.EMAIL_TO)}: {subject}"
                  + (f" (id {mid})" if mid else ""))
        return True, mid
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        log_event("ERROR", "notify", f"Resend HTTP {e.code} sending '{subject}'", detail)
        return False, f"HTTP {e.code}: {detail}"
    except Exception as e:
        log_event("ERROR", "notify", f"failed to send email '{subject}': {e}")
        return False, str(e)


def _row(label, value):
    return (f'<tr><td style="padding:4px 12px 4px 0;color:#667;">{label}</td>'
            f'<td style="padding:4px 0;font-weight:600;">{value}</td></tr>')


def upload_report_html(session, audit):
    s = session
    ok = audit["ok_status"]
    banner_bg, banner = ("#16351f", "✅ Audit OK — bucket matches the index") if ok \
        else ("#3a1f1c", "⚠️ Audit found issues — see below")
    rows = "".join([
        _row("Drive / label", s.get("label") or "—"),
        _row("Source", s.get("source") or "—"),
        _row("Bucket", f's3://{audit["bucket"]}'),
        _row("Uploaded this run", f'{s.get("done_files", 0)} file(s), {_fmt_bytes(s.get("done_bytes"))}'),
        _row("Skipped (unchanged/dupe)", s.get("skipped_files", 0)),
        _row("Failed", s.get("failed_files", 0)),
        _row("Bucket now holds", f'{audit["in_bucket"]} objects · {_fmt_bytes(audit["bucket_bytes"])}'),
        _row("Audit", f'{audit["ok"]} verified · {audit["missing_count"]} missing · '
                      f'{audit["size_mismatch_count"]} size-mismatch · {audit["class_drift_count"]} class-drift'),
    ])
    issues = ""
    if not ok:
        parts = []
        if audit["missing_count"]:
            parts.append("<b>Missing from bucket:</b><br>" +
                         "<br>".join(audit["missing"][:20]) +
                         (f"<br>… +{audit['missing_count'] - 20} more" if audit["missing_count"] > 20 else ""))
        if audit["size_mismatch_count"]:
            parts.append("<b>Size mismatch:</b><br>" +
                         "<br>".join(f'{m["key"]} (index {m["index"]}, bucket {m["bucket"]})'
                                     for m in audit["size_mismatch"][:20]))
        if audit["class_drift_count"]:
            parts.append("<b>Wrong storage class:</b><br>" +
                         "<br>".join(f'{m["key"]} ({m["class"]})' for m in audit["class_drift"][:20]))
        issues = ('<div style="margin-top:16px;padding:12px;background:#2a1a17;border-radius:8px;'
                  'font-family:monospace;font-size:12px;color:#e8a;">' + "<br><br>".join(parts) + "</div>")

    return f"""\
<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:640px;margin:auto;color:#222;">
  <h2 style="margin:0 0 4px;">❄ ColdVault upload report</h2>
  <div style="color:#889;font-size:13px;margin-bottom:16px;">{db.now()}</div>
  <div style="padding:10px 14px;border-radius:8px;background:{banner_bg};color:#fff;font-weight:600;margin-bottom:16px;">{banner}</div>
  <table style="border-collapse:collapse;font-size:14px;">{rows}</table>
  {issues}
</div>"""


def audit_report_html(audit):
    """Report email with no specific upload session — just bucket + audit."""
    ok = audit["ok_status"]
    banner_bg, banner = ("#16351f", "✅ Audit OK — bucket matches the index") if ok \
        else ("#3a1f1c", "⚠️ Audit found issues — see below")
    rows = "".join([
        _row("Bucket", f's3://{audit["bucket"]}'),
        _row("Bucket holds", f'{audit["in_bucket"]} objects · {_fmt_bytes(audit["bucket_bytes"])}'),
        _row("Indexed", audit["in_index"]),
        _row("Imported this run", audit["imported"]),
        _row("Audit", f'{audit["ok"]} verified · {audit["missing_count"]} missing · '
                      f'{audit["size_mismatch_count"]} size-mismatch · {audit["class_drift_count"]} class-drift'),
    ])
    return f"""\
<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:640px;margin:auto;color:#222;">
  <h2 style="margin:0 0 4px;">❄ ColdVault audit report</h2>
  <div style="color:#889;font-size:13px;margin-bottom:16px;">{db.now()}</div>
  <div style="padding:10px 14px;border-radius:8px;background:{banner_bg};color:#fff;font-weight:600;margin-bottom:16px;">{banner}</div>
  <table style="border-collapse:collapse;font-size:14px;">{rows}</table>
</div>"""


def send_audit_report():
    """Run an audit and email a bucket report on demand. Returns (ok, detail)."""
    problem = config_problem()
    if problem:
        return False, problem
    import audit as audit_mod
    report = audit_mod.run_audit(config.BUCKET)
    status = "OK" if report["ok_status"] else "ISSUES FOUND"
    ok, detail = send_email(f"ColdVault audit report — {status}",
                            audit_report_html(report))
    return ok, detail


def notify_upload_complete(sid):
    """Run an audit and email the report for a finished upload session.
    Best-effort; never raises into the caller."""
    if not config.NOTIFY:
        return
    problem = config_problem()
    if problem:
        log_event("WARNING", "notify",
                  f"COLDVAULT_NOTIFY is on but email can't send: {problem}")
        return
    try:
        import audit as audit_mod
        session = db.get_session(sid) or {}
        report = audit_mod.run_audit(config.BUCKET)
        status = "OK" if report["ok_status"] else "ISSUES FOUND"
        label = session.get("label") or "drive"
        subject = f"ColdVault: {label} archived — audit {status}"
        send_email(subject, upload_report_html(session, report))
    except Exception as e:
        log_event("ERROR", "notify", f"post-upload audit/email failed for session #{sid}: {e}")
