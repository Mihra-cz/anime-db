# N4B1 — lokální adaptér existujícího Restic běhu

> **Historický podklad N4B1 — neaktivní.**
> Původní integrační návrh pro srovnávací instance. Není aktuální instalační
> návod. Adaptér zůstává neaktivní; nezapojovat do Resticu na nových cestách.
> Aktuální provoz: [OPERATIONS](OPERATIONS.md), [FIRST_RUN](FIRST_RUN.md).

Stav: **planned / not-deployed**. Implementace je pouze v repozitáři a testy
používají dočasné SQLite DB a simulovaný Restic. Na Nexus nebylo nic instalováno;
Produkční Restic backup/restore ani ARM64 běh adaptéru nejsou ověřené.
[N4A kontrakt](SQLITE_BACKUP.md) se nemění.

**N4B2 doplnění:** N4B2A izolovaně potvrdil CLI/JSON kontrakt skutečného
Resticu 0.18.0 nad novým lokálním testovacím repo. Privilegované čtení původních
skriptů zůstalo nedokončené, produkční integrace není ověřená. Současný N4A
helper na RPi5 bezpečně STOPne kvůli Landlock ENOSYS. Kernel se kvůli aplikaci
měnit nebude; [alternativní copier](N4B2_COMPATIBILITY.md) je teprve návrh,
nikoli dostupný fallback. N4B1 runtime kód se nemění. I při úspěchu původní
systémové zálohy nemůže neúspěšná očekávaná instance získat potvrzení AnimeDB
a agregovaný výsledek adaptéru zůstane nenulový.

## Nejmenší budoucí zásah

Adaptér [sqlite_restic.py](../scripts/sqlite_restic.py) obaluje **jediné existující
volání `restic backup`** uvnitř `/usr/local/sbin/nexus-restic-backup`.
Nevlastní NAS preflight, InfluxDB staging, službu, timer, maintenance ani retenci.
Nevytváří druhý backup systém, zámek nebo systemd jednotku.

Po samostatném schválení instalace změnit pouze začátek původního příkazu:

```bash
# Původní začátek příkazu:
restic \
  -r "$REPO" \
  --password-file "$PASS" \
  backup \
```

```bash
# Navržený začátek téhož příkazu; není to samostatný spustitelný skript:
/usr/bin/python3 -I -B /srv/nexus/projects/anime-db/scripts/sqlite_restic.py -- \
  /usr/bin/restic \
  -r "$REPO" \
  --password-file "$PASS" \
  backup \
```

Za tímto prefixem ponechat **všechny současné argumenty beze změny**, zejména
`/etc`, `/srv/nexus`, `/usr/local/sbin`, skutečnou cestu InfluxDB stagingu,
`--exclude /etc/restic/nexus.password`, existující další výjimky a `--tag`.
Adaptér přidá pouze `--json` a své konkrétní `--exclude` argumenty.
Repozitář ani source groups se nepřidávají/nepřeskupují; současná Restic retence
zůstává ve stávajícím maintenance skriptu.

Volání patří **po současném úspěšném NAS preflightu a přípravě InfluxDB**, pod
již držený společný backup/maintenance `flock` (naposledy popsaný jako FD 9
a `/run/lock/nexus-restic-backup.lock`). Tento zámek nesmí být uvolněn během
helperu, Resticu ani následného ověření. Příkaz nevolat na pozadí ani přes
`exec`, nepřidávat `|| true`. Původní shell čeká na adaptér a drží zámek;
subprocessy nepotřebují jeho kopii.

Původní `set -Eeuo pipefail` musí neúspěch adaptéru předat službě. Existující
`OnFailure` a případné `ExecStartPost` zůstávají beze změny: agregovaný nenulový
exit nesmí skončit následným `echo PASS` nebo úspěšným cleanup příkazem.
Pokud skutečný skript výsledek zachytává, zachovat jej explicitním `exit "$rc"`.
Shell regrese ověřuje tuto exit propagaci; skutečný systemd handler neběžel.

## Výslovná konfigurace správce

Instalovat [šablonu](../config/sqlite-backup.json.example) jako
`/srv/nexus/projects/anime-db/config/sqlite-backup.json`:

```json
{"expected_instances": ["A"]}
```

Teprve po samostatné inicializaci B změnit seznam na `["A", "B"]`.
Jedna/nebo dvě unikátní hodnoty A/B jsou povinné, bez jiných klíčů. Žádný
discovery z adresářů ani inicializace DB. Neaktivní B je v pořádku; chybějící
aktivní B skončí vlastním STOP, ostatní instance a systémová záloha pokračují.

Konfigurace musí být regular file bez symlinků/hardlinků, vlastněný UID služby,
mode `0600`; její rodič bez group/other zápisu, vlastněný UID služby. Všechny
komponenty se otevírají bez následování symlinků. Instalační kód, jeho rodiče,
helper i konfiguraci smí měnit pouze důvěryhodný správce, nikoli aplikační UID.
CLI nemá přepínač rootu, konfigurace nebo instalačního umístění helperu.
Předávané Restic argv pochází ze stávajícího důvěryhodného serverového skriptu.

Podporovaný prefix je přesně `/usr/bin/restic -r REPO --password-file PASS
backup ...`. Skutečná cesta binárky, verze a prefix jsou instalační podmínka;
jiný prefix vyžaduje review adaptéru. Dry-run, skip-if-unchanged, stdin,
files-from, přesměrování repo voleb a oddělovač `--` v backup suffixu jsou STOP
před spuštěním backupu. Adaptér nepodporuje libovolné budoucí varianty CLI.

## Pořadí a výjimky

1. Načíst chráněnou konfiguraci. Spustit sousední `sqlite_backup.py` přes
   izolovaný Python s `--instances A` nebo `A B`, bez aplikace/migrací.
   N4A timeout zůstává 120 s na instanci; adaptér má navíc supervisor limit
   `120 × počet instancí + 30 s` na celý helper proces.
2. Parsovat jediný JSON dokument bez duplicitních klíčů. Ověřit formát,
   očekávaný seznam, přesně jeden výsledek v pořadí pro každou instanci,
   SUCCESS/STOP, návratový kód, `remote_backup_verified: false`, canonical
   cestu a N4A generation ID. Žádný výběr „latest“ ze starých adresářů.
3. U SUCCESS znovu číst pouze chráněný publikovaný bundle přes directory FD.
   Vyžadovat přesně tři independent regular files na stejném mountu, aktuální
   schema/validace, identitu manifestu, DB velikost a SHA-256, přesný SHA256SUMS.
   Čas zahájení i dokončení manifestu musí spadat do právě probíhajícího volání
   helperu. Replay staršího výsledku je chyba; změna systémových hodin může
   vyvolat konzervativní STOP. Nespouští se další SQLite connection.
4. Spustit původní Restic příkaz s přidanými absolutními výjimkami adresářů:

   ```text
   /srv/nexus/appdata/anime-db/A/db
   /srv/nexus/appdata/anime-db/B/db
   /srv/nexus/appdata/anime-db/backups/.partial
   ```

   Vyloučení celých DB adresářů pokrývá i WAL/SHM/journal a případné jiné živé
   soubory. Žádné `*.db`. Artwork, reference včetně její `anime.db`, konfigurace,
   manifest, dokumenty a ověřené publikované generace zůstávají ve výběru.
   Současné výjimky se nemažou ani nenahrazují.
5. Zachytit JSON-lines stdout skutečného backupu do anonymního dočasného
   souboru, stderr ponechat v journalu. Vyžadovat exit 0 a právě jeden poslední
   summary bez error záznamů, s plným 64hex `snapshot_id`. Chybějící/krátké ID
   není nahrazeno dotazem na `latest`.
6. Přečíst identitu stejného repo přes `cat config`, pro **přesné ID tohoto
   běhu** ověřit celý strom přes `ls --json ID` bez path filtru. Vyžadovat
   identické ID, všechny tři soubory každé aktuální generace a žádné další
   soubory/sidecary uvnitř této generace. Odmítnout jakýkoli node živých DB
   adresářů nebo `.partial`, včetně adresářů samotných, a nejisté stromy.
7. Pro každý očekávaný soubor přečíst `dump ID /presna/cesta` do anonymního
   dočasného souboru, ověřit velikost a SHA-256 skutečných vzdálených bytes
   proti lokálně ověřenému bundle. Tím jsou ověřeny DB, snapshot.json
   i SHA256SUMS a jejich vzájemné vazby. Žádný restore target ani zápis do DB.
8. Teprve po úplném úspěchu vzdálených kontrol zapsat fsyncnutý privátní JSON
   doklad do `backups/.restic-confirmations/<UUID>.json` přes pracovní název,
   `RENAME_NOREPLACE` a fsync adresáře. Obsahuje repository ID, snapshot ID,
   generace A/B, přesné cesty, velikosti a hashe všech tří souborů, čas ověření
   a `restore_tested: false`. N4A bundle ani jeho manifest se nepřepisují.

Každý čtecí Restic příkaz má timeout 300 s; backup dál podléhá existujícímu
systemd deadline. Dočasné výstupy mohou spotřebovat místo v host `/tmp`; DB
se nehashuje celá v RAM. Nutné ověřit volné místo, velikosti DB a časový rozpočet
služby. SIGTERM/systemd timeout musí ukončit celý service cgroup; ověřit skutečný
KillMode a OnFailure před instalací. Přerušení nevytváří nový doklad.

Protokol je založen na oficiální dokumentaci Restic 0.18:
[JSON a exit kódy](https://restic.readthedocs.io/en/v0.18.0/075_scripting.html),
[výjimky](https://restic.readthedocs.io/en/v0.18.0/040_backup.html#excluding-files),
[dump](https://restic.readthedocs.io/en/v0.18.0/050_restore.html#printing-files-to-stdout).
Simulované testy používají její formát a výběr absolutních literal cest;
shoda s konkrétní nainstalovanou verzí Resticu zůstává instalační podmínka.

## Výsledky a selhání

Stdout adaptéru je jeden JSON: per-instance `instances`, `helper_exit_code`,
`helper_error`, `restic_exit_code`, `snapshot_id`, `repository_id`,
`verification_error`, `receipt`, agregované `success`, `restore_tested: false`.
Per-instance `remote_backup_verified: true` vzniká až po vzdálené kontrole
a úspěšném zápisu dokladu. SUCCESS bez tohoto příznaku znamená jen lokální
N4A úspěch. Nadále musí být viditelný i STOP druhé instance.

| Událost | Backup Nexusu/InfluxDB | Doklad / celkový exit |
| --- | --- | --- |
| Všechny očekávané instance a vzdálené kontroly OK | Původní příkaz + výjimky | Doklad pro přesné generace; 0 |
| Známý STOP B, A OK | Pokračuje, historické platné kopie zůstávají zahrnuté | Lze potvrdit pouze aktuální A; celkem 1 |
| Helper/config/JSON chybí, neplatný či timeout | Pokračuje, navíc vyloučí `backups/A` i `backups/B` kvůli neznámé publikaci | Bez potvrzení AnimeDB; 1 při úspěchu Resticu |
| Chybí/nefunguje instalovaný soubor helperu | Totéž bezpečné pokračování bez N4A | Bez potvrzení AnimeDB; 1 při úspěchu Resticu |
| Nejistá publikace nebo chybný lokální bundle | Pokračuje, navíc vyloučí celý snapshot strom dotčené instance | Dotčená instance bez potvrzení; celkem 1 |
| Restic nenulový, včetně 3 nebo neznámého kódu | Příkaz již proběhl; neopakuje se | Bez potvrzení, vrátí jeho kód; signál mapuje na 128 + číslo |
| Chybí ID/generace, checksum nesedí, listing/dump chyba/timeout | Backup mohl uložit ostatní data | Bez nového dokladu, celkem 1 |
| Chyba zápisu/fsync dokladu | Data mohou být vzdáleně uložená, evidence není potvrzená | Bez úspěšného výsledku, 1; ruční audit |
| Nelze vytvořit capture soubor pro Restic | Původní příkaz + bezpečné výjimky, stdout do `/dev/null` | Bez ID/content důkazu, 1; nenulový Restic kód zachován |
| Nepodporovaný instalační CLI kontrakt | Bezpečný STOP před příkazem | 2; instalaci neopakovat bez opravy kontraktu |

Starší validní generace mohou být součástí systémového snapshotu, ale nejsou
vydávány za výsledek aktuálního helperu. Při nejistotě mají bezpečnostní výjimky
přednost před archivací dotčeného stromu. Nefungující základní NAS/InfluxDB
preflight zůstává původním STOP ještě před adaptérem.
Bez platného dokončeného dokladu je generace nepotvrzená. Ani přítomný doklad
neprokazuje, že pozdější maintenance daný vzdálený snapshot stále uchovává.

## Retence a oddělená obnova

N4B1 **nic nemaže**: žádný unlink/rmtree/purge generací, žádný `forget`,
`prune` ani změna Nexus maintenance. Zbytky `.partial` i nepotvrzené kopie
zůstávají pro diagnostiku. Budoucí samostatně schválená lokální retence musí
nejprve ověřit dokončený doklad, vazbu repository ID + plné snapshot ID +
instance/generace/hashe a trvající dostupnost těchto bytes v Resticu.
Ponechat dvě nejnovější takto potvrzené generace **každé instance** a všechny
ostatní nepotvrzené generace. Neodvozovat potvrzení z názvu adresáře, exit 0,
N4A manifestu nebo samotného stáří. Doklady vzniklé po backupu budou součástí
až některého následujícího systémového backupu; nejsou nutné k ruční obnově
bundle, jehož metadata a hashe lze znovu ověřit přímo v Resticu.

Budoucí test obnovy se schvaluje samostatně: z konkrétního ID obnovit vybrané
A/B generace a související artwork/config do nového prázdného dočasného rootu
na lokálním FS, nikdy do `/srv/nexus/appdata/anime-db/{A,B}/db`, reference ani
originálu. Ověřit SHA256SUMS, manifest, velikosti, schema/tabulky, read-only
integrity/FK a samostatnost DB bez sidecarů. Aplikaci případně spustit až nad
další oddělenou kopií, bez produkčních bindů, scanneru a NAS zápisu. A/B nejsou
společný časový snapshot; DB+artwork není atomická sada. Samotný content doklad
nepotvrzuje funkční aplikaci po obnově; `restore_tested` zůstává false.

## Instalační prerequisites a přesný rozsah pozdějšího schválení

Cookbook v1.0 byl dostupný v lokální kopii `nexus_project_cookbook`; deklaruje
stejné čtyři zdroje, zachování stávající infrastruktury a obnovu jako samostatný
důkaz. N4B1 neprovedl nové SSH čtení ani serverový runtime test. Dříve zjištěné
aarch64 / Debian 13.7 dokazují pouze zamýšlenou platformu, nikoli syscall práva.

Před instalací povinně read-only zaznamenat aktuální obsah a SHA-256 backup
a maintenance skriptů, `systemctl cat` služby/timeru včetně drop-inů, skutečný
flock, OnFailure, ExecStartPost, TimeoutStartSec/KillMode, zdroje, excludes,
tagy, repo a retenci. Nelze bez toho schválit patch podle historického popisu.
Neověřené detaily se nesmějí doplnit odhadem.

Na Nexusu doložit tyto kontroly, runtime část až v pozdějším schváleném kroku:

- `uname -m = aarch64`, Python se sqlite3, `fork`, procfs a dostupné libc
  `prctl`/`renameat2`. Zkontrolovat syscall mapping 444/445/446 pro aarch64.
- Pro **stávající N4A backend** musí čtecí Landlock ABI dotaz přes `landlock_create_ruleset(..., VERSION)`
  vrátit alespoň 3. Samotný ABI dotaz **neprokazuje** povolení `restrict_self`.
  Stav LSM, seccomp a service sandboxu musí dovolovat `no_new_privs`, vytvoření
  rulesetu, add_rule a `landlock_restrict_self`. Skutečné vynucení vyzkoušet
  v odděleném schváleném procesu nad dočasnými soubory pod účtem služby.
  N4B2A zjistil ENOSYS: tento požadavek se neřeší změnou kernelu ani vypnutím
  ochrany. Před instalací je nutný samostatně implementovaný, revidovaný
  a provozně prokázaný alternativní backend podle N4B2; případná změna worker
  protokolu/confinement evidence vyžaduje nové N4A/N4B1 review.
- Ověřit na skutečném appdata FS `RENAME_NOREPLACE`: úspěšný přesun i kolize
  bez overwrite, fsync souborů i adresářů. To vyžaduje pozdější povolený zápis;
  existence symbolu libc nebo architektura nejsou provozním důkazem.
- `findmnt`/mountinfo: appdata a snapshoty skutečně lokální podporovaný FS
  (ext4/XFS/Btrfs), žádný CIFS/NFS/autofs ani vnořený bind alias. Potvrdit mount
  IDs souborů i adresářů. NAS nesmí být cílem helperu ani evidence.
- `namei`/`stat`: appdata root, backups a evidence vlastní UID služby bez
  group/other zápisu; soubory snapshotů privátní 0600, adresáře 0700.
  Aplikační UID/GID musí zapisovat pouze vlastní živou DB/artwork. Pokud je
  služba root, nesmí application UID vlastnit společný root nebo backupy.
- Ověřit binárky `/usr/bin/python3`, `/usr/bin/restic`, Restic JSON verzi,
  plná snapshot ID, čtecí `cat config`/`ls`/`dump`, zachování výjimek a volné
  místo/časový rozpočet. Provozní testy oddělit od současných simulovaných.

Budoucí schvalovaný zásah má zahrnovat pouze:

1. Instalaci revidovaných `sqlite_backup.py` a `sqlite_restic.py` vedle sebe do
   `/srv/nexus/projects/anime-db/scripts/`, root-owned, bez zápisu aplikace.
2. Nový chráněný `config/sqlite-backup.json` s A-only; B později zvlášť.
3. Existující/prázdné appdata/backup cesty a práva podle N4A, bez automatické
   inicializace DB. Adaptér při běhu vytváří pouze vlastní evidence directory.
4. Zálohu aktuálního `/usr/local/sbin/nexus-restic-backup` a jedinou změnu
   prefixu jeho Restic příkazu podle ověřeného obsahu. Žádná změna služby,
   timeru, OnFailure, repo, InfluxDB stagingu ani maintenance/retence.
5. Instalaci příslušných deployment dokumentů/manifestu a ruční aktualizaci
   registru podle Cookbooku. Stav změnit až po skutečných provozních důkazech.

Rollback budoucí instalace: vrátit pouze schválený diff backup skriptu z jeho
ověřené předinstalační kopie; zachovat generace/evidenci a chránit živé DB
adresáře před přímým Restic čtením i po rollbacku. Návrat původního skriptu
bez těchto výjimek není bezpečný při běžící AnimeDB. Zvolit samostatně
schválené ponechání výjimek nebo odstavení AnimeDB; retenci nepoužít jako úklid.

## Lokální testy a nezávislý review

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -p no:cacheprovider tests/test_nexus_sqlite_restic.py tests/test_nexus_sqlite_backup.py tests/test_nexus_preflight.py
```

Testy pokrývají skutečné dočasné N4A generace, A-only/A+B/chybějící B,
zachování systémových/InfluxDB zdrojů, literal exclude výběr včetně reference
DB a artworku, helper/config/JSON chyby a nejistou publikaci, replay staré
generace, Restic chyby včetně 3, chybějící/odlišné ID, chybějící soubory,
checksumy, sidecary, zakázané cesty, selhání evidence a žádný purge.
Regrese interního review navíc ověřují chybějící instalovaný helper a selhání
alokace dočasného výstupu bez přeskočení původního backupu. Poškozené vzdálené
bytes mají v testu stejnou délku, takže musí selhat hash, nikoli jen listing.
Nejde o spuštění skutečné binárky Resticu nebo systemd OnFailure.

Podklady pro Claude Opus 5.5: celý pracovní diff proti
`6e9f3e5f8cffab7a8b460b4fbfbe0d9a4849dcb0`, nové soubory adaptéru,
konfigurace, testů a tento kontrakt. Zvlášť prověřit zachování původního argv,
flock/exit/OnFailure kontrakt, oddělení výsledků A/B, nejistotu publikace,
plnou vzdálenou identitu/hashe a absenci destruktivní retence. Samotný
`git diff` neukazuje obsah untracked souborů. Tento podklad netvrdí, že externí
Claude review N4B1 již proběhl.
