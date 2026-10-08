# AnimeDB – současný stav projektu

Tento dokument popisuje účel AnimeDB, současnou implementaci a hranice jejího použití.
Verzovaný plán a progress patří do [ROADMAP.md](ROADMAP.md), technické milníky
do [HISTORY.md](HISTORY.md), vstupní a provozní rozcestník do [README.md](../README.md),
pravidla práce do [AGENTS.md](../AGENTS.md) a schválená V6 naming pravidla do
[V6_NAMING_CONTRACT.md](V6_NAMING_CONTRACT.md). Při rozporu mají přednost aktuální
kód, model a testy; datované audity nejsou živou inventurou produkce.
Tento dokument není roadmapa, changelog ani pracovní deník.

## Hlavní cíl projektu

AnimeDB je specializovaný katalog a správce anime knihovny na NAS. Jeho účelem
je udržovat srozumitelnou a důvěryhodnou evidenci anime, sezón, částí, epizod,
filmů, OVA a doplňků, jejich metadat, fyzických reprezentací a stavu médií.

Důraz je na CZ/SK titulky a hardsuby, audio/subtitle kontrolu, rozlišení variant
a duplicit a bezpečná ručně potvrzovaná rozhodnutí nad produkční knihovnou.
Logická identita, parserová evidence a skutečné soubory zůstávají oddělené.

Dlouhodobý cíl zahrnuje kontrolu úplnosti a chybějících dílů, řízenou reorganizaci
fyzických médií a bezpečný import neuspořádaných souborů. Tyto budoucí možnosti
mají vycházet z ověřeného katalogu, plánu a explicitního potvrzení, nikoli
z automatického přepisování produkční knihovny. Jejich rozsah a pořadí stanoví
[ROADMAP](ROADMAP.md); nejsou tím prohlášeny za současné funkce.

AnimeDB nemá znovu implementovat celý Jellyfin. Zachovává roli katalogu
a správce knihovny; dlouhodobý integrační směr počítá s Jellyfinem pro přehrávání
a se Shoko pro anime identifikaci. Tyto integrace zatím nejsou implementované.

## Prostředí a technologie

- Python 3.12+; web tvoří FastAPI, Jinja2 a vlastní HTML/CSS s malými JS interakcemi.
- SQLAlchemy 2.x a SQLite; databáze je lokální aplikační soubor mimo Git,
  běžně `data/anime.db`. Konfiguraci načítá `python-dotenv` a proměnné prostředí.
- Scanner používá `ffprobe` z FFmpeg pro videa a streamy; externí titulky
  vyhodnocuje lokální Python vrstva. MediaInfo má pouze volitelný wrapper,
  není součástí běžné scanner pipeline ani povinnou runtime závislostí.
- AniList přes HTTP/GraphQL (`httpx`) poskytuje title metadata.
  Lokální artwork cache s Pillow uchovává originály a thumbnails odděleně od NAS.
- Media root určuje `ANIME_PATH`. Provozní konfigurace počítá s NAS připojeným
  na hostiteli přes SMB/CIFS; Compose mapuje `/mnt/nas-anime` read-only do
  `/media/anime`, zatímco aplikační data mají vlastní zapisovatelný volume.
- Spuštění: lokální virtualenv + Uvicorn, nebo Docker/Compose s Python 3.12
  image; web standardně na portu 8000. Instalace a konfigurace jsou v README.
- Git/GitHub: repozitář `Mihra-cz/anime-db`; automatické regresní testy používají pytest.

Kontrakt stacku určují [pyproject.toml](../pyproject.toml),
[Dockerfile](../Dockerfile), [Compose](../compose.yaml) a [konfigurace](../app/config.py).
Konkrétní model notebooku, verze desktopového OS ani přístupové údaje NAS
nejsou součástí aplikačního kontraktu a nejsou zde vydávány za ověřené prostředí.

## Aktuální rozsah a baseline

Auditovaný baseline implementace: `05984ff` — Respect confirmed supplementary
placement.
Aktuální fáze: **V6 – Probíhá: příprava bezpečné reorganizace**.
V1–V5 jsou hotové a Pre-V6 gate je uzavřený (PASS). Fyzická reorganizace NAS
zatím nezačala a aplikace rename/move neprovádí.
Podrobný vývojový plán a zbývající kroky jsou výhradně v [ROADMAP.md](ROADMAP.md).

Současné zaměření V6:

- canonical naming kontrakt — schválená pravidla jsou
  v [V6_NAMING_CONTRACT.md](V6_NAMING_CONTRACT.md);
- Naming Review a Physical Layout Review jsou implementované a produkční
  ruční průchod je uzavřený;
- oddělení logické title identity od locatoru jako příprava shared Season
  containers; read-only Target Planner foundation je implementovaný,
  execution zůstává další prací.

### Pre-V6 closure checkpoint

Finální fresh read-only closure audit nad produkční DB (2026-09-27, kód
`05984ff`) skončil s **BLOCKER 0, REVIEW 0, CLEANUP 0**. PASS: hierarchy,
numbering, metadata, Media Check, duplicity, varianty, Media Parts,
supplementary, integrita DB a shoda uložených a odvozených hodnot. Jediný
mechanický rozdíl (zastaralý provenance label číslování u 13 videí po rozšíření
parseru) byl srovnán sdílenou finalizací. Produkční DB po closure: size
7077888 B, SHA-256 `5010425a3b02dfb13022ee0b7b064bec26a1eb7ebfe0c5635d9c70530da5936d`,
`user_version` 6. Checkpoint je snapshot, nikoli živá inventura.

Closure neznamená nulový backlog. Vědomě otevřené zůstává:

- **V6:** fyzická disposition 52 potvrzených (VALID) duplicate secondary kopií;
  Naming Review (dlouhé Romaji, root Bananya/Monogatari, volba Season/Part
  prefixů, názvy season-level supplementary obsahu); canonical parser/formatter;
  numbering migrace pro canonical filename grammar;
  transakce cest ve filesystemu a DB; move manifest, preview a rollback;
  fyzická strategie externích titulků.
- **Konec V6 – completeness:** 16 CZ externích titulků `confirmed_no_match`
  bez video targetu; 11 titulů, kde provider uvádí více epizod než lokální
  knihovna; chybějící standardní epizody a další completeness / Release
  Tracking evidence.
- **INFO / quality backlog:** doporučení k ověření hardsubu, kandidáti metadat
  s nízkým skóre, čínský dabing, Re:Zero Director's Cut s kandidáty pod ručním
  `unavailable`, `soft_long_flat_series` a latentní nálezy O2–O5.

Nic z toho není neuzavřeným Pre-V6 gate.

### Authority contract

Human authority > automatic inference. Automatika rozhoduje pouze při
jednoznačném důkazu. Nejednoznačnost se předává do Review, nikoli řeší
odhadem. Derived projection pouze dopočítává důsledky známé authority.

Aplikace do knihovny médií nezapisuje. Scanner a explicitní uživatelská workflow
mění aplikační databázi; metadata workflow může také ukládat lokální artwork
cache. „Read-only katalog“ tedy neznamená read-only aplikační databázi.
Přejmenování, přesuny, import či fyzický cleanup médií nejsou současné funkce.

## Současná architektura

- Web: FastAPI, serverově renderované Jinja2 šablony, společné CSS a malé
  klientské interakce. Routes oddělují čtení/presentation od potvrzovaných akcí.
- Persistence: SQLAlchemy nad SQLite, foreign keys zapnuté pro každé spojení.
  Idempotentní compatibility migrace mají verzovaný startup přes `user_version`;
  stabilní restart neprovádí novou rekonstrukci celé knihovny. Aktuální
  compatibility i produkční schema verze je 9 (schema verze, nikoli roadmap V6);
  řízený produkční rollout 8→9 je dokončený. Upgrady 1→2 až
  5→6 jsou aditivní, bez rekonstrukce. Upgrade 5→6 přidává lifecycle
  `ExternalTitleLink` s pouze mechanickým backfillem. Upgrade 6→7 vytváří prázdnou
  `physical_naming_choices` bez rekonstrukce a bez backfillu starých názvů,
  upgrade 7→8 stejně prázdnou `physical_layout_choices`. Upgrade 8→9 atomicky
  odstraní pouze title-locator UNIQUE a mechanicky převede jednoznačné legacy
  grouping references na existující owner FKs; knihovnu nerekonstruuje.
- Scanner: rekurzivní evidence MKV/MP4/M4V/AVI, technická data přes `ffprobe`,
  párování a jazyková evidence externích titulků. Velikost a `mtime` určují,
  zda je nutné opakovat probe. Manuální autority se zachovávají.
- Hierarchy: inference z lokální evidence, persistentní ruční rozhodnutí,
  reconciliation/rebuild a společné závěrečné vyhodnocení mají oddělené role.
- Katalog/presentation: společné resolvery názvů, řazení, klasifikace,
  číslování, language profilů a request-local indexy. `CollectionPresentation`
  skládá hlavní části a doplňky bez změny uložené hierarchie.
- Metadata: AniList HTTP/GraphQL provider, service pro potvrzené vazby,
  persistentní kandidáti, count/range evidence a samostatný completion resolver.
  Cover cache má vlastní bezpečnou lokální storage vrstvu.
- Media Check: nad společnými jazykovými fakty vyhodnocuje požadavky a ruční
  workflow; M:N kompatibilita externích titulků je samostatná sdílená autorita.

Implementační hranice dokládají zejména [modely](../app/models.py),
[hierarchy authority](../app/hierarchy_authority.py),
[numbering](../app/numbering.py), [supplementary resolver](../app/supplementary.py),
[metadata completion](../app/metadata/completion.py) a [Media Check](../app/media_check.py).

## Datový model a pravidla autority

### V6 physical naming foundation

`PhysicalNamingChoice` ukládá pouze explicitní lidské rozhodnutí pro právě jednu
collection nebo title (XOR FK, unique owner, delete cascade). Nesanitizovaný
`physical_text`, `choice_kind`, UTC `confirmed_at` a deterministic version-1
`basis_snapshot_json` jsou oddělené od display, hierarchy, numbering a metadata
authority. Absence row je nezkontrolovaný derived stav; i potvrzení default
Romaji vytváří row. Confirm/reconfirm/reset service vlastní jen naming změny,
transakci dokončuje caller; reset row odstraní.

Batch loader a čistý [resolver](../app/physical_naming.py) odvozují default z
confirmed metadata a supplementary inheritance bez nové row. Root source je
jednoznačný S1/P1 main title, případně jediný film bez main Season title;
ambiguita nevytváří metadata anchor. `parent_prefix` kopíruje textový snapshot,
source title ID zůstává historickou provenance. Refresh stejné identity text
nemění; relink/unlink zachová choice a vrací basis mismatch. Rebuild načítá a
klonuje choices, zahrnuje je do stale-plan fingerprintu a chrání ownery před
automatickým odstraněním, aniž vzniká manual hierarchy authority.

Čistý [component sanitizer](../app/physical_naming_components.py) používá
verzovanou policy `v1`: NFC pouze v projekci, portable character mappings,
whitespace/dot/device pravidla a hard limit 255 UTF-8 bytes. Vrací raw text,
preview, délky, transformace a diagnostics; naming snapshot nepřepisuje.
Collision guards porovnávají components ve stejném parent namespace; soft
budget 240 UTF-16 units vyžaduje explicitní absolutní Windows klientskou
cestu od budoucího planneru. 70 code points je samostatný readability review
údaj, nikoli hard limit.

[Foundation formatter](../app/physical_naming_formatters.py) přijímá již
resolved identity, neparsuje ji z prefixu ani filename. Skládá root/Season,
Episode s volitelným Part/MP a schválené supplementary labels včetně exact
Recap Decimal pozice. Lowercase extension je odděleně validovaná; finální
filename byte limit zahrnuje suffix i extension. D01 podporuje také Film/Bonus/CM/Menu bez implicitního ordinalu. D03
přidává suffix ze sanitized explicitního variant manual labelu. Schválené supplementary grouping je samostatná Physical
Layout doména; formatter pouze skládá filename components; cílové cesty plánuje read model níže.

Physical Naming / Pojmenování má samostatnou centrální sekci `/naming-review`
a shared [derived review model](../app/naming_review.py) pro frontu a
homepage/collection/title badges. Výchozí K vyřízení obsahuje A/B/C review,
basis mismatch a technicky neplatný effective název; bezpečné defaults,
platná potvrzení a inherited děti nevytvářejí práci. Kandidáty mají raw text,
portable preview, provenance, délky a diagnostics; current není automatická
autorita a nejednoznačný current prefix se nehádá z filename.

Save/reconfirm/reset používají naming service, serverové kandidáty a stale-form
fingerprint; GET a custom preview jsou read-only. Synonym keys nezávisí na
pořadí seznamu. Season-only supplementary nabídne snapshot textu relevantních
Partů bez změny Part authority. Limit 255 UTF-8 bytes zahrnuje i známé filename
suffixy a inherited prefix dependencies; nepodporovaná grammar se neodhaduje.
Běžné UI display resolvery physical choices nepoužívají. Target Planner
kontroluje full namespace kolize a známý Windows client budget; filesystem
execution neexistuje a žádnou Windows base path neodhaduje. Naming
persistence přidala schema v7 a layout schema v8, bez automatického backfillu.
Produkční DB používá v9; Naming Review je uzavřená s 61 choices a actionable 0.

### V6 physical layout foundation

**Naming ≠ Layout ≠ Hierarchy.** Naming určuje fyzický text, Layout grouping
authoritative title a Hierarchy root/Season/Part attachment. Title-only
`PhysicalLayoutChoice` má unique FK s delete cascade, `layout_kind`, UTC
`confirmed_at` a versioned deterministic `basis_snapshot_json`; nemá folder
text ani novou metadata, content, hierarchy nebo numbering authority.

Row vzniká pouze explicitním lidským confirm, i pro potvrzení defaultu.
[Service](../app/physical_layout_service.py) poskytuje confirm/reconfirm/reset
s caller-owned transakcí, bez autoflush nesouvisejících pending změn. Reset
row smaže; absence znamená bez uloženého lidského rozhodnutí. Batch loader
připraví neměnné scalar evidence pro čistý [resolver](../app/physical_layout.py),
který nevytváří rows a nepersistuje review flags.

Kinds jsou `own_folder`, `shared_ova`, `shared_specials`, `direct_season`,
`extras_openings_endings`, `extras_promo`, `extras_bonus`, `extras_menus`.
Schválená taxonomy je `OVA/`, `Specials/`, direct Season pro příběhový
Preview/Prologue/Recap a `Extras/Openings & Endings/`, `Extras/Promo/`,
`Extras/Bonus/`, `Extras/CM/`, `Extras/Menu/`; podrobnosti jsou v
[layout kontraktu](V6_NAMING_CONTRACT.md#physical-layout).

Own confirmed metadata znamenají candidate na own folder, nikoli automatický
own folder. Bez choice jsou strong/optional candidates unresolved human
Layout Review. Mini Dra je reference strong candidate; singleton Oresuki může
člověk potvrdit do shared OVA. Authoritative Season Preview zůstává direct
Season i s own metadata. Interview grouping ve Special je pro resolver
nejednoznačný (shared Specials nebo Extras Bonus), bez změny content
klasifikace. Pro #279 je schválené lidské rozhodnutí `extras_bonus` při
zachování typu Special; foundation jej nebackfilluje, takže bez uložené
choice zůstává unresolved.

Basis sleduje title/collection identity, authoritative root/Season attachment,
effective grouping profile, vlastní confirmed provider identity nebo absenci
a safe logical count bucket. Count používá společnou supplementary inventory,
takže validní duplicate secondary, varianty a complete MP nezvyšují logical
items; duplicate evidence, která se nesloží, ponechá count unknown.
Metadata/naming text, release text (IV marker, název složky), refresh
timestamp, provider episode count ani physical row count nejsou identity;
release text pouze blokuje shared default. Relevantní move/relink/unlink
zachová choice a otevře derived basis mismatch; nový title po splitu choice
nedostává.
Neodlišené nečíslované položky stejného typu mají count unknown, nikoli odhad
z physical rows. Jednoznačný shared grouping lze resolve-nout i při nejistém
countu; řešení numbering/identity problémů zůstává jiné doméně.

Rebuild chrání layout ownera, klonuje choice do detached projekce, zahrnuje ji
do stale-plan fingerprintu a ověřuje preservation parity. Choice nikdy
nevytváří membership selector. Scanner ji nevytváří ani nereconfirmuje.
Compatibility 7→8 vytvoří pouze prázdnou tabulku a constraints/index, bez
backfillu nebo rekonstrukce; stabilní startup na aktuální v9 je no-op.
Cílové cesty nyní odvozuje read-only Target Planner foundation; filesystem
execution není implementovaný.

### V6 Target Planner read-only foundation

Foundation je implementovaný jako backend/read model bez UI,
nové persistence nebo schema změn. [Immutable types](../app/target_planner_types.py)
a [čistá projekce](../app/target_planner.py) používají současnou Naming, effective
Hierarchy, canonical numbering, Physical Layout, explicitní varianty a duplicate
validity. Videa se routují podle owner IDs z DB; parser není owner authority.
P1/P2 jedné Season sdílejí fyzický Season container, supplementary Part=None se
nemění. [D01–D06](V6_NAMING_CONTRACT.md#schválený-target-planner-contract-d01d06)
řeší Film/Bonus/CM/Menu, variant suffix, preservation quarantine a subtitles.

[Loader](../app/target_planner_service.py) požaduje clean session, načte celou
library v jedenácti SELECT queries nezávisle na počtu Video/Subtitle rows,
hydratuje vztahy bez business změn a předá immutable scalar evidence shared
resolverům. SQLite entrypoint používá `mode=ro` a konzistentní read transaction,
bez aplikačního startupu/migrací/scanneru. Session new/dirty/deleted zůstává 0/0/0.

[Filesystem/preflight](../app/target_planner_filesystem.py) provede jeden traversal
na snapshot, nefollowuje symlinks, do `#recycle` nevstupuje a nic fyzicky nemění.
Regular files mají explicitní kategorii, chybějící/nonregular/error evidence
zůstává viditelná. Pouze archives čte pro SHA-256 copy evidence; média neprobuje.
Records obsahují kind/ID, source/optional target, planned action, READY/WARNING/
BLOCKED/REVIEW, diagnostics a provenance; duplicate safety a subtitle compatibility
jsou explicitní. Actions KEEP/MOVE/QUARANTINE/REVIEW jsou pouze návrhy; DELETE není.

Preflight kontroluje full namespace exact/casefold/uppercase/file-directory
kolize, auxiliary vs managed namespaces, component limit a runtime NAME_MAX/
PATH_MAX. Windows 240 UTF-16 soft budget vyžaduje skutečný client root; jinak je
NOT_CHECKED / PRE_EXECUTION_REQUIRED. Výsledek i snapshot mají deterministic hash.
Duplicate diagnostic nikdy neopravňuje purge: budoucí delete vyžaduje čerstvou
physical-primary regular-file existence a další D04 gates bezprostředně před akcí.
READY quarantine move a purge readiness jsou oddělené (`purge_state` je vždy
PRE_EXECUTION_REQUIRED). Side assets se přiřazují jen vlastnímu secondary;
secondary v library rootu ani adresář s nepřiřaditelným souborem nejsou accounted.

[CLI](../app/tools/target_plan.py) vypíše JSON bez uloženého manifestu a umí dva
fresh běhy s porovnáním records/counts/statuses/collisions/hash a DB fingerprintu;
exit status odliší čistý plán (0), BLOCKED/REVIEW (2) a selhání evidence (1).
Source archives se zachovávají flat v Subs jako READY s info diagnostikou
neklasifikovaného obsahu; auxiliary v root Extras se skutečným subtree; root TXT
zůstává KEEP. confirmed_no_match je subtitle present/video missing,
nikoli quarantine. D06 safe continuation používá jen přesně doloženou ručně
confirmed series; nejistota zachová original subtitle přímo v rootu.
P2C, execution, NAS fyzický cleanup, purge action a Completeness UI jsou pending.

[Container locator projection](../app/target_planner.py) zachovává single-leaf
locator a pro řízený CM/Promo fan-out `extras_promo` jednoho ownera vrací explicitní
root/Season `Extras` anchor. File-level targets mají své CM/Promo leaf folders;
owner IDs ani grouping selectors se nemění. Neznámý fan-out je Review, žádné
obecné LCA guessing. Shared Season P1/P2 locators zůstávají podporované.

`Subs`/`Duplicates`/`#recycle` jsou reserved technical roots. Flat Subs archive
je owner-less KEEP bez content inspection; nečekaný asset zůstává Review.
Quarantine používá DB duplicate/compatibility nebo explicitní side provenance,
nikoli cestu jako authority. Settled assets mají KEEP se zachovanými duplicate
diagnostics. Side accounting má explicitní stavy COMPLETE, INCOMPLETE a UNKNOWN;
jen COMPLETE může splnit tuto podmínku future purge, stále s fresh primary gate.
Quarantine je povolená ve všech třech stavech. Po reloadu quarantine bez carried
execution evidence je accounting UNKNOWN, nikdy implicitně COMPLETE.
`#recycle` je mimo aktivní knihovnu: excluded snapshot/count context bez traversal,
inventarizace, oprávnění, obnovy či execution; Completeness jeho obsah ignoruje.
Stale DB-known Video, subtitle nebo carried side asset s locatorem uvnitř se neztratí:
má stávající missing-file stav `source_missing_or_not_regular` bez targetu, tedy
bez recovery authority a bez požadavku na přístup do koše.
Technické roots členů neposkytují collection/title locator authority;
`Video.root_folder='Duplicates'` zůstává pouze physical locator.

[Pure post-state simulation](../app/target_planner_post_state.py) aplikuje approved
file paths a container locators na detached scalar evidence/snapshot, přenese
ověřenou side-asset evidence a znovu použije Target Planner. Ověřuje settled
KEEP, classification/coverage parity, target a locator fixed point i namespace
collisions včetně existujících prázdných directories. Nevytváří manifest a
nevolá SQL, filesystem writers nebo scanner. CLI `--post-state` vypíše také
post-state plan, locators, provenance, convergence diagnostics a deterministic
hash. `verify_post_state` přijímá explicitní immutable `DuplicateExecutionEvidence`
pro každý secondary: jeho ID, primary ID, identities/paths jen jím vlastněných
side assets (sousedé v adresáři se nepřiřazují), tri-state accounting, classification provenance a případné archive identity/hash
evidence. Jsou to execution facts, nikoli AnimeDB domain authority; žádné schema
se nepřidává. Immutable execution manifest tuto přenositelnou evidence nese
přes JSON round-trip. Uzaki secondary archive může být ověřen touto
evidence bez nového hashování primary ZIP v Subs. Bez provenance je REVIEW.
Regression gate aplikuje přesné locator-only patches na scratch DB kopii,
reloaduje přes normální loader a porovná celý plán i domain rows s pure projekcí;
negative reload bez evidence nesmí poskytovat purge-safe accounting.
`Video.filename` je persisted source/parser evidence, immutable během V6
reorganization; `Video.relative_path` je current physical locator. Detached
planner evidence ji nese jako `source_evidence_filename` a post-state ji zachová.
[Current physical filename](../app/video_paths.py) je basename locatoru; používají
jej technical UI labels, filename sorting/similarity a physical extension budget.
Parser/numbering, variant hints a manual split source patterns dál používají
`Video.filename`. Manifest DB locator patches toto pole nemění; source evidence
a target locator jsou oddělené. Skutečný patch executor zatím neexistuje.

P2C není required pro fyzickou reorganizaci ani read-only post-state verification,
ale je required před ordinary write-capable rescanem reorganizovaného canonical
tree. Verifier používá persisted paths a DB-known identity, bez owner inference.
Budoucí execution vyžaduje zastavený scanner/inventory writer nebo explicitní
maintenance boundary; její mechanismus zde není implementovaný.
Dnešní scanner při create používá physical basename a při changed-file update
jej zapisuje zpět do `Video.filename`; zároveň klasifikuje podle current path.
Zachování historical evidence při canonical rescanu patří do P2C, není zde změněné.

`UnresolvedExternalSubtitle.filename` má stejný source/parser contract; V6
patchuje pouze `relative_path`. Audit reads/writes rozlišuje:

| Použití | Význam a současné chování |
| --- | --- |
| `subtitle_review.subtitle_candidates` | A: parserový subtype/číselný hint z původního `filename`; beze změny. |
| `subtitle_review._rank_candidate` | B: podobnost current basenames z `relative_path`, stejně jako u Video. |
| `templates/media_check.html` | B: fyzický label z current locatoru; source evidence se nepřepisuje. |
| `target_planner_service` | A: detached `source_evidence_filename`; fyzické umístění z `relative_path`. |
| `scanner.service._sync_external_subtitles` | C / P2C debt: create bere basename; oba update branches (včetně confirmed_no_match) dnes přepisují filename current basename. Lifecycle zatím beze změny. |
| `subtitle_review.reopen_manual_subtitle_link` | C: vytváří novou unresolved row s basename při reopen; ExternalSubtitle původní source filename nenese. Zachování evidence přes převody rows je lifecycle debt, bez nového schema nyní. |

Manual assign převede unresolved row na owner-less ExternalSubtitle; rozhodnutí,
rejection a reopen workflows žádný existující unresolved filename nepřepisují.
Model declaration není dodatečná authority a schema se tímto contractem nemění.

### Immutable execution manifest a read-only preflight

[Manifest foundation](../app/target_execution_manifest.py) zachytí jeden fresh
Target Planner result do immutable versioned payloadu se stabilním canonical
SHA-256. Envelope creation/acknowledgement timestamps nemění semantic hash.
KEEP/MOVE/QUARANTINE actions, exact locator-only patches, target directories,
warnings a carried duplicate accounting/archive evidence přežijí strict JSON
round-trip. Dry-run používá fresh planner pouze pro stale/parity check;
schválené targets nenahrazuje. Žádná DELETE action ani filesystem executor není.

Criticality PRIMARY/LOW řídí pouze future failure severity. Mini Dra a další
standalone Bonus s explicitní own-content authority jsou PRIMARY; generic Bonus
ani DramaCD nemají automaticky LOW. Současných 11 Overlord/Tenki Bonus položek
má explicitní human LOW decision přes existující manifest authority; přesný
[allowlist a scope](V6_EXECUTION_MANIFEST.md#criticality-a-preservation) nepoužívá filenames.
M:N subtitles dědí nejvyšší valid compatible severity;
D06 confirmed_no_match má mandatory PRIMARY preservation bez Video ownera.
Warning acknowledgements jsou konkrétní IDs/class/objects s actor/time evidence,
nikoli class wildcard nebo globální bypass.

Preflight vyžaduje externí maintenance výluku scanner/inventory writeru až do
verification, skutečný Windows client root s 240 UTF-16 budgetem, baseline-bound
snapshot a DB backup, safe write capability a mount rename no-replace support.
Current read-only production audit je záměrně NOT_READY při chybějící evidence.
Žádný snapshot, backup ani write/rename probe se nevytváří. Journal foundation
definuje data/serialization, nikoli vykonávané filesystem state transitions.
Podrobný [execution contract](V6_EXECUTION_MANIFEST.md) popisuje preconditions,
criticality, stale checks a hranici budoucího executor/recovery tasku.

### V6 physical layout review

Layout Review UI je záložka **Rozložení** na `/naming-review/layout`, vedle
**Názvy** na `/naming-review`. Hlavní navigace zůstává Pojmenování. Obě
záložky ukazují oddělené derived počty; společný status v knihovně a detailech
zahrnuje i layout práci a při ní odkazuje přímo do scope Rozložení. Naming Review semantics se nemění.

[Sdílený read model](../app/layout_review.py) vytváří frontu, applicable
kandidáty, doporučení a náhledy nad batch evidence. K vyřízení obsahuje jen
skutečná lidská rozhodnutí a stale choices; bezpečné shared/direct Season
defaults jsou ve Vše označené Odvozené. Vlastní metadata nikdy sama
nepotvrzují vlastní složku. Mini Dra nabídne celý sanitized PhysicalNaming,
Oresuki doporučené OVA a interview Special také Extras/Bonus bez změny typu.
MP, varianty a validní duplicate copies nezvyšují zobrazený logical count;
nejistá identita se ukáže jako neznámá, nikoli jako počet souborů.

Save/reconfirm/reset používají foundation service v caller-owned transakci.
POST znovu ověří current context, fingerprint a serverové kandidáty; root
owner nemůže potvrdit direct Season. GET je read-only. Fingerprint sleduje
basis, uloženou layout choice a applicability kandidátů, nikoli naming text,
metadata Romaji, provider count, filename nebo timestamps. Platné naming
přejmenování mění preview, nikoli layout potvrzení; naming problém má vlastní
odkaz do Názvy a fail-safe own-folder náhled. Žádný folder text se neukládá.
Reconfirm se nabízí jen pro stále použitelnou uloženou volbu; položka bez
použitelného rozložení odkazuje do Hierarchie místo prázdné volby.

Produkční DB používá schema v9 a obsahuje 32 potvrzených lidských
`PhysicalLayoutChoice`, actionable 0 a basis mismatch 0. Naming i Layout mají
uzavřenou actionable frontu. UI není target planner
a neprovádí žádné filesystem operace.

### Fyzická evidence a logická struktura

`CatalogCollection → CatalogTitle → Video` odděluje anime, jeho logickou část
a fyzický soubor. Title může představovat Season, Part, Film, OVA, Special
nebo doplněk; není ekvivalentem fyzické složky ani jedné epizody.
`Video.relative_path` identifikuje soubor. Logické přesuny nemění filename ani NAS.

V6.3-P1A je committed a používá `CatalogTitle.id` jako persisted logical
owner identity.
Rebuild intents, specs, projekce, apply i verification rozlišují existující
owner ID a deterministic plan-local handle nového title. Locator index vrací
0..N kandidátů; nejednoznačný lookup nevytváří membership a vyžaduje review.
V6.3-P1B je committed a produkční rollout v8→v9 je dokončený
(`user_version = 9`). `CatalogTitle.relative_root_path` je NOT NULL,
NON-UNIQUE s neunikátním lookup indexem; shared Season persistence je podporovaná
schema i runtime. Více persisted owners smí sdílet locator bez změny logické
identity. Rollout zachoval všechna title IDs a původní locator hodnoty,
včetně `.catalog-part-*`; žádné canonical cesty nebackfilloval.

Current grouping authority je `CollectionGroupingDecision.target_collection_id`
(FK) a `grouping_decision_titles.catalog_title_id` (relační selected-owner rows).
Legacy `target_collection_path`, `selected_title_paths_json` a jednotlivé
`title_path_snapshot` jsou pouze historie, nikdy runtime resolver key.
Produkční migrace zachovala 13 decisions a převedla 24 legacy refs na 18 current
title owner FKs a 6 history-only refs s NULL owner FK; target collection FK má
12 decisions. Převod použil pouze jednoznačně existující ownery. Šest historical
missing paths zůstalo zachovaných bez guessed successorů a bez current authority.
Nejednoznačný backfill migrace atomicky odmítne. Smazání title nebo target
collection nastaví příslušný FK na NULL a zachová evidence; nové objekty na
stejné cestě vztah neobnoví.
Smazání decision odstraní jeho reference rows. Explicitní nová/aktualizovaná
lidská rozhodnutí zapisují IDs, takže přejmenování locatoru autoritu nezmění.

Locator je evidence, nikoli canonical target container. Ten bude derived z
Naming + Hierarchy + Layout; žádný canonical path field ani backfill neexistuje.
Legacy series URL při více title kandidátech vrací 409; interní navigace používá
title ID. Virtuální title handles se při vytváření explicitně rezervují přes
existence lookup, bez globální uniqueness běžných fyzických locatorů.
V6.3-P2B [canonical parser core](../app/canonical_filename.py) je implementovaný.
Pure `parse_canonical_stem` a filename
adapter s explicitní lowercase extension vracejí immutable discriminated
Episode/Supplementary/Recap identity, oddělený Media Part a grammar version `v1`.
Episode číslo má explicitní `canonical` coordinate provenance; Recap používá
exact Decimal. Přijímá pouze podporovaný formatter language a sanitizovaný
prefix zachovává jako textovou evidence. Supplementary formatter je
non-injective: výsledek `unique/ambiguous/not_canonical` uchovává všechny platné
lexical alternatives v deterministic pořadí bez precedence. Schválený inverse
proto vyžaduje následné authoritative context resolution. Parser žádného
ownera neřeší ani nevytváří.
Scanner routing P2C není implementovaný; produkční chování scanneru i tolerantní
legacy parser zůstávají beze změny. Existing membership/explicit selectors zachovávají owners;
nový soubor `... - S02P02E03.mkv` při ambiguous locatoru zůstává unresolved/review.
Naming/Layout choices zůstávají owner-ID based a čistá změna locatoru nemění
jejich basis. Read-only Target Planner D01–D06 foundation je implementovaný;
filesystem execution, fyzická reorganizace, purge a Completeness UI jsou pending.

Produkční DB po rollout: size `7213056`, mtime_ns `1791062165382571032`,
SHA-256 `907ca68f2fbac1b1fb61413c0c8128a719afbe9a3cfc6f898d0d3295aa6107de`.

Season a Part jsou různé strukturální údaje. `Part 2` není `Season 2`.
Více Season titles se stejným season číslem v collection vyžaduje unikátní
explicitní Part čísla; rozsahy epizod je samy nedoplňují. Legacy Cour je čitelný,
ale nenabízí se jako nová uživatelská jednotka. Generické `title` je technický
fallback inference, nikoli nová autoritativní ruční klasifikace.

### Ruční rozhodnutí, parser a presentation

- Úplný aktivní manual hierarchy snapshot má přednost před automatic poli,
  včetně explicitního `NULL` season/Part kontextu. Neaktivní hodnoty nejsou
  autoritou. Historicky neúplný snapshot je chráněn před destruktivním přepisem,
  ale není effective hierarchy ani verified; vyžaduje kontrolu. Numbering má
  pro aktivní neúplné legacy snapshoty omezenou kompatibilní projekci.
- Autorita výběru videí pro manual split je nezávislá na výsledném assignmentu:
  explicitní rozsah, filename pattern nebo `ManualSplitRuleVideo`. Samotné
  `Video.catalog_title_id` se na selector automaticky nepovyšuje. Rozsah a
  filename pattern jsou autoritou nad celou collection, proto obsah, který
  nepokrývají, vyžaduje review. Explicitní `ManualSplitRuleVideo` je autoritou
  pouze nad konkrétně vyjmenovanými videi: chrání jejich ruční assignment, ale
  collection do manual-split režimu sám nepřepíná a po budoucích souborech
  shodu s pravidlem nevyžaduje. Při odebrání posledního explicitního selectoru
  se uvolněný assignment ještě před commitem finalizuje, a to výhradně použitím
  jediného už existujícího title na vlastní kanonické cestě videa. Pokud by
  assignment vyžadoval domněnku nebo založení nové `CatalogCollection` či
  `CatalogTitle`, zůstane video unassigned a jde do review; odebrání lidské
  autority nikdy nezakládá novou strukturu. Jiný selector, collection-scope
  range/pattern i chráněný manual hierarchy snapshot nad vlastním dosavadním
  zařazením videa mají dál přednost. Uvolněná videa se finalizují nad neměnným
  snapshotem právě zasažených ID, ne nad živou ORM relationship, kterou write
  současně mění.
- Strukturální autorita a membership autorita jsou oddělené osy. Manual
  hierarchy snapshot chrání strukturu a umístění svého vlastního title, a proto
  potvrzený merge nebo přesun přežije automatickou rekonstrukci. Sám o sobě
  ale není důvodem přiřadit do toho title jiné video nalezené pod jeho cestou:
  membership vzniká jen z explicitní autority, z chráněného dosavadního
  zařazení videa, nebo z jednoznačné path inference. Nejednoznačnost je review,
  ne automatický odhad.
- `Video.file_type` je uložená parserová klasifikace. Obecný effective typ má
  prioritu video manual → konkrétní filename/raw typ → supplementary kontext
  title. Bonus/Extras container proto nezakrývá konkrétní Special/OVA/OP evidence.
  Detail videa nabízí „Typ obsahu“ nad existujícím `content_type_manual`,
  včetně Episode a Film; „automaticky“ odstraní pouze tuto ruční autoritu.
- Raw filename/parser evidence se nepřepisuje jen kvůli presentation nebo
  ručnímu rozhodnutí. Effective resolvery čtou autority; scanner/migrace mohou
  v určeném write workflow aktualizovat automatickou evidenci a projekce.
- Manual display title má přednost. Společné title/collection resolvery
  respektují Romaji/English/Native preference a vlastní konzervativní fallbacky;
  název supplementary child části nepřejmenuje běžnou hlavní collection.
- `sort_order_manual` znamená explicitní uživatelský override, nikoli kopii
  automatického pořadí. Existující legacy hodnoty se bez doloženého původu nemažou.

### Nezaměnitelné osy

| Osa | Současný význam |
| --- | --- |
| Typed local ordinal | Lokální identita v namespace `(CatalogCollection, authoritative structural context, effective video type)`; Preview/PV namespace sdílejí. Není automaticky providerové číslo. |
| Media Part | Ručně určený fyzický segment jedné logické položky, `Video.media_part_number`. |
| Video variant | Ručně potvrzená release/content skupina jednoho title; její reprezentace konkrétní epizody sdílí stejnou logickou identitu. |

Supplementary ordinal, Media Part a variant jsou tři různé osy.
Ani jedna sama nenahrazuje zbývající dvě ani neprokazuje duplicitu.
Stejně tak candidate metadata není confirmed metadata a `not_required`
není chybějící ani ručně potvrzená metadata.

## Hierarchy workflow

Scanner a compatibility rekonstrukce dokončují hierarchii společným evaluatorem;
hierarchy write akce používají shared finalization po změně členství, struktury
a číslování. Stabilní startup bez potřebného upgradu tuto práci neopakuje.
Rebuild má oddělený snapshot/plan/apply a kontrolu zastaralého plánu; není to
fyzický přesun médií. Konfliktní manual selectors se neřeší „prvním matchem“.

Evaluator odvozuje collection status a poznámku: blocking issue znamená
review/conflict; bez něj je collection verified jen s úplnou manual authority
všech jejích titles, jinak automatic. Derived supplementary review je navíc
read-only vrstva, proto samotný uložený status nevystihuje celou review frontu.

Manual collection merge/přesun uchovává persistentní grouping authority.
Změna parent collection obnovuje automatic strukturu závislou na kontextu,
nepřepisuje complete ani chráněnou incomplete manual authority. Explicitní
akce přepočtu automatic hierarchy řeší změněný kontext, ne čtení stránky.

Bezpečná direct-root řada od E1 může založit automatic S1; existence jediného
title sama nestačí. Pro automatickou souvislou direct-root řadu bez chráněné
manual authority je 15–24 standardních epizod soft warning a více než 24
safety review. Délka nikdy sama nevytváří hranici sezóny; supplementary obsah
se do tohoto profilu nepočítá.

Hierarchy Review ukazuje pracovní frontu strukturálních/numbering problémů,
návrhy grouping a globálně nezařazená videa. Chybějící či nekonzistentní řetězec
Video → Title → Collection je problém i mimo fyzický root. `/unassigned-videos`
řeší logické přiřazení; `/root-videos` je nezávislý inventář fyzického rootu.

Derived supplementary review doplňuje uložený hierarchy stav o chybějící
ordinaly v opakovaných typech, kolize a porušené duplicate identity. Čtení
nepřepisuje `hierarchy_status`, manual snapshot ani verified timestamp.

Neblokující návrh **Pravděpodobně doplňkový obsah** je nápověda pro automatické
nebo nepotvrzené zařazení explicitně označeného supplementary videa. Potlačí jej
ruční typ obsahu videa, title s kompletním manual snapshotem shodného typu, nebo
lidské umístění: title s kompletním manual snapshotem spolu s explicitním
selectorem (`ManualSplitRuleVideo`) právě tohoto videa do něj. Při rozdílném typu
samotné verified title či collection nestačí, protože video mohlo do ověřené
struktury přibýt automaticky. Subtype videa a typ title/kontejneru jsou oddělené
osy a nemusí se shodovat (např. OVA ve Specials, PV v Bonus).

Dolní sbalený index **Všechna anime** obsahuje reálné collections s evidovanými
videi, nikoli prázdné placeholdery či technický root `.`. Je navigací nezávislou
na horní frontě: vyřešené anime zůstává dostupné přímým odkazem do review detailu.
Řadí se podle display názvu, filtruje klientsky case-insensitive/NFKC a ukazuje
počet zobrazených položek. Sdílená badge precedence je:

1. aktuální hierarchy/numbering nebo derived supplementary problém → **Vyžaduje kontrolu**;
2. existující neblokující upozornění na délku → **Zvláštně dlouhá sada epizod**;
3. uložená verified hierarchy bez těchto problémů → **Ověřeno**;
4. jinak → **Automaticky OK**.

## Číslování a doplňkový obsah

### Epizody, duplicity a varianty

Standardní `LogicalEpisodeIdentity` je `(CatalogTitle, season_episode_number)`.
Není to samostatná persistentní Episode tabulka. Lokální, season, absolute
a external čísla nejsou zaměnitelná; canonical pole jsou řízené projekce
z numbering autority a lokální evidence. Ruční číslo/numbering režim mají
své explicitní workflow včetně náhledu hromadné opravy. Sekvenční numbering
seskupuje úplné Media Parts podle jejich ordinálů uvnitř již známé logical
identity, nikdy podle pořadí filename. Více variantních lanes spojí pouze tehdy,
když mají všechny přesně stejnou množinu již známých logical identities; bez
takového důkazu preview i apply operaci odmítnou místo pozičního odhadu.
Standardní Episode musí mít logical episode number vždy, i jako singleton;
chybějící číslo vyžaduje review. Toto pravidlo nemění strukturální čísla
Season/Part/Cour.

Explicitní Part (`SxxPyyEzz`) používá po ručním potvrzení
`numbering_mode=part_local` vlastní standardní namespace E01..EN uvnitř svého
`CatalogTitle`. V tomto režimu `episode_start_offset` znamená offset zdrojového
(filename) číslování, nikoli absolutní osu; ruční číslo videa je přímo
Part-lokální. Samotné přiřazení či potvrzení Part struktury čísla nemění;
Part-lokální číslování je samostatný náhled a potvrzení nad jednoznačnou souvislou
řadou současných logical identities. Mezera, kolize, nekompatibilní ruční číslo
nebo Recap vyžadující převod jsou Review. Absolutní číslo zůstává odvozenou
informační projekcí z bezpečně známých počtů předchozích canonical titulů, jinak
prázdné; není canonical identitou ani podkladem názvu souboru.

`duplicate_of_video_id` je zachovaná ruční evidence potvrzené duplicity, nikoli
sama důkaz její současné effective platnosti. Shared resolver rozlišuje `VALID`
(obě současné identity jsou známé a shodné), `INVALID` (současná data prokazují
rozpor nebo chybí primary) a `UNKNOWN` (identitu nelze bezpečně ověřit). Pouze
`VALID` secondary se kolabuje a nezvyšuje logical/variant/completion count.
`INVALID` i `UNKNOWN` zůstávají aktivní a ruční evidence se nemaže; první je
konflikt, druhý review bez automatického odhadu. Nevzniká automatická náhrada
primary ani mazání souborů.

Pro nečíslovaná supplementary videa je samostatné explicitní potvrzení
fyzických kopií téhož obsahu. Secondary ukládá
`duplicate_confirmation_kind=unnumbered_supplementary_same_content` vedle
`duplicate_of_video_id`; samotné `suspected` zůstává jen poznámkou. Resolver
vazbu uzná pouze při současné shodě title, collection, typu a strukturálního
kontextu, bez konfliktního ordinalu, varianty nebo Media Parts. Při pozdější
změně evidence vazba zůstane uložená, ale přestane kolabovat logical count a
vrátí se do Review. Běžné duplicity nad známou identitou se nemění.

`VideoVariantGroup` je ruční skupina v rámci title, s volitelným release source
a content variant. `NULL` assignment je neurčeno, ne implicitní výchozí varianta.
Různé potvrzené skupiny mohou reprezentovat jednu epizodu; neoznačené či
nevysvětlené opakování uvnitř identity vyžaduje review. Parserové TV/UC/A/B hinty
jsou návrhy, ne autorita. Assignment je vratný; přesun videa do jiného title
nepřenáší cizí variant group.

Media Part rozděluje fyzickou reprezentaci, nikoli title/Season. Úplná ruční
sada 1..N může tvořit jednu logickou supplementary položku; mezery a opakování
segmentů mají diagnostiku. `OVA P1/P2` s Media Part autoritou samo neprokazuje
dva OVA ordinaly. U známých supplementary identit se segmenty posuzují uvnitř
konkrétního ordinalu a potvrzené varianty.

Effective content type Recap smí existovat pouze v authoritative Season
structural contextu. Je to nestandardní doplněk, ne běžná epizoda, a jako jediný
effective content type smí používat fractional chronologickou pozici. Ruční
desetinná pozice je přesně uložena v desetinách, nezvyšuje standardní logical
count ani se nezaokrouhluje do integer epizodní osy. Ruční pozice má přednost
před přesnou parserovou fractional hodnotou (např. 5.5, 24.9 či 24.25);
parserová přesnost se nezkracuje. Jde o chronologickou identitu, nikdy o
typed ordinal, a počet Recapů ji nepřečísluje. Episode používá integer logical
episode number; ostatní non-Episode typy používají integer typed ordinal,
případně podle současných multiplicity pravidel žádný.

Pokud uživatel raw/parser Recap explicitně ručně překlasifikuje na Bonus nebo
jiný non-Recap typ, raw evidence zůstává zachována, ale fractional Recap
numbering už není effective. Structural assignment nebo move samo o sobě není
content classification: nemění `Video.content_type_manual` ani automaticky
nemaže fractional manual authority.

### Supplementary ordinal

Společný resolver v `app/supplementary.py` pokrývá všechny canonical non-episode
video typy z `VIDEO_CONTENT_TYPES`, včetně Film, Bonus, Menu a explicitního Other.
Chronologicky očíslovaný Recap je vyňat; Recap bez takové pozice používá obecné
pravidlo multiplicity. Existující Recap editor zapisuje chronologickou pozici.
Raw Other je také parserový fallback: bezpečně rozpoznaná běžná epizoda v hlavní
části zachovává episode semantics a nezařazené zero/fractional/A-B zachovávají
svou nonstandard diagnostiku. Nerozpoznaný obsah v hlavním kontejneru zůstává
review problémem; explicitní Other (video nebo kontejner) používá typed namespace.

Ruční video klasifikace a explicitní použitelné číslo mají přednost. Jinak je
potřeba bezpečná filename evidence odpovídajícího typu; broad Bonus/Special
kontejner přesný marker nezakrývá. Pouhé `Episode 14` přesunuté pod OVA není
automaticky OVA14. Čísla se nevymýšlejí z pořadí, abecedy, počtu souborů ani
z metadat. Rozpoznané TV/A/B suffixy nevytvářejí variantní autoritu.
Parserový Special05 se po ruční změně typu na Bonus nestává Bonus05.
Teprve kompatibilní evidence nebo explicitní manual ordinal 5 vytvoří Bonus05;
raw typ a filename evidence přitom zůstávají dostupné v detailu.

Ordinal je povinný až při 2+ distinct logical identities stejného effective
typu a authoritative structural contextu uvnitř collection, i napříč supplementary
titles téhož kontextu. Singleton může zůstat
bez čísla; existující bezpečný parser/manual ordinal se nemaže. Preview/PV
sdílejí namespace, OP a ED mají oddělené.

Structural context přebírá attachment z `CollectionPresentation` nad effective
hierarchy: hlavní Season/Part/Cour/title je vlastním kontextem, připojený doplněk
přebírá identitu této hlavní části. Pouhá shoda ordinalu nebo názvu souboru
není attachment. Současná projekce připojuje supplementary title jen při právě
jedné hlavní části odpovídající effective season contextu; chybějící či nejednoznačný
attachment zůstává v anime-level/root namespace dané collection. Nový season/part
odhad nevzniká. Complete manual snapshot má přes effective properties přednost;
neúplná historická pole nejsou effective ruční autoritou.

Multiplicity i collision review používají společný inventory: potvrzené
duplicate secondary nezvyšují count, různé potvrzené varianty známé identity a
úplné Media Parts téže identity tvoří jednu položku. Úplná nečíslovaná sada
Media Parts uvnitř jednoho title může být singleton. Samotné variant groups
bez společné identity ani neúplné segmenty nejsou důkazem totožnosti.
Stejný ordinal ve dvou různých structural contextech není kolize. Ve stejném
kontextu se posoudí společná identita i napříč supplementary titles; úplnost
segmentů ověřuje shared `media_part_total` před multiplicitou a collision review.
Variant groups z jiného title nejsou vysvětlením společného ordinalu. Missing ordinal při
multiplicitě, nevysvětlená kolize a neplatná duplicate vazba vyžadují derived
review bez zápisu hierarchy statusu. Detail videa a Hierarchy Review používají
stejnou structural namespace projekci v rámci collection. Metadata count nadále vyhodnocuje scope
konkrétního title; přísný inventory při nejasnosti vrací neznámý logical count.
Absence review u singletonu není sama důkaz připravenosti pro budoucí rename.

## Metadata

### Vyhledání, kandidáti a potvrzení

AniList je implementovaný provider pro search/fetch. Výsledky se uchovávají
jako kandidáti s evidence breakdownem, možností odmítnutí a ručního potvrzení.
Score je pomocná confidence s pevným maximem, nikoli pravděpodobnost ani
automatická autorita. Neznámá evidence se odlišuje od shody a konfliktu.
Batch search kandidáty hledá, nepotvrzuje je.

Potvrzení metadata completion vyžaduje `linked_manual` a aktivní primární
ruční `ExternalTitleLink` s `verified_at`; samotný status, candidate nebo uložený
metadata payload nestačí.

`ExternalTitleLink.lifecycle_state` popisuje současný vztah potvrzené vazby
k title: `active` (jediná metadata authority, vždy `is_primary`), `superseded`
(nahrazena explicitním potvrzením jiné vazby), `unlinked` (explicitně odpojena
bez náhrady) a `legacy_historical` (non-primary vazba z doby před lifecycle;
její historická příčina se neodhaduje). `is_manual` a `verified_at` zůstávají
faktickou evidencí dřívějšího potvrzení. Historické vazby se nemažou a nedodávají
completion, refresh, provider count, konflikt ani primary presentation; opětovná
volba téže identity reaktivuje stejný row. Metadata split přesouvá aktivní row
beze změny lifecycle. Odmítnutí kandidáta zůstává samostatnou osou
`MetadataCandidate.rejected_at`. Authority určuje sdílený resolver
v `app/metadata/link_lifecycle.py`.

Service spravuje link, normalizovaný `TitleMetadata`,
obnovení, odpojení a lock. Lock brání běžnému refreshi/přepsání bez příslušného
explicitního potvrzení. Metadata se nepoužívají k tichému přepsání ruční hierarchy.
Změna existence potvrzeného metadata payloadu nebo jeho `episode_count` je
současně změnou numbering evidence: metadata service proto ve stejné transakci
spustí shared finalization právě owning collection. Změna synopsis, artworku
nebo provider identity při stejné existenci payloadu a stejném countu tuto
projekci nespouští.

### Requirement a completion

`metadata_requirement_manual`: `NULL` = automaticky, `required` = vyžadována,
`not_required` = nejsou vyžadována. Manual rozhodnutí má přednost.
Automatická výjimka platí pouze pro neprázdný title, jehož všechna videa mají
přesný typ OP/ED/NCOP/NCED/Menu/CM; video manual přebíjí raw typ, broad title
kontejner tuto úzkou policy nemění. Film/OVA/Special/Bonus/Preview ani směs
standardních epizod s technickým obsahem tuto výjimku samy nezískávají.

Presentation rozlišuje potvrzeno / not_required / chybí. Potvrzená vazba má
přednost i při `not_required` a nemaže se. Collection má **Metadata OK**, pokud
jsou resolved všechny titles s videi; prázdné titles nevstupují do aggregate.
Výchozí fronta Metadata Check obsahuje required části bez potvrzení,
„Všechny části“ zachovává inspekci a batch hledání resolved části přeskakuje.

### Count/range není totéž co completion

Candidate i potvrzený detail používají společnou lokální count evidenci:
standardní logical identities, bezpečné supplementary identity nebo jednu
položku z úplné sady Media Parts. Duplicity, varianty a Recap standardní počet
nezvyšují. Supplementary count má také konzervativní legacy/singleton fallbacky;
nejde o V6 ordinal authority. Ambiguita se nemá vydávat za jistou shodu.

Detail vysvětluje fyzický versus logický počet a odlišuje neutrálně vysvětlený
rozdíl od warning. Provider count může zahrnovat Episode 0 či Special, který
je lokálně samostatný; není autoritou pro přepis číslování.
Metadata split má samostatný přísnější gate: potvrzená vazba, jednoznačná
souvislá lokální řada a bezpečný přesný subset. Vyžaduje preview a další
potvrzení; rozděluje DB title a vazby, nikdy fyzické soubory.

### Artwork a chyby provideru

Cover URL přichází z title metadat. Existující download/cache workflow vytváří
lokální originál a thumbnail s kontrolou typu/velikosti a atomickým publikováním.
Thumbnail presentation používá primary cover a bezpečný `/artwork/` mount; nepoužitelný,
chybějící či mimo cache mířící thumbnail vede na placeholder, ne nový download.
Collection vybírá první použitelný primary cover v hlavními částmi prioritizovaném
strukturálním pořadí, poté může použít jinou část. Detail části používá její
vlastní primary cover, nikoli automatický collection fallback.

AniList klient rozlišuje HTTP 429 (rate limit, kladný celočíselný Retry-After
v sekundách, je-li dostupný), známý temporary-disabled GraphQL payload při
HTTP 403 (dočasná nedostupnost AniListu) a obecné HTTP/network chyby.
Jiný 403 není automaticky globální outage. GraphQL `errors` i při HTTP 200
jsou chybou; UI dostává bezpečnou zprávu, ne raw exception či stack trace.

## Media Check

`VideoLanguageProfile` odděluje audio a subtitle fakta. Audio rozlišuje
Japonštinu, pouze angličtinu, jiný známý jazyk, neznámý jazyk a skutečnou absenci
audio stopy. Chybějící JP samo není chyba. CZ/SK dostupnost může doložit interní
stream, kompatibilní externí asset nebo ručně ověřený CZ/SK hardsub.
Pouze interní EN slouží jako fallback; externí EN je technická evidence.

Media Check nad fakty vede požadavek a nullable ruční workflow externích CZ/SK
titulků `seeking` (Sháním) a `unavailable` (Neexistují). Bezpečný scanner
`automatic_match` je faktická dostupnost bez další lidské práce; neposouzený
kandidát je jen možnost k posouzení. Pozitivní CZ/SK evidence má před markerem
faktickou přednost a `unavailable` s pouhým kandidátem zůstává uzavřené.
Scanner, kandidát, nekompatibilita, návrat na neurčeno ani jiný jazyk marker
nemažou; oba ukončí jen ruční potvrzení CZ/SK titulku jako kompatibilního pro
dané video, ve stejné transakci. Rozpor markeru s nalezenou evidencí je
odvozené upozornění bez uloženého stavu: u `seeking` výrazné s přímým
„Potvrdit kompatibilitu“, u `unavailable` pouze INFO. Výchozí pracovní fronta
„Titulky k vyřízení“ spojuje faktickou mezeru se Sháním reconciliation
a stejné pravidlo řídí souhrnné Media badge; faktický filtr „Doplnit CZ/SK“
se nemění. Pouze secondary s aktuálně `VALID` duplicate relation nevytváří
další povinnou completion jednotku; `INVALID` a `UNKNOWN` evidence zůstává
samostatnou completion položkou a jiné fyzické reprezentace se rovněž
posuzují samostatně.

Úzká OP/ED/NCOP/NCED policy nevyžaduje titulky a unknown audio je zde neutrální;
skutečná absence audia (`no_audio`) zůstává problémem. Video manual klasifikace je první autorita,
jinak přesný raw OP/ED/NC subtype předchází broad title kontejneru. Toto není
plošné „supplementary nepotřebuje titulky“ ani změna obecné content klasifikace.

`ExternalSubtitle` je fyzický asset podle `relative_path`, bez legacy vlastníka
`video_id`. M:N compatibility k jednotlivým Videos má stavy `automatic_match`,
`confirmed_compatible`, `confirmed_incompatible`; chybějící row je neurčeno.
Pouze první dva poskytují dostupnost. Ruční rozhodnutí přebíjí automatický
match; jeho zrušení obnoví platnou automatic evidenci, jinak unknown.
Kompatibilita se automaticky nepřenáší mezi BD/TV, A/B ani known/NULL variantami.
Unresolved workflow umožňuje preview a ruční přiřazení, ne přesun subtitle souboru.
Smazání Video odstraní jeho compatibility rows, nikoli sdílený fyzický asset.
Asset bez relationship zůstává evidencí, ne pokynem k automatickému mazání.

Sdílená normalizace v `catalog.py` podporuje běžné ISO aliasy a zachovává
dosavadní interní kódy, např. `cs`, `ja`, `deu`, `kor`, `zho`. Display resolver
přidává české názvy: např. **CZ – Čeština**, **JA – Japonština**, **KO – Korejština**,
**ZH – Čínština**, **? – Neznámý jazyk**. Používají jej jazykové selecty,
audio/internal/external detail a compatibility UI; kompaktní badge mohou
zůstat krátké. Hodnoty formulářů a URL se kvůli labelům nemění. Audio i již
existující interní subtitle stopa mají nullable ruční language override;
external subtitle má stejnou autoritu na úrovni sdíleného assetu. Override
přebíjí efektivní prezentaci, nikdy scannerem zjištěný raw/normalized záznam,
a jeho vymazání vrací detected hodnotu.

## UI a navigace

- Hlavní katalog seskupuje aktivní collections, nabízí search, sort a současné
  filtry. Hierarchy, Metadata a media/translation informace mají různé významy.
- Katalogový pracovní přehled dává název, lokální thumbnail a odvozené badge
  Hierarchie, Média a Metadata do čtyř čitelných sloupců; počty a technická
  cesta jsou rozbalitelné. Hierarchy badge používá stejný read-only resolver
  jako Hierarchy Review, metadata existující completion aggregate a media
  existující Media Check evaluátor. Ověřeno, Auto OK, Review, Konflikt a
  neblokující informace zůstávají vizuálně odlišné.
- Collection detail skládá hlavní části a supplementary skupiny. Doplněk se
  vnoří jen při jednom přesném matchi effective season čísla; chybějící nebo
  nejednoznačný match zůstává anime-level. Jednoznačná jediná část může otevřít
  přímo title detail, aniž zmizí anime-level sourozenec.
- Hierarchy Review je strukturální pracovní fronta i přímý navigační index.
  Metadata Check a Media Check jsou samostatná workflow, nikoli další hierarchy statusy.
- `/titles/{id}` je read-only přehled effective stavu. Tři sdílené statusové
  badge vedou do title-scoped editorů Hierarchie, Metadata a Média; běžná
  authority se na katalogovém detailu neupravuje.
- Title-scoped Hierarchy Edit vlastní typ části, číslování, content type,
  Media Part, varianty, duplicity a membership/confirmation workflow.
  Globální Hierarchy Review zůstává strukturální pracovní frontou. Nabízí
  lokální uložení a atomické „Uložit všechny změny“
  pouze pro úpravy existujících variant groups a ruční pozice Recapu. Dávka
  používá stejné domain helpers jako lokální uložení a jediný commit; příkazy,
  náhledy, potvrzení a destruktivní akce do ní nepatří.
- Title-scoped Metadata Edit vlastní requirement, provider vazbu, kandidáty,
  artwork, lock/update/unlink a metadata split. Po potvrzení metadata vazby
  standardně ukazuje potvrzená
  metadata a výběr ostatních kandidátů zavře. „Změnit metadata“ pouze otevře
  uložené kandidáty; provider search spouští až samostatná akce a první POST
  přesměruje přímo na viditelné výsledky.
- Title-scoped Media Edit zobrazuje všechna videa části. Jedno atomické
  preview/confirm/save mění přesně určené audio a internal-subtitle stopy,
  hardsub a ruční CZ/SK workflow marker. External subtitle language zůstává
  samostatný asset-level zápis. `None`, `seeking` a `unavailable` se prezentují
  jako Neurčeno, Sháním a Neexistují; „Mám“ je pouze factual derived stav.
- Hromadný editor je jeden sticky toolbar nad Media Edit seznamem. Prázdná hodnota znamená
  „Neměnit“. Bulk language změna pokračuje pouze tehdy, když každé vybrané
  video má přesně jednu stopu daného druhu; nula nebo více stop odmítne celou
  dávku před potvrzením. Každé odmítnutí popíše důvod, problematické položky a
  bezpečný další krok; partial update se neprovádí.
- Katalog má malé 44px portrait thumbnails. V collection detailu je artwork
  u všech `CollectionPresentation.primary_parts` a navíc anime-level Film/OVA/Special.
  Podřízené Film/OVA/Special ani běžné extras nemají cover, placeholder či prázdné
  odsazení. Jde o presentation roli, ne rozšíření main-content taxonomie.
- Sdílené thumbnail makro používá rezervovaný box, `object-fit: cover`,
  dekorativní `alt=""`, neutrální placeholder a lazy loading. Layout a dlouhé názvy
  se přizpůsobují menším viewportům. Nativní rozbalování je ovladatelné klávesnicí.

## Výkon a bezpečnostní invarianty

- GET routes a presentation resolvery neprovádějí semantic writes; odvozený
  badge, count či filtr nepersistuje nové business rozhodnutí.
- Načítání používá batch/eager loading a request-local indexy. Počet dotazů
  nesmí růst po jednom dotazu na video/title/collection; select-in dávky nejsou N+1.
  Count/presentation resolvery pracují s předem načtenými daty.
- Artwork používá dávkově načtené vazby a kontroly konkrétních lokálních cest,
  nikoli filesystem glob či HTTP lookup pro každý řádek. Language labels jsou
  čisté in-memory lookupy. Žádný presentation GET artwork nestahuje.
- Regresní suite pokrývá semantic snapshoty/SQL DML na GET, bounded query růst,
  parser/index práci, parity scan/migrace/rebuild, manual authority a responsive UI.
  Přesné průběžné počty testů nejsou součástí tohoto stavového dokumentu.
- Scanner je write workflow s kontrolou dostupnosti knihovny před/průběžně/po
  průchodu. Výpadek vede k rollbacku, prázdný výsledek neospravedlňuje vymazání
  katalogu a velký úbytek vyžaduje explicitní potvrzení DB cleanupu.
- Produkční DB je při agentní práci bez povolení read-only. Startup aplikace
  může provést migraci, proto není vhodný pro čistě read-only audit DB.
  Testy a experimenty patří do dočasných databází. NAS se automaticky nemění.
- Potvrzená logická změna ani duplicita nepovoluje fyzický zásah do médií.
  Současný kód rename planner neobsahuje; požadavky na budoucí fyzické operace
  jsou v [ROADMAP](ROADMAP.md), schválená naming pravidla
  v [V6_NAMING_CONTRACT](V6_NAMING_CONTRACT.md).

Kontrolní opory: [performance invariants](../tests/test_performance_invariants.py),
[hierarchy pipeline parity](../tests/test_hierarchy_pipeline_parity.py),
[supplementary identity](../tests/test_supplementary_ordinals.py) a
[metadata completion](../tests/test_metadata_completion.py).

## Současná omezení a nevyřešené oblasti

- AniList search/fetch a cover cache fungují, ale provider `fetch_relations`
  a samostatné `fetch_artwork` jsou neimplementovaná rozhraní. Neexistuje
  automatické propojení celé franchise podle provider relations.
- Provider episode count nemusí odpovídat lokálnímu standardnímu scope.
  Referenční systémový případ je HERO s Episode 0 zahrnutou v provider count,
  zatímco lokálně je oddělená; obdobné Special/shorts případy nelze opravit
  pouhým porovnáním fyzického počtu. Aktuální externí data tím nejsou znovu ověřena.
- `covered-by-parent` není implementováno. `not_required` řeší metadata workflow,
  nepřičítá child k parent count a nepotvrzuje budoucí V6 completeness.
- Supplementary singleton bez čísla, neurčené varianty a nejednoznačný season
  parent mohou být legitimně zachované neznámé údaje; aplikace je nedoplňuje
  heuristikou kvůli zelenému badge nebo přípravě filename.
- Neexistuje fyzický rename/import planner. V6 completeness audit je plánovaný
  jako závěrečná část V6 po úklidu NAS. Read-only identity inventory je dílčí
  předpoklad, ne povolení k přesunu.

Produkční backlog zde není odhadován z historických počtů. Datované podklady:
[classification audit](CONTENT_CLASSIFICATION_AUDIT_2026-09-02.md),
[metadata completion audit](METADATA_COMPLETION_AUDIT_2026-09-05.md),
[původní ordinal audit](SUPPLEMENTARY_ORDINAL_AUDIT_2026-09-05.md) a
[navazující candidate re-audit](SUPPLEMENTARY_CANDIDATE_REAUDIT_2026-09-05.md).
Popisují své tehdejší snapshoty; nejsou seznamem dnes otevřených chyb.
