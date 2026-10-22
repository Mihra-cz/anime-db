# AnimeDB na Nexusu — jedna instalace

Stav: **planned / not-deployed**. Aktuální cíl je jedna služba `app`, jeden
Docker kontejner, jedna nová SQLite v9 a jeden artwork adresář. Přístup bude
`http://192.168.11.238:8000` až po ověření Docker forward firewallu pro domácí
LAN. ARM64 build, host bindy, firewall a skutečný start zatím nejsou ověřené.

Struktura navazuje na Nexus Project Cookbook v1.0. Lokální kopie
`PROJECT-STANDARD.md` byla přečtena; skutečný serverový standard a registr
`/srv/nexus/PROJECTS.md` se před schválenou instalací musí znovu ověřit.
Manifest zůstává `planned`; žádný podklad není povolení k nasazení.

| Lokální podklad | Cíl na Nexusu |
| --- | --- |
| Schválené zdroje | `/srv/nexus/projects/anime-db/src/` |
| `manifest.toml`, tento `README.md`, `docs/` | `/srv/nexus/projects/anime-db/` |
| `compose.yaml` | `/srv/nexus/stacks/anime-db/compose.yaml` |
| `.env.example` | `/srv/nexus/stacks/anime-db/.env` |

`first_run.py` a dosavadní `preflight.py` se vkládají do image. Kořenový
vývojový Compose se s tímto deploymentem neslučuje.

- [Architektura, datové cesty a hranice](docs/ARCHITECTURE.md)
- [Provoz a neověřené podmínky](docs/OPERATIONS.md)
- [Přesné instalační příkazy a první spuštění](docs/FIRST_RUN.md)
- [Podklady pro cílené review Claude Opus 5.5](docs/FIRST_RUN_REVIEW.md)
- [Changelog](docs/CHANGELOG.md)

Lokální statická validace bez Docker daemonu:

```bash
docker compose --env-file deploy/nexus/.env.example \
  -f deploy/nexus/compose.yaml config -q
docker compose --env-file deploy/nexus/.env.example \
  -f deploy/nexus/compose.yaml config --services
```

Výsledkem seznamu služeb musí být pouze `app`. Profily, druhá služba, druhý
port ani datové větve pro porovnávání databází nejsou součástí nasazení.
První instalaci registruje instalační příkaz; DB automaticky vytvoří první
start přes existující migrace. Pozdější ztráta DB znamená STOP.

## Historické a rozpracované podklady

Následující dokumenty **nejsou aktuální instalační návod**:

- [N4A SQLite helper](docs/SQLITE_BACKUP.md)
- [N4B1 Restic adaptér](docs/SQLITE_RESTIC.md)
- [N4B2 historický návrh](docs/N4B2_COMPATIBILITY.md)
- N5B_OFFLINE_DB.md — rozpracovaný podklad mimo First Run commit.
- N5C_STARTUP_DESIGN.md — rozpracovaný návrh mimo First Run commit.

`scripts/sqlite_backup.py`, `scripts/sqlite_restic.py` a
`config/sqlite-backup.json.example` se zachovávají jako historický kód/config.
Nepoužívat je pro nové datové cesty ani je nenapojovat na Restic bez samostatně
schválené úpravy. Aktuální deployment na nich nezávisí. N5B/N5C rozpracované
soubory se nemění; další řešení historických migrací není součástí First Run.
