import requests, smtplib, json, os, re, time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, date

CIK = "0002045724"
FUND_NAME = "Situational Awareness LP"
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_USER = os.environ["SMTP_USER"]
SMTP_PASS = os.environ["SMTP_PASS"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
ALERT_EMAIL = os.environ["ALERT_EMAIL"]
FORM_TYPES = ["13F-HR", "13F-HR/A", "SC 13D", "SC 13G", "SC 13D/A", "SC 13G/A"]
STATE_FILE = "seen_filings.json"
POSITIONS_FILE = "positions.json"

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
    "any notable short positions or put options",
]
ACTION_TOPICS = [
    "3-5 specific buy or avoid recommendations a normal retail investor could act on today",
    "for each recommendation include the ticker, why SALP's move matters, and what it means for a regular person",
    "for any private or illiquid positions, suggest the closest public market equivalent and explain why",
    "write like a knowledgeable friend giving real advice, clear, direct, no jargon",
    "use the live prices provided, do not guess or use outdated prices",
]

def load_seen():
    try:
        with open(STATE_FILE) as f:
            return set(json.load(f))
    except:
        return set()

def save_seen(seen):
    with open(STATE_FILE, "w") as f:
        json.dump(list(seen), f)

def load_previous_positions():
    try:
        with open(POSITIONS_FILE) as f:
            return json.load(f)
    except:
        return {}

def save_positions(positions):
    with open(POSITIONS_FILE, "w") as f:
        json.dump(positions, f, indent=2)

def fetch_recent_filings():
    url = f"https://data.sec.gov/submissions/CIK{CIK.zfill(10)}.json"
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; filing-tracker/1.0; +mailto:k.franzmeilinger@gmail.com)",
        "Accept-Encoding": "gzip, deflate",
        "Host": "www.sec.gov"
    }
    r = requests.get(url, headers=headers, timeout=15)
    r.raise_for_status()
    data = r.json()
    recent = data.get("filings", {}).get("recent", {})
    filings = []
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    for form, filing_date, acc in zip(forms, dates, accessions):
        if any(form.startswith(t.replace("/", "")) or form == t for t in FORM_TYPES) or \
           any(t in form for t in FORM_TYPES):
            filings.append({"form": form, "date": filing_date, "accession": acc})
    return filings

def fetch_filing_documents(accession):
    acc_clean = accession.replace("-", "")
    cik_clean = CIK.lstrip("0")
    index_url = f"https://www.sec.gov/Archives/edgar/data/{cik_clean}/{acc_clean}/{accession}-index.html"
    headers = {
        "User-Agent": "filing-tracker k.franzmeilinger@gmail.com",
        "Accept": "application/json",
    }
    time.sleep(0.5)
    r = requests.get(index_url, headers=headers, timeout=15)
    r.raise_for_status()
    index_html = r.text
    combined_text = ""
    positions_xml = None
    xml_links = re.findall(r'href="(/Archives/edgar/data/[^"]+\.xml)"', index_html, re.IGNORECASE)
    xml_links = [l for l in xml_links if not l.endswith(".txt")]
    for link in xml_links:
        doc_url = f"https://www.sec.gov{link}"
        doc_r = requests.get(doc_url, headers=headers, timeout=15)
        if "infoTable" in doc_r.text:
            positions_xml = doc_r.text
        text = re.sub(r'<[^>]+>', ' ', doc_r.text)
        text = re.sub(r'\s+', ' ', text).strip()
        combined_text += text + "\n\n"
    return combined_text[:15000] or None, positions_xml, index_url

def parse_positions_from_xml(xml_text):
    if not xml_text:
        return {}
    parser_data = {}
    entries = re.findall(r'<infoTable>(.*?)</infoTable>', xml_text, re.DOTALL)
    for entry in entries:
        def get(tag):
            m = re.search(rf'<{tag}[^>]*>(.*?)</{tag}>', entry, re.DOTALL)
            return m.group(1).strip() if m else ""
        name = get("nameOfIssuer").upper()
        value = int(get("value") or 0) * 1000
        shares = int(get("sshPrnamt") or 0)
        put_call = get("putCall")
        sh_type = get("sshPrnamtType")
        cusip = get("cusip")
        key = f"{name}_{put_call}" if put_call else name
        if key in parser_data:
            parser_data[key]["value"] += value
            parser_data[key]["shares"] += shares
        else:
            parser_data[key] = {
                "name": name, "value": value, "shares": shares,
                "putCall": put_call, "shType": sh_type, "cusip": cusip,
            }
    return parser_data

def compare_positions(new_positions, prev_positions):
    changes = {}
    total_value = sum(p["value"] for p in new_positions.values())
    for key, pos in new_positions.items():
        pct = (pos["value"] / total_value * 100) if total_value else 0
        pos["pct"] = round(pct, 2)
        if key not in prev_positions:
            changes[key] = "NEW"
        else:
            prev_val = prev_positions[key].get("value", 0)
            if prev_val > 0:
                change_pct = (pos["value"] - prev_val) / prev_val * 100
                change_dollar = pos["value"] - prev_val
                if abs(change_pct) > 1:
                    changes[key] = {
                        "type": "INCREASED" if change_pct > 0 else "DECREASED",
                        "pct": round(change_pct, 1),
                        "dollar": change_dollar,
                    }
    today = date.today().isoformat()
    for key, pos in prev_positions.items():
        if key not in new_positions:
            changes[key] = {
                "type": "EXITED",
                "exited_date": today,
                "prev_value": pos.get("value", 0),
                "name": pos.get("name", key),
                "putCall": pos.get("putCall", ""),
            }
    return changes, total_value

def extract_tickers_from_filing(filing_text):
    tickers = set()
    matches = re.findall(r'\b([A-Z]{1,5})\b', filing_text)
    ignore = {"THE", "AND", "FOR", "LLC", "INC", "LTD", "ETF", "SEC", "USD",
              "AUM", "AGI", "AI", "LP", "NA", "DE", "CA", "NY", "OR", "PUT",
              "CALL", "SHS", "COM", "CL", "TR", "NEW", "OLD", "II", "III", "CORP"}
    for m in matches:
        if m not in ignore and len(m) >= 2:
            tickers.add(m)
    tickers.update(["BE", "GE", "NU", "ET"])
    return list(tickers)

def fetch_prices(tickers):
    prices = {}
    for ticker in tickers:
        price = None
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1d"
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            data = r.json()
            result = data.get("chart", {}).get("result")
            if result:
                price = result[0]["meta"]["regularMarketPrice"]
        except:
            pass
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

def change_badge_html(key, changes):
    c = changes.get(key, "")
    if not c:
        return ""
    if c == "NEW":
        return '<span style="background:rgba(76,175,125,0.2);color:#4caf7d;font-size:10px;padding:1px 6px;border-radius:2px;margin-left:6px;">NEW</span>'
    if isinstance(c, dict):
        if c["type"] == "INCREASED":
            return f'<span style="background:rgba(76,175,125,0.2);color:#4caf7d;font-size:10px;padding:1px 6px;border-radius:2px;margin-left:6px;">+{c["pct"]}% (+${abs(c["dollar"]):,.0f})</span>'
        if c["type"] == "DECREASED":
            return f'<span style="background:rgba(224,82,82,0.2);color:#e05252;font-size:10px;padding:1px 6px;border-radius:2px;margin-left:6px;">{c["pct"]}% (-${abs(c["dollar"]):,.0f})</span>'
        if c["type"] == "EXITED":
            return '<span style="background:rgba(224,82,82,0.2);color:#e05252;font-size:10px;padding:1px 6px;border-radius:2px;margin-left:6px;">EXITED</span>'
    return ""

def summarize_with_claude(filing_text, form_type, prices_str="", changes=None):
    topics_str = "\n".join(f"- {t}" for t in SUMMARY_TOPICS)
    position_str = "\n".join(f"- {t}" for t in POSITION_TOPICS)
    action_str = "\n".join(f"- {t}" for t in ACTION_TOPICS)
    changes_str = json.dumps(changes or {}, indent=2)
    prompt = f"""You are analyzing an SEC {form_type} filing for {FUND_NAME}, an AI-focused hedge fund run by Leopold Aschenbrenner.

Filing text:
{filing_text}

Position changes vs previous filing:
{changes_str}

Provide three sections with EXACTLY these headers:

SECTION 1 - MACRO & PHILOSOPHY
5-8 sentences on market outlook, macro themes, philosophy shifts. Plain English.
Topics: {topics_str}

SECTION 2 - POSITION CHANGES
3-8 bullet points on dollar-level moves. Include new, increased, decreased, exited vs last quarter.
Topics: {position_str}

Live prices for Section 3:
{prices_str}

SECTION 3 - ACTIONABLE INSIGHTS FOR RETAIL INVESTORS
3-5 bullets. Lead with BUY/AVOID/WATCH, ticker, 2-3 sentences. Use live prices. Suggest public proxies for illiquid positions.
Topics: {action_str}

Write section 3 like a knowledgeable friend. Clear, direct, no jargon."""
    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": "claude-opus-4-5", "max_tokens": 1500, "messages": [{"role": "user", "content": prompt}]},
        timeout=60,
    )
    response.raise_for_status()
    return response.json()["content"][0]["text"]

def build_positions_data(new_positions, changes, filing_date, accession, prev_positions):
    cik_clean = CIK.lstrip("0")
    acc_clean = accession.replace("-", "")
    filing_url = f"https://www.sec.gov/Archives/edgar/data/{cik_clean}/{acc_clean}/{accession}-index.html"
    total_value = sum(p["value"] for p in new_positions.values())
    positions_list = []
    for key, pos in sorted(new_positions.items(), key=lambda x: -x[1]["value"]):
        c = changes.get(key, "")
        if c == "NEW":
            change_str = "NEW"
        elif isinstance(c, dict) and c["type"] in ("INCREASED", "DECREASED"):
            change_str = f"{c['type']}|{c['pct']}|{int(c['dollar'])}"
        else:
            change_str = ""
        positions_list.append({
            "key": key, "name": pos["name"], "value": pos["value"],
            "shares": pos["shares"], "putCall": pos.get("putCall", ""),
            "shType": pos.get("shType", ""), "pct": pos.get("pct", 0),
            "change": change_str,
        })
    today = date.today().isoformat()
    exited_list = []
    for key, c in changes.items():
        if isinstance(c, dict) and c.get("type") == "EXITED":
            exited_list.append({
                "key": key, "name": c.get("name", key),
                "putCall": c.get("putCall", ""),
                "prev_value": c.get("prev_value", 0),
                "exited_date": c.get("exited_date", today),
            })
    unique_companies = len(set(p["name"] for p in new_positions.values()))
    return {
        "filing_date": filing_date, "filing_url": filing_url,
        "total_aum": total_value, "unique_companies": unique_companies,
        "total_rows": len(new_positions),
        "top_position_name": positions_list[0]["name"] if positions_list else "",
        "top_pct": positions_list[0]["pct"] if positions_list else 0,
        "positions": positions_list, "exited": exited_list,
        "updated_at": datetime.now().isoformat(),
    }

def send_alert(filing, summary, filing_url, new_positions, changes):
    summary = re.sub(r'#+\s*', '', summary)
    parts = summary.split("SECTION 2")
    section1 = parts[0].replace("SECTION 1 - MACRO & PHILOSOPHY", "").replace("SECTION 1 — MACRO & PHILOSOPHY", "").strip() if parts else summary
    remainder = parts[1] if len(parts) > 1 else ""
    parts2 = remainder.split("SECTION 3")
    section2 = parts2[0].replace("- POSITION CHANGES", "").replace("— POSITION CHANGES", "").strip()
    section3 = parts2[1].replace("- ACTIONABLE INSIGHTS FOR RETAIL INVESTORS", "").replace("— ACTIONABLE INSIGHTS FOR RETAIL INVESTORS", "").strip() if len(parts2) > 1 else ""

    def to_bullets(text):
        text = re.sub(r'\*\*(.*?)\*\*', r'<strong style="color:#ffffff;">\1</strong>', text)
        lines = [l.strip().lstrip("-*").strip() for l in text.strip().splitlines() if l.strip()]
        return "".join(
            f'<div style="display:flex;gap:10px;margin-bottom:8px;"><span style="color:#f5a623;flex-shrink:0;">▸</span>'
            f'<p style="font-size:13px;color:#cccccc;margin:0;line-height:1.6;">{l}</p></div>'
            for l in lines if l
        )

    total_value = sum(p["value"] for p in new_positions.values())
    unique_companies = len(set(p["name"] for p in new_positions.values()))
    positions_rows = ""
    for key, pos in sorted(new_positions.items(), key=lambda x: -x[1]["value"]):
        pct = pos.get("pct", 0)
        put_call = pos.get("putCall", "")
        badge = f' <span style="font-size:10px;color:#888;">[{put_call}]</span>' if put_call else ""
        chg = change_badge_html(key, changes)
        val_str = f"${pos['value']/1e9:.2f}B" if pos['value'] >= 1e9 else f"${pos['value']/1e6:.0f}M"
        positions_rows += f'<tr><td style="padding:5px 8px;font-family:monospace;font-size:12px;color:#f5a623;border-bottom:0.5px solid #1a1a1a;">{pos["name"]}{badge}{chg}</td><td style="padding:5px 8px;font-family:monospace;font-size:12px;color:#ccc;text-align:right;border-bottom:0.5px solid #1a1a1a;">{val_str}</td><td style="padding:5px 8px;font-family:monospace;font-size:12px;color:#888;text-align:right;border-bottom:0.5px solid #1a1a1a;">{pct:.1f}%</td></tr>'
    exited_rows = ""
    for key, c in changes.items():
        if isinstance(c, dict) and c.get("type") == "EXITED":
            val_str = f"${c['prev_value']/1e6:.0f}M"
            exited_rows += f'<tr><td style="padding:5px 8px;font-family:monospace;font-size:12px;color:#e05252;border-bottom:0.5px solid #1a1a1a;">{c.get("name", key)} [EXITED]</td><td style="padding:5px 8px;font-family:monospace;font-size:12px;color:#888;text-align:right;border-bottom:0.5px solid #1a1a1a;">{val_str}</td><td style="padding:5px 8px;font-family:monospace;font-size:12px;color:#888;text-align:right;border-bottom:0.5px solid #1a1a1a;">—</td></tr>'

    html = f"""<!DOCTYPE html><html><body style="margin:0;padding:0;background:#000;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#000;"><tr><td align="center" style="padding:2rem 1rem;">
<table width="620" cellpadding="0" cellspacing="0" style="background:#0a0a0a;border-radius:8px;font-family:monospace;color:#e0e0e0;">
<tr><td style="padding:2rem;">
<table width="100%" cellpadding="0" cellspacing="0" style="border-bottom:1px solid #f5a623;padding-bottom:1rem;margin-bottom:1.5rem;">
<tr><td><p style="font-size:11px;color:#f5a623;margin:0 0 4px;letter-spacing:2px;">SITUATIONAL AWARENESS LP</p><p style="font-size:18px;font-weight:500;margin:0;color:#fff;">SEC FILING ALERT</p></td>
<td align="right"><p style="font-size:11px;color:#888;margin:0 0 2px;">FORM TYPE</p><p style="font-size:13px;color:#f5a623;margin:0;">{filing['form']}</p></td></tr>
<tr><td colspan="2" style="padding-top:1rem;"><table cellpadding="0" cellspacing="0"><tr>
<td style="padding-right:2rem;"><p style="font-size:10px;color:#666;margin:0 0 2px;letter-spacing:1px;">FILED</p><p style="font-size:12px;color:#ccc;margin:0;">{filing['date']}</p></td>
<td style="padding-right:2rem;"><p style="font-size:10px;color:#666;margin:0 0 2px;letter-spacing:1px;">COMPANIES</p><p style="font-size:12px;color:#ccc;margin:0;">{unique_companies}</p></td>
<td><p style="font-size:10px;color:#666;margin:0 0 2px;letter-spacing:1px;">TOTAL AUM</p><p style="font-size:12px;color:#ccc;margin:0;">${total_value/1e9:.2f}B</p></td>
</tr></table></td></tr></table>
<p style="font-size:10px;color:#f5a623;letter-spacing:2px;margin:0 0 10px;">01 / MACRO & PHILOSOPHY</p>
<div style="border-left:2px solid #f5a623;padding-left:1rem;margin-bottom:1.5rem;"><p style="font-size:13px;line-height:1.7;color:#ccc;margin:0;">{section1}</p></div>
<div style="border-top:0.5px solid #222;margin:1.5rem 0;"></div>
<p style="font-size:10px;color:#f5a623;letter-spacing:2px;margin:0 0 10px;">02 / POSITION CHANGES</p>
<div style="margin-bottom:1rem;">{to_bullets(section2)}</div>
<table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:1.5rem;border:0.5px solid #1a1a1a;border-radius:4px;">
<tr><td style="padding:4px 8px;font-size:10px;color:#666;letter-spacing:1px;border-bottom:0.5px solid #333;">POSITION</td><td style="padding:4px 8px;font-size:10px;color:#666;letter-spacing:1px;text-align:right;border-bottom:0.5px solid #333;">VALUE</td><td style="padding:4px 8px;font-size:10px;color:#666;letter-spacing:1px;text-align:right;border-bottom:0.5px solid #333;">% PORT</td></tr>
{positions_rows}{exited_rows}</table>
<div style="border-top:0.5px solid #222;margin:1.5rem 0;"></div>
<p style="font-size:10px;color:#f5a623;letter-spacing:2px;margin:0 0 10px;">03 / WHAT TO DO</p>
<div style="margin-bottom:1.5rem;">{to_bullets(section3)}</div>
<div style="border-top:0.5px solid #222;margin:1.5rem 0;"></div>
<table width="100%" cellpadding="0" cellspacing="0"><tr>
<td><p style="font-size:11px;color:#444;margin:0;">Generated by SEC Filing Tracker</p></td>
<td align="right"><a href="{filing_url}" style="font-size:11px;color:#f5a623;text-decoration:none;letter-spacing:1px;">VIEW FULL FILING</a></td>
</tr></table>
</td></tr></table></td></tr></table></body></html>"""

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

def update_index_html(positions_data):
    try:
        with open("index.html", "r") as f:
            html = f.read()
        new_json = json.dumps(positions_data)
        html = re.sub(r'const POSITIONS_DATA = \{.*?\};', f'const POSITIONS_DATA = {new_json};', html, flags=re.DOTALL)
        with open("index.html", "w") as f:
            f.write(html)
        print("index.html updated with new positions data")
    except Exception as e:
        print(f"Could not update index.html: {e}")

def main():
    seen = load_seen()
    filings = fetch_recent_filings()
    new_filings = [f for f in filings if f["accession"] not in seen]
    if not new_filings:
        print(f"[{datetime.now():%Y-%m-%d %H:%M}] No new filings.")
        return
    for filing in new_filings:
        print(f"New filing found: {filing['form']} on {filing['date']}")
        filing_text, positions_xml, filing_url = fetch_filing_documents(filing["accession"])
        prev_positions = load_previous_positions()
        new_positions = parse_positions_from_xml(positions_xml) if positions_xml else {}
        changes, total_value = compare_positions(new_positions, prev_positions)
        raw_tickers = extract_tickers_from_filing(filing_text or "")
        prices = fetch_prices(raw_tickers)
        prices = {k: v for k, v in prices.items() if v}
        prices_str = "\n".join(f"{t}: {p}" for t, p in prices.items())
        if filing_text:
            summary = summarize_with_claude(filing_text, filing['form'], prices_str, changes)
        else:
            summary = "Could not retrieve filing text for summarization."
        positions_data = build_positions_data(new_positions, changes, filing['date'], filing['accession'], prev_positions)
        save_positions(new_positions)
        update_index_html(positions_data)
        if os.environ.get("TEST_MODE") == "true":
            print("TEST MODE — no email sent.")
            print(summary)
        else:
            send_alert(filing, summary, filing_url, new_positions, changes)
            seen.add(filing["accession"])
    save_seen(seen)

if __name__ == "__main__":
    main()
