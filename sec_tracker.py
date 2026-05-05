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
        r = requests.get(index_url, headers=headers, timeout=15)
        r.raise_for_status()
        xml_links = re.findall(
            r'href="(/Archives/edgar/data/[^"]+\.xml)"', r.text, re.IGNORECASE
        )
        xml_links = [l for l in xml_links if not l.endswith(".txt")]
        for link in xml_links:
            doc_url = f"https://www.sec.gov{link}"
            doc_r = requests.get(doc_url, headers=headers, timeout=15)
            text = re.sub(r'<[^>]+>', ' ', doc_r.text)
            text = re.sub(r'\s+', ' ', text).strip()
            combined_text += text + "\n\n"
        combined_text = combined_text[:15000]
    except Exception as e:
        print(f"Could not fetch filing text: {e}")
    return combined_text or None, index_url

def extract_tickers_from_filing(filing_text):
    tickers = set()
    matches = re.findall(r'\b([A-Z]{1,5})\b', filing_text)
    ignore = {"THE", "AND", "FOR", "LLC", "INC", "LTD", "ETF", "SEC", "USD",
              "AUM", "AGI", "AI", "LP", "NA", "DE", "CA", "NY", "OR", "PUT",
              "CALL", "SHS", "COM", "CL", "TR", "NEW", "OLD", "II", "III"}
    for m in matches:
        if m not in ignore and len(m) >= 2:
            tickers.add(m)
    fallback = ["BE", "GE", "AI", "NU", "DL", "ET"]
    tickers.update(fallback)
    return list(tickers)

def fetch_prices(tickers):
    prices = {}
    for ticker in tickers:
        price = None

        # try Yahoo Finance first
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1d"
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            data = r.json()
            result = data.get("chart", {}).get("result")
            if result:
                price = result[0]["meta"]["regularMarketPrice"]
        except:
            pass

        # fall back to Alpha Vantage if Yahoo failed
        if not price:
            try:
                av_key = os.environ.get("ALPHA_VANTAGE_KEY", "")
                url = f"https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol={ticker}&apikey={av_key}"
                r = requests.get(url, timeout=10)
                data = r.json()
                price_str = data.get("Global Quote", {}).get("05. price")
                if price_str:
                    price = float(price_str)
            except:
                pass

        if price:
            prices[ticker] = f"${price:,.2f}"

    return prices

def summarize_with_claude(filing_text, form_type, prices_str=""):
    topics_str = "\n".join(f"- {t}" for t in SUMMARY_TOPICS)
    position_str = "\n".join(f"- {t}" for t in POSITION_TOPICS)
    action_str = "\n".join(f"- {t}" for t in ACTION_TOPICS)

    prompt = f"""You are analyzing an SEC {form_type} filing for {FUND_NAME}, an AI-focused hedge fund run by Leopold Aschenbrenner.

Here is the filing text:
{filing_text}

Please provide three sections with EXACTLY these headers:

SECTION 1 — MACRO & PHILOSOPHY
5-8 sentences summarizing the fund's market outlook, macro themes, or investment philosophy. Focus on big picture directional shifts, AGI thesis updates, or sector rotation. Write in plain English as if briefing a smart non-expert.

Topics to focus on:
{topics_str}

SECTION 2 — POSITION CHANGES
3-8 bullet points on the most important dollar-level moves. Be specific with numbers where available.

Topics to focus on:
{position_str}

Here are today's live prices for stocks found in this filing — use these exact prices in Section 3, do not guess or use outdated numbers:
{prices_str}

SECTION 3 — ACTIONABLE INSIGHTS FOR RETAIL INVESTORS
3-5 bullet points written for a normal person who wants to act on this information today. For each one: lead with BUY, AVOID, or WATCH, then the company name and ticker, then 2-3 sentences explaining what SALP did and why a regular person should care. Use the live prices provided above so the reader knows exactly what they are getting into. If something SALP holds is not publicly tradable, suggest the best public proxy and explain the connection.

Topics to focus on:
{action_str}

Write section 3 like a knowledgeable friend giving real advice — clear, direct, no jargon."""

    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-opus-4-5",
            "max_tokens": 1500,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=60,
    )
    response.raise_for_status()
    return response.json()["content"][0]["text"]

def send_alert(filing, summary, filing_url):
    summary = re.sub(r'#+\s*', '', summary)
    parts = summary.split("SECTION 2")
    section1 = parts[0].replace("SECTION 1 — MACRO & PHILOSOPHY", "").strip() if parts else summary
    remainder = parts[1] if len(parts) > 1 else ""
    parts2 = remainder.split("SECTION 3")
    section2 = parts2[0].replace("— POSITION CHANGES", "").strip()
    section3 = parts2[1].replace("— ACTIONABLE INSIGHTS FOR RETAIL INVESTORS", "").strip() if len(parts2) > 1 else ""

    def to_bullets(text):
        text = re.sub(r'\*\*(.*?)\*\*', r'<strong style="color:#ffffff;">\1</strong>', text)
        lines = [l.strip().lstrip("-•▸").strip() for l in text.strip().splitlines() if l.strip()]
        return "".join(
            f'<div style="display:flex;gap:10px;align-items:flex-start;margin-bottom:8px;">'
            f'<span style="color:#f5a623;font-size:13px;margin-top:1px;">▸</span>'
            f'<p style="font-size:13px;color:#cccccc;margin:0;line-height:1.6;">{l}</p></div>'
            for l in lines if l
        )

    html = f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#000000;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#000000;">
<tr><td align="center" style="padding:2rem 1rem;">
<table width="600" cellpadding="0" cellspacing="0" style="background:#0a0a0a;border-radius:8px;font-family:monospace;color:#e0e0e0;">
<tr><td style="padding:2rem;">

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
      <td>
        <p style="font-size:10px;color:#666666;margin:0 0 2px;letter-spacing:1px;">CIK</p>
        <p style="font-size:12px;color:#cccccc;margin:0;">0002045724</p>
      </td>
    </tr></table>
  </td></tr>
  </table>

  <p style="font-size:10px;color:#f5a623;letter-spacing:2px;margin:0 0 10px;">01 / MACRO & PHILOSOPHY</p>
  <div style="border-left:2px solid #f5a623;padding-left:1rem;margin-bottom:1.5rem;">
    <p style="font-size:13px;line-height:1.7;color:#cccccc;margin:0;">{section1}</p>
  </div>

  <div style="border-top:0.5px solid #222222;margin:1.5rem 0;"></div>

  <p style="font-size:10px;color:#f5a623;letter-spacing:2px;margin:0 0 10px;">02 / POSITION CHANGES</p>
  <div style="margin-bottom:1.5rem;">
    {to_bullets(section2)}
  </div>

  <div style="border-top:0.5px solid #222222;margin:1.5rem 0;"></div>

  <p style="font-size:10px;color:#f5a623;letter-spacing:2px;margin:0 0 10px;">03 / WHAT TO DO</p>
  <div style="margin-bottom:1.5rem;">
    {to_bullets(section3)}
  </div>

  <div style="border-top:0.5px solid #222222;margin:1.5rem 0;"></div>

  <table width="100%" cellpadding="0" cellspacing="0"><tr>
    <td><p style="font-size:11px;color:#444444;margin:0;">Generated by SEC Filing Tracker</p></td>
    <td align="right"><a href="{filing_url}" style="font-size:11px;color:#f5a623;text-decoration:none;letter-spacing:1px;">VIEW FULL FILING ↗</a></td>
  </tr></table>

</td></tr>
</table>
</td></tr>
</table>
</body>
</html>"""

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
        raw_tickers = extract_tickers_from_filing(filing_text or "")
        prices = fetch_prices(raw_tickers)
        prices = {k: v for k, v in prices.items() if v}
        prices_str = "\n".join(f"{t}: {p}" for t, p in prices.items())
        print(f"Live prices fetched for: {', '.join(prices.keys())}")
        if filing_text:
            summary = summarize_with_claude(filing_text, filing['form'], prices_str)
        else:
            summary = "Could not retrieve filing text for summarization."
        if os.environ.get("TEST_MODE") == "true":
            print("TEST MODE — no email sent. Summary:")
            print(summary)
        else:
            send_alert(filing, summary, filing_url)
            seen.add(filing["accession"])
    save_seen(seen)

if __name__ == "__main__":
    main()
