# V6: lidské řešení archivů v REVIEW

Tato oprava doplňuje `/reorganization`; nemění naming contract, schema SQLite v9,
scanner, WARNING workflow, execution manifest ani executor.

## Proč chybělo řešení

Planner již přijímal explicitní `SideAssetEvidence` a uměl přiřazený archiv
zachovat u potvrzené duplicitní kopie v `Duplicates`. Web ale pouze zobrazoval
reason kód `duplicate_side_asset_association_unresolved`; chyběl formulář,
persistence a společné načtení rozhodnutí pro preview a manifest.

## Použití

Náhled selektivně hashuje jen odpovídající kandidátní archivy stejného názvu
a velikosti v adresářích potvrzených secondary videí a jejich primary videí.
Používá jednu inventuru a současné DB vazby, nečte obsah videí ani nesouvisejících
archivů a nevytváří persistentní hashe. Prokázanou kopii stávající planner vyřeší
automaticky jako READY/QUARANTINE; ruční REVIEW je potřeba jen bez dostatečné
authority nebo důkazu shody.

1. Vytvořit aktuální náhled na `/reorganization` a u archivu otevřít **Vyřešit**.
2. Vybrat konkrétní potvrzené duplicitní video, samostatný archiv v `Subs`,
   nebo **Nechat nerozhodnuté**. Nabídka videí vychází z platných DB vazeb
   v příslušné kolekci; zobrazuje ID a aktuální cesty kopie i primárního videa.
   Název archivu ani podobnost názvů neurčují přiřazení.
3. Uložit rozhodnutí. Fresh worker ověří zdroj, DB vazbu a cílovou cestu běžným
   Target Plannerem. Vrátí nový náhled; platný archiv má READY a konkrétní TARGET.
   Neplatný návrh se neuloží a zobrazí srozumitelnou chybu.
4. U uložené položky zůstává dostupné **Změnit / zrušit rozhodnutí**, také v READY.
   Zrušení obnoví běžné plánování bez lidského přiřazení.

Varianta samostatného archivu používá existující plochou cestu
`Subs/<původní název>`, bez klasifikace obsahu a bez určení kompatibility titulků.
Při kolizi je tato volba nedostupná; archiv se nepřejmenovává ani nepřepisuje.
Ostatní REVIEW mají informaci o příslušném existujícím workflow nebo nutnosti
ručního auditu assetu. WARNING se tímto formulářem neřeší.

## Persistence a změny dat

Rozhodnutí je JSON v adresáři `<SQLite soubor>.archive-reviews/`, jeden soubor
na zdrojovou relativní cestu (jméno JSON je SHA-256 této cesty). Jde o lokální
appdata, nikoli tabulku SQLite nebo soubor v knihovně. Zápis používá existující
atomický writer. Běžný GET a preview pouze čtou; zapisuje explicitní POST
`/reorganization/archive-review`.

Rozhodnutí uchovává velikost a mtime archivu, kolekci a u přiřazení také ID
kopie/primárního videa i jejich aktuální locatory. Při změně těchto faktů se
rozhodnutí zachová, ale nepoužije: archiv zůstane REVIEW s vysvětlením.
Planner rovněž znovu kontroluje kolize a fyzickou dostupnost primárního videa.
Neplatné rozhodnutí se samo neopravuje ani nemaže. Selektivní archive hashing
slouží pouze současnému automatickému pravidlu pro prokázanou kopii;
velikost/mtime uloženého lidského rozhodnutí nejsou kryptografický důkaz obsahu.
Nepřístupná evidence vrátí srozumitelnou chybu a neposkytne platný preview;
nezachází se s ní jako s prázdným seznamem rozhodnutí. GET na adresu formuláře
`/reorganization/archive-review` přesměruje zpět na stránku reorganizace.

`readonly_planner_session` načte sidecary pro webový worker, `target_plan`,
`execution_manifest generate-manifest`, dry-run i hostitelské approval/preflight.
Platné přiřazení se překládá na stávající `SideAssetEvidence`; manifest přenese
stejnou provenance do stávající duplicate execution evidence. Post-state
verification používá tuto manifest evidence, nikoli historický source locator
lokálního formuláře. Změna rozhodnutí vyžaduje nový preview/manifest a běžné
schválení; starý manifest se neupravuje.

## Následné nasazení na Nexus

Datovaný stav k 2026-11-09: tato oprava je na GitHubu v commitu `3d145ee`, na Nexusu
nasazená není a běží `e53e736`. Až po samostatném schválení nasazení:

1. Aktualizovat běžící instalaci výhradně podle
   [RELEASE_UPDATE](../deploy/nexus/docs/RELEASE_UPDATE.md); nevytvářet druhou
   instalaci ani nespouštět `provision`.
2. Zachovat dosavadní lokální DB directory bind. Pro `/data/anime.db` bude stav
   v `/data/anime.db.archive-reviews/`, tedy v `$APPDATA_DIR/db/` na hostiteli.
   Rodič `/data` musí zůstat zapisovatelný aplikačnímu UID/GID. Není třeba
   migrace, nové env proměnné ani další bind. NAS zůstává read-only.
3. Ověřit health a čerstvý preview. Archiv s prokázanou kopií má být READY bez
   ručního zásahu; formulář se použije jen u zbývajících REVIEW. Po případném
   ručním rozhodnutí ověřit READY/TARGET a možnost rozhodnutí změnit nebo zrušit.
4. Při pozdějším generování finálního manifestu musí host/worker číst **stejný
   DB directory včetně `.archive-reviews`** a stejný schválený kód. Kopie jen
   `anime.db` rozhodnutí nepřenese. Samotná SQLite záloha archive-review sidecary
   neobsahuje; při jejich ztrátě lze lidská rozhodnutí v aplikaci zadat znovu.
   Kvůli sidecarům se zálohovací systém nerozšiřuje. Preview a manifest porovnat podle
   disposition/TARGET, jejich hash může nadále lišit hashing archivů a Windows root.

Nasazení ani tyto provozní kroky nebyly v rámci implementace provedeny.
Fyzické spuštění reorganizace/E7 vyžaduje samostatné zadání a schválení.
