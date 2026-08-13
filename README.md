# AnimeDB

AnimeDB je jednoduchý read-only katalog anime knihovny. Rekurzivně najde MKV, MP4 a AVI, načte technická metadata přes `ffprobe`, spáruje externí titulky a zobrazí souhrn ve webovém rozhraní. Do adresáře knihovny nikdy nezapisuje.

## Spuštění bez Dockeru

Požadavky: Python 3.12 a `ffprobe` (součást FFmpeg) dostupný v `PATH`.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e '.[test]'
cp .env.example .env
mkdir -p data
export ANIME_PATH=/skutečná/cesta/k/anime
export DATABASE_URL=sqlite:///./data/anime.db
uvicorn app.main:app --reload --port 8000
```

Otevřete <http://localhost:8000>. Kontrola služby je na <http://localhost:8000/health>.

Soubor `.env` se načítá automaticky pomocí `python-dotenv`. Proměnné nastavené přímo v systémovém prostředí mají před hodnotami z `.env` přednost. Výchozí hodnoty jsou `/media/anime` a `sqlite:///./data/anime.db`.

Pro NAS je doporučeno nastavit `REQUIRE_MOUNT=true`. Sken pak začne pouze tehdy, pokud `ANIME_PATH` leží na samostatně připojeném filesystemu. I bez této volby skener odmítne prázdný výsledek proti neprázdné databázi a odstranění více než 20 % indexu vyžaduje explicitní potvrzení ve webu.

### CIFS a výpadky NASu

CIFS mount nastavte podle vlastností konkrétního NASu a provozních požadavků hostitele; aplikace automaticky nevynucuje žádné agresivní nebo potenciálně nebezpečné mount volby. Filesystemová operace může při výpadku serveru čekat uvnitř kernelového CIFS klienta, proto AnimeDB navíc používá vlastní průběžný healthcheck v odděleném procesu. Výchozí kontrola proběhne každých 25 videí s timeoutem 10 sekund. Při selhání se celý aktuální sken vrátí zpět a databázové záznamy se nemažou.

Související konfigurace:

```ini
FFPROBE_TIMEOUT_SECONDS=60
MEDIAINFO_TIMEOUT_SECONDS=60
LIBRARY_ACCESS_TIMEOUT_SECONDS=10
LIBRARY_HEALTHCHECK_INTERVAL_FILES=25
METADATA_REQUEST_TIMEOUT_SECONDS=15
```

HTTP metadata používají rozdělený timeout pro connect/read/write/pool. `ffprobe` a případné budoucí použití MediaInfo mají vlastní timeout na každý proces.

## Docker Compose

Compose připojuje hostitelský `/mnt/nas-anime` pouze pro čtení jako `/media/anime` a databázi ukládá do `./data`:

```bash
mkdir -p data
docker compose up --build
```

Pokud je knihovna na hostiteli jinde, změňte levou stranu volume v `compose.yaml`. Aplikace poběží na <http://localhost:8000>.

## Testy

```bash
pytest
```

## Chování skenu

- Přeskakuje `#recycle`, `@eaDir` a adresáře začínající tečkou.
- Externí SRT/ASS/SSA/VTT páruje podle shodného názvu (`ep01.srt`) i běžné jazykové přípony (`ep01.cs.srt`).
- Češtinu a slovenštinu odhaduje lokální, transparentní heuristikou typických slov a znaků; neurčité texty označí `unknown`.
- Relativní cesta je jedinečná. Nezměněné video znovu nevolá `ffprobe`; velikost nebo čas změny vyvolá aktualizaci. Externí titulky se obnovují při každém skenu.
- Selhání jednoho souboru se zaloguje a sken pokračuje. Smazané či přesunuté soubory se odstraní pouze z databázového indexu.
- Překročení timeoutu `ffprobe` označí pouze daný soubor jako chybu; jeho předchozí databázový záznam zůstane zachovaný.
- Dostupnost knihovny se kontroluje před skenem, průběžně a znovu před mazací fází. Výpadek vyvolá rollback celého skenu.
- Logická hierarchie používá `CatalogCollection -> CatalogTitle -> Video`: collection je hlavní anime, title je season, film, OVA nebo doplňková část.
- `Season 1`, `Season 2`, `OVA`, `Specials`, `NC`, `OP`, `ED`, `Movies`, `Bonus` a podobné child složky pod zjevným anime rootem nevytvářejí samostatné hlavní collections.
- Hierarchy Review umí bez přesunu souborů vytvořit hlavní collection, přesunout do ní celé CatalogTitle a později rozhodnutí změnit. Ruční assignment má před scannerem přednost.
- Nejasná příbuznost collections se pouze navrhne ke kontrole. Volba „Ponechat samostatně“ je persistentní pro konkrétní stav návrhu.
- Collection merge neřeší fyzické duplicity; ty zůstávají ve vlastním duplicate workflow.
- Explicitní filename suffixy `OVA 01`, `Special 01`, `OP 02`, `ED 02`, `NCOP 01` a `NCED 01` mají přednost před obecným číslem epizody. Jejich pořadí se zobrazuje jako supplementary sequence a nevstupuje do standardní season completeness.
- Duplicate identita doplňků zahrnuje subtype i bezpečně známý season/name context. Hierarchy Review umí jednotlivé video bez změny cesty přesunout do nové nebo existující OVA/Special/NC části.
- Prázdný CatalogTitle lze po explicitním potvrzení odstranit spolu s jeho vlastněnými DB metadata záznamy; CatalogCollection se tím nikdy nemaže automaticky.
- Prázdné CatalogCollection lze odstranit jednotlivě nebo hromadně. Server jejich prázdnost ověřuje z databáze znovu a neprázdné položky bezpečně přeskočí.

## Aktualizace databáze

Při startu aplikace proběhne idempotentní migrace SQLite: chybějící sloupce pro normalizovaný jazyk a typ videa se doplní a existující záznamy se přepočítají. Stávající raw metadata jazyka se zachovají. Před větší aktualizací lze pro jistotu zazálohovat `data/anime.db`; ruční smazání databáze není pro tuto verzi nutné.
