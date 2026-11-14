# AnimeDB V6 – M8: skutečný V2 Docker/Compose LAB

2026-11-14, Linux Mint notebook, amd64. Historický výchozí HEAD původního LAB běhu:
`2144e43f480e778b18428cb7542fef9b3009e6a3`.
Výsledný commit M8: `ee11a9aabfdfe9ff207ccec45115ff74d6a5fce7` (`ee11a9a`),
uložený na `origin/main` po nezávislé Claude revalidaci **PASS s poznámkami**.
**M8: PASS – doložený skutečný LAB průchod a dokončená nezávislá revalidace.**
Tento výsledek neopravňuje k aktivaci Nexusu ani k operacím na Synology.

Níže uvedené počty a důkazy popisují původní M8 validaci: 134 cílených PASS a
10 izolovaných root PASS. Následný test/docs cleanup není nový milník ani
změna produkčního chování. Soukromé důkazy zůstávají pouze v lokálním,
git-ignored `docs/_local/2026-11-14-m8-v2-lab/`; nejsou součástí commitu ani
veřejně dostupnými přílohami. Odkazy na jejich názvy níže jsou pouze lokální
identifikací pro oprávněnou revalidaci.

## A. Původní LAB a rozdíl proti V2

Původní lab.sh nepředával `--approved-root`, používal `stack/approved-v6` a
coordinator JSON neobsahoval approved_root. Šlo o legacy V1 kopie tří Compose
vstupů, bez V2 původních SHA a runtime bind kotev. Původní edge navíc neměl
socket group_add. Kód, nikoli historický úspěšný report, rozhodl o této klasifikaci.

Read-only audit před přípravou ověřil zastavené kontejnery, chybějící CIFS mounty
anebo coordinator unit a zachovanou historickou NORMAL evidenci. Legacy data
měla 64/64 auditních kontrol PASS; nejsou použita jako důkaz nového V2 běhu.

Předchozí jednorázová příprava zachovala původní appdata, stack včetně pěti
legacy generací, share, snapshoty/logy a kopie helperů/edge pod:
`/srv/animedb-v6-lab/history/legacy-20261114T104338Z` (`root:root 0700`).
Golden data zůstala na místě. Archivní DB a všech 15 archivních share souborů
mají původní SHA. V poslední navazující části se prepare-host.sh znovu
nespouštěl, nic se nearchivovalo, neresetovalo ani neregenerovalo.

## B. Nezbytné změny a zjištěné chyby

- LAB má samostatný root-controlled `approved-v2`, runtime-owned appdata předky
  a privátní coordinator adresář mimo ně. Tři vrstvy a projekt `animedb-v6-lab`
  zůstaly zachované; SMB má svůj původní oddělený LAB projekt.
- Sdílený autostart má explicitní allowlisted LAB profil. Výchozí produkční CLI
  dál vyžaduje `anime-db` a Nexus first_run entrypoint. Nový lab_host adaptér
  používá původní resolver/autostart; netvoří jiný deployment engine.
- Nezměněný release verifier pracuje se skutečným lokálním Git clone
  `release/src`, odpovídajícím HEAD, a LAB edge ve stejném relativním umístění
  jako Nexus. Image nese build-input hash i pravý release HEAD.
- File bind entrypointu nebyl slučitelný s V2 adresářovým kontraktem. LAB proto
  připojuje trusted libexec adresář read-only do `/lab`.
- Hash image používal locale-dependent sort. C a české locale dávaly různé tagy
  nad stejnými bytes. `LC_ALL=C sort -z` odstraňuje závislost; regresní test RED→GREEN.
- Před setup/startem koordinátoru se ověřuje skutečná skupina
  `animedb-v6-lab-control`, shoda GID (2001), absence aliasů/členů/primárních
  uživatelů a oddělení od GID aplikace i Nginxu. Skupinu na notebooku vytvořil uživatel.
- Nové `lab.sh coordinator` pouze ověří existující V2 stav a obnoví koordinátor.
  Nevytváří kotvy, nemění generaci/mounty a nepouští Compose up. Readiness
  vyžaduje aktivní unit, socket a skutečný control GET přes Nginx.
- Skutečný systemd probe prokázal chybějící effective/permitted CAP_SETUID přes
  přítomnost v bounding setu: setreuid i RuntimeDirectory skončily EPERM.
  `AmbientCapabilities=CAP_SETUID CAP_SETGID` zachovalo požadované capabilities
  přes exec. Opravený skutečný probe i provozní publikace prošly; NNP zůstává zapnuté.
  Stejné nastavení je doplněno pouze do zdrojové Nexus unity, na Nexus nenasazeno.
- Linux ismount nerozpozná některé bindy na stejném filesystemu. Read-only
  admission používá mountinfo a přesné source/target identity. Sdílený host_io
  navíc nyní odmítá nesprávnou již připojenou kotvu přes descriptor mount ID,
  místo aby ji překryl. Skutečný root test nejprve selhal DID NOT RAISE,
  po opravě prošel, stejně jako skutečný host negative test.

E7/E7H engine, business pravidla, schéma a aplikační šablony se neměnily.

## C. Důkaz approved_root / V2

Aktivní kořen: `/srv/animedb-v6-lab/approved-v2` pod root-controlled `/srv` a LAB.
Aktivní generace: `generation-ca5033109978681a630ae2f6`.
Reviewed deployment manifest SHA:
`5b50dec8d40ae4653b6cb396dedf209bbabc6d41f04456e56ae86f83a88fd908`.
Metadata mají version=2, tři source_sha256, tři SHA odvozených kopií a **7 bindů**.
Původní i odvozené SHA jsou různé a ověřené původním shared verifierem.
Úplné hodnoty, zdroje/cíle, dev/ino, mountinfo a Docker inspect jsou v
sanitizované runtime evidenci (`sanitized-runtime.json`, soukromě lokálně).

Tři zdroje v pořadí:

1. `/srv/animedb-v6-lab/stack/compose.yaml`
2. `/srv/animedb-v6-lab/release/src/deploy/nexus/service-edge/compose.edge.yaml`
3. `/srv/animedb-v6-lab/stack/compose.v6.yaml`

Compose i app/edge skutečně používaly kopie/kotvy této generace, nikoli live
nebo legacy fallback. Worker check-start a execute mají každý devět skutečných
bindů: sedm privátních runtime kotev, LAB RW CIFS a trusted worker config.
Všech sedm worker kotev odpovídá původním adresářům dev/ino.

Pořadí: resolve/hash kontrola → release a původní SHA/env → bindy → Compose model
→ edge/app. Chybějící/změněné selection či release nezpůsobí fallback.

## D. Docker, Samba, CIFS a socket

- Skutečný Docker Engine 29.9.2, Compose v5.6.1, Linux kernel 7.0.0-38-generic.
- Aktuální app image byl skutečně sestaven:
  `anime-db-lab-451e1c259a32:v6-2144e43f480e778b18428cb7542fef9b3009e6a3`,
  ID `sha256:1b3e1005b1076e9bfd634180f21acc3be2790542706b38b913922441a38b7f06`.
  Sestaven i LAB Samba image. Viz build log (`build.log`, soukromě lokálně).
- Samba běží jen na notebooku, share `//127.0.0.1/animelab`, loopback port 1445.
- RO CIFS view používá `lab_scan`, RW view `lab_worker`; SMB3.1.1, odlišné superblocky.
  App má pouze RO view; skutečné MOVE/QUARANTINE provedl izolovaný RW worker.
- Mount-bound capability report: **9 PASS**, včetně fsync souboru/adresáře,
  odmítnutí RENAME_NOREPLACE kolize, zachování obsahu/inode při cross-dir rename,
  RO odmítnutí zápisu a odstranění vlastních probe artefaktů. Bez benchmarků/remountu.
- Coordinator: root, group `animedb-v6-lab-control`/2001, NNP a původní bounded caps;
  explicitní ambient SETUID/SETGID. Socket `root:2001 0660`, adresář 0750.
  Nginx zůstává 101:101 s group_add 2001. Bez skupiny connect odmítnut, se skupinou povolen.
- App i edge zůstaly stejnými kontejnery při obnoveních koordinátoru a negativních
  kontrolách. Web `/health` i `/_service/app-health` vrací 200/ok; control GET funguje.

## E. Skutečný browser E7/E7H průchod

Chromium/CDP použilo skutečný Nginx, coordinator a Docker workery, bez mocků.
Browser JSON (`browser-2026-11-14T11-43-46-618Z/browser.json`, soukromě lokálně),
log (`browser-v2.log`, soukromě lokálně) a
závěrečný screenshot (`browser-2026-11-14T11-43-46-618Z/09-normal-result.png`, soukromě lokálně).

- Read-only preview zachoval SHA LAB DB. Nový finální manifest vznikl na hostiteli.
- Jedno upozornění bylo výslovně potvrzeno. Přesný SHA byl schválen před
  samostatným startem; samotné schválení ještě E7 nespustilo.
- Aktuální fixtures i skutečný manifest: **14 operací = 10 MOVE + 3 KEEP + 1 QUARANTINE**.
- Živý post-E7 návrh obsahoval **11 E7H kandidátů**. Byly explicitně vybrány a
  schváleny k odstranění přes jejich přesný proposal SHA. Počet není převzatý
  z historického reportu; audit jej odvodil ze skutečného requestu/journalu.
- Během cleanup čekání byla app zastavená, servisní stránka a socket dostupné.
- E7 journal má všechny akce COMPLETE, E7H přesně schválené INTENT/VERIFIED a COMPLETE.
  Guard končí NORMAL; coordinator hlásí app_restart_state=available, app_ready=true,
  active=false a šest PASS fází. Browser ověřil konečnou result stránku a health.
- Přesně jeden start POST a jeden cleanup decision POST v úspěšném browser běhu.

Source SHA: `416a54ff86d25b1cb14060f1d6a4efb51e034fcd7b607d32d23f50362c3887bb`.
Approved SHA: `82847ffb7f282ce79ee07338125d10efe65bbc908bcc58a7520c77cd737dfa69`.
Cleanup proposal SHA: `b99dc1513b70a351ba41a34a1d37d41ac4eab4198a1e37332429f8c5043d49b2`.

Před úspěšným během selhal jeden prepare požadavek na chybějícím effective
CAP_SETUID, ještě před stop-writers/worker dispatch. Zůstává v evidenci jako STOP;
neproběhl E7 retry ani obnova dat. Úspěšný běh použil nový čerstvý návrh.

Schvalovací actor je `administrator`; explicitní nezávislý cleanup actor je
`M8 LAB`. To odpovídá současnému kontraktu. První pomocný audit nesprávně požadoval
stejného actora i pro cleanup (63 PASS/1 FAIL); byl opraven podle require_actor
kontraktu, žádná uložená evidence se nepřepisovala. Původní audit je zachovaný.

## F. SQLite a skutečný filesystem

Finální audit po negativních testech (`v2-post-negative-audit/audit.json`, soukromě lokálně):
**65 PASS, 0 FAIL**. integrity_check=ok, foreign_key_check prázdný, user_version=9,
všech 9 video locatorů i titulkové locatory vedou na skutečné schválené cíle.
Všech **15 golden souborů je právě jednou na očekávané cestě** se shodným SHA a
velikostí; bez chybějících nebo přidaných souborů. Schválených 11 adresářů chybí,
10 potřebných cílových adresářů existuje, IMPORT a Duplicates jsou zachované.

Úplné porovnání DB (`business-data-audit.json`, soukromě lokálně): všech
18 tabulek / 31 řádků přesně odpovídá golden DB po aplikaci **22 schválených
locator patches pouze v paměti**. Schéma i user_version se nezměnily, žádné jiné
business hodnoty se neliší. Audit používá mode=ro/immutable nebo offline kopii;
nezapíná běžný aplikační startup.

Golden DB SHA:
`292d7857a9c2749f6a2f48e8e6397f22f8fd22ebf4aaee40bc2fb2def69bb7a2`.
Výsledná LAB DB SHA:
`d5dffbd849616eb75a6e96c7b14fd298674c87c094dec9fb94e818f6fcd3121e`.
Golden i historický archiv zůstaly zachované.

## G. Skutečné negativní kontroly

**23 PASS / 0 FAIL**, viz log (`negative-host.log`, soukromě lokálně)
a seznam kontrol (`negative-results.json`, soukromě lokálně).
Testovací generace a strace důkazy zůstávají pouze pod
`/srv/animedb-v6-lab/coordinator/m8-negative-53yuglqm`.

Odmítnuty změněný manifest, odvozená Compose kopie, neplatná/chybějící selection,
nesouhlas původního source SHA, release/image mismatch, chybějící kotva, kotva
s nebezpečným vlastníkem a skutečně připojená nesprávná root-private kotva na
stejném filesystemu. strace dokládá odmítnutí ještě před exec Dockeru.
Žádný fallback na legacy Compose ani nové app/edge up.

Pozitivní kontrola obnovila testovací V2 bindy a provedla skutečný Compose config
render; ověřila sedm identit. Socket bez správné skupiny odmítl connect.
Identita/start time app/edge/SMB a SHA/mtime LAB DB před/po jsou stejné.
Aktivní schválená generace i původní capability mount session zůstávají platné.

Opravený host_io byl do LAB root kopie instalován po dokončeném E7/E7H. Jeho
nová větev se týká pouze odmítání nesprávných existujících kotev; validní větev
ponechává kotvy beze změny. Byla ověřena deseti root testy a skutečnými negativními
kontrolami. Fyzické E7 se po této opravě neopakovalo a golden se neresetovalo.

## H. Přesné výsledky a limity

| Kontrola | PASS | FAIL | SKIP |
| --- | ---: | ---: | ---: |
| Skutečné CIFS capability checks | 9 | 0 | 0 |
| Skutečný browser průchod | 16 | 0 | 0 |
| SQLite/journal/filesystem audit | 65 | 0 | 0 |
| Úplné DB schema/rows/approved-patch porovnání | 3 | 0 | 0 |
| Skutečné host negativní/restore/socket checks | 23 | 0 | 0 |
| Závěrečná cílená pytest sada | 134 | 0 | 0 |
| Skutečné izolované root testy | 10 | 0 | 0 |
| Dříve spuštěná celá pytest sada | 6106 | 4 | 47 |

Kategorie se nepřičítají k full suite: jde o překrývající se vrstvy validace.
Celá sada má také 43 warnings a není vydávaná za zelenou.
Čtyři původní FAIL byly reprodukované na čisté baseline `2144e43`
(`2144e43f480e778b18428cb7542fef9b3009e6a3`) v izolovaném Dockeru
s ext4 scratch, viz baseline log (`baseline-failures-durable.log`, soukromě lokálně):

- test_v6_execution_manifest.py::test_generation_cli_is_the_only_production_human_criticality_composition
- test_v6_execution_projection.py::test_unapproved_schema_or_user_version_change_aborts_final_transaction[schema]
- test_v6_execution_projection.py::test_unapproved_schema_or_user_version_change_aborts_final_transaction[user_version]
- test_v6_execution_projection.py::test_external_data_version_change_after_final_commit_is_a_stop

První očekává starší seznam callerů, další tři jiné chybové zprávy při bezpečném
STOP. Nebyly způsobené M8 diffem a v rámci M8 se neopravovaly. Jejich zastaralá
očekávání řeší následný test/docs cleanup; historický výsledek full suite
v tabulce zůstává zachovaný. Pokusy o baseline
v host namespace s Docker nsfs a na tmpfs byly správně odmítnuty scratch kontraktem;
jejich neúspěšné logy zůstávají zachované, nejsou označené jako reprodukce těchto čtyř FAIL.

compileall app/tests/deploy modulů, bash -n, syntaxe CDP skriptu a diff --check:
PASS. Jinja load vynechán: šablony ani backend template contract se neměnily.
Samostatný lab_smoke.py se po dokončeném workflow nespouštěl: obsahuje idle-only
kontroly a další řízenou odstávku. Skutečné zastavení i návrat aplikace jsou
doložené browser E7/E7H během; samostatnému smoke skriptu se PASS nepřipisuje.
Celá pytest sada se po každé host opravě neopakovala; konečný diff pokrývá
závěrečná cílená i skutečná root sada. Nové CIFS/performance benchmarky se neprováděly. Nexus ARM64/systemd boot,
Synology ACL/durability, skutečné Synology snapshoty a externí writer fence
nejsou tímto notebookovým testem ověřené. LAB používá Samba server, nezávislou
syntetickou snapshot kopii, LAB hooky a lab_entrypoint místo NVMe first_run attestation.

## I. Soubory a nezávislá Claude revalidace

Změněné sledované soubory:

- deploy/lab/README.md, compose.yaml, lab.sh, lab_smoke.py
- deploy/nexus/reorganization_autostart.py, host_io.py,
  systemd/animedb-reorganization.service, docs/V6_COORDINATOR.md
- tests/test_nexus_autostart.py, test_nexus_host_paths.py, test_nexus_host_privileges.py

Nové M8 soubory: deploy/lab/lab_host.py, tests/test_lab_host.py,
tests/test_lab_shell.py a tento report. Původní untracked práce není součástí M8.
Pracovní scripts/logy/screenshoty jsou v git-ignored docs/_local/2026-11-14-m8-v2-lab.

Předání Claudovi (`CLAUDE_REVALIDATION.md`, soukromě lokálně) obsahuje
přesné podklady a příkazy původního předání. Nezávislá Claude revalidace M8
proběhla s výsledkem **PASS s poznámkami**; výsledný commit je `ee11a9a`.

## J. Závěr a bezpečné dokončení

**M8 technicky PASS:** skutečný úplný V2 approved_root/Docker/Compose/CIFS/browser
E7/E7H průchod je doložen včetně NORMAL, návratu webu, auditu dat a negativních
kontrol. Nezávislá Claude revalidace skončila **PASS s poznámkami**, M8 je
uložený v commitu `ee11a9a` na `origin/main`. Nejde o schválení nasazení nebo aktivace Nexusu.

Schema, produkční data/anime.db, Nexus a Synology se neměnily; produkční SQLite
se ani nečetla. Změny SQLite a knihovny proběhly pouze v izolovaném LABu podle
syntetického schváleného manifestu. Historický archiv i golden zůstávají zachované.
Původní LAB běh sám nevytvořil commit/push ani neměnil větev; následná integrace
M8 je identifikovaná commitem `ee11a9a`. Historický Git výpis v soukromém lokálním
předávacím balíku zachycuje stav před tímto commitem, nikoli aktuální pracovní strom.
Rebase, docker system prune ani produkční aktivace nebyly součástí M8.
