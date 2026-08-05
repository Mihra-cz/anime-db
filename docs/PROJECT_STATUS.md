# AnimeDB – stav projektu a roadmapa

> Tento dokument je hlavní checkpoint projektu. Slouží pro pokračování v novém chatu, předání kontextu Codexu a kontrolu, že vývoj neuhýbá od cíle.
>
> **Aktualizováno:** 5. srpna 2026  
> **Aktuální checkpoint:** konec V4, příprava V5  
> **Repozitář:** `git@github.com:Mihra-cz/anime-db.git`  
> **Projekt:** `~/Projekty/anime-db`

---

## 1. Hlavní cíl projektu

AnimeDB je specializovaný katalog a správce anime knihovny uložené na NASu.

Nemá znovu implementovat celý Jellyfin. Jeho hlavní přínos je v oblastech, které obecné mediální servery řeší jen částečně:

- přesné seskupení titulů, sezón, epizod, filmů, OVA a speciálů,
- evidence českých a slovenských titulků,
- ruční potvrzení hardsubu,
- kontrola úplnosti knihovny,
- rozpoznání chybějících dílů,
- správa různých verzí a duplicit,
- bezpečný import neuspořádaných souborů,
- pozdější propojení s Jellyfinem a Shoko.

Cílové rozdělení odpovědností:

```text
Anime soubory na NASu
        │
        ├── AnimeDB
        │   ├── sken souborů
        │   ├── struktura titul → sezóna → epizoda → soubor
        │   ├── CZ/SK titulky a hardsuby
        │   ├── úplnost knihovny
        │   ├── import a deduplikace
        │   ├── metadata a obaly
        │   └── vlastní webový katalog
        │
        ├── Shoko Server
        │   ├── identifikace anime a souborů
        │   ├── vazby na AniDB
        │   └── anime metadata a relace
        │
        └── Jellyfin
            ├── přehrávání
            ├── transkódování
            ├── TV a mobilní aplikace
            └── historie sledování
```

---

## 2. Prostředí

### Vývojový notebook

- Fujitsu LIFEBOOK E558
- Linux Mint 22.3
- uživatel: `<linux-user>`
- Python 3.12.3
- Git
- Docker + Docker Compose
- VS Code
- Codex CLI
- ffprobe / FFmpeg
- MediaInfo
- SQLite

### Projekt

```text
~/Projekty/anime-db
```

### NAS

- IP: `<NAS_IP>`
- SMB sdílení: `Anime`
- mount: `/mnt/nas-anime`
- účet: `<read-only-user>`
- přístup určený pouze pro čtení
- credentials: `<SMB-credentials-file>`

Konfigurace `.env`:

```ini
ANIME_PATH=/mnt/nas-anime
DATABASE_URL=sqlite:///./data/anime.db
REQUIRE_MOUNT=true
```

### Databáze

- SQLite
- soubor: `data/anime.db`
- databáze se neukládá do Gitu
- aktuálně evidováno 3 098 videí

---

## 3. Technologie aplikace

- FastAPI
- SQLAlchemy
- SQLite
- Jinja2
- Python dotenv konfigurace
- ffprobe
- MediaInfo
- Dockerfile
- Docker Compose
- pytest

Základní struktura obsahuje zejména:

```text
app/
├── config.py
├── database.py
├── main.py
├── models.py
├── probe.py
├── subtitles.py
├── scanner/
├── templates/
└── static/

tests/
data/
docs/
compose.yaml
Dockerfile
pyproject.toml
README.md
.env.example
```

---

# 4. Verze projektu

## V1 – Databáze a bezpečný sken ✅

Dokončeno:

- sken videosouborů z NASu,
- analýza pomocí ffprobe,
- evidence audio stop,
- evidence interních titulků,
- párování externích titulků,
- opakovaný sken bez duplicit,
- aktualizace změněných souborů,
- SQLite databáze,
- základní webové rozhraní,
- načítání konfigurace z `.env`,
- Docker a Docker Compose.

### Bezpečnost skenu

Implementováno:

- `REQUIRE_MOUNT`,
- ověření, že `ANIME_PATH` leží na připojeném filesystemu,
- prázdný sken nesmí vymazat neprázdnou databázi,
- odstranění více než 20 % záznamů vyžaduje explicitní potvrzení,
- nulový sken nelze potvrzením obejít,
- mazání probíhá až po úspěšném dokončení průchodu,
- při chybě nebo bezpečnostním přerušení proběhne rollback,
- chyby průchodu adresářem se netiší,
- web zobrazuje upozornění na možné odpojení knihovny.

Ověřený test:

1. knihovna měla 3 098 záznamů,
2. NAS byl ručně odpojen,
3. mountpoint zůstal jako prázdný lokální adresář,
4. sken byl bezpečnostně odmítnut,
5. databáze zůstala na 3 098 záznamech.

---

## V2 – Strukturovaný katalog ✅

Dokončeno:

- seskupení podle skutečného titulu,
- slučování více sezón jednoho anime,
- rozpoznání technických složek:
  - `Serie 1`,
  - `Série 2`,
  - `Season 01`,
  - `S01`,
  - `Cour 2`,
  - `Part 1`,
  - `Specials`,
  - `OVA`,
- odvozené označení sezóny, například `S1`, `S2`,
- bezpečné rozpoznání čísla epizody,
- klasifikace videí:
  - běžná epizoda,
  - film,
  - OVA,
  - Special,
  - NCOP,
  - NCED,
  - PV,
  - CM,
  - Menu,
  - Other,
- detail titulu se sezónami a jednotlivými videosoubory,
- relativní cesta každého videa zůstává dostupná.

Všechny hlavní přehledy nejprve ukazují tituly, nikoli dlouhý seznam jednotlivých epizod.

---

## V3 – Překlady a ruční validace ✅

Dokončeno:

- normalizace češtiny:
  - `cs`,
  - `cze`,
  - `ces`,
- normalizace slovenštiny:
  - `sk`,
  - `slk`,
  - `slo`,
- rozpoznání angličtiny z názvu stopy při chybějícím jazykovém tagu,
- CZ/SK se počítá z interních i externích titulků,
- příznaky `has_cs` a `has_sk` jsou nezávislé,
- statistiky:
  - pouze CZ,
  - pouze SK,
  - CZ i SK,
  - bez CZ/SK,
  - neznámé titulky,
- ruční příznaky:
  - `manual_hardsub_cs`,
  - `manual_hardsub_sk`,
  - `manual_hardsub_verified_at`,
- ruční volby:
  - bez hardsubu,
  - hardsub CZ,
  - hardsub SK,
  - hardsub CZ i SK,
- štítek **Ověřeno přehráním**,
- datum ručního ověření,
- ruční údaje skener nepřepisuje,
- zrušení ručního označení odstraní datum ověření,
- ruční hardsub se započítává do výsledného CZ/SK stavu,
- neovlivňuje samostatnou evidenci neznámých titulků.

---

## V4 – Použitelné webové rozhraní ✅

Dokončeno:

### Filtry

- pouze CZ,
- pouze SK,
- CZ i SK,
- bez CZ/SK,
- neznámé titulky,
- běžné epizody,
- bonusová a ostatní videa,
- OVA,
- Specials,
- NCOP,
- NCED,
- PV,
- CM,
- Menu,
- Other,
- přehledy kořenových složek.

Každý filtr nejprve zobrazuje seskupené tituly.

### Přehled titulu

Obsahuje:

- titul,
- cestu titulu,
- celkový počet videí,
- počet epizod,
- počet bonusů,
- CZ,
- SK,
- bez CZ/SK,
- neznámé titulky,
- zvýrazněný počet odpovídající aktivnímu filtru.

### Detail titulu

Obsahuje:

- název souboru,
- sezónu,
- původní sezónní složku v tooltipu,
- číslo epizody,
- typ videa,
- rozlišení,
- audio,
- automaticky nalezené CZ,
- automaticky nalezené SK,
- neznámé titulky,
- ruční hardsub,
- štítek Ověřeno přehráním,
- datum ověření,
- úplnou relativní cestu.

### Hledání

Společné vyhledávání funguje na hlavní stránce, ve filtrech i v detailu titulu.

Prohledává bez ohledu na velikost písmen:

- název titulu,
- cestu titulu,
- sezónní složku,
- označení sezóny,
- název souboru,
- relativní cestu,
- typ videa,
- číslo epizody.

Chování relevance:

1. přesná shoda titulu,
2. titul začínající hledaným textem,
3. text obsažený v titulu,
4. shoda pouze ve videu, sezóně nebo cestě.

### Řazení

- klikací hlavičky,
- vzestupně / sestupně,
- aktivní směr označen `▲` nebo `▼`,
- přirozené řazení:
  - `Anime 2` před `Anime 10`,
  - `S2` před `S10`,
- stabilní výsledky i pro hledání jednoho písmene,
- filtrování, hledání a řazení se ukládá do URL,
- stav se zachová při otevření detailu i po změně hardsubu,
- návrat vede ke stejnému videu.

### Poslední ověřený stav testů

```text
56 passed
```

Dále prošlo:

```text
python -m compileall app tests
git diff --check
docker compose config
```

---

# 5. Aktuální statistiky knihovny

Poslední ověřené hodnoty před dalšími ručními úpravami:

| Kategorie | Počet |
|---|---:|
| Všechna videa | 3 098 |
| Běžné epizody | 2 948 |
| Bonusová / ostatní videa | 150 |
| Pouze CZ | 2 111 |
| Pouze SK | 282 |
| CZ i SK | 0 |
| Bez CZ/SK | 705 |
| S neznámými titulky | 159 |

Poznámka: čísla CZ/SK se mohou postupně měnit ručním označováním hardsubů.

---

# 6. V5 – Metadata, obaly a externí databáze ⏳

## Cíl checkpointu V5

Převést technicky seskupený katalog na skutečnou anime knihovnu s ověřenou identitou titulů a externími metadaty.

V5 nesmí měnit ani přesouvat videosoubory. Pracuje pouze s databází, identitou titulu, metadaty, obrázky a ručním potvrzením.

## Hlavní princip

Lokální název složky je primární pracovní identita knihovny.

Externí databáze nesmí automaticky přepsat:

- lokální název titulu,
- strukturu složek,
- ruční údaje,
- stav hardsubu,
- ruční párování.

Externí metadata se ukládají odděleně a propojují se přes explicitní vazby.

## Externí zdroje

### AniList – první hlavní poskytovatel

Použití:

- vyhledání kandidátů,
- romaji, anglický a původní název,
- alternativní názvy,
- popis,
- obaly a bannery,
- rok a sezóna vysílání,
- formát,
- stav,
- počet epizod a délka,
- žánry a tagy,
- vztahy mezi anime.

AniList má veřejné GraphQL API. Dotazy se posílají pomocí HTTP POST a klient si vybírá požadovaná pole.

Dokumentace:

- https://docs.anilist.co/
- https://docs.anilist.co/guide/graphql/
- endpoint: `https://graphql.anilist.co`

### MyAnimeList – druhý nezávislý zdroj

Zamýšlené použití:

- další externí ID,
- alternativní kontrola identity titulu,
- další názvy a základní metadata,
- budoucí propojení se seznamem uživatele.

Integrace se nesmí stavět natvrdo do katalogového modelu. Musí používat samostatný provider/adaptér a konfigurovatelné přihlašovací údaje.

Před implementací ověřit aktuální oficiální podmínky, autentizaci, limity a povolené ukládání dat.

### Shoko / AniDB – zdroj zaměřený na soubory a anime strukturu

Shoko má být důležitý hlavně pro:

- identifikaci anime podle informací o souboru,
- vazby na AniDB,
- přesnější anime relace,
- kontrolu epizod,
- budoucí propojení s Jellyfinem.

Shoko Server poskytuje vlastní API. Po spuštění serveru je jeho aktuální rozhraní dostupné přes Swagger na:

```text
http://<shoko-server>:8111/swagger/
```

Dokumentace:

- https://docs.shokoanime.com/
- https://docs.shokoanime.com/faq

Shoko používá AniDB jako důležitý zdroj identity a může doplňovat metadata z dalších zdrojů.

### Crunchyroll – dostupnost streamování, nikoli primární metadata

Crunchyroll nesmí být považován za spolehlivou hlavní identifikační databázi, dokud nebude ověřeno podporované oficiální API a jeho podmínky.

Možné budoucí použití:

- ručně spravovaný odkaz na titul,
- evidence dostupnosti legálního streamu,
- země / region,
- jazyk titulků a dabingu,
- datum posledního ověření odkazu.

Nepoužívat neoficiální scraping nebo neveřejné endpointy jako základ V5.

### Další budoucí zdroje

Architektura musí umožnit přidat další adaptéry bez změny hlavního katalogového modelu, například:

- TMDB,
- AniDB přímo,
- Anime-Planet,
- Kitsu,
- TVDB,
- lokální NFO,
- Jellyfin,
- Shoko.

---

## 6.1 Datový model V5

Doporučené nové entity:

### `CatalogTitle`

Stabilní interní titul AnimeDB.

Možná pole:

```text
id
local_title
normalized_local_title
relative_root_path
manual_display_title
preferred_metadata_provider
preferred_external_id
metadata_status
metadata_locked
created_at
updated_at
```

`CatalogTitle` nesmí být závislý na jednom externím providerovi.

### `ExternalTitleLink`

Vazba interního titulu na externí databázi.

```text
id
catalog_title_id
provider
external_id
external_url
match_method
match_score
is_primary
is_manual
verified_at
created_at
updated_at
```

Příklady `provider`:

```text
anilist
myanimelist
anidb
shoko
tmdb
crunchyroll
```

Příklady `match_method`:

```text
automatic_exact
automatic_normalized
automatic_alias
manual_search
manual_id
imported_from_shoko
```

### `TitleMetadata`

Normalizovaná metadata používaná webem.

```text
catalog_title_id
display_title
title_romaji
title_english
title_native
description
release_year
season
format
status
episode_count
episode_duration_minutes
genres_json
tags_json
synonyms_json
country_of_origin
is_adult
metadata_provider
metadata_external_id
metadata_fetched_at
metadata_updated_at
```

### `MetadataCandidate`

Kandidáti před ručním potvrzením.

```text
id
catalog_title_id
provider
external_id
candidate_title
candidate_year
candidate_format
candidate_episode_count
match_score
match_reasons_json
raw_payload_json
created_at
rejected_at
confirmed_at
```

### `Artwork`

```text
id
catalog_title_id
provider
external_id
artwork_type
remote_url
local_path
width
height
language
is_primary
fetched_at
```

Typy:

```text
cover
banner
background
logo
thumbnail
```

### `MetadataSyncLog`

```text
id
catalog_title_id
provider
operation
status
message
started_at
finished_at
request_count
```

---

## 6.2 Provider architektura

Vytvořit společné rozhraní, například:

```python
class MetadataProvider(Protocol):
    name: str

    def search_titles(self, query: str) -> list[MetadataCandidate]:
        ...

    def fetch_title(self, external_id: str) -> ProviderTitleMetadata:
        ...

    def fetch_relations(self, external_id: str) -> list[ProviderRelation]:
        ...

    def fetch_artwork(self, external_id: str) -> list[ProviderArtwork]:
        ...
```

Konkrétní adaptéry:

```text
app/metadata/providers/base.py
app/metadata/providers/anilist.py
app/metadata/providers/myanimelist.py
app/metadata/providers/shoko.py
app/metadata/providers/crunchyroll.py
```

Crunchyroll adaptér může být zpočátku pouze ruční nebo vypnutý, dokud nebude ověřené podporované rozhraní.

Provider nesmí zapisovat přímo do tabulek videí. Výsledek se nejprve převede do interního normalizovaného datového modelu.

---

## 6.3 Párování titulů

### Automatická příprava kandidátů

Pro každý nepřiřazený titul:

1. vezmi lokální název,
2. odstraň interní technické suffixy pouze pro hledání,
3. zachovej původní název beze změny,
4. vyhledej kandidáty u aktivních providerů,
5. spočítej skóre,
6. zobraz kandidáty uživateli,
7. nic automaticky nepotvrzuj pod stanoveným prahem.

Příklady interních suffixů, které lze při hledání normalizovat:

```text
(J19)
(Z15-Z16)
(L15-L22)
cz
sk
cz-xx%
1080p
BD
WEB
```

Odstraňování suffixů musí být opatrné, testované a nesmí měnit uložený lokální titul.

### Skóre kandidáta

Možné body:

- přesná normalizovaná shoda názvu,
- shoda alternativního názvu,
- odpovídající rok,
- odpovídající formát,
- podobný počet epizod,
- shoda sezóny vysílání,
- shoda více providerů.

Skóre musí uchovávat vysvětlení, například:

```json
{
  "title_exact": true,
  "alias_match": false,
  "year_match": true,
  "episode_count_delta": 1,
  "format_match": true
}
```

### Ruční potvrzení

Uživatel musí mít možnosti:

- potvrdit kandidáta,
- vyhledat jiný titul,
- zadat externí ID ručně,
- označit titul jako lokální / bez externího záznamu,
- vazbu později změnit,
- zamknout metadata proti automatickému přepsání.

---

## 6.4 Web V5

### Domovská stránka

Postupně přidat:

- obal titulu,
- preferovaný zobrazovaný název,
- rok,
- formát,
- počet evidovaných videí,
- oficiální počet epizod,
- stav spárování metadat.

### Nové filtry

```text
Bez metadat
Čeká na potvrzení
Spárováno automaticky
Spárováno ručně
Konflikt providerů
Chybí obal
Chybí oficiální počet epizod
Metadata zastaralá
```

### Detail titulu

Přidat:

- obal,
- romaji / anglický / původní název,
- popis,
- rok,
- sezónu,
- formát,
- stav,
- oficiální počet epizod,
- délku epizody,
- žánry,
- tagy,
- alternativní názvy,
- externí odkazy,
- zdroj každého hlavního údaje,
- datum poslední aktualizace,
- tlačítko „Vyhledat metadata“,
- seznam kandidátů,
- ruční potvrzení,
- zámek metadat.

### Hromadná operace

- „Najít kandidáty pro všechny tituly bez metadat“
- pouze připraví kandidáty,
- nic automaticky nepřepíše,
- respektuje limity API,
- zobrazuje průběh a chyby,
- lze bezpečně zopakovat.

---

## 6.5 Obrázky a cache

Externí obrázky nepoužívat jen jako vzdálené hotlinky.

Doporučení:

- metadata URL uložit,
- obrázek stáhnout do lokální cache,
- ověřit MIME typ,
- nastavit maximální velikost,
- vytvořit menší náhled,
- ukládat pod stabilním názvem podle provideru a externího ID,
- při chybě ponechat předchozí obrázek,
- nedávat binární cache do Gitu.

Možná cesta:

```text
data/artwork/<provider>/<external_id>/
```

---

## 6.6 Konfigurace V5

Příklad `.env.example`:

```ini
METADATA_ENABLED=true
METADATA_PRIMARY_PROVIDER=anilist
METADATA_REQUEST_TIMEOUT_SECONDS=15
METADATA_CACHE_TTL_HOURS=168
METADATA_AUTO_CONFIRM=false
METADATA_AUTO_CONFIRM_THRESHOLD=0.95
METADATA_DOWNLOAD_ARTWORK=true

ANILIST_ENABLED=true
MYANIMELIST_ENABLED=false
SHOKO_ENABLED=false
SHOKO_BASE_URL=http://127.0.0.1:8111
CRUNCHYROLL_LINKS_ENABLED=false
```

Tajné klíče a tokeny nikdy neukládat do Gitu.

---

## 6.7 Bezpečnost a provozní pravidla V5

- síťová chyba nesmí poškodit katalog,
- selhání provideru nesmí blokovat běžné prohlížení knihovny,
- používat timeout,
- používat retry s omezením,
- respektovat rate limit,
- cacheovat odpovědi,
- neukládat tajné tokeny do logu,
- nepřepisovat ruční údaje,
- nepřepisovat zamknutá metadata,
- zachovat raw odpověď provideru pouze tam, kde je to licenčně a provozně vhodné,
- data z jednotlivých providerů držet odděleně,
- zobrazovat zdroj metadat,
- při konfliktu nic automaticky nemačkat dohromady.

---

## 6.8 Testy V5

Minimální sada:

- vytvoření stabilního interního titulu,
- více externích vazeb na jeden titul,
- jeden externí záznam nesmí být omylem primární pro dva různé lokální tituly bez upozornění,
- AniList provider používá parametrizovaný GraphQL dotaz,
- timeout a chyba API nepoškodí katalog,
- cache zabrání zbytečnému opakování dotazu,
- kandidáti se seřadí podle skóre,
- přesná shoda názvu má přednost,
- alternativní název lze použít,
- rok a počet epizod ovlivní skóre,
- ruční potvrzení přepíše automatický návrh,
- zamknutá metadata se neaktualizují,
- změna provideru nesmaže starou vazbu,
- hromadné hledání nevytváří duplicity kandidátů,
- obrázek se validuje a uloží do cache,
- neplatný obrázek se odmítne,
- migrace zachová stávajících 3 098 videí,
- stávající ruční hardsuby zůstanou zachované,
- všechny stávající filtry, hledání a řazení dál fungují.

Po každém větším kroku spustit:

```bash
pytest -v
python -m compileall app tests
git diff --check
docker compose config
```

---

## 6.9 Akceptační kritéria V5

V5 je dokončená, až když:

1. každý lokální titul má stabilní interní ID,
2. lze vyhledat kandidáty alespoň přes AniList,
3. uživatel může kandidáta ručně potvrdit nebo odmítnout,
4. vazba na externí titul je uložená odděleně,
5. detail zobrazuje obal a základní metadata,
6. je vidět zdroj a čas aktualizace,
7. automatická synchronizace nepřepisuje ruční nebo zamknutá data,
8. selhání internetu neovlivní stávající katalog,
9. migrace zachová všechna videa, titulky a hardsuby,
10. všechny dosavadní testy dál procházejí.

---

# 7. V6 – Úplnost knihovny ⏳

Po V5:

- porovnání lokálních epizod s oficiálním počtem,
- chybějící čísla epizod,
- nerozpoznané epizody,
- titulky bez videa,
- duplicity uvnitř knihovny,
- více verzí jedné epizody,
- procenta úplnosti,
- procenta CZ/SK překladu,
- konflikty mezi lokální strukturou a externí databází.

V6 musí stavět nad potvrzenou identitou titulů z V5.

---

# 8. V7 – Bezpečný import a deduplikace ⏳

V7 je klíčová pro úklid hlavního PC.

Musí zvládnout:

- archivy,
- `záloha 1`, `záloha 2`,
- bitové obnovy zhroucených HDD,
- opakovaně stažené soubory,
- stejné soubory pod jinými názvy,
- stejné epizody v jiné kvalitě,
- repacky a remuxy,
- poškozené nebo zkrácené soubory,
- samostatné titulky z jiné kopie.

Požadovaný tok:

```text
zdrojový chaos
→ inventura
→ hash a technická analýza
→ identifikace titulu / sezóny / epizody
→ skupiny duplicit
→ návrh nejlepší verze
→ návrh sloučení titulků
→ cílová struktura
→ náhled operací
→ ruční potvrzení
→ karanténa
→ kontrolovaný import
→ audit a možnost návratu
```

V7 nesmí nic mazat nebo přesouvat bez předchozího plánu a explicitního potvrzení.

---

# 9. V8 – Automatické sledování NASu ⏳

- watcher,
- detekce nového souboru,
- čekání na dokončení kopírování,
- ffprobe,
- titulky,
- přiřazení titulu a epizody,
- aktualizace databáze,
- metadata a obal,
- upozornění na změny,
- ochrana před částečně zkopírovanými soubory.

---

# 10. V9 – Jellyfin / Shoko integrace ⏳

- odkazy na přehrání v Jellyfinu,
- předání externích ID,
- případně generování `.nfo`,
- historie sledování,
- Shoko jako anime identifikační backend,
- Jellyfin jako přehrávací a transkódovací backend.

AnimeDB zůstává vlastní katalog a správce knihovny.

---

# 11. V10 – Vizuální anime knihovna ⏳

- obaly a dlaždice,
- responzivní vzhled,
- řádky:
  - nově přidáno,
  - bez překladu,
  - kompletní tituly,
  - chybějící epizody,
  - filmy,
  - OVA a Specials,
- detail podobný mediální knihovně,
- později stav sledování z Jellyfinu.

---

# 12. Co dělat jako další krok

Aktuálně nezačínat import V7 ani přehrávání.

Bezprostřední další krok:

1. vytvořit migraci pro `CatalogTitle`,
2. převést současné seskupování na stabilní interní titul,
3. vytvořit provider rozhraní,
4. implementovat první AniList adaptér,
5. vytvořit stránku kandidátů,
6. ručně spárovat několik zkušebních titulů,
7. teprve potom spustit kandidáty pro větší část knihovny.

První implementační iterace V5 má být malá:

```text
CatalogTitle
+ ExternalTitleLink
+ AniList search
+ ruční potvrzení kandidáta
+ základní obal a metadata v detailu
```

Nezačínat hromadným automatickým párováním celé knihovny.

---

# 13. Pravidla pro další chat nebo Codex

- Nejprve přečíst celý `docs/PROJECT_STATUS.md`.
- Držet se aktuální verze roadmapy.
- Nepřeskakovat V5 a V6 kvůli V7.
- Neimplementovat přehrávání od nuly.
- Zachovat read-only přístup ke zdrojové anime knihovně.
- Síťové metadata nesmí být podmínkou funkce lokálního katalogu.
- Automatické návrhy nesmí přepisovat ruční rozhodnutí.
- Každá migrace musí zachovat současná data.
- Po změnách vždy spustit úplnou sadu testů a kontrol.
- Před velkou změnou vytvořit Git commit.
- Databázi, `.env`, tokeny a cache obrázků neukládat do Gitu.

---

# 14. Ověřené externí informace k 5. srpnu 2026

- AniList poskytuje veřejné GraphQL API a požadavky se posílají jako HTTP POST na `https://graphql.anilist.co`.
- Shoko Server má veřejné API, jeho aktuální endpointy lze prohlédnout na `/swagger/` běžící instance.
- Shoko používá AniDB pro identifikaci anime a souborů.
- Shoko přímo nepřehrává obsah z webových služeb, jako je Crunchyroll.
- Crunchyroll proto v projektu zatím chápat jako volitelný externí odkaz nebo údaj o dostupnosti, nikoli jako potvrzený hlavní API provider.
- Před implementací MyAnimeList a Crunchyroll integrace znovu ověřit aktuální oficiální dokumentaci, podmínky a limity.

Oficiální zdroje:

- AniList API: https://docs.anilist.co/
- AniList GraphQL: https://docs.anilist.co/guide/graphql/
- Shoko Docs: https://docs.shokoanime.com/
- Shoko FAQ / API: https://docs.shokoanime.com/faq
