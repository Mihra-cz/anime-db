# V6 – reorganizace existující knihovny

Administrativní vstup `python -m app.tools.reorganize` používá současné E7,
E1 journal, E6B maintenance, identity evidence, locator-only SQLite transakce,
E7H cleanup a guard SCANNING → NORMAL. Nevytváří knihovnu, SQLite, instalační
identitu ani schema. SQLite musí být existující samostatná v9 DB v DELETE
journal mode, bez WAL/SHM/hot journalu. Nesouhlas znamená STOP, bez migrace.

Implementace je připravena pro lokální syntetické ověření a nezávislé review.
Samotná existence vstupu ani testy nejsou povolením k produkční execution.
Nexus deployment, serverová oprávnění a capability skutečného mountu musí být
ověřené a schválené před prvním provozním během. NAS power-loss durability
není potvrzená úspěšným návratem `fsync`.

## Existující instalace a izolace přístupů

Datovaný stav k 2026-11-09 popisuje [architektura instalace](../deploy/nexus/docs/ARCHITECTURE.md):
služby `app` a `edge`, verzovaný image podle lokálního V6 override, SQLite `DB_FILE`,
schema v9 a persistentní artwork. Před během je nutné běžící image a případné
lokální úpravy znovu zjistit read-only. Tag image nedokazuje jeho commit.
N5B/N5C pracovní návrhy nejsou dokladem skutečně nasazeného startupu.

- Běžná aplikace používá NAS účet role `NAS_SCAN_USER` se serverovými RO právy
  a pouze RO media bind. Scanner a ffprobe čtou NAS; scanner zapisuje SQLite.
  Kontejner aplikace nikdy nedostává RW přihlašovací údaje.
- Administrativně spouštěný worker používá samostatný účet role `NAS_IMPORT_USER`
  a jediný schválený RW CIFS mount. SOURCE i TARGET procházejí tímto mountem.
  Účet existuje, ale k 2026-11-09 RW worker ani jeho mount nasazené nejsou;
  samotná existence účtu reorganizaci nepovoluje.
- Credentials a RW mount nejsou dostupné webu. Správu mountu provádí správce
  hostitele; worker nevolá mount helper, Docker API ani obecné sudo.
- Worker používá stejný ověřený image s entrypointem `python -m
  app.tools.reorganize`, nikoli webový First Run entrypoint. Aplikace i worker
  musí používat stejné absolutní vnitřní cesty `/data/anime.db` a
  `/media/anime`; schválený manifest a historie jsou s těmito cestami svázané.
  Runtime UID/GID obou procesů musí být kompatibilní s vlastnictvím a právy
  sdílené lokální SQLite, execution evidence a runtime locku. NAS účet není
  Linux UID/GID. Současné private evidence kontroly vyžadují stejný efektivní
  UID jako vlastník SQLite/evidence a zachycených identity záznamů; samotná
  přístupová práva přes skupinu tento kontrakt nenahrazují.
- SQLite a execution artefakty zůstávají lokálně na NVMe, mimo media root.
  E7 je nadále ukládá vedle explicitně zadané SQLite; `/state` zůstává
  instalační state a runtime lock. Artwork se reorganizace netýká.

Aktualizuje se existující image/kontejner při zachování datových bindů.
Neopakovat provision, nevytvářet prázdnou DB a neměnit installation identity.
Web i worker musí obsahovat kompatibilní guard/NORMAL implementaci.
RO a RW pohled mohou používat různé hostitelské mounty a mít rozdílné
`st_dev`/inode, musejí však zpřístupňovat tentýž Synology server/share/podstrom
na shodné vnitřní cestě `/media/anime`. SQLite, lokální execution evidence
a runtime lock musejí být v obou prostředích skutečně sdílené, nikoli kopie.

## Chráněné namespaces

Rootový `IMPORT` je rezervovaný po normalizaci první komponenty cesty.
Scanner jej neprochází pro videa ani titulky; případné již uložené katalogové
locators pod IMPORT vyžadují explicitní review místo automatického SQL mazání.
`Show/Import` ani `IMPORTed` nejsou rootový IMPORT.

Reorganizace nepřijímá IMPORT jako SOURCE/TARGET, anime root ani cílový parent.
Inventory označí jeho adresář za `system_excluded` bez průchodu obsahem.
IMPORT nesmí být cleanup kandidát, ani když je prázdný.

Potvrzené duplicate secondary používají současný D04 QUARANTINE do Duplicates.
Zachovávají ID, rows i související evidence; V6 neimplementuje fyzické mazání
videí ani titulků. Directory cleanup chrání Duplicates včetně celého podstromu,
Subs a systémové namespaces. Root Duplicates zůstává i po budoucím vyprázdnění.

## Approval a worker

Nejprve se připraví aktuální preview a finální v2 manifest svázaný s cestami
a mountem workeru. Schválí se celý canonical envelope včetně jednotlivých
warning acknowledgements. Schválení je SHA-256 `manifest_to_json(manifest)`
v UTF-8, bez přidaného koncového newline; CLI akceptuje canonical soubor
buď bez newline, nebo s jediným newline. Worker manifest nepřesealuje.

Provozní vstup vyžaduje existující deployment `runtime.lock`. Správce musí
ověřit, že jde o totožný lokální soubor, který drží běžící aplikace. Jiný
lock soubor ani samotné capability booleany neprokazují zastavení writerů.
Před prvním během se odstaví celá aplikace, související úlohy a ostatní
dotčení NAS writeři. Worker drží runtime výluku až přes NORMAL; E7 a completion
používají stávající E6B leases. Nezúčastněné SQLite klienty a vzdálené NAS
klienty advisory flock neblokuje.

Příklad příkazu **pro pozdější samostatně schválené spuštění na Nexusu**,
uvnitř izolovaného prostředí s již připravenými bindy:

```bash
python -m app.tools.reorganize \
  --db /data/anime.db --library-root /media/anime \
  --share //192.168.11.149/Anime --share-root / \
  --manifest /data/approved-manifest.json \
  --approve-manifest-sha256 SCHVALENY_CANONICAL_SHA256 \
  --runtime-evidence /data/runtime-evidence.json \
  --runtime-lock /state/runtime.lock \
  --windows-root '\\192.168.11.149\Anime' --actor SPRAVCE
```

Příkaz sám nepřipojuje share, nezastavuje služby, nevytváří zálohu/snapshot
a neposkytuje RW credentials. Vstupní runtime JSON používá dosavadní kontrakt
přesně 14 externích polí. Mount identity, RO stav a volné místo se zjišťují
fresh; externí assertions je nepřebijí. Úplný preflight musí vrátit
`READY_FOR_EXECUTION`. Snapshot i nezávislá DB backup kopie musí být navázané
na finální manifest; snapshot timestamp musí vyhovovat existujícímu preflightu.

Scratch allowlist se nemění. Produkční media policy navíc ověřuje explicitní
CIFS server/share/podstrom, mount ancestry, nepřítomnost aliasů a crossingů,
RW stav a držené descriptors. State používá lokální filesystem policy.

Pro manifest schválený ve webu používejte [hostitelský `execute` schvalovacího
nástroje](V6_WEB_APPROVAL.md#explicitní-hostitelský-start), který spotřebuje právě
toto schválení, uvolní kontrolní runtime lock a zavolá stejný E7 vstup. Dosavadní
`accept` se stavem `ACCEPTED_EXECUTION_DISABLED` neposkytuje start authority.
Původní přímý CLI příklad výše zůstává samostatným administrativním vstupem;
není návodem obcházet jednorázový webový ledger nebo opakovat nejasný běh.

Hostitelský `execute` spouštějte v `tmux` nebo `screen` s přímo připojeným
terminálem na stdin i stdout a s dostupným Python `readline`. CLI ověří a načte
`readline` ještě před claimem; bez něj se dlouhý JSON cleanupu může v kanonickém
terminálu oříznout. **Nepoužívat `| tee` ani přesměrování stdout**; CLI takový
start odmítne před execution. Původní journal a výsledkový report zůstávají
uložené nezávisle na terminálovém výpisu.

Pro živý přehled během E7 slouží [nasazený HTTP edge](../deploy/nexus/service-edge/README.md),
který vlastní LAN port 8000. Publikace průběhu workerem ani servisní stránka
během reálného E7 zatím ověřené nejsou.

## Cleanup a dokončení

Po všech MOVE/KEEP/QUARANTINE se nejprve ověří původní finální post-state.
E7H potom sestaví aktuální cleanup proposal. CLI jej uloží jako nový lokální
`<db-stem>.cleanup-proposal.json`, vypíše kandidáty a čeká na explicitní JSON
seznam konkrétních cest a potvrzení SHA-256 proposal. Prázdný seznam všechny
adresáře zachová. EOF, nesouhlas nebo neplatný approval znamenají STOP.

Povoleny jsou pouze původně inventarizované, již nepotřebné a výslovně
schválené adresáře. Potřebné target parents a chráněné namespaces jsou
vyloučené. E7H používá pouze `rmdir`, nofollow identity, skutečnou prázdnost,
file/directory fsync a původní append-only cleanup evidence. Není zde
rekurzivní delete, unlink média ani automatické odstranění duplicit.

Completion ověří E7F původní directory authority, úplný E1 journal a E7H
chain/approval/delta, finální SQLite a metadata skutečného post-cleanup filesystemu.
Obsahové SHA se ověřují pouze u výslovně vybraných FULL souborů. Jen v paměti doplní pro kontrolu původního `expected_post_hash`
odstraněné directory entries. Původní manifest a hash se nemění. SCANNING
guard váže SHA-256 cleanup evidence; NORMAL zachovává celou historii.

Worker dokončí NORMAL ve svém RW pohledu. Historické execution pins se potom
ověřují proti zachycenému bindingu, zatímco běžná aplikace prokazuje správný
server/share/podstrom svého RO pohledu. Živé `st_dev`, inode ani mount ID
dvou nezávislých mountů se nesrovnávají jako jedna fyzická identita. Běžný
scan po NORMAL používá současné locators a zachovává známá ID/lidské autority.

Běžný worker používá `METADATA_RENAME` a nečte obsah videí. E3 v2 obsahuje
`content: null`; E7H v2 má metadata snapshoty a directory delty bez fiktivních
SHA. Původní FULL evidence zůstává čitelná. Volitelné opakovatelné
`--verify-content SOURCE_RELATIVE_PATH` ověří obsah pouze výslovně zvolených
manifestových zdrojů. Bez této volby se neprokazuje bytová shoda před/po ani
odhalení přepisu se všemi shodnými metadaty. SOURCE/TARGET, dev/inode,
size/mtime/ctime, nofollow, fsync, journal, guard a locator-only DB se ověřují dál.
[Podrobný kontrakt](V6_EXECUTION_MANIFEST.md#e7-metadata_rename-a-životnost-deskriptorů)
popisuje kontrolní body a jejich omezení. Změny obsahu IMPORT/#recycle
neinvalidují cleanup; jejich adresářová identita zůstává chráněná. Provozní
zákaz souběžných writerů platí dál.

## STOP a provozní ověření

Přerušení, nejistý syscall/commit/fsync, ztracená výluka nebo neplatná evidence
znamenají STOP. Neopakovat worker, nepřepisovat DB, nemažte marker ani sidecary.
Zachovat skutečné soubory, SQLite včetně sidecarů, manifest, E1, guard, E7F,
identity a E7H artefakty. Při sidecarech nespouštět běžný startup ani implicitní
SQLite recovery. Historická inode continuity po zániku procesu je UNKNOWN;
shodný hash není oprávnění k retry, resume nebo rollbacku.

Read-only kontrola 2026-11-09 doložila aktivní verzi, entrypoint, UID/GID, bindy
aplikace, existující v9 DB a instalační evidenci. RW pohled pro worker na hostiteli
zatím neexistuje. Před provozním povolením zbývá aktuální inventura těsně před
během, ověření workeru, shodných vnitřních cest, sdílené lokální evidence, runtime
locku a writerů a kontrola záloh.
Při tomto budoucím nasazení je nutné ověřit skutečné serverové ACL role
`NAS_SCAN_USER` jako RO a role `NAS_IMPORT_USER` jako RW i jejich oddělení mezi
aplikací a workerem; současná dokumentace to neprokazuje. Zbývá také schválené
ověření skutečné CIFS capability. Reprezentativně se změří čas, SHA průchody,
I/O, paměť, FD a evidence; rozsáhlá optimalizace se nedělá bez zjištěného problému.

V7 Import UI a řízené fyzické odstranění schválených duplicit jsou backlog.
Tento vstup importní batch ani DELETE akce nepřijímá.
