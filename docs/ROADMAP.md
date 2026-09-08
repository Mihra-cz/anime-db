# AnimeDB – Roadmap

## Jak roadmapu číst

Tento dokument je autoritou pro verzovaný vývojový plán a progress AnimeDB.
[PROJECT_STATUS](PROJECT_STATUS.md) popisuje současný cíl, technologie a business
semantics; [HISTORY](HISTORY.md) zachycuje významné technické milníky minulosti.
[README](../README.md) je vstupní rozcestník a provozní návod.

Verze zde znamenají vývojové etapy, nikoli číslo Python balíčku. Hotovo označuje
dokončený rozsah etapy, ne bezchybnou produkční knihovnu. Plánováno neznamená
implementováno ani povolení k zahájení práce. Orientační směry vyžadují před
zahájením revizi scope; nejsou novým závazným feature backlogem aktuální verze.

Po významné změně scope nebo stavu verze se aktualizuje tato roadmapa.
Běžný implementační commit automaticky nevytváří nový roadmap bod.
Průběžné test counts, session logy a jednotlivé commity sem nepatří.

## Přehled verzí

| Verze | Stav | Hlavní cíl |
| --- | --- | --- |
| V1 | Hotovo | Databáze a bezpečný sken |
| V2 | Hotovo | Strukturovaný katalog |
| V3 | Hotovo | Překlady a ruční validace |
| V4 | Hotovo | Použitelné webové rozhraní |
| V5 | Uzavírání | Stabilní hierarchie, metadata a číslování včetně navazující stabilizace |
| V6 | Plánováno | Řízená reorganizace knihovny na NAS podle ověřených dat |
| V7 | Plánováno | Bezpečný import a deduplikace |
| V8 | Orientační / k revizi | Automatické sledování NASu |
| V9 | Orientační / k revizi | Jellyfin / Shoko integrace |
| V10 | Orientační / k revizi | Vizuální anime knihovna |

## V1 – Databáze a bezpečný sken

Stav: Hotovo.

- Cíl: vytvořit opakovatelný index knihovny s technickou analýzou médií.
- Přínos: SQLite evidence videí a streamů, párování externích titulků,
  aktualizace změněných souborů a ochrany proti ztrátě katalogu při výpadku NAS.

## V2 – Strukturovaný katalog

Stav: Hotovo.

- Cíl: seskupit soubory podle anime a jeho částí místo pouhého výpisu cest.
- Přínos: strukturované přehledy a detail, rozpoznání sezón/technických složek,
  klasifikace obsahu a základní episode numbering. Současnou podobu těchto
  autorit dále zpřesnila V5; její semantics popisuje PROJECT_STATUS.

## V3 – Překlady a ruční validace

Stav: Hotovo.

- Cíl: spolehlivě evidovat CZ/SK překlad a ruční ověření hardsubu.
- Přínos: sdílená normalizace jazyků, přehled interních/externích titulků,
  nezávislé CZ/SK stavy a ruční hardsub rozhodnutí zachovaná při skenu.

## V4 – Použitelné webové rozhraní

Stav: Hotovo.

- Cíl: zpřístupnit katalog, překlady a technické údaje v praktickém webovém UI.
- Přínos: filtry, globální hledání, přirozené řazení, title detail a zachování
  navigačního kontextu v URL. Pozdější UI polish nenahrazuje dokončený základ V4.

## V5 – Stabilní hierarchie, metadata a číslování

**Stav: Uzavírání**

### Cíl V5

Vybudovat stabilní logickou identitu collection/title/video a ručně potvrzovanou
metadata integraci, oddělenou od fyzické struktury NAS. Rozsah zahrnuje číslování
a navazující stabilizaci hierarchy, duplicit, variant, media workflow a UI nad
reálnými případy. Nezahrnuje fyzické přejmenování, přesun nebo import médií.

### Hotovo

- Ruční hierarchy/review, persistentní split a grouping authority, bezpečný
  rebuild/reconciliation a explicitní Season/Part kontext.
- Logical numbering, potvrzené duplicity, ruční varianty, Media Parts,
  fractional Recap a supplementary ordinal včetně derived review.
- AniList search/fetch, persistentní kandidáti, ruční metadata vazby,
  count/range evidence, metadata requirement a completion.
- Media Check, audio/subtitle fakta a ruční rozhodnutí, M:N kompatibilita
  externích titulků se skutečnými fyzickými video reprezentacemi.
- Katalogová a season presentation, responzivní UI, index Všechna anime
  v Hierarchy Review, lidské AniList chyby, artwork thumbnails a české language labely.
- Shared read modely, bounded loading a regresní ochrany manual authority,
  GET bez semantic writes a lifecycle parity.

Současné funkční kontrakty a auditovaný baseline popisuje PROJECT_STATUS.
Historický metadata closure checkpoint není potvrzením uzavření celého
navazujícího V5 rozsahu.

### Zbývá do uzavření V5

Samostatná revize AGENTS.md byla dokončena.

1. Finální V5 closure audit.

Žádná další feature není tímto dokumentačním rozdělením přidána do V5.

### Vstupní gate V6 po uzavření V5

Formální closure V5 **nespouští V6 automaticky**. Před zahájením V6 následuje:

1. Ruční cleanup produkční DB pomocí existujících V5 workflow.
2. Dořešení skutečných review položek bez mass automatic fixes.
3. Fresh read-only hierarchy audit.
4. Fresh read-only metadata audit.
5. Fresh read-only Media Check audit.
6. Fresh read-only supplementary audit.
7. V6 completeness/precondition audit.
8. Teprve potom zahájení V6.

Toto je vstupní gate, nikoli další samostatná verze. Historické produkční
inventury ani zelený dílčí badge nenahrazují nové posouzení připravenosti.

## V6 – Řízená reorganizace knihovny na NAS

Stav: Plánováno. Implementace nebyla zahájena.

### Cíl a hranice

- Sjednotit fyzickou strukturu NAS podle potvrzené hierarchy; každé anime
  má vlastní root a explicitní části/sezóny.
- Navrhovat přejmenování video souborů i externích titulků podle confirmed
  metadat a potvrzených lokálních identit. Candidate metadata nejsou autorita.
- Nejprve vytvořit plán a preview konkrétních operací. Žádná fyzická změna
  nesmí proběhnout bez explicitního potvrzení uživatele.
- Cílové cesty nesmějí kolidovat. Návrh musí rozlišit supplementary ordinal,
  Media Part, variantu a potvrzenou duplicitu a zachovat subtitle compatibility.
  Chybějící či nejednoznačnou autoritu musí vrátit k review, ne vymyslet.
- V6 nesmí opravovat neuklizená V5 data ani znovu nezávisle hádat jejich hierarchii.

### Doložený směr cílových názvů

Bez hierarchy Partu se počítá s `S01E01`, při skutečném Partu s `S01P01E01`
nebo `S01P02E01`. Fyzický segment má oddělený token `MPxx`; příklad
`S01P02E03-MP01` není nová epizoda ani další hierarchy Part.
Part složky na NAS nejsou povinné: několik Parts může ležet v jedné Season složce.
To jsou podklady budoucího návrhu, nikoli hotový úplný filename formatter;
konkrétní názvy a rozlišení všech reprezentací musí projít preview a kontrolou kolizí.

### Úplnost jako podklad, nikoli odhad z počtu souborů

Dřívější směr kontroly úplnosti zůstává relevantní pro připravenost knihovny:
porovnání konkrétního CatalogTitle s potvrzeným externím rozsahem, chybějící
a nerozpoznané epizody, nevysvětlené duplicity, různé reprezentace, titulky bez
videa a konflikty lokální/providerové struktury. Bezpečný základ je logická
identita a odpovídající season/external scope, nikoli slepě absolute číslo.

Chybějící běžný díl a nevysvětlená kolize standardní identity jsou závažné;
nejistá identita vyžaduje review. Potvrzené legitimní varianty ani duplicate
secondary nesmějí být mechanicky považovány za další chybějící/duplicitní epizodu.
Absence supplementary obsahu není chybou úplnosti hlavní standardní série.
„Bez CZ/SK“ znamená chybějící překlad, nikoli chybějící epizodu.

Původní náměty na procenta úplnosti a CZ/SK překladu zůstávají orientačním
reporting směrem k revizi při vymezení V6. Nejsou novou podmínkou closure V5
ani tvrzením, že současné metadata completion již řeší úplnost knihovny.

## V7 – Bezpečný import a deduplikace

Stav: Plánováno. Navazuje na ověřenou hierarchii a řízenou cílovou strukturu,
nikoli na nové hádání identity při kopírování. Import není implementován.

- Cíl: bezpečně zpracovat neuspořádané zdroje z PC, archivy, zálohy, obnovy HDD,
  opakované downloady, různé encody/repacky/remuxy, poškozená videa a samostatné titulky.
- Před jakýmkoliv kopírováním provést preflight **celého importního batche**:
  inventura → hash/technická analýza → porovnání s katalogem → návrh identit,
  duplicit/náhrad a subtitle vazeb → kompletní plán a preview → rozhodnutí uživatele.
- Shoda spolehlivého hashe celého souboru je důkaz exact duplicate s bezpečným
  výchozím „nekopírovat“. Filename je hint; stejná epizoda v jiné kvalitě,
  variantě nebo s jinými streamy nesmí být odmítnuta jen podle čísla.
- Samostatné subtitle assets, jazyk, kompatibilita, aktuální cesta a navržená
  cílová cesta zůstávají oddělené informace. Potvrzená vazba předchází rename návrhu.
- Plán počítá s karanténou/stagingem, kontrolovaným importem, auditem a možností
  návratu. Replacement nejprve ověří incoming soubor i novou kopii; teprve poté
  lze samostatně nabídnout odstranění původní verze. Funkční kopie se nemaže předem.
- Přesuny, přejmenování a mazání vždy vyžadují explicitní potvrzení.

Současný Video model nemá persistentní file hash a scanner hashing neprovádí;
existující duplicate relation proto není sama důkazem bitové shody pro import.

## V8 – Automatické sledování NASu

Stav: Orientační / k revizi. Dosud neimplementovaný dlouhodobý směr.

Cílem je watcher pro nové soubory, čekání na dokončení kopírování a následné
technické zpracování, subtitle evidence, přiřazení, aktualizace DB/metadat/obalu
a upozornění na změny. Ochrana před neúplnými kopiemi je nutnou podmínkou;
automatické sledování nesmí být záminkou k obejití ručních autorit a bezpečnosti.
Před zahájením je nutné znovu vymezit rozsah a návaznost na hotový scanner/import.

## V9 – Jellyfin / Shoko integrace

Stav: Orientační / k revizi. Dosud neimplementovaný dlouhodobý směr.

Zachovaný záměr: odkazy na přehrání, předávání externích ID, případné `.nfo`,
historie sledování a Shoko jako anime identifikační backend; Jellyfin má řešit
přehrávání/transkódování. AnimeDB zůstává katalogem a správcem knihovny.
Konkrétní integrační kontrakty a rozsah vyžadují revizi před implementací.

## V10 – Vizuální anime knihovna

Stav: Orientační / k revizi. Celá etapa není implementována.

Zachovaný záměr: vizuální knihovna s obaly/dlaždicemi, přehledy nově přidaných,
nepřeložených, kompletních či neúplných titulů a filmů/OVA/Specials, bohatším
detailem a případně watch-state z Jellyfinu. Responzivita a malé artwork miniatury
už existují ve V5; nejsou tímto znovu plánovány ani důkazem dokončení V10.
Před zahájením se musí přehodnotit, co po dřívějších UI změnách ještě dává smysl.

## Podklady a hranice rekonstrukce

Vývojová struktura V1–V10 pochází z historického PROJECT_STATUS, dostupného přes
`git show 4769df4:docs/PROJECT_STATUS.md`; samostatné V11 ani další verze doloženy nejsou.
V1–V4 zachovávají dokončené cíle. V5 používá rozšířený hierarchy/metadata/numbering
scope a aktuální rozhodnutí o uzavírání, nikoli starou značku dokončení.

Původní V6 nadpis „Úplnost knihovny“ nepřebíráme jako poslední scope: pozdější
Season/Part/Media Part a supplementary podklady počítají s fyzickými návrhy
a aktuální rozhodnutí při docs review určuje V6 jako řízenou reorganizaci NAS.
Úplnost je zachována jako vstupní/precondition téma, ne nově přečíslovaná verze.
V7 zachovává konkrétně rozpracovaný importní kontrakt; V8–V10 zůstávají původními
stručnými směry bez nového detailního odsouhlasení, proto mají orientační stav.

Technická chronologie zůstává v HISTORY. Tato rekonstrukce žádnou budoucí
funkci neimplementuje ani automaticky nezahajuje další etapu.
