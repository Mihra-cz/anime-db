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

Stávající konfigurace používá Compose **v přesném pořadí**: základní → edge → produkční override. Pozdější soubor může měnit hodnoty z předchozích souborů. Při aktualizaci běžné aplikace nerecreatovat edge.

## 2. Před každou aktualizací

Aktualizaci nezačínat během přípravy manifestu, aktivního E7/E7H ani při
nevyhodnoceném STOP. Neodstavovat coordinator s běžícím workerem jako součást
běžného release. Případnou změnu jeho root-owned scriptů a helperu instalovat
z konkrétního revidovaného releasu cíleně; živé spouštění Pythonu z user-owned
checkoutu privilegovaný coordinator nepoužívá. Stávající připravené manifesty
se nesmějí převádět na novou verzi automaticky.


Předem určit a schválit **plný SHA** cílového commitu. Nespouštět update na plovoucí `main`.

### Předpoklady

- Všechny bloky se spouštějí **na serveru v jedné interaktivní relaci Bash** (nikoli `sh`), postupně: proměnné → A → B → C → D. Blok E je jen pro návrat po selhání.
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

Žádné heslo ani token nevkládat přímo do příkazů, dokumentace nebo Git remote URL. Použít existující správně nastavené autentizační prostředky.

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

sudo -- /usr/bin/docker compose -f "$BASE_COMPOSE" -f "$EDGE_COMPOSE" -f "$OVERRIDE" config -q
sudo -- /usr/bin/docker compose -f "$BASE_COMPOSE" -f "$EDGE_COMPOSE" -f "$OVERRIDE" ps

# K ověření image a bindů použij skutečné ID běžící služby.
APP_CONTAINER="$(sudo -- /usr/bin/docker compose -f "$BASE_COMPOSE" -f "$EDGE_COMPOSE" -f "$OVERRIDE" ps -q "$APP_SERVICE")"
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

# UID/GID je nutno převzít ze skutečně běžícího kontejneru,
# nikoli odhadovat nebo zapisovat napevno.
APP_CONTAINER="$(sudo -- /usr/bin/docker compose -f "$BASE_COMPOSE" -f "$EDGE_COMPOSE" -f "$OVERRIDE" ps -q "$APP_SERVICE")"
test -n "$APP_CONTAINER"
APP_USER="$(sudo -- /usr/bin/docker inspect --format '{{.Config.User}}' "$APP_CONTAINER")"
case "$APP_USER" in
  *:*) APP_UID="${APP_USER%%:*}"; APP_GID="${APP_USER##*:}" ;;
  *) echo 'STOP: UID/GID neni jednoznacne'; exit 1 ;;
esac
[[ "$APP_UID" =~ ^[0-9]+$ && "$APP_GID" =~ ^[0-9]+$ ]]

sudo -- /usr/bin/docker build --build-arg "APP_UID=$APP_UID" \
  --build-arg "APP_GID=$APP_GID" \
  -t "$NEW_IMAGE" "$STAGE"
sudo -- /usr/bin/docker image inspect "$NEW_IMAGE" --format '{{.Id}}'
echo "BLOK B: OK"
)
```

Když cílový commit není dostupný, **nepřecházet automaticky na nejnovější `main`**. Když výpis `git diff --stat` není prázdný, release mění základní Compose nebo edge: zastavit a posoudit samostatně, protože `$STACK_DIR/compose.yaml` ani běžící edge tyto změny nepřevezmou. Nedokončený `$STAGE` po selhání vyhodnotit a teprve potom řízeně odstranit, jinak se blok A zastaví.

### C. Aktivace: archiv starých zdrojů, aktualizace override, recreace jen aplikace

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

Před následujícím C2 určit, zda je V6 coordinator již instalovaný. Je-li
`$STACK_DIR/reorganization-coordinator.json` přítomný, provést níže uvedené
[schválení snapshotu](#schválený-vstup-koordinátoru) a v této stejné relaci
nastavit `REVIEWED_GENERATION` na právě schválenou generaci. Před aktivací také
cíleně aktualizovat root-owned coordinator image na tentýž `NEW_IMAGE` a
revidovat související změny jeho konfigurace. Tím se nevytváří druhý ručně
udržovaný stack; generace se vždy vyrábí z původních tří vstupů.

```bash
(
set -euo pipefail
EDGE_COMPOSE="$SRC/deploy/nexus/service-edge/compose.edge.yaml"
if test -f "$STACK_DIR/reorganization-coordinator.json"; then
  : "${REVIEWED_GENERATION:?Nejprve schval presnou generaci podle oddilu nize}"
  DEPLOY_BASE="$REVIEWED_GENERATION/compose.1.yaml"
  DEPLOY_EDGE="$REVIEWED_GENERATION/compose.2.yaml"
  DEPLOY_OVERRIDE="$REVIEWED_GENERATION/compose.3.yaml"
  DEPLOY_ENV="$REVIEWED_GENERATION/.env"
else
  DEPLOY_BASE="$BASE_COMPOSE"; DEPLOY_EDGE="$EDGE_COMPOSE"
  DEPLOY_OVERRIDE="$OVERRIDE"; DEPLOY_ENV="$STACK_DIR/.env"
fi
sudo -- /usr/bin/docker compose --project-directory "$STACK_DIR" --env-file "$DEPLOY_ENV" -p anime-db \
  -f "$DEPLOY_BASE" -f "$DEPLOY_EDGE" -f "$DEPLOY_OVERRIDE" config -q
test "$(sudo -- /usr/bin/docker compose --project-directory "$STACK_DIR" --env-file "$DEPLOY_ENV" -p anime-db \
  -f "$DEPLOY_BASE" -f "$DEPLOY_EDGE" -f "$DEPLOY_OVERRIDE" config --images "$APP_SERVICE")" = "$NEW_IMAGE"
sudo -- /usr/bin/docker compose --project-directory "$STACK_DIR" --env-file "$DEPLOY_ENV" -p anime-db \
  -f "$DEPLOY_BASE" -f "$DEPLOY_EDGE" -f "$DEPLOY_OVERRIDE" up -d --no-deps --no-build --pull=never "$APP_SERVICE"
echo "BLOK C: OK"
)
```

Pozor na edge: běžící edge kontejner drží bind na adresář, který existoval při jeho startu. Po přesunu tedy dál čte soubory z archivovaného checkoutu, nikoli z nového `src/`, a změny v `deploy/nexus/service-edge/` se projeví až při samostatně schválené recreaci edge. Edge kontejner se zbytečně nerecreatuje. Staré image ani archivované zdroje neodstraňovat. **Nepoužívat** `docker compose down`, `--remove-orphans` nebo `docker system prune`.

### D. Ověření po aktualizaci

```bash
(
set -euo pipefail

# -a: i kontejner zastavený po neúspěšném startu musí ukázat stav a logy.
APP_CONTAINER="$(sudo -- /usr/bin/docker compose -f "$BASE_COMPOSE" -f "$EDGE_COMPOSE" -f "$OVERRIDE" ps -aq "$APP_SERVICE")"
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

Blok předpokládá dokončený blok C a stejnou relaci s původními proměnnými. Pokud se blok C přerušil uprostřed, tento blok nespouštět a stav dořešit ručně podle skutečných cest.

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

sudo -- /usr/bin/docker compose -f "$BASE_COMPOSE" -f "$EDGE_COMPOSE" -f "$OVERRIDE" config -q
test "$(sudo -- /usr/bin/docker compose -f "$BASE_COMPOSE" -f "$EDGE_COMPOSE" -f "$OVERRIDE" config --images "$APP_SERVICE")" = "$OLD_IMAGE"
sudo -- /usr/bin/docker compose -f "$BASE_COMPOSE" -f "$EDGE_COMPOSE" -f "$OVERRIDE" up -d --no-deps --no-build "$APP_SERVICE"
echo "NAVRAT: OK"
)
```

Neúspěšný checkout zůstane ve `$STAGE` k diagnostice a nový image se neodstraňuje. Před dalším pokusem o stejný SHA je nutné `$STAGE` řízeně odstranit, jinak se blok A zastaví.

## 3. Co se nikdy nemění automaticky

- Produkční SQLite a její schéma.
- NAS mount, jeho práva a obsah knihovny.
- Datové bindy a artwork.
- Schvalovací evidence a runtime lock.
- Compose topologie, Nginx edge a porty.
- Předchozí verzované image a checkouty.

Nasazení aplikace **není souhlas s fyzickou reorganizací NAS**. Ta má samostatný postup a schválení.

## 4. Opakované použití

Pro příští release vyplnit nové `NEW_SHA` a podle skutečného stavu proměnné `PROJECT_ROOT`, `STACK_DIR`, `APPDATA_DIR`, `DB_FILE`, `MEDIA_DIR`, `APP_SERVICE` a `IMAGE_REPO`. Před akcí potvrdit výsledný Compose. Po úspěšném nasazení zapsat výsledek do release historie. Do repozitáře neukládat lokální `.env`, IP adresy, hesla, tokeny ani osobní SSH aliasy.

## Schválený vstup koordinátoru

Tento krok se používá po instalaci V6 coordinatoru při každém release, který
mění image nebo kterýkoli z jeho tří Compose vstupů či `.env`. Běžný checkout
`src/` a archiv `releases/` zůstávají uživatelské; coordinator privilegovaně
nečte živé YAML pod jejich zapisovatelnými předky. Kořen `$STACK_DIR` a
administrátorská konfigurace zůstávají root-owned. Podrobnosti prvního nasazení
jsou v [V6_COORDINATOR](V6_COORDINATOR.md#schválený-snapshot-tří-compose-vstupů).

Po C1 (nové `src/` a root override připravené) root explicitně vytvoří pouze
kopii vstupů, bez Docker operace:

```bash
sudo python3 /usr/local/libexec/anime-db/reorganization_deployment.py stage \
  --compose "$BASE_COMPOSE" \
  --compose "$SRC/deploy/nexus/service-edge/compose.edge.yaml" \
  --compose "$OVERRIDE" --env-file "$STACK_DIR/.env"
```

Zapsat výstupní `generation` jako `REVIEWED_GENERATION`. Správce zkontroluje
přesné tři kopie `compose.1.yaml`, `compose.2.yaml`, `compose.3.yaml`, `.env`,
image, bindy a složenou konfiguraci proti schválenému releasu. Potom teprve
schválí výstupní přesný manifest SHA jako `REVIEWED_DEPLOYMENT_SHA`:

```bash
sudo python3 /usr/local/libexec/anime-db/reorganization_deployment.py approve \
  --generation "$REVIEWED_GENERATION" --reviewed-sha256 "$REVIEWED_DEPLOYMENT_SHA"
```

Koordinátor používá atomicky zvolenou generaci v `$STACK_DIR/approved-v6/current.json`
a před každým Compose voláním kontroluje root vlastnictví a všechny SHA.
Původní tři cesty/order jsou v jeho konfiguraci jen přesná provenance, nikoli
nechráněný runtime vstup. Project zůstává `anime-db`, relativní cesty se stále
vyhodnocují vůči původnímu `$STACK_DIR`; nevznikne druhá sada kontejnerů.
Schválené kopie se nikdy needitují a není nutné rekurzivně měnit vlastníka
`src/` nebo `releases/`. Při návratu k předchozímu releasu musí správce stejně
znovu vybrat a schválit odpovídající snapshot a root-owned image; nesmí nechat
coordinator pracovat s jiným releasem než běžná aplikace.
