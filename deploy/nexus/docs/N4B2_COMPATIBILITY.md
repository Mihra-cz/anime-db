# N4B2 — kompatibilita RPi5 a oddělená write authority

> **Historický podklad N4B2 — neaktivní.**
> Zachovaný návrh původního srovnávacího deploymentu, nikoli aktuální návod.
> CIFS RO kontrola se dále využívá, budoucí write/backup návrhy se neaktivují.
> Aktuální provoz: [OPERATIONS](OPERATIONS.md), [FIRST_RUN](FIRST_RUN.md).

Stav: **planned / not-deployed**. Lokálně je opravený CIFS preflight a jeho
regrese. SQLite alternativa a produkční V6/V7 write procesy níže jsou pouze
návrh. Nexus se přizpůsobuje aplikací, nikoli změnou Linuxu či kernelu.
Tento dokument určuje Nexus deployment hranice; nepřepisuje roadmapu, V6
naming/manifest contract ani neautorizuje operaci na NAS.

## NAS Anime ve třech fázích

| Fáze | NAS Anime účet | Host mount | Běžná A/B | Zápis do knihovny |
| --- | --- | --- | --- | --- |
| 1 — nyní | RO | RO | RO bind | Žádný |
| 2 — schválená reorganizace V6 | RW, trvale | RW, trvale | Stále RO bind | Jen samostatný schválený V6 executor pro konkrétní manifest |
| 3 — běžný provoz po V6 | Zůstává RW | Zůstává RW | Stále RO bind | Jen samostatný V7 executor po schválení konkrétního importu |

Současné **RO/RO** zde znamená NAS účet / host mount. Neznamená požadavek
`VFS=ro / FS=ro` v mountinfo. Kernel rozlišuje per-mount VFS options
a per-superblock FS options; běžný kontejner může bezpečně používat RO bind
nad RW hostem/superblockem. `FS=rw` samo nevypovídá o SMB oprávněních účtu.
Účet/ACL se ověřují samostatnou administrátorskou evidencí, nikoli odhadem
z mountinfo nebo pokusem o zápis na knihovnu.
Podklad: [mountinfo](https://man7.org/linux/man-pages/man5/proc_pid_mountinfo.5.html)
a [Docker read-only bind](https://docs.docker.com/engine/storage/bind-mounts/#use-a-read-only-bind-mount).

NAS Zálohy (`/mnt/nas-backup`) je nezávislé RW úložiště se samostatným RW účtem
pro existující Restic. Není knihovnou Anime, bindem webu ani pracovním SQLite
úložištěm. Jeho účty, ACL, mount a workflow se nemění. Pracovní DB A/B,
artwork/cache, reference a lokální snapshoty zůstávají na SSD podle
[architektury](ARCHITECTURE.md).

## Rozdělení preflight a Docker kontraktu

`preflight.py` před čtením pracovní DB a před předáním commandu kontroluje
pouze vlastní `/proc/self/mountinfo`, bez přístupu do NAS stromu:

- Přesně jeden mount na `/media/anime`, skutečný `cifs`, root share `/`,
  přesný source `//192.168.11.149/Anime`. Pokud je uvedeno `addr`, musí souhlasit
  s `192.168.11.149`; prefix podadresáře není schválená celá knihovna.
- VFS musí být jednoznačně `ro`. FS `ro` i `rw` jsou přípustné. VFS `rw`,
  autofs bez CIFS, chybějící/stacked mount, jiný source nebo nečitelná či
  poškozená mount tabulka znamenají STOP.
- Žádné další mounty uvnitř library. Odmítá se RW alias stejného device,
  rozpoznané RW typy `cifs`, `smb3`, `nfs`, `nfs4`, `smbfs`, `9p`, `virtiofs`,
  `ceph`, `cephfs`, `glusterfs`, `afs` a konzervativně každý RW `fuse.*`
  (včetně sshfs/rclone i případných lokálních FUSE podtypů). Není to úplná
  detekce všech možných síťových protokolů. NAS Backup do webu nepatří.
  RO alias mimo library není write authority; běžné lokální Docker mounty
  pro RW DB/artwork/tmp zůstávají povolené.
- Kernelové escape sekvence se dekódují; duplicitní mount IDs a nejednoznačné
  permission options jsou STOP. Parent mount chain musí odpovídat předkům
  library cesty; CIFS skrytý překrývajícím ancestor mountem nebo cyklická
  ancestry nejsou důkaz aktivní knihovny. Žádný fallback na lokální adresář.

Docker požadavek `bind.read_only=true` nelze z kontejnerového mountinfo
zpětně prokázat jako původní Docker parametr. Proto jsou důkazy rozdělené:
rozbalený Compose a instalační kontrola Docker `Mounts` musí doložit
`Source=/mnt/nas-anime`, `Destination=/media/anime`, `RW=false`; guard navíc
ověřuje skutečné VFS a aliasy. Do webu se kvůli inspekci nepřidává Docker
socket, credentials ani právo ovládat daemon.

Oba běžné profily mají trvale `read_only: true` u library bindu,
`propagation: rprivate`, `create_host_path: false`, non-root UID,
`cap_drop: ALL` a `no-new-privileges`. Testy kontrolují rozbalenou Compose
konfiguraci včetně absence dalších host bindů. Root filesystem kontejneru
je také RO; to je samostatná ochrana, nikoli náhrada RO library bindu.
Trusted správce/daemon mohou konfiguraci změnit; guard není ochrana před
škodlivým host rootem ani průběžný monitor mountů. Nepovolené overrides a
`--entrypoint` nejsou podporovaný způsob startu.

## Budoucí V6 a V7 write procesy — návrh, ne implementace

Dosavadní [V6 manifest](../../../docs/V6_EXECUTION_MANIFEST.md), planner
a `app/tools/execution_manifest.py` oddělují read-only generation/dry-run od
execution. `app/target_execution_filesystem.py` otevírá jen nově vytvořený
scratch; `app/target_execution_mounts.py` nepovoluje CIFS. E6B maintenance
a E7 sekvenční execution jsou scratch foundations, nikoli nasaditelný NAS
executor. [V7](../../../docs/ROADMAP.md#v7--bezpečný-import-a-deduplikace)
je plánovaný a začíná po closure V6. Tento model nevyžaduje RW NAS ve webu;
produkční write orchestrace a maintenance zapojení ještě chybějí.

Nejmenší navazující model je samostatně spuštěný krátkodobý executor proces
nebo kontejner mimo Compose služby A/B. RW mount dostane pouze tento job
pro schválený root/nezbytné podstromy; A/B se kvůli němu nepřekonfigurují.
Nemá být běžící daemon s obecnou write API ani automatický sidecar webu.

Schválení V6 se váže na konkrétní immutable manifest hash, aktuální
DB/filesystem baseline, source/target identity, konečný seznam operací,
warning acknowledgements a actor/time evidenci. RW host mount nebo úspěšný
dry-run nejsou schválení. Job znovu ověří stale stav, mount/inode identity,
kolize, bezpečný no-overwrite přesun, backup/snapshot a produkční maintenance
boundary. V6 musí po dobu execution i verification vyloučit scanner/inventory
a další dotčené DB writery. Nynější scratch lock toto na produkci nezajišťuje.
Lokální DB reconciliation je oddělená explicitní write authority; nemění
historical filenames nebo business autority mimo schválený locator contract.

V7 později použije vlastní schválení konkrétního importního batche, evidence
zdrojů a jejich hashů, přesných cílů, kolizí/duplicit a seznamu povolených
operací. Nesmí přebírat blanket souhlas z V6. Zdroj importu může mít RO bind,
cílový job jen potřebný RW rozsah. Replacement/delete potřebují své konkrétní
schválení; nedostanou se do běžného scan workflow. Podrobný importní executor
není touto specifikací navržen ani implementován.

Po ukončení nebo chybě job ztratí RW přístup ukončením svého namespace;
host a NAS účet zůstávají RW podle trvalého rozhodnutí. Nejasný výsledek
znamená STOP/reconciliation, žádné automatické resume, retry nebo rollback.
Běžný scan, stránka, metadata refresh a startup write job nespouštějí.
Scanner čte knihovnu a zapisuje katalog do SSD SQLite; metadata workflow
může zapisovat také obaly do lokálního `/artwork`, nikdy do NAS knihovny.
V této změně nevzniká nový executor, import, launch endpoint ani write profil.

## SQLite bez Landlocku — konkrétní neimplementovaný návrh

N4B2A zjistil na cílovém RPi5 kernelu Landlock `ENOSYS`. N4A nyní správně vrací
`SANDBOX_UNAVAILABLE`. Helper i N4B1 adaptér zůstávají v tomto kroku beze změny;
pro Nexus dosud neexistuje prokázaný bezpečný snapshot backend. Neplánuje se
kvůli AnimeDB změna kernelu, LSM, boot parametrů ani host systémových služeb.

Pouhý další proces + `mode=ro`, vypnutí kontroly Landlocku nebo celý zdrojový
adresář připojený RO nestačí k dosavadnímu kontraktu. RO mount chrání před
zápisem přes něj, ale nezabraňuje otevření jiného inodu podstrčeného za cestu
mezi kontrolami. `immutable=1` není správný režim pro souběžně zapisovanou DB;
samostatné kopírování DB/WAL/SHM není atomický SQLite snapshot.

Preferovaný kandidát je izolovat pouze čerstvý SQLite copier, zachovat N4A
supervisor/validaci/publikaci a přidat úzký důvěryhodný launcher využívající
stávající mount namespace rozhraní Linuxu:

1. Supervisor drží dosavadní `O_NOFOLLOW` directory/source FD, singleton link,
   původní inode/device/mount IDs a lock. Připne hlavní DB i existující sidecary.
2. Launcher pro jedinou instanci vytvoří nový private mount namespace bez
   propagace ven. Minimální runtime root je RO; zpřístupní jen explicitní
   ověřené zdrojové inody jako jednotlivé RO file bindy pod stabilními SQLite
   jmény a jedinou vlastní pracovní generaci jako RW. Žádné NAS, jiné DB,
   host root, Docker socket, síť ani jiné RW adresáře.
3. Každý bind vzniká z připnutého inode, nikoli znovu z aplikačně měnitelné
   cesty. Launcher ověří inode/device a RO flag cílového bindu a zaznamená
   mapování původních mount IDs na nové IDs namespace. Nové bind IDs se nesmějí
   zaměňovat s existující N4A kontrolou nežádoucích host aliasů.
4. Před fresh exec zavře původní host directory/file FD i jiné únikové handles,
   odstraní přístup ke starému rootu a dropne capabilities včetně bounding setu.
   Vynutí no_new_privs a zákaz návratu do host namespace/remountu či získání
   mount authority. Ponechat původní FD do RW host mountu a zpřístupnit je přes
   procfs by RO ochranu mohlo obejít. Fresh copier nesmí dědit SQLite mappingy.
5. Copier používá `mode=ro`, `query_only`, bez immutable a zdrojového
   checkpoint/recovery. Online Backup API čte živé připnuté DB/WAL/SHM inody,
   ne jejich nekonzistentní byte kopie. Souběžné commity musí fungovat bez
   mutace zdroje copierem. Změna inode/sidecar lifecycle nebo potřeba recovery
   znamená bezpečný STOP. Připnuté bindy musí zabránit čtení podstrčené DB
   i při dočasné záměně a návratu; trvající změnu identity před publikací musí
   supervisor odmítnout kontrolou proti původním cestám.
6. Supervisor drží deadline a umí ukončit/reapovat celý launcher/copier strom
   včetně smrti rodiče. Po návratu provede stávající integrity/FK/schema/hash
   kontroly a fsync/RENAME_NOREPLACE publikaci; při chybě nic nepotvrdí.

To je návrh k ověření, nikoli tvrzení, že primitiva či read-only WAL chování
byly na Nexusu prokázány. Minimální runtime root, uzavření escape FD, drop
privilegií, namespace/seccomp politika a kill/reap lifecycle vyžadují review.
Root launcher je citlivý privilegovaný kód, i když nemění host konfiguraci.
Varianta s Docker workerem navíc musí řešit daemon-side TOCTOU při bindu
host cesty a zabití kontejneru při timeoutu, nikoli jen zabití Docker klienta.
Ani obyčejné `docker run -v dbdir:ro` proto není hotová náhrada N4A.

Před implementací jsou nutné izolované důkazy na stávajícím kernelu: create
namespace/private mounts, zákaz write/truncate původní DB i SHM, zakázané cizí
čtení, přetrvávající WAL obsah, souběžné commity, dočasná DB/SHM záměna,
hardlink a mount alias, sidecar replacement, kill/parent-death, finální
identity a kolize/publikační fsync. Pouze nová dočasná SSD data. Pokud bezpečné
RO otevření existujících WAL/SHM nefunguje, vrátit STOP; neudělovat copieru
zápis do živých sidecarů a nepředstírat podporu souběhu odstavenou aplikací.

Nový backend vyžaduje revizi worker protokolu a pravdivé `copy_confinement`
evidence místo předstíraného `landlock_abi`. N4B1 musí být znovu prověřen proti
tomuto konkrétnímu backendu; jeho stávající výjimky živých DB/.partial,
jednoznačné snapshot ID a kontrola vzdálených bytes zůstávají nutné.
Do té doby se N4A/N4B1 na Nexusu neaktivují jako funkční AnimeDB snapshot řešení.
Neověřené privilegované části původního Resticu zůstávají samostatný gate.

Technické podklady: [SQLite read-only WAL](https://www.sqlite.org/wal.html#read_only_databases),
[Online Backup API](https://www.sqlite.org/backup.html),
[private mount namespace](https://man7.org/linux/man-pages/man7/mount_namespaces.7.html)
a [bind/remount](https://man7.org/linux/man-pages/man2/mount.2.html).

## Podklady pro nezávislý review Claude Opus 5.5

Baseline `ca820ea7880161382d53dc06f8d0fffd068d30d9`; review zahrnuje aktuální
necommitnuté změny, včetně tohoto nového dokumentu, který nemusí být ve
výchozím `git diff`. Žádný review výsledek zatím není prohlášen.

- Prověřit fail-closed parser nad celou mount tabulkou, přesnou source/root
  identitu, dekódování paths, nested mounts a RW aliasy i při jiném device.
- Ověřit, že FS=rw neblokuje RO web a že mountinfo nedokazuje SMB ACL ani
  původní Docker requested flag. Compose/Docker evidence a runtime VFS jsou
  dvě samostatné kontroly, bez Docker socketu v aplikaci.
- Zkontrolovat, že všechny běžné služby zůstávají RO a žádný webový request
  nezískává nebo nespouští NAS write authority. Scratch foundations nejsou
  vydávané za produkční executor.
- Posoudit neimplementovaný SQLite namespace návrh zejména proti DB/SHM
  TOCTOU, únikovým FD, WAL souběhu a zabití celého procesu. Landlock kontrola
  není vypnutá a současný helper na Nexusu stále bezpečně STOPne.
- Ověřit izolaci testů a nezměněný N4A/N4B1 runtime kód, produkci, backlog,
  planned/not-deployed stav a absenci commitu/pushe/deploye.
