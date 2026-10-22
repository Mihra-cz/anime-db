# Provoz jediné instalace AnimeDB

Stav: **planned / not-deployed**. Aktivní služba se jmenuje `app`; celý stack
má jeden kontejner a jednu pracovní DB. Instalační příkazy jsou v
[FIRST_RUN](FIRST_RUN.md). Neprovádět je před samostatným schválením a LAN
firewall gate níže. Kořenový vývojový Compose není deployment Compose.

## Podmínky před instalací

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

**NEOVĚŘENO — před LAN startem STOP.** Aktuální pravidla se nyní nečetla
z Nexusu a žádná se neměnila. Soukromá host IP ani existující UFW INPUT
allowlist nejsou důkazem omezení publikovaného Docker portu.

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

## Běžné příkazy po schválené instalaci

Přesná konfigurace je v `/srv/nexus/stacks/anime-db/compose.yaml` a `.env`.
Příkazy nevyžadují žádný profil:

```bash
cd /srv/nexus/stacks/anime-db
dc=(docker compose --env-file .env -f compose.yaml)
"${dc[@]}" config -q
"${dc[@]}" up -d --no-build app
"${dc[@]}" ps -a
"${dc[@]}" logs --tail 80 app
curl --fail --silent --show-error --max-time 5 http://192.168.11.238:8000/health
"${dc[@]}" stop app
```

První start má navíc jednorázový instalační provision podle FIRST_RUN.
`up -d` ani samotné `/health` nepotvrzují integritu DB či dostupnost NAS.
Preflight při chybě STOPne a `restart: no` zabraňuje opakování v chybové smyčce.
Scanner se nespouští při startupu; explicitní scan workflow tím není schválené.
Metadata, AniList, remote images a download artworku zůstávají vypnuté.

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
