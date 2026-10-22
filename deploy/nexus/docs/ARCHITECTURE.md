# Architektura jediné instalace AnimeDB

Stav: **planned / not-deployed**. Jeden ARM64 image `anime-db:nexus` s Pythonem
3.12 a FFmpeg/FFprobe obslouží jedinou Compose službu `app`, jeden Uvicorn
proces a jednu pracovní SQLite DB. Metadata enrichment i AniList jsou vypnuté.
Běžný web nepoužívá žádný backup helper, executor ani importní službu.

| Hostitelská cesta | Kontejner / účel | Přístup |
| --- | --- | --- |
| `/srv/nexus/projects/anime-db/src/` | zdroje a build context | bez runtime bindu |
| `/srv/nexus/projects/anime-db/manifest.toml` | Cookbook manifest | bez runtime bindu |
| `/srv/nexus/stacks/anime-db/compose.yaml` | jediný deployment Compose | bez runtime bindu |
| `/srv/nexus/stacks/anime-db/.env` | non-secret UID/GID | bez runtime bindu |
| `/srv/nexus/appdata/anime-db/db/` | `/data`, soubor `anime.db` | RW, lokální NVMe |
| `/srv/nexus/appdata/anime-db/artwork/` | `/artwork` | RW, oddělené NVMe |
| `/srv/nexus/appdata/anime-db/state/` | `/state`, instalační evidence a runtime lock | RW, oddělené NVMe |
| `/srv/nexus/appdata/anime-db/backups/daily/` | budoucí denní snapshoty | bez runtime bindu |
| `/srv/nexus/appdata/anime-db/backups/pre-change/` | budoucí snapshoty před změnou | bez runtime bindu |
| `/mnt/nas-anime` | `/media/anime` | vždy RO CIFS |
| `/mnt/nas-backup` | stávající Restic infrastruktura | bez runtime bindu |

`state/` je malá trvalá evidence již připravovaného First Run: umožňuje
rozlišit doloženou první instalaci od pozdější ztráty DB. Není v DB adresáři,
aby ztráta tohoto adresáře neztratila současně i historii inicializace.
Nepřidává další databázi, instanci, coordinator ani backup framework.

Všechny čtyři datové bindy vyžadují `create_host_path: false`. Není připojen
společný appdata rodič, zdrojový kód, notebooková DB, snapshoty ani Docker
socket. Žádné srovnávací datové větve nebo další služby/sítě se nezakládají;
Compose používá jednu vlastní default bridge síť.

## První instalace a další starty

Samostatný instalační příkaz vytvoří `provisioned.json` v prázdném `state/`,
bez SQLite. První runtime nejprve ověří konfiguraci, non-root, dosavadní CIFS
RO preflight a skutečné lokální NVMe bindy. Pod výhradním flockem zapíše
fsyncnutý `initializing.json`, automaticky vytvoří DB a zavolá existující
`migrate_schema_at_startup`. Po kontrole v9, integrity, FK a tabulek uloží
`ready.json` a předá řízení jedinému Uvicornu. Scanner při startu neběží.

READY restart validuje existující DB; přerušená inicializace, chybějící
nebo neplatná evidence/DB znamenají STOP, bez retry, resetu či automatické
obnovy. `DATABASE_URL` už v Compose vyžaduje SQLite `mode=rw&uri=true` pro
každé připojení včetně pozdějšího otevření poolu. Evidence používá stabilní
project ID `anime-db` a náhodné installation ID. Úplné administrátorské
smazání/rollback DB i veškeré evidence není tímto minimálním mechanismem
rozpoznatelné. Restore/adoption a storage UUID attestation nejsou implementované.

## Síť a izolace

Jediné publikování na hostiteli je **`192.168.11.238:8000:8000`**.
Cílová URL je `http://192.168.11.238:8000`. Uvicorn uvnitř kontejneru
poslouchá na `0.0.0.0:8000`; hostitelský bind je pouze konkrétní LAN adresa.
Žádný internetový endpoint, reverse proxy ani další publikovaný port nevzniká.

Před LAN startem musí být prokázané omezení na domácí LAN v Docker forward
firewall cestě, včetně správného pořadí vůči Docker ACCEPT pravidlům. Pouhá
INPUT/UFW konfigurace ani bind na privátní IP to nedokazují. Kontrolní postup
je v [OPERATIONS](OPERATIONS.md); současný firewall se bez dalšího schválení
nemění. Síťová ani aplikační autentizace není tímto nově implementována.

Image zůstává non-root UID/GID `1000:1000` (konfigurovatelné při buildu),
root FS RO, `/tmp` tmpfs 64 MiB, `cap_drop: ALL`, `no-new-privileges`,
`init: true`, `restart: no`. Docker `local` log driver rotuje 3 × 10 MB.
NAS bind zůstává RO i při CIFS superblocku RW; dosavadní preflight odmítá
nesprávný share, RW aliasy a nejednoznačnou mount tabulku. Artwork je oddělený,
persistentní a zapisovatelný, i když jsou download a remote images vypnuté.

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

Skutečný ARM64 build/runtime, host oprávnění/mounty a firewall zůstávají
neověřené. `planned` se nesmí změnit na `deployed` jen na základě lokálních testů.
