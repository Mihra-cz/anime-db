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
| PV / trailer / CM | `Extras/Promo/` |
| skutečný Bonus / drama / voice drama / storyboard / music video | `Extras/Bonus/` |
| release menu video | `Extras/Menus/` |

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
Produkční DB používá schema v8 a obsahuje lidské PhysicalLayoutChoice;
Naming i Physical Layout Review jsou uzavřené. Automatický backfill se
neprovádí. Target planner a execution nejsou implementované.

P1A odděluje interní title owner identity (`CatalogTitle.id`, případně planned
handle před vytvořením) od locatoru. `relative_root_path` zůstává NOT NULL
a UNIQUE do P1B; sdílená persisted Season cesta zatím není povolená. Budoucí
container je projekcí Naming + Hierarchy + Layout, nikoli novou identitou.
Canonical parser a routing nových souborů zůstávají P2 dependency.

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
bez backfillu nebo library reconstruction; stabilní v8 startup je no-op.

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
Film/Bonus/CM/Menu grammar zůstává otevřená; formatter ji odmítá.

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

## Otevřené V6 design otázky

Následující body nejsou součástí schváleného kontraktu:

- syntax tokenu pro video varianty (representation lanes);
- fyzická disposition potvrzených duplicate secondary kopií;
- fyzická strategie M:N externích titulků;
- kompletní canonical grammar/parser, včetně Film/Bonus/CM/Menu; supplementary
  grouping taxonomy a persistence/resolver foundation jsou schválené a
  implementované, Layout Review UI je implementované a produkční review uzavřená;
  canonical parser a target planner zatím chybí;
  component sanitizer a foundation formatter
  jsou již implementované, nikoli target planner;
- numbering migrace pro canonical filename grammar;
- transakce aktualizace cest ve filesystemu a DB;
- execution manifest;
- finální rollback/recovery protokol.
