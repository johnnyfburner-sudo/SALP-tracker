import requests, smtplib, json, os, re
from email.mime.text import MIMEText
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
    try:
        r = requests.get(index_url, headers=headers, timeout=15)
        r.raise_for_status()
        # find the main document link
        matches = re.findall(r'href="(/Archives/edgar/data/[^"]+\.(?:xml|htm|txt))"', r.text, re.IGNORECASE)
        for match in matches:
            if "index" not in match.lower():
                doc_url = f"https://www.sec.gov{match}"
                doc_r = requests.get(doc_url, headers=headers, timeout=15)
                text = re.sub(r'<[^>]+>', ' ', doc_r.text)
                text = re.sub(r'\s+', ' ', text).strip()
                return text[:12000], index_url
    except Exception as e:
        print(f"Could not fetch filing text: {e}")
    return None, index_url

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
            "model": "claude-opus-4-20250514",
            "max_tokens": 1000,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["content"][0]["text"]

def send_alert(filing, summary, filing_url):
    acc = filing["accession"]
    body = f"""New SEC filing detected for {FUND_NAME}

Form type:  {filing['form']}
Filed:      {filing['date']}
View filing: {filing_url}

{'='*50}
AI SUMMARY
{'='*50}

{summary}

{'='*50}
All filings: https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={CIK}&type=13F&dateb=&owner=include&count=10
"""
    msg = MIMEText(body)
    msg["Subject"] = f"[SEC Alert] {FUND_NAME} filed {filing['form']} on {filing['date']}"
    msg["From"] = SMTP_USER
    msg["To"] = ALERT_EMAIL
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
        print(f"New filing found: {filing['form']} on {filing['da
