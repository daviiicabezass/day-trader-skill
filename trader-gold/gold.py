#!/usr/bin/env python3
"""trader-gold: day trading de oro (XAUUSD) para MetaTrader 5. Solo biblioteca estandar.

    python3 gold.py clock                                   # mercado del oro y sesiones de Londres / Nueva York
    python3 gold.py orb --session london                    # ruptura del rango de apertura de Londres (08:00)
    python3 gold.py orb --session ny --write                # Nueva York (08:20, COMEX) -> trader/gold-entry.json
    python3 gold.py orb --session london --csv XAUUSDM5.csv # con las velas exportadas de tu MT5
    python3 gold.py plan --stop or-mid --target 2R --write  # -> trader/gold-plan.json
    python3 gold.py replay --write                          # 10.000 repeticiones + historial -> trader/gold-replay.json
    python3 gold.py size --write                            # lotes -> trader/gold-order.json
    python3 gold.py ticket                                  # la orden explicada para colocarla a mano en MT5

Nada de esto coloca una orden. El ticket se coloca a mano en MetaTrader 5, si quieres.

Datos:
  - CSV exportado de MT5 (recomendado: son los precios de TU broker). En MT5: Ver > Simbolos >
    Barras, XAUUSD, M5 o M1, Exportar. La hora del CSV es la del servidor del broker: --server-tz
    "ny+7" (por defecto, la convencion GMT+2 / GMT+3 que cierra la semana a las 17:00 de Nueva
    York), o un desfase fijo como "+2", "0" o "-5".
  - Yahoo Finance GC=F (futuros del oro en COMEX, gratuito y no oficial). El futuro cotiza unos
    dolares por encima del spot: --basis -25 resta 25 a todos los precios para acercarlo al XAUUSD
    de tu broker. Las distancias (riesgo, objetivo) valen igual; los niveles exactos, mejor del CSV.
Las velas de 1 minuto se agrupan en velas de 5 minutos.

Sesiones (configurables en trader/gold.json, "sessions"):
  london  apertura 08:00 hora de Londres, entradas hasta 11:00, fuera a las 16:00
  ny      apertura 08:20 hora de Nueva York (COMEX), entradas hasta 11:30, fuera a las 13:25
El horario de verano de Londres (ultimo domingo de marzo a ultimo domingo de octubre, 01:00 UTC)
y de Nueva York (segundo domingo de marzo a primer domingo de noviembre) se calcula aqui.
"""

import argparse
import datetime
import gzip
import json
import math
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "day-trader-skill/2.0 (trader-gold)"
FIVE = datetime.timedelta(minutes=5)
TRADER = "trader"

DEFAULTS = {
    "symbol": "XAUUSD",          # el nombre en tu MT5 (XAUUSD, GOLD, XAUUSD.r...)
    "contract_size": 100,        # onzas por lote (casi siempre 100; compruebalo en la especificacion del simbolo)
    "lot_min": 0.01,
    "lot_step": 0.01,
    "lot_max": 5.0,
    "balance": 5000.0,
    "max_loss_usd": 50.0,
    "leverage": 100,
    "spread": 0.30,              # dolares por onza
    "cash_buffer_pct": 10,
    "min_reward_risk": 1.0,
    "or_minutes": 15,
    "server_tz": "ny+7",
    "yahoo_symbol": "GC=F",
    "basis": 0.0,
    "sessions": {},
}

SESSIONS = {
    "london": {"name": "Londres", "tz": "london", "open": "08:00", "no_entries_after": "11:00", "flat_by": "16:00"},
    "ny": {"name": "Nueva York", "tz": "ny", "open": "08:20", "no_entries_after": "11:30", "flat_by": "13:25"},
}
TZ_NAMES = {"ny": "Nueva York", "london": "Londres", "utc": "UTC"}


class GoldError(Exception):
    pass


# ---------------------------------------------------------------- tiempo

def _nth_sunday(year, month, n):
    d = datetime.date(year, month, 1)
    d += datetime.timedelta(days=(6 - d.weekday()) % 7)
    return d + datetime.timedelta(weeks=n - 1)


def _last_sunday(year, month):
    nxt = datetime.date(year + (month == 12), month % 12 + 1, 1)
    d = nxt - datetime.timedelta(days=1)
    return d - datetime.timedelta(days=(d.weekday() - 6) % 7)


def ny_offset(utc):
    y = utc.year
    start = datetime.datetime.combine(_nth_sunday(y, 3, 2), datetime.time(7, 0))
    end = datetime.datetime.combine(_nth_sunday(y, 11, 1), datetime.time(6, 0))
    return -4 if start <= utc < end else -5


def london_offset(utc):
    y = utc.year
    start = datetime.datetime.combine(_last_sunday(y, 3), datetime.time(1, 0))
    end = datetime.datetime.combine(_last_sunday(y, 10), datetime.time(1, 0))
    return 1 if start <= utc < end else 0


OFFSETS = {"ny": ny_offset, "london": london_offset, "utc": lambda utc: 0}
CANDIDATES = {"ny": (-4, -5), "london": (1, 0), "utc": (0,)}


def to_local(utc, tz):
    return utc + datetime.timedelta(hours=OFFSETS[tz](utc))


def local_to_utc(local, tz):
    for off in CANDIDATES[tz]:
        utc = local - datetime.timedelta(hours=off)
        if OFFSETS[tz](utc) == off:
            return utc
    return local - datetime.timedelta(hours=CANDIDATES[tz][-1])


def parse_hm(text):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(text))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise GoldError("hora no valida: %r (usa HH:MM)" % (text,))
    return datetime.time(int(m.group(1)), int(m.group(2)))


def parse_now(text):
    """'2026-10-19 10:15' (Nueva York), o con zona al final: 'london', 'ny', 'utc'."""
    if not text:
        return datetime.datetime.utcnow().replace(microsecond=0)
    parts = text.split()
    zone = "ny"
    if parts and parts[-1].lower() in ("ny", "london", "londres", "utc"):
        zone = {"londres": "london"}.get(parts[-1].lower(), parts[-1].lower())
        parts = parts[:-1]
    try:
        local = datetime.datetime.strptime(" ".join(parts), "%Y-%m-%d %H:%M")
    except ValueError:
        raise GoldError("--now no valido: %r (usa \"AAAA-MM-DD HH:MM\", opcionalmente con london, ny o utc)" % text)
    return local_to_utc(local, zone)


def hm(dt):
    return dt.strftime("%H:%M")


def market_open(utc):
    """El oro al contado / CFD: domingo 18:00 a viernes 17:00 de Nueva York, pausa diaria de 17:00 a 18:00."""
    ny = to_local(utc, "ny")
    wd, m = ny.weekday(), ny.hour * 60 + ny.minute
    if wd == 5:
        return False
    if wd == 6:
        return m >= 18 * 60
    if wd == 4 and m >= 17 * 60:
        return False
    return not (17 * 60 <= m < 18 * 60)


# ---------------------------------------------------------------- configuracion

def load_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None
    except ValueError as e:
        raise GoldError("%s no es JSON valido: %s" % (path, e))


def load_config(path=None):
    cfg = dict(DEFAULTS)
    raw = load_json(path or os.path.join(TRADER, "gold.json"))
    if raw:
        cfg.update(raw)
    return cfg


def session_cfg(name, cfg):
    if name not in SESSIONS:
        raise GoldError("sesion desconocida: %r (usa london o ny)" % name)
    s = dict(SESSIONS[name])
    s.update((cfg.get("sessions") or {}).get(name, {}))
    s["key"] = name
    for k in ("open", "no_entries_after", "flat_by"):
        parse_hm(s[k])
    return s


def bounds(day, sess):
    tz = sess["tz"]
    f = lambda k: local_to_utc(datetime.datetime.combine(day, parse_hm(sess[k])), tz)
    return f("open"), f("no_entries_after"), f("flat_by")


def n_slots(sess):
    o = parse_hm(sess["open"])
    f = parse_hm(sess["flat_by"])
    mins = (f.hour * 60 + f.minute) - (o.hour * 60 + o.minute)
    if mins <= 0 or mins % 5:
        raise GoldError("la sesion %s debe durar un multiplo de 5 minutos" % sess["key"])
    return mins // 5


# ---------------------------------------------------------------- datos

def parse_server_tz(text):
    t = str(text).strip().lower()
    m = re.match(r"^ny([+-]\d+(?:\.\d+)?)$", t)
    if m:
        return ("ny", float(m.group(1)))
    if t in ("utc", "gmt"):
        return ("fixed", 0.0)
    try:
        return ("fixed", float(t))
    except ValueError:
        raise GoldError("--server-tz no valido: %r (usa ny+7, +2, 0, -5...)" % text)


def server_to_utc(dt, tzspec):
    kind, off = tzspec
    if kind == "ny":
        return local_to_utc(dt - datetime.timedelta(hours=off), "ny")
    return dt - datetime.timedelta(hours=off)


_DATE = re.compile(r"^(\d{4})[.\-/](\d{2})[.\-/](\d{2})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?$")
_TIME = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")


def parse_mt5_csv(text, server_tz="ny+7"):
    """Velas exportadas de MT5: <DATE> <TIME> <OPEN> <HIGH> <LOW> <CLOSE> ... separadas por tab, coma o ;."""
    tzspec = parse_server_tz(server_tz)
    bars = []
    for line in text.splitlines():
        line = line.strip().lstrip("﻿")
        if not line or line.startswith("<") or line[0].isalpha():
            continue
        f = [x.strip() for x in re.split(r"[\t,;]", line) if x.strip() != ""]
        m = _DATE.match(f[0]) if f else None
        if not m:
            continue
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if m.group(4) is not None:
            hh, mm, rest = int(m.group(4)), int(m.group(5)), f[1:]
        else:
            t = _TIME.match(f[1]) if len(f) > 1 else None
            if not t:
                continue
            hh, mm, rest = int(t.group(1)), int(t.group(2)), f[2:]
        try:
            o, h, l, c = (float(x) for x in rest[:4])
        except ValueError:
            continue
        server = datetime.datetime(y, mo, d, hh, mm)
        bars.append((server_to_utc(server, tzspec), o, h, l, c))
    if not bars:
        raise GoldError("no encontre velas en el CSV (espero fecha, hora, apertura, maximo, minimo, cierre)")
    return to_five(bars)


def parse_yahoo(text, basis=0.0):
    try:
        doc = json.loads(text)
    except ValueError:
        raise GoldError("la respuesta de Yahoo no es JSON")
    chart = doc.get("chart") or {}
    res = chart.get("result")
    if not res:
        err = chart.get("error") or {}
        raise GoldError("Yahoo no devolvio velas: %s" % (err.get("description") or "sin datos"))
    r = res[0]
    ts = r.get("timestamp") or []
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0]
    bars = []
    for i, t in enumerate(ts):
        try:
            o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        except (KeyError, IndexError):
            continue
        if None in (o, h, l, c):
            continue
        utc = datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=int(t))
        bars.append((utc, o + basis, h + basis, l + basis, c + basis))
    if not bars:
        raise GoldError("Yahoo devolvio un grafico sin velas")
    return to_five(bars)


def to_five(bars):
    """Agrupa velas (de 1 o 5 minutos) en velas de 5 minutos. Devuelve {inicio_utc: (t, o, h, l, c, fin_datos)}."""
    out = {}
    for t, o, h, l, c in sorted(bars, key=lambda b: b[0]):
        if t.second or t.microsecond:
            continue  # el tick en vivo de Yahoo no es una vela
        start = t - datetime.timedelta(minutes=t.minute % 5)
        if start in out:
            _, o0, h0, l0, _, _ = out[start]
            out[start] = (start, o0, max(h0, h), min(l0, l), c, t)
        else:
            out[start] = (start, o, h, l, c, t)
    return out


def yahoo_url(sym, host="query2"):
    return "https://%s.finance.yahoo.com/v8/finance/chart/%s?range=60d&interval=5m" % (host, urllib.parse.quote(sym))


def fetch_yahoo(sym):
    last = None
    for host in ("query2", "query1"):
        for attempt in (0, 1):
            req = urllib.request.Request(yahoo_url(sym, host), headers={"User-Agent": UA, "Accept": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    return r.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                last = e.code
                if e.code == 429 and attempt == 0:
                    time.sleep(5)
                    continue
                break
            except (urllib.error.URLError, OSError) as e:
                raise GoldError("no pude conectar con Yahoo: %s" % getattr(e, "reason", e))
        time.sleep(0.5)
    raise GoldError("Yahoo respondio HTTP %s. Prueba mas tarde o usa --csv con las velas de tu MT5." % last)


def read_text(path):
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def load_bars(a, cfg):
    """Devuelve (velas, descripcion de la fuente)."""
    basis = cfg["basis"] if a.basis is None else a.basis
    if a.csv:
        tz = a.server_tz or cfg["server_tz"]
        return parse_mt5_csv(read_text(a.csv), tz), "MT5 CSV %s (hora del servidor %s)" % (os.path.basename(a.csv), tz)
    sym = cfg["yahoo_symbol"]
    text = read_text(a.offline) if a.offline else fetch_yahoo(sym)
    note = "Yahoo %s, futuros (gratuito, no oficial, puede ir con retraso)" % sym
    if basis:
        note += ", base %+.2f" % basis
    return parse_yahoo(text, basis), note


# ---------------------------------------------------------------- redondeo

def floor_c(x):
    return math.floor(x * 100 + 1e-6) / 100.0


def ceil_c(x):
    return math.ceil(x * 100 - 1e-6) / 100.0


def round_c(x):
    return math.floor(x * 100 + 0.5 + 1e-9) / 100.0


def floor_step(x, step):
    n = math.floor(x / step + 1e-9)
    return round(max(n, 0) * step, 2)


# ---------------------------------------------------------------- ruptura del rango de apertura

def opening_range(idx, open_utc, k):
    rng = [idx.get(open_utc + i * FIVE) for i in range(k)]
    if any(b is None for b in rng):
        return None
    return max(b[2] for b in rng), min(b[3] for b in rng)


def orb(idx, sess, day, now_utc, or_minutes=15, side="both"):
    if or_minutes % 5 or or_minutes <= 0:
        raise GoldError("--or-minutes debe ser multiplo de 5")
    k = or_minutes // 5
    open_utc, cutoff_utc, flat_utc = bounds(day, sess)
    doc = {"session": sess["key"], "session_name": sess["name"], "date": day.isoformat(), "side": side,
           "or_minutes": or_minutes, "tz": sess["tz"]}
    if day.weekday() >= 5:
        doc["status"] = "closed"
        return doc
    if now_utc < open_utc:
        doc.update(status="not_open", opens=hm(to_local(open_utc, sess["tz"])))
        return doc
    if now_utc < open_utc + k * FIVE:
        doc["status"] = "forming"
        return doc
    rg = opening_range(idx, open_utc, k)
    if rg is None:
        doc["status"] = "no_data"
        return doc
    hi, lo = rg
    doc["opening_range"] = {"high": round(hi, 2), "low": round(lo, 2), "minutes": or_minutes}
    i, last = k, None
    while True:
        t = open_utc + i * FIVE
        end = t + FIVE
        if end > now_utc or end > cutoff_utc:
            break
        b = idx.get(t)
        i += 1
        if b is None:
            continue
        last = b
        hit = None
        if side in ("long", "both") and b[4] > hi:
            hit = "long"
        elif side in ("short", "both") and b[4] < lo:
            hit = "short"
        if hit:
            doc.update(status="triggered", side=hit, entry=round(b[4], 2), entry_bar=i - 1,
                       entry_time=hm(to_local(end, sess["tz"])), entry_utc=end.isoformat() + "Z")
            return doc
    if last is not None:
        doc["last_close"] = round(last[4], 2)
        doc["last_bar_time"] = hm(to_local(last[0], sess["tz"]))
    doc["status"] = "expired" if now_utc >= cutoff_utc else "waiting"
    doc["cutoff"] = hm(to_local(cutoff_utc, sess["tz"]))
    return doc


def orb_report(doc, source):
    sess, tzn = doc["session_name"], TZ_NAMES[doc["tz"]]
    head = "XAUUSD  sesion de %s, %s  rango de apertura de %d minutos, velas de 5 minutos (%s)" % (
        sess, doc["date"], doc["or_minutes"], source)
    st = doc["status"]
    if st == "closed":
        return head + "\nFin de semana: no hay sesion de %s." % sess
    if st == "not_open":
        return head + "\nLa sesion de %s todavia no ha abierto (abre a las %s hora de %s)." % (sess, doc["opens"], tzn)
    if st == "forming":
        return head + "\nEl rango de apertura todavia se esta formando."
    if st == "no_data":
        return head + "\nFaltan velas del rango de apertura en los datos: no puedo calcularlo."
    r = doc["opening_range"]
    lines = [head, "Rango de apertura: maximo %.2f, minimo %.2f (%.2f de ancho)." % (r["high"], r["low"], r["high"] - r["low"])]
    if st == "triggered":
        lado = "LARGO (compra)" if doc["side"] == "long" else "CORTO (venta)"
        rel = "por encima del maximo" if doc["side"] == "long" else "por debajo del minimo"
        lines.append("ACTIVADO %s a las %s (%s) en %.2f: la vela %d cerro %s del rango." % (
            lado, doc["entry_time"], tzn, doc["entry"], doc["entry_bar"], rel))
        lines.append("No se ha colocado nada. Siguiente: el stop y el objetivo ANTES de entrar: gold.py plan.")
    elif st == "waiting":
        if "last_close" in doc:
            lc = doc["last_close"]
            lines.append("ESPERANDO  ultimo cierre %.2f (vela de las %s): %.2f bajo el maximo, %.2f sobre el minimo." % (
                lc, doc["last_bar_time"], r["high"] - lc, lc - r["low"]))
        else:
            lines.append("ESPERANDO  todavia no ha cerrado ninguna vela despues del rango.")
        lines.append("Entradas hasta las %s (%s)." % (doc["cutoff"], tzn))
    else:
        lines.append("CADUCADO  ninguna vela cerro fuera del rango antes de las %s (%s). Hoy no hay entrada." % (doc["cutoff"], tzn))
    return "\n".join(lines)


# ---------------------------------------------------------------- plan

def stop_from_rule(rule, side, entry, hi, lo):
    long = side == "long"
    if rule == "or-mid":
        raw = (hi + lo) / 2.0
    elif rule == "or-opposite":
        raw = lo if long else hi
    elif rule.startswith("price:"):
        raw = float(rule[6:])
    elif rule.startswith("usd:"):
        d = float(rule[4:])
        raw = entry - d if long else entry + d
    else:
        raise GoldError("--stop no valido: %r (or-mid, or-opposite, price:X, usd:X)" % rule)
    return floor_c(raw) if long else ceil_c(raw)


STOP_LABELS = {"or-mid": "punto medio del rango de apertura", "or-opposite": "el otro extremo del rango",
               "price": "tu precio", "usd": "dolares desde la entrada"}


def make_plan(entry, stop_rule="or-mid", target="2R", cfg=None):
    cfg = cfg or DEFAULTS
    if entry.get("status") != "triggered":
        raise GoldError("la entrada no se ha activado; usa --entry y --side para planificar por adelantado")
    side, e = entry["side"], float(entry["entry"])
    r = entry.get("opening_range") or {}
    if stop_rule.startswith("or-") and not r:
        raise GoldError("un stop %s necesita el rango de apertura (--or-high y --or-low)" % stop_rule)
    stop = stop_from_rule(stop_rule, side, e, r.get("high"), r.get("low"))
    sign = 1 if side == "long" else -1
    risk = round((e - stop) * sign, 2)
    if risk <= 0:
        raise GoldError("el stop %.2f esta en el lado equivocado de la entrada %.2f" % (stop, e))
    spread = float(cfg["spread"])
    if risk < 3 * spread:
        raise GoldError("stop demasiado ajustado: %.2f de riesgo con un spread de %.2f; el ruido normal te sacaria. "
                        "Usa un stop mas amplio (or-opposite o usd:X)." % (risk, spread))
    t = str(target).upper()
    if t.endswith("R"):
        rr = float(t[:-1])
        tgt = round_c(e + sign * rr * risk)
    elif t.startswith("PRICE:"):
        tgt = round(float(t[6:]), 2)
        rr = round((tgt - e) * sign / risk, 2)
    else:
        raise GoldError("--target no valido: %r (2R, 1.5R, price:X)" % target)
    if rr <= 0:
        raise GoldError("el objetivo %.2f esta en el lado equivocado de la entrada %.2f" % (tgt, e))
    plan = dict(entry)
    plan.update(created_by="trader-gold", symbol=cfg["symbol"], entry=e, stop=stop,
                stop_rule=stop_rule.split(":")[0], stop_arg=stop_rule, stop_label=STOP_LABELS[stop_rule.split(":")[0]],
                target=tgt, target_r=rr, risk_per_oz=risk, reward_risk=rr)
    return plan


def plan_report(p):
    tzn = TZ_NAMES[p["tz"]]
    lado = "LARGO" if p["side"] == "long" else "CORTO"
    return "\n".join([
        "Plan de salida: %s %s, %s, sesion de %s" % (p["symbol"], lado, p["date"], p["session_name"]),
        "  Entrada      %.2f a las %s (%s)" % (p["entry"], p.get("entry_time", "?"), tzn),
        "  Stop         %.2f  %s" % (p["stop"], p["stop_label"]),
        "  Objetivo     %.2f  %sR" % (p["target"], _num(p["target_r"])),
        "  Riesgo/onza  %.2f" % p["risk_per_oz"],
        "  Fuera a las  %s (%s), se gane o se pierda" % (p.get("flat_by", "?"), tzn),
        "Un stop es un plan, no una garantia: con un gap o una noticia fuerte puede ejecutarse peor.",
    ])


def _num(x):
    return ("%.2f" % x).rstrip("0").rstrip(".")


# ---------------------------------------------------------------- maquina del tiempo

def past_sessions(idx, sess, before_day, n):
    """Sesiones completas (todas las velas de 5 minutos) anteriores a before_day."""
    if not idx:
        return []
    first = to_local(min(idx), sess["tz"]).date()
    out = []
    d = first
    while d < before_day:
        if d.weekday() < 5:
            o, _, _ = bounds(d, sess)
            bars = [idx.get(o + i * FIVE) for i in range(n)]
            if all(b is not None for b in bars):
                out.append((d, bars))
        d += datetime.timedelta(days=1)
    return out


def build_pool(sessions, n):
    pool = [[] for _ in range(n)]
    for _, bars in sessions:
        for j in range(1, n):
            pc = bars[j - 1][4]
            b = bars[j]
            pool[j].append((b[1] / pc, b[2] / pc, b[3] / pc, b[4] / pc))
    return pool


def _pct(a, q):
    s = sorted(a)
    return s[min(int(q * len(s)), len(s) - 1)]


def simulate(pool, entry, stop, target, start, n, side, rr, replays=10000, seed=19, window=2):
    rng = random.Random(seed)
    long = side == "long"
    risk = abs(entry - stop)
    counts = {"target": 0, "stop": 0, "neither": 0}
    green, rs, finals = 0, [], []
    for _ in range(replays):
        p, out = entry, None
        for i in range(start, n):
            j = min(max(i + rng.randint(-window, window), 1), n - 1)
            o, h, l, c = rng.choice(pool[j])
            hi, lo = p * h, p * l
            if out is None:
                if long:
                    if lo <= stop:
                        out = "stop"
                    elif hi >= target:
                        out = "target"
                else:
                    if hi >= stop:
                        out = "stop"
                    elif lo <= target:
                        out = "target"
            p *= c
        finals.append(p)
        if out is None:
            out = "neither"
            r = (p - entry) / risk * (1 if long else -1)
        else:
            r = rr if out == "target" else -1.0
        counts[out] += 1
        rs.append(r)
        if out == "target" or (out == "neither" and r > 0):
            green += 1
    pc = lambda k: 100.0 * counts[k] / replays
    return {"replays": replays, "seed": seed, "target": pc("target"), "stop": pc("stop"),
            "neither": pc("neither"), "green": 100.0 * green / replays, "avg_r": sum(rs) / replays,
            "median": _pct(finals, 0.5), "p5": _pct(finals, 0.05), "p95": _pct(finals, 0.95)}


def history(sessions, plan, k, n, cutoff_slot):
    side, long = plan["side"], plan["side"] == "long"
    rr = plan["target_r"]
    res = {"sessions": len(sessions), "fired": 0, "target": 0, "stop": 0, "neither": 0, "r": []}
    for _, bars in sessions:
        hi = max(b[2] for b in bars[:k])
        lo = min(b[3] for b in bars[:k])
        ent = None
        for i in range(k, min(cutoff_slot, n)):
            c = bars[i][4]
            if (long and c > hi) or (not long and c < lo):
                ent = i
                break
        if ent is None:
            continue
        e = bars[ent][4]
        rule = plan.get("stop_arg", "or-mid")
        if rule.startswith("price:"):
            rule = "usd:%s" % plan["risk_per_oz"]
        stop = stop_from_rule(rule, side, e, hi, lo)
        risk = abs(e - stop)
        if risk <= 0:
            continue
        tgt = e + (rr * risk if long else -rr * risk)
        res["fired"] += 1
        out = None
        for b in bars[ent + 1:]:
            if long:
                if b[3] <= stop:
                    out = "stop"
                elif b[2] >= tgt:
                    out = "target"
            else:
                if b[2] >= stop:
                    out = "stop"
                elif b[3] <= tgt:
                    out = "target"
            if out:
                break
        if out is None:
            out = "neither"
            r = (bars[-1][4] - e) / risk * (1 if long else -1)
        else:
            r = rr if out == "target" else -1.0
        res[out] += 1
        res["r"].append(r)
    rl = res.pop("r")
    res["avg_r"] = sum(rl) / len(rl) if rl else None
    return res


def verdict(mc, rr):
    hit = mc["target"] + mc["stop"]
    if hit <= 0:
        return "Ninguna repeticion toco el stop ni el objetivo: el cierre lo decide todo."
    share = 100.0 * mc["target"] / hit
    be = 100.0 / (1 + rr)
    gap = share - be
    a = "objetivo primero %.1f %% frente a stop primero %.1f %%" % (mc["target"], mc["stop"])
    if abs(gap) < 5:
        return ("Casi como lanzar una moneda: " if abs(rr - 1) < 1e-9 else "Cerca del punto de equilibrio: ") + a + "."
    side = "el objetivo" if gap > 0 else "el stop"
    return ("Se inclina hacia %s: " if abs(gap) < 15 else "Claramente hacia %s: ") % side + a + "."


def run_replay(idx, plan, cfg, replays=10000, seed=19, window=2):
    sess = session_cfg(plan["session"], cfg)
    n = n_slots(sess)
    k = int(plan.get("or_minutes", 15)) // 5
    day = datetime.date.fromisoformat(plan["date"])
    sessions = past_sessions(idx, sess, day, n)
    if len(sessions) < 20:
        raise GoldError("solo hay %d sesiones completas de %s antes del %s; necesito al menos 20 para un conjunto justo"
                        % (len(sessions), sess["name"], plan["date"]))
    start = int(plan["entry_bar"]) + 1
    if start >= n:
        raise GoldError("la entrada es en la ultima vela de la sesion: no queda nada que simular")
    pool = build_pool(sessions, n)
    mc = simulate(pool, plan["entry"], plan["stop"], plan["target"], start, n, plan["side"], plan["target_r"],
                  replays, seed, window)
    o, cut, _ = bounds(day, sess)
    cutoff_slot = int((cut - o).total_seconds() // 300)
    hist = history(sessions, plan, k, n, cutoff_slot)
    return {"plan": plan, "mc": mc, "history": hist, "verdict": verdict(mc, plan["target_r"]),
            "bars_left": n - start, "flat_by": sess["flat_by"]}


def replay_report(doc):
    mc, h, p = doc["mc"], doc["history"], doc["plan"]
    tzn = TZ_NAMES[p["tz"]]
    lines = [
        "%s repeticiones del resto de la sesion de %s, %s a %s (%s, %d velas de 5 minutos), semilla %d." % (
            "{:,}".format(mc["replays"]).replace(",", "."), p["session_name"], p.get("entry_time", "?"),
            doc["flat_by"], tzn, doc["bars_left"], mc["seed"]),
        "  objetivo primero  %5.1f %%" % mc["target"],
        "  stop primero      %5.1f %%" % mc["stop"],
        "  ninguno           %5.1f %%   todavia abierta a las %s" % (mc["neither"], doc["flat_by"]),
        "  en verde          %5.1f %%   objetivo primero, o a favor a las %s" % (mc["green"], doc["flat_by"]),
        "  Precio a las %s: mediana %.2f, 90 %% de las repeticiones entre %.2f y %.2f." % (
            doc["flat_by"], mc["median"], mc["p5"], mc["p95"]),
        "  R media: %+.3f" % mc["avg_r"],
    ]
    if h["fired"]:
        lines.append("Historial: la misma regla en %d sesiones reales se activo %d veces: %d objetivo, %d stop, %d ninguno. "
                     "R media %+.3f." % (h["sessions"], h["fired"], h["target"], h["stop"], h["neither"], h["avg_r"]))
    else:
        lines.append("Historial: la misma regla no se activo en ninguna de las %d sesiones reales." % h["sessions"])
    lines.append(doc["verdict"])
    if mc["neither"] > 40:
        lines.append("El %.1f %% de las repeticiones no toco ninguno: el precio a las %s decide esas." % (mc["neither"], doc["flat_by"]))
    lines.append("Es una simulacion con las sesiones recientes, no una prediccion. El spread no esta incluido. "
                 "Datos de empleo (NFP), IPC, la Fed o un titular pueden hacer lo que esas sesiones nunca hicieron.")
    return "\n".join(lines)


# ---------------------------------------------------------------- tamano en lotes

def size_order(plan, cfg):
    e, stop, tgt = float(plan["entry"]), float(plan["stop"]), float(plan["target"])
    contract, spread = float(cfg["contract_size"]), float(cfg["spread"])
    step, lmin, lmax = float(cfg["lot_step"]), float(cfg["lot_min"]), float(cfg["lot_max"])
    if plan.get("reward_risk", 0) + 1e-9 < float(cfg["min_reward_risk"]):
        raise GoldError("recompensa:riesgo %.2f por debajo de tu minimo %.2f" % (plan["reward_risk"], cfg["min_reward_risk"]))
    risk_oz = abs(e - stop) + spread
    risk_lot = risk_oz * contract
    equity = float(cfg["balance"]) * (1 - float(cfg["cash_buffer_pct"]) / 100.0)
    margin_lot = contract * e / float(cfg["leverage"])
    limits = [
        ("perdida maxima", floor_step(float(cfg["max_loss_usd"]) / risk_lot, step),
         "%.2f $ / (%.2f de riesgo + %.2f de spread) x %g onzas" % (float(cfg["max_loss_usd"]), abs(e - stop), spread, contract)),
        ("margen", floor_step(equity / margin_lot, step),
         "%.2f $ disponibles / %.2f $ de margen por lote (apalancamiento 1:%g)" % (equity, margin_lot, float(cfg["leverage"]))),
        ("lote maximo", lmax, "lot_max en la configuracion"),
    ]
    lots = min(x[1] for x in limits)
    decided = [x[0] for x in limits if x[1] == lots][0]
    if lots < lmin - 1e-9:
        raise GoldError("ni el lote minimo cabe: %.2f lotes arriesgan %.2f $ en el stop (con spread), mas que tu "
                        "perdida maxima de %.2f $. Usa un stop mas cercano o salta la operacion."
                        % (lmin, lmin * risk_lot, float(cfg["max_loss_usd"])))
    max_loss = round(lots * risk_lot, 2)
    reward = round(lots * (abs(tgt - e) - spread) * contract, 2)
    order = {"symbol": cfg["symbol"], "type": "buy" if plan["side"] == "long" else "sell", "volume": lots,
             "sl": stop, "tp": tgt, "entry_ref": e, "max_loss_usd": max_loss, "reward_usd": reward,
             "margin_usd": round(lots * margin_lot, 2), "decided_by": decided,
             "limits": [{"limit": n, "lots": l, "math": m} for n, l, m in limits],
             "flat_by": plan.get("flat_by"), "tz": plan.get("tz"), "date": plan.get("date"),
             "session": plan.get("session")}
    return order


def size_report(o):
    lines = ["Tamano: %s" % o["symbol"]]
    for x in o["limits"]:
        mark = "  <- lo decide" if x["limit"] == o["decided_by"] else ""
        lines.append("  %-15s %6.2f lotes   %s%s" % (x["limit"], x["lots"], x["math"], mark))
    lines.append("%.2f lotes. Perdida maxima en el stop, con spread: %.2f $. En el objetivo: +%.2f $. Margen: %.2f $." % (
        o["volume"], o["max_loss_usd"], o["reward_usd"], o["margin_usd"]))
    return "\n".join(lines)


def ticket_report(o):
    tipo = "Buy (compra)" if o["type"] == "buy" else "Sell (venta)"
    tzn = TZ_NAMES.get(o.get("tz") or "ny", "")
    return "\n".join([
        "ORDEN para colocar A MANO en MetaTrader 5 (nada se ha enviado):",
        "  Simbolo      %s" % o["symbol"],
        "  Tipo         Ejecucion por mercado, %s" % tipo,
        "  Volumen      %.2f lotes" % o["volume"],
        "  Stop Loss    %.2f" % o["sl"],
        "  Take Profit  %.2f" % o["tp"],
        "  Perdida maxima si el stop se ejecuta en su precio: unos %.2f $ (puede ser peor en un mercado rapido)." % o["max_loss_usd"],
        "  Cierra lo que siga abierto a las %s (%s)." % (o.get("flat_by") or "?", tzn),
        "En MT5: F9 (Nueva orden) > Simbolo > Volumen > Stop Loss > Take Profit > %s." % (
            "Comprar por mercado" if o["type"] == "buy" else "Vender por mercado"),
        "Comprueba antes que el precio en MT5 sigue cerca de %.2f: si se ha ido lejos, es otra operacion." % o["entry_ref"],
        "Usa primero una cuenta demo.",
    ])


# ---------------------------------------------------------------- comandos

def _write(path, doc):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False, default=str)
        fh.write("\n")
    print("Escrito %s." % path)


def _need(path, what):
    doc = load_json(path)
    if doc is None:
        raise GoldError("no existe %s: %s" % (path, what))
    return doc


def cmd_clock(a, cfg):
    now = parse_now(a.now)
    print("Ahora: %s UTC, %s Londres, %s Nueva York." % (
        now.strftime("%Y-%m-%d %H:%M"), to_local(now, "london").strftime("%H:%M"), to_local(now, "ny").strftime("%H:%M")))
    print("Oro (XAUUSD): %s." % ("ABIERTO" if market_open(now) else "CERRADO (fin de semana o pausa diaria de 17:00 a 18:00 Nueva York)"))
    for key in ("london", "ny"):
        s = session_cfg(key, cfg)
        day = to_local(now, s["tz"]).date()
        o, cut, flat = bounds(day, s)
        if day.weekday() >= 5:
            st = "sin sesion hoy (fin de semana)"
        elif now < o:
            st = "abre a las %s" % s["open"]
        elif now < cut:
            st = "abierta: entradas hasta las %s" % s["no_entries_after"]
        elif now < flat:
            st = "sin entradas nuevas; fuera a las %s" % s["flat_by"]
        else:
            st = "terminada por hoy"
        print("  %-11s %s-%s hora de %s: %s" % (s["name"], s["open"], s["flat_by"], TZ_NAMES[s["tz"]], st))
    print("Festivos: el oro suele cotizar con horario reducido en festivos de EE. UU. y del Reino Unido; mira el calendario de tu broker.")
    return 0


def cmd_orb(a, cfg):
    sess = session_cfg(a.session, cfg)
    now = parse_now(a.now)
    idx, source = load_bars(a, cfg)
    day = to_local(now, sess["tz"]).date()
    doc = orb(idx, sess, day, now, a.or_minutes or int(cfg["or_minutes"]), a.side)
    doc.update(flat_by=sess["flat_by"], source=source, symbol=cfg["symbol"])
    print(json.dumps(doc, indent=2, ensure_ascii=False) if a.json else orb_report(doc, source))
    if a.write and doc["status"] in ("triggered", "waiting"):
        _write(os.path.join(TRADER, "gold-entry.json"), doc)
    return 0 if doc["status"] == "triggered" else 1


def cmd_plan(a, cfg):
    if a.entry is not None:
        if a.side not in ("long", "short"):
            raise GoldError("con --entry di el lado: --side long o --side short")
        sess = session_cfg(a.session, cfg)
        entry = {"status": "triggered", "side": a.side, "entry": a.entry, "session": a.session,
                 "session_name": sess["name"], "tz": sess["tz"], "flat_by": sess["flat_by"],
                 "date": a.date or to_local(parse_now(a.now), sess["tz"]).date().isoformat(),
                 "or_minutes": a.or_minutes or int(cfg["or_minutes"]), "entry_bar": a.entry_bar,
                 "entry_time": a.entry_time}
        if a.or_high is not None and a.or_low is not None:
            entry["opening_range"] = {"high": a.or_high, "low": a.or_low, "minutes": entry["or_minutes"]}
    else:
        entry = _need(os.path.join(TRADER, "gold-entry.json"), "ejecuta antes gold.py orb --write, o pasa --entry")
    plan = make_plan(entry, a.stop, a.target, cfg)
    print(json.dumps(plan, indent=2, ensure_ascii=False) if a.json else plan_report(plan))
    if a.write:
        _write(os.path.join(TRADER, "gold-plan.json"), plan)
    return 0


def cmd_replay(a, cfg):
    plan = _need(os.path.join(TRADER, "gold-plan.json"), "ejecuta antes gold.py plan --write")
    if plan.get("entry_bar") is None:
        raise GoldError("el plan no tiene entry_bar (la vela de entrada dentro de la sesion); pasalo con plan --entry-bar N")
    idx, _ = load_bars(a, cfg)
    doc = run_replay(idx, plan, cfg, a.replays, a.seed, a.window)
    print(json.dumps(doc, indent=2, ensure_ascii=False, default=str) if a.json else replay_report(doc))
    if a.write:
        _write(os.path.join(TRADER, "gold-replay.json"), doc)
    return 0


def cmd_size(a, cfg):
    plan = _need(os.path.join(TRADER, "gold-plan.json"), "ejecuta antes gold.py plan --write")
    for k in ("balance", "max_loss", "leverage", "spread"):
        v = getattr(a, k)
        if v is not None:
            cfg["max_loss_usd" if k == "max_loss" else k] = v
    o = size_order(plan, cfg)
    print(json.dumps(o, indent=2, ensure_ascii=False) if a.json else size_report(o))
    if a.write:
        _write(os.path.join(TRADER, "gold-order.json"), o)
    return 0


def cmd_ticket(a, cfg):
    o = _need(os.path.join(TRADER, "gold-order.json"), "ejecuta antes gold.py size --write")
    print(ticket_report(o))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="gold.py", description="Day trading de oro (XAUUSD) para MetaTrader 5.")
    p.add_argument("--config", help="configuracion (por defecto trader/gold.json)")
    sub = p.add_subparsers(dest="cmd")

    def data_opts(sp):
        sp.add_argument("--csv", help="velas exportadas de MT5 (M1 o M5)")
        sp.add_argument("--server-tz", help="hora del servidor del CSV: ny+7 (por defecto), +2, 0...")
        sp.add_argument("--offline", help="JSON de Yahoo guardado (.json o .json.gz)")
        sp.add_argument("--basis", type=float, help="suma esto a los precios de Yahoo (futuros -> spot)")

    sp = sub.add_parser("clock")
    sp.add_argument("--now")
    sp = sub.add_parser("orb")
    sp.add_argument("--session", default="london", choices=sorted(SESSIONS))
    sp.add_argument("--side", default="both", choices=["both", "long", "short"])
    sp.add_argument("--or-minutes", type=int)
    sp.add_argument("--now")
    sp.add_argument("--write", action="store_true")
    sp.add_argument("--json", action="store_true")
    data_opts(sp)
    sp = sub.add_parser("plan")
    sp.add_argument("--stop", default="or-mid")
    sp.add_argument("--target", default="2R")
    sp.add_argument("--entry", type=float)
    sp.add_argument("--side", choices=["long", "short"])
    sp.add_argument("--session", default="london", choices=sorted(SESSIONS))
    sp.add_argument("--or-high", type=float)
    sp.add_argument("--or-low", type=float)
    sp.add_argument("--or-minutes", type=int)
    sp.add_argument("--entry-bar", type=int)
    sp.add_argument("--entry-time")
    sp.add_argument("--date")
    sp.add_argument("--now")
    sp.add_argument("--write", action="store_true")
    sp.add_argument("--json", action="store_true")
    sp = sub.add_parser("replay")
    sp.add_argument("--replays", type=int, default=10000)
    sp.add_argument("--seed", type=int, default=19)
    sp.add_argument("--window", type=int, default=2)
    sp.add_argument("--write", action="store_true")
    sp.add_argument("--json", action="store_true")
    data_opts(sp)
    sp = sub.add_parser("size")
    sp.add_argument("--balance", type=float)
    sp.add_argument("--max-loss", type=float)
    sp.add_argument("--leverage", type=float)
    sp.add_argument("--spread", type=float)
    sp.add_argument("--write", action="store_true")
    sp.add_argument("--json", action="store_true")
    sub.add_parser("ticket")

    a = p.parse_args(argv)
    if not a.cmd:
        p.print_help()
        return 2
    try:
        cfg = load_config(a.config)
        return {"clock": cmd_clock, "orb": cmd_orb, "plan": cmd_plan, "replay": cmd_replay,
                "size": cmd_size, "ticket": cmd_ticket}[a.cmd](a, cfg)
    except GoldError as e:
        print("No se pudo: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
