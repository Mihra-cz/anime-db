# AnimeDB – stav projektu a roadmapa

> Tento dokument je hlavní checkpoint projektu. Slouží pro pokračování v novém chatu, předání kontextu Codexu a kontrolu, že vývoj neuhýbá od cíle.
>
> **Aktualizováno:** 30. srpna 2026
> **Aktuální checkpoint:** Video Variant – Manual authority UI
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
`local_period_hint`. Jde o legacy časové/metadata hinty, nikoli hierarchy
autoritu: neurčují počet sezón, částí ani epizod a samy nejsou důvodem
`review_required`. Závorková varianta, například `(L20-P23)` nebo
`( L20-P23 )`, historicky navíc znamenala „dokoukáno“; ani tato informace není
hierarchy údaj a současná verze z ní nemigruje watch-state. Původní `local_title`
a cesta zůstávají beze změny. Budoucí slabé použití při metadata candidate
scoringu je pouze roadmapa a zatím není implementované.

### `CatalogTitle`

Konkrétní metadata jednotka uvnitř kolekce: jedna sezóna, část, film, OVA,
Specials nebo samostatné anime bez rozpoznané podstruktury. Každý `CatalogTitle`
může mít vlastní `ExternalTitleLink` a `TitleMetadata`.

Vedle stabilní lokální identity a metadata polí jsou implementována zejména:

```text
season_number_manual
part_number_manual
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
`effective_part_number` používá stejnou prioritu manual → automatic jako season
number. Season scope a pořadí Partu jsou dvě nezávislé hodnoty.

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

- `--dry-run` sestaví strukturovaný globální reconciliation plán bez zápisu do DB,
- `--apply` aplikuje tentýž plán po kontrole, že se zdrojový stav mezitím nezměnil,
- nástroj vytváří chybějící automatic collections a titles, přepočítává membership,
  numbering, provenance a hierarchy status stejnými shared pravidly jako scanner,
- čistě automatické obsolete objekty odstraňuje pouze tehdy, když nenesou žádná
  uživatelská data ani relevantní vazbu,
- metadata, explicitní manual-split authority a ručně potvrzenou hierarchii
  konzervativně zachovává.

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
reálném případu `Akame ga Kill! (L14)` doplněn bezpečný návrh pro kolekce s
jediným `CatalogTitle`. Současný formulář **Ručně potvrdit typ jediné části**
nepředpokládá, že jediná část je definitivně Season 1. Uživatel vybírá typ,
volitelné číslo sezóny a volitelné označení; číslo 1 je pouze editovatelný návrh.
Automatický folder hint může podobně předvyplnit například 2 / S2, ale dokud
uživatel formulář nepotvrdí, ruční pole zůstávají prázdná. Formát `TV` nebo
`TV_SHORT` z uložených metadat může návrh pouze vizuálně podpořit; metadata
nikdy změnu sama neprovedou.

Akce vyžaduje explicitní potvrzení a teprve potom nastaví `part_type_manual`,
volitelné `season_number_manual`, `season_label_manual`, příznak manuálního
override a ověření této části. Pokud uživatel potvrdí číslo sezóny a label
ponechá prázdný, vznikne label podle skutečně potvrzeného čísla, například S2;
bez potvrzeného čísla se automaticky nevytvoří S1. Pokud nejsou přítomné jiné
aktuální problémy, kolekce se nastaví na `verified` a původní automatický review
reason se odstraní. Akce nemění přiřazení videí, episode numbering, display
title ani metadata vazbu.

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
Season/Part, Film/OVA/Special částí, jejich rozdělování, pořadí a přesuny
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

## 6.18 Potvrzené fyzické duplicity

Hierarchy Review nyní rozlišuje dvě různé situace. **Podezření na duplicitu**
znamená, že více videí má stejné standardní číslo epizody, ale stejný obsah
zatím nebyl potvrzen. **Potvrzená duplicita** znamená pouze to, že uživatel
ověřil společnou logickou identitu souborů a ručně vybral jedno primary video.
Neznamená to, že obě fyzické kopie jsou žádoucí nebo že problém knihovny skončil.

Persistentní vztah používá nullable self-reference
`Video.duplicate_of_video_id`. `NULL` znamená, že video není potvrzenou kopií;
ID ukazuje na uživatelem zvolené primary video. Více kopií může ukazovat na
stejné primary. Workflow kontroluje stejný `CatalogTitle`, kolekci a známé
episode number, nedovolí self-reference ani cyklus a primary nikdy nevybírá
automaticky podle filename nebo technických parametrů. Pokud primary při
pozdějším scanu zmizí, zbývající kopie dostane
`duplicate_primary_missing=true`, vztah se považuje za neplatný a kolekce se
vrátí do review; AnimeDB sama nevybere nové primary.

UI **Vyřešit duplicity** zobrazuje pro všechny kolizní epizody filename,
`relative_path`, délku, rozlišení, codec, velikost, audio, interní a externí
titulky a ruční stav hardsubu. Primary volí uživatel u každé skupiny, ale všechna
rozhodnutí může potvrdit jedním hromadným formulářem. Označení lze zrušit a
primary lze změnit. Po zrušení se skupina znovu zobrazí jako nevyřešená kolize
čísel.

Potvrzená duplicate copy se nepočítá jako další logická standardní epizoda.
Například `Bungo to Alchemist - Shinpan no Haguruma` s E1–E13 ve dvou kopiích
proto po potvrzení ukazuje 26 fyzických videí, 13 logických standardních epizod,
13/13, rozsah E1–E13, nula nevyřešených číselných kolizí a 13 potvrzených
duplicitních souborů. Číslování i logická identita zůstávají vyřešené.
Platná potvrzená duplicita s existujícím primary sama
nezpůsobuje `review_required`, nepočítá se mezi blokující problémy a kompletní
ruční hierarchy proto může zůstat `verified`. UI ji nadále zobrazuje jako
neblokující backlog fyzického cleanupu včetně primary, secondary kopií, změny
primary a zrušení chybného potvrzení. Unresolved kolize a
`duplicate_primary_missing` zůstávají blokující; chybějící primary AnimeDB
automaticky nenahrazuje.

Dlouhodobým cílem je na NASu vztah **jedna logická epizoda = jedna uživatelem
preferovaná fyzická verze**. Budoucí samostatná akce **Vyřešit duplicitu** má
porovnat obě verze a nabídnout ponechat A, ponechat B, zatím ponechat obě nebo
zrušit označení duplicity. Pomocné údaje mohou zahrnovat cestu, velikost, délku,
rozlišení, video codec, audio a titulkové stopy a hardsub. Současný model tyto
údaje převážně má; neukládá ale bitrate ani content hash. Vyšší rozlišení nebo
větší velikost sama nikdy neznamená automaticky lepší verzi. Jiný střih, audio,
titulky nebo edice mohou znamenat nezaměnitelný obsah. Destruktivní volbu musí
později potvrdit uživatel.

Fyzický cleanup nyní implementován nebyl: AnimeDB duplicitní soubor nemaže,
nepřejmenovává, nepřesouvá, nenahrazuje, nevytváří hardlink ani symlink. Změna
je pouze databázová a prezentační. Pro vztah a explicitní stav chybějícího
primary byla přidána minimální idempotentní migrace sloupců
`videos.duplicate_of_video_id` a `videos.duplicate_primary_missing`; staré
záznamy dostanou `NULL` a `false` a jejich ostatní data zůstávají zachována.
Produkční databáze nebyla otevřena ani migrována.

### Hash audit a budoucí V7 duplicate preflight

`Video` v současnosti neobsahuje file hash ani content hash a scanner žádný
hash nepočítá, ať už z celého souboru nebo ze vzorků. Persistované hashové
pokrytí je proto z hlediska současného schématu 0 videí; bez nového řízeného
hashing workflow nelze stávající data použít jako důkaz exact duplicate. V této
iteraci nebyl spuštěn hromadný výpočet hashů ani otevřena produkční databáze.

Budoucí V7 Import musí před jakýmkoliv kopírováním na NAS provést duplicate
preflight nad **celým importním batchem**, nikoli až soubor po souboru během
kopírování:

```text
vybraný importní adresář
→ úplná analýza plánovaného importu
→ porovnání se stávající AnimeDB
→ exact / pravděpodobné duplicity / možné náhrady / nejednoznačné položky
→ kompletní import preview
→ rozhodnutí uživatele
→ teprve potom fyzické kopírování
```

Spolehlivý hash celého incoming souboru shodný s hashem existujícího souboru má
být silným důkazem exact duplicate a bezpečný default je **nekopírovat**.
Filename je pouze hint. Samotná shoda collection, season a episode number nikdy
nestačí k automatickému odmítnutí: jiný encode, release, střih, audio nebo
titulky mohou být legitimně jinou verzí. Bez shodného hashe má V7 kombinovat
logickou identitu, délku, velikost, mediální parametry a audio/titulkovou
strukturu a výsledek zobrazit jako pravděpodobnou duplicitu nebo možnou
kvalitnější náhradu.

Pokud uživatel jednou zvolí replacement, bezpečný budoucí tok je ověřit incoming
soubor, zkopírovat jej do staging/cíle, ověřit kopii a integritu, aktualizovat DB
a teprve potom samostatně nabídnout odstranění staré verze. Funkční existující
kopie se nikdy nesmí smazat před ověřením nové. V7 ani toto cleanup workflow
nyní zahájeny ani implementovány nebyly.

Automatické ověření 12. srpna 2026:

```bash
.venv/bin/pytest -q                       # 278 passed
.venv/bin/python -m compileall app tests  # prošlo
načtení všech Jinja2 šablon               # 12 šablon, prošlo
git diff --check                          # prošlo
```

---

## 6.19 Klasifikace videí, vratné zařazení a rychlé číslování

Kontrola reálné kolekce `Arifureta Shokugyou de Sekai Saikyou (L19-Z22)`
ukázala, že typ doplňkového obsahu a jeho logické umístění jsou dvě různá
rozhodnutí. Hierarchy Review proto nově umožňuje:

- označit jednotlivé video jako `preview`, `special`, `recap`, `ova`, `bonus`
  nebo `other` a ponechat je v současném `CatalogTitle`,
- přesunout vybraná videa do existující části bez ohledu na to, zda jsou
  standardní, fractional, `00` nebo unknown,
- vytvořit z libovolného explicitního výběru novou lokální doplňkovou část,
- přesunout všechna videa jedné části do jiné; zdrojová část se automaticky
  nemaže,
- po samostatném potvrzení odstranit pouze prázdnou lokální virtuální část bez
  metadat a dalších vazeb,
- před hromadným sekvenčním číslováním zobrazit deterministický náhled mapování.

Minimální persistentní rozšíření je nullable `Video.content_type_manual`.
Současný model dříve ukládal typ `recap` nebo `ova` pouze na `CatalogTitle`;
`Video.file_type` je automatická klasifikace scanneru a není vhodná pro ruční
rozhodnutí. Nové pole je proto nutné, aby například fractional `5.5` mohlo
zůstat uvnitř Season 1 jako ručně vyřešený Recap. Takové video nevstupuje do
standardní completeness, nevytváří mezeru ani samo nedrží kolekci v review.
Databázová změna je idempotentně připravená v `migrate_schema`; produkční
databáze v této iteraci automaticky migrována nebyla.

Ruční zařazení používá stávající `hierarchy_manual_override` a explicitní ID
videí v definici části. Scanner tak zachová klasifikaci, přesuny mezi částmi i
ruční epizodní čísla. Fyzický adresář není autoritou logické hierarchie a scan
ruční rozhodnutí nevrací podle umístění na NASu.

Episode parser navíc bezpečně rozpoznává výhradně explicitní koncový vzor
`OVA P<number>`, včetně `P01`, jako běžné lokální číslo OVA. Samotné `Title
P1.ext` se nerozpoznává a token `S2` před `OVA P1` se nikdy nepoužije jako
číslo epizody. Díky tomu lze bez nové hierarchické úrovně vytvořit například:

- `OVA – Serie 1` typu `ova` s E1–E2,
- `OVA – Serie 2` typu `ova` s E1–E2.

U doplňkového `CatalogTitle` se rozsah E1–E2 může informativně zobrazit, ale
standardní completeness hlavní season se na něj nepoužije. Externí metadata
zůstávají pro OVA, Recap, Preview a další doplňkové části volitelná.

Hromadné číslování řadí výběr stabilně podle natural filename ordering, poté
podle `relative_path` a ID. Než se cokoliv uloží, uživatel vidí přesné mapování
a musí je potvrdit; přepsání existujících ručních čísel vyžaduje další výslovné
potvrzení.

Všechny operace této iterace jsou databázové a prezentační. Nemění filename,
`relative_path`, fyzické soubory ani adresáře na NASu. Nebyla přidána úroveň
`ParentCatalogTitle`, `SeasonGroup` ani jiný strom. Stále jde o fázi
**Stabilizace hierarchie a ladění UI nad reálnou knihovnou** a V6 nebyla
zahájena.

## 6.20 Uzavření suffixové ambiguity potvrzením Season 1

Tato sekce zaznamenává dřívější stav implementace. Od změny popsané v části
6.37 se legacy period hint už jako hierarchy ambiguity ani review reason
nepoužívá; confirmation workflow zůstává platné pro jiné skutečné nejasnosti.

Při kontrole `Asobi Asobase (L18)` se ukázalo, že potvrzení návrhu **Nastavit
jako Season 1** sice persistovalo `season_number_manual=1`,
`season_label_manual="S1"`, `part_type_manual="season"` a
`hierarchy_manual_override`, ale neaktualizovalo stav ani původní
`CatalogCollection.hierarchy_note`. Detail proto mohl vedle ručně určené Season
1 stále ukazovat historický důvod „Interní časový rozsah neurčuje bezpečně
hranice sezón nebo částí.“ Formulář celkového stavu navíc tento předvyplněný
reason znovu odesílal.

Confirmation workflow nyní považuje ručně uložené strukturální údaje a jejich
ověření za odpověď na původní otázku. U čisté kolekce nastaví `CatalogTitle` i
`CatalogCollection` jako ověřené, odstraní vyřešený suffixový reason a select
po novém načtení odpovídá uloženému stavu `verified`. `local_period_hint="L18"`
zůstává zachovaný a v UI se nadále zobrazuje jako informační interní suffix;
samotný suffix stále není zdrojem automatického odhadu sezóny.

Nejde o obecné pravidlo, že `verified` potlačuje každé varování. Vyřešení se
odvozuje z existujících ručních strukturálních polí titulu, nikoli pouze ze
statusu kolekce. Nové nezařazené video, unknown, nevyřešené `00` nebo fractional
číslování, mezera či duplicita mohou kolekci znovu přepnout do
`review_required` s novým aktuálním důvodem. Po ručním vyřešení těchto nových
problémů lze kolekci znovu konzistentně ověřit.

Oprava nepřidává databázové pole ani migraci, nepřepočítává existující epizodní
hodnoty při samotném potvrzení, nemění produkční databázi ani fyzické soubory a
adresáře na NASu. V6 zůstává nezahájena.

## 6.21 Editovatelné potvrzení typu a čísla sezóny

Ruční kontrola ukázala, že kolekce s jediným `CatalogTitle` nemusí představovat
Season 1: samostatná fyzická složka nebo samostatně pojmenované anime může být
Season 2, Season 3 nebo jiná část. Původní confirmation helper přesto natvrdo
ukládal `season_number_manual=1`, `season_label_manual="S1"` a
`part_type_manual="season"`.

Hierarchy Review nyní před potvrzením zobrazuje editovatelný typ části, číslo
sezóny a označení sezóny. Podporované scénáře zahrnují Season 1, Season 2,
Season 3 i `part_type_manual="season"` s neurčeným
`season_number_manual=NULL`. Automatické číslo z adresáře, interní suffix,
pořadí nebo skutečnost, že jde o jediný titul, se mohou použít pouze jako návrh
formuláře a samy nic nezapisují do ručních polí. Po explicitním ručním potvrzení
typu části jsou manuální číslo a label autoritativní i jako `NULL`; prázdná
hodnota proto znovu neaktivuje dřívější automatický folder hint.

Po potvrzení například Season 2 se uloží `season_number_manual=2` a label S2,
nastaví se existující `hierarchy_manual_override` a ruční rozhodnutí přežije
následný scan. V tehdejším chování se tím uzavírala suffixová nejednoznačnost;
od části 6.37 samotný suffix žádnou hierarchy nejednoznačnost nevytváří.
Confirmation nadále řeší jiné skutečné nejasnosti. Display title, lokální název,
`ExternalTitleLink`, filename a `relative_path` jsou na čísle sezóny nezávislé a
confirmation je nemění.

Nebyla přidána databázová tabulka, sloupec ani migrace. Produkční databáze a
fyzická struktura NASu se nemění a V6 zůstává nezahájena.

Automatické ověření 12. srpna 2026:

```bash
.venv/bin/pytest -q                       # 271 passed
.venv/bin/python -m compileall app tests  # prošlo
načtení všech Jinja2 šablon               # 12 šablon, prošlo
git diff --check                          # prošlo
```

## 6.22 Seskupování anime rootu a ruční collection reassignment

Audit reálné knihovny potvrdil, že současný datový model je pro problém
dostatečný. `CatalogCollection` představuje hlavní anime nebo uživatelskou
logickou collection, `CatalogTitle` představuje jednotlivou season, film, OVA,
Specials, bonus nebo jinou supplementary část a `Video` zůstává fyzickým
záznamem souboru. `CatalogTitle.catalog_collection_id` je nullable FK a celé
CatalogTitle lze bezpečně přesunout změnou této vazby; video nadále ukazuje na
stejné `catalog_title_id`. Nebyla přidána další hierarchická úroveň.

Původní scanner rozpoznával jako child část pouze číslované Season/Series/Part/
Cour složky, `OVA`/`OAD`, `Special(s)` a skupinu sourozenců s římským suffixem.
Ostatní child složky propadly do `determine_parent_series`, které bez známého
strukturálního adresáře zvolilo nejhlubší fyzický adresář. Proto například
`NC`, `OP`, `Movies` nebo `CM&PV` mohly vzniknout jako samostatná hlavní
collection. Referenční `Mob Psycho 100` fungoval správně právě proto, že jeho
`Season 1`, `Season 2`, `Season 3` a `OVA` patřily do bezpečně rozpoznaných
vzorů.

Scanner nyní určuje anime root z fyzické ancestry a prvního bezpečně
rozpoznaného child partu. Pod jedním rootem automaticky seskupí:

- číslované `Season`, `Series`, `Serie`, `S`, `Part` a `Cour` složky;
- `OVA`, `OAD`, `Special` a `Specials`;
- `Bonus`, `Bonuses`, `Extra`, `Extras`, `NC`, `NCOP`, `NCED`, `OP`, `ED`,
  `Preview`, `PV`, `Recap`, `Movies` a `Films` s mapováním na existující typy;
- filmový root označený `(FILM)` a jeho `CM&PV` jako dva CatalogTitle stejné
  collection;
- vnořenou supplementary složku, například `Season 1/NC`, stále pod anime
  rootem, ale jako samostatný supplementary CatalogTitle.

Nové content type enum hodnoty pro `NC`, `OP` ani `ED` nevznikly. Používá se
existující obecný `bonus`; `Movies` používá `film`, `PV` používá `preview` a
nejistý obsah lze nadále ručně klasifikovat existujícími typy. Vlastní název
season, například `High School DxD Born`, není důvodem pro samostatnou hlavní
collection, pokud je child složkou parentu se shodným celým základem názvu.
Taková část se seskupí, ale bez odhadu čísla season a collection zůstane v
review s důvodem k ručnímu potvrzení. Pouhá podobnost názvů bez společné
ancestry nic automaticky neslučuje.

Hierarchy Review má tři odlišné DB operace:

1. **Vytvořit hlavní anime / collection** vytvoří lokální CatalogCollection bez
   povinných externích metadat a přesune do ní vybrané existující CatalogTitle.
2. **Sloučit collections / přesunout části** přesune všechny nebo jen vybrané
   CatalogTitle do existující collection.
3. Původní **merge CatalogTitle** zůstává samostatnou operací, která přesouvá
   videa mezi CatalogTitle. UI výslovně vysvětluje rozdíl.

Collection grouping nemění `Video.catalog_title_id`, filename, `relative_path`,
season/episode numbering, `content_type_manual`, manual display title,
`TitleMetadata`, `ExternalTitleLink`, kandidáty ani artwork. Aktualizuje pouze
`CatalogTitle.catalog_collection_id`, redundantní
`Video.catalog_collection_id` a příznak existujícího
`hierarchy_manual_override`. Scanner i idempotentní startupová synchronizace
tento ruční assignment respektují. Uživatel může později část přesunout zpět
nebo do jiné collection, takže rozhodnutí je vratné bez manipulace s NASem.

Po přesunu posledního CatalogTitle zůstává zdrojová CatalogCollection prázdná a
nikdy se nemaže automaticky. Hierarchy Review ji zobrazí odděleně a odstranění
vyžaduje explicitní potvrzení. Backend před smazáním ověří, že collection nemá
žádné CatalogTitle ani Video; collection model nemá vlastní externí metadata
ani další metadata relationships. Scanner prázdnou ručně opuštěnou collection
nesmaže.

Přehled navíc vytváří konzervativní návrhy **Možné společné anime**. Kandidáty
spojuje pouze fyzická ancestry spolu s bezpečným supplementary/season-like
folderem nebo příbuzným základem názvu; samotná string similarity nestačí.
Detail návrhu ukazuje collections, CatalogTitle, jejich `relative_root_path` a
počet videí. Uživatel může vytvořit/vybrat hlavní collection, přesunout vybrané
části nebo zvolit **Ponechat jako samostatné anime**.

Odmítnutí návrhu se ukládá do malé tabulky
`collection_grouping_decisions` jako hash identity návrhu, fingerprint jeho
aktuálních collections/titles/videos a rozhodnutí `separate`. Stejný nezměněný
návrh se znovu nezobrazuje; přidání nebo přesun části či změna relevantního
video/duplicate stavu změní fingerprint a dovolí novou kontrolu. Volba sloučit
se persistuje především existujícím ručním assignmentem na CatalogTitle a
přežije session, restartovou migraci i scan. Nová tabulka je jediná změna
datového modelu a vzniká idempotentně přes `Base.metadata.create_all`; nebyl
potřeba nový hierarchy subsystem ani nový sloupec CatalogTitle.

Collection reassignment fyzickou duplicitu nezakrývá. Dvě různé
`relative_path` zůstávají dvěma Video záznamy a případný
`duplicate_of_video_id` se při přesunu CatalogTitle nemění. Shodný
`relative_path` je nadále unikátní a opakovaný scan nevytvoří druhé Video.
Hierarchy Review proto u případu typu `Uzaki-chan Season 2` umožní podle cest a
epizodního číslování rozlišit chybné collection assignment od dvou skutečných
fyzických kopií; confirmed duplicate workflow zůstává samostatné.

Praktický výsledek pro známé případy:

- `High School DxD (Z12-J18)` seskupí bezpečné Season/Specials/NC children pod
  parent root; vlastní názvy `New`, `Born` a `Hero` se stejným základem jsou
  reviewovatelné CatalogTitle a existující rozdrobení lze ručně sloučit;
- `OVERLORD (L15-L22)/OVERLORD I–IV` zůstává jednou collection díky římským
  sourozencům a `Movies`/`NC` jsou child části stejného rootu;
- `Tenki no Ko (FILM)/CM&PV` je jedna collection s filmovým a bonusovým title;
- správný `Mob Psycho 100` zůstává jednou collection se čtyřmi CatalogTitle a
  nová logika jej nerozdělí.

Produkční databáze nebyla v této iteraci změněna ani migrována. Pro pozdější
performance audit byla pouze souborově zkopírována do `/tmp` a veškeré měření
proběhlo nad touto pracovní kopií. Fyzické soubory a adresáře na NASu nebyly
změněny. Práce zůstává ve fázi
**Stabilizace hierarchie a ladění UI nad reálnou knihovnou**; V6 ani V7 nebyly
zahájeny.

### Výkon Hierarchy Review

První implementace grouping suggestions obsahovala dvě navazující chyby. Pro
každou aktivní collection procházela všechny ostatní collections kvůli hledání
ancestor cesty a uvnitř každého parent bucketu porovnávala všechny dvojice
názvů. Horší byla následná smyčka pro hledání společného parentu: komponenta
obsahující dvě podobně pojmenované top-level collections z různých root cest
dospěla k `PurePosixPath(".")`. Jeho `.parent` je znovu `.` a podmínka založená
na prefixu cest nemohla být splněna. Worker proto neprováděl pomalý SQL dotaz
ani render; běžel neomezeně v CPU smyčce v
`collection_grouping_suggestions`.

Profil na read-only pracovní kopii skutečné SQLite DB ukázal 197 collections,
235 titles a 3096 videí. Před opravou trvalo eager SQL načtení přibližně
0,215 s a všechny numbering summaries 0,100 s; stack dump po dalších osmi
sekundách stále ukazoval řádek výpočtu common parentu. Delší měření bylo po více
než dvou minutách přerušeno a odpovídá pozorovanému reálnému běhu přes 18 minut.
Candidate discovery před smyčkou provádělo 32 041 globálních ancestor
porovnání a 13 207 sibling name porovnání, celkem 45 248.

Optimalizace předpočítá pro každou collection `PurePosixPath`, normalizovaný
name base a structural příznak jednou. Ancestory nenachází globálním
collection×collection průchodem, ale výstupem po několika skutečných ancestor
segmentech a O(1) lookupem v `relative_root_path -> collection`. Sibling
analýza nejprve zahodí root/generic parent buckety a uvnitř specifického parentu
dále bucketuje podle prvního normalizovaného tokenu. Prefixově příbuzná jména
nemohou skončit v různých token bucketech, takže význam kontroly zůstává stejný.
Společný parent používá konečné `posixpath.commonpath`; nekonečná smyčka už
neexistuje.

Fingerprint nad relevantními collections, titles, videos a duplicate stavem
zůstal významově i deterministicky stejný. Každá disjunktní candidate komponenta
projde pouze vlastní data. Grouping decisions se načtou jediným bulk dotazem a
lookupují podle suggestion key. List route předává již eager-loaded collections
do suggestion helperu místo jejich druhého načtení. Počty videí načítá jedním
agregačním SQL dotazem a numbering summary pro jeden title už nepočítá podruhé.
List view nezobrazuje tisíce jednotlivých video cest; fyzické detaily zůstávají
na detailu collection.

Na stejné bezpečné kopii DB po opravě:

```text
načtení dat                  0,147 s
numbering summaries          0,081 s
grouping suggestions         0,009 s
celý výpočet                 0,237 s
GET + Jinja render           0,402 s
SQL dotazy                   6
candidate comparisons        18 (16 ancestor lookupů + 2 sibling porovnání)
```

Předchozí list path používala 5 eager dotazů a suggestion helper znovu načítal
collections/titles/videos plus decisions, tedy celkem 9 dotazů. Nešlo o klasické
N+1, ale o redundantní bulk načtení; nyní jde o 6 dotazů bez závislosti na počtu
collections. Regresní test vytváří 600 collections a 3000 Video ve 200 parent
skupinách a deterministicky ověřuje právě 1800 candidate operací místo 360 000
globálních dvojic. Čas konkrétního hardware není součástí testovací podmínky.

## 6.23 Supplementary číslování, season context a falešné duplicity

Audit parseru ukázal, že obecný trailing pattern zpracoval poslední číslo dříve,
než numbering a duplicate workflow získaly informaci o druhu obsahu. Soubor
`High School DxD - OVA 01.mkv`, `Special 01.mkv`, `OP 02.mkv` nebo `ED 02.mkv`
proto mohl dostat standardní `season_episode_number` a následně tvořit falešnou
skupinu E01/E02. Duplicate identita navíc dříve používala pouze toto číslo.

Parser nyní před standardními episode patterny kontroluje explicitní,
tokenově ohraničené supplementary suffixy `OVA`/`OAD`, `Special(s)`, `NCOP`,
`NCED`, `OP`, `ED`, `Preview`/`PV`, `Recap`, `Bonus` a `Extra`. Výsledek má
`kind=supplementary`, normalizovaný subtype a odvozené pořadí, které se v UI
zobrazuje například jako `OVA 01`, `Special 01` nebo `OP 02`. Toto pořadí není
zapisováno do standardních episode sloupců: `local_episode_number`,
`season_episode_number`, `absolute_episode_number` a `external_episode_number`
zůstávají `NULL`; diagnostický `episode_number_source` používá například
`supplementary_ova`. Nebyl přidán nový DB sloupec ani content type enum.

Read-only numbering summary explicitní doplňky ignoruje i tehdy, pokud starší
automatický DB záznam ještě obsahuje stale standardní číslo. Supplementary
CatalogTitle má nulový počet standardních epizod, žádný E rozsah, mezery ani
standardní duplicate numbers. Automatický přepočet při scanneru nebo startup
sync staré automatické číslo bezpečně vyčistí; ruční episode override zůstává
autoritativní a automaticky se nepřepisuje.

Duplicate candidate se nyní skládá z celé logické identity:

```text
standard:      episode number
supplementary: subtype + supplementary sequence + season/name/path context
```

Proto `OP 01` a `ED 01` nejsou stejná identita, stejně jako `OVA 01` a
`Special 01`. `S2/OP 02` a `S3/OP 02` odděluje season context. Dvě různé
fyzické cesty se stejným `S2 + OP + 01` naopak zůstávají candidate duplicate a
lze je potvrdit stávajícím workflow; confirmed `duplicate_of_video_id` se
nemění a fyzický cleanup zůstává samostatným problémem.

Season context se neodvozuje z pořadí OP/ED/OVA/Special. Používá pouze:

- přesnou normalizovanou shodu filename prefixu s CatalogTitle stejné
  CatalogCollection, včetně bezpečného odstranění závěrečné interní anotace;
- známý season context aktuálního CatalogTitle, pokud filename prefix odpovídá
  názvu hlavní collection;
- fyzický parent pro doplněk bez title shody;
- jinak vlastní filename prefix jako reviewovatelný name context bez vymyšleného
  čísla season.

Při stromu `NC/High School DxD New/ED 02.mkv`, `NC/High School DxD Born/OP
02.mkv` a `NC/High School DxD Hero/OP 02.mkv` scanner zachová tři oddělené
CatalogTitle `NC – High School DxD New/Born/Hero` v jedné hlavní collection.
Pokud v collection již existují odpovídající season titles, přesná shoda prefixu
přenese jejich potvrzené `S2`/`S3`/`S4`; bez takové shody zůstane vlastní název
a collection je označena k review. Žádná nová hierarchická vrstva nevznikla.

Detail Hierarchy Review zobrazuje u silného filename hintu upozornění
**Pravděpodobně doplňkový obsah**. Uživatel může jediné Video přesunout do
existujícího CatalogTitle nebo vytvořit nový `OVA – S1`, `Specials – S3`,
`NC – High School DxD New` apod. Formulář předvyplní typ, lokální název a pouze
bezpečně známý season context. Stejná cesta je dostupná z podezření na duplicitu
jako **Není duplicita / zařadit jinam** a z obecné správy zařazení. Operace mění
jen `Video.catalog_title_id` a případnou ruční klasifikaci; filename,
`relative_path` ani fyzický soubor se nemění.

Nově vytvořený CatalogTitle používá existující `part_type_manual`,
`season_number_manual`, `season_label_manual` a `hierarchy_manual_override`.
Přesun do existující části označí cílovou hierarchii jako ruční a zachová již
existující `content_type_manual`. Scanner i startup sync proto ruční přiřazení a
season context při dalších bězích respektují. Rozhodnutí lze později opravit
stejným přesunem do jiné části; nevznikla destruktivní migrace starých ručně
potvrzených dat.

Season-context lookup v detailu používá jednu předpočítanou mapu názvů všech
CatalogTitle relevantní collection pro celý request. Duplicate grouping stejnou
mapu sestaví jednou pro danou malou title skupinu. Nevzniklo globální
videos×titles ani videos×videos porovnávání a optimalizovaný list collection
grouping suggestions zůstal beze změny.

Nebyla provedena žádná cílená produkční migrace ani CRUD operace a NAS nebyl
čtením UI nijak fyzicky měněn. Audit kontrol však odhalil, že import globálního
`app.main:app` během testů spouštěl idempotentní startup sync proti `DATABASE_URL`
z `.env`, a tím se změnil mtime produkčního SQLite souboru. Opakování téhož
startup syncu nad pracovní kopií ponechalo SHA-256 bitově identické. Nový
`tests/conftest.py` proto před importem testovacích modulů nastavuje in-memory
SQLite a cesty v `/tmp`; následný celý suite ponechal velikost, mtime i SHA-256
produkční DB beze změny. Práce zůstává ve fázi **Stabilizace hierarchie a ladění
UI nad reálnou knihovnou**; V6 ani V7 nebyly zahájeny.

## 6.24 Bezpečný úklid prázdných CatalogTitle a CatalogCollection

Po ručním přesunu posledního Video z CatalogTitle zůstával prázdný title
záměrně zachován. Původní delete helper dovoloval odstranit pouze ručně
vytvořenou virtuální cestu `/.catalog-part-*` bez jakýchkoli metadat. Reálná
část typu `High School DxD OVA` s původní fyzickou cestou nebo s externím
metadata linkem proto neměla dostupnou bezpečnou delete akci.

Detail CatalogTitle i Hierarchy Review nyní u každého title s nulovým počtem
Video zobrazí **Odstranit prázdnou část**. Akce vyžaduje explicitní potvrzení.
Backend načte title pouze v očekávané CatalogCollection, provede aktuální SQL
`COUNT(Video.id)` a samotný `DELETE` má ještě korelovanou podmínku
`NOT EXISTS(Video)`. Pokud do části mezi zobrazením stránky a delete requestem
přibyde Video, operace skončí lidskou chybou a celá transakce se vrátí zpět.
Filename, `relative_path`, jiné CatalogTitle ani fyzický NAS nejsou součástí
operace.

Audit modelu našel čtyři druhy záznamů vlastněných CatalogTitle:

- `ExternalTitleLink`;
- `TitleMetadata`;
- `MetadataCandidate`;
- `Artwork` reference.

Všechny vztahy mají ORM `delete-orphan` a jejich FK používají `ON DELETE
CASCADE`. Helper je navíc explicitně vyprázdní v téže transakci, aby cleanup
nezávisel na nastavení SQLite `foreign_keys`. Smaže se pouze DB reference
Artwork; případný soubor uvedený v `local_path` se fyzicky nemaže. Test ověřuje
nulový počet orphan záznamů i zachování sentinel cover souboru.

Odstranění posledního CatalogTitle nikdy nemaže jeho CatalogCollection. Ta po
reloadu zůstane v přehledu **Prázdné collections** a teprve samostatná explicitní
collection delete akce ji může odstranit. Původní individuální formulář a jeho
potvrzení zůstaly zachované.

Přehled prázdných collections doplňuje výběrové checkboxy, akce **Vybrat
všechny**, **Zrušit výběr** a **Odstranit vybrané prázdné collections**. Bulk
backend nepoužívá stav stránky jako autoritu. Jedním bulk dotazem načte
collections, jednou agregací počty CatalogTitle a jednou agregací počty Video.
Každý vlastní SQL `DELETE` je navíc omezen dvěma korelovanými `NOT EXISTS`.
Skutečně prázdné položky odstraní, změněné/neexistující přeskočí a redirect
vypíše jména obou skupin. Počet SQL dotazů není závislý na počtu zvolených
collections; nevzniklo N+1.

`collection_grouping_decisions` neobsahuje FK ani ID na CatalogCollection.
Ukládá pouze deterministický hash suggestion key, state fingerprint a volbu.
Smazáním collection proto nevzniká relační orphan ani dereference neexistujícího
objektu. Suggestion builder pracuje pouze s aktuálně existujícími neprázdnými
collections; historický decision zůstane neaktivním auditem a scanner podle něj
collection nevytváří. Význam fingerprintu nebyl změněn.

Scannerová regrese modeluje původní samostatnou `High School DxD Born`
collection, přesune její CatalogTitle do hlavní `High School DxD`, explicitně
smaže prázdný fragment a provede další scan fyzického stromu. Scanner používá
existující ručně potvrzený CatalogTitle a anime root, fragmentovanou collection
znovu nevytvoří, počet Video zůstane stejný a historický grouping decision
nezpůsobí chybu.

Testovací bootstrap nadále směruje globální aplikaci na in-memory SQLite před
importem `app.main`. Produkční DB se při testech nesmí změnit ve velikosti,
mtime ani SHA-256. NAS se nemění a práce zůstává ve fázi **Stabilizace
hierarchie a ladění UI nad reálnou knihovnou**; V6 ani V7 nebyly zahájeny.

---

## 6.25 Ruční podezření na duplicitu bez výběru primary

Při ručním procházení knihovny lze nově označit soubor, který nevytvořil
automatickou duplicate kolizi, ale uživateli připadá jako pravděpodobná
duplicita nebo bordelový kandidát k pozdějšímu prověření. Jde o samostatnou
poznámku na `Video`, nikoli o potvrzení společné identity souborů.

Datový model používá nullable řetězcové pole:

```text
videos.duplicate_status_manual
NULL         uživatel video ručně z hlediska duplicity neoznačil / neposuzoval
suspected    uživatel video označil jako podezřelou duplicitu
```

`NULL` výslovně neznamená „není duplicita“, `keep`, `ok` ani jiný pozitivně
potvrzený stav. Aplikace v této první verzi přijímá pouze `suspected` nebo
návrat na `NULL`. Sloupec nemá databázový `CHECK` omezený na jedinou hodnotu,
takže další skutečné manuální stavy lze v budoucnu přidat rozšířením aplikační
validace a UI bez přestavby SQLite tabulky.

Duplicate workflow nyní rozlišuje tři nezávislé informace:

1. **Automaticky nalezená unresolved duplicita** vzniká jen z kolize stejné
   bezpečně odvozené logické identity a nadále ji počítá
   `unresolved_duplicate_groups`.
2. **Ruční podezření** je pouze
   `duplicate_status_manual='suspected'`; nevyžaduje shodné číslo epizody ani
   výběr primary.
3. **Potvrzená duplicita** vzniká výhradně existujícím potvrzovacím workflow a
   self-reference `duplicate_of_video_id` na ručně vybrané primary video.

Nastavení ani zrušení ručního podezření nemění `duplicate_of_video_id`,
`duplicate_primary_missing`, `CatalogCollection`, `CatalogTitle`, season/part,
episode numbering, `content_type_manual`, `TitleMetadata`, externí vazby ani
technická metadata videa. Potvrzení duplicity ruční podezření automaticky
nepřepisuje a ruční podezření nijak nemění existující automatickou detekci.

Detail `CatalogTitle` a Hierarchy Review nabízejí akci **Označit jako podezřelou
duplicitu** a po uložení badge **Ruční podezření na duplicitu** s akcí **Zrušit
ruční označení**. Zrušení pouze vrátí sloupec na `NULL`. Hierarchy Review
současně používá odlišné badge pro automaticky nalezený problém, ruční
podezření a členství v potvrzené duplicate skupině. Pokud je podezření uložené
na již potvrzené kopii, potvrzený vztah zůstává dominantní; UI vysvětlí, že
ruční stav je samostatná starší poznámka, nenabízí druhé potvrzení a dovolí jen
její zrušení.

Katalogový filtr **Ruční podezření na duplicitu** vybírá pouze videa s hodnotou
`suspected`. Neoznačená videa s `NULL` se do něj nedostanou a uživatel nemusí
potvrzovat každé normální video.

Samostatný katalogový filtr **Všechny duplicity** je sjednocením aktuálních
členů `unresolved_duplicate_groups` a videí s vlastním nenulovým
`duplicate_of_video_id`. Nepoužívá historický příznak ani novou detekci:
vyřešená automatická kolize z něj zmizí, potvrzená kopie v něm naopak zůstane.
Samotné `duplicate_status_manual='suspected'` není důvodem k zařazení a primary
video se nezařadí jen proto, že na něj potvrzená kopie odkazuje.

Idempotentní `migrate_schema` doplní chybějící nullable sloupec a index. Všem
existujícím řádkům SQLite tím logicky zůstane `NULL`; migrace žádné video sama
neoznačuje. Scanner pole nepřepisuje. Implementace nic fyzicky nemaže,
nepřesouvá ani nepřejmenovává a nemění žádný soubor nebo adresář na NASu.
Produkční `data/anime.db` nebyla připojena k aplikaci ani migrována.

Automatické ověření 14. srpna 2026:

```bash
.venv/bin/pytest -q                       # 341 passed
.venv/bin/python -m compileall app tests  # prošlo
načtení všech Jinja2 šablon               # 13 šablon, prošlo
git diff --check                          # prošlo
```

---

## 6.26 Season-scoped supplementary části a priorita anime rootu

Při stabilizaci reálné knihovny byly nalezeny dvě varianty stejné obecné chyby.
Child složky `season 1 L20` a `season 2 P22` nebyly rozpoznány jako seasons,
protože interní časový kód nebyl v závorkách. Složka `Season 2 Shorts (L21)`
současně kombinovala season scope a supplementary subtype, zatímco původní
parser přijímal pouze jeden z těchto významů. V obou případech fallback zvolil
nejhlubší adresář jako anime root a vytvořil samostatnou CatalogCollection.

Parser nyní před určením collection bezpečně rozpoznává:

- explicitní Season/Series/Serie/S token s číslem a koncovým interním kódem
  `[A-Z][0-9][0-9]`, ať je kód v závorkách nebo bez nich;
- explicitní kombinace `Season N`/`S<N>` se známým supplementary tokenem,
  například `Shorts`, `Specials`, `OVA`, `SPs`, `NC`, `OP`, `ED`, `Extras`,
  `Preview`, `Recap`, `Movies` nebo `CM&PV`.

Collection identity se v těchto případech vždy odvozuje ze společného
fyzického parentu před první bezpečně rozpoznanou částí. Metadata zůstávají
vlastností konkrétního CatalogTitle a odlišný oficiální název season proto
nemůže změnit její CatalogCollection. Nezávislé anime rooty se podle podobnosti
názvu ani shodného metadata display title neslučují.

Současný model nepotřeboval novou tabulku ani parent-title vztah. Například
`Season 2 Shorts` se ukládá jako supplementary CatalogTitle s
`part_type=bonus`, `season_number=2` a `season_label=S2`. Lokální název zachová
token `Shorts`; číslování jej díky supplementary part typu nezahrne mezi
standardní epizody. Ručně přesunutý nebo potvrzený CatalogTitle se stávajícím
`hierarchy_manual_override` zůstává autoritativní pro scanner, startup sync i
hierarchy rebuild.

Oprava je založená pouze na ancestry a explicitních strukturálních tokenech.
Nezavádí fuzzy slučování, nemění fyzické cesty a nevyžaduje zásah do NASu ani
produkční databáze.

---

## 6.27 Výrazné doporučení při potvrzení jediné části

Sekce **Ručně potvrdit typ jediné části** v Hierarchy Review nyní nad
editovatelným formulářem zobrazuje výrazný blok **Doporučené zařazení**. Název
doporučení se formátuje přímo z existujícího
`SingleTitleConfirmationSuggestion`: používá jeho `proposed_part_type`,
`proposed_season_number` a `proposed_season_label`. Nebyla přidána druhá
heuristika ani zvláštní pravidlo podle názvu anime.

Pod doporučením se zobrazují pouze již bezpečně známé údaje z existujícího
`TitleNumberingSummary`: počet očíslovaných a standardních epizod, rozsah E,
počet unknown a nestandardních položek. Existující proposal příznak může navíc
uvést podporu metadat `TV / TV_SHORT`. Například čistá jednosériová collection
`Choyoyu P19` s proposal Season 1 a E1–E12 se zobrazuje jako
**Doporučené zařazení: Season 1 (S1)** s důvody
`TV / TV_SHORT · 12/12 standardních epizod · E1–E12 · unknown 0 · nestandardní 0`.

Blok je read-only prezentace. GET detailu nemění manual pole, hierarchy override,
verified stav, metadata, videa ani cesty. Uživatel může předvyplněný typ, číslo
a označení změnit; autoritativní zápis nadále vzniká pouze původním potvrzeným
POST workflow a checkboxem **Potvrzuji uvedený typ a případné číslo této
části**. Pokud `single_title_confirmation_suggestion()` vrátí `None`, UI žádné
doporučení nevymýšlí ani nezobrazuje.

---

## 6.28 Oddělení hierarchie od metadata search query

Výchozí hodnota pole **Hledat metadata** už neskládá název collection s
`Season N` podle `effective_season_number`. Potvrzení `part_type_manual`,
`season_number_manual` a `season_label_manual` tak nadále ukládá autoritativní
hierarchii, ale samo nemění text určený pro hledání metadat.

`default_metadata_search_query()` nyní zachová normalizovaný skutečný lokální
název části. Pouze u názvu, který existující parser bezpečně rozpozná jako čistě
strukturální část (`Season 2`, `Serie 2` a podobně), hledá vhodnější seed v tomto
pořadí: ruční zobrazovaný název konkrétního titulu, známé romaji/anglické/native
metadata konkrétního titulu, legacy metadata display title a čistý název
collection. Hierarchy číslo ani label se v žádném kroku nepřipojují.

Tím zůstává například Choyoyu po potvrzení S1 hledáno jako
`Choujin Koukousei-tachi wa Isekai demo Yoyuu de Ikinuku you desu!`, zatímco
strukturální `season 2 P22` se známými metadaty může dál použít skutečný název
`Peter Grill to Kenja no Jikan: Super Extra`. Nejde o globální odstraňování
řetězce `Season N`: pokud jsou tato slova legitimní součástí skutečného názvu,
zůstanou zachována. Zobrazení detailu ani výpočet seedu nic nezapisují; uživatel
může input dál ručně změnit a teprve stávající metadata search POST provede
vyhledání.

---

## 6.29 Rozšířené statistiky homepage

Statistické karty homepage nyní zobrazují v jednom pořadí počet anime titulů,
běžných epizod, filmů, bonusových/ostatních videí, nezměněné jazykové statistiky
a celkový počet fyzických videí.

**Anime titul** je aktivní `CatalogCollection`, která je zastoupena alespoň
jedním evidovaným videem v existujícím logickém katalogu homepage. Více
`CatalogTitle` jedné collection, například S1, S2 a Shorts, proto stále tvoří
jeden anime titul. Prázdné collections a historická technická root collection
`.` se nezapočítávají.

Film se neurčuje z filename. Video patří do kategorie **Filmů**, pouze pokud je
přiřazeno k `CatalogTitle` s efektivním hierarchy typem `film`; respektuje se
tedy i autoritativní manual hierarchy override. Zbývající `file_type="episode"`
zůstávají běžnými epizodami a ostatní fyzická videa jsou bonusová/ostatní.
V přehledových kartách jsou tyto tři kategorie disjunktní, takže jejich součet
odpovídá `Celkem videí`. Technické statistiky v ostatních tabulkách ani jejich
dosavadní navigace se kvůli této změně nepřestavovaly.

Karta **Filmů** používá stejný odkazový styl jako ostatní rychlé filtry a vede
na `/catalog/films`. Statistiky i katalogový predikát volají jediný
`is_film_video()`, takže automatický i ruční efektivní hierarchy typ mají shodný
výsledek. Mixed collection se ve filtru zobrazí, pokud obsahuje alespoň jednu
filmovou část; její season, OVA, Special ani Bonus videa sama filmovým matchem
nejsou. Karta **Anime titulů** zůstává neklikací.

---

## 6.30 SxxExx parser a explicitní bracketed Special

Parser episode čísel bezpečně rozpoznává tokenově ohraničené tvary `SxxExx`,
včetně `S01E01`, `S1E1`, `S01E01-Title`, `S01E01 - Title`, `S01E01 Title`,
`S01E01_Title` a obdobných oddělovačů mezi season a episode tokenem. Výsledek
`EpisodeNumberDetection` odděluje `season_hint`, standardní episode number,
`filename_episode_hint` a nezávazný `title_candidate` za tokenem. Standardní
video ukládá číslo do dosavadních numbering polí a používá diagnostický source
`sxxexx`; žádný nový DB sloupec nevznikl.

Současný model nemá per-episode metadata ani lokální episode-title sloupec.
`TitleMetadata` patří celému `CatalogTitle`. Filename title candidate se proto
zatím uchovává pouze jako bezpečně znovu odvoditelná hodnota parseru a samotný
filename zůstává beze změny. Candidate nepřepisuje manual display title,
potvrzená metadata ani jinou autoritativní vrstvu.

Prioritní větev před standardním `SxxExx` rozpoznává explicitní `[SP]`. Pro
`S01E14 [SP]-The Common Cold.mkv` vrací subtype `special`, season hint `1`,
původní filename episode hint `14` a title candidate `The Common Cold`.
`supplementary_number` zůstává `NULL`: číslo 14 se nezapisuje do
`local_episode_number`, `season_episode_number`, `absolute_episode_number` ani
`external_episode_number` a nevstupuje do completeness Season 1. Source je
`supplementary_special`. Bezpečně rozpoznaný Special nepotřebuje standardní
canonical episode number a samotné `supplementary_number=NULL` proto neotevírá
hierarchy review; žádné pravidlo „první `[SP]` = Special E1“ neexistuje.

Canonical Special E1 lze již existujícím workflow reprezentovat přes
season-scoped `CatalogTitle` s `part_type_manual=special`,
`season_number_manual=1`, manual hierarchy override a explicitní
`episode_number_manual_override=1`. Původní hint 14 zůstává v nezměněném
filename a parser jej vždy umí znovu získat odděleně od canonical čísla. Budoucí
V6/V7 tak může po potvrzení navrhnout fyzickou strukturu `Specials/E01`, tato
změna ale nic nepřejmenovává ani nepřesouvá.

Pokud automaticky odvozený CatalogTitle představuje například folder S1 a
filename obsahuje `S02E03`, collection dostane konkrétní důvod
`review_required`. Shodné `S01E03` konflikt nevytváří. Explicitně potvrzený
manual hierarchy override má nad filename hintem přednost a scanner jej
nepřepisuje.

Regresní integrační test používá pouze dočasnou knihovnu a in-memory SQLite pro
strukturu `Hataraku Saibou/Serie 1 (L18)`: S01E01 až S01E13 jsou standardní
epizody, zatímco `S01E14 [SP]` je Special s neurčeným canonical číslem. Produkční
databáze ani NAS nejsou součástí testu.

---

## 6.31 Doporučené oddělení explicitního supplementary obsahu

Hierarchy Review nad informacemi z existujícího filename parseru zobrazuje
výrazné **Doporučené oddělení**, pokud je bezpečně rozpoznaný explicitní
supplementary soubor stále zařazený v běžné Season části. Nevznikla žádná nová
fuzzy heuristika: recommendation používá výhradně `is_supplementary`, subtype,
`season_hint`, původní `filename_episode_hint` a `title_candidate`, které už
vrací parser. Manual content type a manual hierarchy zůstávají autoritativní.

Pro `S01E14 [SP]-The Common Cold.mkv` karta ukáže Special, související S01,
původní hint S01E14, filename title candidate a výslovně neurčené canonical
číslo. Více explicitních videí se seskupí jen při shodném současném title,
subtype a parserem bezpečně známém season scope. Standardní `S01E03-Influenza`
žádnou supplementary recommendation nedostane.

**Použít doporučení** je čistě klientské tlačítko bez POSTu. Ve stávající
univerzální **Správě zařazení** označí pouze doporučená videa, zvolí operaci
oddělení do nové části a předvyplní editovatelný typ, season a label. Nevyplňuje
numbering. Teprve původní tlačítko **Provést změnu zařazení** volá existující
backend a autoritativně mění DB zařazení. Libovolný ruční výběr, klasifikace,
přesun i vlastní hodnoty formuláře zůstávají dostupné bez recommendation.

Po vytvoření části `Specials` zůstává `[SP]` video bez canonical čísla; tento
stav je pro bezpečně známý supplementary subtype validní a sám collection do
`review_required` nepřepne. Případné supplementary pořadí je oddělený ordinal,
nikoli canonical standardní epizoda. Vytvoření části současně nemění
`Video.content_type_manual`; video-level override vzniká pouze explicitní akcí
klasifikace. DB schema, fyzické cesty, metadata providery ani duplicate workflow
se kvůli této UI zkratce nemění.

Regresní testy používají in-memory nebo dočasnou SQLite a testovací cesty.
Produkční databáze a NAS nejsou součástí tohoto workflow.

---

## 6.32 Effective numbering v Hierarchy Review

Horní numbering summary už dříve při přítomnosti
`episode_number_manual_override` ignoroval raw nestandardní filename detekci.
Spodní seznamy Hierarchy Review však filename parsovaly znovu bez této priority,
stejně jako samostatný collection review reason. Video opravené z raw `00` na
canonical E01 se proto současně zobrazovalo jako vyřešená standardní epizoda i
jako aktivní `nonstandard_zero`.

Nový centralizovaný `effective_video_numbering()` vrací aktivní klasifikaci,
canonical season číslo, numbering input a zároveň původní
`EpisodeNumberDetection`. Priorita je ruční content/title zařazení, ruční
episode override a teprve potom automatický filename parser. Stejný view-model
nyní používá summary, standard/supplementary/nonstandard/unknown grouping,
collection review reason, seznam oddělitelných nestandardních videí a validace
jejich oddělení.

`High School DxD Hero - 00.mkv` bez override zůstává `nonstandard_zero`, nemá
canonical číslo a drží collection v review. Po autoritativním E01 override je
effective standardní epizodou E1. Ve scénáři s E02–E13 pak Hierarchy Review
ukazuje 13 standardních epizod, 13/13, rozsah E1–E13, unknown 0 a nestandardní
0. Pokud neexistuje jiný nezávislý důvod, review reason zmizí. Stejné pravidlo
platí pro libovolné jiné kladné ručně zadané číslo.

Po uložení jednotlivého i sekvenčního ručního numbering se nově přepočítá také
collection review stav. `episode_number_source` nadále popisuje aktivní zdroj,
takže po opravě obsahuje `manual`; raw stav `zero` zůstává auditovatelný z
nezměněného filename přes parser. Nové auditní DB pole ani schema migrace nebyly
potřeba. Supplementary, manual hierarchy/content a duplicate workflow zůstávají
oddělené a autoritativní.

Regresní testy používají pouze in-memory nebo dočasné SQLite databáze. Fyzické
cesty a NAS se nemění.

---

## 6.33 Trvalé odstranění prázdné ručně definované části

„Jednoduchá definice ručního rozdělení“ nemá samostatný JSON ani samostatný
rule objekt. Každá persistentní cílová položka je přímo konkrétní `CatalogTitle`
s `hierarchy_manual_override=True`; její stabilní identitou je
`CatalogTitle.id` a range/pattern pravidla jsou uložena v jeho `episode_start`,
`episode_end`, `episode_filename_pattern`, manual season/type/sort a numbering
polích. Od Commitu 4A se pouze explicitní video membership ukládá nezávisle v
association `manual_split_rule_videos`, jak popisuje část 6.44. Formulář
serializuje title i tuto persistentní authority.

Zdroj znovuvytváření byl v pořadí startup synchronizace. `migrate_schema()`
nejprve odvodil automatický title pro každou fyzickou hierarchy cestu, následně
aplikoval autoritativní manual split a přesunul všechna videa do ručních částí.
Automatický mezivýsledek však už zůstal označený jako použitý a přežil bez
videí. V dalším UI se pak opět objevil i v jednoduché definici, protože ta
zobrazuje všechny současné titles, nejen manual overrides.

Hierarchy Review nyní rozlišuje prázdnou automatickou část a přímou manual split
entry podle `hierarchy_manual_override`, nikoli podle podobnosti názvu. Pro
manual entry nabízí **Odstranit část i z ručního rozdělení** s explicitním
potvrzením. Definition entry a výsledný title jsou tentýž DB řádek, takže jejich
odstranění proběhne jedním cíleným DELETE v jedné transakci. Backend znovu
ověří nulový počet všech `Video` FK; tím jsou zahrnuta standardní, ručně
zařazená i duplicate videa. Vlastněná metadata/reference používají dosavadní
explicitní cascade workflow. Při chybě route provede rollback.

Po DELETE se přepočítá collection review stav. Startup sync po aplikaci manual
splitu navíc odstraní pouze automatické titles, které v dané collection nemají
žádné přiřazené video, nejsou manual override a nevlastní metadata, odkazy,
kandidáty ani artwork. Používané automatické titles a všechny zbývající manual
entries zůstávají autoritativní. Nejde o globální zákaz rekonstrukce.

Regresní test vytvoří fyzickou `NC` cestu, tři manual entries a ruční přiřazení,
odstraní pouze jednu prázdnou NC entry a dvakrát spustí skutečný
`migrate_schema()`. Odstraněná část se nevrátí; ostatní title ID, pattern,
season/type/numbering overrides, manual episode number, filename a relative
path zůstávají zachované. Nové DB pole ani schema migrace nebyly potřeba.

---

## 6.34 Sjednocená ruční klasifikace celého CatalogTitle

Hierarchy Review nově zobrazuje u každého současného `CatalogTitle` malý formulář
**Typ celé části**. Formulář nabízí stejné manual hierarchy údaje jako detail
collection: `part_type_manual`, volitelné číslo a označení sezóny,
`sort_order_manual` a checkbox **Zařazení ověřeno**. Nezavádí nový paralelní
zápisový endpoint. Odesílá přímo na dosavadní
`POST /collections/{collection_id}/titles/{catalog_title_id}/hierarchy`; parametr
návratu pouze přesměruje uživatele zpět na odpovídající kartu Hierarchy Review.

Validace a zápis tohoto endpointu byly vytaženy do
`set_manual_title_hierarchy()`. Ruční hodnoty nastavují stávající
`hierarchy_manual_override` a `hierarchy_verified_at`, takže zůstávají
autoritativní vůči scanneru, startup sync i hierarchy rebuild. Operace nemění
`Video.content_type_manual`, episode numbering, metadata, collection identity,
filename ani `relative_path`.

Uživatelské title-level typy mají jeden uspořádaný zdroj `PART_TYPE_CHOICES`:
Season, Part, Film, OVA, Special, Preview, Recap, Bonus a Other. Legacy Cour je
nadále backendově čitelný přes validační `PART_TYPES`, ale není nabízen pro nový
autoritativní vstup; generický Title je pouze technický fallback. Seznam používá
detail collection, oba hierarchy formuláře,
jednoduchá definice ručního rozdělení a oba selecty **Oddělit do nové části**.
Backend vytváření nové části přijímá stejnou množinu. Samostatný
`VIDEO_CONTENT_TYPE_CHOICES` zůstává omezený na Recap, Preview, Special, OVA,
Bonus a Other. `Film` tedy zůstává výhradně typem celého `CatalogTitle`; při
vytvoření filmové části se do videí nezapisuje neexistující
`content_type_manual="film"`.

Regresní scénář `Isekai Quartet` ověřuje změnu celé části
`Isekai Quartet - Another World Movie` na `film` přímo přes sdílený endpoint,
rozpoznání videa filmovým filtrem přes `effective_part_type`, zachování Season 1
a Season 2, metadata, filename, `relative_path` a fyzického testovacího souboru.
Následný skutečný `migrate_schema()` manual override nepřepíše. Testy používají
pouze dočasné SQLite databáze a dočasné soubory.

Změna nevyžaduje nové DB pole ani schema migraci. Produkční `data/anime.db`,
produkční scan a NAS nebyly použity ani změněny.

Automatické ověření 18. srpna 2026:

```bash
.venv/bin/pytest -q                         # 419 passed
.venv/bin/python -m compileall app tests    # prošlo
načtení všech Jinja2 šablon                 # 13 šablon, prošlo
git diff --check                            # prošlo
```

---

## 6.35 Podpora M4V ve scanneru

Scanner používá jediný seznam podporovaných video přípon `VIDEO_EXTENSIONS` v
`app/scanner/service.py`. `iter_videos()` podle něj filtruje soubory a stejný
iterátor přímo používá celý `scan_library()` workflow; další duplicitní seznam
video přípon v aplikaci není.

K dosavadním `.mkv`, `.mp4` a `.avi` byla doplněna `.m4v`. Porovnání přípony
zůstává case-insensitive. Rozšíření se týká pouze výběru vstupních videosouborů
pro existující bezpečný scan a `ffprobe`; nemění hierarchii ani databázový model.
Audio, archivy, obrázky, fonty a textové doprovodné soubory se tím nestávají
videem.

Regresní test nad dočasnou knihovnou a in-memory SQLite ověřuje import MKV, MP4,
M4V a AVI a současně odmítnutí MKA, M4A, FLAC, ZIP, RAR, PNG, JPG, BMP, TTF a
TXT. Produkční databáze, produkční scan a NAS nejsou součástí testu.

Změna nevyžaduje DB migraci ani změnu schema.

---

## 6.36 Centrální dynamický jazykový profil videa

`build_video_language_profile(video)` v `app/catalog.py` sjednocuje existující
`AudioTrack`, `InternalSubtitle`, `ExternalSubtitle` a ruční CZ/SK hardsub do
jednoho read-modelu. Nic nepersistuje a nevznikly nové DB sloupce ani migrace.
Raw jazyk audia normalizuje za běhu přes existující `normalize_language()` a
JP audio rozlišuje jako `present`, `missing`, `unknown` nebo `no_audio` bez
odhadování default stopy.

Subtitle profil zachovává více zdrojů stejného jazyka (`internal`, `external`,
`hardsub`). Hlavní stav je `cs_sk_available`, `en_only` nebo `no_subtitles`.
EN fallback se počítá pouze z interního EN streamu; externí EN a subtitle s
neznámým jazykem zůstávají technickým detailem a nemění hlavní stav ani prioritu
doplnění CZ/SK (`none`, `normal`, `high`). Dosavadní `translation_status()`
používá stejný profil a zachovává své veřejné chování v aktuálním UI.

Scanner, `probe_video()`, audio/subtitle entity, UI a produkční data se touto
změnou nemění.

### Budoucí ruční určení jazyka subtitle stopy

Současná pole `language` a `normalized_language` u `ExternalSubtitle` a
`InternalSubtitle` jsou detekované hodnoty spravované scannerem. Budoucí ruční
oprava jazyka se do nich nesmí zapisovat, protože opakovaný scan by mohl ruční
rozhodnutí přepsat. Další implementace má přidat oddělenou nullable ruční
normalizovanou hodnotu a efektivní jazyk číst v pořadí:

```text
manual normalized language
→ detected normalized language
→ unknown
```

První priorita je `ExternalSubtitle`; stejný mechanismus lze později použít i
pro chybná nebo neznámá metadata `InternalSubtitle`. V aktuálním kroku nebylo
přidáno žádné DB pole, migrace, formulář ani zápisová logika a
`VideoLanguageProfile` stále čte současnou detekovanou hodnotu.

### Identita, vazba a filename externích titulků

Budoucí import musí oddělit pět různých informací:

1. identitu fyzického subtitle souboru,
2. detekovaný a případně ručně opravený jazyk,
3. logickou vazbu subtitle souboru na konkrétní `Video`,
4. aktuální filename a cestu,
5. budoucí navržený cílový filename a cestu.

Filename není autoritou logické vazby. Import proto musí umět navrhnout vazbu
například mezi `Anime S01E03.mkv` a `titulky_epizoda_3_final.ass` podle dalších
dostupných informací a před potvrzením ji pouze zobrazit uživateli. Až po ručním
potvrzení vazby může navrhnout cílový subtitle filename odpovídající cílovému
videu. Fyzické přejmenování nebo přesun patří výhradně do budoucího
import/reorganizačního workflow a vyžaduje samostatné explicitní potvrzení.

Současné párování podle shodného filename stemu zůstává beze změny jako
praktické pravidlo normálního scanu již uspořádané knihovny. Nebylo rozšířeno na
fuzzy importní párování ani použito jako jediný obecný zdroj pravdy.

---

## 6.37 Legacy seasonal/year suffix není hierarchy autorita

Značky `Z`, `J`, `L` a `P` s dvouciferným rokem jsou historické uživatelské
časové poznámky: zima, jaro, léto a podzim. Hodnoty jako `P21` nebo `L20-P23`
nepopisují season number, part, cour ani hranice `CatalogTitle`. Varianta v
závorkách, například `(L20-P23)` nebo `( L20-P23 )`, historicky navíc vyjadřovala
„dokoukáno“; tato informace se pro hierarchy nepoužívá a watch-state migrace
nebyla implementována.

`extract_local_period_hint()` a `CatalogCollection.local_period_hint` zůstávají
zachované jako informační základ pro možné budoucí metadata matching. Samotná
přítomnost hintu už ale není větví `collection_requires_review()` a nevyžaduje
`hierarchy_manual_override` jednotlivých částí. Explicitní child adresáře jako
`Serie1`, `Serie2`, `Season 1` a `Season 2` se nadále parsují nezávisle na suffixu
parent collection.

Ostatní review důvody zůstávají aktivní: konflikt season ve filename, explicitní
supplementary obsah bez canonical čísla, nestandardní číslování, chybějící nebo
potvrzené duplicity a souvislá dlouhá řada epizod bez bezpečných hranic částí.
Scanner navíc nadále kontroluje nezařazená videa, konflikty manual splitu a
nejisté seskupení pojmenovaných či season-scoped supplementary částí.

Možné budoucí použití `P21` jako slabého hintu pro Fall 2021 nebo rozsahu
`L20-P23` při skórování metadata kandidátů je pouze roadmapa. V této změně nebyl
period hint zapojen do metadata matching, nebylo změněno DB schema a nebyla
přidána watch-state migrace.

---

## 6.38 Automatic direct-root Season 1 a autoritativní stav hierarchie

Generický `CatalogTitle.part_type=title` je od této změny pouze technický nebo
přechodný fallback. Není legitimním konečným effective structural typem ani
novou ruční volbou. Pokud společná inference nedokáže určit konkrétní typ,
collection zůstane v `review_required` s důvodem k ručnímu určení typu.

Sdílená structural inference používá effective video numbering a volají ji
scanner, startup hierarchy sync, hierarchy rebuild i runtime
`refresh_collection_state()`. Direct-root titul bez manual hierarchy override
se odvodí jako automatic `season`, číslo `1` a label `S1`, pokud obsahuje
nejméně dvě standardní epizody, řada začíná E1, je souvislá a nemá nevyřešenou
duplicitu čísla. Video-level recap, OVA, special, bonus a další supplementary
obsah se do řady ani délkových limitů nepočítá. Inference zapisuje jen
automatická pole; manual pole, `hierarchy_manual_override` a title verification
nemění. Existující manual override se nikdy nepřepisuje.

Stavy collection mají oddělenou semantiku:

- `review_required` znamená aktivní hierarchy, numbering nebo type problém;
- `automatic` znamená bezpečnou automatickou strukturu bez aktivního problému,
  která nebyla autoritativně potvrzena člověkem;
- `verified` vyžaduje konkrétní manual hierarchy snapshot všech částí.

Odstranění posledního review reasonu proto vede na `automatic`. Explicitní
potvrzení současné effective hierarchie uloží její konkrétní typ, season number,
label a pořadí do manual polí a teprve potom může stav přejít na `verified`.
Samotná video-level klasifikace například `04.5 -> recap` hierarchy nepotvrzuje.

Délková kontrola direct-root řady je dvoustupňová. E1–E14 nemá length warning.
E1–E15 až E1–E24 zůstává automatic S1 a UI dynamicky zobrazí pouze neblokující
upozornění, které se neukládá do `hierarchy_note`. Více než 24 standardních
epizod ponechá automatic návrh S1, ale aktivuje safety `review_required`.
Hierarchy Review nabídne potvrzení celé řady jako jedné sezóny přes existující
authoritative confirmation workflow a odkaz na existující ruční split. Počet
epizod nikdy sám nevytváří ani neurčuje hranici sezóny.

Změna nevyžaduje DB schema migraci a nemění fyzické soubory ani adresáře.

---

## 6.39 Season + volitelný Part

Season je primární strukturální osa anime. Part je volitelné logické členění
uvnitř Season a obě osy se ukládají samostatně. Strukturální Part může mít
`part_type=part`, `season_number=1`, `part_number=2`; explicitně rozdělená
Season může zachovat `part_type=season`, `season_number=1` a použít stejnou
samostatnou osu `part_number=2`. Obě varianty se zobrazují jako
`S1 · Part 2`; samostatné `Part 2` bez známého season scope má
`season_number=NULL`, nikoli 2. Vnořená cesta `Season 1/Part 2` dědí season
scope z parentu. `Part 2 != Season 2` platí v parseru, scanneru, startup sync,
rebuildu, numbering i manual/effective vrstvě.

Každý Part je samostatný `CatalogTitle` a může mít vlastní metadata identitu.
Jeden `CatalogTitle` nadále nemá dvě hlavní metadata identity. Automatické pole
`part_number` doplňuje nullable `part_number_manual` a effective čtení používá
manual hodnotu před automatickou. Explicitní potvrzení jedné části i celé
collection snapshotuje také Part ordinal. Manual override chrání scanner,
startup sync i rebuild stejně jako u season number.

Běžné hierarchy formuláře a ruční split zobrazují pro `season` i `part`
samostatné **Číslo sezóny** a **Číslo Part**. Pro jedinou Season je Part
volitelný; více sibling Season titles se stejným `season_number` je jednoznačných
jen s explicitními, neprázdnými a unikátními Part ordinaly. Backend neúplný
nebo duplicitní split odmítne a autoritativní `part` bez čísla Part odmítne
také. Centrální
structural label skládá effective hodnoty (`S1`, `S1 · Part 1`,
`S1 · Part 2`, `Part 2`). Legacy `part_type=cour` zůstává čitelný a backendově
kompatibilní pro staré záznamy, ale Cour není nová hlavní uživatelská volba ani
nemá nové číslo či inference.

SQLite migrace pouze idempotentně přidává
`CatalogTitle.part_number_manual INTEGER NULL`. Nemění existující automatic `part_number`, nevytváří manual
snapshot a neinterpretuje heuristicky historická nejednoznačná data.

Roadmapa V6 používá bez Partu filename `S01E01` a při skutečném Partu
`S01P01E01` / `S01P02E01`. Part folders na NASu nejsou cílově povinné; například
obě řady mohou fyzicky ležet přímo v `Anime/Season 1/`. Fyzické přejmenování ani
přesun není součástí této změny. Vícesouborové fyzické médium řeší samostatný
video-level koncept popsaný níže, nikoli hierarchy Part.

---

## 6.40 Video Media Part

`Hierarchy Part` a `Media Part` jsou nezávislé koncepty. Hierarchy Part je
logická část anime, samostatný `CatalogTitle` a může mít vlastní metadata.
`Video.media_part_number` je nullable autoritativní ordinal fyzického segmentu
jednoho logického CatalogTitle / jedné metadata identity. Jeden film ve dvou
souborech proto zůstává jedním `CatalogTitle(type=film)` a jeho videa mohou mít
`media_part_number=1` a `2`.

Hodnota se nyní nastavuje, mění nebo maže ručně na detailu CatalogTitle v poli
**Část média**. Musí být kladné celé číslo. Operace nemění season number,
hierarchy `part_number`, episode numbering, hierarchy status/note ani metadata
vazbu. Nullable SQLite sloupec `videos.media_part_number INTEGER NULL` přidává
malá idempotentní migrace; existující videa zůstávají `NULL` a žádné historické
hodnoty se neodhadují.

Display helper dynamicky počítá pouze aktivní primární videa stejného
CatalogTitle. Přesná souvislá množina 1..N pro N >= 2 se zobrazuje jako
`Část média X/N` a u titulu jako `N částí média`. Samotné MP1 nebo neúplná sada
MP1+MP3 zobrazí pouze jednotlivé ordinaly bez zavádějícího `/N`. Confirmed
secondary duplicate kopie jsou z počtu i konfliktů vyřazeny. Dvě aktivní
primární videa se stejným ordinalem dostanou lokální neblokující diagnostiku;
Media Part nikdy nevytváří hierarchy review reason.

Scanner `media_part_number` neodvozuje ani nepřepisuje. Rescan zachovává ruční
hodnotu a nové video vždy začíná s `NULL`, i když filename obsahuje `P1`, `P2`,
`CD1`, `Disc1` nebo `MP01`. Budoucí import může tyto tokeny v bezpečném kontextu
použít pouze jako kandidátní heuristiku s preview a explicitním potvrzením.

Roadmapa filename rozlišuje hierarchy identitu `S01P02E03` od fyzického
segmentu `MP01`; případná kombinace je `S01P02E03-MP01`. Fyzické přejmenování,
přesuny ani automatická přestavba existujících titulů nejsou implementované.

---

## 6.41 Lokalizovaná diagnostika Hierarchy Review

Hierarchy Review zobrazuje problém co nejblíže objektu, kterého se skutečně
týká. Společný nebo vztahový problém collection zůstává v horním souhrnu,
problém konkrétního `CatalogTitle` je přímo v jeho kartě a video-level problém
je u konkrétního videa. Bezproblémové karty a videa se neoznačují pouze
proto, že jiná část stejné collection vyžaduje kontrolu.

Centralizovaný dynamický read-model odvozuje z aktuální hierarchy, numberingu
a video diagnostiky collection-, title- a video-level issues. Nepřidává kvůli
tomu DB pole a nemění parser, inference ani význam stavů `automatic`,
`verified` a `review_required`. Uložený `CatalogCollection.hierarchy_note`
při standardním přepočtu nadále reprezentuje agregovaný první blokující důvod
(nebo explicitní ruční poznámku); není zdrojem úplného seznamu. UI proto ze
stejného read-modelu dynamicky vypíše všechny současné problémy a přiřadí je
skutečným objektům.

Horní souhrn uvádí počet problémů a dotčených částí; jednoduché anchory
umožňují přejít na příslušnou kartu nebo video. Blokující problémy jsou
vizuálně odlišené od neblokujících informačních upozornění, například
soft warningu pro flat episodickou řadu E1–E15 až E1–E24. Soft warning stav
collection ani `hierarchy_note` nemění.

---

## 6.42 Sdílené hierarchy evaluation a lifecycle scanneru/startupu

Hierarchy status i lokalizovaná diagnostika vycházejí z jednoho strukturovaného
evaluation modelu se stabilními reason codes, explicitním scope a vazbou na
konkrétní `CatalogTitle` nebo `Video`. `hierarchy_note` zůstává pouze odvozený
uživatelský souhrn a legacy fallback; text poznámky není business identita
problému.

Fresh scanner a startup hierarchy synchronizace nyní ukládají finální stav až
v tomto pořadí:

```text
assignment
→ všechny výsledné CatalogTitle včetně zachovaných legacy titulů
→ automatic structural inference
→ finální přepočet numberingu
→ shared structured hierarchy evaluation
→ hierarchy_status + hierarchy_note
→ commit
```

Tím se gap, canonical duplicate, unknown numbering, long-flat gate i soft warning
vyhodnotí ze stejného finálního numberingu jako při runtime refreshi. Ruční
manual-split conflict/unmatched větev zůstává do samostatného lifecycle kroku
bezpečně uzamčená ve svém dosavadním stavu; tento díl ji nereinterpretuje.

Provenance `related_named_child` a `supplementary_named_child` se nepersistuje do
nového DB pole. Jediný společný helper ji deterministicky znovu odvodí z
uložených `Video.relative_path` pomocí stejného hierarchy parseru jako assignment
a přijme ji pouze tehdy, pokud parserem odvozené collection/title paths stále
odpovídají současnému automatickému přiřazení. Scanner, startup i runtime refresh
proto zachovají stejný stable issue code a scope; manual hierarchy override má
nad tímto automatickým path kontextem přednost.

Sjednocení move/manual-authority write paths dokončil Commit 5 popsaný v části
6.46. Na další samostatný krok zůstávají parserové `S01E05.5` a související
Season/Part numbering edge cases v Commitu 6.

Schema se kvůli structured issues ani path provenance nemění. Scanner ani
startup nadále neodvozují, nemažou ani nepřepisují `Video.media_part_number`.

---

## 6.43 Sdílený manual split evaluator a lifecycle konfliktů

Manual split už nemá samostatnou interpretaci rules ve scanneru, startup syncu
a preview/apply. Čistý evaluator v `app/manual_split.py` nejprve vyhodnotí
všechna pravidla pro každé relevantní video a vrátí strukturované rules,
per-video decisions a vazby na cílové `CatalogTitle`. Teprve hotový výsledek se
aplikuje: jeden match je unique assignment, více matchů je conflict bez
first-match assignmentu a žádný match je unmatched pouze tam, kde aktivní
manual split skutečně vyžaduje nové zařazení.

Persisted lifecycle zachovává současnou hranici autority. Video už bezpečně
přiřazené k jiné části stejné collection se kvůli nesouvisejícímu manual
override nepovažuje za unmatched. Confirmed secondary duplicate a explicitní
supplementary obsah bez vlastního matchu jsou `not_required`; pokud je rule
explicitně vybere nebo odpovídá patternu, používají normální unique/conflict
rozhodnutí. Běžná automatic collection bez manual split targets nevytváří
žádný manual split issue.

Shared hierarchy evaluation dynamicky reprodukuje stabilní video-scoped issues
`manual_split_conflict` a `manual_split_unmatched` přímo z uložených rules,
assignmentu a videí. Issue nese konkrétní `Video`; conflict navíc strukturovaně
odkazuje na všechny matched target titles. Aktivní `manual_split_conflict`
odvozuje collection status `conflict`, zatímco jiné blocking issues nadále
odvozují `review_required`. Konflikt se nededuplikuje s nezávislým numbering,
type ani provenance problémem, takže například conflict a `numbering_gap`
zůstávají současně dostupné v diagnostics. `hierarchy_note` je pouze odvozený
prezentační souhrn a změna jeho textu nemění code, scope, affected objects,
assignment ani status.

Scanner a startup aplikují pouze bezpečné unique assignments ze stejného
evaluatoru a potom vždy pokračují existujícím společným hierarchy finalizerem.
Startup už před úplným vyhodnocením nepřiřadí unassigned video prvnímu
path/manual title; konflikt proto končí s `catalog_title_id=NULL`. Runtime
refresh vyhodnotí persisted rules stejným evaluatorem, takže reprodukovatelný
conflict/unmatched po scanu ani restartu nedegraduje na
`legacy_unlocalized_review_state`. Legacy fallback zůstává pouze pro starý
blocking stav, který současná data a rules skutečně nedokážou lokalizovat.

Preview i apply používají totožný structured result. Apply znovu neinterpretuje
rules vlastní větví; provede assignmenty z preview-compatible rozhodnutí a stav
uloží přes současný shared structural/numbering/hierarchy finalizer. Regresní
scénáře ověřují unique, conflict a unmatched přes fresh scan, runtime refresh,
startup sync a další runtime refresh, shodu preview/apply, odstranění startup
first-match chyby, multiple issues, manual Season i Season+Part snapshoty,
incomplete historický snapshot, confirmed duplicates, supplementary obsah a
zachování `Video.media_part_number`.

Samotný Commit 3 nepřidal DB pole ani migraci. Následující Commit 4A doplňuje
samostatnou persistence explicitních video selections, protože resulting
assignment nemůže bezpečně sloužit jako jejich authority. Pro další samostatné
hierarchy kroky zůstává úplný rebuild, sjednocení obecných
move/manual-authority write paths a parser/numbering edge cases včetně
`S01E05.5`, `S01E14.5v2` a Season/Part absolute-numbering offsetu.

---

## 6.44 Persistentní manual split authority

Explicitní manual-split selection a výsledný assignment jsou nyní dvě různé
business identity. `Video.catalog_title_id` nadále znamená pouze aktuální
výsledné zařazení. Explicitní vazba rule target `CatalogTitle` ↔ `Video` se
ukládá v association tabulce `manual_split_rule_videos` s composite primárním
klíčem `(catalog_title_id, video_id)`. Oba foreign keys mají `ON DELETE CASCADE`
a opačný lookup podporuje index na `video_id`. Stejné video tak může být
autoritativním kandidátem více titles současně.

To opravuje původní ztrátu informace při konfliktu. Pokud explicitní rule A i B
vyberou Video V, evaluator vrátí `manual_split_conflict`, výsledný
`Video.catalog_title_id` zůstane `NULL`, ale association A↔V i B↔V přežijí
apply, reload, scanner, startup sync a runtime refresh. Stejně zůstává conflict
stabilní při kombinaci explicitního A a range pravidla B; po reloadu se
nesmí změnit na unique range match. Unique explicitní selection ukládá
association i výsledný assignment k jedinému targetu.

Preview nadále vyhodnocuje navržené transientní `video_ids` bez zápisu. Apply
použije stejný structured decision, po vytvoření nebo nalezení target titles
přesným set-diffem synchronizuje potvrzené authority vazby, aplikuje výsledné
assignmenty a pokračuje stávajícím shared hierarchy finalizerem. Editace
selection vazby nahrazuje, nehromadí; odebrané páry se smažou. JSON i jednoduchý
formulář čtou `video_ids` z association, nikdy z `title.videos`.

Evaluator už nepovažuje current `Video.catalog_title_id` za rule match.
Persisted explicitní IDs čte výhradně z nové association; range a filename
pattern zůstávají beze změny. Scanner, startup a runtime jsou pouze čtenáři této
authority. Nevytvářejí ji z automatic assignmentu a nemažou ji při
conflict/unmatched; při skutečném smazání Video nebo explicitně povoleném
smazání prázdného target title se association uklidí spolu s rodičem.

Migrace je zpětně kompatibilní a idempotentní. Vytvoří pouze novou tabulku a
index, existující `Video.catalog_title_id` nemění a neprovádí žádný heuristický
backfill. Dvě historické příčiny stejného assignmentu — automatic zařazení a
explicitní selection — stará DB nerozlišuje, proto se žádná z nich automaticky
nepovýší na novou autoritu. Historický current assignment bez dokazatelného
selectoru se bez reprodukovatelného multi-rule konfliktu v persisted evaluatoru
konzervativně zachová jako `not_required`, nikoli jako falešný unique match.
Dva nebo více skutečných range/pattern matchů tato legacy ochrana nepotlačí a
zůstanou `manual_split_conflict`; nezařazené video se nadále řídí dosavadní
manual-split coverage semantikou a společnou diagnostics vrstvou.

Již ztracené pre-4A kandidáty nelze zpětně obnovit. Pokud starý conflict před
migrací vynuloval resulting assignment a žádné jiné persistentní pole původní
explicitní selection nedokazuje, nová prázdná association nemá z čeho konflikt
rekonstruovat. Commit 4A tento historický stav záměrně neodhaduje; přesná
authority vznikne až novým explicitním potvrzením uživatele. Commit 4B proto
může deterministicky rebuildovat nové a znovu potvrzené rules, ale nesmí tvrdit,
že umí zpětně dopočítat již chybějící pre-4A rozhodnutí.

Stable issues `manual_split_conflict` a `manual_split_unmatched`, conflict status
precedence, multiple-issue diagnostika, complete/incomplete manual hierarchy
snapshoty, duplicate a supplementary semantika i `Video.media_part_number`
zůstávají beze změny. Produkční hierarchy rebuild nebyl v Commitu 4A rozšířen.
Následující Commit 4B nad touto persistentní informací implementuje úplný
reconciliation rebuild popsaný v části 6.45; obecné move/manual-authority write
paths zůstávají pro Commit 5.

---

## 6.45 Globální hierarchy reconciliation / rebuild

Původní opravný nástroj nebyl úplným rebuildem: odvozoval pouze část hierarchy,
existující `CatalogTitle` hledal převážně podle `relative_root_path`, nevytvářel
chybějící titles, nepřepočítával globálně video membership a neřešil bezpečný
cleanup obsolete automatic objektů. Nemohl proto obecně dosáhnout stejného
logického výsledku jako fresh scan.

Rebuild je nyní dvoufázová reconciliation služba:

```text
aktuální DB
→ build_hierarchy_rebuild_plan()
→ structured dry-run plan
→ apply_hierarchy_rebuild_plan(tentýž plan)
→ shared hierarchy finalizer
```

Plán obsahuje stabilní source fingerprint, create/update/preserve/remove položky
pro collections a titles, změny obou video assignment vazeb, změny numberingu,
strukturované hierarchy issues, safety blockery a souhrnné počty. User-facing
text CLI není zdrojem business rozhodnutí. Apply plán před zápisem znovu porovná
se zdrojovým stavem a stale plán odmítne. Hard blocker také zastaví celý apply;
nejistota se nepřeklápí do částečného destruktivního výsledku.

Dry-run pouze čte aktuální stav. Nevytváří ORM rows, nemění assignmenty,
neaktualizuje timestampy, nevolá rollback cizí transakce a vrací stejný plán,
který lze následně přímo aplikovat. Apply nevytváří druhou interpretační větev.
Po prvním úspěšném apply vrací další rebuild nad nezměněnou DB nulový logický
diff a při nulovém diffu nepřepisuje timestampy.

Automatic část používá přímo společný `derive_library_hierarchy()` scanneru.
Z něj znovu sestaví grouping collections, identity titles a membership videí,
včetně direct-root Season 1, explicitních Season, nezávislých Season/Part os,
named children a supplementary children. Bonus, Extras, Specials, OVA, OP, ED,
NCOP, NCED, Preview nebo Recap se pouze kvůli názvu child složky nepovyšují na
novou hlavní collection. Chybějící automatic collection/title se vytvoří a
existující čistě automatic title se může přejmenovat, přesunout nebo změnit svou
strukturální identitu, když to současná inference určí jednoznačně.

Každé video dostane výsledný záměr pro collection i title. Apply zapisuje
redundantní `Video.catalog_collection_id` společně s `Video.catalog_title_id` a
ověří invariant, že collection přiřazeného title odpovídá přímé collection
videa. Rebuild nemění `duplicate_of_video_id`, manual suspected stav ani výběr
primary. Confirmed secondary duplicate se nadále nepočítá jako další standardní
epizoda a chybějící primary zůstává strukturovaným problémem. Stejně se zachová
`content_type_manual`, effective supplementary classification a přesná nullable
hodnota `Video.media_part_number`; Media Part není používán jako hierarchy Part.

Manual split vyhodnocuje společný evaluator nad persistentní M:N authority
`manual_split_rule_videos`. Výsledný `Video.catalog_title_id` se nikdy nepoužije
jako zdroj explicitní selection. Unique match přiřadí cílový title, conflict
nepoužije first match a nechá title assignment prázdný, unmatched se řídí
stávající coverage semantikou a supplementary nebo confirmed secondary duplicate
může být `not_required`. Explicit+explicit i explicit+range conflict zachovají
všechny authority vazby. Rebuild je nevytváří z automatic assignmentu, nemaže je
při konfliktu a nemění selection bez uživatelské operace.

Explicitní authority zůstává stabilní i tehdy, když fyzická cesta videa ukazuje
do jiné collection než její target. Scanner a startup sync proto určují manual
collection z M:N authority ještě před physical parser fallbackem. Unique i
conflict přežijí scan/startup a další rebuild bez logického diffu; startup po
redirectu odstraní pouze disposable automatic collection/title, které sám v
tomtéž běhu předběžně vytvořil a které nezískaly žádný assignment.

Historickou pre-4A authority, která už v databázi nemá association ani jiný
dokazatelný selector, rebuild nedoplňuje z current membershipu a nehádá původní
kandidáty. Pokud persisted collection stále nese structured stav `conflict`,
unassigned video nemá vlastní M:N authority a současná rules už konflikt
nedokážou reprodukovat, plán vrátí per-video hard blocker
`historical_pre4a_manual_split_conflict` a current assignment zachová. Ani
unikátní range/pattern match pak není vydán za důkaz, že ztracený explicitní
protikandidát neexistoval. Nekonzistentní persistentní authority, například
orphan target, targety z více collections nebo neplatný persisted pattern/range,
se zveřejní jako strukturovaný hard blocker místo automatické opravy. Samotný
M:N selector na title bez manual hierarchy snapshotu není neaktivní: selector
authority je na strukturální identitě title nezávislá.

Complete manual hierarchy snapshot zůstává autoritativní včetně
`hierarchy_manual_override`, manual Season/Part/type/label/sort fields a
`hierarchy_verified_at`. Incomplete historický snapshot se nedoplňuje, nevymýšlí
se mu chybějící hodnota a společná evaluace jej nepovýší na `verified`.
`CatalogTitle`, který nese manual hierarchy/split authority, manual display title,
metadata record/link/candidate/artwork/preference/lock nebo ruční numbering
konfiguraci, se kvůli změně automatic parseru nemaže. Pokud už nemá bezpečnou
automatic identitu, zůstane zachovaný s review blockerem
`protected_obsolete_title`.

Stejná conservative compatibility hranice je sdílená se scannerem a startup
syncem. Historicky neurčitý conflict se proto po rebuildu nerozpadne při dalším
scanu nebo restartu na physical automatic assignment. Nekonzistentní M:N target
scanner/startup nepřesměrují odhadem; strukturovaný rebuild plan jej následně
zveřejní jako hard blocker.

Odstranit lze jen prázdný obsolete title bez manual authority, metadat,
uživatelských hodnot, numbering konfigurace a dalších relevantních vazeb.
Collection lze odstranit až poté, co po novém plánu nemá title ani video a sama
nenese uživatelský stav. Protože současné schema neumí odlišit ručně uložený
collection review/note od historicky odvozeného textu, non-automatic status nebo
existující note se konzervativně považují za důvod preservation. Prázdná
protected collection se nefinalizuje a její status, note, verification timestamp,
normalizovaný název i period hint zůstávají přesně zachované. Nejasný objekt se
vždy zachovává/reviewuje. Cleanup probíhá až po přepojení videí a ORM vazby se
před delete explicitně synchronizují, takže nezůstávají orphan rows.

Po výsledných assignmentech se odvozený numbering přepočítá společným finalizerem
z fresh filename/manual vstupu. Předem uložený derived numbering proto nemůže
změnit range manual-split rozhodnutí. Potom se stejným finalizerem provede
structural inference, finální numbering, deterministická provenance
`related_named_child` / `supplementary_named_child`, persisted manual-split
context a structured hierarchy evaluation do `hierarchy_status` a
`hierarchy_note`. Rebuild nemá vlastní parser, inference, provenance, numbering
ani status engine.

End-to-end regresní testy porovnávají normalizovaný logický stav dvou nezávislých
DB nad stejným synthetic filesystemem: DB po fresh scanu a stale DB po rebuildu.
Porovnávají collections, titles, Season/Part identitu, membership, numbering,
statusy a structured issues bez databázových ID a technických timestampů. Další
testy pokrývají no-mutation dry-run, apply přesně téhož planu, druhý zero-diff
rebuild, manual split lifecycle, ochranu user dat, cleanup a redundantní FK.

Commit 4B nepřidává databázové pole ani migraci a nespouští fyzické operace nad
knihovnou. Obecné manual move/write paths následně sjednotil Commit 5 v části
6.46. Známé parser/numbering problémy `S01E05.5`, `S01E14.5v2` a Season/Part
absolute-numbering offset zůstávají oddělené pro Commit 6.

---

## 6.46 Sjednocená manual authority a hierarchy write paths

Commit 5 odděluje tři dříve částečně směšované vrstvy:

```text
manual hierarchy snapshot = explicitní strukturální identita CatalogTitle
manual-split selector      = range / pattern / explicitní M:N výběr videa
assignment + status/note   = odvozený výsledný stav
```

Manual hierarchy snapshot má nyní jedno centrální třístavové vyhodnocení:
`none`, `incomplete` a `complete`. Complete authority vyžaduje aktivní override a
strukturálně platný snapshot podle typu; Part například vyžaduje explicitní
`part_number_manual`. Neaktivní manual hodnoty neovlivňují effective hierarchy.
Historický incomplete snapshot se zachová beze změny, automaticky se nedoplňuje
a shared structured evaluation nad ním reprodukuje blocking
`incomplete_manual_snapshot` a `review_required`. Jeho manual hodnoty nejsou
effective hierarchy. Nedestruktivní reconciliation ale zachová dosavadní 4B
membership, automatic pole a numbering projection; tato compatibility hranice
není complete authority, nemůže vytvořit `verified` a sama nevytváří selector
ani assignment authority.

Selector authority je na tomto snapshotu nezávislá. Manual-split režim aktivuje
pouze skutečný range, filename pattern nebo explicitní vazba v
`manual_split_rule_videos`, nikoli samotný `hierarchy_manual_override`.
Explicitní výběry se při uživatelských assignment operacích a manual-split
editaci mění exact set-diffem. Scanner, startup, runtime refresh ani rebuild
nevytvářejí authority z výsledného `Video.catalog_title_id`. Reset
strukturálního snapshotu selector nemaže; editace/reset skutečného selectoru
mění pouze příslušnou selector authority.

Běžné write paths dokončuje malý koordinátor `finalize_hierarchy_write()`. Není
to další hierarchy engine: po hotové mutaci používá existující persisted
manual-split evaluator, sjednotí redundantní Video → Title → Collection FK a
spustí dosavadní shared structural inference, finální numbering,
deterministickou named-child provenance a structured hierarchy evaluation.
`hierarchy_status` a `hierarchy_note` se zapisují pouze výsledkem této evaluace.
Status endpoint proto může `verified` interpretovat jen jako explicitní
potvrzení kompletního snapshotu; ani tento request nepřepíše skutečný conflict
nebo jiný blocking issue. Ostatní přímo zadané statusy se jako autorita
nepersistují.

Opravené write workflows zahrnují potvrzení collection/title hierarchy,
manual hierarchy edit/reset, přesun title mezi collections, vytvoření hlavní
collection, přesun videí do existující či nové title, root-video assignment a
vytvoření root title, manual-split apply/edit, smazání prázdného manual-split
targetu a všechny title/video/bulk manual-numbering operace. Přesun title
zachová existující complete i incomplete manual hodnoty, sám override ani
verification timestamp nevytváří a synchronizuje `Video.catalog_collection_id`
s novou collection. Selector conflict nelze přesunem rozdělit mezi dvě
collections; společný přesun všech targetů zachová conflict authority i
`catalog_title_id=NULL`.

Manual confirmation validuje a připraví všechny snapshoty před jejich aktivací.
Routes používají jednu session/transaction posloupnost `validate → mutate →
shared finalize → commit`; při chybě provedou rollback a nevznikne partial
authority. Manual numbering už nefinalizuje pouze jeden title, ale vždy celou
dotčenou collection nad novým numberingem. Complete manual hierarchy bez
selectorů ponechává nová standardní videa ve standardní structural assignment
větvi; pokud skutečné selectors existují, zůstává v platnosti shared
unique/conflict/unmatched/not-required semantika včetně supplementary a
confirmed-secondary duplicate výjimek.

SQLite connection setup nově zapíná `PRAGMA foreign_keys=ON` pro každé SQLite
spojení vytvořené aplikací standardním SQLAlchemy connect listenerem. Jiné DB
backendy nedostávají SQLite-specific SQL. Schema ani migrace se nemění.
Regresní testy ověřují zapnuté FK enforcement, odmítnutí orphan association a
`ON DELETE CASCADE` cleanup při smazání Video i CatalogTitle; existující
explicitní ORM cleanup zůstává zachovaný.

Regresní sada dále pokrývá complete/incomplete move, neaktivní manual hodnoty,
status write bypass, potvrzení s blockerem i bez něj, atomicitu validation
failure, root workflow a redundantní FK, collection-level manual numbering,
exact selector add/remove/reset, explicitní a range konflikty, override bez
selectorů s novým videem, unmatched/supplementary/duplicate chování, lifecycle
scan/reload/startup/runtime a rebuild dry-run po write operaci. Závěrečná
projektová sada tohoto checkpointu prošla s výsledkem `660 passed`.

Commit 5 nepřidává schema změnu, nemění duplicate ani Media Part semantiku a
neprovádí žádnou fyzickou operaci nad knihovnou. Pro Commit 6 zůstávají pouze
oddělené parser/numbering body `S01E05.5`, `S01E14.5v2`, Season/Part absolute
numbering offset a dříve vymezené navazující edge cases.

Runtime acceptance nad post-4B testovací DB odhalila, že první implementace
zaměnila test complete authority za starší nedestruktivní reconciliation guard.
Scanner, startup a rebuild proto přestaly zachovávat current title i u 60
complete a 47 incomplete historických manual titulů a všech 107 videí pustily do
`automatic_path`; projection následně chtěla vytvořit čtyři child collections,
změnit 17 collections, 13 titles a 66 numbering hodnot. Oprava centrálně
odděluje `manual_hierarchy_snapshot_is_complete()` od
`manual_hierarchy_snapshot_requires_preservation()`: první jediná rozhoduje o
autoritě a `verified`, druhá pouze brání automatické reconciliation zničit
existující explicitně označené complete/incomplete rozhodnutí. Assignment bez
takového markeru nadále není authority a nové video bez selectoru používá běžnou
structural větev.

Pozorovaný nárůst 4B `issues=156` na vadných `issues=203` se skládal z
`incomplete_manual_snapshot +45`, `related_named_child +7`,
`supplementary_named_child +3` a `nonstandard_numbering +1`, zatímco kvůli
nežádoucímu přepočtu současně ubylo `generic_structural_type -7` a
`missing_part_number -2`. Named-child a numbering rozdíly tedy byly důsledkem
211 hierarchy mutations, nikoli zamýšleným rozšířením evaluatoru.

Regresní test nyní prochází lifecycle post-4B stavu se společným anime rootem,
same-base Season/related child, OADs, OVA a Specials přes startup sync a následný
rebuild. Nevznikne child main collection, redundantní FK a assignmenty zůstanou
beze změny a následný rebuild má nulový logical diff. Read-only dry-run původní
historické DB po opravě vrací collections `+0/~16/-0`, titles `+0/~0/-0`,
assignments `0`, numbering `0`, issues `202` a logical changes `16`: osm změn je
oprava derived `automatic -> review_required` a osm pouze aktualizuje primární
note pro collection s blocking historickým incomplete snapshotem. Na
SQLite-backup kopii startup změnil jen tyto derived status/note hodnoty; počet
170 collections, 247 titles, 3100 videos i fingerprint membershipu/numberingu
zůstal shodný a následný dry-run vrátil `+0/~0/-0`, `assignments=0`,
`numbering=0`, `logical_changes=0`, `issues=202`. Proti 4B sadě 156 blocking
issues přibylo 45 `incomplete_manual_snapshot`; protože incomplete manual typ už
není effective, přibyl také jeden `generic_structural_type`.

---

## 6.47 Deterministický fractional a Season/Part numbering

Commit 6 uzavírá parserové a numbering nálezy HIER-07 a HIER-08 bez změny DB
schema, manual authority modelu z Commitu 5 nebo collection/title derivation
enginu z Commitu 4B. Audit parseru potvrdil tuto precedence:

```text
SxxExx [SP]
→ explicitní supplementary sequence
→ fractional SxxExx
→ integer SxxExx
→ obecné explicitní/trailing fractional
→ obecné explicitní/trailing integer
→ čistě číselný bezpečný fallback
```

Původní `SXXEXX_TOKEN` končil před desetinnou tečkou, protože ji jeho boundary
považovala za platný nealfanumerický oddělovač. `S01E05.5` se proto chybně
uložilo jako E5 s title suffixem `5` a `S01E14.5v2` jako E14 se suffixem `5v2`.
Nové specifické fractional pravidlo běží před integer tokenem a integer regex
současně odmítá začátek desetinné hodnoty. Oba příklady nyní zachovají
`season_hint=1`, celé číslo a přesný text fraction; `v2` je pouze plně
spotřebovaný revision suffix. `derive_episode_number()` je záměrně nevrací jako
integer canonical epizodu.

Persisted episode pole zůstávají integer. Fractional pozice se reprezentuje
beze ztráty jako dvojice `number: int` a `fraction: str` v
`EpisodeNumberDetection`; `local_episode_number`, `season_episode_number`,
`absolute_episode_number` a `external_episode_number` zůstávají `NULL` a
`episode_number_source=fractional`. Přesné řazení `5, 5.5, 6` používá pouze
read-time `Decimal`, nikdy binary float ani nový DB typ. Samotná fractional
pozice stále není důkaz Recap, OVA nebo jiného supplementary obsahu.

`recalculate_title_numbering()` je nadále jediný zápis parserového inputu do
derived numbering polí. Ruční episode override má přednost. Bez override se
však automatic vstup nově potlačí i pro effective video-level
`content_type_manual`, takže změna standardní epizody na Recap zachová raw
`local_episode_number`, ale odstraní stale season/absolute/external canonical
numbering; návrat do automatic klasifikace jej deterministicky obnoví. Fractional ručně klasifikovaný jako Recap zůstává přesně
fractional filename pozicí a současně effective supplementary. Confirmed
secondary duplicate zůstává ve fyzické skupině, ale summary/evaluation jej
nepočítá jako další canonical epizodu. Manual numbering se nepřepisuje.

Absolute numbering už nepoužívá jeden `structural_sequence_number`, který pro
Part vybral Part ordinal místo Season. Tituly se řadí lexikograficky podle
samostatných effective os `season_number`, potom `part_number`, pak podle
autoritativního sort fallbacku a stabilní cesty/ID. Proto je pořadí
`S1P1 → S1P2 → S2P1`; kombinace `S1 → S2P1 → S2P2 → S3` funguje bez
persistování umělého Part 0. Standalone `Part 1/2` si ponechává
`season_number=NULL` a řadí se podle Part osy. `S2P1` už není omylem považováno
za první strukturální titul jen proto, že jeho Part ordinal je 1.

Známý absolute offset se skládá z oficiálních episode counts všech předchozích
canonical titulů v tomto pořadí. Supplementary title se do součtu nepočítá a
chybějící metadata supplementary titulu známou canonical řadu nepřeruší.
Explicitní `episode_start_offset`, numbering mode i video-level manual override
zůstávají autoritativní podle dosavadní semantiky. `Video.media_part_number` se
v hierarchy sortu ani offsetu vůbec nepoužívá.

Lifecycle regresní test staví synthetic knihovnu
`Season 1/Part 1`, `Season 1/Part 2`, `Season 2/Part 1` včetně
`S01E01.5v2`. Fresh scan, startup `migrate_schema()`, runtime finalization,
rebuild dry-run/apply a druhý rebuild mají shodný normalizovaný logický stav;
druhý rebuild vrací nulový logical diff. Stejný shared parser a numbering volá
scanner, startup, runtime finalizer i rebuild.

Historická acceptance použila novou SQLite-backup kopii původní
`anime-rebuild-apply-test-2026-08-24.db`; originální testovací DB zůstala
nezměněná. Dry-run před startupem vrátil collections `+0/~16/-0`, titles
`+0/~1/-0`, assignments `0`, numbering `104`, issues `202` a logical changes
`121`. Šestnáct collection změn jsou již zdokumentované Commit-5 derived
status/note opravy. Jediný automatic title update je přímý důsledek opravy
parseru: `Kono Yo ...` má E1–E26 a samostatné E26.5, takže po odstranění falešné
duplicate E26 dosavadní shared direct-root inference bezpečně vrátí Season 1.
Membership se tím nemění.

Numbering rozdíl tvoří 46 nově dostupných hierarchy offsetů, které dříve blokoval
Part ordinal nebo supplementary title bez metadata, 56 oprav offsetu, do něhož
se dříve započetl supplementary metadata count, jedno odstranění stale E13 po
ruční klasifikaci Recap a jedna oprava `S01E26.5-SP` z falešného E26 na
fractional. Collection/title/video počty zůstaly `170/247/3100`; fingerprint
membershipu, manual hierarchy authority a selector authority byl před a po
startup shodný. Po startupu následný dry-run vrátil collections `+0/~0/-0`,
titles `+0/~0/-0`, assignments `0`, numbering `0`, issues `202` a
`logical_changes=0`.

Projected issue rozpad acceptance DB je: `unknown_or_missing_numbering 93`,
`incomplete_manual_snapshot 45`, `generic_structural_type 25`,
`confirmed_duplicate 13`, `nonstandard_numbering 12`, `canonical_duplicate 6`,
`long_flat_series 4`, `soft_long_flat_series 3`, `missing_part_number 2`,
`numbering_gap 1` a `supplementary_without_number 1`. Tři soft warnings nejsou
blocking, proto summary uvádí `issues=202`.

Regresní sada pokrývá `S01E05.5`, `S01E14.5v2`, integer/revision/zero baseline,
negativní tokeny, exact fractional sort, obě smíšené Season/Part struktury,
standalone Part, supplementary offset, stale effective content numbering,
confirmed secondary duplicate, manual numbering a izolaci Media Part. Cílená
hierarchy/parser/numbering sada prošla `427 passed`; celý projektový suite
`679 passed`. Commit 6 nemění schema, manual/split authority ani fyzické cesty.
Produkční `data/anime.db` ani NAS nebyly použity pro write test.

---

## 6.48 Effective audio a subtitle availability

Commit 7 sjednocuje media language stav videa do dynamického
`build_video_language_profile(video)`. Source data, manual authority a derived
výsledek jsou oddělené:

```text
source
→ ffprobe AudioTrack.language + stream_index
→ InternalSubtitle detected language/title
→ ExternalSubtitle detected language + relative_path

manual authority
→ AudioTrack.manual_language
→ ExternalSubtitle.manual_language
→ Video.manual_hardsub_cs/sk + manual_hardsub_verified_at

derived read-model
→ effective language jednotlivých stop
→ audio_status
→ subtitle_status
```

Jediný `normalize_language()` používá pro hlavní sledované jazyky canonical
hodnoty `cs`, `sk`, `en` a `ja`; aliasy `cze/ces`, `slo/slk`, `eng` a `jpn`
končí na stejné hodnotě a `unknown` zůstává neznámé. Jazyk se neodhaduje z
pořadí stopy, filename videa, země původu ani disposition flags. Ffprobe
`default`, `forced`, `commentary` a `hearing_impaired` jsou pro základní
availability pouze technická metadata a žádný z nich nemění jazyk ani
nevytváří hardsub.

Audio stopa používá effective precedence `manual_language → detected language
→ unknown`. Před tímto checkpointem scanner při změně media nahrazoval celý
seznam `AudioTrack`, přestože schema už mělo stabilní unique identitu
`(video_id, stream_index)`. Scanner nyní provádí exact sync podle stream indexu:
detekovaný jazyk a codec obnoví, chybějící skutečný stream odstraní, nový přidá
a manual jazyk existující stopy nepřepíše. Stream index se nepáruje fuzzy a
přesun autority na jiný index se neodhaduje.

Audio evaluator rozlišuje:

```text
japanese      → existuje effective ja, i kdyby jiná stopa byla unknown
english_only  → bez ja/unknown a všechny stopy jsou en
other_known   → bez ja/unknown a existuje známý non-EN jazyk
unknown       → bez ja a alespoň jedna stopa je unknown
no_audio      → ffprobe nevrátil žádnou audio stopu
```

`english_only` i `other_known` jsou pouze informace o legitimním dabu. Absence
JP audia není chyba videa a nevstupuje do subtitle problem statusu. `unknown`
vyžaduje ruční kontrolu a `no_audio` je samostatný technický stav, nikoli
automaticky EN/other/unknown language.

External subtitle má stabilní unique identitu `(video_id, relative_path)` a
scanner jej už před změnou aktualizoval na místě. Nové nullable
`ExternalSubtitle.manual_language` proto bezpečně přežije rescan; detected
`language`/`normalized_language` se dále obnovují a effective precedence je
stejná jako u audia. Odstranění override vrátí effective jazyk přesně na
detekovanou hodnotu. Internal subtitle zatím ruční override nemá a při novém
probe se nadále regeneruje z ffprobe.

Subtitle evaluator má jedinou precedence:

```text
preferred             → internal/external effective CS nebo SK,
                         případně explicitní ruční CS/SK hardsub
fallback_internal_en  → bez CZ/SK, ale existuje INTERNAL EN
missing               → bez CZ/SK i bez INTERNAL EN
```

External EN je zachován jako technická jazyková evidence, ale nikdy nesplní EN
fallback. Unknown subtitle samo nesplní preferred ani fallback. Dosavadní
hardsub persistence už bezpečně rozlišuje neposouzeno (`verified_at=NULL`),
ručně potvrzenou absenci (timestamp + oba příznaky false) a explicitní CS/SK
hardsub. Scanner ani ffprobe tato pole nemění. Současný model neukládá obecný
pozitivní „hardsub neznámého jazyka“; preferred proto může ovlivnit pouze
explicitní `manual_hardsub_cs` nebo `manual_hardsub_sk`, nikdy samotná domněnka
existence vypáleného textu.

Detail CatalogTitle nyní u každého videa zobrazuje detected/manual/effective
jazyk audio stop i externích titulků a nabízí jejich nullable override. Audio
stav a subtitle výsledek jsou dvě oddělené sekce; `Pouze EN dab` ani `Jiný dab`
se nezobrazují jako chyba chybějících titulků. Existující ruční hardsub ovládání
zůstalo beze změny.

Idempotentní SQLite kompatibilita přidává pouze dva nullable sloupce:
`audio_tracks.manual_language` a `external_subtitles.manual_language`. Bez
backfillu zůstávají `NULL`; detected source data, hardsub, hierarchie, selector
authority a assignments se nepřepisují. Synthetic lifecycle test ověřuje
`scan → audio/external manual override → změněný ffprobe/detection → rescan →
startup/reload` se zachovanými row ID, authority a derived výsledkem.

Historická acceptance použila novou SQLite backup kopii
`anime-rebuild-apply-test-2026-08-24.db` pouze v `/tmp`. Po startupu měla 3100
videí: audio `japanese=2729`, `english_only=2`, `other_known=16`, `unknown=353`,
`no_audio=0`; titulky `preferred=2395`, `fallback_internal_en=407`, `missing=298`
a jeden detected external `unknown`. Membership fingerprint před/po startupu
zůstal shodný a následný hierarchy rebuild dry-run měl `logical_changes=0`.
Rozdíl numbering fingerprintu odpovídá už zdokumentované Commit-6 startup
normalizaci této původní post-4B DB, nikoli Commitu 7. Na kopii zvolený unknown
audio/external řádek po manual `ja`/`cs` a dalším startupu zůstal efektivně
`ja`/`cs` se stavy `japanese`/`preferred`.

Cílená media/catalog/migration sada prošla `276 passed`; celý projektový suite
`703 passed`. Produkční DB ani NAS nebyly pro write testy použity.

---

## 6.49 Media Check a explicitní CZ/SK workflow

Commit 8 přidává samostatnou stránku `/media-check` a drží tři review oblasti
oddělené:

```text
Hierarchy Review → collection / title / Season / Part / numbering
Metadata Check  → identita titulu a metadata provider
Media Check     → audio, interní/externí titulky, hardsub a language decisions
```

Commit-7 `build_video_language_profile()` zůstává factual source of truth se
subtitle stavy `preferred`, `fallback_internal_en`, `missing` a audio stavy
`japanese`, `english_only`, `other_known`, `unknown`, `no_audio`. Nový
`build_media_check_evaluation()` je samostatná workflow/presentation vrstva.
K factual profilu přidává pouze nullable ruční autoritu
`Video.czsk_availability_manual = NULL | unavailable`:

```text
faktické CZ/SK
→ available bez ohledu na historický manual marker

bez CZ/SK + bez markeru + Internal EN
→ needs_cs_sk_internal_en (otevřený warning)

bez CZ/SK + bez markeru + bez Internal EN
→ needs_cs_sk_no_fallback (otevřený error)

bez CZ/SK + unavailable + Internal EN
→ known_unavailable_internal_en (známý informační stav)

bez CZ/SK + unavailable + bez Internal EN
→ known_unavailable_no_fallback (známý informační stav)
```

Marker tedy netvrdí, že překlad nikdy nevznikne, a scanner jej automaticky
nenastavuje ani nemaže. Později nalezené internal/external CZ/SK nebo explicitní
CS/SK hardsub vždy vyhrají a video se zobrazí jako hotové; pokud faktická stopa
znovu zmizí, uložený marker může opět platit. Clear markeru okamžitě vrátí video
do factual fallback/missing fronty. Partial translation lze řešit výběrem více
řádků a atomickým bulk set/clear, který mění výhradně tento jeden sloupec.

Media Check používá jeden evaluator pro summary, subtitle/audio filtry i řádky.
Faceted counts respektují opačnou aktivní osu, takže kombinace například
`Doplnit CZ/SK + Audio unknown` má shodný počet v kartě a filtru. Search zahrnuje
collection, CatalogTitle, filename, cestu a episode label. Řazení je
deterministické podle collection/title/episode/filename, stránka zobrazuje 50
kompaktních video řádků a na tablet/mobile přepíná přes stávající responsive
card pattern.

Audio severity zůstává doménově oddělená od subtitle problému: `unknown` je
review warning, `no_audio` technický error, `english_only` a `other_known` pouze
informace a `japanese` hotový stav. Hardsub se v hlavní frontě zvýrazňuje jen u
videí bez běžných CZ/SK; neposouzený hardsub u už přeloženého videa sám backlog
nevytváří. Hierarchy Review před tímto checkpointem nemělo hardsub confirmation
formulář, pouze read-only technický údaj pro výběr duplicate primary. Centrální
hardsub, audio-language a external-subtitle-language controls jsou nyní v Media
Check; detail CatalogTitle je nadále může zobrazovat jako lokální video detail.

Idempotentní schema compatibility přidává jediný nullable sloupec
`videos.czsk_availability_manual` bez backfillu. Commit-7 audio/external manual
language authority ani hardsub persistence se nemění. Scanner/rescan a startup
marker zachovávají; hierarchy rebuild projection jej kopíruje jako manual media
data, ale nepoužívá jej pro collection, title, assignment, numbering ani
`hierarchy_status`.

Historická acceptance proběhla na nové SQLite backup kopii v `/tmp`. Po startupu
měla 3100 videí a nezměněný factual rozpad: audio `japanese=2729`,
`english_only=2`, `other_known=16`, `unknown=353`, `no_audio=0`; titulky
`preferred=2395`, `fallback_internal_en=407`, `missing=298`. Počáteční manual
unavailable count byl 0 a workflow proto přesně odpovídal `available=2395`,
`needs_cs_sk_internal_en=407`, `needs_cs_sk_no_fallback=298`. Jeden fallback a
jeden missing řádek se po bulk set přesunuly do odpovídajících known-unavailable
stavů; clear fallback řádku jej vrátil do otevřeného Internal EN workflow a
restart zachoval zbývající marker. Hierarchy fingerprint před/po byl shodný a
následný rebuild dry-run měl collections/titles/assignments/numbering vše nula a
`logical_changes=0`.

Cílená media/web/scanner/migration/responsive sada prošla `155 passed`; celý
projektový suite `716 passed`. Všech 14 Jinja šablon se bezpečně načítá.
Produkční DB ani NAS nebyly pro write testy použity.

---

## 6.50 Bezpečné párování a review externích titulků

Externí subtitle discovery je nově jeden globální účetní průchod, nikoli
nezávislé hledání pro každé video. Automatická vazba vznikne jen při právě jednom
fyzickém video kandidátovi ve stejném adresáři. Exact filename stem má přednost;
teprve bez exact kandidáta se přijme explicitně allowlistovaný jazykový suffix.
Číselné `.5`, revision ani libovolný release token jazykovým suffixem nejsou.
Fuzzy podobnost, normalizace `1` na `01` ani release-name heuristika automatickou
vazbu nikdy nevytvářejí.

Každá podporovaná fyzická subtitle cesta je právě v jednom stavu. Bezpečně
přiřazené soubory zůstávají v `external_subtitles`; jejich `match_method`
rozlišuje `automatic` a autoritativní `manual`. Soubor bez jednoznačné shody je
uložen jednou v `unresolved_external_subtitles` se stavem `unresolved` nebo
`confirmed_no_match`. Globálně unikátní relative path v novém schématu brání
dvojité vazbě jednoho fyzického souboru. Scanner zachovává manual link,
confirmed-no-match i uložené odmítnuté candidate ID; opakovaný scan nevytváří
další řádky. Zmizelý fyzický titulek se odstraní pouze z databázového indexu.

Media Check obsahuje sekci **Nepřiřazené externí titulky**. Candidate pool se
nejprve omezuje na stejný fyzický adresář, následně na bezpečně známý
CatalogTitle, CatalogCollection nebo anime root. Similarity a episode hint pouze
řadí nejvýše dvanáct zobrazených návrhů. Uživatel může konkrétní video ručně
přiřadit, kandidáta persistentně odmítnout, potvrdit absenci odpovídajícího
videa nebo rozhodnutí znovu otevřít. Žádná z těchto akcí nemění NAS.

Read-only ověření fyzické knihovny našlo 2383 externích titulků: 2377 má přesně
jednu exact-stem shodu a 6 zůstává pro ruční rozhodnutí. Čtyři historicky
ambiguous fractional soubory mají po exact precedence vždy pouze svůj `.5`
video protějšek. Ambiguous automatic attachments, double-linked fyzické cesty i
unaccounted soubory jsou nulové. Testovací a migrační scénáře používají pouze
dočasné knihovny a SQLite databáze; produkční DB ani NAS se nemigrují ani
nemění.

---

## 6.51 Nečíslované supplementary markery a season context

Bezpečný explicitní marker určuje supplementary subtype nezávisle na ordinalu.
Například `Title OVA.mkv` a `Title OVA 01.mkv` jsou oba OVA; první nemá
supplementary pořadí a druhý má ordinal 1. Ani jeden tím nezískává canonical
episode number. Stejná parserová větev obsluhuje také již podporované OAD,
Special, OP, ED, NCOP, NCED, PV/Preview, Recap, Bonus/Extra, CM a Menu.
Neznámé `[IV01]`/`[IV02]` zůstávají mimo tuto explicitní množinu a nadále
vyžadují review.

`CatalogTitle.part_type` popisuje druh obsahu, zatímco nullable
`season_number`/`season_label` jsou nezávislý strukturální context. OVA, Special,
Bonus nebo Film proto mohou souviset s konkrétní sezónou, aniž vstoupí do její
standardní completeness; bez bezpečně známého vztahu se season nevymýšlí.
Rozhodnutí, zda několik supplementary souborů sdílí jeden metadata titul, není
odvozováno pouze ze společného subtype a zůstává na Hierarchy Review/Metadata
Check. Budoucí V6/V7 smí tuto potvrzenou strukturu použít jako autoritu, ale tato
změna nic na NASu nepřesouvá ani nepřejmenovává.

Startup compatibility synchronization znovu aplikuje classifier a shared
numbering/evaluation nad existujícími řádky. Po nasazení parserové opravy proto
pro stale `episode_number_source=unknown` stačí restart aplikace; produkční scan
ani nové `ffprobe` nejsou potřebné.

---

## 6.52 Metadata-driven split potvrzené lokální části

Odpovědnost Hierarchy Review a Metadata Check je nyní explicitně oddělená.
Hierarchy Review nadále zobrazuje všechny skutečné `CatalogTitle`, jejich
strukturální typ, season/Part context, číslování, duplicate stav a skutečné
hierarchy ambiguity. Per-video helper **Pravděpodobně doplňkový obsah** však už
nevznikne pouze z OVA/Special/OP/ED/NC markeru, pokud aktuální supplementary
`CatalogTitle` má kompletní autoritativní manual hierarchy snapshot. Doporučení
pro explicitní supplementary soubor chybně ponechaný v Season části a nezávislé
diagnostické kontroly zůstávají zachované.

Rozdělení lokální skupiny podle identity externího titulu patří do Metadata
Check. Samostatný read-only evaluator vrátí doporučení pouze tehdy, když současně:

1. existuje primární ručně potvrzený `ExternalTitleLink` s `verified_at`,
2. odpovídající `TitleMetadata` má shodný provider/external ID a kladný
   `episode_count=N`,
3. `N` je menší než počet lokálních logických videí; shodný nebo vyšší počet
   split nevyvolá,
4. všechna lokální videa tvoří právě jednu úplnou, bezduplicitní číselnou řadu
   `1..M`,
5. subset `1..N` i zbytek `N+1..M` jsou neprázdné a jednoznačné,
6. skupinu nekomplikuje Media Part nebo potvrzená/poškozená duplicate vazba.

Jde o numbering match, nikoli o řazení filename nebo výběr „prvních N souborů“.
Pokud metadata count ukazuje na menší rozsah, ale některé číslo chybí, je
duplicitní, subtype se míchají nebo je položka nečíslovaná, Metadata Check ukáže
nejednoznačnost bez akce. Bez potvrzené metadata identity nevznikne doporučení
ani ambiguity pouze z parseru. Stejná konzervativní semantika pracuje s OVA,
Special, Bonus a Film částmi; konkrétní manual hierarchy typ a season/anime
context se při splitu kopírují, nevymýšlejí.

Detail Metadata Check zobrazuje metadata title, typ a season context, přesný
seznam přesouvaných videí a přesný seznam zůstávajících videí. Akce
**Rozdělit podle potvrzených metadat** je POST s povinným explicitním potvrzením
a před zápisem celý stav znovu vyhodnotí. Teprve potom:

- vytvoří nový virtuální `CatalogTitle` s kompletním manual hierarchy snapshotem,
- uloží explicitní M:N selector authority pro přesouvaná videa,
- přesune pouze subset `1..N`, primární potvrzený link, normalizovaný metadata
  záznam, odpovídající confirmed candidate a artwork,
- zachová `part_type`, season number/label, automatic `Video.file_type`, manual
  video classification i fyzické `relative_path`,
- ponechá původní neprázdný `CatalogTitle` se zbytkem videí a bez vymyšlených
  metadat pro další Metadata Check,
- odmítne transakci, pokud by nový selector překryl existující range/pattern
  manual-split authority.

Persistentní explicitní selector používá stejný lifecycle jako dosavadní ruční
zařazení. Regresní test ověřuje nový DB session, idempotentní startup sync a
normální rescan nad dočasnou knihovnou. Žádná databázová migrace ani nový sloupec
nevznikl. Workflow nemění filename, fyzický soubor ani adresář na NASu a
neprovádí automatický split.

Automatické ověření 25. srpna 2026:

```bash
.venv/bin/pytest -q                       # 877 passed
.venv/bin/python -m compileall -q app tests  # prošlo
načtení všech Jinja2 šablon               # 14 šablon, prošlo
git diff --check                          # prošlo
```

---

## 6.53 Autoritativní local_title a strukturální pořadí

Ručně zadaný `CatalogTitle.local_title` je lokální strukturální identita. Create,
move, manual split, Metadata Check split, potvrzení metadat, startup sync ani
scanner jej nesmějí nahradit názvem prvního videa nebo externím canonical
názvem. Metadata nadále mohou podle existující priority určovat display title,
ale uložený lokální název zůstává fallbackem a budoucím podkladem pro V6.

Nové logické části používají jeden shared naming resolver. Neprázdný ruční
vstup má vždy přednost. Bez něj resolver přijme parserový strukturální název jen
tehdy, když všechny vybrané cesty dokazují stejnou část ve stejné collection;
například `NC/High School DxD Born/` vede obecně k `NC – High School DxD Born`.
Čistě strukturální folder label ani první `OP`/`ED` filename se jako identita
nepoužijí. Poslední fallback je deterministický typ a známý season context,
například `Special – S3`, případně samotné `Bonus` bez season.

`sort_order_manual` nyní znamená výhradně explicitní uživatelský override.
Automatické potvrzení současné hierarchy, potvrzení jediné části, create,
manual split, root-video create ani metadata-driven split už nekopírují
`effective_sort_order` a nevyrábějí sekvenci podle pořadí kliknutí. Prázdné UI
pole zůstává `NULL` a je označené jako automatické strukturální řazení.

Běžný detail collection a sdílené title selection používají centrální klíč:

1. explicitní `sort_order_manual`, pokud existuje v kompletním manual snapshotu,
2. známý season context před anime-level contextem a vzestupné číslo sezóny,
3. stabilní rank z `PART_TYPE_CHOICES` (Season/Part před supplementary typy),
4. skutečný `part_number`, pokud existuje,
5. `local_title`,
6. stabilní ID pouze jako poslední tie-breaker.

Aktivní read-only audit ukázal automatické `sort_order` hodnoty i 14 nenulových
`sort_order_manual` hodnot. U virtuálních High School DxD částí byly hodnoty
1–5 shodné s `.catalog-part-1` až `.catalog-part-5`, což potvrzuje původní
workflow závislost na pořadí operací. Schema však nemá samostatný provenance
flag, který by bezpečně odlišil ruční legacy hodnotu od historicky automaticky
vytvořené. Proto nevznikla migrace ani backfill a všechny existující hodnoty
zůstávají nedotčené; oprava pouze zabraňuje vzniku dalších falešných override.

Metadata Check split přijímá volitelný explicitní lokální název. Prázdný vstup
použije stejný bezpečný resolver, nový title má `sort_order_manual=NULL` a
zachovává season/type/video classification i persistentní selector authority.
Restart a normální testovací rescan zachovávají local title, membership i
strukturální pořadí. Hierarchy Review stále renderuje každý skutečný
`CatalogTitle` samostatně ve stejných kartách; nevzniklo season grouping,
skrývání ani nový prezentační koncept. Změněn byl pouze popisek order inputu na
**Ruční pořadí** s vysvětlením automatic režimu.

Canonical display resolver zachovává nejvyšší prioritu ručního display title a
preferovaných metadat. Bez nich použije u kompletního autoritativního
supplementary snapshotu jeho `local_title` před filename-derived kandidátem
jednotlivého videa. OP/ED/OVA marker tak nepřejmenuje celou potvrzenou NC, OVA,
Special nebo jinou supplementary část pouze při renderování. U Season, Part a
nepotvrzené inference zůstává dosavadní bezpečný filename fallback beze změny.

Změna nepřidává DB sloupec, nemění aktivní `data/anime.db`, neprovádí produkční
scan a nic nepřejmenovává ani nepřesouvá na NASu.

Automatické ověření 26. srpna 2026:

```bash
.venv/bin/pytest -q                       # 891 passed
.venv/bin/python -m compileall -q app tests  # prošlo
načtení všech Jinja2 šablon               # 14 šablon, prošlo
git diff --check                          # prošlo
```

---

## 6.54 Hlavní season-context zobrazení

Hlavní katalog nyní používá samostatný read-only prezentační view-model nad
existujícími `CatalogTitle`. Hlavní episodické části jsou effective typy
`season`, `part`, legacy `cour` a technický fallback `title`. Centrální
supplementary taxonomie zahrnuje Film, OVA, Special, Preview, Recap, Bonus a
Other. Neznámý typ se bezpečně zachová jako anime-level další část.

Supplementary `CatalogTitle` se připojí pod hlavní část jen tehdy, když jeho
`effective_season_number` odpovídá právě jedné hlavní části. Filename,
metadata title ani textový label se pro attachment nepoužívají. `NULL`,
chybějící cílová sezóna nebo více hlavních částí se stejným season contextem
ponechá titul v sekci **Další části**. Každý skutečný title je proto v projekci
právě jednou: jako hlavní položka, nested supplementary nebo anime-level extra.

Homepage zachovává přímý detail jedné hlavní sezóny, pokud collection nemá
anime-level sibling; season-scoped OVA/Special/Bonus/Film tento shortcut neruší.
Více hlavních sezón otevře selector, který zobrazuje pouze hlavní části a
anime-level extras. Detail hlavní sezóny nechá epizody nahoře a pod nimi renderuje
navázané typové skupiny přes výchozí sbalené `<details>`. Každý nested title i
video odkazuje na původní `/titles/{id}` deep link a používá stávající canonical
display-title resolver.

Veškeré title pořadí prochází `catalog_title_sort_key`; explicitní legacy/manual
override zůstává respektovaný a žádná nová hodnota pořadí se nezapisuje.
Databázová hierarchie, metadata, videos, URL identita ani NAS se nemění.
Hierarchy Review nadále renderuje raw seznam všech `CatalogTitle` ve stejných
kartách; jeho source, templates, grouping a workflow nebyly upraveny.

Read-only projekce aktivní High School DxD collection ID 48 obsahuje 14 titulů:
4 hlavní sezóny a 10 jednoznačně navázaných supplementary částí, bez
anime-level zbytku. S1 obsahuje Special, Bonus a OVA; S2 Bonus a OVA; S3
Special, Bonus a OVA; S4 Bonus a Preview. Legacy `sort_order_manual` hodnoty
zůstávají pouze přečtené centrálním sortem a nejsou migrovány.

Automatické ověření 26. srpna 2026:

```bash
.venv/bin/pytest -q                       # 896 passed
.venv/bin/python -m compileall -q app tests  # prošlo
načtení všech Jinja2 šablon               # 14 šablon, prošlo
git diff --check                          # prošlo
```

---

## 6.55 Souhrn doplňkových videí v season selectoru

Read-only `collection_presentation` nyní pro každou hlavní část odvozuje
celkový počet videí přímo ze stejných `supplementary_groups`, které se po
otevření sezóny renderují dole v detailu. Selector jej zobrazuje ve sloupci
**Doplňkový obsah**. Nenulová hodnota má jednoduchý nativní tooltip s lidskými
labely a počty jednotlivých neprázdných typů; nulová hodnota tooltip nemá.
Počítají se `Video`, nikoli počty `CatalogTitle`.

Anime-level extras nejsou součástí `supplementary_groups` žádné primární
části, a proto se do season součtu nemohou dostat. Taxonomie, attachment i
pořadí typů nadále pocházejí z existujícího centrálního view-modelu;
šablona hierarchii znovu neodvozuje. Aktivní High School DxD collection ID 48
v read-only projekci obsahuje součty S1=15 (`Special 11`, `Bonus 2`, `OVA 2`),
S2=5 (`Bonus 4`, `OVA 1`), S3=10 (`Special 6`, `Bonus 3`, `OVA 1`) a S4=4
(`Bonus 3`, `Preview 1`).

Hierarchy Review, databázové schema, hierarchy data, metadata, order hodnoty,
aktivní databáze ani NAS se touto prezentační změnou nemění.

Automatické ověření 26. srpna 2026:

```bash
.venv/bin/pytest -q                       # 898 passed
.venv/bin/python -m compileall -q app tests  # prošlo
načtení všech Jinja2 šablon               # 14/14, prošlo
git diff --check                          # prošlo
```

---

## 6.56 First-class blocking workflow nezařazených videí

Globální Hierarchy Review nyní v úplně prvním výrazném blocking panelu ukazuje
každé známé `Video`, jehož logická vazba není úplná a konzistentní přes konkrétní
`CatalogTitle` do `CatalogCollection`. Definice není vázaná na fyzický root:
zahrnuje chybějící title, chybějící collection přes title, chybějící nebo
rozpornou redundantní Video → Collection vazbu a legacy technické zařazení k
pseudo-collection `.`. Korektně zařazené video s jiným nezávislým hierarchy
problémem se do tohoto panelu nepřidává; jeho původní review issue zůstává.

Akce **Vyřešit nezařazená videa** vede do společného workflow, které rozlišuje
tři autoritativní operace: přiřazení do existujícího anime a existující části,
vytvoření nové části v existujícím anime a vytvoření nového anime s první
částí. Nová část používá stávající hierarchy typy, season/Part pole, manuální
snapshot, centrální naming a order logiku, explicitní M:N selector authority a
společný hierarchy finalizer. Restartová synchronizace i běžný rescan proto
ruční rozhodnutí zachovají, i když soubor fyzicky leží v rootu nebo v jiné
složce. Úspěšné přiřazení odstraní pouze unassigned blocker; jiné skutečné
review issues se dál vyhodnocují.

`/root-videos` zůstává sekundárním technickým filtrem fyzického kořene a odkazuje
zpět do Hierarchy Review. Assignment stránka používá na běžných desktopových a
tabletových šířkách card layout, takže rozsáhlé formuláře nevynucují horizontální
scrollbar. Databázové schema ani fyzické cesty se nemění.

Automatické ověření 27. srpna 2026:

```bash
.venv/bin/pytest -q                          # 903 passed
.venv/bin/python -m compileall -q app tests # prošlo
načtení všech Jinja2 šablon                  # 14/14, prošlo
git diff --check                             # prošlo
```

---

## 6.57 Physical-root inventář nezávislý na logickém assignmentu

Technický `/root-videos` pohled nyní vybírá výhradně podle skutečné fyzické
pozice (`Video.relative_path` bez parent adresáře). Zobrazuje proto jak
nezařazené root soubory, tak root soubory s kompletním autoritativním
`CatalogCollection -> CatalogTitle` assignmentem. Logicky zařazená položka
ukazuje collection, část a hierarchy typ a nenabízí znovu unassigned formuláře;
nezařazená položka odkazuje do jediného společného `/unassigned-videos`
workflow.

Homepage technický přehled již zařazená root videa nevynechává. Řádek
**Videa v kořeni knihovny** ukazuje celkový fyzický počet a samostatný rozpad na
logicky zařazené a nezařazené položky. Hlavní logický katalog a Hierarchy Review
nadále používají svou vlastní assignment semantiku. Stav „logicky správně
zařazeno, fyzicky stále v rootu“ tak zůstává viditelný jako budoucí vstup pro
V6, aniž by sám vytvářel hierarchy blocker nebo prováděl fyzickou reorganizaci.

Databázové schema, assignment persistence, metadata, collection detail ani
fyzické soubory se tímto prezentačním oddělením nemění.

Automatické ověření 27. srpna 2026:

```bash
.venv/bin/pytest -q                          # 904 passed
.venv/bin/python -m compileall -q app tests # prošlo
načtení všech Jinja2 šablon                  # 14/14, prošlo
git diff --check                             # prošlo
```

---

## 6.58 Collection identity názvy bez supplementary úniku

Homepage nadále používá centrální `catalog_collection_display_title` a globální
preference Romaji/English/Native, ale explicitní per-title název může být
zdrojem identity collection jen z hlavních částí definovaných stávající
hierarchy taxonomií. Ruční nebo metadata display title doplňkové části typu
NC/Bonus proto nepřebije název celé collection jen proto, že je prvním
explicitně pojmenovaným title ve strukturálním pořadí.

Priorita zůstává: ruční `CatalogCollection.manual_display_title`, bezpečný
explicitní název hlavní části s jazykovým fallbackem a nakonec
`CatalogCollection.local_title`. Pokud collection nemá žádnou hlavní Season/
Part/title část, jediný samostatný CatalogTitle zůstává bezpečným zdrojem názvu;
samostatné filmy si proto zachovávají metadata varianty. Více čistě
supplementary částí bez hlavní identity spadne konzervativně na collection
local title. Prázdná legacy collection také používá lokální fallback.

Změna je čistě read-only presentation fix. Nemění collection membership,
hierarchii, detail collection, Hierarchy Review, metadata, databázové schema
ani fyzická data. Homepage zachovává dosavadních deset SQL dotazů.

Automatické ověření 27. srpna 2026:

```bash
.venv/bin/pytest -q                          # 914 passed
.venv/bin/python -m compileall -q app tests # prošlo
načtení všech Jinja2 šablon                  # 14/14, prošlo
git diff --check                             # prošlo
```

---

## 6.59 Effective filmová klasifikace videí z hierarchy

Uložené `Video.file_type` nadále přesně znamená automatický výsledek filename
parseru a scanner jej při novém nebo změněném souboru může deterministicky
obnovit. Parser záměrně neklasifikuje video jako film jen podle slova `Movie`
ani podle názvu parent složky. Film s neprůkazným filename proto smí mít raw
`file_type="other"`, aniž je jeho uživatelská klasifikace chybná.

Společný effective resolver používá prioritu explicitní
`Video.content_type_manual` → přesný supplementary
`CatalogTitle.effective_part_type` → parserový `Video.file_type`. Film, OVA,
Special, Preview, Recap a Bonus tak sdílejí jednu taxonomii; hierarchy kontext
opravuje slabé `other`, ale nikdy nepřepisuje ruční video-level rozhodnutí.
`Film` zůstává title-level typem a do `content_type_manual` se nově nezapisuje.
Standardní canonical episode number filmového videa zůstává `NULL`, i když má
Film season context.

Detail hlavní sezóny skládá supplementary skupiny z detached title read-modelu.
Jeho existující `_load_catalog_title()` dříve načítal
`CatalogTitle.videos`, ale nikoli zpětnou vazbu `Video.catalog_title`, protože ji
původní display helper nepotřeboval. Back-reference je nyní eager-loaded JOINem
uvnitř existující větve `collection.titles -> videos`, ze které se skládají
supplementary skupiny. Přímý loader hlavního title zůstává beze změny. Nevznikla
samostatná filmová načítací cesta, lazy load ani nový SQL statement; season
detail drží stejný budget 20 dotazů.

Startup migrace raw klasifikaci ani hierarchy assignment nemění. Běžný rescan
může pro změněný soubor znovu uložit parserové `other`, ale autoritativní title
assignment a jeho effective Film výsledek přežijí. Databázové schema, hierarchy,
metadata, naming ani fyzické cesty se nemění.

Automatické ověření 28. srpna 2026:

```bash
.venv/bin/pytest -q                          # 925 passed
.venv/bin/python -m compileall -q app tests # prošlo
načtení všech Jinja2 šablon                  # 14/14, prošlo
git diff --check                             # prošlo
```

---

## 6.60 Databázově idempotentní startup CatalogTitle

Normální FastAPI lifespan spouští `migrate_schema()`, jehož součástí je také
startup hierarchy synchronizace. Audit stabilního opakovaného startupu odhalil,
že direct-root automatic Season 1 se nejprve v ORM přepnula na raw path hodnoty
`title/NULL/NULL`, tento mezistav se při grouping synchronizaci flushnul a až
finální shared structural inference jej vrátila na `season/1/S1`. SQLAlchemy
proto správně provedlo dva skutečné UPDATE a Python `onupdate=utc_now` při
každém z nich změnil `CatalogTitle.updated_at`, přestože konečný sémantický stav
řádku zůstal shodný.

Stejná scalar hodnota sama problém nezpůsobovala: takové přiřazení může objekt
krátce zařadit do `Session.dirty`, ale `Session.is_modified()` zůstává false a
flush UPDATE nevydá. Churn vznikl až z dočasně odlišné čtveřice `part_type`,
`season_number`, `part_number`, `season_label`. Regrese vznikla přesunem finální
structural inference za mezilehlý flush při sjednocení startup lifecycle; starší
raw path synchronizace byla sama o sobě obecnější a starší.

Startup nyní drží raw path structural hodnoty jako vstup synchronizace odděleně.
Po automatic assignmentu a obnovení persistentní collection-grouping authority
je aplikuje spolu se stejnou čistou structural inference, kterou používá finální
scanner/startup/runtime pipeline, uvnitř jedné no-autoflush fáze. Do následujícího
flush tak vstupuje pouze výsledná persistentní hodnota. Pokud se výsledná hodnota
rovná původnímu řádku, nevznikne `UPDATE catalog_titles` a `updated_at` se
nezmění. Pokud se skutečně změnil vstup a finální inference, vznikne jeden
legitimní UPDATE a běžný `onupdate` timestamp zůstává aktivní.

Complete i historicky incomplete manual snapshoty nadále obchází automatic
strukturální synchronizaci. Nullable manual hodnoty, explicitní
`hierarchy_manual_override`, verified hierarchy, persistentní manual split,
season-scoped supplementary části i Film se season contextem zůstávají
zachované. Numbering recalculation a finální structured hierarchy evaluation se
stále spouštějí; oprava nevypíná žádný startup krok a nemění schema.

Regresní test zachycuje všechny SQL `UPDATE catalog_titles` na druhém stabilním
startup lifecycle nad kombinací direct-root Season 1, explicitní Season,
season-scoped OVA a Filmu, confirmed manual Season a manual Filmu s autoritativně
prázdným season contextem. Ověřuje shodný kompletní persistentní stav i
`updated_at`, nulový počet UPDATE a shodný SHA-256 testovacího SQLite souboru.
Samostatný test odstraní druhou epizodu z automatic E1–E2 řady: finální
hierarchy se legitimně změní ze Season 1 na generic title, vznikne právě jeden
UPDATE a timestamp se obnoví.

Automatické ověření 28. srpna 2026:

```bash
nové cílené startup idempotence testy             # 2 passed
startup/scanner/hierarchy lifecycle sada           # 390 passed
.venv/bin/pytest -q                                # 927 passed
.venv/bin/python -m compileall -q app              # prošlo
načtení všech Jinja2 šablon                        # 14/14, prošlo
git diff --check                                   # prošlo
```

Změna nepřidává DB sloupec, nemění produkční `data/anime.db`, nespouští
produkční scan a nijak nepracuje s fyzickými daty na NASu.

---

## 6.61 Potvrzená duplicita jako neblokující fyzický backlog

Shared hierarchy evaluator dříve emitoval issue `confirmed_duplicate` se stejným
`blocking=true` jako unresolved canonical kolizi. Tím po potvrzení primary sice
numbering správně vyloučil secondary kopii z logického počtu, ale scanner,
startup, runtime refresh i Hierarchy Review ze společného výsledku znovu odvodily
`review_required` a uložily poznámku, že potvrzené soubory vyžadují vyřešení.

Platný `confirmed_duplicate` je nyní structured non-blocking issue s informací,
že fyzický cleanup čeká. Zůstává v DB, diagnostice, detailu titulu,
Hierarchy Review i katalogovém filtru duplicit, ale nevstupuje do blocking count,
primary note ani odvozeného hierarchy statusu. Známá stará odvozená confirmed
poznámka se nepovyšuje na neurčitý legacy blocker, takže existující záznam se
při nejbližší startup synchronizaci přepne na správný `automatic` nebo
`verified` stav. Jiné neznámé legacy review důvody se nadále konzervativně
zachovávají.

Unresolved `canonical_duplicate` a `duplicate_primary_missing` zůstávají
blokující. Scanner při zmizení primary pouze zruší neplatnou vazbu, nastaví
`duplicate_primary_missing=true` a nevybírá náhradu. Samostatné manual
`suspected` se nemění. Oprava nepřidává fyzický cleanup ani DB schema a nemění
soubory na NASu.

Automatické ověření 28. srpna 2026:

```bash
nové cílené duplicate workflow testy             # 8 passed
hierarchy/duplicate/scanner/startup/UI sada        # 408 passed
.venv/bin/pytest -q                                # 930 passed
.venv/bin/python -m compileall -q app              # prošlo
načtení všech Jinja2 šablon                        # 14/14, prošlo
git diff --check                                   # prošlo
```

---

## 6.62 Ruční Part ordinal pro rozdělenou Season

`CatalogTitle.part_number_manual` a effective Part osa už ve schematu existovaly,
ale sdílená authority validace zakazovala `part_number` pro `part_type=season`,
write helper jej před uložením zahazoval a JavaScript pole pro Season skryl a
disableoval. Hierarchy Review proto nedokázalo autoritativně vyjádřit jednu
Season rozdělenou do několika samostatných `CatalogTitle`.

Manual snapshot nyní dovoluje například `season_number=1, part_number=1` a
`season_number=1, part_number=2`, aniž se mění canonical episode numbering nebo
z Part 2 vzniká Season 2. Jediný Season title může mít autoritativní
`part_number=NULL`. Pokud ale v jedné collection existuje více effective Season
titles se stejným neprázdným `season_number`, všechny musí mít explicitní a
vzájemně unikátní Part ordinal. Chybějící Part 1 ani jiná hodnota se nikdy
neodvozuje z E01–E13 / E14–E26 ani z pořadí title.

Guard je server-side a prospektivně kontroluje celou collection před jednotlivým
manual POSTem, takže odmítnutí nezanechá částečný snapshot. Již existující
`S1 + S1`, `S1 + S1/Part 2` nebo duplicitní `S1/Part 1` se automaticky
nepřepisuje: shared evaluator vytvoří blocking `ambiguous_split_season`, startup
a runtime refresh nastaví `review_required` a raw Hierarchy Review zachová oba
titles samostatně. Atomickou opravu všech dotčených ordinalů umožňuje existující
hromadná definice ručního rozdělení; nový paralelní persistence mechanismus
nevznikl.

Centralizovaný structural label zobrazuje manual Season parts jako `S1 · Part 1`
a `S1 · Part 2`. Complete manual authority je nadále chráněna před startupem,
scannerem i rebuildem. Změna nepřidává DB schema ani automatický backfill a
nepracuje s fyzickými soubory.

Automatická validace tohoto kroku:

```text
nové cílené split-season scénáře                    # 17 passed
relevantní hierarchy/numbering/UI/lifecycle sada    # 659 passed
celá testovací sada                                 # 947 passed
```

---

## 6.63 Asistované potvrzení prvního splitu Season

Přísný invariant z části 6.62 zůstává beze změny, ale běžná **Správa zařazení
jednotlivých videí** už se při prvním splitu nezastaví pouze na chybě
`S1 + S1/Part 2`. Read-only prospective evaluator umí pro původní jedinou Season
navrhnout complementary Part 1 nebo Part 2 pouze tehdy, když uživatel výslovně
zvolil P1/P2, vybraná i zbývající množina mají úplný unikátní episode range,
rozsahy od E01 bez mezery navazují a jejich pořadí odpovídá zvoleným ordinalům.
První POST nic nezapíše; zobrazí oba výsledné rozsahy a structural labels.
Teprve explicitně potvrzený druhý POST návrh znovu vyhodnotí nad aktuální DB a v
jedné transakci nastaví complementary manual snapshot, vytvoří novou část a
přesune původně vybraná videa.

Shared proposal evaluator současně nabízí nahoře v Hierarchy Review opravu již
existujícího `S1 + S1`, pokud právě dva Season titles bez Part ordinalů obsahují
úplné nepřekrývající se řady, které od E01 bez mezery navazují. Potvrzení mění
pouze manual hierarchy snapshoty stejných CatalogTitle. Jejich ID, video
membership, metadata vazby, duplicate vazby a canonical numbering zůstávají
beze změny. Návrh je pokaždé znovu serverově ověřen a bez potvrzovacího checkboxu
se nic nezapíše.

Žádná complementary inference nevzniká pro Part 3, pro již existující více Parts,
identické nebo překrývající se rozsahy, mezeru v řadě, neúplné numbering ani
rozdělení potvrzené duplicate skupiny mezi oba cíle. Tyto případy zůstávají v
ručním review. Simple i JSON advanced preview nyní kromě assignment konfliktů
prospektivně kontrolují také výsledný split-season invariant; validní celý P1/P2
plán projde a partial plán se odmítne už v náhledu.

Změna nepřidává schema, nový obecný workflow framework ani filesystem operace.

Automatická validace navazujícího workflow:

```text
nové cílené proposal/atomicity/preview scénáře       # 13 passed
relevantní hierarchy/numbering/UI/lifecycle sada    # 672 passed
celá testovací sada                                 # 960 passed
```

---

## 6.64 Collection move jako hierarchy lifecycle boundary

Persistované automatic hodnoty `CatalogTitle.part_type`, `season_number`,
`part_number` a `season_label` jsou finální cache nad raw path evidence i
collection-context inference. Před opravou přesun zachoval tuto cache a shared
finalizer ji v nové non-direct-root collection znovu použil jako údajně explicitní
strukturální vstup. Slabé automatic `S1`, které vzniklo pouze ze souvislé E01…
řady jediného direct-root title, proto po merge přežilo jako stale `S1` a nový
split-season guard oprávněně hlásil `S1 + S1`.

Skutečná změna `CatalogTitle.catalog_collection_id` nyní nejprve pro každý
přesouvaný title bez manual authority obnoví raw structural input společným
`derive_library_hierarchy()` nad path evidence dotčených collections. Teprve pak
se title a jeho redundantní video collection vazby přesunou a existující
`finalize_hierarchy_write()` lokálně dokončí source i target collection. Explicitní
podporovaný `Season 2` signál se znovu odvodí jako S2; bez bezpečné evidence skončí
původní singleton S1 jako generic/unknown review. Žádné pořadí Season/Part se
nevymýšlí z pořadí přesunu, filename sortu ani episode ranges.

Complete i historicky neúplný manual hierarchy snapshot je nadále chráněn včetně
autoritativního `NULL`, manual Part ordinalů a verified timestampu. Move nemění
CatalogTitle ID, video membership, canonical numbering, metadata, confirmed
duplicate vazby ani manual suspected stav. Split-season invariant zůstává beze
změny: dvě skutečně explicitní S1 bez unikátních Parts dál vytvoří blocking
`ambiguous_split_season`.

Persistentní collection-grouping authority se nově vyhodnotí už při automatic
reconstruction startupu a scanneru. Titul se tak během stabilního lifecycle
dočasně neflushne zpět do fyzicky odvozené source collection a následně znovu do
manual targetu. Stabilní opakovaný startup proto nevytváří transientní
`UPDATE catalog_titles` ani `updated_at` churn. Neexistuje schema migrace ani
historický hromadný backfill; starší stale merge opraví explicitní collection-context
re-evaluation z části 6.65, další skutečný move/merge nebo ruční hierarchy workflow.

Automatická validace tohoto lifecycle:

```text
nové cílené move/provenance/atomicity/idempotence scénáře  # 10 passed
relevantní hierarchy/move/scanner/startup sada             # 541 passed
celá testovací sada                                         # 969 passed
```

---

## 6.65 Explicitní collection-context re-evaluation

Historické collection merges vytvořené před částí 6.64 mohou v jedné collection
stále obsahovat několik stale automatic S1 vedle skutečné manual S1. Jednotlivý
manual title POST takový stav nemůže vždy bezpečně opravit, protože strict
split-season guard správně odmítá neplatný mezistav s dalšími dvěma S1. Guard se
neoslabuje a startup neprovádí žádný automatic backfill.

Hierarchy Review proto obsahuje explicitně potvrzovanou akci **Přepočítat
automatickou hierarchii**. Jedním collection-level write znovu sestaví raw
structural input všech titles bez manual authority přes stejný
`invalidate_automatic_hierarchy_for_collection_move()` helper jako collection
move a následně použije shared inference/evaluation v aktuální collection.
Automatic explicitní Season 2 evidence znovu vytvoří S2; starý singleton-derived
S1 bez bezpečné raw evidence skončí jako generic/unknown review. Pořadí Season ani
Part se nikdy neodvozuje.

Complete i historicky neúplné manual snapshoty včetně autoritativního `NULL`
zůstávají beze změny. Structure-only finalizace nepřepisuje canonical numbering.
Operace před i po změně porovná CatalogTitle IDs, Video membership, všechna
canonical numbering pole, manual hierarchy authority, metadata ownership,
manual-split selectory a duplicate/suspected stav; jakákoli odchylka zruší celou
transakci. Fyzické cesty ani NAS se nemění.

Akce není spuštěna startupem, scannerem ani migrací a neobsahuje žádnou
Kimetsu-specific výjimku. Po explicitním přepočtu persistentní grouping authority
zajistí, že startup/scanner nadále odvodí stejný current-collection výsledek a
stabilní opakovaný startup nevytváří `UPDATE catalog_titles` ani timestamp churn.

Automatická validace explicitní re-evaluation:

```text
nové cílené backend/UI/preservation/startup scénáře  # 6 passed
relevantní hierarchy/move/refresh/lifecycle sada     # 547 passed
celá testovací sada                                   # 975 passed
```

---

## 6.66 Video Variant – schema a základní invarianty

První izolovaný schema krok přidává `VideoVariantGroup` jako explicitní
manual-authority lane pod jedním `CatalogTitle` a nullable
`Video.video_variant_group_id`. Group reprezentuje například celou TV lane přes
E01, E02 a E03; není to jedna epizoda. Budoucí konkrétní legitimní varianta
epizody bude kombinace logical episode identity a group. Samostatný `Episode`
model tímto krokem nevznikl.

Stabilní identitou group je její `id`. `manual_label` je povinný neprázdný
uživatelský popis a lze jej měnit bez změny identity. Nullable `release_source`
přijímá pouze `tv`, `bd`, `web`, `dvd`, `other`; nullable `content_variant`
přijímá pouze `censored`, `uncensored`, `other`. Osy jsou nezávislé a `NULL`
znamená unknown/unspecified. Neexistuje inference `BD → uncensored` ani
`TV → censored`; validní je i group `A` s oběma taxonomy poli `NULL`.
`verified_at` eviduje manuální potvrzení a nullable `note` zůstává doplňkovou
poznámkou.

Cross-title invariant je součástí sdílené doménové write vrstvy:

```text
Video.video_variant_group_id IS NULL
OR Video.catalog_title_id = VideoVariantGroup.catalog_title_id
```

Přiřazení group z jiného title se odmítne bez tiché opravy. Společný helper pro
změnu title membership zachová group pouze tehdy, když patří cílovému title.
Při skutečném přesunu video → jiné `CatalogTitle` se starý assignment bezpečně
vyčistí na `NULL`; group se neklonuje a v cílovém title se žádná odpovídající
group nehádá. Helper současně dovoluje budoucímu atomickému workflow explicitně
předat novou validní group a před jakoukoli mutací ji ověří. Scanner, startup,
manual split, přesun do existující/nové části, root assignment a hierarchy
rebuild používají stejnou title-assignment operaci.

Schema je aditivní a idempotentní. Nová tabulka má FK
`catalog_title_id → catalog_titles.id ON DELETE CASCADE` a index podle title;
nový nullable FK videa používá `ON DELETE SET NULL` a vlastní index. Smazání
videa group nemaže, smazání title odstraní jeho groups, prázdná group se po
zmizení jednoho videa automaticky nemaže. Startup a hierarchy rebuild považují
i prázdnou group za manual user state, takže automatický cleanup nesmí její
title odstranit. Všechna existující videa migrují s `NULL` a žádný filename/path
backfill ani automatická group nevzniká.

`NULL` znamená neposouzeno / bez potvrzené variantní identity. Není to default
group, standardní varianta ani autoritativní tvrzení, že video variantou není.
Parserové `structural_variant` A/B a version hints `Ver.TV`, `UC`, `CZ`,
`CZ END` se nezměnily a nejsou manual authority. Běžný rescan a startup zachová
již validní group assignment, pokud se title membership nezmění.

Tento krok záměrně nemění `video_numbering_identity()`, canonical numbering,
logical/standard counts, unresolved duplicate grouping, confirmed
`duplicate_of_video_id`, `duplicate_primary_missing`, manual `suspected`, Media
Check ani external subtitle linking. All-`NULL` data proto dávají stejné výsledky
jako před migrací. Variant-aware logical/duplicate counting bude následovat v
samostatném commitu; veřejné group assignment UI, subtitle compatibility M:N a
V6 filesystem layout nejsou součástí tohoto kroku.

Testy pokrývají upgrade pre-variant DB a opakovanou migraci, FK/indexy a delete
semantiku, vytvoření a změnu taxonomy bez změny ID, same-title assignment,
cross-title rejection a atomicitu, clear na `NULL`, manual move, rescan/startup
preservation, delete lifecycle, dosavadní duplicate/numbering chování a
nezměněné A/B / `Ver.TV` / `UC` parser evidence.

Automatická validace tohoto schema kroku:

```text
nové cílené VideoVariantGroup scénáře               # 13 passed
migration/scanner/hierarchy/duplicate/numbering/
Media Check/subtitle regresní sada                  # 347 passed
celá testovací sada                                 # 988 passed
compileall, 14/14 Jinja2 šablon, git diff --check   # prošlo
```

Změna neprovádí produkční scan, nemění produkční `data/anime.db` ani fyzická data
na NASu a nezahajuje V6.

---

## 6.67 Video Variant – logical identity a variant-aware duplicate evaluation

Druhý izolovaný krok zavádí immutable derived `LogicalEpisodeIdentity` pro
standardní canonical epizody. Identita je vždy kombinace právě jednoho
`CatalogTitle` a `season_episode_number`; `VideoVariantGroup` v ní záměrně není.
Nevznikla databázová `Episode` entita ani nové schema. Supplementary identita,
fractional/zero/unknown význam, A/B `structural_variant`, absolute/external
číslování a výpočet range/gaps zůstávají na dosavadních osách.

Jedna sdílená partition funkce nyní rozkládá každou logical episode na
potvrzené non-`NULL` variant lanes a explicitní neposouzený `NULL` bucket:

```text
E01 + group A, E01 + group B       -> dvě legitimní varianty, bez collision
E01 + group A, E01 + group A       -> unresolved collision uvnitř group A
E01 + NULL,    E01 + NULL          -> unresolved review zůstává
E01 + group A, E01 + NULL          -> unresolved variant ambiguity zůstává
E01 + A, E01 + B, E01 + A-copy     -> collision pouze A / A-copy
```

Pravidlo platí pouze pro standardní canonical epizody. Supplementary duplicate
identity používá dále svůj subtype a bezpečný season/name context bez dělení
podle variant group. `Ver.TV`, `UC` ani A/B parser evidence nevytváří group a
nepotlačuje collision. Reálnému tvaru 25 fyzických videí s E01–E12 plain +
`Ver.TV` a jedním E13 odpovídá po explicitním manual assignmentu 13 logical
episodes a 25 confirmed variant instances; bez assignmentu zůstává 12
unresolved collision groups.

`TitleNumberingSummary` odděluje:

- `physical_video_count` (kompatibilní `total`) – všechny fyzické Video rows,
- `logical_episode_count` (opravený `standard_total`) – unikátní bezpečné
  standardní logical identities,
- `confirmed_variant_instance_count` – distinct non-`NULL`
  `(LogicalEpisodeIdentity, VideoVariantGroup)` po vyloučení secondary copies,
- `confirmed_duplicate_count` – explicitní secondary rows s
  `duplicate_of_video_id`,
- `unassigned_variant_video_count` – aktivní canonical fyzické reprezentace,
  které stále mají group `NULL`.

Změna významu `standard_total` je záměrná a auditovaná: Hierarchy Review jej už
před tímto krokem prezentoval jako počet logických standardních epizod, zatímco
implementace před potvrzením duplicity počítala fyzické kopie. Například 26
all-`NULL` souborů ve 13 canonical collision groups nyní dává
`physical_video_count=26`, `standard_total=logical_episode_count=13` a stále
všech 13 blocking duplicate warnings. Globální homepage/katalogové statistiky,
které výslovně inventarizují fyzická videa a jejich file type, se tímto krokem
nemění.

`duplicate_of_video_id` zůstává autoritou potvrzené duplicity a confirmation UI
se neredesignovalo. Platná secondary kopie nezvyšuje logical ani confirmed
variant count, ale zůstává ve fyzickém počtu a v neblokujícím cleanup backlogu.
Existující potvrzený vztah mezi dvěma různými non-`NULL` groups se automaticky
neruší ani neopravuje; evaluator jej navíc označí samostatným blocking issue
`confirmed_duplicate_variant_conflict`. Chybějící primary zůstává nezávislým
`duplicate_primary_missing` blockerem.

Hierarchy Review nově zobrazuje fyzický, logical, confirmed-variant,
unassigned a confirmed-duplicate počet. Pro dvě unresolved collisions téhož
E čísla v různých groups používají stávající bulk formuláře group-aware klíč,
takže se jejich vstupy neslijí. Tento druhý krok sám variant management UI
neobsahoval; create, assign, reassign, clear, A/B confirmation a taxonomy editace
jsou implementované následným **Commitem 3 – Manual Video Variant authority UI**
popsaným v části 6.68.

Scanner/startup dál žádnou group nevytváří ani nemění a partition je čistě
derived. Media Check zůstává per Video a subtitle persistence/linking se
nezměnily. Tento krok nepřidává migraci, neprovádí produkční scan, nemění
produkční `data/anime.db`, NAS ani V6 filesystem roadmapu.

Automatická validace tohoto logical/duplicate kroku:

```text
nové cílené logical/variant/duplicate scénáře         # 13 passed
numbering suite                                      # 90 passed
hierarchy evaluation/diagnostics/review              # 174 passed
VideoVariantGroup + scanner/startup/migration/move   # 165 passed
Media Check / Media Part / subtitle / language       # 70 passed
celá testovací sada                                  # 1001 passed
compileall, 14/14 Jinja2 šablon, git diff --check    # prošlo
```

---

## 6.68 Video Variant – manual authority UI

Třetí izolovaný krok zpřístupňuje již existující `VideoVariantGroup` jako
bezpečnou ruční autoritu v raw Hierarchy Review. Každý `CatalogTitle` má vlastní
sekci **Video varianty**, kde lze group vytvořit, upravit její `manual_label`,
`release_source`, `content_variant` a poznámku bez změny stabilního ID a
explicitně odstranit pouze prázdnou group. Neprázdná group se nikdy nemaže ani
nečistí automaticky.

Assignment workflow podporuje všechny vratné přechody `NULL → group`,
`group A → group B` a `group → NULL`. Běžný uživatel vybírá filename, canonical
pozici, presentation label a taxonomy; `Video.id` zůstává pouze hidden POST
hodnotou. Ruční fallback dovoluje označit více videí jednoho title a přiřadit je
do explicitně vybrané existující nebo nově potvrzené group. Cross-title group se
serverově odmítne a obyčejný variant assignment nesmí změnit canonical
numbering, duplicate vztah ani hierarchy membership.

Po prvním produkčním smoke testu byl odstraněn UX blocker clear operace. Stejný
title-level formulář je nyní veřejně pojmenovaný **Přiřadit, změnit nebo odebrat
variantu u vybraných videí** a vedle existující/nové group nabízí explicitní cíl
**Neurčeno / odebrat z varianty**. Každý checkbox řádek ukazuje současnou
variantu. Clear používá stejný prospective preview, fingerprint, required
confirmation a atomický confirm jako assign/reassign; v preview je vidět
například `BD → neurčeno`. Mění pouze `Video.video_variant_group_id`, prázdnou
manual-authority group automaticky nemaže a po clear lze group odstranit až
samostatnou explicitní akcí.

Bulk assignment i řešení canonical collision jsou dvoufázové. První POST vytvoří
read-only prospective snapshot se seznamem filenames, současnou a výslednou
variantou, počtem vytvořených groups, collisions před/po a případnými blockery.
Snapshot nic nepersistuje. Potvrzovací POST vyžaduje checkbox, znovu ověří
fingerprint aktuálního title i všechny invarianty a teprve potom zapíše jednu
transakci. Stale preview se odmítne čitelnou chybou zpět v Hierarchy Review.

Vedle existující volby primary a strukturální akce **Není duplicita / zařadit
jinam** existuje samostatná akce **Potvrdit jako různé video varianty**. Distinct
non-`NULL` groups odstraní canonical duplicate blocker, ale logická epizoda
zůstává jedna. Dvě reprezentace ve stejné group zůstávají duplicate candidate;
`NULL+known` i `NULL+NULL` se po reloadu dál zobrazují jako review. Vyčištění
assignmentu proto legitimně vrátí dříve vyřešenou ambiguity.

Konzervativní bulk detector nabízí opakující se hint/plain lane pouze pokud jsou
všechny zahrnuté canonical identity bezpečné, každá má právě dva vysvětlitelné
členy, discriminator hint je konzistentní a title neobsahuje confirmed duplicate
vztah. Reálný tvar E01–E12 `Ver.TV` + plain tak dostane jeden preview. `Ver.TV`
smí pouze předvyplnit label `TV` a source `tv`; plain lane zůstává bez labelu a
taxonomy, dokud je nedoplní člověk. `BD` se neodvozuje z absence TV markeru,
`TV` neznamená censored a samotné `UC` se bez další doménové autority nemapuje na
`uncensored`. Parser suggestion sama nikdy nevytvoří group ani assignment.

Pro bezpečnou dvojici `01A` / `01B` existuje zvláštní atomic preview. Operace
znovu ověří stejný base number, přesné markery A/B, absenci ignorované třetí
reprezentace, konfliktu manual numbering a confirmed duplicate vztahu. Jedna
transakce potom přes existující manual numbering mechanismus nastaví oběma
videím canonical E01, vytvoří nebo explicitně reuse zvolené groups A/B, přiřadí
je a jednou spustí shared finalizer. Parser, filename a fyzická cesta se nemění;
persistovaný mezistav `E01 NULL / E01 NULL` nevzniká. Stejné existující A/B groups
lze explicitně použít u dalších bezpečných párů.

`duplicate_of_video_id` zůstává samostatnou autoritou. Simple ani bulk write
nesmí rozdělit primary a confirmed secondary do různých známých groups a zobrazí
pokyn nejprve upravit duplicate vztah. Vztah se variant assignmentem nikdy
automaticky neruší. Stable assignment i explicitně potvrzené `NULL` přežijí
startup, rescan a hierarchy rebuild; title move dál používá Commit 1 helper a
neplatnou group starého title vyčistí bez klonování.

Tento krok nemění schema ani migrace, parser persistence, subtitle model, Media
Check evaluator nebo NAS layout. Subtitle compatibility M:N a variant-aware
Media Check completion zůstávají samostatné následující commity.

Automatická validace manual-authority UI kroku:

```text
targeted variant authority UI/write scénáře         # 19 passed
Commit 1 + Commit 2 + manual-authority UI           # 45 passed
numbering/hierarchy/duplicate/split/move regrese    # 597 passed
scanner/startup/migration/rebuild regrese           # 137 passed
Media Check/subtitle regrese                        # 27 passed
celá testovací sada                                 # 1020 passed
compileall, 14/14 Jinja2 šablon, git diff --check   # prošlo
```

Doporučený druhý smoke test clear cesty nad již ručně potvrzeným Nande stavem:

1. V raw Hierarchy Review rozbalit u příslušného title **Přiřadit, změnit nebo
   odebrat variantu u vybraných videí** a v seznamu ověřit `E06 plain` se
   současnou variantou `BD`.
2. Zaškrtnout pouze plain E06, jako výslednou variantu zvolit **Neurčeno /
   odebrat z varianty**, otevřít preview a ověřit řádek `BD → neurčeno`; před
   potvrzením se DB nemění.
3. Zaškrtnout povinné potvrzení a uložit. Po reloadu musí zůstat canonical E06
   oběma videím a logical count beze změny, `Ver.TV` musí zůstat v TV, plain musí
   být neurčeno, BD count musí klesnout o jedna a `known + NULL` review se musí
   vrátit.
4. Stejným formulářem vybrat plain E06, zvolit existující group `BD`, zkontrolovat
   preview `neurčeno → BD` a potvrdit. Ambiguity musí zmizet a BD count se musí
   vrátit. Ani jeden krok nesmí měnit duplicate vztah, numbering, hierarchy,
   metadata, titulky, filename ani fyzickou cestu.

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
