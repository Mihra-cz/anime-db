# AnimeDB na Nexusu — jedna instalace

**Datovaný stav k 2026-11-09:** instalace běží se službami `app` a `edge`, aktivní
release je `e53e736` z 8. 11. 2026. Novější Archive Review není nasazené a fyzická
reorganizace NAS nezačala. Ověřené i neověřené údaje jsou v
[architektuře](docs/ARCHITECTURE.md). Žádný podklad zde není povolení k nasazení.

Struktura navazuje na Nexus Project Cookbook v1.0. Manifest má stav `testing`:
Cookbook dovoluje `deployed` až po ověření restartu, logů a obnovy a obnova
ověřená není.

Produkce se skládá ze **tří Compose souborů** v pořadí základní → edge → lokální
V6 override. Samotný základní soubor není kompletní produkční deployment.

| Podklad | Umístění v instalaci |
| --- | --- |
| Schválený Git commit | `$PROJECT_ROOT/src/` |
| Předchozí zdroje | `$PROJECT_ROOT/releases/` |
| `compose.yaml` | `$STACK_DIR/compose.yaml` |
| `service-edge/compose.edge.yaml` | používá se přímo z `$PROJECT_ROOT/src/` |
| Lokální V6 override, mimo Git | `$STACK_DIR/compose.v6.yaml` |
| `.env.example` | `$STACK_DIR/.env` |
| `manifest.toml`, tento `README.md`, `docs/` | kopie z první instalace v `$PROJECT_ROOT/`, průběžně se neaktualizují |

`first_run.py` a dosavadní `preflight.py` se vkládají do image. Kořenový
vývojový Compose se s tímto deploymentem neslučuje.

- [Architektura, tři Compose soubory a datové cesty](docs/ARCHITECTURE.md)
- [Provoz, první instalace a ztráta databáze](docs/OPERATIONS.md)
- [Aktualizace běžící instalace](docs/RELEASE_UPDATE.md)
- [Nginx edge a servisní stránka](service-edge/README.md)
- [Jednorázová instalace V6 koordinátoru a běžné webové ovládání](docs/V6_COORDINATOR.md)
- [Historický návod první instalace](docs/FIRST_RUN.md), nepoužívat pro aktualizaci
- [Podklady pro cílené review Claude Opus 5.5](docs/FIRST_RUN_REVIEW.md)
- [Changelog](docs/CHANGELOG.md)

Lokální statická validace základního souboru bez Docker daemonu:

```bash
docker compose --env-file deploy/nexus/.env.example \
  -f deploy/nexus/compose.yaml config -q
docker compose --env-file deploy/nexus/.env.example \
  -f deploy/nexus/compose.yaml config --services
```

Výsledkem seznamu služeb tohoto jednoho souboru je pouze `app`; `edge` přidává
až druhý soubor. Základní soubor nemá profily, druhý port ani datové větve pro
porovnávání databází. První instalaci registruje instalační příkaz; DB automaticky
vytvoří první start přes existující migrace. Pozdější ztráta DB znamená STOP.

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
