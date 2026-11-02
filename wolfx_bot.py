"""
WOLF X -> Telegram (runs 24/7 on GitHub Actions)

Loads the ORIGINAL "Wolf X" script on TradingView (OANDA prices) and, when a candle
closes with a Wolf X Buy/Sell arrow, posts "BUY/SELL — Check <symbol> — <tf>" to Telegram.

Env (GitHub secrets): TG_TOKEN, TG_CHAT, optional TG_ADMIN (gets health warnings)
TradingView login (needed since TradingView blocked scripts on the anonymous feed):
  TV_SESSION       = value of the "sessionid" cookie of the bot's TradingView account
  TV_SESSION_SIGN  = value of the "sessionid_sign" cookie
Optional: SYMBOLS, TF, RUN_MINUTES
"""
import json, os, random, string, time, urllib.request, urllib.parse, re, traceback, threading

import websocket  # pip install websocket-client

PINE_ID = "PUB;862506c546514212b9728a634dbc7152"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"

_tok = re.search(r"\d{6,}:[A-Za-z0-9_-]{30,}", os.environ.get("TG_TOKEN", ""))
TG_TOKEN = _tok.group(0) if _tok else ""
TG_CHAT = os.environ.get("TG_CHAT", "").strip()
TG_ADMIN = os.environ.get("TG_ADMIN", "").strip()
TV_SESSION = os.environ.get("TV_SESSION", "").strip()
TV_SESSION_SIGN = os.environ.get("TV_SESSION_SIGN", "").strip()
SYMBOLS = [s.strip() for s in os.environ.get(
    "SYMBOLS", "OANDA:XAUUSD,OANDA:EURUSD,OANDA:GBPUSD,OANDA:USDJPY").split(",") if s.strip()]
TF = os.environ.get("TF", "15").strip()
RUN_SECONDS = int(float(os.environ.get("RUN_MINUTES", "350")) * 60)
TEST = os.environ.get("SEND_TEST", "") == "1"
BIG = 1e10
NICE = {"XAUUSD": "GOLD", "NAS100USD": "NAS100", "US30USD": "US30"}


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def send(chats, text):
    if not TG_TOKEN or not chats:
        log("TELEGRAM (not configured):", text)
        return
    for chat in [c.strip() for c in chats.split(",") if c.strip()]:
        data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
        try:
            urllib.request.urlopen("https://api.telegram.org/bot%s/sendMessage" % TG_TOKEN, data, timeout=20).read()
        except Exception as e:
            log("telegram error", chat, e)


def telegram(text):
    send(TG_CHAT, text)


_warned = set()


def admin(key, text):
    log("ADMIN:", text)
    if TG_ADMIN and key not in _warned:
        _warned.add(key)
        send(TG_ADMIN, "⚠️ WOLF X bot (" + TF + "m): " + text)


def http(url, cookie=None):
    h = {"User-Agent": UA, "Origin": "https://www.tradingview.com"}
    if cookie:
        h["Cookie"] = cookie
    return urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=30).read().decode()


def get_auth():
    """Return (ws_url, origin, auth_token)."""
    if TV_SESSION:
        cookie = "sessionid=%s; sessionid_sign=%s" % (TV_SESSION, TV_SESSION_SIGN)
        page = http("https://www.tradingview.com/chart/", cookie)
        m = re.search(r'"auth_token":"([^"]+)"', page)
        if m:
            return "wss://data.tradingview.com/socket.io/websocket?type=chart", "https://www.tradingview.com", m.group(1)
        admin("login", "TradingView login failed — the TV_SESSION cookie is wrong or expired. Copy it again.")
    return ("wss://widgetdata.tradingview.com/socket.io/websocket?from=embed", "https://s.tradingview.com",
            "widget_user_token")


def load_script():
    r = json.loads(http("https://pine-facade.tradingview.com/pine-facade/translate/%s/last"
                        % urllib.parse.quote(PINE_ID, safe="")))
    res = r["result"]
    mi = res["metaInfo"]
    il = res.get("ilTemplate") or next(i["defval"] for i in mi["inputs"] if i["id"] == "text")
    inputs = {"text": il, "pineId": mi["scriptIdPart"], "pineVersion": mi["pine"]["version"]}
    for i in mi["inputs"]:
        if i["id"] in ("text", "pineId", "pineVersion"):
            continue
        inputs[i["id"]] = {"v": i["defval"], "f": True, "t": i["type"]}
    inputs["__fast_calc"] = {"v": False, "f": True, "t": "bool"}
    inputs["__profile"] = {"v": False, "f": True, "t": "bool"}
    titles = [mi["styles"].get(p["id"], {}).get("title") for p in mi["plots"]]
    return inputs, titles.index("Buy Arrow") + 1, titles.index("Sell Arrow") + 1


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
        self.bars, self.study = {}, {}
        self.last_checked_t = None

    def short(self):
        return self.ticker.split(":")[-1]


def check_closed(sym, bi, si):
    if len(sym.bars) < 22:
        return
    closed = sorted(sym.bars)[:-1]  # last bar is still forming
    last_closed_t = sym.bars[closed[-1]][0]
    if sym.last_checked_t is None:
        recent = time.time() - (last_closed_t + int(TF) * 60) < 180
        sym.last_checked_t = last_closed_t - 1 if recent else last_closed_t
    for idx in closed:
        b = sym.bars[idx]
        if b[0] <= sym.last_checked_t:
            continue
        sv = sym.study.get(idx)
        if sv is None:
            return
        buy = sv[bi] if len(sv) > bi else None
        sell = sv[si] if len(sv) > si else None
        d = 0
        if buy is not None and abs(buy) < BIG and buy != 0:
            d = 1
        elif sell is not None and abs(sell) < BIG and sell != 0:
            d = -1
        sym.last_checked_t = b[0]
        if d:
            msg = ("🟢 BUY" if d == 1 else "🔴 SELL") + " — Check " + NICE.get(sym.short(), sym.short()) + " — " + TF + "m"
            log("SIGNAL", msg)
            telegram(msg)


def watch(sym, inputs, bi, si, deadline):
    """One websocket per symbol, reconnecting until the deadline."""
    while time.time() < deadline:
        try:
            url, origin, token = get_auth()
            ws = websocket.create_connection(url, header=["Origin: " + origin, "User-Agent: " + UA], timeout=60)
            cs = rid("cs_")
            sym.bars.clear(); sym.study.clear()
            ws.send(enc("set_auth_token", [token]))
            ws.send(enc("chart_create_session", [cs, ""]))
            ws.send(enc("resolve_symbol", [cs, "sds_sym_1", '={"symbol":"%s","adjustment":"splits"}' % sym.ticker]))
            ws.send(enc("create_series", [cs, "sds_1", "s1", "sds_sym_1", TF, 300, ""]))
            time.sleep(2)
            ws.send(enc("create_study", [cs, "st1", "st1", "sds_1", "Script@tv-scripting-101!", inputs]))
            started, got = time.time(), False
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
                    if m in ("timescale_update", "du"):
                        data = p[1]
                        for row in data.get("sds_1", {}).get("s", []):
                            sym.bars[row["i"]] = row["v"][:5]
                        rows = data.get("st1", {}).get("st", [])
                        for row in rows:
                            sym.study[row["i"]] = row["v"]
                        if rows and not got:
                            got = True
                            log("study data OK", sym.ticker)
                        check_closed(sym, bi, si)
                    elif m in ("study_error", "symbol_error", "series_error", "critical_error", "protocol_error"):
                        log("ERROR", sym.ticker, m, json.dumps(p)[:300])
                        admin(m + sym.ticker, sym.short() + ": " + json.dumps(p)[:200])
                        raise RuntimeError(m)
                if not got and time.time() - started > 120:
                    admin("nodata", "TradingView is not returning Wolf X data (" + sym.short() + ").")
                    raise RuntimeError("no study data")
            ws.close()
        except Exception as e:
            log("reconnect", sym.ticker, repr(e)[:200])
            time.sleep(30)


def main():
    inputs, bi, si = load_script()
    log("Wolf X loaded. symbols:", SYMBOLS, "tf:", TF, "login:", "yes" if TV_SESSION else "no")
    if TEST:
        send(TG_ADMIN or TG_CHAT, "✅ WOLF X bot is running (" + TF + "m)")
    deadline = time.time() + RUN_SECONDS
    threads = []
    for k, t in enumerate(SYMBOLS):
        th = threading.Thread(target=watch, args=(Sym(t), inputs, bi, si, deadline), daemon=True)
        th.start()
        threads.append(th)
        time.sleep(3)
    for th in threads:
        th.join()
    log("run finished")


if __name__ == "__main__":
    main()
