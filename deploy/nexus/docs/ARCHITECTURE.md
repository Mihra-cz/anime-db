# Architektura jediné instalace AnimeDB

**Datovaný provozní stav, read-only ověřeno 2026-11-09.** AnimeDB na Nexusu běží
se službami `app` a `edge`. Aktivní release je commit `e53e736` z 8. 11. 2026.
Novější Archive Review (`3d145ee`) a dokumentační commit `089dcd4` jsou zatím
jen na GitHubu; aktualizace aplikace je samostatný plánovaný krok. Fyzická
reorganizace NAS nezačala. Tento odstavec není trvalé nastavení, mění ho každé
nasazení.

Jeden ARM64 image s Pythonem 3.12 a FFmpeg/FFprobe obslouží jeden Uvicorn proces
a jednu pracovní SQLite DB. Metadata enrichment i AniList jsou vypnuté. Běžný
web nepoužívá žádný backup helper, executor ani importní službu.

Aktualizaci běžící instalace popisuje jediný runbook
[RELEASE_UPDATE](RELEASE_UPDATE.md). Chování při první instalaci a ztrátě DB je
v [OPERATIONS](OPERATIONS.md#první-instalace-aktualizace-a-ztráta-databáze).

## Umístění dat a přístupová práva

Veřejná dokumentace používá proměnné z runbooku. `SRC` je `$PROJECT_ROOT/src`.

| Hostitelská cesta | Kontejner / účel | Přístup |
| --- | --- | --- |
| `$SRC/` | aktivní Git checkout, zdroj pro build image | bez bindu do `app` |
| `$SRC/deploy/nexus/service-edge/` | `/service` v `edge` | RO |
| `$PROJECT_ROOT/releases/` | archiv předchozích zdrojů | bez bindu v Compose |
| `$PROJECT_ROOT/manifest.toml` | Cookbook manifest | bez runtime bindu |
| `$STACK_DIR/compose.yaml` | základní Compose | bez runtime bindu |
| `$STACK_DIR/compose.v6.yaml` | produkční override, **není v Gitu** | bez runtime bindu |
| `$STACK_DIR/.env` | non-secret UID/GID | bez runtime bindu |
| `$APPDATA_DIR/db/` | `/data`, soubor `anime.db` (`DB_FILE`) | RW, lokální NVMe |
| `$APPDATA_DIR/state/` | `/state`, instalační evidence a runtime lock | RW, lokální NVMe |
| `$APPDATA_DIR/artwork/` | `/artwork` | RW, lokální NVMe |
| `$APPDATA_DIR/approval/inbox/` | `/approval-inbox` | RO v `app` |
| `$APPDATA_DIR/approval/outbox/` | `/approval-outbox` | RW v `app` |
| `$APPDATA_DIR/approval/ledger/` | hostitelský ledger schválení | bez bindu do `app` |
| `$APPDATA_DIR/service/` | `/public-status` v `edge` | RO v `edge` |
| `$APPDATA_DIR/backups/daily/`, `pre-change/` | budoucí snapshoty | bez runtime bindu |
| `$MEDIA_DIR` | `/media/anime` v `app` | vždy RO |

**RO a RW:** aplikace čte knihovnu přes RO bind. Na hostiteli je CIFS mount
knihovny také RO a RW pohled pro reorganizační worker dosud neexistuje; připraví
se až při samostatně schválené reorganizaci. Zapisovat může `app` jen do `/data`,
`/state`, `/artwork`, `/approval-outbox` a tmpfs `/tmp`. Kořenový filesystem
obou kontejnerů je RO. Rozhodnutí Archive Review budou po jeho nasazení ležet
v `anime.db.archive-reviews/` vedle DB; k datu výše tento adresář neexistuje.

**NAS účty.** Na NAS jsou dva oddělené účty, veřejná dokumentace je označuje rolemi:

| Role | Přístup | Určení | Stav k datu výše |
| --- | --- | --- | --- |
| `NAS_SCAN_USER` | RO | běžící AnimeDB, scanner a read-only plánování | v provozu; serverová ACL NEOVĚŘENO |
| `NAS_IMPORT_USER` | RW | samostatný administrační worker při schválené reorganizaci a budoucím importu | účet existuje, RW worker ani jeho mount nasazené nejsou |

Existence účtu `NAS_IMPORT_USER` neznamená, že worker běží ani že je reorganizace
povolená. Běžný kontejner `app` má NAS vždy RO a nemá žádné RW přihlašovací údaje.

## Tři Compose soubory

Produkce se vyhodnocuje vždy v tomto pořadí a pozdější soubor přepisuje dřívější:

1. `$STACK_DIR/compose.yaml`, obsahem shodný s [`deploy/nexus/compose.yaml`](../compose.yaml).
2. `$SRC/deploy/nexus/service-edge/compose.edge.yaml`, shodný s
   [verzí v repozitáři](../service-edge/compose.edge.yaml).
3. `$STACK_DIR/compose.v6.yaml`, lokální produkční override mimo Git.

**Samotný základní Compose není kompletní produkční deployment.** Sám by znovu
publikoval port, který vlastní `edge`, a bez override by použil neverzovaný tag
`anime-db:nexus` a neměl by approval bindy.

| Vlastnost | Base | Edge | V6 override | Výsledek |
| --- | --- | --- | --- | --- |
| Služby | `app` | přidává `edge` | beze změny | `app`, `edge` |
| Image `app` | `anime-db:nexus` | – | `$IMAGE_REPO:v6-<short SHA>` | verzovaný tag, k datu výše `v6-e53e736` |
| Build `app` | context `$SRC`, args `APP_UID`, `APP_GID` | – | `build: !reset null` | žádný, image se staví zvlášť |
| Porty `app` | LAN `:8000` → `8000` | `ports: !reset []` | – | žádný publikovaný port |
| Image `edge` | – | `nginx:stable-alpine` s pinem digestu | – | stejný |
| Porty `edge` | – | LAN `:8000` → `8080` | – | LAN `:8000` → `8080` |
| UID/GID | `${ANIMEDB_UID:-1000}:${ANIMEDB_GID:-1000}` | `edge` `101:101` | – | `app` `1000:1000`, `edge` `101:101` |
| Entrypoint `app` | `python .../nexus_first_run.py` | – | – | beze změny |
| Command `app` | `uvicorn app.main:app --host 0.0.0.0 --port 8000` | – | – | beze změny |
| Entrypoint `edge` | – | `nginx -c /service/nginx.conf -g 'daemon off;'` | – | beze změny |
| Pracovní adresář | `app` `/app` | `edge` `/tmp` | – | beze změny |
| Prostředí `app` | SQLite URL `mode=rw&uri=true`, `ANIME_PATH`, `REQUIRE_MOUNT`, metadata vypnutá | – | `REORGANIZATION_APPROVAL_INBOX`, `_OUTBOX` | sloučeno |
| Bind DB | `$APPDATA_DIR/db` → `/data` | – | – | RW |
| Bind state | `$APPDATA_DIR/state` → `/state` | – | – | RW |
| Bind artwork | `$APPDATA_DIR/artwork` → `/artwork` | – | – | RW |
| Bind NAS | `$MEDIA_DIR` → `/media/anime` | – | – | **RO** |
| Bindy approval | – | – | inbox RO, outbox RW | přidány |
| Bindy `edge` | – | `$SRC/deploy/nexus/service-edge` → `/service`, `$APPDATA_DIR/service` → `/public-status` | – | oba RO |
| Izolace | RO rootfs, `cap_drop: ALL`, `no-new-privileges`, tmpfs `/tmp` 64 MiB, `init` | `edge`: stejně, tmpfs 8 MiB, bez `init` | – | beze změny |
| Restart | `"no"` | `"no"` | – | beze změny |

Override nemění entrypoint, command, porty ani stávající datové bindy. Anonymizovaná
podoba jeho funkce:

```yaml
services:
  app:
    image: IMAGE_REPO:v6-SHORT_SHA
    build: !reset null
    environment:
      REORGANIZATION_APPROVAL_INBOX: /approval-inbox
      REORGANIZATION_APPROVAL_OUTBOX: /approval-outbox
    volumes:
      # type: bind, create_host_path: false
      - APPDATA_DIR/approval/inbox:/approval-inbox:ro
      - APPDATA_DIR/approval/outbox:/approval-outbox
```

### Co bylo a nebylo ověřeno 2026-11-09

Ověřeno read-only: `src/` je čistý Git checkout na `e53e736`; `releases/` obsahuje
`v6-c596fe3` (Git) a `pre-v6-20261022` (export bez `.git`). Base, edge a `nginx.conf`
se shodují s GitHubem podle SHA-256, override byl přečten přímo. Sloučené
`docker compose config` potvrdilo služby, image, porty a bindy. Mountinfo procesu
`app` potvrdilo RO CIFS na `/media/anime` a RW lokální `/data`, `/state`, `/artwork`.
HTTP potvrdilo Nginx na LAN portu a dostupné `/health`. DB má `user_version` 9
a evidence obsahuje `provisioned.json`, `initializing.json`, `ready.json`.
Trasa `/reorganization/archive-review` vrací 404, Archive Review tedy nasazené
není. Kořeny `Subs`, `Duplicates` a `IMPORT` na NAS neexistují a approval inbox
i outbox jsou prázdné.

Privilegovaný read-only výpis z Docker API (běžný SSH účet k němu přístup nemá)
potvrdil kontejnery `anime-db-app-1` s image `anime-db:v6-e53e736` a
`anime-db-edge-1`. Oba vznikly ze souborů v pořadí `compose.yaml`,
`compose.edge.yaml`, `compose.v6.yaml`, mají restart count 0 a RW příznaky bindů
odpovídají tabulce výše. Starší image `anime-db:v6-c596fe3` a `anime-db:nexus`
zůstávají zachované.

**NEOVĚŘENO:** skutečný hostitelský adresář bindu `/service` běžícího `edge`.

## První instalace a další starty

Samostatný instalační příkaz vytvoří `provisioned.json` v prázdném `state/`,
bez SQLite. První runtime nejprve ověří konfiguraci, non-root, dosavadní CIFS
RO preflight a skutečné lokální NVMe bindy. Pod výhradním flockem zapíše
fsyncnutý `initializing.json`, automaticky vytvoří DB a zavolá existující
`migrate_schema_at_startup`. Po kontrole v9, integrity, FK a tabulek uloží
`ready.json` a předá řízení jedinému Uvicornu. Scanner při startu neběží.

Stejný entrypoint `nexus_first_run.py` běží při každém startu `app`. Při
existujícím `ready.json` nic neinstaluje a jen validuje evidenci a DB.

READY restart validuje existující DB; přerušená inicializace, chybějící
nebo neplatná evidence/DB znamenají STOP, bez retry, resetu či automatické
obnovy. `DATABASE_URL` už v Compose vyžaduje SQLite `mode=rw&uri=true` pro
každé připojení včetně pozdějšího otevření poolu. Evidence používá stabilní
project ID `anime-db` a náhodné installation ID. Úplné administrátorské
smazání/rollback DB i veškeré evidence není tímto minimálním mechanismem
rozpoznatelné. Restore/adoption a storage UUID attestation nejsou implementované.

## Síť a izolace

LAN port 8000 vlastní `edge`. Uvicorn poslouchá na `0.0.0.0:8000` jen uvnitř
Compose sítě, `app` nemá publikovaný port. Nginx směruje požadavky na `app:8000`
přes Docker DNS; detaily jsou v [README edge](../service-edge/README.md).
Žádný internetový endpoint ani další publikovaný port nevzniká.

Omezení na domácí LAN v Docker forward firewall cestě je **NEOVĚŘENO**, pravidla
čte jen root. Pouhá INPUT/UFW konfigurace ani bind na privátní IP omezení
nedokazují; kontrolní postup je v [OPERATIONS](OPERATIONS.md). Síťová ani
aplikační autentizace není implementována.

Image běží jako non-root UID/GID `1000:1000` (konfigurovatelné při buildu),
root FS RO, `cap_drop: ALL`, `no-new-privileges`, `init: true`, `restart: no`.
Docker `local` log driver rotuje 3 × 10 MB. NAS bind zůstává RO i při CIFS
superblocku RW; preflight odmítá nesprávný share, RW aliasy a nejednoznačnou
mount tabulku. Artwork je oddělený, persistentní a zapisovatelný, i když jsou
download a remote images vypnuté.

## Zálohy a historické podklady

Jediný backup root má `daily/` a `pre-change/`. Budoucí záměr: denní snapshot
ve 02:00, snapshot před významnou DB změnou a převzetí hotových snapshotů
existujícím Resticem kolem 03:30. Časování, snapshot writer, výjimky živé DB,
retence a obnova **nejsou instalované ani ověřené**. Snapshot adresáře nejsou
přístupné webovému kontejneru.

Historické N4A/N4B1 helpery, jejich config a N2/N4 dokumenty používají původní
srovnávací model. Zůstávají neaktivní a nejsou závislostí této instalace;
nepřesměrovávat je na nové cesty bez samostatného zadání. Podrobnosti jsou
zachované v [historickém N4A dokumentu](SQLITE_BACKUP.md) a
[historickém N4B1 dokumentu](SQLITE_RESTIC.md).

## Připravená hostitelská kompatibilita V6 (nenasazeno)

Host coordinator podporuje uživatelské předky runtime dat bez změny vlastnictví
Nexusu. Konfigurace patří do `/etc/anime-db/`, privátní evidence a schválené
Compose generace do `/var/lib/anime-db/`. Runtime publikace používá připnuté
dirfd a non-root child. Socket používá samostatnou `animedb-control` skupinu,
kterou edge dostává doplňkově; jeho primární `101:101` se nemění. Připravený
app-only stop a boot app+edge používají tutéž generaci existujícího projektu.
Podrobnosti, limity validace a stav nasazení jsou v
[reportu](../../../docs/V6_NEXUS_HOST_COMPATIBILITY_REVIEW.md).
