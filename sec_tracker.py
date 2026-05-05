import requests, smtplib, json, os, re
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

CIK = "0002045724"
FUND_NAME = "Situational Awareness LP"
ALERT_EMAIL = "k.franzmeilinger@gmail.com"
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_USER = os.environ["SMTP_USER"]
SMTP_PASS = os.environ["SMTP_PASS"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
FORM_TYPES = ["13F-HR", "13F-HR/A", "SC 13D", "SC 13G", "SC 13D/A", "SC 13G/A"]
STATE_FILE = "seen_filings.json"

# -------------------------------------------------------
# EDIT THESE ANYTIME — topics you want Claude to focus on
# -------------------------------------------------------
SUMMARY_TOPICS = [
    "overall market outlook and macro themes",
    "any shifts in the fund's investment philosophy or AGI thesis",
    "notable concentration or diversification changes",
]

POSITION_TOPICS = [
    "total AUM and number of holdings",
    "largest new positions opened",
    "largest positions exited",
    "biggest increases or decreases in existing stakes",
    "any notable short positions",
]
# -------------------------------------------------------

def load_seen():
    try:
        with open(STATE_FILE) as f:
            return set(json.load(f))
    except:
        return set()

def save_seen(seen):
    with open(STATE_FILE, "w") as f:
        json.dump(list(seen), f)

def fetch_recent_filings():
    url = f"https://data.sec.gov/submissions/CIK{CIK}.json"
    headers = {"User-Agent": "filing-tracker k.franzmeilinger@gmail.com"}
    r = requests.get(url, headers=headers, timeout=15)
    r.raise_for_status()
    data = r.json()
    recent = data.get("filings", {}).get("recent", {})
    filings = []
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    for form, date, acc in zip(forms, dates, accessions):
        if any(form.startswith(t.replace("/", "")) or form == t for t in FORM_TYPES) or \
           any(t in form for t in FORM_TYPES):
            filings.append({"form": form, "date": date, "accession": acc})
    return filings

def fetch_filing_text(accession):
    acc_clean = accession.replace("-", "")
    cik_clean = CIK.lstrip("0")
    index_url = f"https://www.sec.gov/Archives/edgar/data/{cik_clean}/{acc_clean}/{accession}-index.html"
    headers = {"User-Agent": "filing-tracker k.franzmeilinger@gmail.com"}
    combined_text = ""

    try:
        # fetch the index page to find all document links
        r = requests.get(index_url, headers=headers, timeout=15)
        r.raise_for_status()

        # grab all .xml file links (skip the big .txt bundle)
        xml_links = re.findall(
            r'href="(/Archives/edgar/data/[^"]+\.xml)"', r.text, re.IGNORECASE
        )
        xml_links = [l for l in xml_links if not l.endswith(".txt")]

        for link in xml_links:
            doc_url = f"https://www.sec.gov{link}"
            doc_r = requests.get(doc_url, headers=headers, timeout=15)
            # strip XML tags and clean whitespace
            text = re.sub(r'<[^>]+>', ' ', doc_r.text)
            text = re.sub(r'\s+', ' ', text).strip()
            combined_text += text + "\n\n"

        # trim to 15000 chars to stay within Claude's context
        combined_text = combined_text[:15000]

    except Exception as e:
        print(f"Could not fetch filing text: {e}")

    return combined_text or None, index_url
def summarize_with_claude(filing_text, form_type):
    topics_str = "\n".join(f"- {t}" for t in SUMMARY_TOPICS)
    position_str = "\n".join(f"- {t}" for t in POSITION_TOPICS)

    prompt = f"""You are analyzing an SEC {form_type} filing for {FUND_NAME}, an AI-focused hedge fund run by Leopold Aschenbrenner.

Here is the filing text:
{filing_text}

Please provide two sections:

SECTION 1 — MACRO & PHILOSOPHY (5-8 sentences)
Summarize any notable changes in the fund's market outlook, macro themes, or investment philosophy. Focus on big picture directional shifts, AGI thesis updates, or sector rotation. Write in plain English as if briefing a smart non-expert.

Topics to focus on:
{topics_str}

SECTION 2 — POSITION CHANGES (3-8 bullet points)
Summarize the most important dollar-level moves. Be specific with numbers where available.

Topics to focus on:
{position_str}

Keep it concise and factual. If the filing doesn't contain enough information for a section, say so briefly."""

    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-opus-4-5",
            "max_tokens": 1000,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["content"][0]["text"]

def send_alert(filing, summary, filing_url):
    acc = filing["accession"]
    period = filing.get("date", "")
    
    parts = summary.split("SECTION 2")
    section1 = parts[0].replace("SECTION 1 — MACRO & PHILOSOPHY", "").strip() if parts else summary
    section2 = parts[1].replace("— POSITION CHANGES", "").strip() if len(parts) > 1 else ""

    def to_bullets(text):
        lines = [l.strip().lstrip("-•▸").strip() for l in text.strip().splitlines() if l.strip()]
        return "".join(
            f'<div style="display:flex;gap:10px;align-items:flex-start;margin-bottom:8px;">'
            f'<span style="color:#f5a623;font-size:13px;margin-top:1px;">▸</span>'
            f'<p style="font-size:13px;color:#cccccc;margin:0;line-height:1.6;">{l}</p></div>'
            for l in lines if l
        )

    html = f"""
<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#000000;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#000000;">
<tr><td align="center" style="padding:2rem 1rem;">
<table width="600" cellpadding="0" cellspacing="0" style="background:#0a0a0a;border-radius:8px;font-family:monospace;color:#e0e0e0;">
<tr><td style="padding:2rem;">

  <!-- header -->
  <table width="100%" cellpadding="0" cellspacing="0" style="border-bottom:1px solid #f5a623;padding-bottom:1rem;margin-bottom:1.5rem;">
  <tr>
    <td>
      <p style="font-size:11px;color:#f5a623;margin:0 0 4px;letter-spacing:2px;">SITUATIONAL AWARENESS LP</p>
      <p style="font-size:18px;font-weight:500;margin:0;color:#ffffff;">SEC FILING ALERT</p>
    </td>
    <td align="right">
      <p style="font-size:11px;color:#888888;margin:0 0 2px;">FORM TYPE</p>
      <p style="font-size:13px;color:#f5a623;margin:0;">{filing['form']}</p>
    </td>
  </tr>
  <tr><td colspan="2" style="padding-top:1rem;">
    <table cellpadding="0" cellspacing="0"><tr>
      <td style="padding-right:2rem;">
        <p style="font-size:10px;color:#666666;margin:0 0 2px;letter-spacing:1px;">FILED</p>
        <p style="font-size:12px;color:#cccccc;margin:0;">{filing['date']}</p>
      </td>
      <td style="padding-right:2rem;">
        <p style="font-size:10px;color:#666666;margin:0 0 2px;letter-spacing:1px;">PERIOD</p>
        <p style="font-size:12px;color:#cccccc;margin:0;">{period}</p>
      </td>
      <td>
        <p style="font-size:10px;color:#666666;margin:0 0 2px;letter-spacing:1px;">CIK</p>
        <p style="font-size:12px;color:#cccccc;margin:0;">0002045724</p>
      </td>
    </tr></table>
  </td></tr>
  </table>

  <!-- section 1 -->
  <p style="font-size:10px;color:#f5a623;letter-spacing:2px;margin:0 0 10px;">01 / MACRO & PHILOSOPHY</p>
  <div style="border-left:2px solid #f5a623;padding-left:1rem;margin-bottom:1.5rem;">
    <p style="font-size:13px;line-height:1.7;color:#cccccc;margin:0;">{section1}</p>
  </div>

  <!-- divider -->
  <div style="border-top:0.5px solid #222222;margin:1.5rem 0;"></div>

  <!-- section 2 -->
  <p style="font-size:10px;color:#f5a623;letter-spacing:2px;margin:0 0 10px;">02 / POSITION CHANGES</p>
  <div style="margin-bottom:1.5rem;">
    {to_bullets(section2)}
  </div>

  <!-- divider -->
  <div style="border-top:0.5px solid #222222;margin:1.5rem 0;"></div>

  <!-- footer -->
  <table width="100%" cellpadding="0" cellspacing="0"><tr>
    <td><p style="font-size:11px;color:#444444;margin:0;">Generated by SEC Filing Tracker</p></td>
    <td align="right"><a href="{filing_url}" style="font-size:11px;color:#f5a623;text-decoration:none;letter-spacing:1px;">VIEW FULL FILING ↗</a></td>
  </tr></table>

</td></tr>
</table>
</td></tr>
</table>
</body>
</html>
"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[SEC Alert] {FUND_NAME} — {filing['form']} filed {filing['date']}"
    msg["From"] = SMTP_USER
    msg["To"] = ALERT_EMAIL
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls()
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)
    print(f"Alert sent for {filing['form']} filed {filing['date']}")

def main():
    seen = load_seen()
    filings = fetch_recent_filings()
    new_filings = [f for f in filings if f["accession"] not in seen]
    if not new_filings:
        print(f"[{datetime.now():%Y-%m-%d %H:%M}] No new filings.")
        return
    for filing in new_filings:
        print(f"New filing found: {filing['form']} on {filing['date']}")
        filing_text, filing_url = fetch_filing_text(filing["accession"])
        if filing_text:
            summary = summarize_with_claude(filing_text, filing['form'])
        else:
            summary = "Could not retrieve filing text for summarization."
        send_alert(filing, summary, filing_url)
        seen.add(filing["accession"])
    save_seen(seen)

if __name__ == "__main__":
    main()
