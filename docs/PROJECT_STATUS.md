# AnimeDB – stav projektu a roadmapa

> Tento dokument je hlavní checkpoint projektu. Slouží pro pokračování v novém chatu, předání kontextu Codexu a kontrolu, že vývoj neuhýbá od cíle.
>
> **Aktualizováno:** 6. srpna 2026
> **Aktuální checkpoint:** výrazně pokročilá V5 – stabilní hierarchie, perzistentní kandidáti a lokální obaly
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
├── hierarchy.py
├── hierarchy_rebuild.py
├── hierarchy_review.py
├── main.py
├── metadata/
├── models.py
├── numbering.py
├── probe.py
├── subtitles.py
├── tools/
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
V5 toto původní seskupení zpřesnila na nadřazené kolekce a konkrétní části.

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

Původní V4 funkce zůstávají zachované i po databázových změnách V5. Aktuální
automatické ověření je uvedeno v části V5.

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

## Produkční ověření migrace V5

- migrace produkční SQLite databáze proběhla úspěšně,
- zůstalo zachováno všech **3 098 videí**,
- prošla produkční kontrola katalogu, hierarchie, metadat, titulků a ručních hardsubů,
- `rebuild_hierarchy --dry-run` nenavrhl žádné další bezpečné automatické změny,
- nejasné historické kolekce se nerozdělují odhadem a řeší se ručně přes `/hierarchy-review`,
- nebyly měněny, přesouvány ani přejmenovány žádné videosoubory nebo adresáře.

---

# 6. V5 – Stabilní hierarchie, metadata a číslování 🚧

## Cíl checkpointu V5

Převést technicky seskupený katalog na skutečnou anime knihovnu s ověřenou
identitou kolekcí, konkrétních částí a externích metadat.

V5 nesmí měnit ani přesouvat videosoubory. Pracuje pouze s databází, identitou
titulu, metadaty, vzdálenými náhledy obrázků a ručním potvrzením.

V5 je výrazně pokročilá a produkčně nasazená, ale ještě není úplně uzavřená.
Není implementováno automatické párování, další metadata providery ani úplná
kontrola chybějících epizod. Bezpečné dávkové hledání pouze ukládá kandidáty a
nikdy je samo nepotvrzuje.

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
- obalový náhled,
- rok a sezóna vysílání,
- formát,
- stav,
- počet epizod a délka,
- žánry a tagy,
- budoucí vztahy mezi anime.

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

## 6.1 Implementovaná stabilní hierarchie

Aktuální interní model je:

```text
CatalogCollection
└── CatalogTitle
    └── Video
```

### `CatalogCollection`

Nadřazené lokální seskupení zobrazované na hlavní stránce. Představuje například
celé anime uložené v jedné kořenové složce. Nenese externí metadata konkrétní
sezóny.

Implementovaná pole kontroly hierarchie:

```text
hierarchy_status
hierarchy_verified_at
hierarchy_note
local_period_hint
```

Povolené stavy:

```text
automatic
review_required
verified
conflict
not_applicable
```

Interní poznámky jako `J19`, `Z18-L20` nebo `L15-L22` se mohou uložit do
`local_period_hint`. Neurčují automaticky počet sezón, částí ani epizod a původní
`local_title` a cesta zůstávají beze změny.

### `CatalogTitle`

Konkrétní metadata jednotka uvnitř kolekce: jedna sezóna, část, film, OVA,
Specials nebo samostatné anime bez rozpoznané podstruktury. Každý `CatalogTitle`
může mít vlastní `ExternalTitleLink` a `TitleMetadata`.

Vedle stabilní lokální identity a metadata polí jsou implementována zejména:

```text
season_number_manual
season_label_manual
part_type_manual
sort_order_manual
hierarchy_verified_at
part_number
episode_start
episode_end
episode_start_offset
episode_filename_pattern
numbering_mode
```

Ruční sezóna, typ, pořadí a pravidla přiřazení mají přednost před automatickou
detekcí. Sken ani opravný nástroj ručně ověřenou hierarchii nepřepisují.

### `Video`

Video je primárně přiřazené ke konkrétnímu `CatalogTitle` a současně si uchovává
přímou vazbu na kolekci:

```text
catalog_collection_id
catalog_title_id
local_episode_number
season_episode_number
absolute_episode_number
external_episode_number
episode_number_source
episode_number_confidence
episode_number_manual_override
episode_number_verified_at
```

`catalog_title_id` může být `NULL`, pokud video nelze bezpečně přiřadit. Takové
video zůstává evidované v `CatalogCollection` a je viditelné ve filtru
**Nezařazená videa**. Ruční override číslování má vždy přednost a sken jej
nepřepisuje.

### `ExternalTitleLink`

Implementovaná vazba konkrétního `CatalogTitle` na externí databázi.

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
cover_image_url
metadata_fetched_at
metadata_updated_at
```

### Implementované doplňkové entity

Kandidáti a lokální obaly jsou od 6. srpna 2026 perzistentní:

#### `MetadataCandidate`

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

#### `Artwork`

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

#### `MetadataSyncLog`

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

Je implementované společné provider rozhraní v `app/metadata/providers/base.py`
a první adaptér v `app/metadata/providers/anilist.py`:

```python
class MetadataProvider(Protocol):
    name: str

    def search_titles(self, query: str) -> list[ProviderTitleMetadata]:
        ...

    def fetch_title(self, external_id: str) -> ProviderTitleMetadata:
        ...

    def fetch_relations(self, external_id: str) -> list[ProviderRelation]:
        ...

    def fetch_artwork(self, external_id: str) -> list[ProviderArtwork]:
        ...
```

Stav adaptérů:

```text
base.py             implementováno
anilist.py          implementováno: search_titles a fetch_title
myanimelist.py      neimplementováno
shoko.py            neimplementováno
crunchyroll.py      neimplementováno
```

`fetch_relations` a `fetch_artwork` jsou součástí rozhraní, ale v AniList adaptéru
zatím nejsou dokončené. Crunchyroll adaptér může být zpočátku pouze ruční nebo
vypnutý, dokud nebude ověřené podporované rozhraní.

Provider nesmí zapisovat přímo do tabulek videí. Výsledek se nejprve převede do interního normalizovaného datového modelu.

---

## 6.3 Párování titulů

### Implementované ruční vyhledání a potvrzení

Pro konkrétní `CatalogTitle` lze:

1. předvyplnit bezpečně očištěný vyhledávací dotaz,
2. dotaz před odesláním ručně upravit,
3. zobrazit nejvýše deset kandidátů z AniListu,
4. kandidáta ručně potvrdit,
5. uložit nebo aktualizovat `ExternalTitleLink` a `TitleMetadata`,
6. primární vazbu změnit nebo odpojit,
7. metadata ručně aktualizovat nebo zamknout,
8. nastavit ruční zobrazovaný název bez změny lokálního názvu.

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

Odstraňování suffixů je opatrné, testované a používá se pouze pro výchozí
vyhledávací dotaz. Nemění uložený lokální titul, cestu ani názvy souborů.

### Budoucí automatické skóre kandidáta

Možné body:

- přesná normalizovaná shoda názvu,
- shoda alternativního názvu,
- odpovídající rok,
- odpovídající formát,
- podobný počet epizod,
- shoda sezóny vysílání,
- shoda více providerů.

Ruční i dávkové hledání ukládá transparentní pomocné skóre a jeho vysvětlení,
například:

```json
{
  "title_exact": true,
  "alias_match": false,
  "year_match": true,
  "episode_count_delta": 1,
  "format_match": true
}
```

Potvrzení kandidáta vždy znovu načte data ze serveru a zapisuje je v databázové
transakci. Stejné AniList ID primárně použité jiným lokálním titulem vyžaduje
explicitní potvrzení. Síťová, HTTP nebo GraphQL chyba provede rollback a lokální
katalog zůstává funkční.

---

## 6.4 Web V5

### Domovská stránka

Hlavní katalog nejprve zobrazuje `CatalogCollection` a agreguje statistiky všech
jejích částí. Detail kolekce zobrazuje seznam `CatalogTitle`; teprve detail
konkrétní části zobrazuje videa a její externí metadata. Navigace používá stabilní
ID:

```text
/collections/{collection_id}
/titles/{catalog_title_id}
```

### Nové filtry

```text
Nezařazená videa
Konflikt hierarchie
Hierarchie ke kontrole
```

### Detail titulu

Detail konkrétního `CatalogTitle` zobrazuje:

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
- seznam kandidátů z AniListu,
- ruční potvrzení,
- změnu a odpojení vazby,
- historii externích vazeb,
- zámek metadat,
- lokální, sezónní, absolutní a externí číslo epizody, pokud se liší.

Externí popis je převáděn na bezpečný prostý text. Vzdálený obal lze konfigurací
vypnout; lokální cache obrázků zatím není implementovaná.

### Ruční kontrola hierarchie

Stránka `/hierarchy-review` zobrazuje kolekce ve stavech `review_required` a
`conflict`. Umožňuje vytvářet virtuální části uvnitř jedné fyzické složky bez
změny videosouborů nebo adresářů.

Videa lze přiřadit podle:

- rozsahu lokálních čísel epizod,
- bezpečně omezeného `episode_filename_pattern`,
- seznamu jednotlivých `video_ids`.

Před zápisem se zobrazí náhled cílových částí, nezařazených videí a překryvů.
Překrývající se pravidla nelze aplikovat bez explicitního potvrzení; konfliktní
videa zůstávají bezpečně nezařazená. Po úplném potvrzení má kolekce stav
`verified` a datum ručního ověření.

Další sken zachová virtuální části i jejich pravidla. Pokud najde nové video mimo
ověřené rozsahy nebo pravidla, nepřiřadí je odhadem a vrátí kolekci do
`review_required` s důvodem **Nové nezařazené video**. AniList kandidáti na této
stránce slouží pouze jako pomůcka a sami kolekci nerozdělují.

### Číslování částí a epizod

Číslování rozlišuje hodnotu z názvu souboru, číslo v konkrétní části, absolutní
číslo v kolekci a číslo externího provideru.

Příklad pro `Part 2` uloženou jako lokální epizody 14–26 s offsetem 13:

```text
local_episode_number     14–26
season_episode_number     1–13
absolute_episode_number  14–26
external_episode_number   1–13
```

Externí číslo se pro samostatnou část odvozuje od sezónního, nikoli slepě od
absolutního čísla. Pokud offset nebo počet předchozích epizod není známý, absolutní
číslo se nehádá. Ruční režim, offset a override jednotlivého videa mají vždy
přednost a přežijí další sken.

### Opravný nástroj hierarchie

```bash
python -m app.tools.rebuild_hierarchy --dry-run
python -m app.tools.rebuild_hierarchy --apply
```

- `--dry-run` pouze vypíše navrhované změny a nic nezapisuje,
- `--apply` opravuje jen bezpečně jednoznačně rozpoznané existující části,
- nástroj nevytváří nové části,
- nepřesouvá externí vazby ani metadata,
- nepřepisuje ručně ověřenou hierarchii.

### Hromadná operace

Hromadné hledání kandidátů pro nezamknuté tituly bez metadat je implementované
se synchronním konfigurovatelným limitem a omezením mezi dotazy. Přeskakuje
konfliktní hierarchii, u hierarchie ke kontrole varuje a nic automaticky
nepotvrzuje ani nepřepisuje.

---

## 6.5 Obrázky a cache

Po ručním potvrzení se obal volitelně ukládá do lokální cache
`data/artwork/<provider>/<external_id>/`. Ověřuje se schéma URL, MIME typ a
velikost, zápis je atomický a vytváří se WebP náhled. Chyba obalu neruší uložení
metadat a předchozí úspěšný obrázek zůstává zachovaný.

Cílové doporučení pro dokončení V5:

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
METADATA_ALLOW_REMOTE_IMAGES=true
METADATA_CANDIDATE_LIMIT=10
METADATA_BATCH_SEARCH_LIMIT=10
METADATA_ARTWORK_MAX_BYTES=10485760
METADATA_ARTWORK_THUMBNAIL_WIDTH=400
FFPROBE_TIMEOUT_SECONDS=60
MEDIAINFO_TIMEOUT_SECONDS=60
LIBRARY_ACCESS_TIMEOUT_SECONDS=10
LIBRARY_HEALTHCHECK_INTERVAL_FILES=25

ANILIST_ENABLED=true
```

Nastavení dalších providerů se přidají až společně s jejich implementací.
`METADATA_CACHE_TTL_HOURS` a `METADATA_DOWNLOAD_ARTWORK` jsou připravené pro
budoucí cache; současná iterace cache ani stahování ještě nespouští.

Tajné klíče a tokeny nikdy neukládat do Gitu.

---

## 6.7 Bezpečnost a provozní pravidla V5

Implementované jsou timeouty, ošetření rate limitu a síťových/HTTP/GraphQL chyb,
transakční rollback a ochrana ručních či zamknutých dat. Následující pravidla
platí i pro zbývající iterace V5; retry a lokální cache jsou stále budoucí práce:

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

Aktuálně automaticky ověřeno mimo jiné:

- migrace stabilních kolekcí a částí,
- zachování všech videí při testovací migraci; produkčních 3 098 videí je
  potvrzeno samostatnou produkční kontrolou,
- více externích vazeb na jeden titul,
- jeden externí záznam nesmí být omylem primární pro dva různé lokální tituly bez upozornění,
- AniList provider používá parametrizovaný GraphQL dotaz,
- timeout, HTTP a GraphQL chyba nepoškodí katalog,
- ruční potvrzení, změna, aktualizace a odpojení vazby,
- zamknutá metadata se neaktualizují,
- rozdělení kolekce na více virtuálních částí,
- přiřazení podle rozsahu, vzoru i jednotlivého výběru,
- odmítnutí překrývajících se pravidel bez potvrzení,
- zachování ruční hierarchie a číslování při dalším skenu,
- označení nového nezařazeného videa,
- idempotence migrací a opravného nástroje,
- zachování titulků, metadat a ručních hardsubů,
- všechny stávající filtry, hledání a řazení dál fungují.

Automatické ověření po iteraci perzistentních kandidátů a obalů:

```bash
pytest -v                         # 172 passed
python -m compileall app tests   # prošlo
git diff --check                 # prošlo
docker compose config            # prošlo
```

---

## 6.9 Stav uzavření V5

Hotová je stabilní hierarchie `CatalogCollection → CatalogTitle → Video`, ruční
kontrola a dělení kolekcí, oddělené číslování, ruční párování AniList metadat,
zobrazení a správa uložených metadat i ochrana ručních rozhodnutí.

V5 ještě není úplně uzavřená. Perzistentní kandidáti, jejich ruční odmítání,
pomocné skóre, bezpečná cache obalů a omezené dávkové vyhledání jsou hotové.
Zbývá synchronizační log, provider relace a případné další providery.
Automatické potvrzování celé knihovny není povoleno bez samostatné bezpečné
iterace.

---

# 7. V6 – Úplnost knihovny ⏳

V6 není dokončená. Naváže na ověřenou hierarchii V5 a bude řešit skutečnou
úplnost knihovny:

- porovnání lokálních epizod s oficiálním počtem,
- chybějící čísla epizod,
- nerozpoznané epizody,
- titulky bez videa,
- duplicity uvnitř knihovny,
- více verzí jedné epizody,
- procenta úplnosti,
- procenta CZ/SK překladu,
- konflikty mezi lokální strukturou a externí databází.

V6 musí porovnávat konkrétní `CatalogTitle` proti jeho externímu počtu epizod a
používat `season_episode_number` nebo `external_episode_number`, nikdy slepě
`absolute_episode_number`. Současný filtr **Bez CZ/SK** znamená chybějící překlad,
nikoli skutečně chybějící epizodu.

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
Fyzické uklízení a import musí vycházet z již ručně nebo bezpečně automaticky
ověřené databázové hierarchie V5; nesmí si při přesunu znovu nezávisle hádat
identitu kolekcí, částí nebo epizod.

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

1. přes `/hierarchy-review` projít historické kolekce ve stavech `review_required`
   a `conflict`,
2. ručně potvrdit nebo rozdělit pouze kolekce, jejichž strukturu lze ověřit,
3. pokračovat v ručním párování jednotlivých `CatalogTitle` na AniList,
4. dokončit zbývající provozní části V5 bez hromadného automatického párování,
5. potom zahájit malou iteraci V6 pro kontrolu skutečně chybějících epizod,
6. V7 spustit až nad ověřenou databázovou hierarchií.

Nejasný interní suffix ani externí návrh není oprávněním k automatickému rozdělení
kolekce. Nezačínat hromadným automatickým párováním celé knihovny.

---

# 13. Pravidla pro další chat nebo Codex

- Nejprve přečíst celý `docs/PROJECT_STATUS.md`.
- Držet se aktuální verze roadmapy.
- Nepřeskakovat V5 a V6 kvůli V7.
- Neimplementovat přehrávání od nuly.
- Zachovat read-only přístup ke zdrojové anime knihovně.
- Síťové metadata nesmí být podmínkou funkce lokálního katalogu.
- Automatické návrhy nesmí přepisovat ruční rozhodnutí.
- Ručně ověřenou hierarchii nesmí přepsat sken, migrace ani opravný nástroj.
- Nezařazené video musí zůstat evidované v kolekci; nesmí se přiřadit odhadem.
- Interní časové suffixy jsou pouze lokální poznámka, nikoli údaj o sezóně.
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
