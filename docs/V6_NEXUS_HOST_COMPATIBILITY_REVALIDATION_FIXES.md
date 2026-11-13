# V6 host compatibility – opravy Claude revalidace

2026-11-13; podklad:
`docs/_local/2026-11-13-claude-host-compat-reval/REMARKS_FOR_CODEX.md`.
HEAD stále `db40987da37bef5e74e5fb75c04874457e34bd37`, `main...origin/main`.
Nenasazeno, bez commitu/pushe. Architektura B1/B5/socketu zachována.

## Opravy

- **M1:** RELEASE_UPDATE C2 i rollback E v approved režimu používají existující
  `reorganization_autostart.py start`: resolve → ověření releasu/V2 původních SHA
  → příprava bindů → kontrola Compose modelu → edge → app. Raw up nad novou
  generací není podporovaný postup. Rollback má oddělené obnovení src/image,
  nové stage/review/approve současného `.env` a teprve potom start. Žádný fallback.
  Runbook výslovně řeší odstavení idle coordinatoru se starým configem v paměti
  a možnou recreaci edge kvůli novým bind sources. Při částečném C1 STOP.
- **M2:** nový produkční test soubor skutečně nastavuje approved_root. Testuje všech
  sedm privátních worker zdrojů, reálné V2 source_sha256 nad odlišnými odvozenými
  kopiemi, pořadí kontrol, STOP při změně releasu/image/živých vstupů a při výměně
  runtime adresáře mezi tick a workerem. Status/progress/ledger procházejí skutečnou
  RuntimeDirectory publikací. Root test navíc vykoná skutečné worker bindy i
  publikaci pod jiným UID; Docker dispatch je zde zachycený testovacím runnerem.
- **M3:** socketový předkrok před jakýmkoli Compose čtením: založení/ověření
  vyhrazené skupiny bez členů/aliasů/primárních uživatelů, skutečný GID, bezpečná
  aktualizace původního `.env` se zachováním vlastníka/módu, kontrola shody
  JSON/systemd/tmpfiles/group_add. A–E a OPERATIONS používají explicitní env,
  project directory a project anime-db. Režim approved/legacy se určí předem;
  nepřítomný root JSON není fallback. Nový edge se renderuje před C1.
- **m4:** konkrétní instalace trust adresářů, helperů, B5 skriptu a drop-inu.
  B5 drop-in až po root JSON a schválené generaci; daemon-reload bez restartu.
- **m5:** health čekání 900 s a TimeoutStartSec=1500; pokrývá pomalý NORMAL
  admission a bounded Compose volání. Health smyčka neopakuje up.
- **m6:** child TimeoutExpired se převádí na OSError/ETIMEDOUT pro stávající
  fail-closed error handling.
- **m7:** # v source/target hodnotě se odmítá včetně quoted variant; skutečné
  YAML komentáře zůstávají komentáři. Žádné tiché zkrácení na jinou cestu.
- **m8 – zbývající validační gate:** úplný V2 LAB průchod s reálným Docker/Compose
  nebyl proveden. Root mechanika, produkční orchestrace a reálný Compose render
  jsou ověřené odděleně. Před aktivací Nexusu je stále nutná evidence celého LAB
  průchodu s approved_root; legacy LAB ani tato sada ji nenahrazují. Gate je
  výslovně uvedený ve V6_COORDINATOR, nikoli vydávaný za PASS.

## Výsledky a opakování

**125 passed** v cílené sadě:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider \
  tests/test_nexus_autostart.py tests/test_nexus_host_paths.py \
  tests/test_nexus_production_mode.py tests/test_nexus_compose_environment.py \
  tests/test_reorganization_deployment.py tests/test_reorganization_coordinator.py \
  -q --tb=short
```

Unix socket vyžaduje povolený lokální socket mimo omezený sandbox. Compose testy
neprovádějí deploy: s GID pouze config render; bez něj config/config --images/
ps/logs/stop všechny skončí při interpolaci povinné proměnné před Docker operací.

**8 passed** root testů v izolovaném existujícím LAB image, bez sítě/pullu, repo
pouze RO, zápisy/mounty jen v kontejnerovém /run a /tmp. Přesný Docker příkaz z
[původního reportu, H](V6_NEXUS_HOST_COMPATIBILITY_REVIEW.md#h-nezávislá-revalidace-claudem)
se nemění; test_nexus_host_privileges.py nyní obsahuje osm případů. Skipped root
sada bez root oprávnění se za tuto validaci nepovažuje. Updater `.env` z runbooku
byl skutečně spuštěn nad dočasným user-owned stackem, včetně zachování 0640/UID/GID
 a odmítnutí GID 101. Pomalá health smyčka je testována bez skutečného čekání.

compileall app/tests/host modulů, bash -n obou skriptů i všech bash bloků tří
upravených runbooků, relativní dokumentační odkazy a git diff --check: PASS.
Full pytest, skutečný boot, E7/E7H/CIFS experimenty a plný Docker LAB workflow
záměrně neprovedeny. Šablony ani jejich contract se neměnily, Jinja load se nespouštěl.

## Soubory tohoto opravného kroku

- Runbooky: deploy/nexus/docs/RELEASE_UPDATE.md, OPERATIONS.md, V6_COORDINATOR.md.
- Malé runtime opravy: deploy/nexus/host_io.py, reorganization_deployment.py,
  scripts/autostart.sh, systemd/anime-db.service.d/approved-production.conf.
- Doplněné testy: tests/test_nexus_autostart.py, test_nexus_host_paths.py,
  test_nexus_host_privileges.py.
- Nové testy: tests/test_nexus_production_mode.py, test_nexus_compose_environment.py.
- Tento report a odkaz z původního compatibility reportu.

Nezávislá revalidace má kromě opakování sad sledovat konkrétní approved větve
C2/E a OPERATIONS (žádný raw generation up), pořadí skupina → GID → env →
konzistence → Compose a to, že m8 zůstává explicitním aktivačním gate.

Schema, app business/engine, produkční SQLite, metadata, NAS, vlastnictví
/srv/nexus, image, snapshoty, Restic a běžící služby beze změny. V tomto opravném
kroku nebylo žádné SSH na Nexus, hostitelský systemd/Docker restart, build,
reorganizace, commit ani push. Původní nesledované soubory zachovány.

Závěrečný `git diff --stat` celého dosavadního B1/B5 pracovního stromu:
**16 files changed, 760 insertions(+), 157 deletions(-)**; navíc dosud nesledované
soubory této i předchozí implementace (prostý diff --stat je nezahrnuje).
Git status: `main...origin/main`, změny unstaged, původní nesledovaná práce
zachována; HEAD beze změny. Výše uvedený seznam odlišuje soubory tohoto opravného
kroku od celého dosavadního diffu.
