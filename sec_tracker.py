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
# EDIT THESE ANYTIME
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

ACTION_TOPICS = [
    "3-5 specific buy or avoid recommendations a normal retail investor could act on today",
    "for each recommendation include the ticker, why SALP's move matters, and what it means for a regular person",
    "for any private or illiquid positions, suggest the closest public market equivalent and explain why",
    "write like a knowledgeable friend giving real advice — clear, direct, no jargon",
    "use the live prices provided — do not guess or use outdated prices",
]

TICKERS_TO_PRICE = [
    "CRWV", "CORZ", "IREN", "APLD", "NVDA", "AMD",
    "SMCI", "RKLB", "IONQ", "QBTS", "MSTR", "CLBT"
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
        r =
