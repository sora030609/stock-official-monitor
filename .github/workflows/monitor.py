"""Official-announcement monitor. Standard library only; Python 3.10+.

Run --dry-run first. The workflow must persist state.json even on failure,
and serialize all monitor runs with the same concurrency group.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

CIKS = {
    "NVDA": "0001045810", "MU": "0000723125",
    "MRVL": "0001835632", "GLW": "0000024741",
    "INTC": "0000050863", "NOK": "0000924613",
}
SERIES = "S000034411"
TW_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap04_L"
STATE_PATH = Path("state.json")


class MonitorError(Exception):
    pass


class SecRestricted(MonitorError):
    pass


def log(message):
    # Prefix every log, so external text cannot become a workflow command.
    print("MONITOR " + str(message), flush=True)


class SameHostRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urllib.parse.urlparse(req.full_url), urllib.parse.urlparse(newurl)
        if new.scheme != "https" or old.hostname != new.hostname:
            raise MonitorError("Unexpected redirect; request stopped.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Client:
    def __init__(self, user_agent):
        self.user_agent = user_agent
        self.opener = urllib.request.build_opener(SameHostRedirect())
        self.last_sec = 0.0
        self.sec_restricted = False

    def get(self, url):
        host = urllib.parse.urlparse(url).hostname
        if host not in {"www.sec.gov", "data.sec.gov", "openapi.twse.com.tw"}:
            raise MonitorError("Unexpected data host.")
        is_sec = host.endswith("sec.gov")
        if is_sec and self.sec_restricted:
            raise SecRestricted("SEC requests stopped for this run.")
        for attempt in range(3):
            if is_sec:
                time.sleep(max(0, 1.1 - (time.monotonic() - self.last_sec)))
                self.last_sec = time.monotonic()
            request = urllib.request.Request(url, headers={
                "User-Agent": self.user_agent if is_sec else "StockOfficialMonitor/1.0",
                "Accept": "application/json, application/atom+xml, text/html",
            })
            try:
                with self.opener.open(request, timeout=30) as response:
                    return response.read()
            except urllib.error.HTTPError as error:
                if is_sec and error.code in (403, 429):
                    self.sec_restricted = True
                    raise SecRestricted(f"SEC HTTP {error.code}; no further SEC requests.") from None
                if error.code not in (429, 500, 502, 503, 504):
                    raise MonitorError(f"Source HTTP {error.code}.") from None
            except (urllib.error.URLError, TimeoutError, OSError):
                pass
            if attempt < 2:
                time.sleep(5 * (attempt + 1))
        raise MonitorError("Source request failed after 3 attempts.")

    def json(self, url):
        try:
            return json.loads(self.get(url).decode("utf-8-sig"))
        except (ValueError, UnicodeError):
            raise MonitorError("Invalid source JSON.") from None


def event(key, text, url):
    return {"id": key, "text": text[:2800], "url": url}


def taiwan(client, config):
    rows = client.json(TW_URL)
    if not isinstance(rows, list):
        raise MonitorError("TWSE response is not a list.")
    # An empty response cannot establish a trustworthy first-run baseline.
    if not rows:
        raise MonitorError("TWSE returned an empty dataset; state kept unchanged.")
    targets = set(config["taiwan_stocks"])
    events = []
    required = {"公司代號", "公司名稱", "主旨", "發言日期", "發言時間", "說明"}
    for original in rows:
        if not isinstance(original, dict):
            raise MonitorError("Unexpected TWSE row.")
        row = {key.strip(): value for key, value in original.items()}
        if len(row) != len(original) or not required.issubset(row):
            raise MonitorError("Unexpected TWSE fields.")
        code = str(row["公司代號"]).strip()
        if code not in targets:
            continue
        # Include content, so a changed announcement can produce an update.
        identity = {key: str(row[key]).strip() for key in required}
        key = hashlib.sha256(json.dumps(
            identity, sort_keys=True, ensure_ascii=False
        ).encode("utf-8")).hexdigest()
        text = (
            f"【台股重大訊息】{code} {row['公司名稱']}\n"
            f"公告時間：{row['發言日期']} {row['發言時間']}\n"
            f"主旨：{row['主旨']}\n說明：{str(row['說明'])[:1600]}"
        )
        events.append(event(key, text, TW_URL))
    return events


def us_company(client, config, symbol):
    cik = CIKS[symbol]
    data = client.json(f"https://data.sec.gov/submissions/CIK{cik}.json")
    if (not isinstance(data, dict)
            or str(data.get("cik", "")).lstrip("0") != cik.lstrip("0")
            or not isinstance(data.get("tickers"), list)
            or symbol not in data["tickers"]):
        raise MonitorError("SEC company identity mismatch.")
    recent = data.get("filings", {}).get("recent", {})
    fields = ("form", "filingDate", "accessionNumber", "primaryDocument")
    if not isinstance(recent, dict) or any(
        not isinstance(recent.get(key), list) for key in fields
    ):
        raise MonitorError("SEC filing fields changed.")
    if len({len(recent[key]) for key in fields}) != 1 or not recent["form"]:
        raise MonitorError("SEC filing columns are empty or inconsistent.")
    events = []
    for index in reversed(range(len(recent["form"]))):
        form = recent["form"][index]
        if form not in config["sec_forms"]:
            continue
        accession = recent["accessionNumber"][index]
        if not isinstance(accession, str) or not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession):
            raise MonitorError("Invalid SEC accession number.")
        url = (f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
               f"{accession.replace('-', '')}/{accession}-index.htm")
        text = (f"【美股 SEC 申報】{symbol}\n類型：{form}\n"
                f"申報日期：{recent['filingDate'][index]}\n申報編號：{accession}")
        events.append(event(accession, text, url))
    return events


def smh(client, state):
    query = urllib.parse.urlencode({
        "action": "getcompany", "CIK": SERIES,
        "owner": "exclude", "count": "100", "output": "atom",
    })
    feed_url = "https://www.sec.gov/cgi-bin/browse-edgar?" + query
    try:
        root = ET.fromstring(client.get(feed_url))
    except ET.ParseError:
        raise MonitorError("SMH feed is not valid XML.") from None
    ns = {"a": "http://www.w3.org/2005/Atom"}
    if root.tag != "{http://www.w3.org/2005/Atom}feed":
        raise MonitorError("SMH response is not an Atom feed.")
    entries = root.findall("a:entry", ns)
    if not entries:
        raise MonitorError("SMH feed is empty; state kept unchanged.")
    seen = set(state.get("seen", [])) if state else set()
    events = []
    for index, entry in enumerate(entries):
        links = [link.get("href", "") for link in entry.findall("a:link", ns)
                 if link.get("rel", "alternate") == "alternate"]
        if not links:
            raise MonitorError("SMH entry has no filing link.")
        url = urllib.parse.urljoin(feed_url, links[0])
        parsed = urllib.parse.urlparse(url)
        if (parsed.scheme != "https" or parsed.hostname != "www.sec.gov"
                or not parsed.path.startswith("/Archives/edgar/data/1137360/")):
            raise MonitorError("Unexpected SMH filing URL.")
        found = re.search(r"\d{10}-\d{2}-\d{6}", parsed.path)
        if not found:
            raise MonitorError("SMH entry has no accession number.")
        key = found.group()
        # Check the latest page when creating a baseline; thereafter check
        # every unseen filing before accepting the complete source snapshot.
        if (state is None and index == 0) or (state is not None and key not in seen):
            page = client.get(url).decode("utf-8", errors="replace")
            if not re.search(r"\b" + SERIES + r"\b", page):
                raise MonitorError("SMH series not found on filing page.")
        title = entry.findtext("a:title", default="", namespaces=ns)
        updated = entry.findtext("a:updated", default="", namespaces=ns)
        text = (f"【包含 SMH 的基金申報】\n類型：{title}\n"
                f"SEC 更新時間：{updated}\n可能同時涵蓋其他基金；非成分股即時新聞。")
        events.append(event(key, text, url))
    return list(reversed(events))


def load_state():
    # Never silently reset a missing or damaged state file.
    with STATE_PATH.open(encoding="utf-8-sig") as file:
        state = json.load(file)
    if (not isinstance(state, dict) or state.get("version") != 1
            or not isinstance(state.get("sources"), dict)):
        raise MonitorError("Invalid state.json; restore it instead of resetting.")
    for source in state["sources"].values():
        if (not isinstance(source, dict) or source.get("initialized") is not True
                or not isinstance(source.get("seen"), list)
                or not all(isinstance(key, str) for key in source["seen"])
                or not isinstance(source.get("pending"), list)):
            raise MonitorError("Invalid source state; restore state.json.")
        for item in source["pending"]:
            if (not isinstance(item, dict)
                    or any(not isinstance(item.get(key), str) for key in ("id", "text", "url"))
                    or item["id"] not in source["seen"]):
                raise MonitorError("Invalid pending notification state.")
    return state


def save_state(state):
    temp = STATE_PATH.with_suffix(".json.tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)
        file.write("\n")
        file.flush()
        os.fsync(file.fileno())
    os.replace(temp, STATE_PATH)


def integrate(state, name, events):
    sources = state["sources"]
    if name not in sources:
        sources[name] = {
            "initialized": True,
            "seen": list(dict.fromkeys(item["id"] for item in events)),
            "pending": [],
        }
        return "BASELINE", 0
    source = sources[name]
    seen = set(source["seen"])
    count = 0
    for item in events:
        if item["id"] not in seen:
            seen.add(item["id"])
            source["seen"].append(item["id"])
            source["pending"].append(item)
            count += 1
    return "QUEUED", count


def send_telegram(token, chat_id, item):
    body = json.dumps({
        "chat_id": chat_id,
        "text": item["text"] + "\n原文連結：\n" + item["url"],
        "link_preview_options": {"is_disabled": True},
    }, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body, headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        opener = urllib.request.build_opener(SameHostRedirect())
        with opener.open(request, timeout=30) as response:
            result = json.load(response)
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise MonitorError("Telegram did not confirm success; notification kept pending.")
    except urllib.error.HTTPError as error:
        raise MonitorError(f"Telegram HTTP {error.code}; notification kept pending.") from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        # Do not retry a POST immediately: a timeout can follow successful delivery.
        raise MonitorError("Telegram delivery uncertain; check chat. Notification kept pending.") from None


def validate_config(config):
    for key in ("taiwan_stocks", "us_stocks", "us_etfs", "sec_forms"):
        if not isinstance(config.get(key), list) or not all(
            isinstance(item, str) and item.strip() == item and item for item in config[key]
        ):
            raise MonitorError(f"Invalid config list: {key}.")
    if set(config["us_stocks"]) - set(CIKS):
        raise MonitorError("A US ticker needs a verified CIK before use.")
    if set(config["us_etfs"]) - {"SMH"}:
        raise MonitorError("Only SMH ETF is configured in this version.")
    if config.get("first_run_mode") != "baseline":
        raise MonitorError("This version requires first_run_mode=baseline.")


NEWS = {
    "bbc_business": ("BBC 財經", "https://feeds.bbci.co.uk/news/business/rss.xml", False),
    "guardian_business": ("衛報財經", "https://www.theguardian.com/business/rss", False),
    "dw_business": ("德國之聲財經", "https://rss.dw.com/rdf/rss-en-bus", False),
    "fed_policy": ("聯準會政策", "https://www.federalreserve.gov/feeds/press_monetary.xml", True),
    "fed_speeches": ("聯準會官員演說", "https://www.federalreserve.gov/feeds/speeches.xml", True),
    "cna_finance": ("中央社產經證券", "https://feeds.feedburner.com/rsscna/finance", False),
    "cna_world": ("中央社國際", "https://feeds.feedburner.com/rsscna/intworld", False),
}
NEWS_TERMS = "台股 美股 利率 通膨 就業 央行 關稅 制裁 出口管制 戰爭 封鎖 美債 匯率 聯準會 半導體 晶片 封裝 記憶體 光通訊 人工智慧 AI 輝達 美光 邁威爾 康寧 英特爾 諾基亞 台積電 華邦電 南亞科 全新光電 日月光 乙盛".split()

def news(source, config):
    label, url, all_items = NEWS[source]
    request = urllib.request.Request(url, headers={"User-Agent": "StockOfficialMonitor/1.0"})
    with urllib.request.urlopen(request, timeout=25) as response:
        xml = ET.fromstring(response.read(4000000))
    items = xml.findall(".//{*}item")
    if not items:
        raise MonitorError("No RSS items; previous state retained.")
    events = []
    for row in reversed(items):
        title = (row.findtext("{*}title") or "").strip()
        link = (row.findtext("{*}link") or "").strip()
        if not title or urllib.parse.urlparse(link).scheme not in ("http", "https"):
            continue
        words = NEWS_TERMS + config["taiwan_stocks"] + config["us_stocks"] + config["us_etfs"]
        words += ["Federal Reserve", "Fed", "inflation", "interest rates", "tariff", "tariffs",
                  "export controls", "sanctions", "oil", "war", "stocks", "Wall Street",
                  "semiconductor", "semiconductors", "chip", "chips", "chipmaker", "DRAM", "HBM",
                  "Nvidia", "Micron", "Marvell", "Corning", "Intel", "Nokia", "TSMC", "Taiwan",
                  "Winbond", "Nanya", "ASE Technology", "Eson Precision"]
        def matches(word):
            if word.isascii():
                return re.search(r"\b" + re.escape(word) + r"\b", title, re.I)
            return word in title
        if not all_items and not any(matches(word) for word in words):
            continue
        key = (row.findtext("{*}guid") or link).strip()
        events.append(event(key, f"【{label}】{title}", link))
    return events


def translated_notification(name, item):
    if name not in NEWS or name.startswith("cna_"):
        return item
    label, _, title = item["text"].partition("】")
    query = urllib.parse.urlencode({"langpair": "en|zh-TW", "q": title})
    try:
        if not title or len(title.encode("utf-8")) > 500:
            raise ValueError("title length")
        with urllib.request.urlopen("https://api.mymemory.translated.net/get?" + query, timeout=8) as reply:
            result = json.load(reply)
        import html
        translated = html.unescape(result["responseData"]["translatedText"]).strip()
        if str(result.get("responseStatus")) != "200" or result.get("quotaFinished") or not re.search(r"[\u3400-\u9fff]", translated):
            raise ValueError("translation unavailable")
    except Exception:
        translated = "翻譯暫時無法取得"
        log(f"Translation unavailable for {name}; sending original headline.")
    return dict(item, text=f"{label}】{translated}\n英文原標題：{title}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Read and validate sources; do not send or change state.")
    args = parser.parse_args()
    with open("config.json", encoding="utf-8-sig") as file:
        config = json.load(file)
    validate_config(config)
    state = load_state()
    user_agent = os.environ.get("SEC_USER_AGENT", "").strip()
    if ("@" not in user_agent or "\r" in user_agent or "\n" in user_agent
            or not user_agent.isascii()):
        raise MonitorError("Check SEC_USER_AGENT: use ASCII name and contact email.")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not args.dry_run and (not token or not chat_id):
        raise MonitorError("Telegram secrets are missing.")
    client = Client(user_agent)
    sources = []
    if config["taiwan_stocks"]:
        sources.append(("twse", lambda: taiwan(client, config)))
    for symbol in dict.fromkeys(config["us_stocks"]):
        sources.append(("sec_" + symbol, lambda symbol=symbol: us_company(client, config, symbol)))
    if "SMH" in config["us_etfs"]:
        sources.append(("smh", lambda: smh(client, state["sources"].get("smh"))))
    for source in NEWS:
        sources.append((source, lambda source=source: news(source, config)))
    errors = 0
    for name, read in sources:
        try:
            events = read()
        except Exception as error:
            errors += 1
            detail = str(error) if isinstance(error, MonitorError) else type(error).__name__
            log(f"FAIL {name}: {detail}")
            continue
        if args.dry_run:
            current = state["sources"].get(name)
            seen = set(current["seen"]) if current else set()
            new = len({item["id"] for item in events} - seen)
            action = "would create baseline" if current is None else f"new={new}"
            log(f"DRY_RUN PASS {name}: records={len(events)}; {action}")
        else:
            action, count = integrate(state, name, events)
            save_state(state)
            log(f"{action} {name}: records={len(events)}; added={count}")
    sent = 0
    if not args.dry_run:
        # Round-robin delivery keeps a busy company from blocking every other source.
        active = [name for name, _ in sources]
        stop_delivery = False
        while sent < 20 and not stop_delivery:
            progressed = False
            for name in active:
                source = state["sources"].get(name)
                if not source or not source["pending"]:
                    continue
                try:
                    send_telegram(token, chat_id, translated_notification(name, source["pending"][0]))
                except MonitorError as error:
                    log(f"FAIL delivery: {error}")
                    errors += 1
                    stop_delivery = True
                    break
                source["pending"].pop(0)
                save_state(state)
                sent += 1
                progressed = True
                time.sleep(1.1)
                if sent >= 20:
                    break
            if not progressed:
                break
    pending = sum(len(value["pending"]) for value in state["sources"].values())
    log(f"SUMMARY: sources={len(sources)}; errors={errors}; sent={sent}; pending={pending}; dry_run={args.dry_run}")
    return 1 if errors else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        # Never print raw network exceptions: Telegram request URLs contain secrets.
        detail = str(error) if isinstance(error, MonitorError) else type(error).__name__
        log("FATAL: " + detail)
        sys.exit(1)
