# V6: host coordinator pro workflow v prohlížeči

Tento instalační podklad není doklad nasazení ani souhlas se změnou produkce.
Lokálně jsou ověřeny transport, orchestrace s falešným Docker runtime a původní
E7/E7H na syntetických datech. Skutečný Nexus, RW CIFS capabilities, snapshot
backend, writer hooks a kompletní produkční průchod zůstávají k ověření před
aktivací. Chybějící integrace znamená viditelný STOP. Původní E7, journal,
approval ledger, execution guard a NORMAL validace zůstávají autoritou.

Po jednorázové instalaci probíhá každý běh z prohlížeče: náhled → příprava
host manifestu → přesné review a schválení SHA → samostatný start → původní
E7 → živý E7H návrh s explicitním zachováním všech nebo výběrem adresářů →
NORMAL → ověřený restart aplikace. Žádné per-run terminálové schvalování.
Chyba při přípravě obnoví původní aplikaci přes její startup guard, aby šly
opravit uložené autority. Chyba po startu E7 aplikaci automaticky neobnovuje.

## Oddělení oprávnění

- Běžný `app` dál používá existující `NAS_SCAN_USER`, `/mnt/nas-anime` a RO
  `/media/anime`. Nedostává Docker socket, credentials ani RW alias knihovny.
- Existující druhý účet `NAS_IMPORT_USER` slouží pouze oddělenému
  `/mnt/nas-anime-v6-rw`. Nový NAS účet ani změna práv prvního účtu nejsou potřeba.
- Host service vlastní root. Pouští pouze image z root konfigurace, bez pull,
  s pevným Python modulem, bindy, non-root UID/GID, bez sítě a capabilities.
  Nepouští shell a nepřijímá příkazy, image ani host cesty z webu.
- `edge` má pouze RO veřejný progress a adresář s Unix socketem. Nedostává
  private ledger, DB, NAS, credentials ani Docker socket. Zápis přes socket
  dovoluje jedině aktuální E7H rozhodnutí; nemůže spouštět joby ani restarty.

| Host cesta | Worker cesta / účel |
| --- | --- |
| `/srv/nexus/appdata/anime-db/db` | `/data` RW |
| `/srv/nexus/appdata/anime-db/state` | `/state` RW; stejný `runtime.lock` jako app |
| `/mnt/nas-anime-v6-rw` | `/media/anime` RW, stejná vnitřní cesta jako app |
| `approval/inbox` | `/approval-inbox` RW pro návrhy a výsledky, app má RO |
| `approval/outbox` | `/approval-outbox` RO pro přesná rozhodnutí webu |
| `approval/ledger` | `/approval-ledger` RW, mimo app a edge |
| `backups/pre-change` | `/backups` RW, app potřebuje odpovídající RO bind |
| `service` | `/service-status` RW, edge má RO `/public-status` |
| Root worker JSON | RO `/coordinator-config/worker.json` |
| `coordinator` | root-only claims, chyby a snapshot receipts; worker nemountuje |
| `/run/animedb-reorganization` | edge RO `/run/animedb-control`, socket `coordinator.sock` |

Všechny relativní host cesty v tabulce jsou pod
`/srv/nexus/appdata/anime-db`. DB/lock/backup/názvy kořenů uvnitř kontejneru
musí přesně odpovídat worker konfiguraci; namountování stejné knihovny pod jinou
vnitřní cestou zneplatňuje manifest. Snapshot receipts jsou host-private.
Publikovaný proposal obsahuje runtime evidence, které používá stávající review.

## Jednorázové prerequisites správce

1. Přečíst aktuální [architekturu](ARCHITECTURE.md),
   [release postup](RELEASE_UPDATE.md) a [NAS preflight](N4B2_COMPATIBILITY.md).
   Ověřit současné cesty, UID/GID a image, neobnovovat DB ze starší relace.
   Před instalací pořídit aktuální read-only fingerprint, velikost a mtime DB
   a mediální baseline pro tento zásah. Neprovádět scan, migraci ani opravy.
2. Instalovat konkrétní revidovaný release obsahující worker a coordinator.
   `app` i worker musí používat přesně tentýž lokální verzovaný image
   `anime-db:v6-<commit>` nebo digest. Image se sestaví samostatně podle
   release runbooku; coordinator nikdy nestaví ani nestahuje image.
3. Zachovat existující tři Compose soubory a jejich pořadí. Následující
   příklad používá stack adresář `/srv/nexus/stacks/anime-db`; dosaďte skutečný
   aktuální `STACK_DIR` z release runbooku také do JSON konfigurace a příkazů.
   Kvůli příkladu nepřesouvat stack, `src/`, `releases/` ani appdata:

   ```text
   /srv/nexus/stacks/anime-db/compose.yaml
   /srv/nexus/projects/anime-db/src/deploy/nexus/service-edge/compose.edge.yaml
   /srv/nexus/stacks/anime-db/compose.v6.yaml
   ```

   `src/` zůstává pod běžným vývojovým účtem, včetně současného módu `775`;
   `releases/` rovněž zůstává uživatelský. Git a příprava zdrojů nepoužívají root.
   Privilegovaný coordinator nečte živé YAML z tohoto zapisovatelného stromu.
   Používá přesné, samostatně schválené kopie těchto tří vstupů a `.env` pod
   root-owned `$STACK_DIR/approved-v6/`. Nejde o další stack: původní vstupy jsou
   jediným editovaným zdrojem a schválená generace je jejich byte-exact snapshot.
   Před každým Docker/Compose voláním se ověří root vlastnictví a SHA všech kopií.
   Stejný project name `anime-db`, původní `--project-directory` a pořadí tří `-f`
   zachovají identitu existujícího deploymentu. Podrobnosti jsou níže a v
   [release runbooku](RELEASE_UPDATE.md#schválený-vstup-koordinátoru).
   Žádný rekurzivní `chown src/` nebo `releases/` není potřeba ani přípustný.
4. Root JSON podle [coordinator příkladu](../config/reorganization-coordinator.json.example)
   uložit jako `$STACK_DIR/reorganization-coordinator.json` s módem `0600`.
   Nahradit image a exact public origin skutečnými hodnotami. Worker JSON podle
   [příkladu](../config/reorganization-worker.json.example) uložit vedle s módem
   `0644` (root-owned, non-root worker jej musí číst). JSON je konfigurace,
   nikoli místo pro credentials. Statické capability příznaky jsou pouze
   konfigurační povolení, nikdy provozní důkaz. Existující RuntimeEvidence se
   načítá z root-controlled `runtime_evidence` souboru; musí obsahovat skutečně
   získaný důkaz pro aktuální mount identity, včetně mount session. Remount nebo
   změna identity starý důkaz zneplatní a vyžaduje nové ověření. Scanner a další
   writeři se dokládají skutečným aktuálním výsledkem coordinatoru/hooků.
5. Privátní host adresář `coordinator/` musí být root `0700`. DB/state,
   `approval/{inbox,outbox,ledger}`, `backups/pre-change` a `service` musí
   patřit runtime UID/GID bez group/other zápisu; běžně `0700`, veřejný `service`
   `0755`. Každý je samostatný existující adresář, nikoli symlink ani vnořený
   kořen jiné role. Předkové musí zůstat pod kontrolou root. `runtime.lock`
   se zachovává; nedělat nový soubor místo současného runtime locku.
6. Připravit níže popsaný samostatný RW mount a skutečné trusted snapshot/writer
   hooks. Bez nich službu neaktivovat jako funkční V6. Automatický reset,
   fake snapshot ID ani `true` místo writer fence nejsou validní integrace.

## Oddělený RW mount se stávajícím druhým účtem

Tento krok je instalační změna host/NAS přístupu, vyžaduje samostatně schválené
nasazení. Nemění existující RO mount, jeho účet, mount jednotku ani CIFS session.
Root uloží credentials **již existujícího** `NAS_IMPORT_USER` do samostatného
souboru `0600`; žádné heslo nepatří do shell command line, Gitu ani JSON config.

Příklad příkazů po schválení instalace, se skutečnými již ověřenými CIFS options:

```bash
sudo install -d -m 0755 /mnt/nas-anime-v6-rw
sudo install -d -m 0700 /etc/anime-db
sudo install -m 0600 /dev/null /etc/anime-db/nas-v6-rw.credentials
sudoedit /etc/anime-db/nas-v6-rw.credentials
```

Soubor obsahuje `username=`, `password=` a případně existující `domain=`.
Nikdy nepoužít účet `NAS_SCAN_USER`. Jednorázově namountovat například:

```bash
sudo mount -t cifs //nas.example.test/Anime /mnt/nas-anime-v6-rw \
  -o credentials=/etc/anime-db/nas-v6-rw.credentials,rw,vers=3.1.1,uid=1000,gid=1000,nosuid,nodev,noexec
findmnt --kernel --nocanonicalize -rn -T /mnt/nas-anime-v6-rw -o TARGET,SOURCE,FSTYPE,VFS-OPTIONS,FS-OPTIONS
findmnt --kernel --nocanonicalize -rn -T /mnt/nas-anime -o TARGET,SOURCE,FSTYPE,VFS-OPTIONS,FS-OPTIONS
```

Options jsou příklad, nikoli potvrzení kompatibility současného Nexusu.
Správce musí doložit oddělenou RW session druhého účtu, celou share `/`, správný
source a žádný podmount ani alias v aplikačním kontejneru. RO účet a RO pohled
app zůstávají nezměněné. CIFS `rw` v mountinfo neprokazuje serverovou ACL ani
bezpečný rename; capability test musí být výslovně schválený a používat pouze
samostatné disposable testovací soubory. Žádný probe nad existujícími médii.
Změnu skutečných dat knihovny tato instalace neschvaluje.

Pro opakované browser workflow mount připravit při jednorázové instalaci a
udržovat v provozním okně. Coordinator nic nemountuje, neumountuje ani nečte
credentials. Neexistující nebo RO mount zastaví žádost před odstavením app.
Po skončení schváleného V6 provozního okna se zastaví coordinator a správce
samostatně odpojí pouze tento RW mount; nesmí zasáhnout původní RO mount.

## Pevné trusted executable integrace

Tři spustitelné soubory vlastní root, jejich kód i předkové jsou bez zápisu
app/edge. Nejsou obecné shell command endpointy. Coordinator je volá přímo
argv, bez argumentů a bez shellu, s omezeným prostředím. Standardní výstup je
jediný malý JSON; stderr je diagnostika správce. Chyba, timeout, neúplný nebo
neočekávaný JSON znamená STOP. Bez reálného backendu není dodán fake executable.

`/usr/local/libexec/anime-db/v6-stop-writers` musí skutečně zastavit a ověřit
všechny externí writery a zachovat fence přes snapshot, publish nebo celé E7.
Coordinator nejprve zastaví pouze Compose `app`, nikdy `edge`. Hook vrací přesně:

```json
{"writers_stopped": true}
```

`/usr/local/libexec/anime-db/v6-start-writers` obnovuje pouze původně povolené
externí writery po ověřeném startu `app`. Vrací přesně:

```json
{"writers_started": true}
```

Neexistují-li žádní externí writeři, hook to musí ověřovat na základě konkrétní
správcem schválené konfigurace; samotná úspěšná exit code bez JSON není důkaz.
Hooky se nesmějí spouštět asynchronně a předčasně potvrdit úspěch.

`/usr/local/libexec/anime-db/v6-snapshot` musí po zastavení writerů vytvořit a
nezávisle ověřit skutečný NAS snapshot celé schválené knihovny. Čte stdin:

```json
{"manifest_id":"<64 hex>","created_at":"<UTC timestamp>","source_sha256":"<64 hex>"}
```

Vrací pouze skutečný doklad, přesně:

```json
{"snapshot_id":"<skutečné ID>","snapshot_at":"<UTC timestamp>","snapshot_manifest_id":"<stejné manifest_id>"}
```

Snapshot musí odpovídat danému manifestu a časovému gate původního E7, nikoli
starému běhu; stdout není způsob převzetí neověřeného tvrzení webu. Backend,
NAS API a serverové oprávnění nejsou v tomto patchi ověřeny. Snapshot executable
musí být pro konkrétní Synology nakonfigurován a integračně ověřen před
nasazením; chybějící hook je blokující prerequisite. Lokální nezávislou DB
kopii vytváří původní worker automaticky do `/backups/<request_id>.sqlite`
bez přepsání, kontroluje fingerprint a váže ji k manifestu.

## Instalace host služby a edge

Po splnění prerequisites správce může instalovat revidované soubory:

```bash
sudo install -d -m 0755 /usr/local/libexec/anime-db
sudo install -o root -g root -m 0755 \
  deploy/nexus/reorganization_coordinator.py \
  /usr/local/libexec/anime-db/reorganization_coordinator.py
sudo install -o root -g root -m 0644 \
  deploy/nexus/reorganization_deployment.py \
  /usr/local/libexec/anime-db/reorganization_deployment.py
sudo install -o root -g root -m 0644 \
  deploy/nexus/systemd/animedb-reorganization.service \
  /etc/systemd/system/animedb-reorganization.service
sudo install -d -o root -g root -m 0700 /srv/nexus/appdata/anime-db/coordinator
sudo install -o root -g root -m 0644 \
  deploy/nexus/tmpfiles.d/animedb-reorganization.conf \
  /etc/tmpfiles.d/animedb-reorganization.conf
sudo install -d -o root -g root -m 0755 /etc/systemd/system/docker.service.d
sudo install -o root -g root -m 0644 \
  deploy/nexus/systemd/docker.service.d/animedb-runtime.conf \
  /etc/systemd/system/docker.service.d/animedb-runtime.conf
sudo systemd-tmpfiles --create /etc/tmpfiles.d/animedb-reorganization.conf
stat -c '%U %g %a %n' /run/animedb-reorganization
```

Unit používá host Python standard library a `/usr/bin/docker`; neimportuje app
ani SQLAlchemy, nespouští startup a nemá Python dependencies k instalaci.
`tmpfiles.d` vytvoří adresář jako root:101 `0750` při každém bootu před Dockerem,
nezávisle na aktivitě coordinatoru. Numeric GID `101` musí odpovídat skutečnému
GID non-root edge image a `socket_gid` konfigurace; hostitelské jméno skupiny není
rozhodující. Jiný edge GID vyžaduje konzistentně změnit tyto tři hodnoty a unit
`Group=`. Drop-in Dockeru výslovně řadí start za `systemd-tmpfiles-setup.service`;
instalace nevolá restart Dockeru ani se nedotýká ostatních kontejnerů.
Unit coordinatoru adresář nevytváří ani neodstraňuje. Zastavený coordinator tedy
nebere běžné AnimeDB její edge bind; pouze cleanup API není dostupné a nic se
nespustí samo. Po rebootu vznikne nový adresář před startem edge.
Unix socket coordinator vytváří v něm jako root:101 `0660`.
`Restart=no` zabraňuje tichým retry. Po ručním startu se rozpracované claims
vyhodnotí jako STOP a přesně pojmenované osiřelé worker kontejnery zastaví.

Instalační pořadí je: připravit root-owned konfiguraci/hooky a datové adresáře;
instalovat tmpfiles pravidlo a ordering drop-in; spustit `systemd-tmpfiles --create`
a zkontrolovat root/GID/mód; schválit Compose snapshot; teprve potom jednorázově
recreate edge se socket bindem a nakonec aktivovat coordinator. Při zastaveném
coordinatoru ověřit běžné `/health` a web. Po budoucím samostatně schváleném rebootu
ověřit stejné vlastnictví `/run/animedb-reorganization`, startup edge a běžnou
aplikaci ještě před případnou aktivací reorganizace. Skutečný reboot Nexusu zde
proveden nebyl; lokální test simuluje ztrátu `/run` v alternativním rootu.

Ve třetím Compose override zachovat všechny dosavadní položky a doplnit
`app` pouze RO backup bind na `/backups`; `edge` používá socket bind dodaný
[edge override](../service-edge/compose.edge.yaml). Aplikace nedostává control
socket ani ledger. Edge je potřeba jednorázově re-create, aby převzal nové
statické soubory a socket bind; aktuální edge držící starý checkout je nepřevezme
sám. Před nasazením ověřit sloučenou konfiguraci všech tří souborů. Nikdy
nepoužívat samotný base Compose ani `compose down`.

```bash
docker compose \
  -f /srv/nexus/stacks/anime-db/compose.yaml \
  -f /srv/nexus/projects/anime-db/src/deploy/nexus/service-edge/compose.edge.yaml \
  -f /srv/nexus/stacks/anime-db/compose.v6.yaml config -q
sudo systemctl daemon-reload
sudo systemctl start animedb-reorganization.service
sudo systemctl status animedb-reorganization.service --no-pager
```

Případné boot enable je samostatná provozní volba až po ověřené aktivaci a
trvalé mount integraci; jednorázový ruční mount po rebootu chybí a musí bezpečně
STOPnout. Unit sama mount ani V6 start neprovádí. Neaktivovat novou službu s
historickým nezkontrolovaným obsahem outboxu: všechny čekající prepare/start
žádosti musí správce zrevidovat; jejich expiraci kontroluje worker.

## Schválený snapshot tří Compose vstupů

Helper `reorganization_deployment.py` má dvě explicitní administrátorské fáze.
Neprovádí Docker příkazy, startup aplikace ani změnu DB/NAS. Nejprve root vytvoří
novou generaci z přesných tří aktuálních souborů ve stávajícím pořadí a `.env`:

```bash
sudo python3 /usr/local/libexec/anime-db/reorganization_deployment.py stage \
  --compose "$STACK_DIR/compose.yaml" \
  --compose "$PROJECT_ROOT/src/deploy/nexus/service-edge/compose.edge.yaml" \
  --compose "$STACK_DIR/compose.v6.yaml" \
  --env-file "$STACK_DIR/.env"
```

Výstup obsahuje `generation` a `reviewed_sha256`. Správce zkontroluje všechny
zkopírované YAML, `.env`, pořadí, image a bindy i jejich sloučenou konfiguraci.
Při kontrole použije stejné `--project-directory "$STACK_DIR"`, `-p anime-db`,
explicitní snapshot `.env` a tři snapshot soubory `compose.1.yaml` až
`compose.3.yaml`. Až jako poslední krok schválí přesný SHA manifestu generace:

```bash
sudo python3 /usr/local/libexec/anime-db/reorganization_deployment.py approve \
  --generation "$REVIEWED_GENERATION" --reviewed-sha256 "$REVIEWED_DEPLOYMENT_SHA"
```

`current.json` se atomicky přepne pouze po kontrolách. Generace se needituje;
jakákoli byte změna následně znamená STOP. Změna živého user-owned `src/` sama
nezmění privilegovaný vstup. Původní tři cesty v coordinator JSON slouží jako
přesná provenance; helper dodá schválené cesty a porovná je s touto trojicí.
Starší generace zůstávají k auditu. Při každém dalším release se snapshot
regeneruje z původních zdrojů a znovu schválí; neopravuje se zvlášť druhý stack.

Podporovaný současný kontrakt používá block YAML bez dalších `include`,
`extends`, `env_file`, `label_file`, `configs` nebo `secrets` readerů, YAML anchors/aliases a `.env`
obsahuje jen číselné `ANIMEDB_UID`/`ANIMEDB_GID`. Helper tyto nepřímé živé vstupy
odmítá, místo aby po schválení načítal další měnitelné soubory. Pokud je produkce
opravdu potřebuje, je nutné konkrétní rozšíření předem revidovat. Aplikační
paths a současný Compose build context zůstávají přesně podle původního stacku;
coordinator vždy používá `--no-build` a pevný schválený image.

Hostitelská služba musí pozorovat stejnou mount session jako rootem získaný
runtime capability důkaz. Unit proto nepoužívá `ProtectSystem`, `ProtectHome`,
`PrivateTmp`, `ReadWritePaths` ani filesystem protection přepínače pro kernel/
cgroups, které by vytvořily jiný mount namespace a jiné mount IDs. Nevytváří se
překlad identit ani další proof systém. Místní systemd drop-in nesmí tuto
vlastnost potichu změnit; po změně skutečné identity se vyžaduje nový důkaz.

Host coordinator je již privilegovaný správce Dockeru. Hranici tvoří root-owned
konfigurace, schválený Compose snapshot, přesné pevné příkazy, root-controlled
executable hooky, runtime výluka a jednorázové evidence. App ani edge nedostávají
Docker socket nebo obecný root command endpoint. Unit zachovává
`NoNewPrivileges`, omezení capabilities/address families, privátní umask a
`LockPersonality`. Hooky a jejich předkové musí být bez group/other zápisu a
bez symlinků; samy musejí mít revidovaný malý rozsah vedlejších účinků. Chybějící
hostitelský mount nelze nahrazovat lokálním adresářem ani tvrzením z browseru.

## Přesný transport a restart

App publikuje immutable `<id>.prepare-request.json` a později samostatný
`<source>.start-request.json`. Coordinator před prvním vedlejším účinkem
atomicky vytvoří `<id>.<kind>-claim.json` v root-private adresáři. Jsou-li
spotřebované, nikdy je nespustí znovu. Inbox `<id>.<kind>-status.json` je atomický
odvozený stav `RUNNING/READY/NORMAL/STOP`, fáze, bezpečný důvod a source SHA.
Nemá execution authority. Podrobné výjimky zůstávají v root-private error JSON
nebo ve stávajícím originálním E7 výsledku, nikoli v public error poli.

Příprava používá fixní worker `plan`, trusted snapshot hook, `publish`, poté
vrátí aplikaci pro review. `plan.source_sha256` musí být shodné s publikovaným
source SHA. Start nezávisle opakuje mount/image/writer fence a volá `execute`
pro přesný source; schválení samo nespouští žádnou operaci.

Worker vrací hostiteli jen malý doklad výsledné identity; celý report a seznam
cleanup cest zůstávají v původním inboxu. NORMAL se přijímá pouze při shodě
worker výsledku a originálního
`<source>.result.json` s přesným source/approved SHA a manifest ID. Coordinator
poté stejnými třemi Compose soubory zavolá `up -d --no-deps --no-build
--pull=never app`, ověří HTTP health a teprve pak publikuje `app_ready:true` a
kanonickou result URL. Selhání restartu zachová E7 NORMAL, ale `app_ready:false`
a stav `app_restart_failed`; neprohlašuje dostupnou aplikaci.

Během přípravy publikuje odvozený průběh host; během E7 jej včetně heartbeat
publikuje worker. Cleanup GET/POST je pouze `/cleanup` na Unix socketu, edge
jej mapuje na `/_service/control/cleanup`. GET ukazuje celý živý návrh a exact
proposal SHA; samostatný canonical text zachovává i nanosekundové hodnoty
bez zaokrouhlení čísel v JavaScriptu. POST vyžaduje přesné trusted Origin, cookie a proposal-bound
HMAC CSRF, explicitní actor, exact SHA a `confirm:true`. Cleanup decision body má limit 2 MiB, běžné transportní/config záznamy 64 KiB.
Výběr je pouze subset přesných kandidátů s kontrolou child closure, původní
worker znovu ověřuje skutečný návrh, rozhodnutí i post-state. `directories:[]`
explicitně zachová vše; `decision:"stop"` skončí STOP. Rozhodnutí se atomicky
publikuje pouze jednou, žádné automatické potvrzení při timeoutu.

## STOP, přerušení a ověření nasazení

Nesmazat claims, decisions, snapshot receipts, lokální DB kopie, guard,
execution journal, cleanup evidence, approval ledger ani kontejnery po chybě.
Worker kontejnery nejsou `--rm`; mají deterministické názvy
`anime-db-v6-<kind>-<id>-<verb>`, takže zůstávají dostupné `docker logs`.
Nevyčišťovat je automaticky. Pád nebo timeout workeru vede k zastavení tohoto
pevného kontejneru a STOP; nejistota není důvod spouštět náhradní worker.

Přerušený claim se při příštím startu služby převede na STOP bez opakování.
Při selhání před E7 po odstavení app se pouze zkusí guarded návrat aplikace
pro opravu autorit; guard se nikdy nemaže ani neobchází. E7 STOP zůstává
administrační incident, nikoli automaticky obnovitelná fronta. Aktuální
provozní stav zkontrolovat bez oprav a rozhodnout samostatně. Nové schválení
nemůže zneplatnit spotřebovaný E7 ledger ani guard.

Před produkčním povolením ověřit celý průchod nejprve na izolované kopii DB a
syntetické knihovně včetně skutečného Docker/Compose runtime. Na Nexusu doložit
root ownership, skutečné bindy a RO/RW pohledy, snapshot receipt, writer fence,
jednorázovost při přerušení, cleanup zachovat/vybrat/STOP/stale a návrat přes
health. Doložit stejné před/po DB a NAS baseline pro instalační kontrolu bez
execution; skutečnou změnu knihovny vyžaduje až samostatné explicitní schválení
v browser workflow. Lokální testy tohoto patchu nejsou tento provozní důkaz.

Izolovaný následující test popisuje [návrh labu](../../lab/README.md); plný CIFS
lab není stávající opt-in transportní test ani produkční Nexus verifikace.


## Časové limity pro velkou knihovnu

`app_health_timeout_seconds` v hostitelském JSON má výchozí hodnotu **900 s**
(15 minut), povolené celé hodnoty 1–3600 s. Ve starším JSON může chybět;
použije se výchozí hodnota. Je to čekání na HTTP health po Compose startu,
nikoli prodloužení execution ani nový start E7/E7H. Startup provádí NORMAL
admission při tvorbě aplikace i v lifespan, takže 60 s pro tisíce identit nestačí.

Veřejná evidence rozlišuje `app_restart_state`: `initializing`, `available`,
`failed`, `timed_out`. Během čekání drží host heartbeat; nedostupný HTTP endpoint
je přípustný jen s Docker evidence běžícího nebo právě vytvářeného kontejneru.
Exited/dead/restarting/paused/OOM nebo neověřitelný runtime čekání ukončí.
Dostupnost vyžaduje původní JSON `/health`, poté writer acknowledgement.
Po úspěšném E7 zůstává fyzický výsledek NORMAL i při neúspěšném restartu;
`app_ready:false` a `app_restart_failed` výslovně hlásí nedostupnost. E7 STOP
se nemění a žádný worker ani cleanup se automaticky neopakuje.

Existující `REORGANIZATION_PREVIEW_TIMEOUT_SECONDS` má default **600 s**
pro web preview i read-only inspect manifestu. Původních 120 s může překročit
produkční FS inventura/preflight (audit odhaduje až několik minut). Nginx pro
aplikační requesty čeká **660 s**; při explicitně vyšším nastavení až do 3600 s
musí správce odpovídajícím způsobem sladit proxy limit. Existující explicitní
hodnota prostředí má stále přednost před novým defaultem.

900 s od publikace je stále samostatné schvalovací okno a během review se
neprodlužuje; zahrnuje návrat app pro review i read-only approval kontroly.
Při expiraci je potřeba nový návrh, nikoli obejití admission. Worker má stále
24 h, včetně čekání na živé E7H rozhodnutí. Snapshot hook zůstává 600 s.
Nginx cleanup status/POST 5 s, detail 30 s a health 3 s jsou krátké lokální
transporty, nikoli čekání na dokončení reorganizace; jejich limity se nemění.
