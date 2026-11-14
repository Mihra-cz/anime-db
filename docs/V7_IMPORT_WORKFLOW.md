# AnimeDB – závazný funkční záměr V7: ručně řízený IMPORT

**Datum rozhodnutí:** 14. 11. 2026\
**Stav:** Odsouhlasený uživatelský koncept; **není implementováno**. Technický návrh a rozsah konkrétních změn teprve vzniknou.\
**Určeno k uložení:** `docs/V7_IMPORT_WORKFLOW.md` v repozitáři `Mihra-cz/anime-db`.\
**Návaznost:** `docs/ROADMAP.md`, `docs/PROJECT_STATUS.md`, V6 naming a E7/E7H kontrakty.

## 1. Smysl a hranice verzí

- **V6:** jednorázové fyzické uspořádání nynější rozházené knihovny podle potvrzené hierarchie, názvů a plánu. Po dokončení se celoknihovní migrační režim nebude běžně opakovat.
- **V6.5 (plánováno, ještě není hotovo):** stránka **Completeness**, optimalizace, konsolidace testů, **postupný výpočet a persistence SHA-256 všech evidovaných videí** a další úkoly odložené do V6.5. Náročné hashování se nesmí přidávat jako jednorázová podmínka dokončení V6. Konkrétní harmonogram, obnovení přerušeného hashování a datový model navrhne V6.5; neoznačovat za implementované.
- **V7:** ručně spouštěný **přírůstkový import** nových souborů ze složky `Anime/IMPORT` do již uspořádané knihovny. Využije hotové komponenty V5/V6 a výstupy V6.5, nebude reimplementovat scanner, parser, hierarchy editor, naming ani přesunovací engine.
- **V8 automatický watcher NASu:** není součástí schváleného cíle importu. Automatické sledování, periodický scan nebo automatický start importu nyní **nepožadujeme**. Pokud roadmapa V8 ponechá, pouze jako nezávaznou historickou možnost vyžadující nové výslovné rozhodnutí.

## 2. Uživatelský průchod V7

1. **Ruční nahrání:** Uživatel z počítače zkopíruje soubory a složky přímo přes SMB do `Anime/IMPORT`. AnimeDB nic nestahuje ani neposkytuje upload. Uživatel potvrdí, že kopírování skončilo.
2. **Tlačítko „Import“:** V aplikaci uživatel spustí jednorázový scan **pouze uvnitř `IMPORT`**, nikoli celé knihovny. Neprobíhá automatické sledování složky na pozadí.
3. **Stávající scanner a parser:** Stejný parsing, mediální inspekce, metadata a pravidla jako u běžné knihovny; rozdílný je vstupní scope a kontext importní dávky. Znovupoužití existujícího scanneru je preferovaný kontrakt; případný adaptér musí být tenký, ne druhý scanner s vlastními heuristikami.
4. **Rozpracovaná dávka:** Nově nalezené soubory se označí konkrétní importní dávkou. Zůstávají fyzicky v `IMPORT` a jejich aktuální cesta není vydávána za finální cestu. Rozpracovaný stav, ruční rozhodnutí a průběh přežijí opuštění stránky i restart aplikace.
5. **Kontrola a opravy:** Soubory projdou dosavadním kolečkem rozpoznání a přiřazení k anime, sezóně, části, epizodě, variantám a titulkům. Uživatel je řeší **stávajícími** editory hierarchie, Naming Review, Media Check a relevantními přehledy; napříč obrazovkami je dostupný filtr **„Nový import“** / konkrétní dávka. Completeness se využije až poté, co bude ve V6.5 skutečně implementované.
6. **Příprava dokončení:** Až jsou vyřešené všechny blokující nejasnosti dávky, nabídne aplikace „**Dokončit import**“. Před provedením ukáže přesný plán **SOURCE → TARGET**, kolize, duplicity a dotčené položky. Finální přesun vyžaduje explicitní potvrzení člověka.
7. **Fyzické dokončení:** Znovupoužití ověřených částí V6 plánování, preflightu, bezpečného `rename` a kontrolované aktualizace SQLite locatorů a historie. Přesun se omezuje na schválené soubory aktuální importní dávky **z `IMPORT` do cílového stromu**. Neprovádí se celoknihovní V6 migrace ani automatické přejmenování již uspořádané knihovny.
8. **Úklid:** Po ověření výsledku se odstraní pouze skutečně **prázdné** a ke schválené importní dávce patřící zdrojové podsložky. Samotný kořen `Anime/IMPORT` zůstává. Neznámé soubory ani neprázdné složky se nemažou.

## 3. Pravidla, která se nesmějí ztratit

- **Člověk > automatika; nejednoznačnost → Review.** Automatické přiřazení je možné pouze při jednoznačné existující autoritě.
- Nepřidávat nový hierarchy editor, druhý parser, druhý naming systém ani nezávislý reorganizační engine. Nejprve ověřit znovupoužití existujících modulů.
- Žádné fyzické přemístění před vyřešením blokujících otázek a výslovným schválením dokončení.
- **Bez přepisu existujícího cíle.** Konflikt nebo neschválená duplicita znamená Review/STOP. Legitimní varianty a různé encody nelze zahazovat pouze podle názvu nebo čísla epizody.
- External Subtitle a Media Check zachovávají dosavadní oddělení technických faktů a ručních rozhodnutí.
- V6.5 hash katalog je užitečný při detekci exact duplicit, **není však důvod znovu hashovat celou knihovnu při každém V7 importu**. Nová dávka může zpracovat vlastní nové soubory proti již uloženým výsledkům; chybějící hash nesmí být maskován jako shoda. Technické detaily určí návrh V6.5/V7.
- Import neprovádí automatické odstraňování souborů z `Duplicates`, nenahrazuje potvrzené PRIMARY a nezařazuje neznámé soubory silou.
- Stav databáze musí rozlišovat **rozpracovaný import** od běžné zařazené knihovny. Při chybě nesmí docházet k tichým ztrátám nebo falešně dokončeným locatorům.
- Rozsah scanu i fyzických změn je omezen na dávku a kořen `IMPORT`; bez automatického full scanu NASu.

## 4. Co už existuje a má být znovu použito

| Funkce | Základ |
| --- | --- |
| Technické vyhledání a rozpoznání souborů | Stávající scanner / parser / mediální inspekce |
| Ruční přiřazení a opravy | Existující hierarchie, numbering, metadata, Media Check |
| Cílová fyzická struktura | Naming Review, Physical Layout, canonical naming kontrakt |
| Vyhodnocení rizik před přesunem | V6 Target Planner / manifest / preview / preflight |
| Fyzické změny | Ověřené části V6 E7, `RENAME_NOREPLACE`, ochrana SQLite |
| Prázdné zdrojové složky | Relevantní mechanismy E7H s ochranou kořene IMPORT |
| Completeness | **Teprve plán V6.5**, ne dnes hotová funkce |
| Video SHA-256 katalog | **Teprve plán V6.5**, s postupným výpočtem |

## 5. Co rozhodne až technický návrh V7

- Minimální perzistence importní dávky a vazeb na existující `Video`/`ExternalSubtitle` identity; žádná paralelní katalogová databáze bez odůvodnění.
- Jak bezpečně omezit stávající scanner na `IMPORT`, zachovat ruční autority a zabránit tomu, aby běžný scan omylem považoval rozpracované importní soubory za finální knihovnu.
- Jak ověřit dokončení kopírování a změnu zdrojů během Review; zastaralý plán se musí znovu potvrdit.
- Konkrétní vyhodnocení kolizí, náhrad, titulků a batch scope při dokončování; spíše využít stávající V6 kontrakty než vyvíjet nový engine.
- Jak minimalizovat hostitelskou orchestrace pro malé ruční importy. **Nepřenášet automaticky všechny provozní kroky jednorázové V6 migrace**; zachovat však ochranu dat a bezpečný STOP.

## 6. Povinná úprava současné roadmapy

V `docs/ROADMAP.md` aktualizovat sekce **V6.5, V7 a V8**:

1. V6.5 zůstává **plánováno**: Completeness + postupné perzistentní SHA-256 všech evidovaných videí, optimalizace a odložené položky. Nehlásit jako HOTOVO.
2. V7 formulovat jako **ručně spuštěný importní batch nad stávajícím scannerem/parserem** se scope `Anime/IMPORT`, přes filtry a existující editory; přidání perzistence dávky neznamená nový scanner. Aktualizovat dřívější větu „Nový importní scanner“, která odporuje tomuto rozhodnutí.
3. V7 má samostatné tlačítko **„Dokončit import“** až po Review; využije bezpečné části V6 a uklidí jen prázdné podsložky. `IMPORT` nikdy nemaže.
4. V8 automatický watcher přestat prezentovat jako předpokládané pokračování tohoto importního workflow. Ponechat nejvýše jako vzdálený nesjednaný návrh.
5. Stávající doménové a bezpečnostní kontrakty V6, včetně potvrzených duplicit, metadata autority a přesného preflightu, zachovat. Tento dokument není souhlasem s implementací ani spuštěním importu.

---

**Jednovětý kontrakt:** *Uživatel nahraje do `IMPORT`, ručně spustí omezený scan, s existujícími nástroji vyřeší hierarchii a pojmenování a po výslovném potvrzení AnimeDB přesune pouze schválené soubory do knihovny, aktualizuje databázi a uklidí prázdné importní podsložky.*
