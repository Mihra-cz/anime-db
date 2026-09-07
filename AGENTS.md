# AGENTS.md – AnimeDB

Trvalá pravidla pro coding agenty pracující v tomto repozitáři. AGENTS je
provozní návod, nikoli druhý popis aplikace, roadmapa ani changelog.

## 1. Zdroj pravdy

- Aktuální repozitář, databázový model a testy jsou primární autorita. Při
  rozporu mají současný kód, testy a schema před historickou dokumentací
  přednost.
- Rozpor nejprve pojmenuj a urči pravděpodobnou autoritu. Pokud může znamenat
  skutečnou chybu, nezakrývej ji automatickou úpravou dokumentace ani
  nepřenášej historickou spekulaci do kódu bez zadání.
- Dokumenty mají oddělené role:
  - [README.md](README.md) je lidský rozcestník a provozní základ;
  - [docs/PROJECT_STATUS.md](docs/PROJECT_STATUS.md) popisuje současnou
    architekturu, business semantics a známá omezení;
  - [docs/ROADMAP.md](docs/ROADMAP.md) je autorita pro V1–VX, aktuální fázi,
    progress a closure/gate podmínky;
  - [docs/HISTORY.md](docs/HISTORY.md) je stručná technická historie
    významných milníků.
- PROJECT_STATUS není changelog ani roadmapa, ROADMAP není commit log a HISTORY
  není current-state autorita.
- Před změnou zkontroluj `git status`, aktuální větev a stav vůči upstreamu;
  načti relevantní kód, testy, README a příslušné části projektové dokumentace.
  Nevycházej slepě ze staré relace nebo checkpointu.
- Případný nested `AGENTS.md` nebo `AGENTS.override.md` má v příslušném podstromu
  přednost před tímto kořenovým souborem.

## 2. Rozsah a pracovní postup

- Řeš přesně zadaný úkol. Tangenciální nálezy reportuj, ale neopravuj bez
  zadání; audit ani diagnostika samy o sobě neopravňují k implementaci.
- Existující necommitnuté změny jsou práce uživatele. Nezahazuj je, nepřepisuj
  je a pracuj kolem nich.
- Nemíchej bez technické nutnosti business logiku, databázové změny, čisté
  presentation/CSS úpravy a dokumentační refaktor. Presentation změna nesmí
  měnit uložená business data.
- Nezačínej V6 ani jinou budoucí etapu během V5 polish/closure úkolu. Rozsah
  verzí určuje ROADMAP a explicitní zadání uživatele.
- Preferuj nejmenší změnu, která používá stávající shared resolver a zachová
  chování mimo scope. Nevytvářej paralelní source of truth ani novou závislost
  bez prokazatelné potřeby.
- Před editací zjisti skutečný write/read lifecycle, autority a relevantní
  regresní testy. Pro hledání v repozitáři preferuj `rg`.

## 3. Produkční DB a NAS

- Produkční databáze je `data/anime.db`; media root určený `ANIME_PATH` je
  produkční knihovna na NAS. Bez explicitního pokynu uživatele je neměň.
- Bez explicitního povolení nad produkční DB nespouštěj migrace, scan,
  testovací data, mass automatic fixes ani jiné semantic writes. Testy a
  experimenty používej nad dočasnou nebo testovací databází.
- Audit produkční DB prováděj pouze read-only. Nezakládej kvůli němu běžný
  aplikační startup, pokud by mohl spustit compatibility/migrační write
  lifecycle.
- Na NAS bez explicitního pokynu nic nepřejmenovávej, nepřesouvej, nemaž,
  nekopíruj ani nepřepisuj; platí to pro videa, audio, externí titulky i artwork.
  Logická hierarchie v DB sama neopravňuje k fyzické změně knihovny.
- Pokud úkol může produkční data zasáhnout, pořiď pro tento konkrétní úkol
  aktuální read-only baseline a před/po porovnej vhodný fingerprint, velikost a
  `mtime`. Nikdy neobnovuj DB na starší fingerprint: uživatel mohl mezitím
  legitimně provést změny přes UI.
- Destruktivní nebo nejednoznačný zásah zastav a vyžádej explicitní potvrzení.

## 4. Git workflow

- Bez výslovného pokynu uživatele nevytvářej commit ani neprováděj push.
  Požadavek na commit automaticky neznamená povolení k pushi.
- Nevytvářej automatické checkpoint commity. Po implementaci ukaž změněné
  soubory, `git diff --stat`, kontroly a závěrečný `git status` a nech commit/push
  uživateli.
- Bez výslovného souhlasu nepoužívej `git reset --hard`, `git clean -fd`,
  force-push, přepis historie ani odstranění větve s neověřenou prací.
- Rebase, merge a checkout jiné větve neprováděj automaticky. Případný commit
  musí obsahovat pouze změny daného úkolu a až po odpovídající validaci.

## 5. Databázové a write workflow

- Před změnou schema ověř současný model a migrační mechanismus. Preferuj
  zpětně kompatibilní, idempotentní migrace a ověř je na testovací kopii,
  včetně opakovaného spuštění, má-li být idempotentní.
- Zachovávej význam `NULL`, explicitně prázdných hodnot a ručních overrides.
  Automatický odhad nesmí bezdůvodně přepisovat existující autoritu.
- Stabilní GET a read-only presentation/review routes nesmějí provádět semantic
  writes ani měnit authority, hierarchy, numbering, metadata, compatibility či
  jejich timestampy. Scanner/startup reconciliation a explicitní POST workflow
  jsou oddělené write lifecycle.
- Derived review warning je read model, nikoli automatická persistence změna.
  Ručně verified/manual stav se mění jen explicitním workflow.

## 6. Doménové invarianty

Detaily a konkrétní resolvery jsou v PROJECT_STATUS a kódu; zde jsou pravidla,
která agent nesmí při lokální změně obejít.

### Ruční autorita a hierarchie

- Manual authority má přednost pouze tam, kde ji příslušný model/resolver
  definuje. Nevytvářej jednu univerzální precedence pro různé domény.
- `NULL`, explicitně prázdná hodnota a aktivní ruční rozhodnutí nemusí znamenat
  totéž. Raw/parser evidence nepřepisuj jen proto, aby odpovídala effective
  presentation; presentation resolver musí být bez business side effectu.
- Pouze kompletní aktivní manual hierarchy snapshot je effective ruční
  hierarchie a základ `verified`. Neúplný historický snapshot se
  nedestruktivně zachovává a blokuje review, ale není effective authority ani
  selector; neaktivní historická pole se rovněž jako autorita nepoužívají.
- Manual-split selector authority (range, pattern nebo explicitní M:N výběr)
  je oddělená od výsledného assignmentu. `Video.catalog_title_id` je výsledek,
  nikoli důkaz selectoru; samotný hierarchy override členství ve splitu
  nevytváří.
- Season ani její číslo nevymýšlej z existence jediného title. Supplementary
  obsah nepovyšuj na hlavní část podle názvu adresáře; používej současnou
  taxonomy a hierarchy evaluátor.

### Duplicity, číslování a identity

- Rozlišuj automaticky zjištěnou nevyřešenou duplicitu, ruční podezření a
  potvrzenou duplicitu přes `duplicate_of_video_id`. Ruční podezření není
  potvrzení a primární video nevybírej svévolně.
- Potvrzená duplicate secondary zůstává fyzickým souborem, ale nezvyšuje logical
  identity count. Chybějící primary nebo neplatná vazba musí zůstat viditelný
  problém; nic se kvůli tomu automaticky nemaže z NAS.
- **Supplementary ordinal, Media Part a variant jsou tři oddělené osy.**
  Nezaměňuj je ani je od sebe automaticky neodvozuj.
- Varianta sama nevytváří novou logical episode identity. Media Parts mohou být
  více fyzických souborů jedné logical identity. Běžné raw/source episode number
  se bez bezpečné evidence nesmí překlopit na supplementary ordinal.
- Episode vyžaduje logical číslo vždy. Ostatní video typy vyžadují typed ordinal
  až při 2+ logical identities stejného namespace přes celou collection;
  nepočítej fyzické rows. Singletonu nemaž existující bezpečné číslo.
- Fractional Recap si zachovává přesnou chronologickou pozici (manual před
  parserem); nikdy mu nepřidávej druhý typed ordinal ani jej nepřečísluj.
- `Video.content_type_manual` přebíjí parserový typ, ale parserový ordinal se
  nesmí přenést do jiného namespace jen změnou typu. Explicitní manual ordinal
  zůstává autoritou; container sám nenahrazuje video-level manual rozhodnutí.
- Parserové rozšíření musí zachovat již podporované formáty a mít regresní test.
  Nestandardní číslo neklasifikuj jako běžnou epizodu jen proto, že obsahuje
  číslice.

### Metadata a názvy

- Metadata candidate není confirmed metadata. Confirmed stav vyžaduje současný
  kontrakt ručně potvrzené primární vazby; candidate scoring je pouze evidence.
- Metadata requirement je samostatná osa. `not_required` není missing metadata
  ani confirmed metadata a změna requirement nesmí mazat existující confirmed
  link.
- Ručně zadaný display title má přednost podle současného resolveru. Nehardcoduj
  jednu jazykovou variantu ani nevytvářej vlastní fallback chain v template.

### Externí titulky

- `ExternalSubtitle` je owner-less fyzický asset; jedinou autoritou vztahu k
  jednotlivým fyzickým Videos je explicitní M:N compatibility. Chybějící row
  znamená neurčeno, nikoli nekompatibilitu.
- Dostupnost poskytuje pouze bezpečný `automatic_match` nebo ručně
  `confirmed_compatible` pro konkrétní Video. Nepřenášej ji automaticky mezi
  BD/TV, A/B, known/NULL variantami ani na duplicate copy; návrh kandidáta není
  potvrzená kompatibilita.

## 7. Výkon, UI a read modely

- Nevytvářej N+1. Query count musí zůstat bounded s růstem knihovny; seznamy,
  katalog, review a Media Check mají používat batch/eager loading a
  request-local indexy.
- Vyhýbej se per-row DB, filesystem a HTTP lookupům. Presentation helper má být
  pokud možno čistý in-memory resolver nad již načtenými daty.
- Při změně listové nebo review stránky audituj existující performance a
  read-only regression testy. Nezapisuj do dokumentace náhodný historický query
  count, není-li skutečným testovaným kontraktem.
- Zachovávej současné UI a navigační konvence. Důležité ovládání nesmí být jen
  na `hover`; responzivitu neřeš pouhým zmenšením fontu a presentation úprava
  nesmí měnit business semantics.
- U relevantní responzivní změny ověř 1366×768, 1600×900, 1920×1080,
  2560×1440 a iPad Air 11" v portrait i landscape, včetně dotykového ovládání.

## 8. Testování a validace

Pro změnu kódu:

1. spusť nejprve cílené testy;
2. přidej relevantní regression, performance a read-only kontroly;
3. před běžným funkčním commitem typicky spusť celý `pytest`, pokud to rozsah a
   dostupný limit rozumně dovolují;
4. spusť `python -m compileall app tests`;
5. ověř načtení všech Jinja templates, pokud se měnila šablona nebo její backend
   contract;
6. spusť `git diff --check` a závěrečný `git status`.

Pro čistě dokumentační změnu standardně stačí `git diff --check`, kontrola
Markdown struktury a relativních odkazů, případně existující docs checker, a
`git status`. Pytest, compileall ani Jinja load bez důvodu nespouštěj.

Nevydávej změnu za hotovou při selhávajícím relevantním testu. Baseline selhání
ověř a odliš od regrese. Neprovedenou kontrolu i důvod transparentně reportuj;
nepředstírej úspěch full suite a neřiď se historickým počtem testů.

## 9. Dokumentace

- Funkční změna podle potřeby aktualizuje dokument, jehož role je uvedena v
  části „Zdroj pravdy“. Neopisuj stejnou informaci do všech dokumentů.
- Pracovní session, diff staty, přechodné checkpointy a přesné průběžné počty
  testů automaticky nepatří do PROJECT_STATUS ani ROADMAP.
- Významná změna scope, stavu verze nebo gate může vyžadovat ROADMAP; běžný
  implementační commit automaticky nevytváří nový roadmap bod. HISTORY vybírá
  jen významné technické milníky.
- Plánovanou funkci nepopisuj jako existující. Komentáře mají vysvětlovat účel,
  ne samozřejmou syntaxi; obecný docs/comment cleanup drž odděleně od funkční
  změny.

## 10. Bezpečnost budoucí V6

- V6 začne až po formálním closure V5, ručním cleanupu produkčních dat a všech
  precondition auditech uvedených v ROADMAP. V6 není současná funkcionalita.
- Fyzická reorganizace vždy začíná pouze plánem/preview. Rename, move nebo delete
  se smí provést až po explicitním potvrzení uživatele.
- Cílové cesty musí být jednoznačné a bez kolizí; videa a jejich externí titulky
  se plánují konzistentně. V6 nesmí opravovat neuklizená V5 data.
- „Není v AnimeDB“ neznamená „lze smazat“. Neznámý soubor nebo asset je důvod k
  auditu, nikoli implicitní delete authority.

## 11. Závěrečný report a bezpečné dokončení

Úkol je hotový teprve po odpovídající validaci a kontrole scope. V závěru vždy
stručně uveď:

1. co a které soubory se změnily;
2. spuštěné testy/kontroly a jejich výsledky, včetně vynechaných kontrol;
3. zda se změnilo schema, produkční `data/anime.db` nebo NAS;
4. `git diff --stat` a aktuální `git status`;
5. známé problémy a nálezy mimo scope;
6. zda byl proveden commit nebo push; bez výslovného zadání nesmí být proveden
   ani jeden.
