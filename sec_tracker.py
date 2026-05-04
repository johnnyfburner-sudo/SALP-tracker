import requests, smtplib, json, os
from email.mime.text import MIMEText
from datetime import datetime

CIK = "0002045724"
FUND_NAME = "Situational Awareness LP"
ALERT_EMAIL = "k.franzmeilinger@gmail.com"
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_USER = os.environ["SMTP_USER"]
SMTP_PASS = os.environ["SMTP_PASS"]
FORM_TYPES = ["13F-HR", "13F-HR/A", "SC 13D", "SC 13G", "SC 13D/A", "SC 13G/A"]
STATE_FILE = "seen_filings.json"

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
    headers = {"User-Agent": "filing-tracker your@email.com"}
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

def send_alert(filing):
    acc = filing["accession"]
    url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={CIK}&type={filing['form']}&dateb=&owner=include&count=5"
    body = f"""New SEC filing detected for {FUND_NAME}

Form type:  {filing['form']}
Filed:      {filing['date']}
Accession:  {acc}

View on EDGAR:
https://www.sec.gov/Archives/edgar/data/{CIK.lstrip('0')}/{acc.replace('-','')}/{acc}-index.html

All filings: {url}
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
        send_alert(filing)
        seen.add(filing["accession"])
    save_seen(seen)

if __name__ == "__main__":
    main()
