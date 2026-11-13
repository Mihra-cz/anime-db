# Izolovaný V6 lab na Linux Mint (pouze test)

Lab slouží k ručnímu průchodu webovým V6 workflow na notebooku. Používá
skutečný aktuální kód aplikace, skutečný Nginx edge, skutečný hostitelský
koordinátor a původní E7/E7H worker nad syntetickou knihovnou a SQLite v9.
**Nic z tohoto adresáře není produkční konfigurace** a nesmí se slučovat se
soubory v `deploy/nexus/`.

## Izolace

| Část | Lab |
| --- | --- |
| Compose projekty | `animedb-v6-lab` (app, edge), `animedb-v6-lab-smb` (lab SMB server) |
| Web | pouze `http://127.0.0.1:18080` (edge); app nemá publikovaný port |
| SMB | Samba kontejner, pouze `127.0.0.1:1445`; žádný systémový SMB server |
| Identity | lab-only `lab_scan` (serverově RO) a `lab_worker` (RW); náhodná hesla jen v `/srv/animedb-v6-lab/smb/secrets` (root `0700`) |
| CIFS | `/srv/animedb-v6-lab/mnt/ro` (lab_scan, `ro`) → app; `/srv/animedb-v6-lab/mnt/rw` (lab_worker, `rw`) → pouze worker |
| Data | `/srv/animedb-v6-lab/appdata/*`; golden kopie v `/srv/animedb-v6-lab/golden` |
| Koordinátor | transientní systemd unit `animedb-v6-lab-coordinator`, root kopie v `/srv/animedb-v6-lab/libexec` |
| Socket | `/run/animedb-v6-lab/coordinator.sock` (`root:<CONTROL_GID> 0660`, adresář `0750`) |
| Compose pro koordinátor | schválená root generace v `/srv/animedb-v6-lab/stack/approved-v6` |

Nikdy se nepoužívá produkční `data/anime.db` (vyloučeno i z image přes
`.dockerignore`), `/mnt/nas-anime`, Synology účty, Nexus cesty ani projekt
`anime-db`. Do `/etc` ani fstab se nic nezapisuje; mounty a unit zmizí rebootem.

## Soubory

| Soubor | Účel |
| --- | --- |
| `compose.yaml`, `compose.v6.yaml` | lab base a override; root kopie v `/srv/animedb-v6-lab/stack/` (obdoba root stacku na Nexusu) |
| `compose.edge.yaml` | lab edge; zůstává uživatelský zdroj v repozitáři (obdoba `src/`), koordinátor používá jen schválenou root kopii |
| `compose.smb.yaml`, `smb/` | lab SMB server (Alpine + Samba, pinned digest) |
| `lab_entrypoint.py` | drží sdílený `/state/runtime.lock` jako `first_run.py`, bez Nexus NVMe/instalační attestation |
| `hooks/` | lab snapshot (nezávislá serverová kopie + fingerprint) a writer fence; režim selhání přes `hook-mode` |
| `lab_capability.py` | jednorázové zkoušky na RW CIFS (fsync, `RENAME_NOREPLACE`, cross-dir rename), RO odmítnutí zápisu, poté mount-bound runtime evidence |
| `make_lab_data.py` | syntetická knihovna a SQLite v9 z existujících fixture helperů; ověřeno plannerem |
| `lab_smoke.py` | nedestruktivní smoke test včetně krátké odstávky lab app |
| `lab.sh` | ovládání |

Syntetická knihovna: Show, Lab Alpha, Lab Beta (2 sezóny); 10× MOVE
(videa i titulky), 3× KEEP, 1× QUARANTINE (potvrzený duplikát → `Duplicates`),
1× WARNING (`subtitle_present_video_missing`), chráněné `IMPORT` a `Subs`,
po přesunech prázdné adresáře pro E7H cleanup.

## Ovládání

```bash
deploy/lab/lab.sh build
```
```bash
sudo deploy/lab/lab.sh up
```
```bash
deploy/lab/lab.sh smoke
```
```bash
deploy/lab/lab.sh status
```
```bash
sudo deploy/lab/lab.sh reset
```
```bash
sudo deploy/lab/lab.sh stop
```

`up` = jednorázový `setup` + golden data (pod tvým účtem) + `start`. Další
starty po `stop` nebo rebootu: `sudo deploy/lab/lab.sh start`. Simulace selhání:
`sudo deploy/lab/lab.sh hook-mode fail-snapshot` (nebo `fail-stop-writers`,
`fail-start-writers`, zpět `ok`). Úplné odstranění pouze lab prostředků:
`sudo deploy/lab/lab.sh teardown --yes` (image zůstanou, příkaz vypíše jejich
názvy). Žádné `docker system prune` ani mazání cizích kontejnerů či volumes.

Po změně kódu: `lab.sh build` a `sudo lab.sh reset` (nová image, nová
schválená generace a nový capability důkaz). Remount CIFS vždy vyžaduje nový
důkaz; `start` jej vytváří automaticky jen po úspěšných zkouškách.

## Prohlížeč

Otevírat výhradně `http://127.0.0.1:18080/reorganization` (ne `localhost`):
koordinátor ověřuje přesný Origin. Platnost připraveného manifestu je 900 s od
publikace; schválení i start musí proběhnout v tomto okně.

## Co lab prokazuje a co ne

Skutečné: kód V6, Docker image (amd64), Compose model tří vstupů se schválenou
root generací, Nginx a Unix socket, root koordinátor se stejnými systemd
vlastnostmi procesu, `docker run` workeru, sdílený `runtime.lock`, CIFS kernel
klient nad Samba serverem se dvěma identitami a serverovým RO ACL, mount-bound
runtime evidence, původní E7/E7H a SQLite lifecycle.

Simulované nebo jiné než Nexus: Samba místo Synology (ACL, rename a durability
semantika Synology neověřeny), lab snapshot místo Synology snapshotu, writer
hooky bez skutečných externích writerů, `lab_entrypoint.py` místo `first_run.py`
(NVMe a instalační evidence neověřeny), amd64 místo ARM64, výkon a časování
Raspberry Pi 5 neověřeny.

Socket má vlastní číselný `CONTROL_GID` (příklad 2001, přepis `ANIMEDB_LAB_CONTROL_GID`). Před setup ověřit, že zvolený GID nemá nesouvisející členy. Nginx zůstává `101:101`, socket group dostává přes `group_add`; `_ssh` se nemění. Helper instaluje také sdílený trusted `host_io.py`. Tato úprava sama LAB nespouští ani nemění jeho existující data.
