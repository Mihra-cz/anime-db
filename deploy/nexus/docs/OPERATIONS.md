# Provoz jediné instalace AnimeDB

Instalace na Nexusu běží; datovaný stav a tři Compose soubory popisuje
[ARCHITECTURE](ARCHITECTURE.md). Aplikační služba se jmenuje `app`, HTTP vstup
`edge`. Aktualizace se provádí jen podle [RELEASE_UPDATE](RELEASE_UPDATE.md).
[FIRST_RUN](FIRST_RUN.md) je historický návod první instalace a na běžící
instalaci se nesmí použít. Kořenový vývojový Compose není deployment Compose.

## Podmínky případné nové instalace

Platí jen pro novou prázdnou instalaci, nikoli pro aktualizaci té stávající.

- Přečíst skutečný Cookbook v1.0, registr `/srv/nexus/PROJECTS.md` a ověřit
  projektový slug, nové prázdné cesty a schválený source tree.
- Ověřit ARM64, Docker oprávnění, lokální NVMe pro DB/artwork/state,
  non-root UID/GID a volnou adresu `192.168.11.238:8000`.
- Ověřit mount table bez aktivace NAS/autofs: skutečný CIFS
  `/mnt/nas-anime` ze zdroje `//192.168.11.149/Anime`, v nynější fázi host RO.
- Ověřit, že běžný kontejner obsahuje jen library RO a vlastní DB/state/artwork
  bindy. Všechny musí existovat předem, bez symlinků či aliasů.
- Pro běžný provoz s hodnotnými daty splnit Cookbook backup/restore podmínky:
  konzistentní snapshot, výjimky živé DB, ověřený Restic obsah a test obnovy.
  První schválený čistý smoke se za takovou produkční připravenost nevydává.

## Povinný LAN firewall gate

**NEOVĚŘENO.** Aplikace na LAN běží, ale dokumentace nemá doklad o splnění
tohoto gate a při read-only kontrole 2026-11-09 se pravidla nečetla, protože
vyžadují root. Jde o otevřený provozní bod. Soukromá host IP ani existující UFW
INPUT allowlist nejsou důkazem omezení publikovaného Docker portu.

Po samostatném schválení pouze read-only inventura hostu:

```bash
ip -4 address show
ip -4 route show
sudo nft -a list ruleset
sudo iptables-save
ss -ltn '( sport = :8000 )'
```

Podle skutečného Docker firewall backendu ověřit cestu FORWARD a pravidla
zpracovaná před Docker ACCEPT: u iptables typicky `DOCKER-USER`, u nftables
odpovídající forward hook/priority. Zkontrolovat skutečný domácí subnet
(poslední Cookbook uvádí `192.168.11.0/24`), ingress rozhraní, povolení domácí
LAN k publikované službě a zákaz ostatních zdrojů. Při posuzování cíle pravidel
zohlednit DNAT. Ověřit i absenci internetového port forwardu/tunelu a
nepublikování dalšího/IPv6 portu. Chybějící nástroj nebo nejasné pořadí pravidel
znamená neověřeno, nikoli souhlas ke startu.

Cílené omezení pro `192.168.11.238:8000` musí povolit pouze domácí LAN
`192.168.11.0/24`, přežít reboot a neovlivnit ostatní Docker služby.
Před LAN provozem doložit způsob trvalého načtení pravidla a po samostatně
schváleném rebootu znovu ověřit jeho přítomnost i účinnost ve forward cestě.
Dočasné pravidlo pouze v paměti nesplňuje gate. Žádné veřejné vystavení,
další port nebo změna firewallu se tímto dokumentem neprovádí.

Po spuštění v již omezené síti potvrdit dostupnost z povoleného LAN klienta
alespoň GET `/health` a podle možností odmítnutí z nepovolené sítě. Pokud
současná pravidla omezení nezajišťují, nejprve připravit konkrétní firewall
patch k samostatnému schválení; tento návod žádná pravidla nemění.

Technická opora: [Docker packet filtering](https://docs.docker.com/engine/network/packet-filtering-firewalls/)
a [Docker s iptables](https://docs.docker.com/engine/network/firewall-iptables/).
Dokumentace Dockeru výslovně rozlišuje forwarding publikovaných portů od
INPUT/OUTPUT filtrů hostitelského firewallu.

## Běžné příkazy

Všechny Compose příkazy musí používat **všechny tři soubory ve stejném pořadí**.
Samotný `compose.yaml` by vrátil neverzovaný image a znovu publikoval port, který
vlastní `edge`. Proměnné jsou stejné jako v [RELEASE_UPDATE](RELEASE_UPDATE.md);
`APP_URL` je skutečná interní adresa z místní konfigurace. Přístup k Dockeru
vyžaduje privilegovanou relaci, běžný SSH účet ho nemá.

```bash
dc=(docker compose -f "$BASE_COMPOSE" -f "$EDGE_COMPOSE" -f "$OVERRIDE")
"${dc[@]}" config -q
"${dc[@]}" ps -a
"${dc[@]}" logs --tail 80 "$APP_SERVICE"
"${dc[@]}" up -d --no-deps --no-build "$APP_SERVICE"
curl --fail --silent --show-error --max-time 5 "$APP_URL/health"
"${dc[@]}" stop "$APP_SERVICE"
```

`up -d` ani samotné `/health` nepotvrzují integritu DB či dostupnost NAS.
Preflight při chybě STOPne a `restart: no` zabraňuje opakování v chybové smyčce.
Scanner se nespouští při startupu; explicitní scan workflow tím není schválené.
Metadata, AniList, remote images a download artworku zůstávají vypnuté.

## První instalace, aktualizace a ztráta databáze

`nexus_first_run.py` je entrypoint **každého** startu `app`. Název není důkaz, že
se při aktualizaci znovu instaluje: novou instalaci smí založit jen výslovný
příkaz `provision` a následný první start nad prázdnými adresáři. Prostá absence
`anime.db` není souhlas s novou instalací.

**První instalace.** DB ani evidence neexistují a adresáře `db/` a `state/` jsou
nové a prázdné. Správce jednou spustí `provision` podle [FIRST_RUN](FIRST_RUN.md),
vznikne `provisioned.json`. První řádný start zapíše `initializing.json`, vytvoří
`anime.db` se schema v9, ověří ho, zapíše `ready.json` a spustí web.

**Běžná aktualizace.** DB i evidence existují. Mění se jen zdrojový checkout, image
a reference v override podle [RELEASE_UPDATE](RELEASE_UPDATE.md). Bindy zůstávají,
`provision` se nespouští. Start pouze validuje evidenci a DB. Při schema v9 je
`migrate_schema_at_startup` bez zápisu. Image s jinou startup schema verzí se
zastaví, migrace v9 na vyšší verzi vyžaduje samostatný postup.

**Databáze zmizela nebo je neplatná.** Současný kód se zastaví a nic nezaloží.
`DATABASE_URL` používá `mode=rw`, takže SQLite chybějící soubor nevytvoří ani
později za běhu. Sloupec „Co má udělat správce“ je doporučení, kód žádnou obnovu
neprovádí.

| Stav | Jak jej poznáme | Co aplikace udělá | Co má udělat správce |
| --- | --- | --- | --- |
| Neregistrovaná nová instalace | Chybí `provisioned.json` | STOP `Installation evidence missing or invalid; no automatic create`, DB nevznikne | Pokud jde opravdu o novou instalaci, jednou spustit `provision` |
| Registrováno, první start | `provisioned.json`, bez `initializing.json` a `ready.json`, `db/` prázdný | Založí DB v9, zapíše `initializing.json` a `ready.json`, spustí web | Ověřit logy a `/health` |
| Běžný start nebo aktualizace | Všechny tři JSON shodné, DB v9 platná | Validuje a spustí web, DB nezakládá ani nepřepisuje | Nic, žádný `provision` |
| **DB chybí v existující instalaci** | `ready.json` existuje, `anime.db` chybí | **STOP** `DB missing, lost or invalid; no automatic create` | Nespouštět `provision`, nezakládat prázdnou DB, zachovat evidenci, obnovu samostatně schválit |
| Nečitelná DB nebo soubor není SQLite | Selže otevření nebo čtení | STOP `DB missing, lost or invalid; no automatic create` | Zachovat soubor, diagnostikovat kopii |
| Poškozená DB | `integrity_check` nebo `foreign_key_check` neprojde | STOP `DB integrity check failed` nebo `DB foreign key check failed` | Zachovat soubor, diagnostikovat kopii |
| Nekompatibilní schema | `user_version` není 9 nebo tabulky neodpovídají modelu | STOP `First-run installation requires schema v9` nebo `DB schema tables differ; no automatic repair` | Vrátit předchozí image, žádná ruční migrace |
| Image s jinou startup verzí | `STARTUP_COMPATIBILITY_VERSION` není 9 | STOP `Review first-run support before a schema upgrade` | Vrátit předchozí image, upgrade řešit samostatně |
| SQLite sidecary | Existuje `anime.db-*` (journal, WAL, SHM) | STOP `SQLite sidecars require offline diagnosis; no automatic recovery` | Sidecary nemazat, diagnostikovat offline kopii |
| Chybějící nebo neplatná evidence | `provisioned.json` je neplatný, nebo při `ready.json` chybí `initializing.json` | STOP `Installation evidence missing or invalid; no automatic create` | Markery ručně nevytvářet ani nemazat |
| Rozpor evidence | `ready.json` nebo `initializing.json` neodpovídá `provisioned.json` | STOP `Installation evidence mismatch` | Zachovat soubory, diagnostikovat |
| DB bez dokončené registrace | `provisioned.json`, bez `initializing.json` a `ready.json`, ale `db/` není prázdný | STOP `Unexpected DB/files before initialization; no adoption` | Neadoptovat, zjistit původ souborů |
| Přerušená první inicializace | `initializing.json` bez `ready.json` | STOP `Initialization interrupted; preserve files for diagnosis` | Zachovat soubory, žádný retry |
| Opakovaný `provision` | `db/` nebo `state/` není prázdný | Odmítne `Provision requires empty DB/state directories; never repeat after data loss` | Neobcházet mazáním |
| Souběžná instance | `runtime.lock` drží jiný proces | STOP `Another instance is running (runtime lock)` | Ukončit druhou instanci, lock nemazat |

Kód nezaloží nový katalog, nezmigruje prázdnou DB a nezmění installation ID.
Úplné smazání DB **i veškeré evidence** správcem nelze odlišit od nové instalace.
Ověřený restore postup neexistuje.

## NAS účty a RW worker

Role účtů popisuje [ARCHITECTURE](ARCHITECTURE.md#umístění-dat-a-přístupová-práva).
Do Compose běžné aplikace se nikdy nepřidává RW mount knihovny ani přihlašovací
údaje role `NAS_IMPORT_USER`. Aktualizace aplikace RW workera nepřipravuje ani
nespouští; jeho příprava patří k samostatně schválené
[reorganizaci](../../../docs/V6_PRODUCTION_REORGANIZATION.md).

## Zálohy, změny a obnova

Cíl je jeden backup root `/srv/nexus/appdata/anime-db/backups/` s `daily/` a
`pre-change/`. Plánováno: snapshot ve 02:00, před významnou změnou DB a
převzetí hotových snapshotů stávajícím Resticem kolem 03:30. Žádný nový timer,
snapshot writer ani Restic patch nyní nevzniká. Běžný web tyto adresáře nemountuje.
Neexistuje ověřená obnova ani automatická retence.

Staré `scripts/sqlite_backup.py`, `scripts/sqlite_restic.py` a
`config/sqlite-backup.json.example` jsou **historické a neaktivní**. Nespouštět
je pro nové cesty a nenapojovat na existující Restic. Odpovídající
[SQLITE_BACKUP](SQLITE_BACKUP.md), [SQLITE_RESTIC](SQLITE_RESTIC.md) a
[N4B2](N4B2_COMPATIBILITY.md) nejsou současný instalační postup.

Před budoucí významnou změnou připravit konkrétní schválený snapshot a
návratový postup. Při chybě zastavit `app`, zachovat DB, všechny sidecary,
instalační state i logy a diagnostikovat nezávislou kopii. Žádné mazání markerů,
přepsání starou DB, opakování provision, reset volumes ani automatický recovery.
Ztracená DB po READY se nikdy nepovažuje za novou instalaci.

Manifest i registr upravit na skutečně nasazený stav až po doložení buildu,
startu/restartu, LAN omezení a požadované provozní připravenosti. Změny Linuxu,
firewallu, backupu, NAS a nasazení jsou samostatně schvalované operace.
