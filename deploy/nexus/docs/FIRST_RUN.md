# První čistý start AnimeDB na Nexusu

Stav: lokálně připraveno, **nenasazeno**. Tento postup vybírá jedinou čistou
instalaci se službou `app` a DB schema v9. Tento návod nahrazuje historické
instalační podklady. N5B, návrh N5C ani jejich rozpracované soubory se
nemění. R1 export skutečné notebookové v9 zůstává neověřený; čistý start na
něm nezávisí. Nevytváří se další migrátor ani backup framework.

**Všechny následující instalační příkazy jsou pouze k samostatnému schválení.
Příprava tohoto dokumentu je nespouští.** Použít pouze Nexus `compose.yaml`;
kořenový vývojový Compose nepřidávat jako další `-f` soubor.

## 1. Hostitelské podmínky a povinný LAN gate

Před zápisem přečíst aktuální `/srv/nexus/docs/PROJECT-STANDARD.md` a
`/srv/nexus/PROJECTS.md`, ověřit volné projektové cesty a Docker oprávnění.
Existující adresáře/databáze nepřepisovat ani nepřebírat jako novou instalaci.
Zdroje v `src` musí obsahovat **schválené lokální změny tohoto úkolu**;
samotný nynější `origin/main` je neobsahuje. Přenos zdrojů bude samostatný
schválený instalační krok, bez notebookové DB, `.env`, artworku a `.git`.

Pouze čtení lokálního mount table, bez průchodu NAS:

```bash
uname -m
findmnt --kernel --nocanonicalize -rn -T /srv/nexus -o TARGET,SOURCE,FSTYPE,VFS-OPTIONS
findmnt --kernel --nocanonicalize -rn -t cifs -o TARGET,SOURCE,FSTYPE,VFS-OPTIONS,FS-OPTIONS
awk '$5=="/" || $5 ~ "^/srv(/|$)"' /proc/self/mountinfo
ip -4 address show
ss -ltn '( sport = :8000 )'
```

Vyžaduje se ARM64 (`aarch64`), `/srv/nexus` na lokálním `/dev/nvme…`
filesystemu ext4/xfs/btrfs s RW přístupem, přítomná LAN adresa
`192.168.11.238` a volný port 8000.
Ve výpisu surového mountinfo ověřit skutečný zdroj za oddělovačem ` - `
pro `/` a mount pokrývající `/srv/nexus`, včetně případných samostatných
mountů `/srv` či `/srv/nexus`. Samotný findmnt může uvádět `/dev/root`;
nenahrazuje kontrolu surového zdroje, který čte také container preflight.
Pokud ani surový záznam neprokáže `/dev/nvme…`, STOP a read-only diagnostika
skutečného RPi. Bez doložené chyby neměnit `first_run.py` ani gate obcházet.
Před jakýmkoli `up` musí být samostatně ověřené omezení domácí LAN v Docker
forward firewall cestě včetně persistence po rebootu podle
[OPERATIONS](OPERATIONS.md#povinný-lan-firewall-gate).
**Dosud NEOVĚŘENO: do ověření STOP.** Firewall se tímto postupem nemění.
Samotná privátní IP ani UFW INPUT pravidlo nejsou dostačující důkaz.

NAS musí již mít aktivní CIFS mount `/mnt/nas-anime` ze zdroje
`//192.168.11.149/Anime`; v současné fázi host VFS RO. Samotný autofs nestačí.
Chybějící mount znamená STOP; tento postup nekonfiguruje Linux ani mount
neaktivuje. Container bind je vždy RO, i při FS/superblock `rw`.

## 2. Projektové adresáře a konfigurace

Po schválení, na Nexusu, pro dosavadní runtime UID/GID `1000:1000`:

Tato hodnota odpovídá `ANIMEDB_UID=1000` a `ANIMEDB_GID=1000` v dodané
`.env.example`. Instalační `.env`, build args, runtime `user`, vlastník
DB/state/artwork i provision `--user` musí souhlasit. Před buildem zkontrolovat
rozbalené `compose config`; při odlišné konfiguraci nejprve sladit příkazy,
bez zakládání dalších uživatelů.

```bash
sudo install -d -m 0755 /srv/nexus/projects/anime-db/src /srv/nexus/stacks/anime-db
sudo install -d -m 0755 /srv/nexus/appdata/anime-db
sudo install -d -o 1000 -g 1000 -m 0700 \
  /srv/nexus/appdata/anime-db/db \
  /srv/nexus/appdata/anime-db/state \
  /srv/nexus/appdata/anime-db/artwork
sudo install -d -m 0700 /srv/nexus/appdata/anime-db/backups \
  /srv/nexus/appdata/anime-db/backups/daily \
  /srv/nexus/appdata/anime-db/backups/pre-change
```

Přesný přenos schváleného pracovního stromu, nejprve na notebooku v kořeni
tohoto repozitáře (historické N5B/N5C moduly první start nepotřebuje):

```bash
tar -czf /tmp/anime-db-first-run-src.tar.gz \
  --exclude=__pycache__ --exclude='*.pyc' \
  --exclude=app/installation_state.py --exclude=app/tools/nexus_db.py \
  --exclude=deploy/nexus/docs/N5B_OFFLINE_DB.md \
  --exclude=deploy/nexus/docs/N5C_STARTUP_DESIGN.md \
  app Dockerfile .dockerignore pyproject.toml README.md LICENSE \
  deploy/nexus/preflight.py deploy/nexus/first_run.py \
  deploy/nexus/compose.yaml deploy/nexus/.env.example deploy/nexus/manifest.toml \
  deploy/nexus/README.md deploy/nexus/docs
scp /tmp/anime-db-first-run-src.tar.gz nexus:/tmp/anime-db-first-run-src.tar.gz
```

Potom na Nexusu do ověřeného nového, prázdného `src`:

```bash
sudo tar -xzf /tmp/anime-db-first-run-src.tar.gz \
  --no-same-owner -C /srv/nexus/projects/anime-db/src
sudo install -m 0644 \
  /srv/nexus/projects/anime-db/src/deploy/nexus/compose.yaml \
  /srv/nexus/stacks/anime-db/compose.yaml
sudo install -m 0644 \
  /srv/nexus/projects/anime-db/src/deploy/nexus/.env.example \
  /srv/nexus/stacks/anime-db/.env
sudo install -m 0644 /srv/nexus/projects/anime-db/src/deploy/nexus/manifest.toml \
  /srv/nexus/projects/anime-db/manifest.toml
sudo install -m 0644 /srv/nexus/projects/anime-db/src/deploy/nexus/README.md \
  /srv/nexus/projects/anime-db/README.md
sudo install -d -m 0755 /srv/nexus/projects/anime-db/docs
sudo install -m 0644 /srv/nexus/projects/anime-db/src/deploy/nexus/docs/*.md \
  /srv/nexus/projects/anime-db/docs/
namei -l /srv/nexus/appdata/anime-db/db \
  /srv/nexus/appdata/anime-db/state /srv/nexus/appdata/anime-db/artwork
stat -Lc '%F %d:%i %U:%G %a %n' /srv/nexus/appdata/anime-db/db \
  /srv/nexus/appdata/anime-db/state /srv/nexus/appdata/anime-db/artwork
```

Žádná komponenta cest nesmí být symlink a adresáře musí mít různé device/inode
páry. DB i state musí být při nové instalaci prázdné. Nevytvářet `anime.db`
pomocí `touch`, SQLite CLI ani kopie notebookové DB. Zde se nezakládá reference,
backup služba ani další instance. Nepřepisovat existující konfiguraci.

## 3. ARM64 build

Příkazy dále používat v jednom Bash shellu. Instalační `.env` obsahuje pouze
UID/GID a neobsahuje konfigurovatelný druhý port:

```bash
cd /srv/nexus/stacks/anime-db
dc=(docker compose --env-file .env -f compose.yaml)
"${dc[@]}" config -q
"${dc[@]}" config --services
"${dc[@]}" build app
docker image inspect anime-db:nexus --format '{{.Os}}/{{.Architecture}}'
```

Seznam služeb musí obsahovat pouze `app`; image musí být `linux/arm64`.
Build je nativní na Raspberry Pi 5; nevyžaduje změnu Linuxu nebo QEMU.
Build vyžaduje dostupný base image, Debian balíčky a Python dependencies.
Nebyly nyní staženy ani sestaveny na Nexusu.

## 4. Jednorázová registrace nové instalace

Instalátor zaznamená důkaz první instalace mimo DB. Tento krátký kontejner
nemá NAS, síť, artwork ani aplikační startup; žádnou DB nevytváří:

```bash
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true --user 1000:1000 \
  --mount type=bind,src=/srv/nexus/appdata/anime-db/db,dst=/data \
  --mount type=bind,src=/srv/nexus/appdata/anime-db/state,dst=/state \
  --entrypoint python anime-db:nexus \
  /usr/local/libexec/anime-db/nexus_first_run.py provision
```

Očekává se `PROVISIONED anime-db`. Příkaz není běžný startup a po první registraci
se neopakuje. Odmítá neprázdný DB/state adresář. `provisioned.json` nenahrazuje
zálohu; při ztrátě nebo poškození evidence STOP, bez ručního mazání markerů.

## 5. První start a LAN smoke

Tento běžný startup smí proběhnout až po potvrzení LAN firewall gate včetně
persistence po rebootu. Compose publikuje pouze `192.168.11.238:8000:8000`;
hostitelský `localhost:8000` není jeho smoke endpoint. Skutečný LAN smoke
dosud neproběhl. Volitelný postup bez publikování portů zde není ověřený.

```bash
"${dc[@]}" up -d --no-build app
"${dc[@]}" ps -a
"${dc[@]}" logs --tail 80 app
docker inspect "$("${dc[@]}" ps -aq app)" \
  --format '{{range .Mounts}}{{println .Source "->" .Destination "RW=" .RW}}{{end}}'
curl --fail --silent --show-error --max-time 5 http://192.168.11.238:8000/health
curl --fail --silent --show-error --max-time 5 --output /dev/null http://192.168.11.238:8000/
"${dc[@]}" exec -T app python -c 'import sqlite3; c=sqlite3.connect("file:/data/anime.db?mode=ro", uri=True); v=c.execute("PRAGMA user_version").fetchone()[0]; i=c.execute("PRAGMA integrity_check").fetchall(); f=c.execute("PRAGMA foreign_key_check").fetchall(); n=c.execute("SELECT count(*) FROM videos").fetchone()[0]; print("schema",v,"integrity",i,"FK",f,"videos",n); assert v==9 and i==[("ok",)] and f==[] and n==0; c.close()'
```

`up -d` nepotvrzuje připravenost; po startup logu se očekává HTTP 200 a
`{"status":"ok"}`. Případný časný curl lze zopakovat až po kontrole logu.
Kontrola mountů musí ukázat NAS `RW=false`, samostatné DB/state/artwork
`RW=true`, bez jiných host bindů. `/health` sám zdraví DB/NAS neověřuje,
proto je DB kontrola explicitní. Neklikat na scan ani neposílat `POST /scan`.

Preflight nejprve ověří konfiguraci, non-root, CIFS RO a NVMe bindy.
První start zapíše a fsyncne `initializing.json` **před** vytvořením DB,
použije stávající `migrate_schema_at_startup`, ověří v9, integritu/FK a
tabulky a teprve potom zapíše `ready.json` a spustí Uvicorn. Neprovádí scan.
Metadata enrichment, AniList i stahování/vzdálené obrázky jsou vypnuté.

`state/` je zachovaná minimální instalační evidence oddělená od DB; pomáhá
rozpoznat ztrátu již existující DB. Snapshot adresáře se do kontejneru
nepřipojují. Jejich budoucí použití ve 02:00 / před významnou změnou a
převzetí hotových snapshotů Resticem kolem 03:30 zatím nejsou implementované.

## 6. Vypnutí a opakovaný start

```bash
"${dc[@]}" stop app
sudo sha256sum /srv/nexus/appdata/anime-db/db/anime.db
sudo stat -c '%s %Y %n' /srv/nexus/appdata/anime-db/db/anime.db
"${dc[@]}" up -d --no-build app
"${dc[@]}" logs --tail 40 app
curl --fail --silent --show-error --max-time 5 http://192.168.11.238:8000/health
curl --fail --silent --show-error --max-time 5 --output /dev/null http://192.168.11.238:8000/
"${dc[@]}" stop app
sudo sha256sum /srv/nexus/appdata/anime-db/db/anime.db
sudo stat -c '%s %Y %n' /srv/nexus/appdata/anime-db/db/anime.db
```

Bez dalších UI změn musí fingerprint/velikost/mtime zůstat stejné. Poslední
příkazový blok nechává instanci vypnutou. Po schválení dalšího běhu stačí
znovu `"${dc[@]}" up -d --no-build app`, bez provision.

Při READY a chybějící DB aplikace STOPne. Všechna její SQLite připojení
používají `mode=rw` přímo z Compose konfigurace, takže ztráta DB mezi preflightem a connectem ani při
novém pool connection nesmí vytvořit soubor. Smazání/přepsání správcem během
živé transakce není podporovaný workflow. Souběžný runtime odmítne flock,
který jediný Uvicorn proces drží až do ukončení. Nepoužívat workers/reload.

## Omezení a skutečné blokéry

- ARM64 image, skutečné bindy/UID a LAN start na Nexusu dosud nejsou
  ověřené. Blokuje neověřený LAN forward firewall, chybějící schválený source tree/image, neúspěšný build,
  jiný disk než NVMe, chybné mounty/práva nebo obsazený LAN port.
- Chybějící/poškozená evidence, přerušená inicializace, SQLite sidecary,
  chybějící/neplatná DB nebo jiné schema znamenají STOP a zachování souborů.
  Neexistuje automatický retry, reset, restore, adoption ani upgrade.
- Důkaz první instalace je explicitní registrace správce a samostatný trvalý
  state adresář. Úplné smazání/rollback DB **i celé instalační historie**
  správcem nelze těmito lokálními markery rozpoznat. Nejde o pokročilý N5C
  coordinator, storage UUID attestation nebo ochranu proti host rootovi.
- Konzistentní backup/restore a zapojení Resticu nejsou tímto zavedené.
  Landlock omezení staršího backup helperu nebrání tomuto čistému smoke testu;
  běžný provoz s hodnotnými daty vyžaduje samostatně vyřešenou zálohu.
- R1 notebookové v9 není blocker čisté instalace. V6 executor, P2C,
  Completeness a další historické migrace nejsou součástí tohoto kroku.
