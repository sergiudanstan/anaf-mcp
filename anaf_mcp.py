#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
anaf-mcp — server MCP local pentru date publice romanesti.

Surse (toate publice si gratuite, fara cont si fara cheie de API):
  - ANAF  : https://webservicesp.anaf.ro/api/PlatitorTvaRest/v9/tva   (date firma, TVA, inactivi, e-Factura)
  - ANAF  : https://webservicesp.anaf.ro/bilant                        (situatii financiare anuale)
  - BNR   : https://curs.bnr.ro/nbrfxrates.xml + arhiva pe ani         (curs valutar de referinta)

Fara dependinte externe: doar biblioteca standard Python 3.
Comunica pe stdio (JSON-RPC 2.0), conform protocolului MCP.

Rulare manuala pentru test:
    python3 anaf_mcp.py --test
    python3 anaf_mcp.py --firma 14399840
"""

import json
import os
import re
import sys
import time
import hashlib
import threading
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import date, datetime

SERVER_NAME = "anaf-mcp"
SERVER_VERSION = "1.0.0"
PROTOCOL_VERSION = "2024-11-05"

ANAF_TVA_URL = "https://webservicesp.anaf.ro/api/PlatitorTvaRest/v9/tva"
ANAF_BILANT_URL = "https://webservicesp.anaf.ro/bilant?an={an}&cui={cui}"
BNR_TODAY_URL = "https://curs.bnr.ro/nbrfxrates.xml"
BNR_YEAR_URL = "https://curs.bnr.ro/files/xml/years/nbrfxrates{an}.xml"

USER_AGENT = "anaf-mcp/1.0"  # atentie: WAF-ul ANAF respinge User-Agent cu paranteze/;
HTTP_TIMEOUT = 25

CACHE_DIR = os.path.join(
    os.environ.get("ANAF_MCP_CACHE_DIR")
    or os.path.join(os.path.expanduser("~"), ".cache", "anaf-mcp")
)

# Prospetimea cache-ului, in secunde, per tip de date.
TTL = {
    "anaf_firma": 6 * 3600,        # datele de identificare se schimba rar
    "anaf_bilant": 30 * 24 * 3600,  # bilanturile sunt anuale
    "bnr_today": 30 * 60,           # cursul zilei se publica ~13:00
    "bnr_year": 12 * 3600,          # arhiva anului curent
    "bnr_year_past": 90 * 24 * 3600,  # anii inchisi nu se mai schimba
}

_anaf_lock = threading.Lock()
_last_anaf_call = [0.0]  # ANAF: maximum 1 apel/secunda


# --------------------------------------------------------------------------
# Cache pe disc
# --------------------------------------------------------------------------

def _cache_path(key):
    h = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    return os.path.join(CACHE_DIR, h + ".json")


def cache_get(key, ttl):
    path = _cache_path(key)
    try:
        with open(path, "r", encoding="utf-8") as f:
            blob = json.load(f)
    except Exception:
        return None, None
    age = time.time() - blob.get("ts", 0)
    if age > ttl:
        return None, None
    return blob.get("data"), age


def cache_put(key, data):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = _cache_path(key) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "data": data}, f, ensure_ascii=False)
        os.replace(tmp, _cache_path(key))
    except Exception:
        pass  # cache-ul e optional, niciodata blocant


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def http_request(url, payload=None, accept="application/json"):
    data = None
    headers = {"User-Agent": USER_AGENT, "Accept": accept}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        body = b""
        try:
            body = e.read()
        except Exception:
            pass
        # ANAF raspunde 404 cu un corp JSON valid cand niciun CUI din lot nu e gasit.
        if body[:1] in (b"{", b"["):
            return body
        raise RuntimeError("HTTP %s de la %s: %s"
                           % (e.code, url, body.decode("utf-8", "replace")[:300]))
    except urllib.error.URLError as e:
        raise RuntimeError("Nu am putut contacta %s: %s" % (url, e.reason))
    return raw


def anaf_post(url, payload):
    """Apel ANAF cu respectarea limitei de 1 cerere/secunda."""
    with _anaf_lock:
        wait = 1.1 - (time.time() - _last_anaf_call[0])
        if wait > 0:
            time.sleep(wait)
        try:
            raw = http_request(url, payload=payload)
        finally:
            _last_anaf_call[0] = time.time()
    return json.loads(raw.decode("utf-8", "replace"))


def anaf_get(url):
    with _anaf_lock:
        wait = 1.1 - (time.time() - _last_anaf_call[0])
        if wait > 0:
            time.sleep(wait)
        try:
            raw = http_request(url)
        finally:
            _last_anaf_call[0] = time.time()
    return json.loads(raw.decode("utf-8", "replace"))


# --------------------------------------------------------------------------
# Utilitare
# --------------------------------------------------------------------------

def normalize_cui(value):
    """Accepta 'RO14399840', ' 14399840 ', 14399840 -> 14399840 (int)."""
    s = str(value).strip().upper().replace(" ", "")
    s = re.sub(r"^RO", "", s)
    if not s.isdigit():
        raise ValueError("CUI invalid: %r" % (value,))
    return int(s)


def normalize_date(value):
    if not value:
        return date.today().isoformat()
    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError("Data invalida: %r (foloseste YYYY-MM-DD)" % (value,))


# --------------------------------------------------------------------------
# ANAF: date firma
# --------------------------------------------------------------------------

def anaf_lookup(cuiuri, data_ref):
    """Returneaza (found, notFound) pentru o lista de CUI-uri, folosind cache-ul."""
    found = []
    not_found = []
    de_cerut = []

    for cui in cuiuri:
        key = "anaf_firma:%s:%s" % (cui, data_ref)
        cached, _age = cache_get(key, TTL["anaf_firma"])
        if cached is not None:
            if cached == "NOT_FOUND":
                not_found.append(cui)
            else:
                found.append(cached)
        else:
            de_cerut.append(cui)

    for i in range(0, len(de_cerut), 100):  # ANAF: max 100 CUI-uri per apel
        lot = de_cerut[i:i + 100]
        payload = [{"cui": c, "data": data_ref} for c in lot]
        resp = anaf_post(ANAF_TVA_URL, payload)
        if resp.get("cod") not in (200, None):
            raise RuntimeError("ANAF a raspuns: %s %s" % (resp.get("cod"), resp.get("message")))
        for rec in resp.get("found", []) or []:
            cui = rec.get("date_generale", {}).get("cui")
            cache_put("anaf_firma:%s:%s" % (cui, data_ref), rec)
            found.append(rec)
        for cui in resp.get("notFound", []) or []:
            cui_i = normalize_cui(cui)
            cache_put("anaf_firma:%s:%s" % (cui_i, data_ref), "NOT_FOUND")
            not_found.append(cui_i)

    return found, not_found


def rezumat_firma(rec):
    """Compacteaza raspunsul ANAF in campurile care conteaza intr-o verificare de partener."""
    g = rec.get("date_generale", {}) or {}
    tva = rec.get("inregistrare_scop_Tva", {}) or {}
    inactiv = rec.get("stare_inactiv", {}) or {}
    tvai = rec.get("inregistrare_RTVAI", {}) or {}
    split = rec.get("inregistrare_SplitTVA", {}) or {}
    sediu = rec.get("adresa_sediu_social", {}) or {}

    perioade = tva.get("perioade_TVA") or []
    ultima = perioade[-1] if perioade else {}

    return {
        "cui": g.get("cui"),
        "denumire": g.get("denumire"),
        "nr_reg_com": g.get("nrRegCom"),
        "adresa": g.get("adresa"),
        "judet": sediu.get("sdenumire_Judet"),
        "cod_caen": g.get("cod_CAEN"),
        "forma_juridica": g.get("forma_juridica"),
        "stare_inregistrare": g.get("stare_inregistrare"),
        "data_inregistrare": g.get("data_inregistrare"),
        "organ_fiscal": g.get("organFiscalCompetent"),
        "iban": g.get("iban") or None,
        "telefon": g.get("telefon") or None,
        "platitor_tva": bool(tva.get("scpTVA")),
        "tva_din": ultima.get("data_inceput_ScpTVA") or None,
        "tva_pana_la": ultima.get("data_sfarsit_ScpTVA") or None,
        "tva_la_incasare": bool(tvai.get("statusTvaIncasare")),
        "split_tva": bool(split.get("statusSplitTVA")),
        "inactiv": bool(inactiv.get("statusInactivi")),
        "data_inactivare": inactiv.get("dataInactivare") or None,
        "data_reactivare": inactiv.get("dataReactivare") or None,
        "data_radiere": inactiv.get("dataRadiere") or None,
        "ro_e_factura": bool(g.get("statusRO_e_Factura")),
        "e_factura_din": g.get("data_inreg_Reg_RO_e_Factura") or None,
    }


def semafor(s):
    """Verdict scurt pentru facturare: se poate lucra cu firma asta?"""
    probleme = []
    if s.get("inactiv"):
        probleme.append("firma este INACTIVA fiscal (cheltuielile cu ea nu sunt deductibile)")
    if s.get("data_radiere"):
        probleme.append("firma este RADIATA la %s" % s["data_radiere"])
    atentionari = []
    if not s.get("platitor_tva"):
        atentionari.append("NU este platitoare de TVA - factureaza fara TVA")
    if s.get("tva_la_incasare"):
        atentionari.append("aplica TVA la incasare")
    if s.get("split_tva"):
        atentionari.append("este in split TVA")
    if s.get("ro_e_factura"):
        atentionari.append("este inregistrata in Registrul RO e-Factura")
    return {
        "verdict": "ATENTIE" if probleme else "OK",
        "probleme": probleme,
        "de_stiut": atentionari,
    }


# --------------------------------------------------------------------------
# BNR: curs valutar
# --------------------------------------------------------------------------

def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _parse_bnr(xml_bytes):
    """-> {'YYYY-MM-DD': {'EUR': 5.0812, ...}, ...}
    Namespace-agnostic: BNR foloseste http://... in fisierele vechi si https://... in cele noi."""
    root = ET.fromstring(xml_bytes)
    out = {}
    for cube in (e for e in root.iter() if _local(e.tag) == "Cube"):
        d = cube.get("date")
        rates = {"RON": 1.0}
        for rate in cube:
            cur = rate.get("currency")
            mult = float(rate.get("multiplier") or 1)
            try:
                val = float((rate.text or "").strip())
            except ValueError:
                continue
            rates[cur] = val / mult
        out[d] = rates
    return out


def bnr_rates(an=None):
    if an is None:
        key, url, ttl = "bnr:today", BNR_TODAY_URL, TTL["bnr_today"]
    else:
        key = "bnr:%s" % an
        url = BNR_YEAR_URL.format(an=an)
        ttl = TTL["bnr_year"] if int(an) >= date.today().year else TTL["bnr_year_past"]
    cached, _age = cache_get(key, ttl)
    if cached is not None:
        return cached
    raw = http_request(url, accept="application/xml")
    parsed = _parse_bnr(raw)
    cache_put(key, parsed)
    return parsed


def curs_la_data(data_ref):
    """Cursul valabil la data ceruta (ultima zi bancara <= data)."""
    an = int(data_ref[:4])
    tabel = bnr_rates(None) if data_ref == date.today().isoformat() else {}
    if data_ref in tabel:
        return data_ref, tabel[data_ref]
    tabel = bnr_rates(an)
    zile = sorted(d for d in tabel if d <= data_ref)
    if not zile:
        tabel_prec = bnr_rates(an - 1)
        zile = sorted(tabel_prec)
        if not zile:
            raise RuntimeError("Nu am gasit curs BNR pentru %s" % data_ref)
        return zile[-1], tabel_prec[zile[-1]]
    return zile[-1], tabel[zile[-1]]


# --------------------------------------------------------------------------
# Implementarea tool-urilor
# --------------------------------------------------------------------------

def tool_anaf_firma(args):
    cui = normalize_cui(args.get("cui"))
    data_ref = normalize_date(args.get("data"))
    complet = bool(args.get("complet"))
    found, not_found = anaf_lookup([cui], data_ref)
    if not found:
        return {"gasit": False, "cui": cui, "mesaj": "CUI-ul %s nu exista in evidentele ANAF" % cui}
    rec = found[0]
    s = rezumat_firma(rec)
    out = {"gasit": True, "data_referinta": data_ref, "firma": s, "evaluare": semafor(s),
           "sursa": ANAF_TVA_URL}
    if complet:
        out["raspuns_anaf_brut"] = rec
    return out


def tool_anaf_firme(args):
    lista = args.get("cuiuri") or []
    if not isinstance(lista, list) or not lista:
        raise ValueError("Trimite 'cuiuri' ca lista de coduri fiscale")
    if len(lista) > 100:
        raise ValueError("Maximum 100 de CUI-uri per apel (limita ANAF)")
    data_ref = normalize_date(args.get("data"))
    cuiuri = [normalize_cui(c) for c in lista]
    found, not_found = anaf_lookup(cuiuri, data_ref)
    rezumate = [rezumat_firma(r) for r in found]
    return {
        "data_referinta": data_ref,
        "gasite": rezumate,
        "negasite": not_found,
        "inactive": [r["cui"] for r in rezumate if r["inactiv"]],
        "neplatitoare_tva": [r["cui"] for r in rezumate if not r["platitor_tva"]],
        "sursa": ANAF_TVA_URL,
    }


def tool_anaf_bilant(args):
    cui = normalize_cui(args.get("cui"))
    an = int(args.get("an") or (date.today().year - 1))
    key = "anaf_bilant:%s:%s" % (cui, an)
    cached, _age = cache_get(key, TTL["anaf_bilant"])
    if cached is None:
        cached = anaf_get(ANAF_BILANT_URL.format(an=an, cui=cui))
        cache_put(key, cached)
    indicatori = {}
    for it in cached.get("i", []) or []:
        den = (it.get("val_den_indicator") or "").strip()
        indicatori[den] = it.get("val_indicator")
    if not indicatori:
        return {"gasit": False, "cui": cui, "an": an,
                "mesaj": "ANAF nu are bilant publicat pentru CUI %s pe anul %s" % (cui, an)}
    return {
        "gasit": True,
        "cui": cui,
        "an": an,
        "denumire": cached.get("deni"),
        "caen": cached.get("caen"),
        "den_caen": cached.get("den_caen"),
        "indicatori_lei": indicatori,
        "sursa": ANAF_BILANT_URL.format(an=an, cui=cui),
    }


def tool_bnr_curs(args):
    data_ref = normalize_date(args.get("data"))
    valute = args.get("valute")
    zi, rates = curs_la_data(data_ref)
    sursa = BNR_TODAY_URL if zi == date.today().isoformat() else BNR_YEAR_URL.format(an=zi[:4])
    if valute:
        if isinstance(valute, str):
            valute = [valute]
        sel = {}
        for v in valute:
            v = v.strip().upper()
            if v not in rates:
                raise ValueError("Valuta %s nu apare in cursul BNR din %s" % (v, zi))
            sel[v] = rates[v]
    else:
        sel = rates
    return {"data_curs": zi, "moneda_de_baza": "RON", "cursuri": sel, "sursa": sursa}


def tool_bnr_conversie(args):
    suma = float(args.get("suma"))
    din = str(args.get("din", "EUR")).upper()
    catre = str(args.get("in") or args.get("catre") or "RON").upper()
    data_ref = normalize_date(args.get("data"))
    zi, rates = curs_la_data(data_ref)
    sursa = BNR_TODAY_URL if zi == date.today().isoformat() else BNR_YEAR_URL.format(an=zi[:4])
    for v in (din, catre):
        if v not in rates:
            raise ValueError("Valuta %s nu apare in cursul BNR din %s" % (v, zi))
    in_ron = suma * rates[din]
    rezultat = in_ron / rates[catre]
    return {
        "data_curs": zi,
        "suma": suma,
        "din": din,
        "in": catre,
        "rezultat": round(rezultat, 4),
        "curs_folosit": round(rates[din] / rates[catre], 6),
        "sursa": sursa,
    }


TOOLS = [
    {
        "name": "anaf_firma",
        "description": (
            "Date oficiale ANAF despre o firma dupa CUI: denumire, nr. reg. com., adresa, CAEN, "
            "stare TVA (inclusiv TVA la incasare si split TVA), stare de inactivitate fiscala, "
            "inregistrare in Registrul RO e-Factura, plus un verdict scurt daca se poate factura in siguranta."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "cui": {"type": "string", "description": "Codul fiscal, cu sau fara prefixul RO (ex. 'RO14399840')"},
                "data": {"type": "string", "description": "Data de referinta YYYY-MM-DD (implicit azi)"},
                "complet": {"type": "boolean", "description": "true = include si raspunsul brut ANAF"},
            },
            "required": ["cui"],
        },
    },
    {
        "name": "anaf_firme",
        "description": (
            "Verifica in bloc pana la 100 de CUI-uri intr-un singur apel ANAF. Util pentru curatarea "
            "unei baze de clienti/furnizori: intoarce rezumatul fiecarei firme si listele de firme "
            "inactive, neplatitoare de TVA sau negasite."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "cuiuri": {"type": "array", "items": {"type": "string"}, "description": "Lista de coduri fiscale (max. 100)"},
                "data": {"type": "string", "description": "Data de referinta YYYY-MM-DD (implicit azi)"},
            },
            "required": ["cuiuri"],
        },
    },
    {
        "name": "anaf_bilant",
        "description": (
            "Situatiile financiare anuale depuse la ANAF pentru un CUI si un an: cifra de afaceri, "
            "profit/pierdere, active, datorii, numar mediu de salariati."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "cui": {"type": "string", "description": "Codul fiscal"},
                "an": {"type": "integer", "description": "Anul bilantului (implicit anul trecut)"},
            },
            "required": ["cui"],
        },
    },
    {
        "name": "bnr_curs",
        "description": (
            "Cursul valutar de referinta BNR pentru o data (implicit azi). Daca data cade in weekend "
            "sau sarbatoare, intoarce cursul ultimei zile bancare anterioare."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "data": {"type": "string", "description": "Data YYYY-MM-DD (implicit azi)"},
                "valute": {"type": "array", "items": {"type": "string"}, "description": "Ex. ['EUR','USD']; gol = toate"},
            },
        },
    },
    {
        "name": "bnr_conversie",
        "description": "Converteste o suma intre doua valute la cursul BNR dintr-o data data (implicit azi).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "suma": {"type": "number"},
                "din": {"type": "string", "description": "Valuta sursa, ex. EUR"},
                "in": {"type": "string", "description": "Valuta tinta, ex. RON"},
                "data": {"type": "string", "description": "Data YYYY-MM-DD (implicit azi)"},
            },
            "required": ["suma", "din", "in"],
        },
    },
]

HANDLERS = {
    "anaf_firma": tool_anaf_firma,
    "anaf_firme": tool_anaf_firme,
    "anaf_bilant": tool_anaf_bilant,
    "bnr_curs": tool_bnr_curs,
    "bnr_conversie": tool_bnr_conversie,
}


# --------------------------------------------------------------------------
# Bucla MCP (JSON-RPC 2.0 pe stdio)
# --------------------------------------------------------------------------

def send(msg):
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle(req):
    method = req.get("method")
    rid = req.get("id")
    params = req.get("params") or {}

    if method == "initialize":
        return {
            "jsonrpc": "2.0", "id": rid,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }

    if method in ("notifications/initialized", "initialized", "notifications/cancelled"):
        return None

    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = HANDLERS.get(name)
        if fn is None:
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32601, "message": "Tool necunoscut: %s" % name}}
        try:
            result = fn(args)
            text = json.dumps(result, ensure_ascii=False, indent=2)
            return {"jsonrpc": "2.0", "id": rid,
                    "result": {"content": [{"type": "text", "text": text}], "isError": False}}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": rid,
                    "result": {"content": [{"type": "text", "text": "Eroare: %s" % e}], "isError": True}}

    if rid is None:
        return None
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": -32601, "message": "Metoda nesuportata: %s" % method}}


def serve():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        if isinstance(req, list):
            for r in req:
                resp = handle(r)
                if resp:
                    send(resp)
            continue
        resp = handle(req)
        if resp:
            send(resp)


def cli():
    a = sys.argv[1:]
    if a[0] == "--firma":
        print(json.dumps(tool_anaf_firma({"cui": a[1]}), ensure_ascii=False, indent=2))
    elif a[0] == "--bilant":
        print(json.dumps(tool_anaf_bilant({"cui": a[1], "an": int(a[2]) if len(a) > 2 else None}),
                         ensure_ascii=False, indent=2))
    elif a[0] == "--curs":
        print(json.dumps(tool_bnr_curs({"data": a[1] if len(a) > 1 else None}), ensure_ascii=False, indent=2))
    elif a[0] == "--test":
        print("tool-uri:", ", ".join(t["name"] for t in TOOLS))
        print(json.dumps(tool_bnr_curs({"valute": ["EUR", "USD"]}), ensure_ascii=False, indent=2))
    else:
        print(__doc__)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        cli()
    else:
        serve()
