# N4A — izolovaný SQLite backup helper

Stav: **planned / not-deployed**. Helper je lokální podklad pro budoucí Nexus,
nikoli instalovaná služba. Používá pouze standardní Python knihovnu; neimportuje
AnimeDB, nevytváří aplikační schema a nespouští migrace, scanner ani Restic.
N4A nemění Docker, systemd, mounty, firewall ani serverové skripty.

## Rozhraní a očekávané instance

Budoucí ruční/spouštěcí rozhraní po samostatně schválené instalaci:

```bash
python3 /schvalene/umisteni/sqlite_backup.py --instances A
python3 /schvalene/umisteni/sqlite_backup.py --instances A B --timeout 120
```

Tyto příklady nyní na serveru nespouštět. Root je pevně
`/srv/nexus/appdata/anime-db`; CLI nemá volbu rootu, zdrojové/cílové cesty,
generace ani environment override. `--instances` je povinný neprázdný seznam
unikátních `A` a/nebo `B`. A se nasazuje první; bez explicitně očekávané B se
B ani její snapshot adresář neotevírá a nevytváří. Existence adresáře žádnou
instanci neaktivuje. Očekávaná chybějící DB je chyba, nikoli důvod k inicializaci.

Každá instance používá výhradně `<root>/<instance>/db/anime.db` a publikuje do
`<root>/backups/<instance>/<UTC-UUID>/`. Pracovní generace vzniká s unikátním
názvem pouze pod `<root>/backups/.partial/`. Publikovaný adresář obsahuje přesně:

- `anime.db`: samostatnou uzavřenou SQLite kopii bez potřebných sidecarů;
- `snapshot.json`: zdrojovou cestu a device/inode, instanci, generaci, UTC
  interval kopírování a validace, verzi SQLite, schema marker, velikost,
  SHA-256 DB, SHA-256 helperu a výsledky kontrol;
- `SHA256SUMS`: SHA-256 `anime.db` a `snapshot.json`, bez kruhové závislosti.

`git_revision` je `null`: samostatný helper neumí doložit build/Git provenance
a nevydává HEAD jiného souboru nebo uživatelský řetězec za svou revizi.
Přesný lokální artefakt identifikuje `helper_sha256`.

## Bezpečnost a SQLite lifecycle

Před budoucí aktivací musí správce připravit existující appdata root vlastněný
účtem helperu, bez group/other zápisu. Stejné podmínky platí pro výstupní
adresáře; nové vznikají s mode `0700`, výstupní soubory `0600`. Živé DB adresáře
zůstávají zapisovatelné vlastní aplikační instancí pro SQLite lifecycle.
Helper sám oprávnění existujících adresářů neopravuje.

Komponenty cest se otevírají přes directory FD a `O_NOFOLLOW`. Zdroj musí být
regular file s jediným linkem; kontroly pokrývají i existující WAL/SHM/journal.
Drží se identita zdroje a adresářů a znovu se ověří před publikací. Pod rootem
se odmítají jiné mount IDs včetně bind aliasů. Povolené lokální filesystemy jsou
ext4, XFS, Btrfs, tmpfs a overlay; CIFS, NFS, autofs a neznámé typy jsou STOP.
Na Linuxu jsou nutné procfs, `fork`, `prctl`, `renameat2(RENAME_NOREPLACE)`
a zapnutý Landlock ABI alespoň 3. Syscall rozhraní je připravené pouze pro
x86_64 a aarch64; skutečný ARM64 běh zůstává neověřený.
Neexistuje fallback na přepisující rename. Správce/root a instalační artefakt
jsou důvěryhodní; nejde o ochranu před souběžným škodlivým root procesem.

SQLite kopírování používá další **čerstvý interpreter**, aby nepřevzalo SQLite
VFS cache, zapisovatelné SHM mappingy ani cizí zapisovatelné FD supervisoru.
Předávají se jen directory capabilities, read-only FD ověřené hlavní DB
a existujících sidecarů a IPC. Landlock povolí čtení jen těchto připnutých
zdrojových inodů a pracovní generace; veškeré souborové zápisy směřují jen
do této generace. Tím se vynucuje ochrana i mezi jednotlivými `stat` kontrolami:
dočasná záměna hlavní DB nebo podstrčený SHM hardlink neopravňuje SQLite
číst jinou DB či přepsat cizí soubor. Bez úspěšného zapnutí ochrany nastane
`SANDBOX_UNAVAILABLE`, bez nezabezpečeného fallbacku.
Podklad: [Linux Landlock](https://docs.kernel.org/userspace-api/landlock.html).

Zdroj SQLite se otevírá s `mode=ro`, bez `immutable=1`, změny zdrojového
journal mode či explicitního checkpointu. Online Backup API zahrne committed
WAL obsah. Read-only SQLite obecně může použít/vytvořit sidecary; tento helper
jejich čtení omezuje na předem ověřené soubory a zápisy mimo pracovní generaci
zakazuje. Pokud SQLite potřebuje vytvořit či změnit zdrojový sidecar nebo byl
sidecar souběžně nahrazen novým inodem, může nastat bezpečný STOP a je nutný
další pokus po vyřešení lifecycle. Helper neslibuje byte-exact neměnnost živé DB
a všech jejích sidecarů při souběžných zápisech aplikace.
Souběžné aplikační commity jsou přípustné; výměna zdrojového souboru není.
Při nutnosti nepovoleného recovery zápisu se operace zastaví.

Pouze cílová DB se normalizuje na `journal_mode=DELETE`. Po uzavření spojení
se znovu otevře read-only a vyžaduje se aktuální schema marker **9**, všech
18 tabulek z `app/models.py`, úplný `integrity_check = ok`, prázdný
`foreign_key_check` a nezávislost na sidecarech. Prázdná, ale připravená B není
chyba. Testy vytvářejí skutečné schema modelu v dočasných DB, načítají aktuální
marker z `app/migrations.py` bez importu migrací a zkoušejí vynechání každé
aplikační tabulky. Při budoucí změně schématu je nutná revize tohoto kontraktu.

## Timeout, publikace a strojový výsledek

Každou instanci provádí samostatný worker. Supervisor měří celý worker běh,
včetně otevření cest, SQLite, validace, fsync a publikace, s výchozí hranicí
120 sekund. Zablokovaný worker je ukončen přes SIGKILL a reapován; timeout
není závislý jen na SQLite progress callbacku nebo busy timeoutu. Při SIGTERM
či SIGINT se aktuální worker ukončí a zbývající instance už nezačnou.
Linux parent-death signal brání pokračování workeru po smrti supervisoru
i pokračování odděleného SQLite copieru po smrti workeru. Signály se při
zakládání a úklidu FD/workeru odkládají do chráněného bodu; přerušení před
vytvořením workeru má rovněž explicitní výsledek.

Před publikací se ověří metadata i checksumy a fsyncnou všechny tři soubory,
pracovní adresář a příslušní rodiče. Celá generace se přesune atomicky bez
overwrite a fsyncnou se zdrojový i cílový rodič. Kolize zachová starší generaci.
Helper používá neveřejný nonblocking `backups/.sqlite-backup.lock`.

Při chybě, timeoutu nebo havárii workeru supervisor stáhne případnou právě
publikovanou vlastní generaci zpět do `.partial/failed-<UUID>`. Cizí nebo starší
kolizní adresář nestahuje. Nedokončené generace zůstávají pro diagnostiku pouze
v `.partial`; nezapočítávat je do záloh. Pokud selže i karanténa při závadě
filesystemu, výsledek obsahuje `publication_uncertain: true` a STOP. V takovém
stavu je nutný ruční audit; samotná existence adresáře ani validační manifest
nenahrazuje potvrzený úspěšný výsledek běhu. Nelze slíbit dokončení diskových
operací při neodstranitelném kernelovém I/O wait nebo fyzické závadě úložiště.

Stdout obsahuje jeden JSON dokument: `expected_instances`, `success` a
samostatný `results` záznam pro každou očekávanou instanci. Každý má `instance`,
`status: SUCCESS|STOP`; STOP obsahuje také strojový `code` a vysvětlení.
Chyba A nepřepisuje výsledek B a sama nezabrání záloze očekávané B.

Návratové kódy: `0` všechny očekávané instance uspěly, `1` alespoň jedna
selhala, `2` neplatné CLI argumenty, `130` přerušení. Příklady STOP kódů:
`MISSING_SOURCE`, `UNSAFE_PATH`, `PERMISSION`, `SCHEMA`, `SQLITE`, `VALIDATION`,
`LOCKED`, `COLLISION`, `TIMEOUT`, `WORKER_FAILED`, `INTERRUPTED`, `IO_ERROR`,
`SANDBOX_UNAVAILABLE`.
`SUCCESS` znamená ověřený **lokální** snapshot. V manifestu i výsledku je vždy
`remote_backup_verified: false`; helper nepotvrzuje vzdálenou zálohu.

## Retence a budoucí N4B

Lokální N4B1 [integrační kontrakt a adaptér](SQLITE_RESTIC.md) jsou připravené
odděleně; na serveru nejsou instalované. Následující hranice N4A zůstávají
zachovány, včetně zákazu lokální automatické retence.

N4A nemaže žádné generace ani zbytky `.partial`. Návrh lokální retence je
ponechat dvě poslední vzdáleně potvrzené generace každé instance a všechny
dosud neodeslané. Implementace potřebuje důkaz vazby přesné generace a checksumů
na úspěšné Restic snapshot ID. Tento důkaz N4A nevytváří.

N4B musí samostatně řešit integraci do existující úlohy, vyloučení živých DB
adresářů a `.partial` z Resticu, úspěch ostatních Nexus/InfluxDB dat při chybě
AnimeDB a ověřenou obnovu. Artwork, reference, manifest, konfigurace a projektové
dokumenty musí zůstat zahrnuté. A/B snapshoty netvoří společný okamžik a SQLite
kopie sama nezajišťuje atomickou DB+artwork sadu. Obnovu vždy směrovat do nové
oddělené cesty; současnou DB nepřepisovat.

## Lokální ověření a hranice důkazu

Regresní sada `tests/test_nexus_sqlite_backup.py` používá jen dočasné DB a FS.
Pokrývá explicitní A-only/A+B, neaktivní a chybějící B, WAL bez checkpointu,
souběžné commity, schema/tabulky/integritu/FK, aliasy, skutečné zamítnutí práv,
kolizi, checksumy, opakované generace, zámek, chybu fork, timeout při SQLite
i publikaci a přerušení. I/O chyby publikace se injektují na konkrétní operaci;
test vyžaduje, aby se skutečně provedla, a ověřuje výsledek na filesystemu.

Regrese z interního review ověřují SHM hardlink podstrčený mezi kontrolami,
dočasnou záměnu zdrojové DB, souborové mount aliasy a signál během zakládání
supervisoru. Samostatný subprocess test zkouší skutečné kernelové zamítnutí
zápisů do zdroje a čtení cizích souborů, zatímco zápis do generace je povolený.

ARM64 runtime, chování skutečného NVMe/filesystemu Nexusu, instalace/oprávnění
serverových cest, dostupnost Landlocku v cílovém kernelu, Restic integrace,
vzdálená retence a skutečná obnova nejsou
těmito lokálními testy prokázané. Do dokončení příslušných provozních testů
ponechat projekt `planned / not-deployed` a restore údaje neověřené.

## Podklady pro nezávislý review Claude Opus 5.5

Review zahrnuje [helper](../scripts/sqlite_backup.py),
[regresní testy](../../../tests/test_nexus_sqlite_backup.py), tento kontrakt
a změny ostatních Nexus deployment dokumentů proti HEAD
`2f5792b35ad6720da874cfed2d5a5442e1d8f01a`. Nové soubory mohou být untracked;
samotný `git diff` je proto nemusí ukázat. N4A nic necommituje ani nenasazuje.

Zvlášť prověřit:

- Pevnou CLI write authority, explicitní instance, kontrolu komponent cest,
  souborových i adresářových mount IDs a nemožnost přepsat starší generaci.
- Čerstvý SQLite proces, omezené předané FD a Landlock read/write pravidla
  proti záměně DB či SHM mezi kontrolami; žádný nezabezpečený fallback.
- Deadline od vytvoření workeru přes publikaci, kill/reap a parent-death řetěz,
  odložené signály při setup/cleanup a samostatné výsledky A/B.
- Fsync pořadí, no-overwrite rename, identitu stahované generace a explicitní
  STOP/nejistotu, pokud nelze dokončit karanténu při chybě filesystemu.
- Shodu schema kontraktu s aktuálním kódem, kontrolu celé samostatné kopie,
  pravdivý manifest a oddělení lokální validace od Restic/restore důkazu.

Provozní test ARM64, cílového kernelu a NVMe, instalace, skutečná Restic záloha,
retence i obnova patří do pozdějších samostatně autorizovaných kroků.
