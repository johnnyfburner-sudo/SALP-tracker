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
ALERT_EMAIL = os.environ["ALERT_EMAIL"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
ALPHA_VANTAGE_KEY = os.environ.get("ALPHA_VANTAGE_KEY", "")
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
    "write like a knowledgeable friend giving real advice — clear, direct, no jargon",
    "use the live prices provided — do not guess or use outdated prices",
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

def sec_headers():
    return {
        "User-Agent": "filing-tracker k.franzmeilinger@gmail.com",
        "Accept": "application/json, application/xml, text/html",
    }

def fetch_recent_filings():
    url = f"https://data.sec.gov/submissions/CIK{CIK}.json"
    try:
        time.sleep(0.3)
        r = requests.get(url, headers=sec_headers(), timeout=15)
        r.raise_for_status()
        data = r.json()
        recent = data.get("filings", {}).get("recent", {})
        filings = []
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        accessions = recent.get("accessionNumber", [])
        for form, filed_date, acc in zip(forms, dates, accessions):
            if any(form.startswith(t.replace("/", "")) or form == t for t in FORM_TYPES) or \
               any(t in form for t in FORM_TYPES):
                filings.append({"form": form, "date": filed_date, "accession": acc})
        return filings
    except Exception as e:
        print(f"Error fetching filings: {e}")
        return []

def parse_positions_from_html(html_text):
    """Parse positions from SEC rendered HTML table"""
    positions = {}
    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', html_text, re.DOTALL | re.IGNORECASE)
    for row in rows:
        cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL | re.IGNORECASE)
        cells = [re.sub(r'<[^>]+>', '', c).strip() for c in cells]
        cells = [re.sub(r'\s+', ' ', c) for c in cells]
        if len(cells) < 5:
            continue
        name = cells[0]
        if not name or name.lower() in ('name of issuer', 'issuer name', ''):
            continue
        # skip header rows
        if name.lower().startswith('name') or name.lower().startswith('issuer'):
            continue
        try:
            cusip = cells[1] if len(cells) > 1 else ""
            value_str = cells[2].replace(',', '').replace('$', '').strip() if len(cells) > 2 else "0"
            shares_str = cells[3].replace(',', '').strip() if len(cells) > 3 else "0"
            share_type = cells[4].strip() if len(cells) > 4 else "SH"
            put_call = cells[5].strip().upper() if len(cells) > 5 else ""
            val_dollars = int(float(value_str)) * 1000
            if val_dollars == 0:
                continue
            key = name.upper().strip()
            if key not in positions:
                positions[key] = {"name": name, "cusip": cusip, "holdings": [], "total_value": 0}
            positions[key]["holdings"].append({
                "value": val_dollars, "shares": shares_str,
                "share_type": share_type, "put_call": put_call if put_call in ("PUT", "CALL") else ""
            })
            positions[key]["total_value"] += val_dollars
        except (ValueError, IndexError):
            continue
    return positions

def parse_positions_from_xml(xml_text):
    """Parse positions from raw XML (handles ns1: namespace prefix)"""
    positions = {}
    # try ns1: prefixed tags first, then plain tags
    entries = re.findall(r'<ns1:infoTable>(.*?)</ns1:infoTable>', xml_text, re.DOTALL | re.IGNORECASE)
    if not entries:
        entries = re.findall(r'<infoTable>(.*?)</infoTable>', xml_text, re.DOTALL | re.IGNORECASE)
    
    def get_field(field, text):
        m = re.search(rf'<(?:ns1:)?{field}[^>]*>(.*?)</(?:ns1:)?{field}>', text, re.DOTALL | re.IGNORECASE)
        return m.group(1).strip() if m else ""

    for entry in entries:
        name = get_field("nameOfIssuer", entry)
        value = get_field("value", entry)
        shares = get_field("sshPrnamt", entry)
        share_type = get_field("sshPrnamtType", entry)
        put_call = get_field("putCall", entry)
        cusip = get_field("cusip", entry)
        if not name or not value:
            continue
        key = name.upper().strip()
        try:
            val_dollars = int(value.replace(",", "")) * 1000
        except:
            continue
        if key not in positions:
            positions[key] = {"name": name, "cusip": cusip, "holdings": [], "total_value": 0}
        positions[key]["holdings"].append({
            "value": val_dollars, "shares": shares,
            "share_type": share_type, "put_call": put_call,
        })
        positions[key]["total_value"] += val_dollars
    return positions

def fetch_filing_data(accession):
    acc_clean = accession.replace("-", "")
    cik_clean = CIK.lstrip("0")
    index_url = f"https://www.sec.gov/Archives/edgar/data/{cik_clean}/{acc_clean}/{accession}-index.htm"
    # also try .html extension
    headers = sec_headers()
    combined_text = ""
    positions = {}
    aum_value = 0
    holdings_count = 0
    filing_url = index_url

    try:
        # fetch AUM from primary_doc.xml
        primary_url = f"https://www.sec.gov/Archives/edgar/data/{cik_clean}/{acc_clean}/primary_doc.xml"
        time.sleep(0.5)
        primary_r = requests.get(primary_url, headers=headers, timeout=15)
        if primary_r.status_code == 200:
            val_match = re.search(r'<tableValueTotal>\s*([\d]+)\s*</tableValueTotal>', primary_r.text)
            cnt_match = re.search(r'<tableEntryTotal>\s*([\d]+)\s*</tableEntryTotal>', primary_r.text)
            if val_match:
                aum_value = int(val_match.group(1))
                print(f"AUM from filing: ${aum_value:,}")
            if cnt_match:
                holdings_count = int(cnt_match.group(1))
                print(f"Holdings count from filing: {holdings_count}")

        # try .htm index first, then .html
        for ext in [".htm", ".html", "-index.htm", "-index.html"]:
            try:
                test_url = f"https://www.sec.gov/Archives/edgar/data/{cik_clean}/{acc_clean}/{accession}{ext}"
                time.sleep(0.3)
                r = requests.get(test_url, headers=headers, timeout=15)
                if r.status_code == 200:
                    filing_url = test_url
                    index_text = r.text
                    break
            except:
                continue
        else:
            index_text = ""

        # find XML links from index
        xml_links = re.findall(
            r'href="(/Archives/edgar/data/[^"]+\.xml)"', index_text, re.IGNORECASE
        )
        xml_links = [l for l in xml_links if "primary_doc" not in l and not l.endswith(".txt")]
        
        # also try direct URL patterns for the info table XML
        direct_xml_urls = [
            f"/Archives/edgar/data/{cik_clean}/{acc_clean}/salp13fq1xml.xml",
            f"/Archives/edgar/data/{cik_clean}/{acc_clean}/SALP_13FQ425.xml",
            f"/Archives/edgar/data/{cik_clean}/{acc_clean}/SALP_13F.xml",
        ]
        for url_path in direct_xml_urls:
            if url_path not in xml_links:
                xml_links.append(url_path)

        print(f"Trying {len(xml_links)} XML links")

        for link in xml_links:
            doc_url = f"https://www.sec.gov{link}"
            time.sleep(0.5)
            doc_r = requests.get(doc_url, headers=headers, timeout=15)
            print(f"Fetching {doc_url} — status {doc_r.status_code}, length {len(doc_r.text)}")
            
            if doc_r.status_code != 200:
                continue

            content = doc_r.text
            
            # detect if it's HTML or XML
            is_html = content.strip().startswith('<!DOCTYPE') or '<html' in content[:200].lower()
            
            if is_html:
                print("Detected HTML format — parsing as table")
                parsed = parse_positions_from_html(content)
            else:
                print("Detected XML format — parsing as XML")
                parsed = parse_positions_from_xml(content)

            if parsed:
                positions.update(parsed)
                print(f"Parsed {len(parsed)} positions from {doc_url}")
                # get plain text for Claude
                text = re.sub(r'<[^>]+>', ' ', content)
                text = re.sub(r'\s+', ' ', text).strip()
                combined_text += text[:8000] + "\n\n"
                break  # stop after first successful parse

        print(f"Total unique positions: {len(positions)}")

    except Exception as e:
        print(f"Could not fetch filing: {e}")
        import traceback
        traceback.print_exc()

    return combined_text or None, positions, filing_url, aum_value, holdings_count

def compute_changes(new_positions, prev_positions):
    total_new = sum(p["total_value"] for p in new_positions.values())
    changes = {}
    for key, pos in new_positions.items():
        pct = (pos["total_value"] / total_new * 100) if total_new else 0
        if key not in prev_positions:
            changes[key] = {"status": "NEW", "pct": pct, "value": pos["total_value"], "prev_value": 0, "delta": pos["total_value"]}
        else:
            prev_val = prev_positions[key]["total_value"]
            delta = pos["total_value"] - prev_val
            delta_pct = (delta / prev_val * 100) if prev_val else 0
            if abs(delta_pct) < 1:
                status = "UNCHANGED"
            elif delta > 0:
                status = "INCREASED"
            else:
                status = "DECREASED"
            changes[key] = {"status": status, "pct": pct, "value": pos["total_value"], "prev_value": prev_val, "delta": delta, "delta_pct": delta_pct}
    for key in prev_positions:
        if key not in new_positions:
            prev_val = prev_positions[key].get("total_value", 0) if isinstance(prev_positions[key], dict) else 0
            changes[key] = {"status": "EXITED", "pct": 0, "value": 0, "prev_value": prev_val, "delta": -prev_val, "exited_date": str(date.today())}
    return changes

def extract_tickers_from_filing(filing_text):
    tickers = set()
    matches = re.findall(r'\b([A-Z]{1,5})\b', filing_text)
    ignore = {"THE", "AND", "FOR", "LLC", "INC", "LTD", "ETF", "SEC", "USD",
              "AUM", "AGI", "AI", "LP", "NA", "DE", "CA", "NY", "OR", "PUT",
              "CALL", "SHS", "COM", "CL", "TR", "NEW", "OLD", "II", "III", "CORP"}
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
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1d"
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            data = r.json()
            result = data.get("chart", {}).get("result")
            if result:
                price = result[0]["meta"]["regularMarketPrice"]
        except:
            pass
        if not price and ALPHA_VANTAGE_KEY:
            try:
                url = f"https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol={ticker}&apikey={ALPHA_VANTAGE_KEY}"
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

def fmt_value(v):
    if abs(v) >= 1e9:
        return f"${v/1e9:.2f}B"
    return f"${v/1e6:.0f}M"

def fmt_aum(v):
    return f"${v/1e9:.1f}B"

def summarize_with_claude(filing_text, form_type, prices_str, changes):
    topics_str = "\n".join(f"- {t}" for t in SUMMARY_TOPICS)
    position_str = "\n".join(f"- {t}" for t in POSITION_TOPICS)
    action_str = "\n".join(f"- {t}" for t in ACTION_TOPICS)

    changes_summary = []
    for key, ch in changes.items():
        if ch["status"] == "NEW":
            changes_summary.append(f"NEW: {key} — ${ch['value']/1e6:.1f}M")
        elif ch["status"] == "EXITED":
            changes_summary.append(f"EXITED: {key} — was ${ch['prev_value']/1e6:.1f}M")
        elif ch["status"] == "INCREASED":
            changes_summary.append(f"INCREASED: {key} — +${ch['delta']/1e6:.1f}M ({ch['delta_pct']:+.1f}%)")
        elif ch["status"] == "DECREASED":
            changes_summary.append(f"DECREASED: {key} — ${ch['delta']/1e6:.1f}M ({ch['delta_pct']:+.1f}%)")
    changes_text = "\n".join(changes_summary) if changes_summary else "No previous filing data for comparison."

    prompt = f"""You are analyzing an SEC {form_type} filing for {FUND_NAME}, an AI-focused hedge fund run by Leopold Aschenbrenner.

Here is the filing text:
{filing_text}

Here are the position changes vs the previous filing:
{changes_text}

Please provide three sections with EXACTLY these headers:

SECTION 1 — MACRO & PHILOSOPHY
5-8 sentences summarizing the fund's market outlook, macro themes, or investment philosophy. Focus on big picture directional shifts, AGI thesis updates, or sector rotation. Write in plain English as if briefing a smart non-expert.

Topics to focus on:
{topics_str}

SECTION 2 — POSITION CHANGES
3-8 bullet points on the most important dollar-level moves vs the previous quarter. Be specific with numbers. Flag new positions, exits, and significant changes.

Topics to focus on:
{position_str}

Here are today's live prices for stocks found in this filing — use these exact prices in Section 3:
{prices_str}

SECTION 3 — ACTIONABLE INSIGHTS FOR RETAIL INVESTORS
3-5 bullet points written for a normal person who wants to act on this information today. Lead with BUY, AVOID, or WATCH, then company name and ticker, then 2-3 sentences. Use live prices provided.

Topics to focus on:
{action_str}

Write section 3 like a knowledgeable friend — clear, direct, no jargon."""

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

def change_indicator_text(ch):
    status = ch.get("status", "")
    if status == "NEW": return "🟢 NEW"
    if status == "EXITED": return "🔴 EXITED"
    if status == "INCREASED": return f"▲ +{fmt_value(abs(ch['delta']))} ({ch['delta_pct']:+.1f}%)"
    if status == "DECREASED": return f"▼ -{fmt_value(abs(ch['delta']))} ({ch['delta_pct']:+.1f}%)"
    return ""

def update_index_html(new_positions, changes, filing, aum_value, holdings_count):
    try:
        with open("index.html", "r") as f:
            html = f.read()

        total_value = sum(p["total_value"] for p in new_positions.values())
        sorted_positions = sorted(new_positions.items(), key=lambda x: -x[1]["total_value"])

        positions_js_list = []
        for key, pos in sorted_positions:
            pct = round(pos["total_value"] / total_value * 100, 2) if total_value else 0
            ch = changes.get(key, {})
            change_data = None
            if ch.get("status") in ("NEW", "INCREASED", "DECREASED", "EXITED"):
                change_data = {"status": ch["status"], "delta": ch.get("delta", 0), "delta_pct": ch.get("delta_pct", 0)}
            entry = {"key": key, "name": pos["name"], "value": pos["total_value"], "pct": pct, "holdings": pos["holdings"]}
            if change_data:
                entry["change"] = change_data
            positions_js_list.append(entry)

        for key, ch in changes.items():
            if ch["status"] == "EXITED":
                positions_js_list.append({
                    "key": key, "name": key, "value": 0, "pct": 0, "holdings": [],
                    "change": {"status": "EXITED", "delta": -ch["prev_value"], "delta_pct": -100, "prev_value": ch["prev_value"]},
                })

        positions_json = json.dumps(positions_js_list)
        html = re.sub(r'const KNOWN_POSITIONS = \[.*?\];', f'const KNOWN_POSITIONS = {positions_json};', html, flags=re.DOTALL)

        aum_str = fmt_aum(aum_value) if aum_value else fmt_value(total_value)
        count_str = str(holdings_count) if holdings_count else str(len(new_positions))
        filing_date = filing['date']

        html = re.sub(r'document\.getElementById\("statAum"\)\.textContent = "[^"]*";', f'document.getElementById("statAum").textContent = "{aum_str}";', html)
        html = re.sub(r'document\.getElementById\("statHoldings"\)\.textContent = "[^"]*";', f'document.getElementById("statHoldings").textContent = "{count_str}";', html)
        html = re.sub(r'document\.getElementById\("statFiled"\)\.textContent = "[^"]*";', f'document.getElementById("statFiled").textContent = "Filed {filing_date}";', html)

        with open("index.html", "w") as f:
            f.write(html)

        print(f"index.html updated — AUM: {aum_str}, Holdings: {count_str}, Positions: {len(sorted_positions)}")

    except Exception as e:
        print(f"Could not update index.html: {e}")
        import traceback
        traceback.print_exc()

def send_alert(filing, summary, filing_url, new_positions, changes, aum_value, holdings_count):
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

    total_value = sum(p["total_value"] for p in new_positions.values())
    sorted_pos = sorted(new_positions.items(), key=lambda x: -x[1]["total_value"])
    aum_str = fmt_aum(aum_value) if aum_value else fmt_value(total_value)
    count_str = str(holdings_count) if holdings_count else str(len(new_positions))

    pos_rows = ""
    for key, pos in sorted_pos:
        ch = changes.get(key, {})
        pct = (pos["total_value"] / total_value * 100) if total_value else 0
        indicator = change_indicator_text(ch)
        badges = ""
        for h in pos["holdings"]:
            if h.get("put_call") in ("PUT", "CALL"):
                badges += f' [{h["put_call"]}]'
        indicator_html = ""
        if indicator:
            color = "#4caf7d" if ch.get("status") in ("NEW", "INCREASED") else "#e05252"
            indicator_html = f'<span style="color:{color};font-size:10px;margin-left:8px;">{indicator}</span>'
        pos_rows += f"""
        <tr>
          <td style="padding:6px 0;border-bottom:0.5px solid #222;font-family:monospace;font-size:12px;color:#f5a623;">{pos['name']}{badges}{indicator_html}</td>
          <td style="padding:6px 0;border-bottom:0.5px solid #222;font-family:monospace;font-size:12px;color:#ccc;text-align:right;">{fmt_value(pos['total_value'])}</td>
          <td style="padding:6px 0;border-bottom:0.5px solid #222;font-family:monospace;font-size:12px;color:#888;text-align:right;">{pct:.1f}%</td>
        </tr>"""

    for key, ch in changes.items():
        if ch["status"] == "EXITED":
            pos_rows += f"""
        <tr>
          <td style="padding:6px 0;border-bottom:0.5px solid #222;font-family:monospace;font-size:12px;color:#e05252;opacity:0.6;">{key} 🔴 EXITED</td>
          <td style="padding:6px 0;border-bottom:0.5px solid #222;font-family:monospace;font-size:12px;color:#888;text-align:right;">was {fmt_value(ch['prev_value'])}</td>
          <td style="padding:6px 0;border-bottom:0.5px solid #222;font-family:monospace;font-size:12px;color:#888;text-align:right;">—</td>
        </tr>"""

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
      <td style="padding-right:2rem;">
        <p style="font-size:10px;color:#666666;margin:0 0 2px;letter-spacing:1px;">AUM</p>
        <p style="font-size:12px;color:#cccccc;margin:0;">{aum_str}</p>
      </td>
      <td>
        <p style="font-size:10px;color:#666666;margin:0 0 2px;letter-spacing:1px;">HOLDINGS</p>
        <p style="font-size:12px;color:#cccccc;margin:0;">{count_str}</p>
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
  <div style="margin-bottom:1rem;">{to_bullets(section2)}</div>
  <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:1.5rem;">
    <tr>
      <th style="font-size:10px;color:#666;text-align:left;padding-bottom:6px;font-weight:normal;letter-spacing:1px;">COMPANY</th>
      <th style="font-size:10px;color:#666;text-align:right;padding-bottom:6px;font-weight:normal;letter-spacing:1px;">VALUE</th>
      <th style="font-size:10px;color:#666;text-align:right;padding-bottom:6px;font-weight:normal;letter-spacing:1px;">% PORT</th>
    </tr>
    {pos_rows}
  </table>
  <div style="border-top:0.5px solid #222222;margin:1.5rem 0;"></div>
  <p style="font-size:10px;color:#f5a623;letter-spacing:2px;margin:0 0 10px;">03 / WHAT TO DO</p>
  <div style="margin-bottom:1.5rem;">{to_bullets(section3)}</div>
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
    prev_positions = load_previous_positions()
    filings = fetch_recent_filings()
    new_filings = [f for f in filings if f["accession"] not in seen][:1]

    if not new_filings:
        print(f"[{datetime.now():%Y-%m-%d %H:%M}] No new filings.")
        return

    for filing in new_filings:
        print(f"New filing found: {filing['form']} on {filing['date']}")
        filing_text, new_positions, filing_url, aum_value, holdings_count = fetch_filing_data(filing["accession"])

        if not new_positions:
            print("Could not parse positions from filing.")
            continue

        total_value = sum(p["total_value"] for p in new_positions.values())
        changes = compute_changes(new_positions, prev_positions)

        raw_tickers = extract_tickers_from_filing(filing_text or "")
        prices = fetch_prices(raw_tickers)
        prices = {k: v for k, v in prices.items() if v}
        prices_str = "\n".join(f"{t}: {p}" for t, p in prices.items())
        print(f"Live prices fetched for {len(prices)} tickers")

        if filing_text:
            summary = summarize_with_claude(filing_text, filing['form'], prices_str, changes)
        else:
            summary = "Could not retrieve filing text for summarization."

        if os.environ.get("TEST_MODE") == "true":
            print("TEST MODE — no email sent.")
            print(f"AUM: ${aum_value:,} | Holdings: {holdings_count} | Positions: {len(new_positions)}")
            for k, v in sorted(new_positions.items(), key=lambda x: -x[1]["total_value"])[:5]:
                print(f"  {k}: {fmt_value(v['total_value'])}")
        else:
            update_index_html(new_positions, changes, filing, aum_value, holdings_count)
            send_alert(filing, summary, filing_url, new_positions, changes, aum_value, holdings_count)
            seen.add(filing["accession"])
            save_positions(new_positions)

    save_seen(seen)

if __name__ == "__main__":
    main()
