# AnimeDB – V6 naming kontrakt

Tento dokument je aktuálním source of truth pro **již schválená** pravidla
fyzických názvů a struktury knihovny ve V6. Obsahuje pouze odsouhlasená
rozhodnutí; co ještě schválené není, je výslovně vedeno v části
[Otevřené V6 design otázky](#otevřené-v6-design-otázky). Návrhy a doporučení
z přípravných V6 analýz se sem nepřebírají automaticky.

Stav a pořadí V6 určuje [ROADMAP](ROADMAP.md#v6--řízená-reorganizace-knihovny-na-nas),
současnou implementaci a doménovou semantics [PROJECT_STATUS](PROJECT_STATUS.md),
pravidla práce [AGENTS](../AGENTS.md). Kontrakt popisuje cílový stav; dnešní
aplikace žádný rename ani move neprovádí.

## Authority princip

Canonical fyzická cesta a název souboru jsou **deterministickou projekcí
authoritative stavu databáze**. Nejsou novým zdrojem identity a planner jimi
nesmí opravovat hierarchii, odhadovat Season či Part, doplňovat supplementary
ordinal, slučovat varianty, vybírat primary duplicity, párovat titulky podle
podobnosti ani vytvářet metadata authority.

Precedence:

1. human/manual authority;
2. confirmed metadata a bezpečná derived authority;
3. jinak Review — nikoli odhad z názvu.

Zdrojový název souboru nebo adresáře není autoritou tam, kde DB už obsahuje
potvrzené rozhodnutí. Chybějící nebo nejednoznačná autorita znamená Review
nebo odklad, nikdy „chytrý“ filename guess.

Fyzické názvy jsou nezávislé na UI display preferenci. Změna preferovaného
jazyka zobrazení (Romaji/English/Native) fyzickou knihovnu nepřejmenovává.

## Naming ≠ Layout ≠ Hierarchy

Tvrdý invariant: **naming choice je fyzická presentation, hierarchy je doménová
autorita; jsou to oddělené osy.**

- Textový prefix neurčuje strukturální identitu; tu určuje výhradně token
  `Sxx` / `SxxPyy` / `Exx` odvozený z hierarchy a numbering autority.
- Výběr názvu Partu jako textového prefixu nevytváří Part authority.
  Season-level obsah (`Season = 2`, `Part = None`) smí nést prefix převzatý
  z názvu Partu, ale tím se nikdy nestane `Part = 1`.
- Naming Review ani uložení naming choice nemění hierarchy, numbering,
  content type, ordinal, varianty, duplicity ani metadata vazby.

Physical Naming říká, jak se fyzický objekt jmenuje. Physical Layout říká,
jak se authoritative `CatalogTitle` seskupí. Hierarchy určuje root/Season/Part
attachment a metadata určuje provider identity. Layout choice nemá folder
text ani hierarchy/numbering/metadata authority; název případného own folderu
se později resolve-ne z Physical Naming stejného title.

## Collection root

**Default:** Romaji z potvrzené AniList vazby anchoru collection.

Automaticky se jako fyzická autorita **nepoužívají**:

- AniList `userPreferred`;
- `manual_display_title`, `TitleMetadata.display_title` ani jiná starší
  naming/display pole;
- současný lokální root nebo zdrojová složka.

Naming Review nabízí člověku tyto kandidáty:

- Romaji;
- English — lidský kandidát, nikoli automatický fallback;
- AniList synonyms — lidský kandidát, nikoli automatický „short title“;
- current semantic root (současný root bez historických period markerů);
- custom text.

### Current root

Současný root je pouze kandidát. Koncové lokální period markery, například
`(J23)`, `(L12-L20)`, `(P19-L20)` a jiné historické period hints, lze pro
review analyticky oddělit; do canonical rootu se defaultně nepřenášejí.
Původní zdrojová cesta zůstává zachovaná v budoucím manifestu/historii operací.
Shoda current názvu s AniList synonymem je pouze fakt shody; název, který mezi
AniList tituly není, má neznámý původ.

## Naming Review triggery

Root i title prefix jdou do Naming Review, pokud platí alespoň jedno:

| | Trigger |
| --- | --- |
| A | sanitized Romaji má více než 70 Unicode znaků |
| B | current semantic název je výrazně kratší: alespoň o 10 znaků, nebo má nejvýše 70 % délky Romaji |
| C | autorita názvu je nejednoznačná |
| D | planner najde explicitní naming konflikt či kolizi |

Hranice 70 znaků je hranice čitelnosti, nikoli limit filesystému.

Physical Naming / Pojmenování je samostatná aplikační doména s vlastní
centrální Naming Review sekcí. Knihovna shrnuje stav, Hierarchie určuje co
položka je a kam patří, Metadata určuje provider authority, Media Check
skutečný mediální obsah a Pojmenování fyzický název. `/naming-review` má
výchozí frontu K vyřízení, kandidáty s portable preview a explicitní
confirm/reconfirm/reset. Review A/B/C a basis mismatch jsou derived;
inheritance nepřidává další lidské rozhodnutí. Hard limit se kontroluje i
nad známými kompletními filenames. Planner konflikty a absolute-path
warning >240 UTF-16 units přijdou až s plannerem a skutečnou cílovou cestou.

## Portable component policy v1

Sanitizace je čistá derived projekce nesanitizovaného human snapshotu,
stejná pro všechny druhy naming choice. Nemění DB ani doménovou identitu,
nezkracuje význam názvu a neřeší kolize pořadovými suffixy.

- Unicode se normalizuje pouze na NFC. Japonština, emoji, ZWJ/ZWNJ,
  full-width znaky a platná interpunkce zůstávají zachované.
- NUL a kategorie `Cc`, `Cs`, `Zl`, `Zp` jsou invalid input; neodstraňují se
  tiše. `Zs` a U+200B se mění na ASCII space; opakované spaces se slučují
  a vnější spaces odstraňují.
- `?` a `*` → space, `<` → `(`, `>` → `)`, `"` → `'`.
- Run `: / \ |` se mění na jedinou pomlčku: s whitespace u runu nebo
  uvnitř runu na ` - `, uvnitř textu bez whitespace na `-`.
  `Bananya: Fushigi` → `Bananya - Fushigi`, `Re:Zero` → `Re-Zero`,
  `Fate/Grand Order` → `Fate-Grand Order`. Existující platné pomlčky se
  heuristicky neslučují; opakovaný spaced separator má diagnostic.
- Leading dot dostane prefix `_`; trailing run spaces/dots se odstraní.
  Internal dots zůstávají. `.` / `..` a empty/separator-only text jsou
  invalid; placeholder se nevytváří. Samotné neviditelné fillery/blank a
  combining marks nejsou obsah; uvnitř skutečných názvů zůstávají zachované.
- Windows device stem před první dot se kontroluje case-insensitive,
  včetně `CON`, `PRN`, `AUX`, `NUL`, `COM1–9`, `LPT1–9`, superscript
  `¹²³`, `CONIN$` a `CONOUT$`. Escape je jeden leading `_`.
- Directory/file component má hard limit **255 UTF-8 bytes**. Filename
  se kontroluje až včetně identity, MP a extension. Overflow blokuje
  výsledek; nikdy se automaticky netruncuje.
- **240 UTF-16 units celé absolutní Windows klientské cesty** je soft
  portability target. Overflow vyžaduje kratší lidský název; skutečnou
  base path dodá až planner. Není to component limit ani 70-char trigger.
- Extension vstupuje zvlášť, je validovaná a canonical lowercase;
  při chybějící nebo invalid extension se nic nehádá.

Sanitizer je idempotentní a vrací preview, délky, transformace a diagnostics
s policy ID a runtime Unicode verzí. Collision guards ve stejném target
parent namespace jsou exact text, `NFC(casefold(NFD(text)))` a
`NFC(uppercase(text))`; kolize vyžaduje Review. Tyto hodnoty se do naming
authority nepersistují.

## Adresářová struktura

- Sezóny: `Season 01`, `Season 02`, … (dvoumístné zero padding).
- Part složky defaultně nevznikají; více Partů jedné Season může ležet v jedné
  Season složce.

## Prefix standardního videa

Prefix názvu souboru standardního videa vychází z konkrétního `CatalogTitle`
(Season/Part/release), nikoli automaticky z collection rootu.

**Default:** pokud má title potvrzená AniList metadata, použije se celé
confirmed Romaji tohoto title.

Gramatika:

```text
bez Partu:            <TitlePrefix> - S01E01.ext
s potvrzeným Partem:  <TitlePrefix> - S01P02E01.ext
```

Příklady:

- Peter Grill S1: `Peter Grill to Kenja no Jikan - S01E01.mkv`
- Peter Grill S2: `Peter Grill to Kenja no Jikan - Super Extra - S02E01.mkv`

Pro title prefix platí stejné review triggery i kandidáti (Romaji, English,
AniList synonyms, current, custom) jako pro root. Volba rootu a volba title
prefixu jsou **nezávislé**.

Textový Season/Part marker v Romaji (`Part 2`, `2nd Season` apod.) se
automaticky neodstraňuje. Například `Re-Zero … 2nd Season Part 2 - S02P02E01`
je přípustné: text je popisný, `S02P02` je strukturální identita. Případnou
redundanci řeší Naming Review.

## Supplementary obsah

### Physical Layout

Vlastní confirmed metadata identita je **candidate na own folder, nikoli
automatický own folder**. Candidate link není own metadata authority. Mini
Dra (`Kobayashi-san Chi no Maidragon S: Mini Dra`, Season 2) je referenční
strong candidate: více souvisejících logical položek. Oresuki singleton OVA
je optional candidate; člověk může explicitně potvrdit praktické `OVA/`.
Provider episode count je pouze informace o providerovi, nikoli layout
identity nebo completeness blocker.

Bez own metadata nebo při explicitní lidské volbě sdíleného grouping platí:

| Obsah | Shared grouping |
| --- | --- |
| OVA | `OVA/` |
| Special | `Specials/` |
| příběhový Preview / Episode 0 / Prologue | přímo authoritative `Season NN/`, canonical postfix |
| Recap | přímo authoritative `Season NN/`, Recap postfix |
| OP / ED / NCOP / NCED | `Extras/Openings & Endings/` |
| PV / trailer | `Extras/Promo/` |
| CM | `Extras/CM/` |
| skutečný Bonus / drama / voice drama / storyboard / music video | `Extras/Bonus/` |
| release menu video | `Extras/Menu/` |

Own folder **nahrazuje** shared grouping: Season-attached title bude
`<Root>/Season NN/<Own Folder>/...`, root-attached title
`<Root>/<Own Folder>/...`, nikdy `Extras/<Own Folder>/...`. Part folder
nevzniká; Season-attached supplementary s `Part=None` zůstává u celé Season
i při několika main Partech. Naming prefix převzatý z Partu to nemění.
`Interviews/`, `Featurettes/` ani `Other/` nejsou schválené layout kinds.
Interview Special je pro resolver nejednoznačný (`shared_specials` nebo
`extras_bonus`) a vyžaduje člověka; resolver nepřeklasifikuje obsah. Pro #279
je lidské rozhodnutí schválené (viz [Zaznamenaná lidská
rozhodnutí](#zaznamenaná-lidská-rozhodnutí)).

Implementovaný foundation ukládá `PhysicalLayoutChoice` pouze po explicitním
lidském confirm, včetně potvrzení defaultu. Jeden CatalogTitle má nejvýše
jednu choice; delete ownera ji smaže cascade. Absence row znamená bez
uloženého lidského rozhodnutí. Confirm/reconfirm/reset je explicitní service,
commit patří callerovi. Review flags jsou derived a nikdy se neukládají.
Layout Review UI je implementované v záložce Rozložení na
`/naming-review/layout`; Názvy zůstávají na `/naming-review`. Výchozí
fronta obsahuje pouze required human decisions a stale choices. Bezpečné
derived defaults nevyžadují další potvrzení. Radio kandidáti a jedna Save
akce vyžadují explicitní volbu, i při potvrzení doporučeného shared defaultu.
Server před save/reconfirm znovu ověří applicability a stale-form fingerprint.
Direct Season lze nabídnout pouze pro applicable Season context.

Náhled vlastní složky používá aktuální PhysicalNaming resolver a společný
sanitizer; nikdy nekrátí Mini Dra na vlastní layout text. Platný naming rename
nemění layout basis ani form identity. Unresolved/invalid Naming náhled
zablokuje a odkazuje na Názvy, ale layout UI naming authority nepotvrzuje.
Produkční DB používá schema v9 po dokončeném rollout v8→v9. Naming Review
je uzavřená s 61 choices a actionable 0; Physical Layout Review s 32 choices,
actionable 0 a basis mismatch 0. Naming/Layout choices se
automaticky nebackfillují. Read-only Target Planner foundation je implementovaný;
execution není implementovaný.

P1A a P1B jsou committed. P1A odděluje interní title owner identity
(`CatalogTitle.id`, případně planned handle před vytvořením) od locatoru.
Po produkčním rollout P1B (`user_version = 9`) je `relative_root_path` NOT NULL,
NON-UNIQUE a neunikátně indexovaný. Shared Season persistence je podporovaná
schema i runtime: více persisted owner IDs může sdílet Season locator,
který není logical identity. Rollout žádnou současnou source cestu nepřepsal.
Current grouping authority je relační owner-ID/FK persistence;
legacy paths jsou pouze historical evidence
a chybějící owner se nehádá. Nejednoznačný locator nenahrazuje routing evidence.
Budoucí container je projekcí Naming + Hierarchy + Layout, nikoli novou identitou.
P2B pure canonical parser core je implementovaný; authoritative context
resolution a scanner routing P2C zůstávají odloženou dependency.
Produkční scanner se nezměnil. Nový soubor ve shared Season containeru bez další authority
zůstává Review. Target Planner nyní zná owner IDs z DB a na P2C nezávisí.
Execution, fyzická reorganizace a finální Completeness UI zůstávají pending.

Bez choice použije čistý resolver jen jednoznačné shared defaults. Own
metadata strong/optional candidate zůstává unresolved human Layout Review,
nikoli automatický own folder. Authoritative Season Preview/Prologue
(Ansatsu episode 0, Arifureta Prologue, Fate Initium Iter, SAO Reflection)
zůstává direct Season i s own metadata. PV v Bonus kontejneru je Promo.
Recap videa uvnitř main Season title nevytvářejí další layout ownera.

Deterministic version-1 basis zahrnuje owner/collection ID, derived root/Season
attachment, effective content/grouping profile, vlastní confirmed
`(provider, external_id)` nebo explicitní absenci a safe logical count bucket
`empty/singleton/multiple/unknown`. Count používá supplementary inventory:
confirmed duplicate secondary, varianty a complete Media Parts nepřidávají
logical identities; nevyřešená duplicate evidence ponechá count unknown.
Arifureta OVA #289 je 1 logical item / 2 MP files.
Neodlišené nečíslované položky stejného typu ponechají count unknown; variant
nebo MP rows z nich nevytvářejí multi-item work. Jednoznačná shared taxonomy
nevyžaduje vyřešené ordinaly/count: numbering review je samostatná doména.

Move/attachment změna, grouping změna a metadata relink/unlink zachovají
choice, ale vrátí basis mismatch; fresh basis vzniká jen explicitní reconfirm.
Metadata text/synonyms/refresh timestamp, naming text/confirmed_at, release
text (IV marker, název složky), provider count a physical row count nejsou
basis identity; release text pouze blokuje shared default. Split ponechá
choice na existujícím ownerovi a nepřenáší ji na nový title. Rebuild ji chrání,
zahrnuje do fingerprint/parity a neodvozuje z ní membership. Scanner choice
nevytváří ani nereconfirmuje. Compatibility 7→8 přidává prázdnou tabulku,
bez backfillu nebo library reconstruction; stabilní startup na aktuální v9 je no-op.

### S vlastními confirmed metadaty

Má-li supplementary `CatalogTitle` vlastní potvrzenou AniList vazbu, default
prefix je **celé confirmed Romaji tohoto title**. Automaticky se nerozděluje na
series title a podtitul, nemaže se text OVA/Special/Part a nevytváří se short
title. Redundance se řeší Naming Review, ne parserovou heuristikou.

Příklad Oresuki:

- root: `Ore wo Suki nano wa Omae dake ka yo`
- OVA prefix: `Ore wo Suki nano wa Omae dake ka yo - Oretachi no Game Set`

### Bez vlastních metadat

| Situace | Prefix |
| --- | --- |
| A. jednoznačný authoritative Season parent | schválený fyzický název této Season/Part naming unit |
| B. root-level obsah bez Season parentu | schválený fyzický root |
| C. Season-only autorita, ale Season je rozdělena na více Partů | Human Naming Review |

Technický label `CatalogTitle` se jako prefix automaticky nepoužívá.

### Season-only obsah napříč Party

Season-only kontext (`Season = N`, `Part = None`) je legitimní autorita
(např. SAO Reflection jako Preview S4, Slime NCOP/NCED S2). Naming Review
nabídne názvy relevantních Partů — a pokud už člověk pro Part schválil kratší
či alternativní fyzický název, nabídne **tento schválený název**, nikoli znovu
raw Romaji. Příklad: P1 schváleno `Tensura S2`, P2 `Tensura S2 Part 2`;
season-level bonus dostane na výběr `Tensura S2`, `Tensura S2 Part 2` a custom.
Zvolený text Part authority nevytváří (viz [Naming ≠ Layout ≠ Hierarchy](#naming--layout--hierarchy)).

### Identita a tokeny

- Supplementary ordinal se v názvu použije jen tam, kde existuje v DB autoritě.
  Singleton bez ordinalu nedostane vymyšlené `01`; season-only obsah nedostane
  vymyšlený Part.
- **Media Part** má token `MP01`, `MP02`, … za logickou identitou, např.
  `… - S01P02E03-MP01.mkv` nebo `… S01 - OVA 01-MP01.mkv`. Media Part není hierarchy Part, nová episode
  identita ani supplementary ordinal.
- **Recap** používá chronologickou fractional pozici s plnou přesností a
  nevydává se za standardní Episode, např. `S02 - Recap 12.5`,
  `S03 - Recap 24.25`.

Foundation formatter podporuje labels `OVA`, `Special`, `Preview`, `OP`,
`ED`, `NCOP`, `NCED`, `Recap`, např. `<Prefix> - S01 - OVA.ext` nebo
`<Prefix> - S01P02 - OVA 01-MP01.ext`. Padding má minimum width 2;
vyšší čísla se netruncují. Recap přijímá exact Decimal/integer, nepoužívá
float ani exponent notation a odstraňuje jen fractional trailing zeros.
D01 přidává labels `Film`, `Bonus`, `CM`, `Menu` se stejnými explicitními
Season/Part/ordinal/MP osami; např. `Tenki no Ko - Film.mkv` nebo
`Show - S01 - Bonus 02-MP01.mkv`. Žádný implicitní ordinal `01`.
P2B parser tuto novou grammar ani variant suffix dosud nerozpoznává;
rozšíření parseru/routing není prerequisite read-only Target Planneru.

Schválený inverse contract je formatter → jedna nebo více syntakticky platných
canonical interpretations → authoritative context resolution → původní logical
identity. Supplementary formatter je non-injective: `Foo - S01 - OVA.mkv`
může znamenat prefix `Foo` + Season 1 OVA i prefix `Foo - S01` + root OVA.
Naming grammar se proto nemění; parser uchovává oba rozklady a deterministic
pořadí alternatives neznamená precedence. Prefix je presentation evidence,
nikoli title identity. P2B core reprezentuje lexical ambiguity; její řešení
patří P2C. Episode token E je canonical coordinate, nikoli source parser číslo.

## Persistence naming choices

Jednou člověkem potvrzená fyzická naming choice musí být persistentní a
znovupoužitelná: schválený root používají root-level supplementary bez
metadat, schválený Season/Part prefix se nabízí season-level supplementary
obsahu a změna UI display preference fyzický název nemění.

Schválená persistence foundation V6.2 používá `physical_naming_choices` s právě
jedním collection/title ownerem. Ukládá nesanitizovaný human snapshot, druh
volby (`romaji`, `english`, `synonym`, `current`, `custom`, `parent_prefix`),
čas potvrzení a verzovaný kontext. Absence row není potvrzení defaultu; reset
row odstraní. Inheritance a Review status jsou derived. Metadata refresh
snapshot nepřepisuje; změna confirmed identity jej zachová s basis mismatch.
Season-only výběr Part prefixu je kopie textu, nikoli živá dependency ani Part
authority. Implementovaný stav a hranice popisuje PROJECT_STATUS.

## Zaznamenaná lidská rozhodnutí

| Případ | Rozhodnutí |
| --- | --- |
| Oresuki, video #2115 | OVA singleton, ordinal None |
| SAO Reflection, video #2622 | Preview (nikoli Recap), S4, Part None, ordinal None; `00` ve filename není canonical identita |
| Slime, videa #2805/#2806 | NCOP/NCED, Season S2, Part None — season-level bonus bez P1/P2 |
| Peter Grill S2 | `Super Extra` je název Season/release, nikoli supplementary marker |
| Isekai Maou, title #279 (2 interview videa) | content/hierarchy typ zůstává Special; potvrzená Physical Layout choice `extras_bonus`, žádná samostatná `Interviews/` taxonomy |

## Schválený Target Planner contract D01–D06

Planner je derived read model nad DB authority a čerstvým read-only filesystem
snapshotem. Nezapisuje target paths, statuses, collisions, review flags ani
execution state. `CatalogCollection.id`, `CatalogTitle.id`, `Video.id` a
`ExternalSubtitle.id` jsou identity; current/target cesta je pouze locator.
Canonical parser existujícímu Video ownera neurčuje. Shared Party téže Season
leží ve společném `Season NN/`, nikdy v automatické `Part NN/` složce.

### D02 – Film a doplňky

Film bez authoritative Season attachmentu leží přímo v anime rootu, s ním
v `Season NN/`. Povinné `Film/` ani `Movies/` nejsou. Effective Physical Layout
zůstává autoritou grouping; relevantní explicitní `own_folder` má přednost.
Current architecture Film z Layout choice ownerů vylučuje; planner žádnou novou
Layout authority ani Film choice nevytváří. Bonus používá `Extras/Bonus/`, CM
`Extras/CM/`, Menu `Extras/Menu/` relativně k root/Season contextu. Stored kinds
`extras_promo` a `extras_menus` se nemigrují; jejich CM/Menu physical projection
používá finální namespaces. Mini Dra explicitní own folder zachovává.

### Container locator a owner identity

`CatalogTitle.relative_root_path` je physical locator, nikoli membership
selector ani další owner authority. Jediný leaf folder zachovává dosavadní
semantics. Pokud stejný effective layout `extras_promo` deterministicky umístí
CM a Preview/PV jednoho title do řízených `Extras/CM` a `Extras/Promo` child
folders, title locator je jejich explicitní `Extras` container anchor v daném
root/Season contextu. File-level layout stále určuje příslušný leaf.
Nevzniká nový title ani owner; nejde o obecné LCA všech target paths.
Neznámý fan-out znamená Review. P1/P2 mohou nadále sdílet Season locator a
`own_folder` má svou dosavadní precedence.

### D03 – Explicitní varianty

Každá explicitně assigned representation má před extension suffix
` [<sanitized authoritative manual_label>]`, např. `[BD]`, `[TV]`, `[A]` nebo
`[Director's cut]`. Používá stejný component sanitizer a limit celého filename.
NULL nebo nebezpečný label znamená Review/Blocked, žádný odhad z release filename
ani default. Suffix se používá i bez současné collision. Následuje nový full
namespace collision gate (exact, Unicode/casefold, uppercase a file/directory).

### D04 – Duplicate quarantine a budoucí purge

VALID PRIMARY má běžný canonical target. VALID SECONDARY má target
`Duplicates/<mirrored canonical primary parent>/<original secondary filename>`.
Nejde o content classification, title, Bonus ani Extras. Secondary filename se
necanonicalizuje, nepřidává se `[DUP]` a reorganizace nic nemaže.
Explicitní secondary side assets sdílejí lane. U zdrojového archive copy je
bezpečnou derived evidence pouze výhradně secondary directory, validní vazby,
jednoznačný primary target parent a byte-identická primary archive kopie;
nejistota zůstává Review. Primary archive patří do běžného archive flow.

Diagnostic nese secondary/primary Video IDs, current primary locator,
primary canonical target, quarantine target, validity, physical-primary stav,
side assets právě tohoto secondary a jejich accounting. Asset jiného secondary se
mu nepřiřazuje; secondary v library rootu nebo adresář s nepřiřaditelným souborem
není accounted. Side-asset accounting je explicitně COMPLETE / INCOMPLETE / UNKNOWN.
Chybějící carried execution evidence po reloadu znamená UNKNOWN, nikdy COMPLETE.
INCOMPLETE se relocation nesmí změnit na COMPLETE. Quarantine je povolená ve
všech třech stavech; future purge pouze při COMPLETE a splnění ostatních gates.
READY quarantine move neznamená purge readiness.
Žádný snapshot není purge authority.
Budoucí „Smazat duplicity“ musí **bezprostředně před delete** znovu ověřit VALID
relation, existující primary DB row, **fyzicky existující regular primary file**
mimo Duplicates, skutečné umístění secondary pod Duplicates a accounted side assets.
Neurčitý/error stav znamená NESMAZAT/Review. Purge/UI/executor nyní neexistují.

### D05 – Externí titulky M:N

1:1 subtitle používá exact stem kompatibilního video targetu a svou lowercase
extension. Pro bezpečně jednu logical identity s explicitními TV/BD lanes a
jedním BD targetem má jeden M:N asset jeden exact-stem BD sidecar target,
např. `Show - S01E01 [BD].ass`. TV i BD compatibility edges zůstávají.
Nevzniká TV physical copy ani symlink. Jiné nejednoznačné M:N placement je Review;
planner žádný nový language suffix ani compatibility authority nevymýšlí.

### D06 – Subtitle present, video missing

`confirmed_no_match` není junk/quarantine: subtitle existuje a aktuální video
owner chybí. Při známém anime rootu bez bezpečné logical/Season placement zůstává
`<Root>/<original filename>`, například OVA 01/02. Nevzniká owner ani Season guess.
Bezpečné pokračování může použít expected canonical stem stejné potvrzené řady.
Foundation konzervativně požaduje přesný source pattern včetně zero-padding,
alespoň tři souvislé pozice ručně `confirmed_compatible` řady, jednu title/Season
identity bez variant/MP, jediný Season/Part context v collection, žádné Video na
pokračovací ani vyšší pozici a bezprostředně následující pozici. Jinak použije
root/original filename.
`confirmed_no_match` se zachovává a records mají `subtitle_present_video_missing`
pro budoucí Completeness. Completeness UI je samostatná pending část V6.

### Coverage, pomocné soubory a preflight

Source subtitle ZIP/RAR/7Z archives mají flat `Subs/<original filename>`;
archive se přesouvá beze změny a obsah se záměrně neotevírá, takže record je READY
s info diagnostikou neklasifikovaného obsahu, nikoli WARNING. Secondary archive
copy používá D04. Associated auxiliary má
`<Root>/Extras/<actual meaningful subtree>/...`, včetně bundle struktury a
původních filenames. Žádný hardcoded seznam bundle names ani mechanické sloučení
s managed Extras namespace. Skutečný namespace konflikt znamená Review/Blocked.
Historický `seznam-souboru.txt` zůstává KEEP v library rootu a není autoritou.

Root-level `Subs`, `Duplicates` a `#recycle` jsou reserved technical namespaces,
nikoli anime/content/metadata owners. Regular ZIP/RAR/7Z přímo v `Subs` je
owner-less source subtitle archive a v post-state má KEEP; archive contents ani
per-anime owner guessing nejsou potřeba. Nečekaný soubor, nested archive nebo
konfliktní secondary evidence v této lane zůstává Review.
DB-known quarantine Video/Subtitle používá stávající duplicate/compatibility
authority. Side asset může použít explicitní ověřenou relocation provenance;
samotné umístění v `Duplicates` ji nevytváří a nikdy neopravňuje purge.
Already-settled quarantine má KEEP se zachovanou duplicate diagnostic, nikoli
target→target MOVE. Neznámý/unprovenanced soubor v Duplicates zůstává Review.

Synology `#recycle` je SYSTEM_EXCLUDED: bez descentu, coverage, manifest actions,
Completeness blockeru a budoucího execution. Neznámé, nepřístupné a nonregular
assets zůstávají explicitně Review; nic se silent skipem neztrácí.
Excluded directory je pouze snapshot/count context, nevytváří plan record.
Soubor v `#recycle` je pro AnimeDB smazaný / mimo aktivní knihovnu: obsah se nikdy
netraversuje, neinventarizuje ani neobnovuje a AnimeDB k němu nežádá oprávnění.
Stale DB-known Video/subtitle nebo carried side asset s locatorem uvnitř `#recycle`
se neztratí: má stávající missing-file stav `source_missing_or_not_regular` bez
targetu. Není to evidence obsahu koše, recovery authority ani požadavek na zásah
do koše; produkce takové rows nemá.
Technical-root-only quarantine/archive members nesmějí poskytovat anime
collection/title locator authority. Jejich fyzický `Video.root_folder` může
být `Duplicates`, ale container locator projection tuto technical lane ignoruje.
Na runtime NAS rootu se read-only zjistí NAME_MAX/PATH_MAX; component hard limit
je 255 UTF-8 bytes, bez truncation. Soft 240 UTF-16 units se vyhodnotí jen při
skutečném absolutním Windows client rootu. Bez něj je
`NOT_CHECKED / PRE_EXECUTION_REQUIRED`, což neblokuje read-only foundation.

### Pure post-state projection a scanner boundary

Read-only projekce aplikuje pouze approved target locators na in-memory
filesystem snapshot a immutable DB-known scalar evidence, potom znovu spustí
stejný Target Planner. Zachová IDs, hierarchy, numbering, varianty, MP,
duplicate relations a subtitle compatibility. Derived container locators,
ověřená side-asset provenance a původní neúplné side accounting se přenášejí
explicitně; nejde o novou persistence. Stale nebo BLOCKED/REVIEW input je odmítnut.
Side-asset classification/accounting je execution evidence, ne domain authority.
`verify_post_state` musí přijmout explicitní carried evidence z budoucího immutable
manifestu/journalu: secondary Video ID, primary Video ID, identities/paths side
assets vlastněných právě tímto secondary (sousední soubory adresáře, primary sidecars
ani auxiliary se mu nepřiřazují), accounting state, classification provenance a případně archive
identity/hash evidence. Carried primary archive locator ukazuje na jeho expected
post-state cestu; verifier kontroluje existence/regular-file a size parity,
konzistenci approved copy hashů a classification provenance, bez primary rehashu.
Totéž identity/stat/provenance ověření platí pro DB-known subtitle sides.
Current pure projection vystaví stejnou immutable evidence;
nesmí mít tajný marker, který reálný reload nemůže dodat. Immutable manifest ji
nese přes strict JSON serialization. Bez evidence je quarantine accounting UNKNOWN a
unprovenanced secondary archive REVIEW. Primary ZIP v Subs se znovu nehashuje.
Fixed point vyžaduje stejné file classifications a targets, pouze settled KEEP,
bez nových collisions/reviews a bez locator drift. Existing directory namespace
se kontroluje včetně prázdných folders; původní empty source directories se nemažou.
Povinný closure gate zároveň vytvoří scratch DB kopii, aplikuje přesné future
locator-only patches, reloaduje normálním production planner loaderem a plánuje
nad simulated post-execution FS snapshotem s explicitní carried evidence.
S pure projekcí musí souhlasit records, locators, statuses, duplicate diagnostics,
accounting, archive provenance, actions, collisions a domain state. Negative reload
bez evidence nesmí nic falešně označit COMPLETE ani nechat asset potichu zmizet.

### Source filename a physical locator

`Video.filename` je persisted source/parser evidence a při V6 physical
reorganization je immutable. Execution jej nesmí přepsat canonical fyzickým
názvem. `Video.relative_path` je authoritative current physical locator;
současný physical filename je jeho basename. Tolerantní parser, supplementary
ordinal, variant hints a explicitní source filename split patterns dál používají
původní evidence. Fyzický název pro UI, filename similarity a extension budget
používají current locator. Schema se tímto contractem nemění.

Stejná hranice platí pro `UnresolvedExternalSubtitle.filename`: je source/parser
evidence, V6 execution jej nemění. `relative_path` je current physical locator
a jeho basename je current physical filename pro UI/similarity. Parserový subtype
a číselný hint dál čtou source evidence. Scanner create/update lifecycle je P2C
debt a v této změně se nepřepisuje.

Pure post-state zachovává také detached `source_evidence_filename`; scratch
DB reload patchuje pouze physical locators a ponechá `Video.filename` beze změny.
Budoucí execution DB patches mění locator fields podle approved lifecycle
(`Video.relative_path`/`root_folder`, collection/title anchors a subtitle paths),
nikoli source/parser nebo domain authority. Immutable manifest může
obsahovat `source_evidence_filename` i `target_relative_path`; tyto významy se
nesmí směšovat. Manifest locator patches filename nezahrnují; DB patch executor
zatím neexistuje.

P2C **není** prerequisite fyzické reorganizace ani read-only post-state
verification. **Je required před ordinary write-capable scanner rescanem
reorganizovaného canonical tree.** Post-state verifier používá DB-known IDs,
persisted locators, filesystem existence/parity a Target Planner; nikdy nevolá
normal scanner ani neodhaduje ownera z filename. Během budoucí execution musí
být scanner/inventory writer zastavený nebo za explicitní maintenance boundary.
P2C musí vyřešit dnešní scanner create/update lifecycle: physical basename
nesmí u existujícího Video ani unresolved subtitle zničit source/parser evidence. Canonical routing
a práce s technical lanes zároveň musí zachovat DB-known IDs a compatibility.
Manifest/preflight vyžadují external/runtime maintenance evidence. Persisted
maintenance mechanismus, executor, DB write transaction, rollback a snapshot
automation nejsou implementované; journal má pouze data/serialization foundation.

### Future execution failure criticality

Manifest-level pravidlo; future failure execution zatím není implementovaná:

- PRIMARY: Season, Film, OVA, Special, Recap, Preview a Mini Dra či obdobný
  samostatný supplementary/Bonus s vlastním obsahem podle explicitní human/metadata authority.
- LOW: OP, ED, NCOP, NCED, CM, PV, Menu a promotional/technical extras.

Criticality řídí pouze future execution failure severity. Nemění Hierarchy,
Naming, Layout, Metadata requirement/authority ani identity. Bonus ani DramaCD
nejsou automaticky LOW. Explicitní human LOW decision pro současných 11
Overlord/Tenki Video IDs má úzký [execution allowlist](V6_EXECUTION_MANIFEST.md#criticality-a-preservation);
nejde o obecné pravidlo typu nebo filename. Mini Dra zůstává PRIMARY.
Ambiguous criticality vyžaduje review. Subtitle dědí nejvyšší severity platných
M:N compatibility; D06 confirmed_no_match je mandatory preservation PRIMARY.
PRIMARY failure vyžaduje STOP; LOW smí pokračovat pouze s nedotčeným source,
bez partial targetu, s journaled failure a bez dependent PRIMARY action.

### Immutable manifest a read-only dry-run

[Execution contract](V6_EXECUTION_MANIFEST.md) definuje immutable snapshot
jednoho fresh plánu, canonical payload/hash, exact warning acknowledgements,
locator-only patches, carried duplicate evidence, target directories a expected
post-state. Dry-run targets nenahrazuje; fresh planner slouží jen ke stale/parity
comparison. Relevantní změna baseline znamená STALE a STOP.

Readiness vyžaduje actual Windows root, snapshot/DB backup evidence navázané na
baseline, scanner/inventory maintenance a ověřené write/no-replace mount
capabilities. Current read-only audit záměrně vrací NOT_READY, pokud evidence
chybí. Žádný library move/rename/copy/delete/mkdir, production DB DML,
snapshot creation, permission probe ani ordinary scanner se neprovádí.
Skutečný executor a crash recovery jsou samostatný pending task.

## Otevřené V6 design otázky

Následující body nejsou součástí schváleného kontraktu:

- grammar pro `Other` a další nepodporované identity;
- rozšíření inverse parser contractu o D01/D03 a scanner routing P2C;
- numbering migrace pro canonical filename grammar;
- transakce aktualizace cest ve filesystemu a DB;
- finální rollback/recovery protokol.
