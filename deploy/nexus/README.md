# AnimeDB na Nexusu — podklady N2, N4A a N4B1

Stav: **planned / not-deployed**. Soubory jsou připravené pouze v lokálním
repozitáři. Build, ARM64 runtime, aktivní CIFS mount, obnova DB ani začlenění
do záloh nejsou tímto podkladem ověřené. Aplikační startup se nemění a může
zapisovat do pracovní databáze.

Podklad vychází ze skutečného Nexus Project Cookbook v1.0,
`/srv/nexus/docs/PROJECT-STANDARD.md`, registru `/srv/nexus/PROJECTS.md`
a šablony `/srv/nexus/docs/templates/manifest.toml`, přečtených při N1.
Tento adresář není novým serverovým standardem. Před budoucí instalací
ověřit, zda se Cookbook nebo registr mezitím nezměnily.

| Lokální podklad | Budoucí umístění |
| --- | --- |
| Celý zdrojový repozitář | `/srv/nexus/projects/anime-db/src/` |
| `compose.yaml` | `/srv/nexus/stacks/anime-db/compose.yaml` |
| `.env.example` | šablona `/srv/nexus/stacks/anime-db/.env` |
| `manifest.toml`, tento `README.md`, `docs/` | `/srv/nexus/projects/anime-db/` |

`preflight.py` se při budoucím buildu vloží do image; na server se samostatně
neinstaluje. Kořenový `compose.yaml` slouží nadále původnímu lokálnímu provozu;
oba Compose soubory se **neslučují**.

- [Architektura a přesné cesty](docs/ARCHITECTURE.md)
- [Instalační podmínky a provoz](docs/OPERATIONS.md)
- [Stav přípravy](docs/CHANGELOG.md)
- [N4A SQLite snapshoty: rozhraní, validace a omezení](docs/SQLITE_BACKUP.md)
- [N4B1 adaptér existujícího Resticu a instalační prerequisites](docs/SQLITE_RESTIC.md)

`scripts/sqlite_backup.py` je samostatný lokálně testovaný helper pro budoucí
snapshoty explicitně očekávaných instancí (`--instances A`, později `A B`).
Na server nebyl instalován ani zapojen do Resticu. Nemá volbu libovolných cest,
neinicializuje B, neprovádí retenci a nepotvrzuje vzdálené uložení.

`scripts/sqlite_restic.py` je lokálně testovaný obal jednoho existujícího
Restic příkazu. Připraví explicitní instance, přidá výjimky a ověří konkrétní
snapshot ID i vzdálené bytes. Serverová integrace není instalovaná; původní
Nexus lock, OnFailure, InfluxDB a retence musí zůstat zachovány. Konkrétní
budoucí soubory a minimální patch jsou v N4B1 kontraktu, nikoli v nové službě.

Lokální validace bez Docker daemonu a bez spuštění aplikace:

```bash
docker compose --env-file deploy/nexus/.env.example \
  -f deploy/nexus/compose.yaml --profile a --profile b config -q
```

Instalace, registrace projektu na Nexusu, příprava DB i první start vyžadují
samostatný schválený krok. N2 žádný z nich neprovádí.
