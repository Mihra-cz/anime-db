# AnimeDB – technická historie

Výběr významných milníků, nikoli úplný changelog nebo pracovní deník.
Současnou implementaci a zbývající práci popisuje [PROJECT_STATUS.md](PROJECT_STATUS.md);
provozní návod je v [README.md](../README.md).

Chronologie vychází z Git historie do `4769df4`. Hash a původní subject jsou
převzaty z commitů; data odpovídají autorskému datu (`git log --format=%as`).
Související změny jsou seskupené, drobné mezikroky vynechané. Historické názvy
verzí označují tehdejší rozsah, nikoli automaticky dnešní closure stav.

## Základ katalogu a metadata integrace

### 2026-08-04 — Základ scanneru a webového katalogu

`95ca99e` — Create initial AnimeDB scanner

`f11ac05` — Improve subtitle language normalization and media classification

- Vznik evidence videí, ffprobe streamů a externích titulků nad SQLite a webového přehledu knihovny.
- Navazující změna zpřesnila jazykovou normalizaci a klasifikaci médií.

### 2026-08-05 — Bezpečnost skenování knihovny

`b9e339e` — Harden library scanning and mount safety

- Kontroly dostupnosti/mountu a ochrany před nebezpečným odstraněním databázové evidence při nedostupné knihovně.

### 2026-08-05 — Seskupený katalog, hledání a řazení

`181ecdf` — Add grouped catalog views and manual hardsub verification

`a701014` — Unify grouped catalog views and title details

`7b09420` — Add global catalog search

`48c32cd` — Add sortable catalog and detail tables

- Sjednocení skupinových pohledů a detailu titulu, ruční ověření hardsubu, globální search a řaditelné tabulky.

### 2026-08-05 — Stabilní title identity a AniList vazby

`e470501` — Add stable catalog titles and AniList metadata search

`c13f371` — Add manual AniList metadata linking and updates

- CatalogTitle se stal stabilním bodem pro externí metadata; přibylo AniList hledání, ruční propojení a aktualizace.
- Lokální identita se oddělila od providerových dat.

### 2026-08-06 — Ruční hierarchy a číslování epizod

`97d3213` — Add manual hierarchy review and episode numbering

- Vznik collection/title hierarchie, Hierarchy Review, ručního číslování a databázového rebuild nástroje.
- Logická oprava katalogu se oddělila od fyzických cest médií.

### 2026-08-06 až 2026-08-07 — Persistentní kandidáti a původní metadata checkpoint V5

`800e5c5` — Persist metadata candidates and harden network operations

`aa46d9d` — Close V5 metadata integration

- Ukládání metadata kandidátů, lokální cover cache a zpevnění síťových operací.
- Dokumentační commit Close V5 metadata integration uzavřel tehdejší metadata rozsah; neznamená dokončení pozdější stabilizace ani dnešního finálního V5 closure auditu.

## Stabilizace hierarchie a ručních workflow

### 2026-08-11 až 2026-08-12 — Stabilizace review a přechod homepage na logickou hierarchii

`b8a5fa1` — Stabilize episode numbering and hierarchy review

`13920d3` — Improve hierarchy review workflow

`59e05dd` — Improve episode list and root video workflow

`2e6b929` — Use logical catalog hierarchy on homepage

- Zpřesnění numbering/review workflow, seznamu epizod a práce s root videi.
- Homepage začala používat logickou katalogovou hierarchii místo pouhého fyzického seskupení.

### 2026-08-12 až 2026-08-14 — Ruční klasifikace a správa duplicit

`8ae519f` — Rozšíření ručního zařazení a číslování obsahu

`fd0814e` — Rozšíření ověřování hierarchie a správy duplicit

`097b8b1` — Rozšíření hierarchie a správy doplňkového obsahu

`380b51d` — Přidání ruční správy a filtrování duplicit

- Rozšíření vratného ručního zařazení a doplňkového obsahu.
- Oddělení potvrzených duplicate vztahů od ručního podezření a doplnění jejich správy a filtrů.

### 2026-08-17 až 2026-08-18 — Parser a společná taxonomie částí

`e12326b` — Rozšíření parseru a zpřesnění Hierarchy Review

`81c3e84` — Sjednocení ruční klasifikace částí

- Rozšíření rozpoznávaných názvů a diagnostiky Hierarchy Review.
- Společné volby a validace ručních typů částí; Film zůstává title-level klasifikací.

### 2026-08-19 až 2026-08-21 — Jazykový profil a konzervativní automatic hierarchy

`5c80ed7` — Přidání jazykového profilu videí

`0c9cec2` — Oprava významu časových suffixů v hierarchii

`d36c291` — Stabilizace automatické hierarchie a kontroly

- Sdílený jazykový profil oddělil audio a subtitle fakta.
- Historické časové suffixy přestaly určovat sezóny; automatic inference/review získaly konzervativnější strukturální pravidla.

### 2026-08-23 — Season, Part a Media Part jako oddělené pojmy

`4abc863` — Přidání podpory Season a Part

`49bde12` — Přidání podpory Media Part

- Part jako logická část anime není Season číslo.
- Media Part přidal ručně evidovaný fyzický segment, aniž by vznikal další CatalogTitle.

### 2026-08-23 — Responzivní UI

`94d6f2c` — Přidání responzivního layoutu

- Společný layout a adaptace navigace, formulářů a datových přehledů na menší šířky.

### 2026-08-23 — Společné hierarchy vyhodnocení a selector authority

`8a5800b` — Sjednocení vyhodnocení hierarchy

`7355f62` — Sjednocení lifecycle vyhodnocení hierarchy

`1d7a85f` — Sjednocení manual split lifecycle

`9f87893` — Oddělení manual split authority od assignmentu

- Sjednocení výsledného hierarchy vyhodnocení a lifecycle napříč write cestami.
- Manual split dostal samostatnou selector authority, která se neodvozuje z výsledného video assignmentu.

### 2026-08-24 — Rebuild a úplná manual hierarchy authority

`0fb488c` — Přestavba hierarchy rebuildu

`d963c46` — Sjednocení manual hierarchy authority

`ec9da33` — Oprava parseru a hierarchy numberingu

- Rebuild přešel na explicitní plánování a kontrolované použití plánu; parity chrání vztah ke scanneru a migraci.
- Společná autorita rozlišuje none/incomplete/complete snapshot a zachovává význam ručního NULL; navázalo zpřesnění parseru/numberingu.

### 2026-08-24 — Media Check jako samostatné workflow

`f86828b` — Přidání vyhodnocení audia a titulků

`a1b755a` — Přidání Media Check workflow

- Vyhodnocení audia, CZ/SK dostupnosti a interního EN fallbacku.
- Vznik pracovní fronty a ručního markeru nedostupnosti bez přepisování technických mediálních faktů.

### 2026-08-25 — Supplementary parser a práce s externími titulky

`f447b60` — Upřednostnění supplementary číslování

`abea6e3` — Sjednocení supplementary typů

`0e1a60f` — Přidání ručního přiřazení externích titulků

`0b09cb7` — Oprava supplementary klasifikace a číslování

`d5160f5` — Rozpoznání nečíslovaného supplementary obsahu

- Zpřesnění priority supplementary evidence, sdílení typů a podpory nečíslovaného doplňkového obsahu.
- Ruční workflow pro externí titulky bez bezpečného automatického přiřazení.

### 2026-08-25 až 2026-08-26 — Metadata split a season presentation

`7c2cd52` — Přesun splitu částí do Metadata Check

`bf8bfed` — Sjednocení názvů a řazení částí

`03e5aa1` — Seskupení hlavního zobrazení podle sezón

`e118ecc` — Doplnění přehledu doplňkového obsahu

- Rozdělení lokální části podle potvrzených metadat přešlo do Metadata Check.
- Společné názvy/řazení a read-only season view-model pro hlavní části a jejich jednoznačně navázané doplňky.

### 2026-08-27 až 2026-08-28 — Nezařazená videa a idempotentní synchronizace

`b4b96f6` — Zpřístupnění nezařazených videí v kontrole hierarchie

`e4b86de` — Oprava idempotence startup synchronizace

- Logicky nezařazená videa dostala vlastní přístupné workflow i mimo fyzický root.
- Nezměněné CatalogTitle při compatibility synchronizaci přestaly dostávat zbytečné UPDATE/timestamp změny.

### 2026-08-28 — Potvrzené kopie a rozdělené sezóny

`04dd8bf` — Oddělení potvrzených duplicit od blokující hierarchy

`2209946` — Doplnění správy rozdělených sezón

`ee2c81f` — Oprava hierarchie po sloučení kolekcí

- Platná potvrzená duplicita přestala sama blokovat hierarchy; chybějící primary zůstává problém.
- Explicitní Part identita pro rozdělenou Season a obnova automatic kontextu po změně collection.

## Varianty, titulky a škálování

### 2026-08-30 až 2026-08-31 — Video variants jako ruční autorita

`ce0e8dc` — Přidání datového modelu video variant

`ef50c4d` — Doplnění logiky video variant a duplicit

`7e36aa2` — Přidání ruční správy video variant

`2dab5ca` — Doplnění zobrazení video variant v katalogu

- Vznik title-scoped VideoVariantGroup, assignment workflow a katalogové presentation.
- Jedna logical episode identity může mít více potvrzených variant; NULL neznamená implicitní variantu a parser hint skupinu nepotvrzuje.

### 2026-08-31 až 2026-09-01 — M:N kompatibilita externích titulků

`13f52fb` — Přidání kompatibility externích titulků s video variantami

`c487275` — Přidání variantního vyhodnocení dostupnosti titulků

`76ad72b` — Odstranění legacy vlastníka externích titulků

- Kompatibilita konkrétního subtitle assetu s konkrétní fyzickou video reprezentací se stala samostatnou autoritou dostupnosti.
- Odstranění legacy ExternalSubtitle.video_id: jeden fyzický asset může mít více vztahů bez kopírování souboru.

### 2026-09-01 — Přesné fractional Recap pozice a hromadná oprava

`f5b9518` — Přidání desetinného číslování recapů a hromadné opravy epizod

- Desetinné ruční Recap číslování v přesných desetinách, oddělené od standardního integer číslování.
- Deterministický náhled a potvrzovaná hromadná oprava epizod.

### 2026-09-02 — Výkon a read-only presentation

`8f97bd9` — Optimalizace výkonu a škálování aplikace

- Odstranění opakovaných dotazů/načítání pomocí dávkových read modelů a request-local indexů.
- Verzovaný startup omezil opakované rekonstrukce; regresní testy ukotvily bounded query chování a GET bez semantic writes.

### 2026-09-02 — Úzká Media Check policy pro opening/ending

`4d405b2` — Úprava Media Checku pro openingy a endingy

- OP/ED/NCOP/NCED nevyžadují titulky a neznámý audio jazyk je zde neutrální, skutečná absence audia nikoli.
- Pravidlo respektuje manual video authority a nemění obecnou supplementary klasifikaci.

## Metadata completeness a závěrečný V5 polish

### 2026-09-05 — Metadata count/range a completion

`3685e90` — Sjednocení vyhodnocení počtu a rozsahu metadat

`5211758` — Přidání vyhodnocení úplnosti metadat

- Společná logická count evidence pro candidate i potvrzený detail, oddělená od přísného metadata-split gate.
- Nezávislý metadata requirement a completion: confirmed versus not_required versus missing, collection aggregate a sloupec Metadata.

### 2026-09-06 — Supplementary ordinal a derived review

`8deb88a` — Sjednocení číslování a kontroly doplňkového obsahu

- Společný lokální ordinal resolver pro opakovatelné supplementary typy, oddělený od Media Parts, variant a providerových čísel.
- Parserová doplnění, bezpečné identity/count a derived review chybějících čísel, kolizí či porušených duplicate vztahů bez nové persistentní status machine.

### 2026-09-06 — Všechna anime v Hierarchy Review

`cf1361e` — Přidání přehledu všech anime do Hierarchy Review

- Sbalený navigační index relevantních collections s přímými odkazy, hledáním a sdílenými stavovými badge vedle nezměněné pracovní fronty.
- Aktuální supplementary problém přebíjí stored verified/automatic presentation.

### 2026-09-06 — Lidské AniList chyby

`91148da` — Zpřesnění chybových hlášek AniList API

- Centralizované rozlišení známého temporary-disabled 403, rate limitu 429 a obecného selhání.
- Jiný 403 není outage; GraphQL errors při HTTP 200 zůstávají chybou a platný Retry-After může doplnit čekací dobu.

### 2026-09-06 — Artwork miniatury v katalogu a collections

`02b8bb8` — Přidání miniatur artworku do katalogu a kolekcí

- Sdílený výběr lokálního primary coveru, bezpečné thumbnail URL a neutrální placeholder.
- Miniatury pro katalog, hlavní collection parts a anime-level Film/OVA/Special; podřízené doplňky nepřebírají cizí artwork.

### 2026-09-06 — České názvy jazyků v Media Check

`4769df4` — Zpřehlednění jazyků v Media Check

- Společné lidské language labely pro selecty a audio/subtitle/compatibility presentation.
- Zachování dosavadních canonical kódů, ISO normalizace, hodnot formulářů a Media Check požadavků.
