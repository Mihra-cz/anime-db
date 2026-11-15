# V6 M11 — manual_one_shot ve skutečném M8 V2 LABu

**M11: PASS.** Dne 2026-11-15 proběhl jediný úplný průchod schváleného
`a11a6b285a9b3b8fc3d3141e291fd094c631a96d` přes skutečný web, koordinátor,
Nginx, Docker workery, CIFS/Sambu a původní E7/E7H až do NORMAL a dostupné
AnimeDB. Tím je lokální integrační gate M10 uzavřený. Není to produkční
reorganizace, closure V6 ani souhlas s nasazením Nexusu.

## Prostředí a zachování původního LABu

Použit byl výhradně existující `/srv/animedb-v6-lab`, projekty
`animedb-v6-lab` a `animedb-v6-lab-smb`, loopback web `127.0.0.1:18080`
a stávající RO/RW CIFS views lokální Samby. Nevznikl další LAB ani framework.
Před přípravou byly tři kontejnery zastavené, koordinátor inactive a žádný LAB
mount aktivní. Původní M8 read-only audit prošel 65/65 kontrolami.

Celý předchozí pracovní běh včetně SQLite, journalů, konfigurací, release,
snapshotů, helperů a logů je zachovaný v
`/srv/animedb-v6-lab/history/m11-before-20261115T181614Z`.
Staré workery a image nebyly odstraněné. Původní automatické konfigurace jsou
archivované; zachované jsou i automatické konfigurace nového a11 releasu.
Starší schválené V2 generace se nemazaly ani nepřepisovaly. Pracovní data byla
znovu připravená ze stávající golden kopie, golden se negenerovala.

Závěrečné nezávislé root porovnání potvrdilo shodu SHA, velikostí, mtime a
identit všech 33 golden položek, 182 starších historických položek, všech
archivovaných původních DB/evidence položek a 33 původních snapshot položek.
Změna samotného nadřazeného history adresáře odpovídá přidání nového archivu.
Původních 14 nesledovaných položek pracovního checkoutu zůstalo beze změny.

## Přesný release, locale a approved_root

Image vznikl z čistého lokálního klonu přesného schváleného commitu, bez
nesledovaných či ignorovaných build souborů. SHA všech **159 aplikačních
souborů v image** odpovídá commitu; uživatelské untracked moduly nejsou uvnitř.
Build hash se počítal při `LC_ALL=C`; image má `LANG=C.UTF-8`, encoding UTF-8.
Použit byl schválený Dockerfile; nejde o tvrzení bitové reprodukovatelnosti
jeho externích base/package závislostí.

- Image: `anime-db-lab-fabd895a642c:v6-a11a6b285a9b3b8fc3d3141e291fd094c631a96d`.
- Skutečné image ID: `sha256:4a54938a5abe7091faca4742f4fca64fd5f9e81a534e2fe050a810ab23a770d4`.
- Approved root: `/srv/animedb-v6-lab/approved-v2`.
- Generace: `generation-0dbeb0b487aaf3deb0d5da07`, deployment metadata **version=2**.
- Deployment manifest SHA: `e87a1d44328e56d0b2455bfc8beefa7e71699912cdd336ca185881482a6135a6`.

Původní shared verifier ověřil tři source SHA, tři odvozené SHA a sedm V2 bind
kotev. Skutečné app/edge mounty používají všechny tyto kotvy. Každý ze čtyř
reálných workerů má přesný image, UID/GID 1000:1000, network=none, read-only
root, sedm privátních runtime kotev a samostatný RW CIFS view; všechny skončily
exit 0. Běžná app má RO media view. Žádný legacy Compose fallback se nepoužil.

Oba aktuální root JSONy mají `protection_mode=manual_one_shot`. Koordinátor
má samostatné snapshot čekání 1800 s, automatické hook cesty null. Stávající
LAB start/probe připravil čerstvý skutečný mount-bound doklad: všech **9 CIFS
kontrol PASS**, včetně fsync, no-replace kolize a cross-directory rename.
Stejná mount session a capability evidence zůstaly platné i po dokončení.

## Browser, schválení a negativní scénář

Původní M8 Chromium/CDP skript byl pouze přizpůsoben ručnímu kroku a záznamu
časů. Nebyly mockované HTTP požadavky, coordinator, Docker, worker ani cleanup.
Celý běh má **24 browser kontrol PASS**, jeden start POST a jeden cleanup POST.

1. Read-only preview zachoval bytes SQLite.
2. Reálný worker vytvořil manifest a nezávislý SQLite backup.
3. Nginx při odstavené app zobrazil skutečný ruční formulář svázaný s manifestem.
4. Snapshot čas `2020-01-01T00:00:00Z` byl odmítnut HTTP 400. SQLite zůstala
   shodná, nevznikl E7 journal ani execution claim. Challenge zůstal nepřevzatý.
5. Následovalo jediné platné **LAB** potvrzení téhož návrhu, konkrétní WARNING
   review, přesné SHA schválení a samostatný start s potvrzením trvající výluky.
6. Původní E7 provedlo 10 MOVE + 3 KEEP + 1 QUARANTINE, celkem 14 COMPLETE.
7. E7H dostalo živý proposal a výslovný výběr všech 11 kandidátů. Záznamy
   INTENT/VERIFIED odpovídají přesně výběru a cleanup končí COMPLETE.
8. Guard i výsledková evidence končí NORMAL. Koordinátor hlásí
   `NORMAL/normal/available`, `app_ready=true`, inactive a šest PASS fází.
   Browser otevřel výslednou AnimeDB a `/health` vrátil 200.

Source SHA: `7e687a4c42041c1d40db52bda84a5832b7022b986ba1f8e0186f8771278362e7`.
Approved SHA: `ef2d7577c0c3c64b5647c76d20a21809a7b89bf6a915ac3cfbcc5cf0c62b2409`.
Cleanup SHA: `b799709e0597c35b5e768b4839b9d0e6609f22f6264c2168837d888fd2a93751`.

Snapshot ID je výslovně `LAB_ONLY_SIMULATED_SNAPSHOT_a11a6b2`, actor `M11 LAB ONLY`.
Nezávislá hostitelská kopie syntetického share byla uložená do lokální evidence;
nejde o uzamčený DSM snapshot. Také Windows SMB potvrzení je LAB simulace.
Runtime je `operator_attested`, strojová snapshot pole false/null. SHA této
neměnné evidence je shodné v approval/start a tatáž evidence se přenesla do
fresh execution admission. Tento výsledek neprokazuje Synology snapshot ani
strojové vyloučení vzdáleného Windows klienta.

## Nezávislá SQLite a filesystem kontrola

Původní M8 audit byl opět použit beze změny: **65 PASS, 0 FAIL**. Všech
15 golden souborů existuje právě jednou na očekávaných cílech, se stejným SHA
a velikostí, bez chybějících či přidaných souborů. Všech 10 potřebných target
adresářů existuje, 11 přesně schválených prázdných adresářů chybí.
IMPORT včetně obsahu a Duplicates zůstaly zachované.

Prázdné `Subs`/`Subs/empty` neměly žádný canonical target a byly v živém
schváleném cleanup výběru. Jejich odstranění odpovídá současnému kontraktu;
Subs nemá plošnou ochranu. Nepřesná opačná formulace v LAB README byla cíleně
opravena, engine ani schválený výběr se kvůli ní neměnily.

`integrity_check=ok`, `foreign_key_check` prázdný, schema v9. Úplné porovnání
všech **18 business tabulek / 31 řádků** s golden po aplikaci **22 schválených
locator patches pouze v RAM** je shodné; žádné jiné business hodnoty ani schema
se nezměnily. Backup odpovídá původnímu manifestu a je nezávislá kopie.
Doplňkový DB/runtime/container audit prošel **47/47**, root preservation audit
**11/11**. Skupiny se překrývají; nejsou součtem full pytest sady.

## Skutečné časy a ovládání

Časy jsou notebookové LAB měření, nikoli odhad Nexusu/RPi nebo Synology.
Čekání obsahuje automatizovanou roli člověka a záměrný negativní vstup.

| Úsek | Naměřeno |
| --- | ---: |
| Worker plan | 1.838 s |
| Ruční snapshot fáze včetně negativního vstupu | 11.441 s |
| Worker publish | 2.023 s |
| Prepare request → publikace | 16.923 s |
| Publikace → přesné schválení | 11.805 s |
| Publikace → execution acceptance | 18.644 s |
| Online check-start | 1.956 s |
| Execute worker včetně E7/E7H a čekání na cleanup | 43.208 s |
| Celý browserový průchod | 88.901 s |

Provozní návaznost: po prepare se v tomto běhu již načetla běžná job stránka,
ale app byla následně odstavená. Její polling pouze oznámil potřebu obnovení.
**Jedno ruční obnovení této stránky** v 18:17:04.677 UTC zpřístupnilo Nginx
formulář. Po platném potvrzení se review otevřelo automaticky; po NORMAL se
automaticky otevřel výsledek, další refresh nebyl potřeba. Funkční ani
architektonická oprava nebyla pro úspěšný průchod nutná.

Snapshot deadline zůstal 1800 s a původní 900sekundové approval/admission
okno se neposouvalo. Publikace 18:17:11.771535 UTC, schválení 18:17:23.577003,
acceptance 18:17:30.415855, výsledek NORMAL 18:18:11.327906.

## Podklady, změny a předání

Lokální neveřejná evidence: `docs/_local/2026-11-15-m11-manual-lab/`.
Klíčové soubory: `lab-before.json`, `lab-after.json`, `prepared-runtime.json`,
`prepare-lab.log`, `build-source.json`, `build.log`, `image-app-hashes.json`,
`actual-containers.json`, `browser.log`, `timings.json`, `preservation-audit.json`,
`business-runtime-audit.json` a `final-audit/audit.json`.
Browser evidence je v `browser-2026-11-15T18-16-51-791Z/`, včetně HTML/PNG,
časů a POST počtů. Použité one-off skripty jsou lokální adaptace původního M8
postupu, nikoli nový produktový framework.

Změny v repozitáři jsou pouze dokumentační:

- Tento nový report — skutečný integrační důkaz a limity.
- `docs/ROADMAP.md` — ověřený M11 gate; V6 zůstává otevřená do produkčního provedení.
- `deploy/lab/README.md` — oprava konkrétního popisu ochrany prázdného Subs.

Python/JS syntax lokálních helperů, Markdown struktura/relativní odkazy,
`git diff --check` a kontrola scope: **PASS**. Aplikační pytest,
compileall app/tests a další plný automatický LAB běh se neopakovaly: runtime
kód zůstal přesně na schváleném commitu a cílem byl jediný skutečný ruční běh.
Po dokončení neprovádět reset, nový E7, remount nebo startup smoke. Aktuální
LAB zůstává NORMAL s dostupnou aplikací a uchovanou evidencí pro Claude review.

Produkční předpoklady zůstávají oddělené: samostatně schválený release a
instalace koordinátoru/app/workeru na Nexus, aktuální odpovídající mount-bound
RO/RW doklady, produkční preview/backup, skutečný ručně vytvořený a uzamčený
DSM snapshot a odpojené Windows Anime SMB sdílení přes celé provozní okno,
přesná WARNING/SHA/start/E7H rozhodnutí a závěrečná kontrola. Dříve ověřené
Nexus/Synology capabilities M11 nezpochybňuje ani znovu nenahrazuje LAB důkazem.
Notebook neověřuje ARM64, NVMe first_run attestation ani Synology durability.

Nexus, Synology, produkční SQLite/schema a skutečný archiv Anime nebyly tímto
testem zasažené; produkční DB se neotevírala. Změny DB/filesystemu proběhly
jen v povolené syntetické pracovní knihovně LABu. Commit ani push neproběhl.
Následná nezávislá Claude revalidace M11 zůstává samostatným krokem.

## Nezávislá revalidace Claude (2026-11-15)

**PASS.** Revalidace byla read-only nad uloženou evidencí a dokončeným LABem.
LAB se neopakoval; nespustil se E7, remount, reset ani startup smoke. Do LABu
se nezapisovalo, SQLite se porovnávala jen jako kopie. Všech 65 položek
`SHA256SUMS` lokální evidence sedí.

- **Release:** image `4a54938a…` nese revision `a11a6b2`. Všech 159 souborů
  `app/` v běžící app i v nainstalovaném balíčku odpovídá `git show a11a6b2`.
  Koordinátor v `libexec` a edge `nginx.conf`/`service.js`/`index.html` jsou
  bajtově shodné s commitem.
- **Režim a V2:** přímo čtený worker JSON i root inspekce koordinátoru mají
  `manual_one_shot`. App a edge používají jen compose a `bind-0`…`bind-6`
  generace `0dbeb0b4…`. Tři source SHA odpovídají živým souborům a dev/inode
  kotev odpovídají metadatům. Čtyři workery: přesný image, 1000:1000,
  `network=none`, read-only root, exit 0. Adresář `snapshots` zůstal prázdný,
  LAB snapshot hook tedy neběžel.
- **Evidence:** SHA `operator_attestation` `df5ccf2b…` je shodné v approval,
  start requestu, acceptance i publish/execute runtime. Strojová snapshot pole
  jsou false/null. Rozdíl deadline − waiting je přesně 1800 s, approval i
  acceptance proběhly do 900 s od publikace.
- **E7/E7H:** journal má 71 záznamů, souvislý hash chain a 14 akcí COMPLETE
  (10 MOVE, 3 KEEP, 1 QUARANTINE). E7H má 11 INTENT a 11 VERIFIED. Odstraněné
  adresáře se rovnají schválenému výběru, přidané adresáře `new_directories`.
- **Data (vlastní porovnání):** 15 souborů odpovídá golden mapovanému přes
  manifest (SHA a velikost). SQLite: `integrity_check=ok`, FK prázdný, schema
  shodné s golden, 18 tabulek / 31 řádků. 22 rozdílů je přesně 22 schválených
  locator patches. Backup a guard `initial_db_fingerprint` jsou bajtově shodné
  s golden, takže odmítnutý vstup SQLite nezměnil.
- **Výsledek:** `/health` i `/_service/app-health` vrací 200, guard i progress
  jsou NORMAL. Beze změny zůstaly golden, 182 starších history položek,
  archivované snapshoty a původní DB i 14 původních nesledovaných položek.

Úprava README odpovídá kódu: `_protected_directory` chrání pouze `IMPORT`,
`Duplicates`, `#recycle` a `@eadir`. `Subs` se odstranil už v M8.

**UX poznámka (neblokující):** job stránka `/reorganization/jobs/<id>` se po
odstavení app sama nepřepne na Nginx formulář. Zobrazí „Stav není potvrzený.
Obnovte stránku…“ a formulář se objeví po jednom ručním obnovení. Bezpečnost
to neovlivňuje: bez potvrzení se nic nepublikuje a čekání po 1800 s končí
STOP. Nejde o blocker produkčního použití. Případná úprava je samostatná UX
práce mimo M11.
