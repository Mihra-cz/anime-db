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

## Aktualizace databáze

Při startu aplikace proběhne idempotentní migrace SQLite: chybějící sloupce pro normalizovaný jazyk a typ videa se doplní a existující záznamy se přepočítají. Stávající raw metadata jazyka se zachovají. Před větší aktualizací lze pro jistotu zazálohovat `data/anime.db`; ruční smazání databáze není pro tuto verzi nutné.
