# AnimeDB – stav projektu a roadmapa

> Tento dokument je hlavní checkpoint projektu. Slouží pro pokračování v novém chatu, předání kontextu Codexu a kontrolu, že vývoj neuhýbá od cíle.
>
> **Aktualizováno:** 19. srpna 2026
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
duplicitních souborů. Číslování je vyřešené, ale kolekce zůstává
`review_required` s důvodem **Potvrzené duplicitní soubory vyžadují vyřešení.**
Potvrzená duplicita se záměrně nezobrazuje zeleně jako legitimní konečný stav.

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
`supplementary_special`. Dokud video není ručně klasifikované nebo přesunuté do
supplementary CatalogTitle, chybějící canonical číslo otevře hierarchy review;
žádné pravidlo „první `[SP]` = Special E1“ neexistuje.

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

Po vytvoření části `Specials` zůstává `[SP]` video bez canonical čísla a
collection oprávněně zůstane `review_required`. Dosavadní ruční numbering
workflow může následně potvrdit například Special E1; filename E14 se za
canonical Special 14 ani Special 1 nepovažuje. DB schema, fyzické cesty,
metadata providery ani duplicate workflow se kvůli této UI zkratce nemění.

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

„Jednoduchá definice ručního rozdělení“ nemá samostatnou tabulku ani uložený
JSON. Každá persistentní položka je přímo konkrétní `CatalogTitle` s
`hierarchy_manual_override=True`; její stabilní identitou je `CatalogTitle.id`
a pravidla jsou uložena v jeho `episode_start`, `episode_end`,
`episode_filename_pattern`, manual season/type/sort a numbering polích. Formulář
tyto stejné řádky pouze serializuje.

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
uvnitř Season a obě osy se ukládají samostatně. `Season 1 Part 2` má
`part_type=part`, `season_number=1`, `part_number=2` a zobrazuje se jako
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

Běžné hierarchy formuláře a ruční split zobrazují pro `season` číslo a označení
sezóny; pro `part` samostatné **Číslo sezóny** a **Číslo Part**. Backend stejné
kombinace validuje a autoritativní Part bez čísla Part odmítne. Centrální
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
přesun není součástí této změny. Vícesouborový film s fyzickými segmenty P1/P2
je jiný budoucí problém na úrovni `Video` (pravděpodobně samostatný media-part
koncept), nikoli hierarchy Part.

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
