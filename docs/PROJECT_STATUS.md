# AnimeDB – stav projektu a roadmapa

> Tento dokument je hlavní checkpoint projektu. Slouží pro pokračování v novém chatu, předání kontextu Codexu a kontrolu, že vývoj neuhýbá od cíle.
>
> **Aktualizováno:** 12. srpna 2026
> **Aktuální checkpoint:** V5 dokončena – následuje stabilizace hierarchie a ladění UI nad reálnou knihovnou
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
  - neověřeno,
  - bez hardsubu,
  - hardsub CZ,
  - hardsub SK,
  - hardsub CZ i SK,
- štítek **Ověřeno přehráním**,
- datum ručního ověření,
- ruční údaje skener nepřepisuje,
- potvrzení absence hardsubu ukládá datum ověření stejně jako potvrzení
  jeho přítomnosti,
- teprve volba **neověřeno** odstraní ruční hodnotu i datum ověření,
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

## Závěrečné produkční ověření V5

Ručně ověřený stav k 7. srpnu 2026:

| Kategorie | Počet |
|---|---:|
| Videa | 3 098 |
| Kolekce | 194 |
| Tituly | 229 |
| Metadata kandidáti | 40 |
| Záznamy artworku | 2 |
| Videa bez kolekce | 0 |
| Videa bez titulu | 0 |

Ručně bylo dále ověřeno:

- aplikace se spustí i s odpojeným NASem,
- pokus o sken s odpojeným NASem skončí řízenou bezpečnostní chybou a databáze
  zůstane nedotčená,
- výpadek NASu během probíhajícího skenu je zachycen a způsobí rollback celé
  transakce,
- po opětovném připojení NASu proběhl normální sken s výsledkem
  `found=3098`, `created=0`, `updated=0`, `unchanged=3098`, `removed=0`,
  `errors=0`,
- offline aktualizace metadat skončila řízenou chybou bez poškození existujících
  dat,
- lokálně cachovaný artwork zůstal dostupný i bez internetu.

---

# 6. V5 – Stabilní hierarchie, metadata a číslování ✅

## Cíl checkpointu V5

Převést technicky seskupený katalog na skutečnou anime knihovnu s ověřenou
identitou kolekcí, konkrétních částí a externích metadat.

V5 nesmí měnit ani přesouvat videosoubory. Pracuje pouze s databází, identitou
titulu, metadaty, vzdálenými náhledy obrázků a ručním potvrzením.

V5 je produkčně nasazená, závěrečně ověřená a uzavřená. Automatické párování,
další metadata providery, provider relace a úplná kontrola chybějících epizod
nepatří do uzavřeného rozsahu V5. Bezpečné dávkové hledání pouze ukládá kandidáty
a nikdy je samo nepotvrzuje.

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
thumbnail_path
mime_type
width
height
file_size
is_primary
fetched_at
updated_at
```

Současná implementace používá `artwork_type=cover`; thumbnail je samostatný
soubor evidovaný v `thumbnail_path`, nikoli samostatný databázový typ artworku.

### Plánovaná, ale neimplementovaná entita `MetadataSyncLog`

Synchronizační log nebyl součástí uzavíracího kritéria V5. Pro případné budoucí
rozšíření byl navržen tento tvar:

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
vypnout; při dostupném lokálním artworku se používá jeho cachovaný thumbnail.

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

Po ručním potvrzení nebo aktualizaci metadat se obal volitelně ukládá do lokální
cache. Ověřuje se HTTP/HTTPS URL bez přihlašovacích údajů, podporovaný MIME typ,
platnost obrázku a maximální velikost. Soubory se publikují atomicky a vedle
originálu vzniká WebP thumbnail. Ruční akce **Obnovit obal** vynutí nové stažení.

Chyba stažení obalu neruší již potvrzená metadata. Neúspěšný download ani
neplatný nový obrázek nepřepíše předchozí úspěšný artwork, takže cachovaný obal
zůstává použitelný také při nedostupném internetu. Binární cache je vyloučená z
Gitu.

Použitá cesta:

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
`METADATA_DOWNLOAD_ARTWORK` řídí aktuálně implementované stahování obalů.
`METADATA_CACHE_TTL_HOURS` je připravené pro případnou budoucí cache metadata
odpovědí a nynější artwork cache jej nepoužívá.

Tajné klíče a tokeny nikdy neukládat do Gitu.

---

## 6.7 Bezpečnost a provozní pravidla V5

Implementované jsou explicitní dělené HTTP timeouty, ošetření rate limitu a
síťových/HTTP/GraphQL chyb, transakční rollback, lokální cache artworku a ochrana
ručních či zamknutých dat. Síťová nedostupnost je řízená chyba a neblokuje běžné
prohlížení lokálního katalogu. Pro budoucí síťové integrace nadále platí:

- síťová chyba nesmí poškodit katalog,
- selhání provideru nesmí blokovat běžné prohlížení knihovny,
- používat timeout,
- případný retry vždy omezit,
- respektovat rate limit,
- podle potřeby cacheovat odpovědi,
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

Závěrečné automatické ověření 7. srpna 2026:

```bash
pytest -q                         # 172 passed
python -m compileall app tests   # prošlo
git diff --check                 # prošlo
```

V kontrolním shellu nebyly aliasy `pytest` a `python` přímo na `PATH`, proto byly
první dva příkazy spuštěny ekvivalentně přes projektové virtuální prostředí jako
`.venv/bin/pytest -q` a `.venv/bin/python -m compileall app tests`.

---

## 6.9 Stav uzavření V5

V5 je dokončena a formálně uzavřena. Ověřený rozsah tvoří:

- stabilní hierarchie `CatalogCollection → CatalogTitle → Video`, ruční kontrola
  a dělení kolekcí a oddělené číslování,
- ruční hledání titulů na AniListu a ruční propojení `CatalogTitle` s AniList
  metadaty,
- perzistentní metadata kandidáti, jejich pomocné skóre, potvrzení, odmítnutí a
  zrušení odmítnutí,
- přehled **Metadata Review** a omezené dávkové hledání, které kandidáty nikdy
  samo nepotvrzuje,
- bezpečná normalizace lokálního názvu výhradně pro výchozí metadata search
  query,
- explicitní potvrzení konfliktu stejného primárního AniList ID,
- zamykání ručně potvrzených metadat a ochrana ručních rozhodnutí,
- lokální cache artworku, WebP thumbnail, ruční refresh a zachování starého
  artworku při neúspěšném stažení,
- explicitní HTTP timeouty a bezpečné offline chování bez poškození existujících
  dat.

`MetadataSyncLog` zůstává pouze plánovaným rozšířením; synchronizační historie
není součástí uzavíracího kritéria této etapy. AniList relace, další providery,
retry politika a automatické potvrzování celé knihovny jsou možné budoucí
rozšíření, nikoli nedokončené položky V5.

## 6.10 Související provozní hardening doplněný během V5

Následující změny byly dokončeny a ověřeny během stejné etapy, ale jsou
provozním hardeningem skeneru, nikoli funkčním rozsahem metadata V5:

- `FFPROBE_TIMEOUT_SECONDS=60`,
- `MEDIAINFO_TIMEOUT_SECONDS=60`,
- `LIBRARY_ACCESS_TIMEOUT_SECONDS=10`,
- `LIBRARY_HEALTHCHECK_INTERVAL_FILES=25`,
- kontrola dostupnosti NASu před skenem,
- průběžná kontrola NASu během skenu,
- poslední kontrola dostupnosti před mazací fází,
- rollback celé databázové transakce při výpadku knihovny během skenu,
- zachování původního databázového záznamu změněného videa při timeoutu
  `ffprobe`.

Procesní healthcheck omezuje dobu čekání aplikace i v situaci, kdy filesystemová
operace uvázne v CIFS klientovi. Ruční produkční test potvrdil bezpečné chování
při odpojeném NASu před skenem i při jeho výpadku během skenu.

## 6.11 Následující pracovní fáze – Stabilizace hierarchie a ladění UI nad reálnou knihovnou

Tato fáze proběhne **před implementací kontroly úplnosti knihovny (V6)**.

Cíl fáze:

- ručně projít existující kolekce a tituly,
- opravit skutečnou hierarchii sezón, částí, filmů, OVA a dalších titulů,
- ověřovat hierarchii pomocí statusu `verified`,
- při praktické práci identifikovat problémy nebo zbytečně složité kroky v UI,
- průběžně opravovat problémy nalezené při ruční tvorbě hierarchie,
- zachovat ručně ověřenou hierarchii proti automatickému přepsání.

## 6.12 Stabilizace episode numbering nad reálnou knihovnou

Probíhá fáze **Stabilizace hierarchie a ladění UI nad reálnou knihovnou**. V6
nebyla zahájena.

První konkrétní problém byl nalezen u kolekce
`100-man no Inochi no Ue ni Ore wa Tatteiru (P20-L21)`. Soubory ve tvaru
`100-man no Inochi no Ue ni Ore wa Tatteiru - 01.mkv` zůstávaly bez čísla,
protože původní parser našel současně `100` uvnitř názvu a `01` na konci a
nejednoznačný výsledek správně odmítl. Parser nyní před obecným odmítnutím
rozpozná bezpečný koncový tvar `Title - NN`; obecné samostatné číslo na libovolném
místě názvu už nepovažuje za epizodu. Explicitní tvary `E01`, `EP02`,
`Episode 03` a čistě číselné názvy souborů zůstávají podporované. Rok,
rozlišení, číslo uvnitř názvu, technické hodnoty a suffixy jako `(P20-L21)`
zůstávají bez bezpečné shody `unknown`.

### Skutečná semantika uložených polí

- `Video.local_episode_number` je výhradně bezpečně rozpoznaná hodnota z názvu
  souboru. Ruční vstup ji nenahrazuje.
- `Video.episode_number_manual_override` je ručně zadaný vstup s předností před
  lokální hodnotou. Jeho uložení nastaví `episode_number_verified_at`; smazání
  override datum odstraní.
- `Video.season_episode_number` je výsledné číslo v rámci konkrétního
  `CatalogTitle`, nově zobrazované jako `E`.
- `Video.absolute_episode_number` je výsledné absolutní číslo v rámci kolekce,
  zobrazované jako `A`. Bez známého bezpečného offsetu se u pozdější části
  neodhaduje.
- `Video.external_episode_number` je v současném modelu číslo očekávané externě
  propojeným titulem. Pokud má titul `TitleMetadata`, přebírá vypočtené
  `season_episode_number`; nejde o samostatně načtené číslo konkrétní epizody od
  provideru. UI je proto označuje samostatně jako `X`, nikoli jako `E`.
- `Video.episode_number_source` nabývá hodnot `unknown`, `filename`, `manual`
  nebo `derived_from_part_offset`. `episode_number_confidence` je současně
  `NULL`, `0.95`, `1.0`, respektive `0.9` podle použité cesty.
- `CatalogTitle.numbering_mode=unknown` odpovídá volbě **automaticky**. S
  offsetem rozliší lokální a absolutní vstup podle čísel; bez offsetu bezpečně
  stanoví sezónní číslo a absolutní číslo pouze pro první část.
- `numbering_mode=season_local` interpretuje rozpoznaný nebo ruční vstup jako
  `season_episode_number` (`E`).
- `numbering_mode=absolute` interpretuje vstup jako
  `absolute_episode_number` (`A`).
- Hodnota `mixed` zůstává podporovanou interní kompatibilní hodnotou, ale web ji
  nenabízí jako samostatný ruční režim; výpočet používá stejnou bezpečnou
  detekční větev jako automatický režim.
- `CatalogTitle.episode_start_offset` je počet epizod před aktuálním titulem,
  nikoli počáteční číslo epizody. Pro sezónní vstup platí `A = E + offset`, pro
  absolutní vstup `E = A - offset`. Pro první sezónu je offset `0`, pro druhou
  sezónu po 12 epizodách `12`. Prázdný offset dovolí použít jen bezpečně známý
  součet oficiálních počtů předchozích titulů. Pokud počet kteréhokoli
  předchozího titulu chybí, další absolutní offset se už neodvozuje.
- `CatalogTitle.numbering_manual` a `numbering_verified_at` zaznamenávají ruční
  uložení režimu nebo offsetu. Nemění `season_number`, přiřazení videa ani
  metadata vazbu.

Původní výpis `S1 A4 E1` ve sloupci čísla epizody znamenal
`season_episode_number=1`, `absolute_episode_number=4` a
`external_episode_number=1`. Písmeno `S` zde neznamenalo
`CatalogTitle.season_number`; skutečná sezóna se vždy zobrazovala v sousedním
sloupci z `CatalogTitle.effective_season_label`. UI nyní používá jednoznačné
značky `E` pro epizodu v sezóně, `A` pro absolutní číslo, `L` pro lokální
detekci a `X` pro externí číslo.

### Ruční a sekvenční číslování

Ruční zadání jednotlivého čísla mění pouze episode numbering pole videa a
následný výpočet jejich odvozených reprezentací. Nemění `CatalogTitle`,
`season_number`, hierarchii ani metadata.

Detail `CatalogTitle` nabízí akci **Očíslovat videa podle aktuálního pořadí**.
Náhled používá deterministické přirozené pořadí názvů souborů, při shodě cestu a
ID, a nic nezapisuje. Zápis vyžaduje samostatné explicitní potvrzení. Pokud by
návrh změnil existující ruční override, označí konflikt a backend odmítne zápis
bez dalšího výslovného potvrzení. Potvrzení ukládá čísla jako ruční override a
nemění příslušnost videí ani sezónu titulu.

### Hierarchy Review

Ručně ověřená kolekce zůstává v `/hierarchy-review`, dokud některý její titul
obsahuje neznámé číslování, mezery nebo duplicity. Detail u každého
`CatalogTitle` uvádí počet videí, poměr očíslovaných videí, rozsah `E`, mezery,
duplicity a srozumitelný stav. Ověřená hierarchie s neznámými čísly zobrazuje
samostatné varování; vyřešení číslování nemění její stav hierarchie.

Tato změna neobsahuje databázovou migraci ani automatický hromadný přepočet
produkční databáze. Existující produkční hodnoty nebyly změněny. Oprava se
provádí následně a explicitně přes UI pro jednotlivé tituly.

Automatické ověření 11. srpna 2026:

```bash
.venv/bin/pytest -q                    # 191 passed
.venv/bin/python -m compileall app tests  # prošlo
git diff --check                       # prošlo
```

## 6.13 Jednosériové kolekce a jednodušší ruční rozdělení

Ve fázi **Stabilizace hierarchie a ladění UI nad reálnou knihovnou** byl na
reálném případu `Akame ga Kill! (L14)` doplněn bezpečný návrh pro jednoznačné
jednosériové kolekce. Pokud kolekce obsahuje právě jeden obecný `CatalogTitle`
s alespoň jedním videem, bez určené sezóny, konfliktu nebo dřívějšího ručního
zařazení, zobrazí Hierarchy Review návrh **Nastavit jako Season 1**. Formát
`TV` nebo `TV_SHORT` z uložených metadat může návrh pouze vizuálně podpořit;
metadata nikdy změnu sama neprovedou.

Akce vyžaduje explicitní potvrzení a nastaví pouze ruční číslo sezóny `1`,
označení `S1`, typ `season` a příznak manuálního override. Nemění přiřazení
videí, episode numbering, metadata vazbu, `hierarchy_status` kolekce ani datum
ověření kolekce či titulu. Uživatel proto stále samostatně potvrzuje
**Zařazení ověřeno** na úrovni `CatalogTitle` a **Hierarchie ověřena** na úrovni
celé kolekce.

Výchozí ruční rozdělení v Hierarchy Review nyní používá lidský formulář pro
název a typ části, sezónu, Part, pořadí, rozsah epizod, offset, pravidlo názvu
souboru a explicitní `video_ids`. Formulář se převádí do existujícího
`ManualTitleDefinition`, takže používá původní validace, read-only preview a
stejnou potvrzovací akci. Technický JSON zůstává dostupný v rozbalovací sekci
pro pokročilé případy. Virtuální rozdělení nadále nemění ani nepřesouvá fyzické
soubory na NASu a nedochází k žádnému automatickému přepisování hierarchie.

V6 nebyla zahájena a produkční databáze nebyla touto změnou automaticky
upravena.

Automatické ověření 11. srpna 2026:

```bash
.venv/bin/pytest -q                       # 205 passed
.venv/bin/python -m compileall app tests  # prošlo
git diff --check                          # prošlo
```

## 6.14 Čitelnější seznam epizod na detailu titulu

V rámci fáze **Stabilizace hierarchie a ladění UI nad reálnou knihovnou**
byla tabulka videí na detailu `CatalogTitle` upravena tak, aby jako hlavní
informace zobrazovala uživatelský název, sérii, epizodu, délku, společný
seznam titulků, hardsub a stav ověření. Filename a další technické údaje
zůstávají dostupné se sníženou vizuální prioritou. Změna nemění
hierarchii, číslování ani databázový model.

Pozdější samostatný UX refaktor má rozdělit detail titulu na:

- běžné **Zobrazení** s metadaty, artworkem, seznamem epizod a čitelnými
  informacemi,
- **Úpravy** s hierarchií, propojením metadat, číslováním, override a
  technickými údaji.

Tento View/Edit refaktor nyní implementován nebyl. V6 nebyla zahájena.

Hardsub se v seznamu zobrazuje trojstavově: **Ano** znamená ručně potvrzenou
přítomnost, **Ne** ručně potvrzenou absenci a **Neznámé** chybějící
ruční ověření. Hodnotu nesou existující boolean příznaky a stav ověření
rozlišuje `manual_hardsub_verified_at`; nebyla potřeba databázová migrace.

### Videa přímo v kořeni knihovny

Při ruční kontrole byly nalezeny samostatné filmy uložené přímo v kořeni
knihovny. Scanner je správně eviduje s `root_folder="."`; původní přehled ale
tečku zobrazoval jako kořenovou složku a odkaz `/folders/.` mohl být při
zpracování URL normalizován na prázdnou cestu. Výsledkem byl prázdný katalog.

UI nyní používá samostatný přehled **Nezařazená videa z kořene knihovny**.
Root soubory se automaticky neslučují do anime kolekce pouze podle společného
fyzického umístění. Lze je explicitně přiřadit k existujícímu
`CatalogTitle` nebo pro jednotlivý soubor ručně vytvořit samostatný titul.
Existující smysluplné ruční přiřazení scanner zachová. Žádná z těchto
akcí nemění ani nepřesouvá fyzický soubor. Staré technické přiřazení k
pseudo-kolekci `.` se pouze zobrazí jako stav ke kontrole; tato změna sama
produkční databázi automaticky neupravuje.

Ruční akce **Vytvořit samostatný titul** ukládá pro každé video vlastní
virtuální `CatalogCollection` s cestou `@root/<video_id>`, její `CatalogTitle` s
cestou `@root/<video_id>/title` a oba cizí klíče na `Video`. Přehled root videí
potom vybírá jen soubory bez smysluplného logického přiřazení, nikoli všechny
soubory s fyzickým `root_folder="."`. Startupová idempotentní migrace ani
následný sken virtuální vazbu nepřepisují. Legacy vazba na `.` zůstává při
startu beze změny a UI ji nadále prezentuje jako technický stav ke kontrole.

### Logický katalog na titulní stránce

Hlavní katalog na titulní stránce je založen na uložené logické hierarchii
`CatalogCollection -> CatalogTitle -> Video`. `CatalogCollection` je jeho primární
uživatelská jednotka; fyzická struktura NASu (`root_folder`, složky a cesty)
zůstává dostupná v samostatném sekundárním technickém pohledu. Virtuální
collection s cestou `@root/...` se proto zobrazuje stejně jako běžná collection
ve fyzické složce. Legacy pseudo-přiřazení `.` není prezentováno jako anime
collection a zůstává v workflow nezařazených root videí.

Navigace z homepage vynechává zbytečný mezikrok: pokud collection obsahuje
právě jeden `CatalogTitle` a všechna její zobrazená videa jsou k tomuto titulu
jednoznačně přiřazena, odkaz vede přímo na `/titles/{id}`. Collection s více
částmi nebo neúplným přiřazením vede na `/collections/{id}`. Toto pravidlo je
pouze prezentační a nemění databázovou hierarchii. Vytváření dalších
Season/Part/Cour, Film/OVA/Special částí, jejich rozdělování, pořadí a přesuny
videí nadále patří do **Hierarchy Review**.

Změna nevyžadovala databázovou migraci, neupravuje produkční data a nemění
fyzické cesty souborů. V6 nebyla zahájena.

## 6.15 Nestandardní číslování a logické oddělení doplňkového obsahu

V rámci fáze **Stabilizace hierarchie a ladění UI nad reálnou knihovnou** byl
parser rozšířen o bezpečnou detekci běžného koncového čísla bez pomlčky.
Například `Title 01.mkv`, `Title 02.mp4` a `Title 22.mp4` se nyní rozpoznají jako
E1, E2 a E22. Číslo musí být na konci názvu před příponou. Samostatný číselný
název anime, rok, rozlišení, codec, release group ani technický suffix se nadále
nesmí odhadnout jako číslo epizody; nejednoznačný výsledek zůstává `unknown` a
jde do Hierarchy Review.

Parser nově rozlišuje čtyři výsledky:

- standardní celé číslo větší než nula,
- nestandardní `00`,
- nestandardní desetinné číslo, například `14.5`,
- `unknown`.

`00` se neukládá jako E0. `14.5` se nezaokrouhluje ani nepřevádí na E14 nebo
E15 a parser mu automaticky neurčuje typ Recap. U obou případů zůstávají
integer pole `local_episode_number`, `season_episode_number`,
`absolute_episode_number` a `external_episode_number` prázdná. Druh detekce se
ukládá do existujícího `episode_number_source` jako `nonstandard_zero` nebo
`fractional`; původní textová hodnota se bezpečně zobrazuje z filename. Pro
desetinné číslo tedy nebyla nutná změna datového modelu ani databázová migrace.

Souhrn číslování odděluje fyzický počet videí, standardní epizody, `unknown` a
nestandardní položky. `00` ani fractional epizoda nevstupují do standardního
rozsahu a nevytvářejí v něm mezeru. Kolekce s nevyřešenou nestandardní položkou
zůstává v Hierarchy Review i tehdy, když uživatel ručně označil její hierarchii
za ověřenou.

Příklad `Ansatsu Kyoushitsu (Z15-Z16)` lze nyní zobrazit takto:

```text
Season 1
  videí fyzicky: 23
  standardních epizod: 22
  očíslováno: 22/22
  rozsah: E1–E22
  nestandardní položky: 1
  00 -> vyžaduje zařazení

Season 2
  videí fyzicky: 25
  standardních epizod: 25
  očíslováno: 25/25
  rozsah: E1–E25
```

Hierarchy Review nabízí pro rozpoznané `00` a fractional položky akci
**Oddělit do nové části**. Uživatel vybere jedno nebo více videí, zadá lokální
název a typ `Preview`, `Special`, `Recap`, `OVA`, `Bonus` nebo `Other`. Vznikne
samostatný `CatalogTitle` a změní se pouze `Video.catalog_title_id`. Fyzický
`relative_path`, adresář ani videosoubor na NASu se nemění. Ruční logická vazba
přežije novou databázovou session, restart, idempotentní startupovou migraci i
následný sken. Nově nalezené nejednoznačné video zůstane nezařazené a znovu
otevře kontrolu.

Preview, Recap, Special a jiný doplňkový `CatalogTitle` může zůstat ve stavu
`unlinked` bez `ExternalTitleLink` a bez `TitleMetadata`; nejde o chybu. Pokud
provider vhodný samostatný titul nabízí, uživatel jej může propojit ručně.

Zdrojová knihovna zůstává read-only. Později může vzniknout samostatný workflow,
který až nad ručně ověřenou logickou hierarchií nabídne fyzické přeskládání
souborů na NASu. V této iteraci nebyl takový workflow implementován, produkční
databáze nebyla automaticky měněna a V6 nebyla zahájena.

Automatické ověření 12. srpna 2026:

```bash
.venv/bin/pytest -q                       # 230 passed
.venv/bin/python -m compileall app tests  # prošlo
načtení všech Jinja2 šablon               # 11 šablon, prošlo
git diff --check                          # prošlo
```

## 6.16 Prezentační názvy a přehlednější Hierarchy Review

V rámci fáze **Stabilizace hierarchie a ladění UI nad reálnou knihovnou** byl
sjednocen způsob, kterým se zobrazuje primární název `CatalogTitle`. Původní
helper používal `manual_display_title`, uložený `TitleMetadata.display_title` a
nakonec `CatalogTitle.local_title`. Technické označení části jako `Serie 1`
proto mohlo bez metadat působit jako název anime.

Centrální helper `catalog_title_display_title` nyní používá toto pořadí:

1. existující explicitní per-title `manual_display_title`, pokud jej uživatel
   dříve ručně nastavil,
2. varianty připojených metadat podle preferovaného jazyka,
3. legacy `TitleMetadata.display_title`, pokud starší metadata nemají jednotlivé
   varianty,
4. bezpečný společný název odvozený z filename videí,
5. `CatalogTitle.local_title`,
6. obecný text `Titul bez názvu`.

AniList skutečně poskytuje `romaji`, `english` a `native`. Provider je převádí
na `ProviderTitleMetadata.title_romaji`, `title_english` a `title_native` a při
potvrzení je všechny persistuje do stejných polí entity `TitleMetadata`.
Nevznikla žádná nová metadata pole.

Preferovaný jazyk názvu je samostatný vstup resolveru, nikoli vlastnost
`CatalogTitle`. Výchozí aplikační hodnota je `romaji` a lze ji nastavit přes
`Settings.preferred_title_language` / `PREFERRED_TITLE_LANGUAGE`. UI v hlavičce
nabízí globální volby **Romaji**, **Anglický** a **Originální**. Uživatelská
volba pro současnou single-user instalaci se ukládá do dlouhodobé browser cookie
`animedb_preferred_title_language`, která má před aplikačním defaultem přednost
a přežije restart serveru i novou browser session. Cookie není databázové pole
a změna preference proto nevyžaduje migraci.

Zdroj preference je soustředěn ve funkci `get_preferred_title_language` a je
oddělen od vlastního resolveru. Po případném budoucím zavedení uživatelských
účtů nebo doménové autentizace tak půjde cookie/aplikační default nahradit
hodnotou například z `current_user.preferences`, aniž by se měnil fallback
názvů nebo jednotlivé šablony. User model, tabulka uživatelských preferencí,
role, LDAP/Active Directory ani multi-user administrace nyní implementovány
nebyly.

Fallback metadata názvů je deterministický:

- `romaji`: Romaji → English → Native,
- `english`: English → Romaji → Native,
- `native`: Native → Romaji → English.

Chybějící preferovaná varianta tedy nevytvoří prázdný název a nespadne rovnou
na technické `Serie 1`, pokud metadata obsahují jinou použitelnou variantu.
Bez metadata linku preference nijak neovlivní filename fallback.

Filename helper odstraňuje pouze suffix, který bezpečně rozpoznal episode
parser. Podporuje například `Title 01`, `Title - 01`, `Title 00` a `Title 14.5`.
Z více videí použije výsledek pouze tehdy, když se jejich odvozené prefixy
shodují. Konfliktní názvy souborů, samotné `Episode 01`, rok, technický suffix
ani samostatný číselný název se agresivně neořezávají; použije se lokální
fallback. Tato logika pouze čte filename a nic nepřejmenovává.

Společný display-title mechanismus nyní používají:

- seznam a detail Hierarchy Review,
- detail `CatalogCollection`,
- detail `CatalogTitle` včetně seznamu epizod a metadata panelu,
- workflow root videí a výběr cílového titulu.

Hlavní katalog nadále primárně zobrazuje `CatalogCollection`, nikoli jednotlivý
`CatalogTitle`; jeho collection název proto zůstává záměrně beze změny. Přehled
metadat má explicitní sloupec **Lokální název**, který také nadále ukazuje
`local_title` podle významu sloupce.

Detail Hierarchy Review zobrazuje každý `CatalogTitle` v samostatném bloku s
display title, lokálním označením části, typem, fyzickým počtem videí,
standardními epizodami, stavem číslování, rozsahem, počtem `unknown`, počtem
nestandardních položek a neutrálním stavem metadata linku. Standardní epizody
jsou kompaktní a rozbalitelné. `00` a fractional obsah jsou viditelné zvlášť od
`unknown`; problematické položky zůstávají dominantní. Akce **Oddělit do nové
části** nadále podporuje Preview, Special, Recap, OVA, Bonus a Other, mění pouze
`Video.catalog_title_id` a nabízí upravitelný lokální název nové části.

Preview, Recap a další doplňkové části mohou zůstat bez externích metadat;
zobrazení **Bez externích metadat** proto samo o sobě není warning ani chyba.
Změna preference ani nový fallback nemění `CatalogTitle`, `TitleMetadata`,
`ExternalTitleLink`, filename, `relative_path` nebo fyzickou strukturu NASu.
Produkční databáze nebyla automaticky upravena, fyzické přeskládání NASu se
stále neprovádí a V6 nebyla zahájena.

Automatické ověření 12. srpna 2026:

```bash
.venv/bin/pytest -q                       # 250 passed
.venv/bin/python -m compileall app tests  # prošlo
načtení všech Jinja2 šablon               # 11 šablon, prošlo
git diff --check                          # prošlo
```

## 6.17 Výběr a změna externích metadat

Detail `CatalogTitle` po úspěšném přiřazení `ExternalTitleLink` standardně
zobrazuje pouze aktuální metadata, jejich varianty názvu a základní údaje o
primární externí vazbě. Dříve uložené, ale nevybrané metadata kandidáty už
nezůstávají trvale rozbalené.

Metadata panel nyní rozlišuje dvě akce:

- **Změnit metadata** otevře již persistované kandidáty bez nového síťového
  dotazu, takže lze bezpečně zvolit jiný externí titul;
- **Vyhledat metadata znovu** vždy explicitně zavolá metadata provider, uloží
  aktuální výsledky a ihned otevře jejich výběr.

Původní ruční search používal samostatný POST render a šablona přehazovala dvě
různé podoby proměnné `metadata_candidates`. Výsledek hledání proto neměl
jednoznačný request/response stav a první render mohl pracovat s jiným seznamem
než následné načtení detailu. Tok je nyní sjednocen na
`POST search → uložení kandidátů → 303 redirect → GET detailu s otevřeným
výběrem`. Výsledky jsou při tomto prvním navazujícím GET již načtené z databáze;
není potřeba druhé kliknutí ani druhý provider search.

Potvrzení jiného kandidáta používá stávající `ExternalTitleLink` a
`TitleMetadata`: nová vazba se stane primární a předchozí zůstane neprimární
historickou vazbou podle dosavadního mechanismu. Běžný detail ukazuje pouze
aktuální primární vazbu. Změna metadat nemění `CatalogTitle.local_title`,
`manual_display_title`, hierarchii nebo přiřazení videí, epizodní číslování,
filename, `relative_path` ani fyzickou strukturu NASu. Nově zvolená vazba je
uložená v databázi a přežije novou DB session i restart aplikace.

Po změně linku zůstává zobrazovaný název řešen centrálně přes
`catalog_title_display_title`. Globální prezentační preference Romaji,
English nebo Native se tedy aplikuje i na právě zvolená metadata a nadále není
vlastností `CatalogTitle`. Chybějící varianta používá stejný bezpečný fallback
jako na ostatních obrazovkách.

Tato úprava nepřidala databázovou migraci ani nový stavový subsystém. Produkční
databáze nebyla automaticky měněna, fyzické soubory ani adresáře na NASu nebyly
upravovány a V6 nebyla zahájena.

Automatické ověření 12. srpna 2026:

```bash
.venv/bin/pytest -q                       # 252 passed
.venv/bin/python -m compileall app tests  # prošlo
načtení všech Jinja2 šablon               # 11 šablon, prošlo
git diff --check                          # prošlo
```

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

### Zásadní pravidlo kontroly úplnosti

- chybějící běžná epizoda potvrzené série nebo sezóny je `ERROR`,
- duplicitní běžná epizoda je `ERROR`,
- `00`, fractional epizoda, Preview, Recap, Special, Bonus, OVA, ONA, OP, ED,
  NCOP nebo NCED nejsou součástí completeness hlavní série,
- chybějící OVA, ONA, Preview, Recap, Special, Bonus, OP, ED, NCOP nebo NCED
  není chyba úplnosti hlavní série,
- absence tohoto doplňkového obsahu je maximálně `INFO` o jeho existenci,
- nejisté nebo nejednoznačné číslování je `WARNING` a vyžaduje ruční kontrolu,
- doplňkový obsah nesmí způsobit označení hlavní série jako nekompletní.

Toto pravidlo je zatím pouze dokumentované; implementace V6 nebyla zahájena.

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

Bezprostřední další krok je fáze **Stabilizace hierarchie a ladění UI nad reálnou
knihovnou** popsaná v části 6.11:

1. přes `/hierarchy-review` a detaily kolekcí projít existující kolekce a tituly,
2. ručně potvrdit nebo rozdělit pouze kolekce, jejichž strukturu lze ověřit,
3. ukládat ověřenou hierarchii se statusem `verified`,
4. při této práci průběžně opravovat konkrétní problémy a zbytečně složité kroky
   v UI,
5. teprve po této stabilizaci zahájit V6 podle pravidel kontroly úplnosti,
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
