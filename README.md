# anaf-mcp — MCP local pentru date publice ANAF / BNR

Server MCP care ia datele **direct de la sursă**, nu printr-un intermediar plătit.
Un singur fișier Python, **zero dependențe** (doar biblioteca standard), rulează local.

📖 **Tutorial și documentație: [sergiudanstan.github.io/anaf-mcp](https://sergiudanstan.github.io/anaf-mcp/)**

## Ce date ia și de unde

| Sursă | Endpoint | Ce conține |
|---|---|---|
| ANAF | `webservicesp.anaf.ro/api/PlatitorTvaRest/v9/tva` | denumire, nr. reg. com., adresă, CAEN, TVA, TVA la încasare, split TVA, inactivi, RO e-Factura |
| ANAF | `webservicesp.anaf.ro/bilant` | bilanțuri anuale: cifră de afaceri, profit/pierdere, active, datorii, salariați |
| BNR | `curs.bnr.ro/nbrfxrates.xml` + arhiva pe ani | curs valutar de referință, azi și istoric |

Toate sunt publice, fără cont și fără cheie de API.

## Tool-uri expuse

- **`anaf_firma`** — un CUI: date complete + verdict scurt („OK" / „ATENȚIE": inactivă, radiată, neplătitoare de TVA…)
- **`anaf_firme`** — până la 100 de CUI-uri într-un singur apel; întoarce și listele de firme inactive / neplătitoare de TVA / negăsite. Bun pentru curățat baza de clienți sau furnizori.
- **`anaf_bilant`** — situațiile financiare pe un an
- **`bnr_curs`** — cursul zilei sau al unei date din trecut (weekend/sărbătoare → ultima zi bancară)
- **`bnr_conversie`** — conversie sumă între valute la cursul BNR dintr-o dată anume

---

# Tutorial

## 1. Instalare

Ai nevoie doar de Python 3 (există deja pe macOS și Linux; pe Windows se ia de pe python.org).

```bash
git clone https://github.com/sergiudanstan/anaf-mcp.git
cd anaf-mcp
python3 anaf_mcp.py --firma 14399840     # test rapid, trebuie să scoată datele eMAG
```

Dacă vezi un JSON cu `"denumire": "DANTE INTERNATIONAL SA"`, serverul merge. Reține calea absolută a fișierului — o folosești mai jos:

```bash
pwd    # ex: /Users/dan/anaf-mcp  →  calea completă e /Users/dan/anaf-mcp/anaf_mcp.py
```

## 2. Claude Code

O singură comandă, din orice folder:

```bash
claude mcp add anaf -- python3 /CALEA/ABSOLUTA/CATRE/anaf-mcp/anaf_mcp.py
```

Adaugă `-s user` dacă vrei serverul disponibil în toate proiectele, nu doar în cel curent:

```bash
claude mcp add anaf -s user -- python3 /CALEA/ABSOLUTA/CATRE/anaf-mcp/anaf_mcp.py
```

Verifici:

```bash
claude mcp list          # anaf trebuie să apară cu ✓ connected
```

În sesiunea Claude Code, `/mcp` îți arată tool-urile încărcate. De acolo încolo ceri în limbaj natural:

```
verifică CUI 14399840 la ANAF
ia-mi bilanțul pe 2024 pentru 14399840 și spune-mi marja netă
am lista asta de CUI-uri din facturi — zi-mi care sunt inactive sau neplătitoare de TVA
cât era euro pe 30 iunie 2026?
convertește 12.500 EUR în RON la cursul din 15 martie 2026
```

Ștergi serverul cu `claude mcp remove anaf`.

## 3. Codex (OpenAI Codex CLI)

Editezi `~/.codex/config.toml` și adaugi:

```toml
[mcp_servers.anaf]
command = "python3"
args = ["/CALEA/ABSOLUTA/CATRE/anaf-mcp/anaf_mcp.py"]
```

Pe versiunile recente de Codex CLI merge și direct din terminal:

```bash
codex mcp add anaf -- python3 /CALEA/ABSOLUTA/CATRE/anaf-mcp/anaf_mcp.py
codex mcp list
```

Repornești `codex` și ceri la fel, în limbaj natural: *„verifică la ANAF CUI-ul 14399840"*.

> Atenție la numele secțiunii: în Codex e `mcp_servers` (cu underscore), în Claude e `mcpServers` (camelCase). E cea mai frecventă greșeală.

## 4. Claude Desktop

Editezi `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) sau
`%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "anaf": {
      "command": "python3",
      "args": ["/CALEA/ABSOLUTA/CATRE/anaf-mcp/anaf_mcp.py"]
    }
  }
}
```

Repornești complet Claude Desktop (Quit, nu doar închis fereastra). Tool-urile apar la iconița de MCP din bara de input.

## 5. Verificare din terminal, fără client

Fiecare tool are un echivalent CLI, util ca să vezi dacă problema e la server sau la client:

```bash
python3 anaf_mcp.py --firma RO14399840
python3 anaf_mcp.py --bilant 14399840 2024
python3 anaf_mcp.py --curs 2026-06-30
python3 anaf_mcp.py --test
```

## 6. Dacă nu merge

| Simptom | Cauză uzuală |
|---|---|
| serverul nu apare / „failed to connect" | calea din config nu e absolută, sau `python3` nu e în PATH-ul clientului — pune calea completă, ex. `/usr/bin/python3` |
| merge în terminal, nu în client | ai editat alt fișier de config, sau nu ai repornit clientul |
| „HTTP 403" de la ANAF | ai modificat `USER_AGENT`; WAF-ul ANAF respinge șirurile cu paranteze sau `;` |
| date vechi | e cache-ul: `rm -rf ~/.cache/anaf-mcp` |

---

## Cache și prospețime

Cache pe disc în `~/.cache/anaf-mcp/` (sau `ANAF_MCP_CACHE_DIR`), cu TTL per tip de date:

| Date | Prospețime |
|---|---|
| date firmă (ANAF) | 6 ore |
| bilanț | 30 de zile |
| curs BNR ziua curentă | 30 de minute |
| arhiva BNR an curent / ani încheiați | 12 ore / 90 de zile |

Ștergi cache-ul cu `rm -rf ~/.cache/anaf-mcp`.

## Limite respectate

- ANAF: maximum **100 CUI-uri per apel** și **1 apel/secundă** — serverul serializează și așteaptă singur.
- WAF-ul ANAF respinge cereri cu `User-Agent` care conține paranteze sau `;`. Nu modifica `USER_AGENT` decât cu un șir simplu (`nume/versiune`).

## Ce NU acoperă

ONRC nu are API public gratuit (asociați, administratori, istoric mențiuni se iau prin RECOM, contra cost).
Codurile CAEN sunt întoarse ca număr de ANAF; dacă vrei și denumirea lor la `anaf_firma`, se adaugă un tabel local CAEN Rev.3.

## Licență

MIT — vezi [LICENSE](LICENSE). Datele aparțin ANAF și BNR; serverul doar le citește din endpoint-urile publice.
