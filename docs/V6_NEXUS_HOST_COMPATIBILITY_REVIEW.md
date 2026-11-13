# V6 – Nexus host compatibility: B1, B5 a socket

Datum: 2026-11-13. Výchozí commit: `db40987da37bef5e74e5fb75c04874457e34bd37`,
větev `main`, při zahájení shodná s `origin/main`. Implementace je pouze v pracovním
stromu. **Nenasazeno, bez commitu a pushe.** Existující nesledovaná práce zachována.

Následná Claude revalidace a její opravy jsou v
[opravném reportu](V6_NEXUS_HOST_COMPATIBILITY_REVALIDATION_FIXES.md).
Níže uvedené původní počty a Git stat zachycují první implementaci.

## A. Přesná oprava B1

Původní kontrola vyžadovala root-owned předky také u runtime appdata. Její prosté
vypnutí by dovolilo root zápisy a Docker bindy přes uživatelsky nahrazené cesty.
Oprava odděluje trusted konfiguraci od runtime dat a řeší oba způsoby použití:

1. `host_io.open_directory/open_file` prochází každou komponentu přes
   descriptor-relative `O_NOFOLLOW`; kontrola samotného posledního basename nestačí.
2. Coordinator připne sedm runtime adresářů. JSON čte přes jejich dirfd;
   publikaci provede nový `/usr/bin/python3 -I` s runtime UID/GID a prázdnými
   supplementary groups. Root v těchto adresářích soubory nevytváří ani nechownuje.
   Temp soubor používá O_EXCL/NOFOLLOW, fsync; replace/RENAME_NOREPLACE i cleanup
   používají stejný dirfd. Cílový symlink se nenásleduje. Záměna předka po kontrole
   tedy nezmění adresář publikace ani privilegia zapisovatele.
3. Kontrola dev/inode před tick/worker startem zůstává dodatečným STOP. Není
   vydávána za ochranu zápisu před TOCTOU.
4. Docker nemůže používat původní path strings pod uživatelskými předky.
   Schválená generace V2 proto odvozuje tři Compose kopie s root-private bind
   kotvami. Manifest zachycuje SHA původních vstupů, SHA odvozených kopií a
   dev/inode původních bind adresářů. Mění se pouze potřebné `source:` řádky.
5. `ensure_bindings` otevře zdroj bezpečně, ověří schválenou identitu a vytvoří
   MS_BIND **z otevřeného deskriptoru**, nikoli opětovným vyhodnocením původní
   cesty. Cíl je pod root-owned předky. Docker následně dostává stabilní kotvu.
   Worker používá obdobné kotvy pod private_dir. Stav/DB v app a workeru tak
   zůstává stejným inode, i při záměně původní cesty po otevření.

Runtime předci mohou být root nebo runtime UID, bez group/other zápisu. Leaf
runtime adresáře zůstávají runtime-owned bez group/other zápisu. `src/` a
`releases/` ani jejich mód `775` se nemění; nejsou trusted executables.

## B. Trust hranice a životnost kotev

| Účel | Umístění / pravidlo |
| --- | --- |
| Coordinator JSON | `/etc/anime-db/reorganization-coordinator.json`, root `0600` |
| Worker JSON | `/etc/anime-db/reorganization-worker.json`, root, bez g/o zápisu |
| Runtime capability evidence | `/var/lib/anime-db/runtime-evidence.json`, root trust |
| Claims, chyby, receipts | `/var/lib/anime-db/coordinator/`, root `0700` |
| Schválené generace | `/var/lib/anime-db/approved-v6/`, root trust |
| Compose bind kotvy | `generation-*/binds/bind-*`, root-private parent |
| Worker bind kotvy | `private_dir/worker-binds/*`, root-private parent |
| SQLite/state/artwork/approval/backups/service | současné appdata, beze změny vlastnictví |

Trusted vstupy dál vyžadují root-owned předky bez g/o zápisu a bez symlinků.
Root ochrana se u konfigurace neuvolňuje. Stage/approve nevolají Docker ani
mount; vytvoření kotev provádí až hostitelský start/Compose ingress po schválení.

Kotvy jsou bind mounty stejných adresářů, nikoli kopie dat, přesun nebo nová
ručně udržovaná konfigurace. Původní tři Compose vstupy a jejich pořadí jsou
provenience, `.env` se kopíruje přesně, project name je stále `anime-db` a
project directory stále původní stack. Krátké volumes, flow volumes,
indentationless sequences a implicitní create-host-path jsou pro V2 odmítnuty;
podporovaný long-form bind musí mít absolutní source/target a
`create_host_path: false`. Komentář nesmí obejít tuto kontrolu.

Kotvy se nevytvářejí znovu, pokud už mají správnou identitu; flock na root parent
fd serializuje souběh. Po stopu coordinatoru zůstávají pro app/edge. Reboot
odstraní mounty; autostart po ověření generace znovu vytvoří potřebné kotvy.
Změna source dev/inode, chybějící source nebo neočekávaný mount znamená STOP.
Úklid starých generací/mountů je samostatná budoucí administrátorská operace.
Původní root-protected RO/RW NAS mountpointy se nepřipojují znovu ani nemění.

Unit potřebuje navíc CAP_SETUID/CAP_SETGID pro child a CAP_SYS_ADMIN pro lokální
bind kotvy. Nevytváří nový mount namespace; identita CIFS runtime proof se
nepřekládá. Coordinator už spravuje root Docker socket; nepřidává se obecný
webový mount/command endpoint. Jeho nové mount cíle určuje pouze trusted stav.

## C. B5: skutečný podklad a oprava

Soubor `B5_anime-db-service-readonly.txt` nebyl v prostředí dostupný. Po explicitním
pokynu byly místo odhadování přečteny přes SSH bez sudo: `systemctl cat/show
anime-db.service`, `systemctl cat animedb-lan-fw.service`, skutečný ExecStart
skript a jím přímo volaný `/usr/local/sbin/animedb-lan-fw`. Nic se nespouštělo.
Lokální záznam: `docs/_local/2026-11-13-nexus-host-compat/evidence/actual-unit.txt`.

Auditovaná unit je oneshot, RemainAfterExit=yes, enabled, bez drop-inů. Dědí
Requires/After pro Docker a `animedb-lan-fw.service` a mount dependency appdata.
ExecStart míří na root-owned skript pod uživatelskými projektovými předky;
ExecStop je base-only `stop -t 30 app`. Skript ověřuje RO CIFS a LAN firewall,
pak startuje pouze base Compose. To vysvětluje návrat k `anime-db:nexus` při bootu.

Připravený `approved-production.conf` mění jen WorkingDirectory na `/` a dvojici
ExecStart/ExecStop existující služby. Firewallové a mount závislosti se dědí.
Nekopíruje ani nepřepisuje celou neověřenou unitu. Nový skript se při budoucím
schváleném nasazení instaluje mimo uživatelské předky do
`/usr/local/libexec/anime-db/autostart.sh`. Zachovává skutečné RO mount a firewall
kontroly i health loop. Firewallový helper ani jeho služba se nemění.

`reorganization_autostart.py` používá tutéž root konfiguraci a approved selection
jako coordinator. Před startem kontroluje všechny tři původní Compose vstupy,
`.env`, aktivní Git HEAD v `src/` a verzovaný `v6-<SHA>` image. Git čte jako data,
nespouští Git ani hooky pod rootem. Digest-only image zatím nemá samostatný
release binding pro nový produkční boot; zde je záměrně vyžadován existující
verzovaný `v6-<SHA>` tag Nexusu. Legacy LAB resolver zůstává podporovaný.

Z generace ověří jméno projektu, image a UID/GID app, původní first-run/guard
entrypoint a pinovaný Nginx s příslušnou socket group. Po vytvoření bind kotev
spustí nejprve edge a potom app, pokaždé `--no-deps --no-build --pull=never`.
Chybějící/pozměněná generace nebo nesoulad releasu znamená STOP bez fallbacku.
Žádný release download, build, pull, druhý stack ani výběr latest/main.

**ExecStop nadále zastavuje pouze app, s timeoutem 30 s.** Stop ověří trusted
selection, ale nevyžaduje neměnný živý checkout; jeho změna nesmí zablokovat
odstavení běžící app. Nginx zůstává dostupný během E7. Návrat app po NORMAL
nadále řídí původní coordinator se shodou originálního výsledku a health checkem.
Guard ani E7/E7H se nemění. Coordinator unit nemá Install/WantedBy a používá
Restart=no; boot jej ani čekající reorganizaci automaticky nespouští.

## D. Socket a GID

Nginx zůstává `101:101`. Socket používá vyhrazenou host skupinu
`animedb-control`; `tmpfiles` vytvoří root:animedb-control adresář `0750`,
coordinator socket `0660`. Edge získá její číselný GID přes `group_add` a
`ANIMEDB_CONTROL_GID` v původním `.env`; stejný GID patří do root `socket_gid`.
Coordinator unit používá Group=animedb-control. Příklad `2001` není rezervace:
při budoucím nasazení vybrat skutečně volnou vyhrazenou skupinu a ověřit členy.
Nový produkční config odmítne GID 101, app GID a nesoulad group control_dir.
**Systémová `_ssh`, SSH služby ani jejich soubory se nemění.**

LAB helper instaluje sdílený host_io a drží odpovídající capabilities.
Jeho socket má samostatný CONTROL_GID, přepis ANIMEDB_LAB_CONTROL_GID; Nginx
stejnou doplňkovou group. Legacy root-controlled stack/approved-v6 layout zůstává
kompatibilní. LAB ani CIFS nebyly spouštěny/restartovány.

## E. Validace

- Hlavní cílená sada: **97 passed** (host paths, autostart, deployment, coordinator),
  včetně skutečného Unix HTTP socketu. Záměny předků/target symlinků, SHA approval,
  pořadí tří vstupů, chybné image, boot/stop a E7 → NORMAL orchestrace.
- Root hranice v izolovaném existujícím LAB image: **6 passed**. Skutečný root
  trust, runtime publication pod jiným UID/GID bez supplementary groups,
  proces bez socket GID odmítnut, fd-based mount při záměně source, odvozená
  generace s původními SHA a load_config se zachovanými user-owned předky.
- Původní startup-guard regression: **1 passed**, ostatní testy souboru záměrně
  deselected. Známý guard odmítne startup před migrací/artwork writes.
- compileall app/tests a host modulů, bash -n obou shell helperů,
  git diff --check a kontrola relativních docs odkazů: PASS.
- Full pytest, úplný browser/Docker edge matrix, běžící LAB/CIFS a skutečný Nexus
  boot/deployment záměrně neprovedeny. Šablony ani jejich contract se neměnily;
  Jinja load nebyl potřeba.

První sandbox běh odmítl bind Unix socketu; opakování mimo sandbox prošlo.
Hostitelské sudo -n nebylo dostupné, proto root testy běžely ve zvláštním
kontejneru bez sítě, pullu a host RW bindů. Jeho Python je v /usr/local/bin;
Nexus cestu /usr/bin/python3 poskytl pouze kontejnerový dočasný filesystem.
Předchozí selhání (včetně nalezených YAML/stop/Docker path chyb) byla opravena
před uvedenými závěrečnými výsledky.

## F. Soubory změny

Upravené:

- deploy/nexus/reorganization_coordinator.py
- deploy/nexus/reorganization_deployment.py
- deploy/nexus/config/reorganization-coordinator.json.example
- deploy/nexus/.env.example
- deploy/nexus/service-edge/compose.edge.yaml
- deploy/nexus/systemd/animedb-reorganization.service
- deploy/nexus/tmpfiles.d/animedb-reorganization.conf
- deploy/nexus/docs/V6_COORDINATOR.md
- deploy/nexus/docs/RELEASE_UPDATE.md
- deploy/nexus/docs/ARCHITECTURE.md
- deploy/lab/lab.sh
- deploy/lab/compose.edge.yaml
- deploy/lab/README.md
- tests/test_reorganization_coordinator.py
- tests/test_reorganization_deployment.py

Nové:

- deploy/nexus/host_io.py
- deploy/nexus/reorganization_autostart.py
- deploy/nexus/scripts/autostart.sh
- deploy/nexus/systemd/anime-db.service.d/approved-production.conf
- tests/test_nexus_host_paths.py
- tests/test_nexus_host_privileges.py
- tests/test_nexus_autostart.py
- docs/V6_NEXUS_HOST_COMPATIBILITY_REVIEW.md

Git diff --stat nezahrnuje tyto nové dosud nesledované soubory. Původní nesledované
soubory uživatele nejsou součástí změny a nebyly přepisovány.

## G. Zachování produkce a zbývající integrace

Schema, produkční data/anime.db, hostitelská produkční DB, média, původní RO NAS
mount, SQLite rozhodnutí/metadata, E7/E7H a CIFS checkpointy beze změny. Na Nexusu
se četla pouze systemd konfigurace a dva shell skripty. Žádný jeho script se
nespustil; žádný systemctl start/stop/restart/daemon-reload, Docker restart,
release aktivace, chown/chmod, reboot, scan, migrace ani NAS write.

Existující Docker image, snapshoty a Restic se neměnily. Lokální testovací
kontejnery používaly --pull=never a read-only repo. Skutečné snapshot/writer
hooky a schválený RW mount capability test zůstávají **samostatné zbývající
produkční integrace**; jejich náhražky se neimplementovaly. Reorganizace ani
produkční aktivace tímto reportem nejsou schválené.

## H. Nezávislá revalidace Claudem

1. Začít git status -sb, rev-parse HEAD a diff --check. Ověřit zachování původní
   nesledované práce. Diff proti db40987 nesmí obsahovat app business/engine/schema
   změny, produkční DB ani NAS soubory. Zkontrolovat všech osm nových souborů,
   které prostý git diff nezobrazuje.
2. Přečíst host_io.py a oba host helpery: každá komponenta O_NOFOLLOW, runtime
   write child bez root/supplementary groups, fsync/rename relativní k dirfd.
   U root bindů sledovat skutečný fd source mountu, root parent cíle a identitu;
   nikde nesmí zbýt Docker source pod uživatelským předkem v nové produkční větvi.
3. Porovnat skutečnou unitu s výše uloženou SSH evidencí a drop-inem. Ověřit
   zděděný firewall a mount dependencies, app-only ExecStop a root-owned ingress
   mimo /srv/nexus. Žádnou změnu unity na hostiteli v rámci revalidace neprovádět.
4. Spustit pouze cílenou sadu v repozitáři (Unix socket potřebuje povolený lokální
   socket, nikoli přístup k Nexusu):

   ```bash
   PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider \
     tests/test_nexus_host_paths.py tests/test_nexus_autostart.py \
     tests/test_reorganization_deployment.py tests/test_reorganization_coordinator.py \
     -q --tb=short
   PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider \
     tests/test_v6_p2c_execution_guard.py \
     -k startup_refuses_known_guard_before_migration_or_artwork -q --tb=short
   ```

5. Pro skutečnou root hranici použít izolovaný disposable Linux s Python 3.12 a
   pytest, root/CAP_SETUID/CAP_SETGID/CAP_CHOWN/CAP_DAC_OVERRIDE/CAP_FOWNER/CAP_SYS_ADMIN,
   povoleným bind mountem a dočasným /run. Root test soubor nesmí být jen skipped.
   V tomto prostředí lze zopakovat přesný lokální Docker běh níže; image už musí
   existovat, nic nebuildovat ani nepullovat:

   ```bash
   docker run --rm --pull=never --network=none --read-only --user 0:0 \
     --cap-drop=ALL --cap-add=CHOWN --cap-add=DAC_OVERRIDE --cap-add=FOWNER \
     --cap-add=SETUID --cap-add=SETGID --cap-add=SYS_ADMIN \
     --security-opt=no-new-privileges --security-opt=apparmor=unconfined \
     --tmpfs /run:rw,nosuid,nodev,size=16m \
     --tmpfs /tmp:rw,nosuid,nodev,size=16m \
     --tmpfs /usr/bin:rw,nosuid,nodev,size=1m,mode=755 \
     --mount "type=bind,src=$PWD,dst=/workspace,readonly" \
     --env PYTHONPATH=/workspace:/workspace/.venv/lib/python3.12/site-packages \
     --env PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 --env PYTHONDONTWRITEBYTECODE=1 \
     --workdir /workspace --entrypoint python anime-db-lab:v6-cbcd795e07bb \
     -c 'import os,sys; os.symlink(sys.executable,"/usr/bin/python3"); import pytest; sys.exit(pytest.main(["-c","/dev/null","-p","no:cacheprovider","tests/test_nexus_host_privileges.py","-q","--tb=short"]))'
   ```

6. Samostatně posoudit boot chybějící/změněné generace, rozdílné src/HEAD/image,
   každý ze tří vstupů a env; stop po změně src musí dál zastavit jen app. Při
   přerušeném E7 nesmí test pozorovat nový execute/retry. Po NORMAL musí zůstat
   původní ověření originálního výsledku a health před app_ready.
7. Zkontrolovat GID v env/config/tmpfiles/unit/group_add; 101 patří jen Nginxu,
   nikoli ovládacímu socketu. Skutečné členství budoucí vyhrazené host group
   ověřit před jakýmkoli nasazením; nezasahovat do _ssh.
8. compileall, bash -n a diff --check; aktuální git diff --stat a status.
   Žádný commit/push, host deployment, restart či reboot. Po reportu STOP.

Závěrečný Git přehled: `main...origin/main`, HEAD stále `db40987da37bef5e74e5fb75c04874457e34bd37`.
`git diff --stat`: **15 files changed, 445 insertions(+), 124 deletions(-)**.
Navíc osm nových souborů této změny vyjmenovaných v F. Vše zůstává unstaged;
žádný commit/push. Původní nesledované soubory z úvodního git status nadále
existují, včetně installation_state/nexus_db, N5B/N5C dokumentů, dřívějších V6
review dokumentů, docs/superpowers a odpovídajících testů/probe.
