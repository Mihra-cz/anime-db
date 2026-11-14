# AnimeDB – provozní návod pro aktualizaci existující instalace

**Účel:** opakovatelná aktualizace běžící aplikace ze schváleného Git commitu, se zachováním databáze, médií a provozních dat.

**Umístění v repozitáři:** `deploy/nexus/docs/RELEASE_UPDATE.md`

> Tento soubor je záměrně bez uživatelských jmen, hostname, IP adres, přístupových údajů, URL konkrétního repozitáře a pevných verzí. Hodnoty konkrétní instalace se zjišťují až na serveru. Nikdy je necommitovat do tohoto návodu.

## 1. Adresářová struktura a odpovědnosti

| Proměnná / cesta | Účel |
| --- | --- |
| `$PROJECT_ROOT/src/` | Aktivní zdrojový checkout |
| `$PROJECT_ROOT/releases/` | Archiv předchozích checkoutů a příprava další verze |
| `$STACK_DIR/compose.yaml` | Základní Compose |
| `$PROJECT_ROOT/src/deploy/nexus/service-edge/compose.edge.yaml` | Compose pro stávající webový edge |
| `$STACK_DIR/compose.v6.yaml` | Lokální produkční override mimo Git: image, vypnutí build, approval prostředí a bindy |
| `$APPDATA_DIR/db/` | Produkční SQLite a případná lokální rozhodnutí Archive Review |
| `$APPDATA_DIR/state/` | Provozní stav a lock |
| `$APPDATA_DIR/artwork/` | Artwork |
| `$APPDATA_DIR/approval/` | Evidence schvalování |
| `$APPDATA_DIR/service/` | Stav pro webový edge |
| `$MEDIA_DIR` | Knihovna na NAS; aplikace ji používá read-only |

**Data zůstávají mimo `src/` a `releases/`.** Aktualizace mění zdrojový checkout, tag Docker image a referenci v produkčním Compose override. Nepřesouvá SQLite, artwork, stav ani soubory na NAS.

Stávající konfigurace používá Compose **v přesném pořadí**: základní → edge → produkční override. Pozdější soubor může měnit hodnoty z předchozích souborů. V legacy režimu se aktualizuje jen app. V režimu approved se používá bezpečný start app+edge; nová generace mění bind sources i u edge a může jej recreatovat.

## 2. Před každou aktualizací

Aktualizaci nezačínat během přípravy manifestu, aktivního E7/E7H ani při
nevyhodnoceném STOP. Neodstavovat coordinator s běžícím workerem jako součást
běžného release. Případnou změnu jeho root-owned scriptů a helperu instalovat
z konkrétního revidovaného releasu cíleně; živé spouštění Pythonu z user-owned
checkoutu privilegovaný coordinator nepoužívá. Stávající připravené manifesty
se nesmějí převádět na novou verzi automaticky.


Předem určit a schválit **plný SHA** cílového commitu. Nespouštět update na plovoucí `main`.

### Předpoklady

- Všechny bloky se spouštějí **na serveru v jedné interaktivní relaci Bash** (nikoli `sh`), postupně: proměnné → socketový předkrok → A → B → C → D. Blok E je jen pro návrat po selhání.
- Relace běží pod běžným vývojovým účtem. `src/` i `releases/` zůstávají pod tímto účtem; současný mód `src/` `775` není důvodem pro `chown`. Git a příprava releasu běží bez sudo. Docker, změna root-owned override a případné přejmenování adresářů v root-owned projektovém kořeni používají jen jednotlivé cílené `sudo` příkazy. Celou relaci nespouštět přes `sudo -i`.
- `$PROJECT_ROOT/src` je Git checkout s remote `origin`, jehož aktivní commit je na tomto remote dostupný. Platí to od V6 release; nejstarší archiv z první instalace je export bez `.git`. Pokud `src/` není Git checkout, tento návod nelze použít: STOP.
- Produkční override obsahuje právě jeden řádek `image: $IMAGE_REPO:v6-<prvních 7 znaků aktivního SHA>`. Jinak se blok C zastaví.
- Bloky A–E běží v závorkách se `set -euo pipefail`. Chyba ukončí jen daný blok, relace i proměnné zůstanou. Další blok spustit jen po výpisu `OK` z předchozího.
- Za uzavírací `)` nepřidávat `&&` ani `||`, Bash by tím uvnitř bloku vypnul `set -e`.

V této relaci nejprve přiřadit hodnoty proměnným níže (`PROJECT_ROOT=…` atd.) podle **aktuálně běžící instalace** a teprve potom vložit následující blok. Hlášení `Nastav …` znamená STOP. Jde o názvy proměnných, nikoliv o hodnoty, které se mají zapisovat do repozitáře:

```bash
# Vyplnit jen v aktuální terminálové relaci.
: "${PROJECT_ROOT:?Nastav projektovy koren}"
: "${STACK_DIR:?Nastav adresar aktivniho Compose}"
: "${APPDATA_DIR:?Nastav adresar provoznich dat}"
: "${DB_FILE:?Nastav cestu k existujici produkcni databazi}"
: "${MEDIA_DIR:?Nastav NAS mount}"
: "${APP_SERVICE:?Nastav jmeno Compose sluzby aplikace}"
: "${IMAGE_REPO:?Nastav nazev Docker image bez tagu}"
: "${NEW_SHA:?Nastav schvaleny plny Git commit SHA}"
: "${DEPLOY_MODE:?Nastav approved pro host helper nebo legacy pro puvodni instalaci}"
case "$DEPLOY_MODE" in approved|legacy) ;; *) echo "STOP: neplatny DEPLOY_MODE"; exit 1;; esac

SRC="$PROJECT_ROOT/src"
RELEASES="$PROJECT_ROOT/releases"
BASE_COMPOSE="$STACK_DIR/compose.yaml"
EDGE_COMPOSE="$SRC/deploy/nexus/service-edge/compose.edge.yaml"
OVERRIDE="$STACK_DIR/compose.v6.yaml"

OLD_SHA="$(git -C "$SRC" rev-parse HEAD)"
SHORT_NEW="${NEW_SHA:0:7}"
SHORT_OLD="${OLD_SHA:0:7}"
NEW_TAG="v6-$SHORT_NEW"
OLD_TAG="v6-$SHORT_OLD"
STAGE="$RELEASES/$NEW_TAG"
ARCHIVE="$RELEASES/$OLD_TAG"
NEW_IMAGE="$IMAGE_REPO:$NEW_TAG"
```

Režim `approved` je povinný, pokud instalace používá root konfiguraci nebo B5 drop-in;
chybějící JSON není důvod přepnout na legacy. Režim určete z inventury předem,
nikoli podle neprivilegovaného `test -f` v root `0700` adresáři.

Žádné heslo ani token nevkládat přímo do příkazů, dokumentace nebo Git remote URL. Použít existující správně nastavené autentizační prostředky.

### Socketový předkrok – před A i před C1

Jde o jednorázovou **schválenou instalační změnu**, ne součást read-only A.
Při opakované aktualizaci ověřit existující stav; skupinu ani hodnotu neodhadovat.
Nepoužívat GID 101 (`_ssh`) ani GID app. Existující lidské účty do skupiny nepatří.

1. Založit chybějící skupinu a ověřit její skutečný GID a členství:

   ```bash
   (
   set -euo pipefail
   if ! getent group animedb-control >/dev/null; then
     sudo groupadd --system animedb-control
   fi
   getent group animedb-control
   )
   CONTROL_GID="$(getent group animedb-control | cut -d: -f3)"
   export CONTROL_GID
   (
   set -euo pipefail
   [[ "$CONTROL_GID" =~ ^[1-9][0-9]*$ ]]
   test "$CONTROL_GID" != 101
   test "$(getent group | awk -F: -v gid="$CONTROL_GID" '$3 == gid {print $1}')" = animedb-control
   test -z "$(getent group animedb-control | cut -d: -f4)"
   test -z "$(getent passwd | awk -F: -v gid="$CONTROL_GID" '$4 == gid {print $1}')"
   echo "SOCKET GROUP: OK"
   )
   ```

2. Do původního `$STACK_DIR/.env` doplnit přesně jednu číselnou hodnotu
   `ANIMEDB_CONTROL_GID`. Zachovat UID/GID řádky, vlastníka a mód; neobnovovat
   soubor ze starého releasu. Následující cílený updater používá trusted helper,
   per-component NOFOLLOW, připnutý parent fd a atomickou publikaci. Při user-owned
   stacku před zápisem trvale přejde na vlastníka souboru. Nepoužívá root zápis
   přes znovu vyhodnocenou cestu. Helpery podle [instalačního pořadí](V6_COORDINATOR.md#instalace-b5-a-povinného-gid)
   musí být nejprve nainstalované; samotná instalace helperů nespouští služby.

   ```bash
   sudo /usr/bin/python3 -I - "$STACK_DIR" "$CONTROL_GID" <<'PY'
   import os, secrets, stat, sys
   sys.path.insert(0, '/usr/local/libexec/anime-db')
   from host_io import open_directory
   from reorganization_deployment import _check_inputs
   parent = open_directory(sys.argv[1])
   gid = int(sys.argv[2])
   if gid <= 0 or gid == 101: raise SystemExit('STOP: dedicated GID required')
   fd = os.open('.env', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
   try:
       before = os.fstat(fd)
       directory = os.fstat(parent)
       caller = int(os.environ.get('SUDO_UID', '0'))
       if (directory.st_uid not in (0, caller) or directory.st_mode & 0o022
           or not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
           or before.st_size > 65536 or before.st_uid != directory.st_uid):
           raise SystemExit('STOP: unexpected stack/env ownership or type; no ownership repair')
       raw = os.read(fd, 65537)
   finally: os.close(fd)
   _check_inputs([], raw)
   lines = raw.decode().splitlines()
   if sum(line.startswith('ANIMEDB_CONTROL_GID=') for line in lines) > 1:
       raise SystemExit('STOP: duplicate CONTROL_GID')
   app_gid = [line.split('=', 1)[1] for line in lines if line.startswith('ANIMEDB_GID=')]
   if app_gid == [str(gid)]: raise SystemExit('STOP: control GID equals app GID')
   updated = ('\n'.join(line for line in lines if not line.startswith('ANIMEDB_CONTROL_GID='))
              + '\nANIMEDB_CONTROL_GID=' + str(gid) + '\n').encode()
   if updated == raw: raise SystemExit(0)
   if before.st_uid != 0:
       os.setgroups([]); os.setgid(before.st_gid); os.setuid(before.st_uid)
   temp = '.env.' + secrets.token_hex(12)
   out = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
   try:
       os.fchown(out, before.st_uid, before.st_gid)
       os.fchmod(out, stat.S_IMODE(before.st_mode))
       with os.fdopen(out, 'wb', closefd=False) as stream:
           stream.write(updated); stream.flush(); os.fsync(out)
       current = os.stat('.env', dir_fd=parent, follow_symlinks=False)
       if (current.st_ino, current.st_size, current.st_mtime_ns) != (before.st_ino, before.st_size, before.st_mtime_ns):
           raise SystemExit('STOP: env changed concurrently')
       os.replace(temp, '.env', src_dir_fd=parent, dst_dir_fd=parent); os.fsync(parent)
   finally:
       os.close(out)
       try: os.unlink(temp, dir_fd=parent)
       except FileNotFoundError: pass
       os.close(parent)
   PY
   ```

   Pokud stack/env mají jiného vlastníka nebo nebezpečný mód, STOP; tento postup
   jejich vlastnictví ani strukturu neopravuje. Po změně `.env` je dosavadní V2
   approval úmyslně zastaralý: před jakýmkoli startem udělat nové stage/review/approve.
   Diagnostické `config/ps/logs` nad původními vstupy nic nespouštějí.

3. Před prvním Compose příkazem zkontrolovat konzistenci: jméno skupiny v
   `Group=animedb-control` a tmpfiles, skutečný GID v `.env` a root JSON
   `socket_gid`, v Compose Nginx `101:101` plus `group_add` s tímto GID. V approved
   režimu musí být připraven root JSON podle instalačního pořadí a platí:

   ```bash
   (
   set -euo pipefail
   if test "$DEPLOY_MODE" = approved; then
     sudo /usr/bin/python3 -I - "$CONTROL_GID" <<'PY'
   import json, sys
   from pathlib import Path
   sys.path.insert(0, '/usr/local/libexec/anime-db')
   from reorganization_coordinator import secure_root_path, read_json
   p = Path('/etc/anime-db/reorganization-coordinator.json')
   secure_root_path(p); c = read_json(p)
   if c['socket_gid'] != int(sys.argv[1]) or c['gid'] == c['socket_gid']:
       raise SystemExit('STOP: coordinator socket GID differs')
   if Path(c['control_dir']).stat().st_gid != c['socket_gid']:
       raise SystemExit('STOP: tmpfiles control directory group differs')
   PY
     test "$(systemctl show animedb-reorganization.service -p Group --value)" = animedb-control
     test "$(stat -c %g /run/animedb-reorganization)" = "$CONTROL_GID"
     test "$(stat -c %a /run/animedb-reorganization)" = 750
   fi
   # První Compose operace je pouze render; nový edge ve STAGE ověřit
   # po B a před C1 se stejným explicitním env a project contextem. Žádný fallback na samotný base při chybě interpolace.
   sudo -- /usr/bin/docker compose --project-directory "$STACK_DIR" --env-file "$STACK_DIR/.env" -p anime-db \
     -f "$BASE_COMPOSE" -f "$EDGE_COMPOSE" -f "$OVERRIDE" config -q
   )
   ```

V legacy režimu ověřit Group/tmpfiles/JSON v připravených instalačních podkladech;
pokud socket bind už používá `/run/animedb-reorganization`, skupina a tmpfiles musí
být nainstalované před jeho startem. Všechny příkazy A–E používají explicitní
`--env-file "$STACK_DIR/.env"`, `--project-directory` a project `anime-db`.
Chybějící proměnná v novém edge musí skončit chybou už při `config -q`.

### A. Krátká kontrola skutečného stavu (read-only)

```bash
(
set -euo pipefail

test -n "$OLD_SHA"
test -d "$SRC"
test -d "$RELEASES"
test -f "$BASE_COMPOSE"
test -f "$EDGE_COMPOSE"
test -f "$OVERRIDE"
test -f "$DB_FILE"
test -d "$MEDIA_DIR"
test ! -e "$STAGE"
test ! -e "$ARCHIVE"
test -z "$(git -C "$SRC" status --porcelain)"

sudo -- /usr/bin/docker compose --project-directory "$STACK_DIR" --env-file "$STACK_DIR/.env" -p anime-db -f "$BASE_COMPOSE" -f "$EDGE_COMPOSE" -f "$OVERRIDE" config -q
sudo -- /usr/bin/docker compose --project-directory "$STACK_DIR" --env-file "$STACK_DIR/.env" -p anime-db -f "$BASE_COMPOSE" -f "$EDGE_COMPOSE" -f "$OVERRIDE" ps

# K ověření image a bindů použij skutečné ID běžící služby.
APP_CONTAINER="$(sudo -- /usr/bin/docker compose --project-directory "$STACK_DIR" --env-file "$STACK_DIR/.env" -p anime-db -f "$BASE_COMPOSE" -f "$EDGE_COMPOSE" -f "$OVERRIDE" ps -q "$APP_SERVICE")"
test -n "$APP_CONTAINER"
sudo -- /usr/bin/docker inspect --format '{{.Config.Image}} | {{.State.Status}}' "$APP_CONTAINER"
sudo -- /usr/bin/docker inspect --format '{{range .Mounts}}{{println .Destination .RW}}{{end}}' "$APP_CONTAINER"
echo "BLOK A: OK"
)
```

Ověřit, že NAS bind aplikace je read-only, existující databáze a datové bindy jsou na správných místech a aktivní release odpovídá očekávání. **Při nesouladu zastavit**. Nepřepisovat konfiguraci podle historického README.

### B. Připravit zdroje a nový image, zatím bez přepnutí aplikace

```bash
(
set -euo pipefail

# Zdroj repozitáře se převezme z existujícího checkoutu.
# Nezapisovat do návodu konkrétní GitHub URL.
REMOTE="$(git -C "$SRC" remote get-url origin)"
git clone --no-checkout "$REMOTE" "$STAGE"
git -C "$STAGE" checkout --detach "$NEW_SHA"

# Nový checkout vznikl pod stejným běžným účtem jako src/ a releases/.
# Nepoužívat rekurzivní chown zdrojů, archivů ani appdata.
test "$(git -C "$STAGE" rev-parse HEAD)" = "$NEW_SHA"
test -f "$STAGE/Dockerfile"

# Vypíše změny, které tento postup do běžícího stacku nepřenese.
git -C "$STAGE" diff --stat "$OLD_SHA" "$NEW_SHA" -- deploy/nexus/compose.yaml deploy/nexus/service-edge

# Nový edge musí již nyní přijmout připravené .env (před C1).
sudo -- /usr/bin/docker compose --project-directory "$STACK_DIR" --env-file "$STACK_DIR/.env" -p anime-db \
  -f "$BASE_COMPOSE" -f "$STAGE/deploy/nexus/service-edge/compose.edge.yaml" -f "$OVERRIDE" config -q

# UID/GID je nutno převzít ze skutečně běžícího kontejneru,
# nikoli odhadovat nebo zapisovat napevno.
APP_CONTAINER="$(sudo -- /usr/bin/docker compose --project-directory "$STACK_DIR" --env-file "$STACK_DIR/.env" -p anime-db -f "$BASE_COMPOSE" -f "$EDGE_COMPOSE" -f "$OVERRIDE" ps -q "$APP_SERVICE")"
test -n "$APP_CONTAINER"
APP_USER="$(sudo -- /usr/bin/docker inspect --format '{{.Config.User}}' "$APP_CONTAINER")"
case "$APP_USER" in
  *:*) APP_UID="${APP_USER%%:*}"; APP_GID="${APP_USER##*:}" ;;
  *) echo 'STOP: UID/GID neni jednoznacne'; exit 1 ;;
esac
[[ "$APP_UID" =~ ^[0-9]+$ && "$APP_GID" =~ ^[0-9]+$ ]]

# Posledni kontrola schvaleneho checkoutu bezprostredne pred buildem.
test -d "$STAGE"
test -d "$STAGE/.git"
test "$(git -C "$STAGE" rev-parse --is-inside-work-tree)" = true
[[ "$NEW_SHA" =~ ^[0-9a-f]{40}$ ]]
test "$(git -C "$STAGE" rev-parse HEAD)" = "$NEW_SHA"
STAGE_STATUS="$(git -C "$STAGE" status --porcelain --untracked-files=all --ignored)"
if test -n "$STAGE_STATUS"; then
  printf '%s\n' "$STAGE_STATUS"
  echo 'STOP: STAGE obsahuje zmeny nebo nesledovane/ignorovane soubory'
  exit 1
fi

sudo -- /usr/bin/docker build --build-arg "APP_UID=$APP_UID" \
  --build-arg "APP_GID=$APP_GID" \
  -t "$NEW_IMAGE" "$STAGE"
sudo -- /usr/bin/docker image inspect "$NEW_IMAGE" --format '{{.Id}}'
echo "BLOK B: OK"
)
```

Když cílový commit není dostupný, **nepřecházet automaticky na nejnovější `main`**. Když výpis `git diff --stat` není prázdný, release mění základní Compose nebo edge: zastavit a posoudit samostatně, protože `$STACK_DIR/compose.yaml` ani běžící edge tyto změny nepřevezmou. Nedokončený `$STAGE` po selhání vyhodnotit a teprve potom řízeně odstranit, jinak se blok A zastaví.

Poslední kontrola před buildem vyžaduje nový obyčejný Git checkout přesného
schváleného plného SHA, čistý pracovní strom i index. Odmítá všechny nesledované
soubory včetně Git-ignorovaných: `.gitignore` neurčuje Docker build context.
Jakýkoli nesoulad nebo chyba Git příkazu ukončí blok B před buildem díky
`set -euo pipefail`; kontrola žádné soubory neodstraňuje ani neopravuje.

### C. Aktivace: archiv zdrojů, override a bezpečný start

Před C1 ověřit idle a odstavit pouze neaktivní coordinator, je-li spuštěný.
Jeho starý config je v paměti; samotná změna root JSON jej neaktualizuje.
Není dovoleno pokračovat s aktivním/pending E7/E7H nebo nevyhodnoceným STOP.

```bash
(
set -euo pipefail

test "$(git -C "$SRC" rev-parse HEAD)" = "$OLD_SHA"
test "$(git -C "$STAGE" rev-parse HEAD)" = "$NEW_SHA"
test ! -e "$ARCHIVE"

# Ověřit jediný očekávaný image tag v produkčním override.
# Pokud override neobsahuje právě starý image, STOP.
python3 - "$OVERRIDE" "$IMAGE_REPO:$OLD_TAG" "$NEW_IMAGE" <<'PY'
from pathlib import Path
import sys
p = Path(sys.argv[1])
old, new = sys.argv[2:]
data = p.read_text(encoding="utf-8")
old_line, new_line = f"image: {old}", f"image: {new}"
if data.count(old_line) != 1:
    raise SystemExit("STOP: produkcni override nema presne ocekavany image tag")
print("Image tag pripraven pro zmenu:", old, "->", new)
PY

# Stary zdrojovy checkout zustane archivovany.
sudo mv -T "$SRC" "$ARCHIVE"
sudo mv -T "$STAGE" "$SRC"
test "$(git -C "$SRC" rev-parse HEAD)" = "$NEW_SHA"

sudo python3 - "$OVERRIDE" "$IMAGE_REPO:$OLD_TAG" "$NEW_IMAGE" <<'PY'
from pathlib import Path
import os
import sys
import tempfile
p = Path(sys.argv[1])
old, new = sys.argv[2:]
data = p.read_text(encoding="utf-8")
old_line, new_line = f"image: {old}", f"image: {new}"
if data.count(old_line) != 1:
    raise SystemExit("STOP: zmenil se obsah produkcniho override")
updated = data.replace(old_line, new_line)
fd, name = tempfile.mkstemp(prefix=p.name + ".", dir=p.parent)
try:
    os.fchmod(fd, p.stat().st_mode & 0o777)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(updated)
    os.replace(name, p)
finally:
    if os.path.exists(name):
        os.unlink(name)
PY

echo "BLOK C1: zdroje a override pripraveny; aplikace jeste nebyla recreatovana"
)
```

Coordinator během C2 zůstává odstavený; po release se sám nespouští.

V režimu `approved` před následujícím C2 provést níže uvedené
[schválení snapshotu](#schválený-vstup-koordinátoru) a v této stejné relaci
nastavit `REVIEWED_GENERATION` na právě schválenou generaci. Před aktivací také
cíleně aktualizovat root-owned coordinator image na tentýž `NEW_IMAGE` a
revidovat související změny jeho konfigurace. Tím se nevytváří druhý ručně
udržovaný stack; generace se vždy vyrábí z původních tří vstupů.

```bash
(
set -euo pipefail
EDGE_COMPOSE="$SRC/deploy/nexus/service-edge/compose.edge.yaml"
test "$APP_SERVICE" = app
if test "$DEPLOY_MODE" = approved; then
  # load_boot resolves and verifies release/source_sha256, then start pins all
  # bindings before invoking Compose. No raw up over generation-*/binds.
  sudo -- /usr/bin/python3 /usr/local/libexec/anime-db/reorganization_autostart.py start
else
  sudo -- /usr/bin/docker compose --project-directory "$STACK_DIR" --env-file "$STACK_DIR/.env" -p anime-db \
    -f "$BASE_COMPOSE" -f "$EDGE_COMPOSE" -f "$OVERRIDE" config -q
  test "$(sudo -- /usr/bin/docker compose --project-directory "$STACK_DIR" --env-file "$STACK_DIR/.env" -p anime-db \
    -f "$BASE_COMPOSE" -f "$EDGE_COMPOSE" -f "$OVERRIDE" config --images "$APP_SERVICE")" = "$NEW_IMAGE"
  sudo -- /usr/bin/docker compose --project-directory "$STACK_DIR" --env-file "$STACK_DIR/.env" -p anime-db \
    -f "$BASE_COMPOSE" -f "$EDGE_COMPOSE" -f "$OVERRIDE" up -d --no-deps --no-build --pull=never "$APP_SERVICE"
fi
echo "BLOK C: OK"
)
```

V režimu `approved` start používá právě schválený `current.json`; samotný
`REVIEWED_GENERATION` v shellu jej nevybírá. Při jakémkoli selhání C2 STOP,
žádný raw Compose ani legacy fallback. Pokud app dosud běží, samotná příprava
kotev ji neodstavuje; při pozdějším selhání zachovat data a použít řízený E.
Nové bind sources generace znamenají recreaci edge při tomto startu či bootu.

Pozor na legacy edge: běžící edge kontejner drží bind na adresář, který existoval při jeho startu. Po přesunu tedy dál čte soubory z archivovaného checkoutu, nikoli z nového `src/`, a změny v `deploy/nexus/service-edge/` se projeví až při samostatně schválené recreaci edge. Edge kontejner se zbytečně nerecreatuje. Staré image ani archivované zdroje neodstraňovat. **Nepoužívat** `docker compose down`, `--remove-orphans` nebo `docker system prune`.

### D. Ověření po aktualizaci

```bash
(
set -euo pipefail

# -a: i kontejner zastavený po neúspěšném startu musí ukázat stav a logy.
APP_CONTAINER="$(sudo -- /usr/bin/docker compose --project-directory "$STACK_DIR" --env-file "$STACK_DIR/.env" -p anime-db -f "$BASE_COMPOSE" -f "$EDGE_COMPOSE" -f "$OVERRIDE" ps -aq "$APP_SERVICE")"
test -n "$APP_CONTAINER"
sudo -- /usr/bin/docker inspect --format '{{.Config.Image}} | {{.State.Status}} | {{.RestartCount}}' "$APP_CONTAINER"
sudo -- /usr/bin/docker logs --tail 35 "$APP_CONTAINER"
sudo -- /usr/bin/docker inspect --format '{{range .Mounts}}{{println .Destination .RW}}{{end}}' "$APP_CONTAINER"
test "$(git -C "$SRC" rev-parse HEAD)" = "$NEW_SHA"
test "$(sudo -- /usr/bin/docker inspect --format '{{.Config.Image}} {{.State.Status}}' "$APP_CONTAINER")" = "$NEW_IMAGE running"
echo "BLOK D: OK"
)
```

Funkčnost webu ověřit přes **skutečnou interní adresu** z místní konfigurace, nikoliv z napevno napsané IP v repozitáři. U aktualizace Archive Review navíc ověřit `/reorganization`, přesměrování `/reorganization/archive-review` a vytvořit jeden aktuální read-only preview. Žádný přesun médií.

Při selhání aktualizace neopakovat instalační provision, migraci ani reorganizační worker. Identifikovat chybu a vrátit původní image a zdroje podle bloku E. Produkční data zůstávají nedotčená.

### E. Návrat k předchozí verzi

Blok předpokládá dokončený C1 a stejnou relaci s původními proměnnými.
Lze jej použít po neúspěšném C2, pokud kontroly níže potvrdí přesně nový src a
starý archiv. Při částečném C1 nebo jiné topologii STOP a ruční inventura;
žádný automatický přesun podle odhadu.

```bash
(
set -euo pipefail

OLD_IMAGE="$IMAGE_REPO:$OLD_TAG"
sudo -- /usr/bin/docker image inspect "$OLD_IMAGE" --format '{{.Id}}'
test "$(git -C "$SRC" rev-parse HEAD)" = "$NEW_SHA"
test "$(git -C "$ARCHIVE" rev-parse HEAD)" = "$OLD_SHA"
test ! -e "$STAGE"
test "$(grep -cF "image: $NEW_IMAGE" "$OVERRIDE")" = 1

sudo mv -T "$SRC" "$STAGE"
sudo mv -T "$ARCHIVE" "$SRC"
sudo sed -i "s|image: $NEW_IMAGE|image: $OLD_IMAGE|" "$OVERRIDE"

echo "NAVRAT E1: puvodni src a image obnoveny; zatim zadny start"
)
```

V `approved` režimu nyní aktualizovat root JSON image na `OLD_IMAGE` (odvozený
z `IMAGE_REPO:OLD_TAG`) a zkontrolovat socket GID. Pro obnovené aktivní `src/`,
override a **současné** `.env` znovu provést
[stage/review/approve](#schválený-vstup-koordinátoru). Neobnovovat staré `.env`
bez `ANIMEDB_CONTROL_GID`, claims ani DB. Starou generaci nevybírat naslepo:
archiv/vrácené `src/` musí odpovídat schváleným SHA i source inode. Coordinator
zůstává zastavený. Teprve potom pokračovat:

```bash
(
set -euo pipefail
OLD_IMAGE="$IMAGE_REPO:$OLD_TAG"
EDGE_COMPOSE="$SRC/deploy/nexus/service-edge/compose.edge.yaml"
if test "$DEPLOY_MODE" = approved; then
  sudo -- /usr/bin/python3 /usr/local/libexec/anime-db/reorganization_autostart.py start
else
  sudo -- /usr/bin/docker compose --project-directory "$STACK_DIR" --env-file "$STACK_DIR/.env" -p anime-db \
    -f "$BASE_COMPOSE" -f "$EDGE_COMPOSE" -f "$OVERRIDE" config -q
  test "$(sudo -- /usr/bin/docker compose --project-directory "$STACK_DIR" --env-file "$STACK_DIR/.env" -p anime-db \
    -f "$BASE_COMPOSE" -f "$EDGE_COMPOSE" -f "$OVERRIDE" config --images "$APP_SERVICE")" = "$OLD_IMAGE"
  sudo -- /usr/bin/docker compose --project-directory "$STACK_DIR" --env-file "$STACK_DIR/.env" -p anime-db \
    -f "$BASE_COMPOSE" -f "$EDGE_COMPOSE" -f "$OVERRIDE" up -d --no-deps --no-build --pull=never "$APP_SERVICE"
fi
echo "NAVRAT: OK"
)
```

Neúspěšný checkout zůstane ve `$STAGE` k diagnostice a nový image se neodstraňuje. Před dalším pokusem o stejný SHA je nutné `$STAGE` řízeně odstranit, jinak se blok A zastaví.

## 3. Co se nikdy nemění automaticky

- Produkční SQLite a její schéma.
- NAS mount, jeho práva a obsah knihovny.
- Datové bindy a artwork.
- Schvalovací evidence a runtime lock.
- Compose topologie a porty; approved start smí recreatovat edge kvůli nové generaci bind sources.
- Předchozí verzované image a checkouty.

Nasazení aplikace **není souhlas s fyzickou reorganizací NAS**. Ta má samostatný postup a schválení.

## 4. Opakované použití

Pro příští release vyplnit nové `NEW_SHA` a podle skutečného stavu proměnné `PROJECT_ROOT`, `STACK_DIR`, `APPDATA_DIR`, `DB_FILE`, `MEDIA_DIR`, `APP_SERVICE` a `IMAGE_REPO`. Před akcí potvrdit výsledný Compose. Po úspěšném nasazení zapsat výsledek do release historie. Do repozitáře neukládat lokální `.env`, IP adresy, hesla, tokeny ani osobní SSH aliasy.

## Schválený vstup koordinátoru

Tento krok se používá po instalaci V6 coordinatoru při každém release, který
mění image nebo kterýkoli z jeho tří Compose vstupů či `.env`. Běžný checkout
`src/` a archiv `releases/` zůstávají uživatelské; coordinator privilegovaně
nečte živé YAML pod jejich zapisovatelnými předky. Vlastnictví `$STACK_DIR`, projektů a appdata zůstává beze změny. Administrátorská konfigurace patří do `/etc/anime-db/`, schválené generace a privátní evidence do `/var/lib/anime-db/`, s root-owned předky. Podrobnosti prvního nasazení
jsou v [V6_COORDINATOR](V6_COORDINATOR.md#schválený-snapshot-tří-compose-vstupů).

Po C1 (nové `src/` a root override připravené) root explicitně vytvoří pouze
kopii vstupů, bez Docker operace:

```bash
sudo python3 /usr/local/libexec/anime-db/reorganization_deployment.py stage \
  --compose "$BASE_COMPOSE" \
  --compose "$SRC/deploy/nexus/service-edge/compose.edge.yaml" \
  --compose "$OVERRIDE" --env-file "$STACK_DIR/.env" \
  --approved-root /var/lib/anime-db/approved-v6
```

Zapsat výstupní `generation` jako `REVIEWED_GENERATION`. Správce zkontroluje
tři odvozené kopie `compose.1.yaml`, `compose.2.yaml`, `compose.3.yaml`, `.env`,
image, původní SHA, descriptorové bind kotvy a složenou konfiguraci proti schválenému releasu. Pouze sources pod nedůvěryhodnými předky se deterministicky přepíší na root-private kotvy; původní tři vstupy se needitují. Potom teprve
schválí výstupní přesný manifest SHA jako `REVIEWED_DEPLOYMENT_SHA`:

```bash
sudo python3 /usr/local/libexec/anime-db/reorganization_deployment.py approve \
  --generation "$REVIEWED_GENERATION" --reviewed-sha256 "$REVIEWED_DEPLOYMENT_SHA"
```

Koordinátor používá atomicky zvolenou generaci v `/var/lib/anime-db/approved-v6/current.json`
a před každým Compose voláním kontroluje root vlastnictví a všechny SHA.
Původní tři cesty/order jsou v jeho konfiguraci jen přesná provenance, nikoli
nechráněný runtime vstup. Project zůstává `anime-db` a project directory původní `$STACK_DIR`; bind sources musí být absolutní, nevznikne druhá sada kontejnerů.
Schválené kopie se nikdy needitují a není nutné rekurzivně měnit vlastníka
`src/` nebo `releases/`. Při návratu k předchozímu releasu musí správce stejně
znovu vybrat a schválit odpovídající snapshot a root-owned image; nesmí nechat
coordinator pracovat s jiným releasem než běžná aplikace.

## Autostart existující služby

Připravený drop-in `systemd/anime-db.service.d/approved-production.conf` platí
pro skutečně auditovanou `anime-db.service`. Nenahrazuje její firewallové ani
mount závislosti. Root-owned `scripts/autostart.sh` se instaluje do
`/usr/local/libexec/anime-db/`, mimo uživatelské předky, spolu s
`reorganization_autostart.py` a jeho trusted moduly. Jde o podklad budoucího
schváleného deploymentu, nikoli o provedenou změnu Nexusu.

Boot načte stejnou root konfiguraci a generaci jako coordinator, porovná živé
tři vstupy a `.env` se schválenými původními SHA a snapshotem prostředí a ověří Git HEAD aktivního `src/` proti
verzovanému image. Git ani hooky ze zdrojů nespouští. Při neshodě STOP bez
fallbacku. Spustí nejprve edge, potom app s původním first-run/execution guard
entrypointem, vždy no-build/no-pull. `ExecStop` zachovává app-only stop `-t 30`;
Nginx zůstává dostupný při reorganizaci. Coordinator ani E7/E7H se bootem
nespouštějí. Předchozí src patří do releases a nové schválené src převezme
aktivní cestu; generace se při tom znovu vytvoří a schválí.
