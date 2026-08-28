# AnimeDB

AnimeDB je jednoduchý read-only katalog anime knihovny. Rekurzivně najde MKV, MP4, M4V a AVI, načte technická metadata přes `ffprobe`, spáruje externí titulky a zobrazí souhrn ve webovém rozhraní. Do adresáře knihovny nikdy nezapisuje.

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
- Běžný scan již uspořádané knihovny páruje externí SRT/ASS/SSA/VTT automaticky pouze při právě jednom bezpečném kandidátovi: přesném filename stemu (`ep01.srt`), případně explicitně povolené jazykové příponě (`ep01.cs.srt`). Exact stem má před suffixem přednost; číselné a neznámé suffixy se za jazyk nepovažují. Shodný filename stem je pravidlo současného scanu, nikoli budoucí zdroj pravdy pro import nebo ručně potvrzenou vazbu subtitle souboru na video.
- Češtinu a slovenštinu odhaduje lokální, transparentní heuristikou typických slov a znaků; neurčité texty označí `unknown`.
- Dynamický `VideoLanguageProfile` sjednocuje effective jazyky audio stop, interní a externí titulky a ruční CZ/SK hardsub. Audio informativně rozlišuje `japanese`, `english_only`, `other_known`, `unknown` a `no_audio`; absence JP sama není chyba. Subtitle výsledek je `preferred`, `fallback_internal_en` nebo `missing` a EN fallback používá výhradně interní EN stream.
- Detekované jazyky spravuje scanner. Nullable ruční jazyk konkrétní audio stopy nebo externího subtitle má při čtení prioritu a díky stabilní identitě `(video, stream_index)` / fyzické `relative_path` přežije rescan i nový `ffprobe`. Detail titulu umožňuje override nastavit i odstranit.
- Samostatná stránka **Kontrola médií** (`/media-check`) je kompaktní filtrovatelná pracovní fronta pro audio, titulky a hardsub. Navíc eviduje nepřiřazené fyzické titulky, nabízí pouze kontextově omezené návrhy videí a dovoluje ruční přiřazení, persistentní odmítnutí návrhu i stav „bez odpovídajícího videa“. Návrh nikdy sám nevytváří vazbu. Faktický media profil nemění; nullable ruční rozhodnutí „CZ/SK nyní nejsou dostupné“ pouze vyřadí známý nedostatek z otevřeného backlogu. Reálně nalezené interní/externí CZ/SK nebo potvrzený CZ/SK hardsub mají vždy před tímto markerem přednost.
- Relativní cesta je jedinečná. Nezměněné video znovu nevolá `ffprobe`; velikost nebo čas změny vyvolá aktualizaci. Externí titulky se obnovují při každém skenu.
- Selhání jednoho souboru se zaloguje a sken pokračuje. Smazané či přesunuté soubory se odstraní pouze z databázového indexu.
- Překročení timeoutu `ffprobe` označí pouze daný soubor jako chybu; jeho předchozí databázový záznam zůstane zachovaný.
- Dostupnost knihovny se kontroluje před skenem, průběžně a znovu před mazací fází. Výpadek vyvolá rollback celého skenu.
- Logická hierarchie používá `CatalogCollection -> CatalogTitle -> Video`: collection je hlavní anime, title je season, film, OVA nebo doplňková část.
- Season je primární strukturální osa. Part je volitelné členění uvnitř Season a používá samostatné `season_number` a `part_number`; `Part 2` proto nikdy neznamená `Season 2`. Každý Part je samostatný `CatalogTitle`, může mít vlastní metadata a zobrazuje se například jako `S1 · Part 2`.
- **Hierarchy Part** je logická část anime a samostatný `CatalogTitle` / metadata identita. **Media Part** je naproti tomu fyzický segment jednoho logického CatalogTitle a ukládá se autoritativně na konkrétním videu jako nullable `Video.media_part_number`. Dvě videa jednoho filmu mohou být `Část média 1/2` a `Část média 2/2`, aniž se film rozdělí na dva CatalogTitle nebo změní hierarchy.
- Generický `CatalogTitle.part_type=title` je pouze technický fallback během inference, nikoli konečný strukturální typ. Bez bezpečně určeného konkrétního typu zůstává collection v `review_required`; `title` se nenabízí jako nová ruční autoritativní volba.
- Souvislá direct-root řada nejméně dvou standardních epizod od E1 bez mezer a nevyřešených duplicit se interpretuje jako automatic Season 1 (`season`, `1`, `S1`). Automatic inference nikdy nezapisuje manual hierarchy pole ani ověřovací timestamp; autoritativní a `verified` může být pouze kompletní aktivní manual snapshot. Neaktivní ani historicky neúplná manual pole se jako effective hierarchy nepoužijí. Historicky neúplný snapshot zůstává blocking review stavem; nedestruktivní reconciliation zachová jeho dosavadní membership, automatic pole a 4B numbering projection, ale nesmí z něj vytvořit complete authority ani selector.
- Status `automatic` znamená bezpečnou automatickou hierarchii bez aktivního problému. `verified` vyžaduje kompletní ruční hierarchy snapshot a žádný jiný blocking issue; `hierarchy_status` i `hierarchy_note` jsou vždy odvozené společným evaluatorem.
- Délka direct-root řady se počítá pouze ze standardních epizod. E1–E14 nemá length warning, E1–E15 až E1–E24 má pouze dynamické neblokující upozornění a více než 24 epizod aktivuje safety review. Počet epizod nikdy automaticky neurčuje ani nevytváří season boundary; recap, OVA, special, bonus a další supplementary obsah se do limitu nepočítají.
- Legacy značky `Z`/`J`/`L`/`P` + dvouciferný rok (např. `P21` nebo `L20-P23`) jsou pouze historické časové poznámky. Neurčují season, part ani hranice `CatalogTitle` a samy nevyvolávají Hierarchy Review. Závorková varianta, včetně `(L20-P23)` a `( L20-P23 )`, historicky znamenala také „dokoukáno“; watch-state se z ní nyní nemigruje. Případné budoucí použití jako slabého hintu pro metadata candidate scoring je pouze roadmapa.
- `Season 1`, `Season 2`, `OVA`, `Specials`, `NC`, `OP`, `ED`, `Movies`, `Bonus` a podobné child složky pod zjevným anime rootem nevytvářejí samostatné hlavní collections.
- Explicitní kombinace jako `Season 2 Shorts`, `Season 1 Specials`, `S2 OVA` nebo `S2 SPs` zůstávají ve stejné collection, nesou známý season scope a používají existující supplementary typ CatalogTitle.
- Potvrzené hierarchy označení (`Season 1`, `S1`, `Season 2`, …) se mechanicky nepřidává do výchozího metadata search dotazu. Běžný skutečný lokální titul zůstává zachován; u čistě strukturálního názvu části se použije známý název konkrétního titulu z metadat, případně čistý název collection.
- Metadata Check nabízí databázové rozdělení lokálního `CatalogTitle` až po ručním potvrzení konkrétní primární externí vazby. Uložený `episode_count=N` smí vytvořit návrh pouze při úplné jednoznačné lokální řadě `1..M`, kde `0 < N < M`; nikdy nevybírá prostě prvních N souborů. Návrh ukazuje přesný přesouvaný i zbývající subset a vyžaduje další explicitní potvrzení. Nová část převezme potvrzená metadata, `part_type`, season context a persistentní selector authority, původní neprázdná část zůstane bez vymyšlených metadat. Filename, `relative_path` ani NAS se nemění.
- Ručně zadaný `CatalogTitle.local_title` je autoritativní lokální identita a metadata ani klasifikace jednotlivého videa jej nepřepisují. Pokud uživatel název nové části nechá prázdný, společný resolver nejprve vyžaduje shodný bezpečný strukturální kontext všech vybraných cest (například `NC – konkrétní child title`); nikdy nepoužije první video. Bez takového kontextu vznikne konzervativní typ/season název jako `OVA – S3` nebo `Bonus`. Při zobrazení zůstává ruční display title a metadata v dosavadní prioritě; u kompletně potvrzené supplementary části pak její `local_title` předchází filename prefixu jednotlivého OP/ED/OVA videa. Běžná Season nebo Part může nadále použít bezpečný společný filename prefix.
- Hlavní uživatelské UI skládá nad uloženými `CatalogTitle` read-only season view-model. Supplementary část se v něm sbalí pod sezónu jen při právě jednom přesném matchi `effective_season_number`; bez kontextu, bez odpovídající sezóny nebo při nejednoznačnosti zůstává dostupná jako anime-level **Další část**. Season selector u hlavních částí ukazuje celkový počet videí v takto navázaných doplňcích a nativní tooltip jej rozepisuje podle existujících typových skupin; anime-level extras se do žádné sezóny nepočítají. Jediná hlavní sezóna s pouze navázanými doplňky se stále otevře přímo; Hierarchy Review nadále ukazuje všechny skutečné `CatalogTitle` samostatně.
- `sort_order_manual` je pouze explicitní uživatelský override. Potvrzení hierarchy, create, ruční split ani Metadata Check split už z automatického pořadí nevyrábějí hodnoty `0/1/2/…`; prázdná hodnota znamená automatické strukturální řazení. Běžný detail collection používá jeden deterministický klíč: season context, taxonomy rank typu části, Part ordinal, lokální název a nakonec stabilní ID. Season-scoped části jsou před anime-level částmi. Existující nenulové legacy override zůstávají nedotčené, protože jejich původ současné schema neprokazuje.
- Statistiky homepage počítají anime díla podle aktivních `CatalogCollection`, filmy podle hierarchy typu `film` a fyzická videa rozdělují na běžné epizody, filmy a bonusový/ostatní obsah bez dvojího započtení filmů. Karta Filmů otevírá odpovídající katalogový filtr nad stejnou hierarchy definicí; Anime titulů zůstává neklikací souhrn.
- Název `CatalogCollection` na homepage může respektovat globální volbu Romaji/English/Native přes metadata hlavní části, nikdy však nepřevezme ruční ani metadata název supplementary child části typu NC/Bonus. Bez bezpečného hlavního kandidáta používá collection vlastní lokální identitu; jediný samostatný film zůstává platným zdrojem jazykových variant.
- Hierarchy Review umí bez přesunu souborů vytvořit hlavní collection, přesunout do ní celé CatalogTitle a později rozhodnutí změnit. Výslovný výběr konkrétního videa ukládá samostatnou selector authority; samotný výsledný assignment se za autoritu nepovažuje.
- Každé známé video bez úplného bezpečného řetězce `Video -> CatalogTitle -> CatalogCollection` je globální first-class blocking položka úplně nahoře v Hierarchy Review. Patří sem i video mimo fyzický root, chybějící collection přes title, nekonzistentní redundantní collection vazba a legacy technické přiřazení k pseudo-collection `.`. Společné `/unassigned-videos` workflow umí autoritativně vybrat existující část, vytvořit novou část v existujícím anime nebo vytvořit nové anime i jeho první část; fyzická cesta se nemění. `/root-videos` je na této logické ose nezávislý technický inventář všech videí fyzicky v library rootu: po assignmentu video přestane blokovat Hierarchy Review, ale z physical-root pohledu zmizí až po skutečné změně `relative_path`.
- Každý současný `CatalogTitle` lze přímo v Hierarchy Review autoritativně klasifikovat jako Season, Part, Film, OVA, Special, Preview, Recap, Bonus nebo Other. Pro Part se zvlášť ukládá season scope a pořadí Partu. Legacy `cour` zůstává čitelný kvůli kompatibilitě, ale není nabízen jako nová hlavní uživatelská jednotka. `Film` je title-level typ a nepřidává se do video-level `content_type_manual`.
- `Video.file_type` zůstává uloženým výsledkem filename parseru. Effective klasifikace konkrétního videa používá prioritu explicitní `Video.content_type_manual` → přesný supplementary typ jeho `CatalogTitle` → parserový `file_type`; potvrzený nebo bezpečně odvozený Film se proto zobrazuje jako Film i při parserovém `other`, ale ruční video-level klasifikaci nepřepisuje. Samotné slovo `Movie` ve filename film neurčuje.
- Strukturální `part_type` říká, co část představuje; nullable `season_number` a `season_label` nezávisle určují pouze season context. OVA, Special, Bonus nebo Film proto mohou být navázané například na S3, aniž se jejich videa stanou standardními epizodami S3, a bez bezpečného podkladu mohou zůstat na úrovni anime se `season_number=NULL`.
- Nejasná příbuznost collections se pouze navrhne ke kontrole. Volba „Ponechat samostatně“ je persistentní pro konkrétní stav návrhu.
- Ruční sloučení collections je persistentní strukturální autorita: startup compatibility sync i běžný rescan mohou nejprve odvodit fyzickou strukturu, ale následně obnoví přesně potvrzené přiřazení CatalogTitle do cílové collection. Obecný ruční přesun částí ukládá stejnou autoritu; „Ponechat samostatně“ dál chrání nezměněný split stav před opakovaným návrhem.
- Collection merge neřeší fyzické duplicity; ty zůstávají ve vlastním duplicate workflow.
- Ruční podezření na duplicitu se ukládá samostatně jako `videos.duplicate_status_manual='suspected'`. Výchozí `NULL` znamená pouze, že uživatel video ručně neoznačil; není to potvrzení, že soubor duplicitou není.
- Automaticky nalezená unresolved duplicita, ruční podezření a potvrzená duplicita přes `duplicate_of_video_id` jsou tři nezávislé stavy. Ruční označení nevybírá primary, nic nemaže a nemění hierarchii, metadata ani typ obsahu.
- Katalogový filtr **Všechny duplicity** spojuje aktuální členy `unresolved_duplicate_groups` s potvrzenými kopiemi, které mají vlastní `duplicate_of_video_id`. Manual-only `suspected` ani primary video odkazované kopií se do něj samy o sobě nezařazují.
- Explicitní supplementary markery `OVA`, `OAD`, `Special`, `OP`, `ED`, `NCOP`, `NCED`, `PV`/`Preview`, `Recap`, `Bonus`/`Extra`, `CM` a `Menu` určují subtype i bez čísla. Pokud za markerem skutečně je ordinal (`OVA 01`, `OP 02`), zobrazuje se pouze jako supplementary sequence a nikdy nevstupuje do canonical episode polí ani standardní season completeness.
- Tokenově ohraničené názvy `S01E01-Title`, `S1E2 Title` nebo `S02E12 - Title` zachovávají season hint i číslo epizody. Fractional varianty `S01E05.5` a `S01E14.5v2` se rozpoznají před integer `SxxExx` pravidlem, zachovají přesnou desetinnou pozici i season hint a nevstupují bez ručního rozhodnutí do integer canonical polí. Revision suffix `v2` číslo nemění. Explicitní `[SP]` za `SxxExx` má před standardním číslem přednost: video je Special, původní číslo zůstává pouze filename hintem a canonical číslo se bez ručního nebo externího podkladu nevymýšlí.
- Rozpor automaticky rozpoznané season složky a `Sxx` ve filename přepne collection do `review_required`; ručně potvrzená hierarchy zůstává autoritativní.
- Hierarchy Review pro explicitní supplementary video uvnitř Season nabídne **Doporučené oddělení**. Akce **Použít doporučení** pouze v prohlížeči předvyplní stávající **Správu zařazení**; změna se uloží až jejím autoritativním potvrzením. Bezpečně známý supplementary subtype nepotřebuje standardní canonical episode number; jeho volitelné pořadí zůstává samostatným supplementary ordinalem.
- Ručně ověřený supplementary `CatalogTitle` správného typu a season/anime contextu už jednotlivá OVA/Special/OP/ED videa znovu nenabízí ke generic splitu pouze kvůli filename markeru. Nezávislé skutečné hierarchy, numbering a duplicate problémy zůstávají v Hierarchy Review beze změny.
- **Správa zařazení jednotlivých videí** zachovává samostatnou video-level klasifikaci Recap, Preview, Special, OVA, Bonus a Other. Workflow **Oddělit do nové části** mění strukturální typ `CatalogTitle`, ale nevytváří ani nepřepisuje `Video.content_type_manual`; ten mění pouze výslovná akce klasifikace konkrétních videí.
- `media_part_number` se nyní nastavuje a maže pouze ručně na detailu fyzického videa. Scanner jej neodvozuje z `P1`, `CD1`, `Disc1` ani `MP01` a při rescan jej zachovává. Souvislá aktivní sada 1..N se zobrazuje jako `Část média X/N`; confirmed secondary duplicate kopie nezvyšují N. Mezery a duplicitní ordinaly jsou pouze video-level upozornění a nemění hierarchy status.
- Roadmapa V6 počítá bez Partu s `S01E01`, s hierarchy Partem s `S01P01E01` / `S01P02E01` a pro fyzický segment s tokenem `MPxx`. Kombinace `S01P02E03-MP01` jednoznačně odděluje Season 1, hierarchy Part 2, epizodu 3 a první fyzickou část média. Part složky na NASu nejsou cílově povinné a fyzické přejmenování se nyní neprovádí.
- Aktivní grouping a review používají stejný effective numbering stav jako horní souhrn. Například raw `00` bez ručního čísla zůstává nestandardní, ale po autoritativním override E01 se zobrazuje mezi standardními epizodami a původní filename detekce už nevytváří aktivní warning.
- Absolutní numbering řadí tituly podle samostatných os Season a Part (`S1P1`, `S1P2`, `S2P1`) a offset skládá jen z předchozích canonical titulů se známým oficiálním počtem epizod. Supplementary tituly se do offsetu nepočítají ani nepřeruší známou canonical řadu; Part bez Season zůstává `season_number=NULL` a Media Part toto pořadí nikdy neovlivňuje.
- Persistentním cílem ručního rozdělení je `CatalogTitle` se skutečným range, pattern nebo explicitním M:N selectorem; samotný obecný `hierarchy_manual_override` manual-split membership nevytváří. Explicitní výběr videí pro target se ukládá odděleně v M:N authority vazbě, nikoli jako výsledný `Video.catalog_title_id`; konflikt proto může zachovat více kandidátních rules a současně nechat výsledný assignment prázdný. UI title odstraňuje explicitní akcí **Odstranit část i z ručního rozdělení** podle stabilního title ID; startup sync současně zahazuje pouze nepoužitý automatický title odvozený z fyzické cesty, pokud všechna videa autoritativně převzal manual split.
- Globální hierarchy rebuild má read-only structured dry-run a apply téhož plánu. Z aktuálních cest znovu vytvoří automatic collections, titles, membership, numbering, named-child provenance a status stejnými shared pravidly jako scanner; persistentní manual split, manual hierarchy, metadata, duplicate stav, supplementary klasifikaci a Media Part zachová. Odstraní pouze prokazatelně prázdné čistě automatic objekty a nejasný nebo metadata-bearing stav ponechá ke kontrole.
- Duplicate identita doplňků zahrnuje subtype i bezpečně známý season/name context. Hierarchy Review umí strukturálně nevyřešené jednotlivé video bez změny cesty přesunout do nové nebo existující OVA/Special/NC části; rozdělení už potvrzené lokální části podle rozsahu externího titulu patří do Metadata Check.
- Prázdný CatalogTitle lze po explicitním potvrzení odstranit spolu s jeho vlastněnými DB metadata záznamy; CatalogCollection se tím nikdy nemaže automaticky.
- Prázdné CatalogCollection lze odstranit jednotlivě nebo hromadně. Server jejich prázdnost ověřuje z databáze znovu a neprázdné položky bezpečně přeskočí.

## Aktualizace databáze

Při startu aplikace proběhne idempotentní migrace SQLite: mimo jiné doplní nullable `CatalogTitle.part_number_manual`, `Video.media_part_number`, `Video.czsk_availability_manual`, ruční language override a způsob přiřazení external subtitle rows, vytvoří evidenci `unresolved_external_subtitles` a association `manual_split_rule_videos` pro explicitní manual-split authority. Existující `Video.catalog_title_id` se do nové association heuristicky nekopíruje, protože starý automatic assignment nelze bezpečně odlišit od historického explicitního výběru. Každé SQLite spojení aplikace zapíná `PRAGMA foreign_keys=ON`, takže association respektuje své foreign keys a `ON DELETE CASCADE`. Existující automatické `part_number` se nemění, `media_part_number`, CZ/SK workflow marker i language override u starých záznamů zůstávají `NULL` a nic se neodhaduje z filename nebo stáří anime. Konsistenční oprava lokálních názvů a pořadí nepřidává schema změnu ani legacy backfill; neprokazatelné starší `sort_order_manual` hodnoty ponechává beze změny. Ruční smazání databáze není pro tuto verzi nutné.

Startup hierarchy synchronizace je databázově idempotentní: pokud se žádná
persistentní hodnota `CatalogTitle` skutečně nezmění, nevznikne pro něj SQL
`UPDATE` a jeho `updated_at` zůstane beze změny. Legitimní změna inference,
hierarchie nebo jiné persistentní hodnoty nadále timestamp aktualizuje.

## Licence

AnimeDB je vydán pod licencí GNU General Public License v3.0 (`GPL-3.0-only`). Podrobnosti jsou uvedeny v souboru [LICENSE](LICENSE).
