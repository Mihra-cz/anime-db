# AnimeDB – instrukce pro Codex

Tento soubor obsahuje dlouhodobá pravidla práce pro Codex v repozitáři AnimeDB.
Aktuální stav projektu, rozpracované body, checkpointy, čísla testů a plán verzí patří do
`docs/PROJECT_STATUS.md` nebo jiné projektové dokumentace, ne sem.

## 1. Zdroj pravdy a začátek každého úkolu

- Aktuální obsah repozitáře je autoritativní zdroj pravdy.
- Nevycházej slepě z předchozí relace, starého checkpointu ani z paměti konverzace.
- Před každou změnou nejprve:
  1. zkontroluj `git status`,
  2. ověř aktuální větev a stav vůči upstreamu, pokud je dostupný,
  3. načti relevantní zdrojové soubory a testy,
  4. načti `README.md` a relevantní dokumentaci v `docs/`, zejména `docs/PROJECT_STATUS.md`, pokud existuje,
  5. zjisti, jak je dotčená funkcionalita skutečně implementována.
- Existující necommitnuté změny považuj za práci uživatele. Nepřepisuj je, nemaž je a nezahazuj.
- Pokud se dokumentace a skutečný kód rozcházejí, nejprve rozpor identifikuj. Kód bez ověření automaticky nepřepisuj podle zastaralého popisu.

## 2. Rozsah úkolu a oddělení změn

- Řeš přesně zadaný úkol.
- Nedělej vedlejší opravy, refaktory ani redesign jen proto, že sis při práci všiml dalšího problému.
- Nález mimo rozsah úkolu popiš v závěrečném reportu, ale neopravuj ho bez zadání.
- Nemíchej do jednoho zásahu odlišné druhy práce, pokud to není technicky nutné.
- Zejména drž odděleně:
  - audit a diagnostiku,
  - backend/business logiku,
  - databázové změny,
  - CSS/responzivitu a čistě prezentační úpravy,
  - refaktor komentářů a dokumentace.
- Audit je primárně analytický úkol. Pokud zadání říká pouze provést audit, během auditu automaticky neopravuj nalezené problémy.
- Čistě responzivní/CSS úprava nesmí svévolně měnit business logiku, databázové schéma ani význam dat.
- Funkční změna nesmí být schována uvnitř „úklidu“, formátování nebo dokumentačního refaktoru.

## 3. Ochrana produkčních dat

- Produkční `anime.db` je pro Codex ve výchozím stavu READ-ONLY.
- Bez výslovného pokynu uživatele produkční databázi:
  - neměň,
  - nemaž,
  - nepřepisuj,
  - nemigruj,
  - nevytvářej nad ní testovací data.
- Pro testy, migrace a experimenty používej testovací, dočasnou nebo zkopírovanou databázi.
- Nikdy automaticky neměň fyzický obsah anime knihovny na NAS.
- Bez výslovného pokynu uživatele na NAS:
  - nepřejmenovávej soubory ani adresáře,
  - nepřesouvej soubory ani adresáře,
  - nemaž soubory ani adresáře,
  - nepřepisuj video, audio, titulky ani artwork,
  - nevytvářej reorganizovanou produkční strukturu.
- Pokud úkol navrhuje budoucí reorganizaci NAS nebo přejmenování videí či externích titulků, vytvoř pouze návrh/preview. Fyzickou změnu proveď až po explicitním ručním potvrzení uživatele.
- Logická hierarchie v databázi a fyzická struktura NAS jsou dvě oddělené věci. Změna jedné automaticky neopravňuje ke změně druhé.
- Pokud operace může teoreticky zasáhnout produkční data, před i po práci ověř, že produkční databáze a NAS zůstaly nezměněné. Použij vhodné neinvazivní kontroly, například velikost, `mtime`, hash nebo stav relevantních cest.

## 4. Git a práce s historií

- Automaticky nevytvářej commit.
- Automaticky neprováděj `git push`.
- Commit nebo push proveď pouze po výslovném pokynu uživatele.
- Před změnami vždy zkontroluj pracovní strom.
- Nezahoď existující změny uživatele.
- Bez výslovného souhlasu nepoužívej destruktivní operace, například:
  - `git reset --hard`,
  - `git clean -fd`,
  - nucený push,
  - přepis historie,
  - odstranění větví s neověřenou prací.
- Neprováděj automaticky rebase, merge ani checkout jiné větve, pokud to není součást zadání.
- Při commitu zahrň pouze změny patřící k danému úkolu.
- Před commitem nejprve dokonči relevantní testy a kontroly a shrň stav změn.
- Pokud uživatel požádá pouze o commit, neinterpretuj to automaticky jako povolení k pushi.

## 5. Testování a validace

Po každé změně proveď kontroly odpovídající rozsahu zásahu.

Výchozí validační sada projektu je:

- relevantní automatické testy,
- následně plná testovací sada, pokud je její spuštění rozumné pro daný zásah,
- Python `compileall`,
- kontrola načtení všech Jinja2 šablon, pokud se změnily šablony nebo backend, který je používá,
- `git diff --check`,
- závěrečný `git status`.

Další pravidla:

- Nepoužívej historický počet testů jako podmínku úspěchu. Rozhodující je aktuální testovací sada v repozitáři.
- Nevydávej změnu za dokončenou, pokud relevantní testy selhávají.
- Pokud test selže už na nezměněném baseline, nejprve to ověř a jasně odliš od regrese způsobené aktuálním zásahem.
- Pokud některou kontrolu nelze spustit, uveď konkrétní důvod.
- Pokud zásah může ovlivnit produkční data, přidej kontrolu jejich nezměněnosti.
- Při změně databázové migrace ověř její chování na testovací kopii a pokud má být idempotentní, ověř i opakované spuštění.

## 6. Databáze a migrace

- Před změnou schématu zjisti aktuální schéma a existující migrační mechanismus projektu.
- Preferuj zpětně kompatibilní a idempotentní migrace.
- Migrace nesmí bezdůvodně přepisovat existující hodnoty.
- Zachovávej význam `NULL`. Pokud `NULL` znamená „neposouzeno“, „nenastaveno“ nebo jiný odlišný stav, nenahrazuj jej automaticky konkrétní hodnotou.
- Ruční hodnoty a ruční override nepřepisuj automatickým odhadem.
- Databázový refaktor nesmí měnit význam existujících dat jen proto, aby byl model „čistší“.
- Schéma produkční databáze neměň bez explicitního povolení uživatele.

## 7. Doménové invarianty AnimeDB

Následující pravidla chování považuj za důležitá a před jejich změnou vyžaduj, aby ji zadání skutečně požadovalo.

### Ruční rozhodnutí

- Ruční rozhodnutí uživatele mají přednost před automatickým odhadem.
- Automatická detekce nesmí svévolně přepsat ručně potvrzenou:
  - hierarchii,
  - klasifikaci části,
  - metadata,
  - číslování,
  - stav duplicity.
- Pokud současný model používá ručně potvrzenou prázdnou hodnotu jako autoritativní rozhodnutí, zachovej tento význam.

### Hierarchie

- Neodvozuj automaticky sezónu pouze z toho, že existuje jediný `CatalogTitle`.
- Číslo sezóny nevymýšlej, pokud jej nelze spolehlivě určit.
- Obsah typu bonus, extras, specials, NC/OP/ED, OVA, preview, recap, movies/films nebo CM/PV automaticky nepovyšuj na hlavní kolekci jen kvůli názvu adresáře.
- Pokud se logika seskupování nebo klasifikace mění, ověř chování na existujících testovacích případech hierarchie.

### Duplicity

Rozlišuj minimálně tyto významově odlišné stavy:

- automaticky zjištěná nevyřešená duplicita,
- potvrzená duplicita reprezentovaná vazbou na primární video,
- ruční podezření na duplicitu.

Pravidla:

- Ruční podezření není potvrzená duplicita.
- `NULL` u ručního stavu nemusí znamenat „není duplicita“; může znamenat „neposouzeno“.
- Primární video nevybírej svévolně automaticky, pokud současná logika vyžaduje ruční rozhodnutí.
- Potvrzené duplicity automaticky nemaž z NAS.
- Pokud primární video zmizí, zachovej explicitní stav problému místo tichého přepojení na jiný soubor.

### Parser a číslování

- Při úpravě parseru zachovávej již podporované formáty názvů souborů, pokud zadání výslovně nevyžaduje změnu.
- Nové pravidlo parseru nesmí bez testů rozbít starší formáty.
- Nezaměňuj speciální nebo nestandardní číslování za běžnou epizodu pouze proto, že obsahuje číslo.
- Pro nové parserové případy přidej regresní testy.

### Metadata a zobrazovaný název

- Nehardcoduj jednu jazykovou variantu názvu jako jedinou správnou pro všechny uživatele.
- Zachovej možnost volby preferované varianty názvu, pokud ji aplikace podporuje.
- Ručně zadaný zobrazovaný název má přednost před automaticky odvozeným názvem.
- Při změně fallbacků názvu zachovej deterministické pořadí a přidej testy.

## 8. UI a responzivita

- Zachovávej existující vizuální a navigační konvence aplikace, pokud není výslovně zadán redesign.
- Responzivní úpravy řeš napříč celou aplikací, pokud zadání mluví o kompletní responzivitě. Neomezuj je bezdůvodně pouze na jednu stránku.
- Ověř, že formuláře, tabulky, akční prvky a navigace zůstávají použitelné i při menší šířce.
- Neřeš responzivitu pouze zmenšením fontu.
- Vyhýbej se ovládání dostupnému pouze přes `hover`; důležité akce musí být použitelné i dotykem.
- U prvků s velkým množstvím dat preferuj čitelné skládání, scroll nebo jinou použitelnou adaptaci před překrýváním či useknutím obsahu.
- Při relevantních responzivních změnách ověř minimálně tyto cílové velikosti:
  - 1366×768,
  - 1600×900,
  - 1920×1080,
  - 2560×1440,
  - iPad Air 11" v portrait,
  - iPad Air 11" v landscape.
- U tabletového zobrazení ověř dotykové ovládání a nepředpokládej přítomnost myši.
- Čistě prezentační změna nesmí měnit business logiku ani databázové chování.

## 9. Dokumentace a komentáře

- Funkční změna, která mění skutečné chování aplikace, musí podle potřeby aktualizovat relevantní dokumentaci.
- `README.md`, `docs/PROJECT_STATUS.md` a další projektové dokumenty nesmí tvrdit, že plánovaná funkce již existuje.
- Aktuální checkpointy, commit hashe, počty testů a právě rozpracované body neukládej do `AGENTS.md`; patří do stavové dokumentace.
- Refaktor komentářů a dokumentace kódu prováděj samostatně od funkčních změn, pokud není konkrétní komentář nutné opravit kvůli změně chování.
- Při dokumentačním refaktoru zachovej chování aplikace.
- Komentáře mají vysvětlovat účel, ne samozřejmou syntaxi. Nevytvářej hlučné komentáře ke každému řádku.
- Pokud dokumentace popisuje důležité invarianty nebo bezpečnostní omezení, udržuj je při změnách aktuální.

## 10. Verze a roadmapa

- Nezačínej automaticky další projektovou fázi nebo verzi jen proto, že předchozí úkol skončil.
- Řiď se aktuálním stavem a roadmapou v projektové dokumentaci.
- V6, V7 ani jinou budoucí etapu nezačínej bez explicitního zadání uživatele.
- Budoucí reorganizaci NAS, import nebo přejmenování souborů nejprve implementuj jako bezpečný návrh/preview, pokud dokumentace neurčuje jinak.
- Jakákoli fyzická aplikace navržených přesunů, přejmenování nebo mazání vyžaduje ruční potvrzení uživatele.

## 11. Implementační rozhodování

- U drobných implementačních detailů v jasně vymezeném úkolu postupuj samostatně a nezdržuj práci zbytečnými dotazy.
- Pokud existuje více variant s významně odlišným dopadem na:
  - datový model,
  - kompatibilitu,
  - uživatelská data,
  - fyzické soubory na NAS,
  - veřejné chování aplikace,
  nejprve varianty stručně vyhodnoť a neprováděj nevratnou volbu bez souhlasu.
- Preferuj nejmenší změnu, která řeší zadaný problém a zachovává existující chování mimo rozsah úkolu.
- Nepřidávej nové závislosti bez důvodu. Před přidáním knihovny ověř, zda problém nelze rozumně vyřešit existujícími prostředky projektu.

## 12. Závěrečný report

Po dokončení práce vždy stručně uveď:

1. co bylo změněno,
2. které soubory byly změněny,
3. jaké testy a kontroly byly spuštěny,
4. výsledky těchto kontrol,
5. zda se změnilo databázové schéma,
6. zda byla jakkoli změněna produkční `anime.db`,
7. zda byla jakkoli změněna data na NAS,
8. aktuální stav `git status`,
9. případné známé problémy nebo nálezy mimo rozsah úkolu.

Pokud nebyl výslovně požadován commit nebo push, uveď také, že nebyly provedeny.

## 13. Zásada bezpečného dokončení

Úkol není „hotový“ jen proto, že kód vypadá správně.

Za dokončený jej považuj až tehdy, když:

- změna odpovídá zadání,
- nebyly přimíchány nesouvisející zásahy,
- relevantní testy a kontroly prošly,
- produkční data zůstala nedotčena, pokud jejich změna nebyla výslovně povolena,
- pracovní strom a případné zbývající změny jsou jasně popsány,
- uživatel dostal stručný a pravdivý závěrečný report.
