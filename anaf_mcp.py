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
import base64
import zlib
import csv
import sqlite3
import threading
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import date, datetime

SERVER_NAME = "anaf-mcp"
SERVER_VERSION = "1.1.0"
PROTOCOL_VERSION = "2024-11-05"

ANAF_TVA_URL = "https://webservicesp.anaf.ro/api/PlatitorTvaRest/v9/tva"
ANAF_BILANT_URL = "https://webservicesp.anaf.ro/bilant?an={an}&cui={cui}"
BNR_TODAY_URL = "https://curs.bnr.ro/nbrfxrates.xml"
BNR_YEAR_URL = "https://curs.bnr.ro/files/xml/years/nbrfxrates{an}.xml"

# Lista firmelor de la Registrul Comertului, publicata lunar pe portalul de date deschise.
ONRC_PACHET_URL = ("https://data.gov.ro/api/3/action/package_search"
                   "?q=%22Firme%20inregistrate%20la%20Registrul%20Comertului%22"
                   "&rows=1&sort=metadata_modified%20desc")

USER_AGENT = "anaf-mcp/1.1"  # atentie: WAF-ul ANAF respinge User-Agent cu paranteze/;
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

# --------------------------------------------------------------------------
# Nomenclator CAEN (Rev.2, 615 clase) - sursa: data.gov.ro, "CAEN Clase Rev2".
# Pastrat comprimat, ca fisierul sa ramana unul singur si fara dependinte.
# --------------------------------------------------------------------------

_CAEN_B64 = (
    "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"
    "TS+rEXo5PjzmM5j4TctTTaYYCBCTK9e39uuqUETIc+Zb/zQq2Nqr2zHyZBo51iNxelzhapcwZDoN6V0vf9XUnGTTzb1bfGtz"
    "548KUUjbtf7RDp6XJp1ZmukCYgA3M2tTaCC492MrBzCCmcmPy4KtV1UPhI2zmy5V5tcd4z7mVmxQuw7abh8aj39RHIB200Cu"
    "vtU6LFo1UbIK8O0n8NHJuNAdZikQIZbDMuEZXauKMRwmMPgHEOmL83p0ean1ytZ34CINXoWGQXkG7uM40Ucp/V785OZlBpJt"
    "Hlp/d7EaQGZoMBoJil03BTRzx9AlagySCYILB+ugrc5grKxh8qruLpgMXWYPE5u9ErD27nys7amevQIGHT1G1e7ulxZNfFXM"
    "qNC7bPcQmuAKFMDtp+FIPkpKyXsdBrOD9D8vcQMwHuZYOWU9WQej5xRn2OPMRnPxC9aDxhtM8Ns5MTIMy6+nI/Labrt0/wZN"
    "v10ZK4+zuCa0XC8jLsymCn23VBJhbAnvdjOD9xw5TnsMJkARfXS1+UxzlzyC9Fudo8kg6UPjGAViMYdemqD8SNPbZb8Nqwke"
    "m/PktcVl71tdP4tcxHRTn8lwt10kbJvHwQC7JdrzQ7yBd9qi4TCt8wIzrd7gsFJcCqkQs/TR68Peq6O5XuEA+uZEKUItZo6e"
    "TM4olhap6gqdLFzaBxklX5RmPEmIB/VR8Un1poG1Pzu/VWdTvn2UznFyzEYD5Kg0q8tw+i6mB2L3P3p+vVuvL7Nlx+1oXUJv"
    "8fZ5M2qji42ZPIfW9luLFi+kKIJLHofjnSCoNLZTBi/t1fub0pmc5NNUVCpCB4m3HIn1/f3DjZ4F3oynKpCXl6PJbZLNTEs3"
    "nU0y2n8/7rJNtEeVgmsooujHZPNwgbko0TCSZBmJg0YNvcZkPh43XzF9GFoXY+IVYHi6dcPZXG5e8fJ9UWwIkI5L+NUVqFNs"
    "t7YXvC/fk6cWHSvVqYBtpbbwkzCjpakBT0ezhmJ+W449jcdhcWvOlVOAESG4Wt4/VR15c12y+EXk6F+1C4S3GZpBUHdrandE"
    "G+pNfAWtlOqUm0BoJc7sfYYhYBnOc28401bfs+AEdxse6IuKlE9xq1V5tVqiOEk9iqOdsu/h52FgAy4e8lcgH4EF8fjo76KE"
    "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"
    "bIBYCEOnruko+8iMY8r3AM1K/t//DyJX/8M="
)

_caen_cache = [None]


def caen_tabel():
    if _caen_cache[0] is None:
        _caen_cache[0] = json.loads(zlib.decompress(base64.b64decode(_CAEN_B64)).decode("utf-8"))
    return _caen_cache[0]


def caen_denumire(cod):
    """4754 / '4754' / '04754' -> denumirea clasei CAEN, sau None."""
    if cod in (None, ""):
        return None
    c = str(cod).strip().lstrip("0") or "0"
    t = caen_tabel()
    return t.get(c.zfill(4)) or t.get(c)


def caen_cauta(text, limita=20):
    """Cauta dupa cod (prefix) sau dupa cuvinte din denumire; fara diacritice."""
    def fara_diac(s):
        for a, b in (("ăâ", "aa"), ("î", "i"), ("șş", "ss"), ("țţ", "tt")):
            for i, ch in enumerate(a):
                s = s.replace(ch, b[i])
        return s.lower()

    q = (text or "").strip()
    t = caen_tabel()
    if not q:
        return []
    rez = []
    if q.isdigit():
        for cod in sorted(t):
            if cod.startswith(q):
                rez.append({"cod": cod, "denumire": t[cod]})
    else:
        cuvinte = [w for w in fara_diac(q).split() if w]
        for cod in sorted(t):
            den = fara_diac(t[cod])
            if all(w in den for w in cuvinte):
                rez.append({"cod": cod, "denumire": t[cod]})
    return rez[:limita]


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
        "cod_postal": g.get("codPostal") or None,
        "judet": sediu.get("sdenumire_Judet"),
        "cod_caen": g.get("cod_CAEN"),
        "den_caen": caen_denumire(g.get("cod_CAEN")),
        "forma_juridica": g.get("forma_juridica"),
        "forma_organizare": g.get("forma_organizare") or None,
        "forma_proprietate": g.get("forma_de_proprietate") or None,
        "stare_inregistrare": g.get("stare_inregistrare"),
        "data_inregistrare": g.get("data_inregistrare"),
        "organ_fiscal": g.get("organFiscalCompetent"),
        "iban": g.get("iban") or None,
        "telefon": g.get("telefon") or None,
        "fax": g.get("fax") or None,
        "act_autorizare": g.get("act") or None,
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


def tool_search_caen(args):
    q = args.get("q") or args.get("text") or args.get("cod")
    limita = int(args.get("limita") or 20)
    rez = caen_cauta(q, limita)
    return {"cautare": q, "gasite": len(rez), "rezultate": rez,
            "nomenclator": "CAEN Rev.2 (data.gov.ro)"}


# --------------------------------------------------------------------------
# Cautare firma dupa denumire (ONRC, prin data.gov.ro)
#
# ANAF nu ofera cautare dupa nume, doar dupa CUI. Registrul Comertului publica
# insa lunar lista completa a firmelor, ca CSV, pe portalul de date deschise.
# O descarcam o data (~650 MB) si o indexam local intr-un SQLite, dupa care
# cautarea merge offline si instant. sqlite3 e in biblioteca standard.
# --------------------------------------------------------------------------

ONRC_DB = os.path.join(CACHE_DIR, "onrc_firme.sqlite")


def _norm_nume(s):
    """Minuscule, fara diacritice si fara punctuatie - pentru cautare tolerantă."""
    s = (s or "").lower()
    for a, b in (("ăâ", "aa"), ("î", "i"), ("șş", "ss"), ("țţ", "tt")):
        for i, ch in enumerate(a):
            s = s.replace(ch, b[i])
    return re.sub(r"[^a-z0-9 ]+", " ", s).strip()


def onrc_resursa_url(nume_fisier="od_firme.csv"):
    """Intoarce URL-ul celei mai recente publicari a fisierului cerut."""
    raw = http_request(ONRC_PACHET_URL)
    pachete = json.loads(raw.decode("utf-8", "replace")).get("result", {}).get("results", [])
    if not pachete:
        raise RuntimeError("Nu am gasit pachetul ONRC pe data.gov.ro")
    p = pachete[0]
    for res in p.get("resources", []):
        if (res.get("url") or "").lower().endswith(nume_fisier):
            return res["url"], p.get("title", "")
    raise RuntimeError("Pachetul ONRC nu contine %s" % nume_fisier)


def onrc_sync(cale_csv=None, progres=None):
    """Descarca (sau citeste local) od_firme.csv si construieste indexul SQLite."""
    titlu = "fisier local"
    if cale_csv is None:
        url, titlu = onrc_resursa_url()
        cale_csv = os.path.join(CACHE_DIR, "od_firme.csv")
        os.makedirs(CACHE_DIR, exist_ok=True)
        if progres:
            progres("descarc %s ..." % url.split("/")[-1])
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=120) as r, open(cale_csv, "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)

    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = ONRC_DB + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    con = sqlite3.connect(tmp)
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")
    con.execute("""CREATE TABLE firme (cui INTEGER, denumire TEXT, nume_norm TEXT,
                   cod_inmatriculare TEXT, data_inmatriculare TEXT, forma_juridica TEXT,
                   judet TEXT, localitate TEXT)""")

    n = 0
    with open(cale_csv, encoding="utf-8", errors="replace", newline="") as f:
        r = csv.DictReader(f, delimiter="^")
        lot = []
        for row in r:
            den = (row.get("DENUMIRE") or row.get("\ufeffDENUMIRE") or "").strip()
            if not den:
                continue
            try:
                cui = int(row.get("CUI") or 0)
            except ValueError:
                cui = 0
            lot.append((cui, den, _norm_nume(den), (row.get("COD_INMATRICULARE") or "").strip(),
                        (row.get("DATA_INMATRICULARE") or "").strip(),
                        (row.get("FORMA_JURIDICA") or "").strip(),
                        (row.get("ADR_JUDET") or "").strip(),
                        (row.get("ADR_LOCALITATE") or "").strip()))
            n += 1
            if len(lot) >= 20000:
                con.executemany("INSERT INTO firme VALUES (?,?,?,?,?,?,?,?)", lot)
                lot = []
                if progres and n % 200000 == 0:
                    progres("  %d firme indexate..." % n)
        if lot:
            con.executemany("INSERT INTO firme VALUES (?,?,?,?,?,?,?,?)", lot)

    con.execute("CREATE INDEX idx_nume ON firme(nume_norm)")
    con.execute("CREATE INDEX idx_cui ON firme(cui)")
    con.execute("CREATE TABLE meta (cheie TEXT, valoare TEXT)")
    con.executemany("INSERT INTO meta VALUES (?,?)",
                    [("sursa", titlu), ("firme", str(n)), ("actualizat", str(int(time.time())))])
    con.commit()
    con.close()
    os.replace(tmp, ONRC_DB)
    return n


def onrc_cauta(nume, judet=None, limita=20):
    if not os.path.exists(ONRC_DB):
        raise RuntimeError(
            "Indexul ONRC lipseste. Ruleaza o data: python3 anaf_mcp.py --sync-onrc "
            "(descarca ~650 MB de la data.gov.ro si construieste indexul local)")
    q = _norm_nume(nume)
    if not q:
        return [], {}
    con = sqlite3.connect(ONRC_DB)
    sql = "SELECT cui, denumire, cod_inmatriculare, data_inmatriculare, forma_juridica, judet, localitate FROM firme WHERE nume_norm LIKE ?"
    par = ["%" + q + "%"]
    if judet:
        sql += " AND lower(judet) LIKE ?"
        par.append("%" + _norm_nume(judet) + "%")
    # potrivirile care incep cu termenul cautat sunt mai relevante
    sql += " ORDER BY (nume_norm LIKE ?) DESC, length(denumire) LIMIT ?"
    par += [q + "%", int(limita)]
    rows = con.execute(sql, par).fetchall()
    meta = dict(con.execute("SELECT cheie, valoare FROM meta").fetchall())
    con.close()
    rez = [{"cui": r[0] or None, "denumire": r[1], "nr_reg_com": r[2],
            "data_inmatriculare": r[3], "forma_juridica": r[4],
            "judet": r[5], "localitate": r[6]} for r in rows]
    return rez, meta


def tool_search_company(args):
    nume = args.get("nume") or args.get("q") or args.get("denumire")
    rez, meta = onrc_cauta(nume, args.get("judet"), int(args.get("limita") or 20))
    return {"cautare": nume, "judet": args.get("judet"), "gasite": len(rez), "rezultate": rez,
            "sursa": "Registrul Comertului via data.gov.ro (%s)" % meta.get("sursa", ""),
            "firme_in_index": int(meta.get("firme", 0) or 0),
            "nota": "Pentru date fiscale la zi (TVA, inactiv), ia CUI-ul de aici si cheama anaf_firma."}


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
    {
        "name": "search_caen",
        "description": (
            "Cauta in nomenclatorul CAEN Rev.2: dupa cod (exact sau prefix, ex. '47' sau '4754') "
            "sau dupa cuvinte din denumire (ex. 'instalatii electrice'). Diacriticele nu conteaza."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "q": {"type": "string", "description": "Cod CAEN sau cuvinte din denumirea activitatii"},
                "limita": {"type": "integer", "description": "Numar maxim de rezultate (implicit 20)"},
            },
            "required": ["q"],
        },
    },
    {
        "name": "search_company",
        "description": (
            "Cauta o firma DUPA DENUMIRE (ANAF permite doar cautare dupa CUI). Foloseste lista "
            "Registrului Comertului, indexata local. Intoarce CUI-ul, care apoi se poate da la "
            "anaf_firma pentru datele fiscale la zi. Necesita o sincronizare unica: --sync-onrc."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "nume": {"type": "string", "description": "Denumirea sau o parte din ea (diacriticele nu conteaza)"},
                "judet": {"type": "string", "description": "Filtru optional dupa judet, ex. 'Cluj'"},
                "limita": {"type": "integer", "description": "Numar maxim de rezultate (implicit 20)"},
            },
            "required": ["nume"],
        },
    },
]


HANDLERS = {
    "search_caen": tool_search_caen,
    "search_company": tool_search_company,
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
    elif a[0] == "--sync-onrc":
        n = onrc_sync(a[1] if len(a) > 1 else None, progres=lambda m: print(m, flush=True))
        print("index ONRC construit: %d firme -> %s" % (n, ONRC_DB))
    elif a[0] == "--cauta":
        print(json.dumps(tool_search_company({"nume": " ".join(a[1:])}), ensure_ascii=False, indent=2))
    elif a[0] == "--caen":
        print(json.dumps(tool_search_caen({"q": " ".join(a[1:])}), ensure_ascii=False, indent=2))
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
