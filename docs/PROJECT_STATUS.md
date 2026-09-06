# AnimeDB – současný stav projektu

Tento dokument popisuje účel AnimeDB, současnou implementaci a hranice jejího použití.
Verzovaný plán a progress patří do [ROADMAP.md](ROADMAP.md), technické milníky
do [HISTORY.md](HISTORY.md), vstupní a provozní rozcestník do [README.md](../README.md)
a pravidla práce do [AGENTS.md](../AGENTS.md). Při rozporu mají přednost aktuální
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

Auditovaný baseline implementace: `4769df4` — Zpřehlednění jazyků v Media Check.
Aktuální fáze: **V5 – Uzavírání**.
Podrobný vývojový plán a zbývající kroky jsou výhradně v [ROADMAP.md](ROADMAP.md).

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
  compatibility verze je 2; upgrade 1→2 je aditivní, bez rekonstrukce.
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

### Fyzická evidence a logická struktura

`CatalogCollection → CatalogTitle → Video` odděluje anime, jeho logickou část
a fyzický soubor. Title může představovat Season, Part, Film, OVA, Special
nebo doplněk; není ekvivalentem fyzické složky ani jedné epizody.
`Video.relative_path` identifikuje soubor. Logické přesuny nemění filename ani NAS.

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
  `Video.catalog_title_id` se na selector automaticky nepovyšuje.
- `Video.file_type` je uložená parserová klasifikace. Obecný effective typ má
  prioritu video manual → supplementary typ title → raw typ. Film je
  title-level klasifikace, nikoli nová video-level ruční hodnota.
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
| Supplementary ordinal | Lokální pořadí v namespace `(CatalogTitle, subtype)`; není automaticky providerové číslo. |
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
své explicitní workflow včetně náhledu hromadné opravy.

Potvrzená duplicate secondary nezvyšuje počet logických epizod ani potvrzených
variant, ale zůstává fyzickým videem. Autoritou je vazba `duplicate_of_video_id`,
ne nullable `duplicate_status_manual` (ruční podezření/posouzení). Platné potvrzené
kopie jsou cleanup informace, nikoli samy blocking hierarchy problém.
Chybějící primary či vazba mezi různými potvrzenými variantami zůstává problémem;
nevzniká automatická náhrada primary ani mazání souborů.

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

Fractional Recap zůstává nestandardním doplňkem, ne běžnou epizodou. Ruční
desetinná pozice je přesně uložena v desetinách, nezvyšuje standardní logical
count ani se nezaokrouhluje do integer epizodní osy.

### Supplementary ordinal

Společný resolver používá OP, ED, NCOP, NCED, OVA, Special, Preview/PV a CM;
Preview/PV tvoří jeden namespace. Lokální identita je `(CatalogTitle, subtype,
ordinal)`: OP01 a ED01 nejsou kolize. Bonus/Menu parser hinty existují, ale
nejsou novými typy tohoto bezpečného ordinal kontraktu; Other jej nemá.

Ruční video klasifikace a explicitní použitelné číslo mají přednost. Jinak je
potřeba bezpečná filename evidence odpovídajícího typu; broad Bonus/Special
kontejner přesný marker nezakrývá. Pouhé `Episode 14` přesunuté pod OVA není
automaticky OVA14. Čísla se nevymýšlejí z pořadí, abecedy, počtu souborů ani
z metadat. Rozpoznané TV/A/B suffixy nevytvářejí variantní autoritu.

Nečíslovaný singleton zůstává unknown, ale sám neotevírá collision-risk review.
Více videí stejného typu musí mít bezpečně rozlišené identity. Kolizi mohou
vysvětlit potvrzené duplicity, různé potvrzené varianty nebo úplné Media Parts;
neplatná vazba ani kombinace neurčené a známé varianty ji tiše nevyřeší.
Přísný supplementary inventory při nejasnosti vrací neznámý logical count.
Absence review u singletonu tedy není důkaz připravenosti pro budoucí rename.

## Metadata

### Vyhledání, kandidáti a potvrzení

AniList je implementovaný provider pro search/fetch. Výsledky se uchovávají
jako kandidáti s evidence breakdownem, možností odmítnutí a ručního potvrzení.
Score je pomocná confidence s pevným maximem, nikoli pravděpodobnost ani
automatická autorita. Neznámá evidence se odlišuje od shody a konfliktu.
Batch search kandidáty hledá, nepotvrzuje je.

Potvrzení metadata completion vyžaduje `linked_manual` a primární ruční
`ExternalTitleLink` s `verified_at`; samotný status, candidate nebo uložený
metadata payload nestačí. Service spravuje link, normalizovaný `TitleMetadata`,
obnovení, odpojení a lock. Lock brání běžnému refreshi/přepsání bez příslušného
explicitního potvrzení. Metadata se nepoužívají k tichému přepsání ruční hierarchy.

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

Media Check nad fakty vede požadavek a nullable ruční „CZ/SK nedostupné“.
Pozitivní CZ/SK evidence má před tímto markerem faktickou přednost, aniž jej
automaticky smaže. Potvrzená duplicate secondary nevytváří další povinnou
completion jednotku; jiné fyzické reprezentace se posuzují samostatně.

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
zůstat krátké. Hodnoty formulářů a URL se kvůli labelům nemění. Ruční jazyk
audio stopy nebo external assetu přebíjí detekovaný, nikoli jeho raw záznam.

## UI a navigace

- Hlavní katalog seskupuje aktivní collections, nabízí search, sort a současné
  filtry. Hierarchy, Metadata a media/translation informace mají různé významy.
- Collection detail skládá hlavní části a supplementary skupiny. Doplněk se
  vnoří jen při jednom přesném matchi effective season čísla; chybějící nebo
  nejednoznačný match zůstává anime-level. Jednoznačná jediná část může otevřít
  přímo title detail, aniž zmizí anime-level sourozenec.
- Hierarchy Review je strukturální pracovní fronta i přímý navigační index.
  Metadata Check a Media Check jsou samostatná workflow, nikoli další hierarchy statusy.
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
  jsou v [ROADMAP](ROADMAP.md).

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
- Neexistuje fyzický rename/import planner ani kompletní V6 completeness gate.
  Dnešní read-only identity inventory je dílčí předpoklad, ne povolení k přesunu.

Produkční backlog zde není odhadován z historických počtů. Datované podklady:
[classification audit](CONTENT_CLASSIFICATION_AUDIT_2026-09-02.md),
[metadata completion audit](METADATA_COMPLETION_AUDIT_2026-09-05.md),
[původní ordinal audit](SUPPLEMENTARY_ORDINAL_AUDIT_2026-09-05.md) a
[navazující candidate re-audit](SUPPLEMENTARY_CANDIDATE_REAUDIT_2026-09-05.md).
Popisují své tehdejší snapshoty; nejsou seznamem dnes otevřených chyb.
