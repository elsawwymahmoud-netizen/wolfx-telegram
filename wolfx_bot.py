"""
WOLF X -> Telegram (free, runs 24/7 on GitHub Actions)

Loads the ORIGINAL "Wolf X" script (Random_Thoughts123) on TradingView's public
widget data feed (OANDA prices, same as the JIFU scanner), watches every symbol
live, and when a candle closes with a Wolf X Buy/Sell arrow it sends:
Entry (close), Invalid (20-bar low/high), TP1/TP2/TP3 (1R / 3R / 5R).

Env vars: TG_TOKEN, TG_CHAT  (set as GitHub secrets)
Optional: SYMBOLS="OANDA:XAUUSD,OANDA:EURUSD"  TF="15"  RUN_MINUTES="350"
"""
import json, os, random, string, time, urllib.request, urllib.parse, re, traceback

import websocket  # pip install websocket-client

PINE_ID = "PUB;862506c546514212b9728a634dbc7152"
WS_URL = "wss://widgetdata.tradingview.com/socket.io/websocket?from=embed"
HEADERS = ["Origin: https://s.tradingview.com", "User-Agent: Mozilla/5.0"]

TG_TOKEN = os.environ.get("TG_TOKEN", "").strip()
TG_CHAT = os.environ.get("TG_CHAT", "").strip()
SYMBOLS = [s.strip() for s in os.environ.get(
    "SYMBOLS", "OANDA:XAUUSD,OANDA:EURUSD,OANDA:GBPUSD,OANDA:USDJPY").split(",") if s.strip()]
TF = os.environ.get("TF", "15").strip()
RUN_SECONDS = int(float(os.environ.get("RUN_MINUTES", "350")) * 60)
SWING = 20
RR = (1.0, 3.0, 5.0)
TEST = os.environ.get("SEND_TEST", "") == "1"

BIG = 1e10


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def telegram(text):
    if not TG_TOKEN or not TG_CHAT:
        log("TELEGRAM (not configured):", text)
        return
    data = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": text}).encode()
    try:
        urllib.request.urlopen("https://api.telegram.org/bot%s/sendMessage" % TG_TOKEN, data, timeout=20).read()
    except Exception as e:
        log("telegram error", e)


def http_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Origin": "https://s.tradingview.com"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read().decode())


def load_script():
    r = http_json("https://pine-facade.tradingview.com/pine-facade/translate/%s/last" % urllib.parse.quote(PINE_ID, safe=""))
    res = r["result"]
    mi = res["metaInfo"]
    inputs = {"text": res["ilTemplate"], "pineId": mi["scriptIdPart"], "pineVersion": mi["pine"]["version"]}
    for i in mi["inputs"]:
        if i["id"] in ("text", "pineId", "pineVersion"):
            continue
        inputs[i["id"]] = {"v": i["defval"], "f": bool(i.get("isFake")), "t": i["type"]}
    titles = [mi["styles"].get(p["id"], {}).get("title") for p in mi["plots"]]
    bi = titles.index("Buy Arrow") + 1   # +1 because v[0] is time
    si = titles.index("Sell Arrow") + 1
    return inputs, bi, si


def enc(m, p):
    s = json.dumps({"m": m, "p": p}, separators=(",", ":"))
    return "~m~%d~m~%s" % (len(s), s)


def split_frames(raw):
    out, i = [], 0
    while i < len(raw):
        mm = re.match(r"~m~(\d+)~m~", raw[i:])
        if not mm:
            break
        n = int(mm.group(1))
        start = i + mm.end()
        out.append(raw[start:start + n])
        i = start + n
    return out


def rid(p):
    return p + "".join(random.choice(string.ascii_lowercase) for _ in range(12))


class Sym:
    def __init__(self, ticker):
        self.ticker = ticker
        self.cs = rid("cs_")
        self.bars = {}     # idx -> [t,o,h,l,c]
        self.study = {}    # idx -> v
        self.decimals = 5
        self.last_checked_t = None  # time of the last closed bar we evaluated
        self.started = False

    def short(self):
        return self.ticker.split(":")[-1]


def fmt(v, d):
    return ("%." + str(d) + "f") % v


def check_closed(sym, bi, si, start_time):
    """Evaluate every bar that has closed since last check."""
    if len(sym.bars) < SWING + 2:
        return
    idxs = sorted(sym.bars)
    closed = idxs[:-1]  # last bar is still forming
    last_closed_t = sym.bars[closed[-1]][0]
    if sym.last_checked_t is None:
        # first data: only alert on a bar that closed within the last 3 minutes
        sym.last_checked_t = last_closed_t - 1 if time.time() - (last_closed_t + int(TF) * 60) < 180 else last_closed_t
    for k, idx in enumerate(closed):
        b = sym.bars[idx]
        if b[0] <= sym.last_checked_t:
            continue
        sv = sym.study.get(idx)
        if sv is None:
            return  # study not updated yet; try again on next message
        buy = sv[bi] if len(sv) > bi else None
        sell = sv[si] if len(sv) > si else None
        d = 0
        if buy is not None and abs(buy) < BIG and buy != 0:
            d = 1
        elif sell is not None and abs(sell) < BIG and sell != 0:
            d = -1
        sym.last_checked_t = b[0]
        if d == 0:
            continue
        window = [sym.bars[j] for j in closed[max(0, k - SWING + 1):k + 1]]
        entry = b[4]
        sl = min(x[3] for x in window) if d == 1 else max(x[2] for x in window)
        risk = abs(entry - sl)
        tps = [entry + d * risk * r for r in RR]
        dec = sym.decimals
        when = time.strftime("%Y-%m-%d %H:%M", time.gmtime(b[0] + int(TF) * 60 + 3 * 3600))
        head = ("🟢 WOLF X BUY " if d == 1 else "🔴 WOLF X SELL ") + sym.short() + " (" + TF + "m)"
        msg = "\n".join([
            head,
            "Entry: " + fmt(entry, dec),
            "Invalid: " + fmt(sl, dec),
            "TP1: " + fmt(tps[0], dec),
            "TP2: " + fmt(tps[1], dec),
            "TP3: " + fmt(tps[2], dec),
            "Time: " + when + " (UTC+3)",
        ])
        log("SIGNAL", msg.replace("\n", " | "))
        telegram(msg)


def run_once(inputs, bi, si, deadline, syms):
    ws = websocket.create_connection(WS_URL, header=HEADERS, timeout=60)
    ws.send(enc("set_auth_token", ["widget_user_token"]))
    for s in syms:
        s.bars.clear(); s.study.clear()
        ws.send(enc("chart_create_session", [s.cs, ""]))
        ws.send(enc("resolve_symbol", [s.cs, "sds_sym_1", '={"symbol":"%s","adjustment":"splits"}' % s.ticker]))
        ws.send(enc("create_series", [s.cs, "sds_1", "s1", "sds_sym_1", TF, 300, ""]))
        ws.send(enc("create_study", [s.cs, "st1", "st1", "sds_1", "Script@tv-scripting-101!", inputs]))
    by_cs = {s.cs: s for s in syms}
    start = time.time()
    while time.time() < deadline:
        raw = ws.recv()
        for fr in split_frames(raw):
            if fr.startswith("~h~"):
                ws.send("~m~%d~m~%s" % (len(fr), fr))
                continue
            if not fr.startswith("{"):
                continue
            msg = json.loads(fr)
            m, p = msg.get("m"), msg.get("p", [])
            if not p or p[0] not in by_cs:
                continue
            s = by_cs[p[0]]
            if m == "symbol_resolved":
                ps = p[2].get("pricescale", 100000)
                s.decimals = max(0, len(str(int(ps))) - 1)
            elif m in ("timescale_update", "du"):
                data = p[1]
                for row in data.get("sds_1", {}).get("s", []):
                    s.bars[row["i"]] = row["v"][:5]
                for row in data.get("st1", {}).get("st", []):
                    s.study[row["i"]] = row["v"]
                check_closed(s, bi, si, start)
            elif m in ("study_error", "symbol_error", "series_error", "critical_error", "protocol_error"):
                log("ERROR", s.ticker, json.dumps(p)[:300])
    ws.close()


def main():
    inputs, bi, si = load_script()
    log("Wolf X loaded. symbols:", SYMBOLS, "tf:", TF)
    if TEST:
        telegram("✅ WOLF X bot is running: " + ", ".join(x.split(":")[-1] for x in SYMBOLS) + " (" + TF + "m)")
    deadline = time.time() + RUN_SECONDS
    syms = [Sym(t) for t in SYMBOLS]
    while time.time() < deadline:
        try:
            run_once(inputs, bi, si, deadline, syms)
        except Exception:
            log("connection error, reconnecting in 10s\n" + traceback.format_exc())
            time.sleep(10)
            for s in syms:
                s.cs = rid("cs_")
    log("run finished")


if __name__ == "__main__":
    main()
