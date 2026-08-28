# anaf-mcp — MCP remote și local pentru date publice ANAF / BNR

Server MCP care ia datele **direct de la sursă**, nu printr-un intermediar plătit.
Poate fi adăugat printr-un singur URL în Claude sau Codex, ori poate rula local.
Implementarea folosește doar biblioteca standard Python.

📖 **Tutorial și documentație: [sergiudanstan.github.io/anaf-mcp](https://sergiudanstan.github.io/anaf-mcp/)**

## Ce date ia și de unde

| Sursă | Endpoint | Ce conține |
|---|---|---|
| ONRC | `data.gov.ro` — „Firme înregistrate la Registrul Comerțului" | denumire, CUI, nr. reg. com., formă juridică, județ, localitate — pentru căutarea după nume |
| Min. Finanțelor | `data.gov.ro` — „Situațiile financiare" | cifră de afaceri, profit net, salariați, active, datorii, pentru toate firmele — pentru clasamente |
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
- **`search_caen`** — caută în nomenclatorul CAEN după cod (`4754`, sau prefix `47`) sau după cuvinte din denumire (`instalații electrice`); diacriticele nu contează
- **`top_firme`** — clasamentul firmelor dintr-un an după cifra de afaceri, profit net sau număr de salariați, filtrabil pe cod CAEN și județ. Fără limitări de tip Free/Pro
- **`search_company`** — **caută firma după denumire**, nu după CUI. ANAF nu oferă căutare după nume, așa că se folosește lista Registrului Comerțului de pe data.gov.ro, indexată local. Necesită o sincronizare unică (vezi mai jos)

---

# Instalare rapidă prin link (recomandată)

Versiunea remote expune un endpoint MCP **Streamable HTTP**, public și read-only. Nu cere cont,
OAuth Client ID, secret sau cheie API. După ce faci deploy, URL-ul conectorului este:

```text
https://NUMELE-DEPLOYMENTULUI.vercel.app/mcp
```

[![Deploy with Vercel](https://vercel.com/button)](https://vercel.com/new/clone?repository-url=https%3A%2F%2Fgithub.com%2Fsergiudanstan%2Fanaf-mcp&project-name=anaf-mcp&repository-name=anaf-mcp)

Build-ul descarcă automat indexul compact din GitHub Releases. Nu sunt necesare variabile de mediu.

## Claude / Claude Desktop

1. Deschide **Customize → Connectors → + → Add custom connector**.
2. Pune un nume, de exemplu `ANAF România`.
3. Lipește URL-ul complet care se termină în **`/mcp`**.
4. Nu completa setările OAuth; conectorul este public și nu are autentificare.

Pentru Claude Code, aceeași instalare se face dintr-o comandă:

```bash
claude mcp add --transport http anaf https://NUMELE-DEPLOYMENTULUI.vercel.app/mcp
claude mcp list
```

## Codex / ChatGPT desktop

În aplicația desktop: **Settings → MCP servers → Add server → Streamable HTTP**, apoi lipește URL-ul.
Codex CLI acceptă același endpoint:

```bash
codex mcp add anaf --url https://NUMELE-DEPLOYMENTULUI.vercel.app/mcp
codex mcp list
```

> Dacă apare un mesaj despre „sign-in service” sau OAuth Client ID, verifică URL-ul: trebuie să fie
> endpointul HTTPS terminat în `/mcp`, nu pagina GitHub și nu pagina de documentație GitHub Pages.

---

# Instalare locală (opțional)

## 1. Descărcare și test

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

## 2. Claude Code local

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

## 3. Codex local (OpenAI Codex CLI)

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

## 4. Claude Desktop local

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

## 5. Indexul de date (sincronizare unică)

`search_company` și `top_firme` lucrează peste un index local. Îl iei gata construit, dintr-o comandă:

```bash
python3 anaf_mcp.py --sync
```

Descarcă ~350 MB de pe GitHub Releases și îl despachetează în `~/.cache/anaf-mcp/` (~830 MB pe disc). Conține **4.201.586 de firme** de la Registrul Comerțului și **899.961 de rânduri** din situațiile financiare anuale.

Indexul se reconstruiește rulând build-ul și publicând rezultatul ca release:

```bash
python3 tools/build_index.py --iesire anaf-index.sqlite --gzip
gh release delete latest --yes && gh release create latest anaf-index.sqlite.gz --title "Index curent"
```

Build-ul **nu poate rula pe runnerele GitHub**: data.gov.ro rezolvă DNS-ul, dar refuză conexiunile venite din cloud-ul din afara țării (verificat — `curl` expiră după 20 s de pe `ubuntu-latest`, în timp ce de pe o rețea din România merge). Workflow-ul din `.github/workflows/` există, dar are sens doar pe un runner self-hosted dintr-o rețea de unde portalul se vede.

Fără index, celelalte tool-uri merg normal — doar cele două spun că lipsește.

```bash
python3 anaf_mcp.py --cauta dante international
python3 anaf_mcp.py --top 4754
```

Fluxul firesc: cauți după nume → iei CUI-ul → îl dai la `anaf_firma` pentru datele fiscale **la zi**, direct de la ANAF. Indexul e o fotografie lunară; ANAF e sursa live.

În deployment-ul remote, un index compact este inclus automat. Nu se rulează `--sync` pe calculatorul
utilizatorului. Varianta compactă păstrează CUI-ul și numele normalizat pentru căutare; `anaf_firma`
întoarce apoi denumirea oficială și starea fiscală live.

## 6. Verificare din terminal, fără client

Fiecare tool are un echivalent CLI, util ca să vezi dacă problema e la server sau la client:

```bash
python3 anaf_mcp.py --firma RO14399840
python3 anaf_mcp.py --bilant 14399840 2024
python3 anaf_mcp.py --curs 2026-06-30
python3 anaf_mcp.py --caen instalatii electrice
python3 anaf_mcp.py --cauta dante international
python3 anaf_mcp.py --test
```

## 7. Dacă nu merge

| Simptom | Cauză uzuală |
|---|---|
| serverul nu apare / „failed to connect" | calea din config nu e absolută, sau `python3` nu e în PATH-ul clientului — pune calea completă, ex. `/usr/bin/python3` |
| merge în terminal, nu în client | ai editat alt fișier de config, sau nu ai repornit clientul |
| „HTTP 403" de la ANAF | ai modificat `USER_AGENT`; WAF-ul ANAF respinge șirurile cu paranteze sau `;` |
| date vechi | e cache-ul: `rm -rf ~/.cache/anaf-mcp` |
| Claude cere OAuth Client ID | ai introdus pagina repo-ului în locul URL-ului remote terminat în `/mcp` |

---

## Deploy remote manual

Deployment-ul Vercel folosește `api/index.py`, `remote_mcp.py` și `vercel.json`. Pentru un preview:

```bash
vercel deploy -y
```

La actualizarea lunară a datelor, publică ambele indexuri în release-ul `latest`:

```bash
python3 tools/build_index.py --iesire anaf-index.sqlite --gzip
python3 tools/build_remote_index.py \
  --sursa anaf-index.sqlite \
  --iesire anaf-remote-index.sqlite.xz
gh release upload latest anaf-index.sqlite.gz anaf-remote-index.sqlite.xz --clobber
```

Serverul HTTP poate fi verificat și local, înainte de deploy:

```bash
python3 remote_mcp.py --port 8765
# endpoint: http://127.0.0.1:8765/mcp
```

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

**Contracte și achiziții publice (SEAP/SICAP).** API-ul public de la `e-licitatie.ro` răspunde, dar filtrele după firmă, dată sau cod CPV sunt ignorate de server — întoarce mereu aceeași felie fixă de 2000 de înregistrări. Fără contractul corect de filtrare, orice tool construit peste el ar da rezultate greșite, așa că nu există aici.

**Asociați, administratori, istoric mențiuni.** ONRC le dă doar prin RECOM, contra cost. (Lista reprezentanților legali există totuși ca fișier deschis pe data.gov.ro, dacă vrei să o indexezi similar.)

## Licență

MIT — vezi [LICENSE](LICENSE). Datele aparțin ANAF și BNR; serverul doar le citește din endpoint-urile publice.
