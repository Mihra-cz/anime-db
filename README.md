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
- Samostatná stránka **Kontrola médií** (`/media-check`) je kompaktní filtrovatelná pracovní fronta pro audio, titulky a hardsub. Navíc eviduje nepřiřazené fyzické titulky, nabízí pouze kontextově omezené návrhy videí a dovoluje ruční přiřazení, persistentní odmítnutí návrhu i stav „bez odpovídajícího videa“. Návrh nikdy sám nevytváří vazbu. Faktický media profil nemění; nullable ruční rozhodnutí „CZ/SK nyní nejsou dostupné“ pouze vyřadí známý nedostatek z otevřeného backlogu. Reálně nalezené interní/externí CZ/SK nebo potvrzený CZ/SK hardsub mají vždy před tímto markerem přednost. Pro efektivně rozpoznané OP, ED, NCOP a NCED jsou titulky neutrálně **nepožadované** a samotný neurčený jazyk audia nevytváří review problém; nalezené stopy a jejich skutečné jazyky se přesto dál beze změny evidují a zobrazují.
- Relativní cesta je jedinečná. Nezměněné video znovu nevolá `ffprobe`; velikost nebo čas změny vyvolá aktualizaci. Externí titulky se obnovují při každém skenu.
- Selhání jednoho souboru se zaloguje a sken pokračuje. Smazané či přesunuté soubory se odstraní pouze z databázového indexu.
- Překročení timeoutu `ffprobe` označí pouze daný soubor jako chybu; jeho předchozí databázový záznam zůstane zachovaný.
- Dostupnost knihovny se kontroluje před skenem, průběžně a znovu před mazací fází. Výpadek vyvolá rollback celého skenu.
- Logická hierarchie používá `CatalogCollection -> CatalogTitle -> Video`: collection je hlavní anime, title je season, film, OVA nebo doplňková část.
- Season je primární strukturální osa. Part je volitelné členění uvnitř Season a používá samostatné `season_number` a `part_number`; `Part 2` proto nikdy neznamená `Season 2`. Jediný Season title může mít `part_number=NULL`. Pokud ale stejnou Season v jedné collection reprezentuje více `CatalogTitle` typu `season`, každý musí mít unikátní explicitní `part_number`. Hodnota se nikdy automaticky neodvozuje jen z rozsahu epizod. Každá taková část je samostatný `CatalogTitle`, může mít vlastní metadata a zobrazuje se například jako `S1 · Part 2`.
- Hierarchy Review může nabídnout Part 1 / Part 2 pouze jako explicitně potvrzovaný návrh: buď při prvním bezpečném oddělení vybraných videí z jediné Season, nebo pro dva již existující Season titles s úplnými nepřekrývajícími se rozsahy, které od E01 bez mezery navazují. Návrh se před zápisem znovu serverově ověří a nikdy nevzniká pro Part 3, identické či překrývající se rozsahy nebo mezeru v řadě. Potvrzení existujících částí nemění jejich ID, video membership, metadata, duplicity ani canonical numbering.
- **Hierarchy Part** je logická část anime a samostatný `CatalogTitle` / metadata identita. **Media Part** je naproti tomu fyzický segment jednoho logického CatalogTitle a ukládá se autoritativně na konkrétním videu jako nullable `Video.media_part_number`. Dvě videa jednoho filmu mohou být `Část média 1/2` a `Část média 2/2`, aniž se film rozdělí na dva CatalogTitle nebo změní hierarchy.
- Generický `CatalogTitle.part_type=title` je pouze technický fallback během inference, nikoli konečný strukturální typ. Bez bezpečně určeného konkrétního typu zůstává collection v `review_required`; `title` se nenabízí jako nová ruční autoritativní volba.
- Souvislá direct-root řada nejméně dvou standardních epizod od E1 bez mezer a nevyřešených duplicit se interpretuje jako automatic Season 1 (`season`, `1`, `S1`). Automatic inference nikdy nezapisuje manual hierarchy pole ani ověřovací timestamp; autoritativní a `verified` může být pouze kompletní aktivní manual snapshot. Neaktivní ani historicky neúplná manual pole se jako effective hierarchy nepoužijí. Historicky neúplný snapshot zůstává blocking review stavem; nedestruktivní reconciliation zachová jeho dosavadní membership, automatic pole a 4B numbering projection, ale nesmí z něj vytvořit complete authority ani selector.
- Status `automatic` znamená bezpečnou automatickou hierarchii bez aktivního problému. `verified` vyžaduje kompletní ruční hierarchy snapshot a žádný jiný blocking issue; `hierarchy_status` i `hierarchy_note` jsou vždy odvozené společným evaluatorem.
- Délka direct-root řady se počítá pouze ze standardních epizod. E1–E14 nemá length warning, E1–E15 až E1–E24 má pouze dynamické neblokující upozornění a více než 24 epizod aktivuje safety review. Počet epizod nikdy automaticky neurčuje ani nevytváří season boundary; recap, OVA, special, bonus a další supplementary obsah se do limitu nepočítají.
- Legacy značky `Z`/`J`/`L`/`P` + dvouciferný rok (např. `P21` nebo `L20-P23`) jsou pouze historické časové poznámky. Neurčují season, part ani hranice `CatalogTitle` a samy nevyvolávají Hierarchy Review. Závorková varianta, včetně `(L20-P23)` a `( L20-P23 )`, historicky znamenala také „dokoukáno“; watch-state se z ní nyní nemigruje. Případné budoucí použití jako slabého hintu pro metadata candidate scoring je pouze roadmapa.
- `Season 1`, `Season 2`, `OVA`, `Specials`, `NC`, `OP`, `ED`, `Movies`, `Bonus` a podobné child složky pod zjevným anime rootem nevytvářejí samostatné hlavní collections.
- Explicitní kombinace jako `Season 2 Shorts`, `Season 1 Specials`, `S2 OVA` nebo `S2 SPs` zůstávají ve stejné collection, nesou známý season scope a používají existující supplementary typ CatalogTitle.
- Potvrzené hierarchy označení (`Season 1`, `S1`, `Season 2`, …) se mechanicky nepřidává do výchozího metadata search dotazu. Běžný skutečný lokální titul zůstává zachován; u čistě strukturálního názvu části se použije známý název konkrétního titulu z metadat, případně čistý název collection.
- Metadata candidate score zůstává informativní aditivní confidence proti pevnému maximu `1.0`; neznámá evidence je v testovatelném breakdownu odlišena od shody i konfliktu, ale denominator se podle dostupných polí nepřepočítává. Episode evidence používá stejný read-only resolver pro candidate i potvrzený detail: u Season/Part centrální `LogicalEpisodeIdentity`, u bezpečně očíslovaných supplementary částí jejich unikátní logické ordinaly a u úplné sady Media Parts jednu položku. Confirmed duplicate secondary, Video Variants, fractional Recap ani supplementary obsah uvnitř Season standardní počet nezvyšují. Provider count je pouze podpůrná evidence, protože může zahrnovat Episode 0 nebo SP vedené lokálně samostatně. Metadata detail proto prezentuje bezpečně vysvětlený rozdíl fyzických řádků jako neutrální informaci, zatímco samostatný metadata-split evaluator zůstává přísným gate pro návrh a provedení fyzického subset mappingu; neúplné či konfliktní Media Parts, nevyřešené identity a skutečný count mismatch zůstávají warning.
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
- Změna parent `CatalogCollection` je lifecycle boundary pro automatic hierarchy závislou na collection contextu. Při přesunu se automatic strukturální cache `CatalogTitle` znovu sestaví z raw path evidence a vyhodnotí shared finalizerem v source i target collection; slabé singleton `S1` se proto v nové collection nevydává za explicitní Season 1. Complete manual authority i historicky neúplný manual snapshot včetně autoritativního `NULL` zůstávají zachovány. Collection merge nikdy nedopočítává Season ani Part z pořadí title nebo pouze z episode ranges.
- Hierarchy Review nabízí explicitně potvrzovanou akci **Přepočítat automatickou hierarchii** pro historické collection merges. V jednom atomickém write obnoví raw structural input všech automatic titles v jejich aktuální collection a znovu spustí shared inference/evaluation; manual snapshoty, canonical numbering, membership, metadata a duplicate vazby nemění. Akce se nikdy nespouští automaticky při startupu a není datově specifická.
- Collection merge neřeší fyzické duplicity; ty zůstávají ve vlastním duplicate workflow.
- `VideoVariantGroup` je explicitní manual-authority lane v rámci právě jednoho `CatalogTitle`, nikoli jedna epizoda. Stabilní identitou group je její databázové ID; editovatelný `manual_label`, nullable `release_source` (`tv`, `bd`, `web`, `dvd`, `other`) a nullable `content_variant` (`censored`, `uncensored`, `other`) jsou pouze nezávislé popisné osy. `BD` nikdy automaticky neznamená `uncensored` a `TV` nikdy automaticky neznamená `censored`.
- Nullable `Video.video_variant_group_id` znamená „neposouzeno / bez potvrzené variantní identity“, nikoli defaultní nebo standardní variantu. Všechna historická videa po aditivní migraci zůstávají `NULL` a žádná group se nevytváří z filename nebo path. Parserové `A/B`, `Ver.TV` a `UC` zůstávají pouze evidence; scanner ani startup je nepersistují jako manual authority.
- Video smí odkazovat pouze na group stejného `CatalogTitle`. Sdílený write helper cross-title assignment odmítne. Při skutečném přesunu videa do jiného title se neplatná stará group nastaví na `NULL`; neklonuje se a v cíli se žádná náhrada nehádá. Stabilní rescan/startup assignment v témže title naopak zůstává zachován. Smazání videa nemaže group, smazání jejího title ji odstraní FK cascade a prázdné groups se automaticky neuklízejí.
- `LogicalEpisodeIdentity` je jedna centrální derived identita standardní epizody: stabilní `CatalogTitle` + canonical `season_episode_number`. Neobsahuje `VideoVariantGroup`, nevytváří DB `Episode` a nijak nemění absolute/external numbering, range, gaps ani supplementary identitu.
- Duplicate kandidáti standardních epizod se dělí uvnitř logical identity podle potvrzené `VideoVariantGroup`: dvě různé non-`NULL` groups jsou legitimní lanes, dvě videa ve stejné group jsou dál collision. `NULL+NULL` i `NULL+known` zůstávají blocking review, protože `NULL` není defaultní varianta. Parserové hinty samy žádnou kolizi nepotlačí.
- Titulový souhrn odděluje fyzické řádky (`physical_video_count` / kompatibilní `total`), bezpečné logical standard episodes (`logical_episode_count` / opravený `standard_total`), potvrzené `(LogicalEpisodeIdentity, VideoVariantGroup)` instance a confirmed duplicate secondaries. `standard_total` už nepočítá několik nevyřešených fyzických kopií stejného canonical čísla vícekrát; warning přitom zůstává.
- Hlavní katalog prezentuje **Videa** jako fyzické soubory a **Epizody** jako součet `LogicalEpisodeIdentity` v jednotlivých `CatalogTitle`; stejné E01 v různých částech collection se proto neslučuje. Sloupec **Varianty** ukazuje confirmed variant instances jen tam, kde se skutečně používá manual group, jinak zobrazuje `—` namísto falešné nuly.
- Detail `CatalogTitle` seskupuje standardní videa jako logická epizoda → variantní lane → fyzické video → potvrzená duplicitní kopie. Jednoduchá epizoda s jediným `NULL` videem zůstává kompaktní; pokud je reprezentací víc, `NULL` se pravdivě zobrazuje jako **Varianta neurčena**, nikdy jako default. Supplementary obsah si zachovává dosavadní strukturu a pouze může zobrazit group label.
- `ExternalSubtitle` je owner-less fyzický subtitle asset identifikovaný unikátním `relative_path`; jazyk a ruční language override zůstávají vlastností assetu. Neobsahuje `video_id` ani povinné vlastnické Video. `ExternalSubtitleCompatibility` je jediná explicitní M:N vazba asset ↔ konkrétní fyzické `Video`. Absence řádku znamená **neurčeno**, nikoli nekompatibilní; `automatic_match` je bezpečný filename match bez lidského ověření, zatímco `confirmed_compatible` a `confirmed_incompatible` jsou ruční autorita s `verified_at` a volitelnou poznámkou.
- Scanner při dnešním jednoznačném single-video matchi synchronizuje `automatic_match`, ale nikdy jej nešíří na sibling `VideoVariantGroup`, stejnou `LogicalEpisodeIdentity`, A/B lane ani confirmed duplicate copy. Rescan/startup nepřepisuje ani nemaže potvrzený kompatibilní či nekompatibilní vztah. Media Check nabízí pro variant siblings scoped preview → explicitní potvrzení → atomický write; návrat ručního rozhodnutí obnoví automatic row jen tehdy, pokud je pro stejný pár stále platný aktuální jednoznačný filename evidence, jinak obnoví no-row unknown.
- Compatibility presentation v Media Checku předpočítává `LogicalEpisodeIdentity` jednou za request a candidate ovládání sestavuje pouze pro assety na aktuální stránce. Stejný request-scoped index dodává per-Video compatible, incompatible a unknown projekci bez subtitle×video scan. Bezpečný same-title/logical scope, explicitní historické relationships i duplicate filtrování zůstávají zachované a read vrstva sama žádnou compatibility nevytváří.
- Katalogové GET requesty používají immutable request-local index parserových faktů, `LogicalEpisodeIdentity` a jazykové evidence. Hodnoty se nepersistují jako cache a parser ani logical identity se při běžném katalogovém průchodu neopakují podle počtu sekcí stránky. Homepage a katalog mají bounded počet SQL dotazů; Media Check zůstává lineární podle počtu Videos a nikdy nesestavuje subtitle × video matici.
- Stabilní startup a GET `/`, Hierarchy Review, Metadata Check i Media Check jsou sémanticky read-only: nemění authority, hierarchy, numbering, metadata, compatibility ani jejich timestampy. Explicitní write workflow a scanner tím nejsou dotčeny.
- `automatic_match` a `confirmed_compatible` jsou jediné pozitivní external-subtitle vztahy pro dostupnost konkrétního fyzického `Video`. `confirmed_incompatible` dostupnost neposkytuje a no-row zůstává **neurčeno**. Jeden fyzický asset tak může pravdivě poskytovat CZ/SK více Videos, aniž vznikne další `ExternalSubtitle` nebo kopie souboru. Jazyk a manual language override jsou nadále asset-level.
- Media Check, jeho filtry, title detail i CZ/SK/Bez CZ/SK souhrny hlavního katalogu používají stejný effective compatibility resolver. Completion je per fyzická video reprezentace/varianta; BD↔TV, A↔B ani known↔`NULL` se automaticky nesdílejí. Potvrzená duplicate secondary zůstává viditelný fyzický fakt, ale nevytváří novou povinnou completion jednotku. Internal subtitle, hardsub a nullable manual-unavailable workflow se významově nemění; skutečná pozitivní CZ/SK evidence má před starším unavailable markerem faktickou přednost, aniž marker automaticky maže.
- Compatibility UI odděluje badge skutečného stavu (**Neurčeno**, **Automaticky přiřazeno**, **Ručně potvrzeno kompatibilní/nekompatibilní**) od selectoru **Ruční rozhodnutí**. Volba **Bez ručního rozhodnutí** zachovává dosavadní clear semantics: obnoví platnou automatic filename evidence, jinak vrátí no-row unknown. Evidence se zobrazuje česky, např. **Historická automatická vazba**, nikoli jako raw enum.
- Legacy `ExternalSubtitle.video_id` byl po úplném auditu odstraněn. Scanner vytváří nebo zachová jeden fyzický asset a při bezpečném matchi synchronizuje pouze `automatic_match / filename`; unresolved ruční přiřazení vytváří `confirmed_compatible / manual`. Smazání Video odstraní jeho compatibility rows, nikoli sdílený ani orphan physical asset. Asset bez relationship zůstává platnou evidencí, nikoli kandidátem k automatickému garbage collection. Žádný subtitle file se nekopíruje, nepřesouvá, nepřejmenovává ani fyzicky nemaže.
- `duplicate_of_video_id` zůstává jedinou autoritou potvrzené duplicity. Secondary nezvyšuje logical ani confirmed-variant count, ale zůstává ve fyzickém počtu a cleanup backlogu. Historický confirmed vztah mezi dvěma různými non-`NULL` groups se automaticky nepřepisuje, ale dostane explicitní blocking diagnostiku.
- Hierarchy Review spravuje `VideoVariantGroup` jako ruční autoritu: dovoluje group vytvořit, přejmenovat bez změny její identity, upravit taxonomy a poznámku, odstranit pouze prázdnou group a přes jediný title-level formulář **Přiřadit, změnit nebo odebrat variantu u vybraných videí** přiřadit, přeřadit nebo cílem **Neurčeno / odebrat z varianty** vrátit vybraná videa na `NULL`. Assignment je reversibilní a startup, rescan i hierarchy rebuild jej samy nepřepisují.
- Parserové `Ver.TV`, `UC` a A/B jsou v UI pouze označené suggestions. `Ver.TV` smí předvyplnit label `TV` a source `tv`; plain protějšek nedostává odhad `BD`, `UC` ani `uncensored` a samotné `UC` se na `uncensored` nemapuje. Group ani assignment bez explicitního potvrzení nevzniknou.
- Bezpečný opakující se hint/plain pattern může dostat hromadný lane preview. Uživatel vidí canonical epizody i konkrétní filenames, explicitně zvolí existující nebo nové groups a potvrzuje jednu atomickou transakci. Nejednoznačná třetí reprezentace, nekonzistentní hint nebo confirmed duplicate vztah automatic proposal potlačí; ruční bulk selection zůstává fallback.
- A/B `structural_variant` lze pro bezpečnou dvojici explicitně a atomicky potvrdit jako jednu canonical epizodu a dvě distinct variant groups. Tato jediná variantní operace současně zapisuje manual canonical numbering; běžné variant assignmenty číslování nemění.
- Variant assignment nikdy automaticky neruší `duplicate_of_video_id`. Potvrzenou duplicitu nelze rozdělit do dvou různých non-`NULL` groups; same-group collision zůstává duplicate candidate a `NULL+known` i `NULL+NULL` zůstávají review.
- Ruční podezření na duplicitu se ukládá samostatně jako `videos.duplicate_status_manual='suspected'`. Výchozí `NULL` znamená pouze, že uživatel video ručně neoznačil; není to potvrzení, že soubor duplicitou není.
- Automaticky nalezená unresolved duplicita, ruční podezření a potvrzená duplicita přes `duplicate_of_video_id` jsou tři nezávislé stavy. Unresolved kolize blokuje Hierarchy Review. Potvrzená duplicita s existujícím primary je logicky vyřešená a sama review neblokuje; secondary fyzický soubor zůstává viditelně evidovaný pro budoucí explicitně potvrzený cleanup. Pokud primary chybí, `duplicate_primary_missing` je znovu blokující problém a AnimeDB náhradu automaticky nevybírá. Ruční označení `suspected` nevybírá primary, nic nemaže a nemění hierarchii, metadata ani typ obsahu.
- Katalogový filtr **Všechny duplicity** spojuje aktuální členy `unresolved_duplicate_groups` s potvrzenými kopiemi, které mají vlastní `duplicate_of_video_id`. Manual-only `suspected` ani primary video odkazované kopií se do něj samy o sobě nezařazují.
- Explicitní supplementary markery `OVA`, `OAD`, `Special`, `OP`, `ED`, `NCOP`, `NCED`, `PV`/`Preview`, `Recap`, `Bonus`/`Extra`, `CM` a `Menu` určují subtype i bez čísla. Pokud za markerem skutečně je ordinal (`OVA 01`, `OP 02`), zobrazuje se pouze jako supplementary sequence a nikdy nevstupuje do canonical episode polí ani standardní season completeness.
- Ruční číslo standardní epizody zůstává kladné celé číslo. Pouze efektivně klasifikovaný **Recap** dovoluje v detailu videa a Hierarchy Review přesnou ruční pozici s nejvýše jednou desetinnou číslicí, například `14.5`, `24.5` nebo `24.9`; server stejné pravidlo ověřuje nezávisle na HTML `step`. Hodnota se ukládá jako přesný počet desetin, nikoli `float`, řadí se numericky mezi canonical epizody a nikdy nezvyšuje `standard_total`. Změna Recapu na jiný typ je odmítnuta, dokud uživatel tuto samostatnou hodnotu výslovně nesmaže; aplikace ji nezahazuje potichu.
- Tokenově ohraničené názvy `S01E01-Title`, `S1E2 Title` nebo `S02E12 - Title` zachovávají season hint i číslo epizody. Fractional varianty `S01E05.5` a `S01E14.5v2` se rozpoznají před integer `SxxExx` pravidlem, zachovají přesnou desetinnou pozici i season hint a nevstupují bez ručního rozhodnutí do integer canonical polí. Revision suffix `v2` číslo nemění. Explicitní `[SP]` za `SxxExx` má před standardním číslem přednost: video je Special, původní číslo zůstává pouze filename hintem a canonical číslo se bez ručního nebo externího podkladu nevymýšlí.
- Rozpor automaticky rozpoznané season složky a `Sxx` ve filename přepne collection do `review_required`; ručně potvrzená hierarchy zůstává autoritativní.
- Hierarchy Review pro explicitní supplementary video uvnitř Season nabídne **Doporučené oddělení**. Akce **Použít doporučení** pouze v prohlížeči předvyplní stávající **Správu zařazení**; změna se uloží až jejím autoritativním potvrzením. Bezpečně známý supplementary subtype nepotřebuje standardní canonical episode number; jeho volitelné pořadí zůstává samostatným supplementary ordinalem.
- Ručně ověřený supplementary `CatalogTitle` správného typu a season/anime contextu už jednotlivá OVA/Special/OP/ED videa znovu nenabízí ke generic splitu pouze kvůli filename markeru. Nezávislé skutečné hierarchy, numbering a duplicate problémy zůstávají v Hierarchy Review beze změny.
- **Správa zařazení jednotlivých videí** zachovává samostatnou video-level klasifikaci Recap, Preview, Special, OVA, Bonus a Other. Workflow **Oddělit do nové části** mění strukturální typ `CatalogTitle`, ale nevytváří ani nepřepisuje `Video.content_type_manual`; ten mění pouze výslovná akce klasifikace konkrétních videí.
- `media_part_number` se nyní nastavuje a maže pouze ručně na detailu fyzického videa. Scanner jej neodvozuje z `P1`, `CD1`, `Disc1` ani `MP01` a při rescan jej zachovává. Souvislá aktivní sada 1..N se zobrazuje jako `Část média X/N`; confirmed secondary duplicate kopie nezvyšují N. Mezery a duplicitní ordinaly jsou pouze video-level upozornění a nemění hierarchy status.
- Roadmapa V6 počítá bez Partu s `S01E01`, s hierarchy Partem s `S01P01E01` / `S01P02E01` a pro fyzický segment s tokenem `MPxx`. Kombinace `S01P02E03-MP01` jednoznačně odděluje Season 1, hierarchy Part 2, epizodu 3 a první fyzickou část média. Part složky na NASu nejsou cílově povinné a fyzické přejmenování se nyní neprovádí.
- Aktivní grouping a review používají stejný effective numbering stav jako horní souhrn. Například raw `00` bez ručního čísla zůstává nestandardní, ale po autoritativním override E01 se zobrazuje mezi standardními epizodami a původní filename detekce už nevytváří aktivní warning.
- Po vložení explicitního fractional Recapu může Hierarchy Review nabídnout **Navrženou opravu číslování**, pouze když nad současnými `LogicalEpisodeIdentity` existuje právě jedna vysvětlitelná mezera a jediný souvislý standardní suffix s bezpečným konstantním posunem. Potvrzený ruční metadata `episode_count` je silná podmínka; candidate guess se jako autorita nepoužije. Preview ukáže každé `Původní → Nové`, rozsah, offset, počet logických epizod i fyzických variant/kopií a případné ruční overrides. Apply vyžaduje explicitní potvrzení, znovu ověří fingerprint, membership a collisions a proběhne atomicky. Ambiguity, více mezer, blockers nebo konflikty návrh potlačí. Recapy, další supplementary položky, jiné titles/seasons ani NAS se nepřečíslují; varianty jedné logical episode a její confirmed duplicate copies se aktualizují společně bez vytvoření dalších logical episodes.
- Absolutní numbering řadí tituly podle samostatných os Season a Part (`S1P1`, `S1P2`, `S2P1`) a offset skládá jen z předchozích canonical titulů se známým oficiálním počtem epizod. Supplementary tituly se do offsetu nepočítají ani nepřeruší známou canonical řadu; Part bez Season zůstává `season_number=NULL` a Media Part toto pořadí nikdy neovlivňuje.
- Persistentním cílem ručního rozdělení je `CatalogTitle` se skutečným range, pattern nebo explicitním M:N selectorem; samotný obecný `hierarchy_manual_override` manual-split membership nevytváří. Explicitní výběr videí pro target se ukládá odděleně v M:N authority vazbě, nikoli jako výsledný `Video.catalog_title_id`; konflikt proto může zachovat více kandidátních rules a současně nechat výsledný assignment prázdný. UI title odstraňuje explicitní akcí **Odstranit část i z ručního rozdělení** podle stabilního title ID; startup sync současně zahazuje pouze nepoužitý automatický title odvozený z fyzické cesty, pokud všechna videa autoritativně převzal manual split.
- Globální hierarchy rebuild má read-only structured dry-run a apply téhož plánu. Z aktuálních cest znovu vytvoří automatic collections, titles, membership, numbering, named-child provenance a status stejnými shared pravidly jako scanner; persistentní manual split, manual hierarchy, metadata, duplicate stav, supplementary klasifikaci a Media Part zachová. Odstraní pouze prokazatelně prázdné čistě automatic objekty a nejasný nebo metadata-bearing stav ponechá ke kontrole.
- Duplicate identita doplňků zahrnuje subtype i bezpečně známý season/name context. Hierarchy Review umí strukturálně nevyřešené jednotlivé video bez změny cesty přesunout do nové nebo existující OVA/Special/NC části; rozdělení už potvrzené lokální části podle rozsahu externího titulu patří do Metadata Check.
- Prázdný CatalogTitle lze po explicitním potvrzení odstranit spolu s jeho vlastněnými DB metadata záznamy; CatalogCollection se tím nikdy nemaže automaticky.
- Prázdné CatalogCollection lze odstranit jednotlivě nebo hromadně. Server jejich prázdnost ověřuje z databáze znovu a neprázdné položky bezpečně přeskočí.

## Požadavek na metadata a dokončení Metadata Check

Potvrzená metadata a požadavek na samostatná metadata jsou dvě nezávislé osy.
Potvrzení vyžaduje `linked_manual` a primární ruční `ExternalTitleLink`
s `verified_at`; automatický kandidát, skóre ani odmítnutí kandidáta potvrzení
nenahrazují. V detailu Metadata Check lze explicitním POST nastavit
`CatalogTitle.metadata_requirement_manual`: `NULL` = automaticky,
`required` = vyžadována, `not_required` = nejsou vyžadována.
Volba Automaticky ruční override zruší. Ruční rozhodnutí má přednost.

Automaticky metadata nevyžaduje pouze neprázdná část, jejíž **všechna** evidovaná
videa mají přesný typ `op`, `ed`, `ncop`, `nced`, `menu` nebo `cm`.
Non-`NULL` `Video.content_type_manual` má přednost před raw `Video.file_type`;
obecný title-level Bonus/Special kontejner přesný technický subtype nepřekrývá.
Bonus, Other, PV, interview ani making-of samy výjimku nezískávají. Směs Season
epizod a NCOP zůstává required; Mini Dra vedené jako Bonus může mít vlastní
potvrzená metadata.

Část je resolved při potvrzené vazbě nebo effective `not_required`; UI oba
důvody rozlišuje. Potvrzená vazba má při prezentaci dokončení přednost i při
ručním `not_required` a automaticky se nemaže. Výchozí fronta **Metadata chybí**
obsahuje pouze required části bez potvrzení s alespoň jedním videem;
**Všechny části** zachovává možnost inspekce. Hromadné hledání přeskočí resolved
části. Prázdné titles se do aggregate nepočítají.

Sloupec **Metadata** na homepage i `/catalog/all` používá shared aggregate:
collection má **Metadata OK**, jen pokud jsou resolved všechny její části
s evidovaným videem. Řadí se serverově přes `sort=metadata&direction=asc|desc`;
vzestupně jsou nejprve chybějící metadata, sestupně OK, uvnitř skupin podle názvu.
Nevzniká nový katalogový filtr. GET nic nepersistuje.

Requirement nemění hierarchii, klasifikaci, numbering, media fakta ani metadata
vazby. `covered-by-parent` zůstává mimo tuto změnu: označení samostatného
Specialu jako not_required nepřičítá epizodu k parentu a nepotlačuje jeho
episode-count/range advisory ani neřeší budoucí V6 completeness.

## Aktualizace databáze

Compatibility verze 2 přidává nullable `CatalogTitle.metadata_requirement_manual`
bez backfillu; staré rows mají `NULL`, nikoli ruční potvrzení. Upgrade z verze 1
provádí pouze aditivní DDL a posun markeru, bez rekonstrukce knihovny nebo DML.
Starší DB bez compatibility checkpointu nejprve projde plnou dosavadní migrací.
Ruční hodnoty zůstávají zachované a další stabilní startup je bez rekonstrukce.

Při prvním startu této compatibility verze proběhne idempotentní migrace SQLite: mimo jiné doplní nullable `CatalogTitle.part_number_manual`, přesné `Video.recap_episode_number_manual_tenths`, `Video.media_part_number`, `Video.czsk_availability_manual`, ruční language override, evidenci `unresolved_external_subtitles`, association `manual_split_rule_videos` a M:N `external_subtitle_compatibilities`. Dokončenou compatibility verzi eviduje nativní SQLite `user_version`; další stabilní startup pouze ověří schema a marker a znovu neprochází celou knihovnu. Při změně vyžadující rekonstrukci se compatibility verze zvýší a plný rebuild proběhne znovu právě jednou; čistě aditivní upgrade 1→2 uvedený výše jej nevyžaduje. U staré DB nejprve každý platný legacy `ExternalSubtitle.video_id` konzervativně materializuje jako compatibility: automatický odkaz jako `automatic_match / legacy_backfill`, prokazatelně ruční unresolved assignment jako `confirmed_compatible / manual`; existující confirmed row, poznámka ani `verified_at` se nepřepisují. Historické per-video řádky téhož `relative_path` se po zachování všech video vazeb sloučí do jediného physical assetu a následný atomický SQLite table rebuild odstraní legacy `video_id`. Fresh schema jej už vůbec neobsahuje. Subtitle bez platného legacy cíle žádnou vymyšlenou vazbu nedostane. Každé SQLite spojení aplikace zapíná `PRAGMA foreign_keys=ON`; compatibility associations respektují `ON DELETE CASCADE`, zatímco smazání Video nesmaže owner-less asset. Existující `Video.catalog_title_id` se do manual-split association heuristicky nekopíruje. Nový Recap sloupec se nebackfilluje odhadem. Automatické `part_number` se nemění, `media_part_number`, CZ/SK workflow marker i language override u starých záznamů zůstávají zachované a nic se neodhaduje z filename nebo stáří anime. Ruční smazání databáze není pro tuto verzi nutné.

Explicitní compatibility synchronizace je databázově idempotentní: pokud se
žádná persistentní hodnota `CatalogTitle` skutečně nezmění, nevznikne pro něj
SQL `UPDATE` a jeho `updated_at` zůstane beze změny. Legitimní změna inference,
hierarchie nebo jiné persistentní hodnoty nadále timestamp aktualizuje.

## Licence

AnimeDB je vydán pod licencí GNU General Public License v3.0 (`GPL-3.0-only`). Podrobnosti jsou uvedeny v souboru [LICENSE](LICENSE).
