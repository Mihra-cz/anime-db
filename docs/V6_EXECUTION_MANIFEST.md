# V6 immutable execution manifest a read-only preflight

[Manifest foundation](../app/target_execution_manifest.py) zachycuje jeden fresh
Target Planner result. Workflow končí `GENERATE → REVIEW → PREFLIGHT →
READY_FOR_EXECUTION`. Produkční filesystem executor, DB reconciliation,
rollback/recovery ani purge nejsou implementované.

## Immutable approval boundary

Versioned canonical JSON payload má stabilní SHA-256 identity. Čas vytvoření
a konkrétní warning acknowledgements jsou envelope evidence mimo semantic hash.
Změna envelope nevytváří nové targets. Datový model obsahuje immutable scalar
values a tuples; JSON loader odmítá neznámé verze, neznámá pole a chybný hash.

Nově generovaný manifest v2 nese planner/policy identifiers, DB fingerprint, filesystem baseline,
library/mount identity, explicitní `windows_root`, ordered actions, target directory set, locator patches,
warnings, duplicate evidence, preconditions a expected post-state hash.
Dry-run manifest nemění. Fresh planner používá pouze pro stale/parity comparison;
nenahrazuje schválené targets novým plánem. Relevantní změna DB, source inventory
nebo planner evidence znamená `STALE` a zastavení.

## Actions a locator patches

Podporované actions jsou `KEEP`, `MOVE`, `QUARANTINE`. `DELETE` není součástí
této verze. Source musí být regular file pod schváleným rootem s očekávanou
velikostí a `mtime_ns`; změna těchto stat values je hard stale check. Target
vyžaduje no-overwrite. Settled `KEEP` má shodné source a target.

Target directory set obsahuje explicitní parents v pořadí parent-before-child.
Preflight pouze kontroluje existující namespace: symlink, conflicting file,
escape, path/component limit, collision nebo neřešená graph occupancy/cycle
blokují readiness. Nový target occupant/symlink nebo symlink místo source po
sealingu je změna filesystemu, tedy `STALE` s konkrétní diagnostikou; stejný
manifest už nemůže být READY. Nevytváří adresáře, staging ani copy/delete fallback.
Logical moved bytes nejsou požadavkem na stejný objem volného místa pro
same-filesystem rename. Snapshot/COW overhead vyžaduje samostatné ověření.

Locator-only patch whitelist odpovídá post-state closure:

| Objekt | Povolená physical locator fields |
| --- | --- |
| Video | `relative_path`, `root_folder` |
| CatalogCollection, CatalogTitle | `relative_root_path` |
| ExternalSubtitle, UnresolvedExternalSubtitle | `relative_path` |

`Video.filename` a `UnresolvedExternalSubtitle.filename` zůstávají source/parser
evidence. Physical basename se odvozuje z `relative_path`. Manifest nepřidává
schema ani content/hierarchy/numbering/metadata/compatibility authority.

## Duplicate execution evidence

Každý confirmed secondary nese secondary/primary ID, validity při generation,
identities/paths pouze vlastněných side assets, classification provenance,
případnou archive identity/hash evidence a accounting stav `COMPLETE`,
`INCOMPLETE` nebo `UNKNOWN`. Chybějící evidence znamená `UNKNOWN`, nikdy
`COMPLETE`. Relocation nemění `INCOMPLETE` na `COMPLETE`.

Evidence přežije JSON round-trip a předává se explicitně post-state verifieru.
Primary archive locator ukazuje na approved post-state cestu; například Uzaki
primary ZIP v `Subs` se kvůli rekonstrukci provenance znovu nehashuje.
Jsou to execution facts, nikoli AnimeDB domain authority. Quarantine je povolená
i při `INCOMPLETE`/`UNKNOWN`; žádný z těchto faktů neopravňuje purge.

## Criticality a preservation

`PRIMARY`/`LOW` řídí pouze future execution failure severity. Nemění Naming,
Layout, Hierarchy, Metadata ani content classification.

- `PRIMARY`: Season episodes, Film, OVA, Special, Recap, authoritative story
  Preview a Mini Dra či podobný samostatný Bonus s explicitní own-content
  human/confirmed metadata authority.
- `LOW`: OP, ED, NCOP, NCED, CM, PV, Menu a doložené promotional/technical extras.

Generic Bonus ani DramaCD nemají automaticky LOW. Chybějící explicitní authority vyžaduje
criticality review; samotný název adresáře nebo Bonus kontejner ji nevytváří.
Source/parser PV evidence se odlišuje od authoritative story Preview.
Konkrétní human criticality evidence je execution rozhodnutí, nikoli změna
doménových polí.

Human decision z 2026-10-08 uzavírá současných 11 criticality reviews jako LOW:

- Overlord Drama CD, Video IDs **1964–1971**: hodnotnější příběhový Bonus,
  který není samostatným anime alespoň na úrovni ONA/OVA.
- Tenki no Ko Bonus, Video IDs **2711–2713**: filmografie, music video a video storyboard.

Generation CLI předává tato rozhodnutí existujícímu `criticality_decisions`
contractu jako konkrétní severity/reason/actor evidence. Allowlist kontroluje
přesná Video IDs, současný Bonus typ a title/collection owners (Overlord 147/115,
Tenki 202/147); nepoužívá filenames ani source locators. Do manifestu se přenese
pouze authority pro actionable Video. CLI je jediná production composition těchto
rozhodnutí: `build_execution_manifest` pečetí pouze předanou evidence a preflight
re-projektuje sealed authorities. Přímé API volání bez CLI je fail-closed, položky
zůstanou REVIEW a dry-run hlásí `criticality_review_required`. Nový Bonus/DramaCD
toto rozhodnutí nedědí. Mini Dra zůstává PRIMARY podle vlastní standalone
authority a mandatory PRIMARY ochrana se nemění. Uzavření criticality review
nepotvrzuje warning acknowledgements ani externí prerequisites; production
dry-run zůstává NOT_READY.

Subtitle přes platné M:N compatibility přebírá nejvyšší criticality svých
compatible Videos. Pokud má pouze LOW relationships, je LOW.
Owner-less/unmatched a D06 `confirmed_no_match` mají mandatory PRIMARY safety
preservation bez vymyšleného Video ownera. Auxiliary preservation rovněž
nevytváří anime content identity.

Future PRIMARY failure znamená STOP. LOW continuation je možná pouze po přímém
durable `PENDING → FAILED`, při nedotčeném source, žádném částečně vytvořeném
target, journaled failure a žádné dependent PRIMARY action. Pozdější FAILED
vyžaduje STOP/reconciliation. [E1 journal](#e1-durable-execution-journal) trvale ukládá a validuje
caller evidence; [E2B](#e2b-scratch-only-koordinace-jedné-action) provádí pouze
izolované scratch filesystem transitions. Produkční executor není implementovaný.

## Warning approval

Acknowledgement obsahuje konkrétní stable warning ID, class, object identity,
actor a čas. Neexistuje class wildcard ani `ignore_warnings` bypass.
Neacknowledged warning blokuje readiness. Nový warning po schválení vyžaduje
nové review; acknowledgement staré class jej automaticky nepokrývá.
API `WarningAcknowledgement` ukládá tato rozhodnutí do approved envelope;
generování je samo nepotvrzuje. `CriticalityAuthority` obdobně nese konkrétní
human severity/reason/actor pro unresolved content. Mandatory PRIMARY nelze
takovým rozhodnutím snížit na LOW.

## E1: Durable execution journal

[Izolovaná journal vrstva](../app/target_execution_journal.py) implementuje lokální
trvalý zápis a kontrolované načítání. Existující `JournalEntry`, jeho canonical
serializace a `can_continue_after_failure` zůstávají kompatibilní. E1 neprovádí
filesystem actions, DB patches, reconciliation ani recovery. Nezavádí CLI command,
runtime assertions, maintenance lock ani execution authorization.

`DurableJournal.create(path, manifest)` výhradně vytvoří nový journal přes
`O_CREAT|O_EXCL|O_NOFOLLOW`, s permissions `0600`. Vyžaduje absolutní cestu bez
`..`, mimo repo i manifest/explicitní další library roots, na místním
filesystemu. Reuse CLI path checks odmítá neznámý FS, remote FS ze sdíleného
denylistu (nejde o allowlist) a input aliases; journal navíc odmítá
`tmpfs`/`ramfs`, ancestor symlinks, `#recycle`, hardlinks, nepravidelné soubory
a adresářové aliases (např. bind) samotného repo/library rootu. Rodič musí existovat, patřit
účinnému uživateli a nesmí být group/world writable. Directory descriptors jsou
otevřené bez následování symlinků; binding rodiče, file identity, permissions,
link count a size/mtime se kontrolují také během použití.

Writer drží neblokující výhradní `fcntl.flock` na původním file descriptoru po
celou session. Nezávislý loader používá shared lock a živého writera odmítne jako
`JournalBusyError`. Vytvoření druhého journalu na téže cestě je `FileExistsError`,
nikdy overwrite. In-process mutex serializuje append/close; writer zděděný přes
`fork` nesmí zapisovat ani čekat na mutex zděděný uzamčený z jiného threadu.
Používat context manager nebo explicitní `close()`.

Soubor obsahuje canonical UTF-8 JSON Lines s přesnými keys a newline framing:

- Header version 1: schema, `manifest_id`, SHA-256 canonical celého manifest
  envelope, sequence `0` a checksum. Vazba tedy zachová také konkrétní envelope
  acknowledgements a created_at; nemění jejich approval ani manifest semantic ID.
- Events: souvislé sequence `1..N`, digest předchozího frame, nezměněný
  `JournalEntry` a checksum. ID/index musí přesně odpovídat immutable actions.
- Při načítání se ověří canonical bytes, typy, schema/verze, checksum/hash chain,
  pořadí, identity a celý lifecycle; frame má velikostní limit 64 KiB. Duplicitní,
  neznámé, přeházené nebo neúplné records jsou STOP. Checksum není podpis ani
  důkaz pravdivosti caller assertions.

Vytvoření potvrzuje úspěch až po úplném header zápisu, file `fsync` a parent
directory `fsync`. Každý append používá `O_APPEND`, dokončí short writes/EINTR a
potvrdí nový in-memory stav až po file `fsync` a závěrečných binding kontrolách.
Chyba během zápisu/barrier nebo neočekávaná vnější změna writer session zablokuje;
žádný retry, truncate, unlink, overwrite ani oprava historie se neprovádí.
Neúspěšně vytvořený či částečný journal zůstává zachovaný pro diagnostiku.

### Explicitní lifecycle

Nová action začíná pouze callerem dodaným `PENDING`. Actions se zaznamenávají
v immutable manifest pořadí; nezačíná se další action při nedokončené předchozí.
Journal nemá scheduler, automatické přeskakování ani alternativní topological
ordering. Pokud canonical pořadí nemůže splnit dependencies, STOP; E1 žádný plán
nepřepisuje. Dependencies nové action musí mít explicitní `COMPLETE`.

| Předchozí stav | Přípustný další stav téže action |
| --- | --- |
| PENDING | FS_APPLIED nebo FAILED |
| FS_APPLIED | FS_VERIFIED nebo FAILED |
| FS_VERIFIED | DB_RECONCILED nebo FAILED |
| DB_RECONCILED | COMPLETE nebo FAILED |
| COMPLETE | Žádný; terminal |
| FAILED | Žádný; terminal, bez automatického retry |

Duplicitní, přeskočený nebo zpětný přechod je odmítnut před zápisem. Failure
evidence patří jen `FAILED`. Každý stav je explicitní caller evidence z již
provedené skutečné kontroly; E1 nepozoruje ani neodhaduje physical/DB outcome,
nepředvyplňuje stav podle target existence a sama nic takového nepotvrzuje.
REVIEW criticality nesmí vstoupit do execution lifecycle.

PRIMARY failure vždy STOP. Po LOW failure lze v téže živé writer session
zaznamenat `PENDING` následující nezávislé action pouze tehdy, když validovaná
historie dokládá bezprostřední durable `PENDING → FAILED` stejné action s přesným
manifest ID, action ID a indexem. Současně platí všechny původní podmínky
`can_continue_after_failure`: failure code, intact source, absent target,
clear PRIMARY dependencies, failure_journaled a žádná dependent PRIMARY action.
Context-free helper poskytuje pouze nutné podmínky; neobsahuje předchozí stav
a sám nedává continuation authority. Historickou podmínku vynucuje stejný
validator při živém append i při načítání historie.

`FAILED` po `FS_APPLIED`, `FS_VERIFIED` nebo `DB_RECONCILED` zůstává přípustným
diagnostickým záznamem, ale další execution vyžaduje STOP/reconciliation i při
všech boolean assertions True. E1 pozdní failure neodstraňuje ani neopravuje
a nepovolí následující action. Failed dependency není COMPLETE ani při jinak
bezpečném LOW failure. Neúplná, poškozená nebo nepotvrzená historie rovněž
nemůže povolit pokračování.

Boolean assertions nejsou samy důkazem skutečného FS/DB stavu. E2B ověřuje pouze
scratch filesystem; budoucí executor musí ověřit také DB stav a před fyzickou operací získat úspěšný
durable zápis `PENDING` nové action přes journal gate. Samotný context-free
`can_continue_after_failure` nesmí použít jako povolení execution. E1 nadále
neobsahuje executor, rollback, recovery ani automatické resume.

### Přerušení a kontrolované načítání

`load_journal(path, manifest)` je pouze inspekce. Vrací immutable
`JournalSnapshot.entries` a vždy `reconciliation_required=True`, včetně
header-only historie. Celý frame po pádu neprokazuje, že writer obdržel durable
acknowledgement; ztrátu celého posledního frame či všech events nelze rozlišit
od původního platného prefixu pouhým hash chainem. Proto E1 nemá API k otevření
existující historie pro další zápis a neumožňuje automatické resume ani při
zaznamenaném COMPLETE. Prázdný načtený journal není důkaz, že physical execution
nikdy nezačala.

Truncated, corrupt nebo s manifestem/lifecycle nesouhlasná historie vyvolá
`ReconciliationRequired` s record/byte diagnostikou a již validovaným prefixem
`entries`; prefix se nesmí použít jako resume authority. Všechny bytes zůstávají
beze změny. IO/path/lock errors rovněž znamenají STOP. Kontrolovaná physical/DB
reconciliation a případné další otevření budou samostatnou budoucí částí executoru.

Mechanismus používá standardní Linux/POSIX API bez x86 předpokladů a externích
závislostí. ARM64/RPi5 provoz zatím není otestován. `fsync` závisí na skutečné
podpoře filesystemu/hardware; persistent storage a container backing/retention
musí být doložené v cílovém execution contextu. Local FS či overlay classification
sama nedokazuje zachování artefaktů po zrušení kontejneru. Flock je advisory pro
spolupracující uživatele této vrstvy; nechrání proti privilegovanému falšování ani
nenahrazuje production maintenance/executor lock. Různé journal cesty neimplementují
globální nebo distribuovanou výluku execution nad jedním manifestem/knihovnou.

Journal nesmí měnit manifest/targets/criticality/warning approval ani doménovou
autoritu. **Executor NOT IMPLEMENTED. NOT AUTHORIZED FOR NAS EXECUTION.**

## E2A: Izolovaná filesystem rename primitive

[Filesystem primitive](../app/target_execution_filesystem.py) implementuje rename
jedné přesné sealed `MOVE` nebo `QUARANTINE` action. `KEEP` ověří source, ale
neprovádí syscall ani fsync. `DELETE`, `REVIEW`, REVIEW criticality, unresolved
generation diagnostics a actions s dependencies odmítá. Dependency orchestration
patří budoucímu executoru. Immutable manifest, `JournalEntry`, criticality,
warning approvals a locator patches se nemění.

Jediný veřejný vstup je `ScratchRenameSession.create(parent)`: explicitně vytvoří
nový prázdný `0700` scratch adresář v existujícím privátním stromu pod `/tmp`.
Nelze otevřít existující library/execution root, přepnout do production režimu
ani znovu otevřít ukončenou session. Testy připravují soubory výhradně uvnitř
`pytest tmp_path`. Allocator nevytváří target parents: ty musí již existovat.
Session vyžaduje výhradní kontrolu callerem nad scratch obsahem; permissions
nevylučují jiný proces stejného UID ani privilegovaného uživatele.

`session.rename(manifest, action)` kontroluje strict typed manifest/hash/action
identity přes existující serialization contract, přesnou shodu celé action se
sealed položkou a shodu library root/mount identity s drženou scratch session.
Foreign root odmítá ještě před path observations existujícího manifest validatoru.
Relativní cesty nesmějí obsahovat traversal, prázdné či dot components, backslash,
NUL, absolutní cestu ani `#recycle`.

Od `/` po root a dále po source/target parents se otevírají directory descriptors
s `O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC`. Rename používá pouze leaf names relativně
k těmto drženým descriptors. Source se otevírá s `O_NOFOLLOW|O_NONBLOCK`, musí
být regular file s jedním hardlinkem, vlastněný callerem, bez group/world write,
s přesnou manifest size/mtime_ns. Totéž ownership/write omezení platí pro root
a adresáře pod privátním scratch parentem. Source descriptor a současný
nofollow directory entry musejí mít stejné device/inode. Root, rodiče, mount
identity a source binding se znovu kontrolují těsně před syscall a po něm.
Stat evidence nenahrazuje content hash a nedokazuje bytes při zachování size/mtime.

### No-overwrite a mount policy

Primitive používá Linux libc `renameat2` výhradně s `RENAME_NOREPLACE`; libc volí
platformní ABI bez hardcoded x86/ARM syscall čísla. Chybějící symbol nebo
nepodporovaný kernel/filesystem znamená STOP. Target existence se kontroluje
nofollow a konečný atomic no-overwrite gate vynucuje syscall, včetně targetu
vzniklého po kontrole. Žádný `os.rename`/`os.replace`, copy/delete fallback,
staging, retry, collision repair, implicitní target mkdir ani purge neexistuje.

[Mount policy](../app/target_execution_mounts.py) je oddělená od mírnější
read-only CLI denylist policy. Scratch allowlist obsahuje pouze `ext4`, `btrfs`
a `overlay`; overlay povolení platí jen pro nový scratch strom. `tmpfs`/`ramfs`
se pro durability odmítají, stejně jako neznámé, síťové, virtiofs, fuse a ceph
filesystemy. Žádný typ ani úspěšný scratch syscall není production mount approval.

Policy vyžaduje čitelný a validní `/proc/self/mountinfo`, descriptor `mnt_id`
z `/proc/self/fdinfo`, device shodu a RW observations bez remount/probe.
Source i rodiče musejí mít stejné device a mount ID jako root; stejné device
samo neprokazuje absenci bind crossing. Mount uvnitř rootu (i mimo právě
používanou cestu) je odmítnut. Filesystem root offsets všech mounts se porovnávají
také pro differently named bind aliases rootu, jeho ancestors a libovolného
podstromu. Identické path exposure ve filesystem sandboxu není differently
named alias. Změněná mount table znamená STOP.

Tyto kontroly pokrývají viditelnou namespace, nikoli skryté mounts, další mount
namespaces nebo vzdálené klienty. Nezajišťují atomic inode-conditioned rename
ani globální výluku cizích writers/mount změn. Výhradní kontrola nad novým
scratch stromem je vstupní podmínkou. Pro produkci tyto vlastnosti zůstávají
nedoložené a E2A žádnou production authorization API nevystavuje.

### Syscall, durability a nejistota

| Výsledek | Význam |
| --- | --- |
| `RenameResult(outcome='KEEP', syscall_succeeded=False, durability_confirmed=False)` | Kontroly prošly; žádný fyzický rename ani durability barrier. |
| `RenameResult(outcome='DURABLE', syscall_succeeded=True, durability_confirmed=True)` | Rename uspěl, identity byly ověřeny a file/directory fsync barriers byly potvrzeny na scratch filesystemu. Není to DB reconciliation ani důkaz production hardware durability. |
| `RenameRejected` | STOP před syscall nebo validované známé syscall odmítnutí. Neuděluje LOW continuation authority ani booleans pro journal FAILED. |
| `RenameReconciliationRequired` | Syscall byl zkusen a výsledek či durability nelze bezpečně potvrdit; session je zablokovaná. Žádný automatický retry. |
| `KeyboardInterrupt` / `SystemExit` | Původní přerušení se propaguje beze změny třídy a instance. Přerušení během observe/rename/verify session zablokuje i před syscall; další `rename` vrací `RenameReconciliationRequired`. Po pokusu o syscall exception note vyžaduje STOP/reconciliation. |

Po úspěšném rename se ověří absent source a target binding k původnímu
source descriptoru, potom se synchronizuje file, target parent a source parent.
Při stejném parent inode se directory fsync provádí jednou. Závěrečná kontrola
znovu ověří paths, identities a mounts. File barrier nenahrazuje directory
barriers. Directory/hardware durability mimo scratch prostředí není ověřená.
`durability_confirmed` potvrzuje uvedené barriers, nikoli samostatně persistence
předchozího vytvoření celého ancestor stromu; future executor musí zajistit také
durable existenci execution rootu a parents před fyzickými actions.

`EIO`, `EINTR`, timeout, neznámá chyba, přerušení po zahájení syscall nebo změněný
stav po něm vyžadují reconciliation, i když okamžitá inspekce vypadá původně.
Známé refusal errno jako `EEXIST`, `EXDEV`, `ENOSYS`, `EOPNOTSUPP`, `EINVAL`,
`EACCES`/`EPERM` se označí jako rejection pouze po opětovné kontrole nedotčeného
source binding a absence původního source inode v targetu. Ani tento výsledek
neopravňuje pokračování. Každá action s pokusem o syscall je v živé session
neopakovatelná. Neurčitá session nepovolí ani další action.

Selže-li fsync nebo kontrola po úspěšném syscall, exception nese
`syscall_succeeded=True`, `durability_confirmed=False`; fyzicky přemístěný soubor
zůstává pro reconciliation. Při nejasném acknowledgement je
`syscall_succeeded=None`. Selhání nelze prezentovat jako bezpečné neprovedení.
Neexistuje automatické obrácení rename ani resumption po pádu.

E2A journal neotevírá ani nezapisuje `FS_APPLIED`, `FS_VERIFIED` či
`DB_RECONCILED`. E1 gate přímého durable `PENDING → FAILED` pro LOW pokračování
a bezpodmínečný PRIMARY STOP zůstávají beze změny. E2B spojuje pouze scratch
filesystem a journal lifecycle; samotný výsledek primitive nemění DB ani
nepotvrzuje reconciliation.

Read-only `session.observe(manifest, action)` drží původní source inode a rodiče
po dobu context manageru. `rename(..., observation=observation)` navíc porovná
skutečné syscall descriptors s touto evidence; nepovoluje jiný root nebo slabší
kontroly. `ScratchObservation.verify()` znovu otevírá parents a target přes
nofollow dirfds a porovnává je s původní evidence. Neprovádí rename ani fsync.
Držený source descriptor brání falešné shodě přes recyklovaný inode. Expired,
foreign nebo inherited observation je odmítnutá. KEEP observation lifecycle nemá.

Testy zahrnují skutečný scratch rename a souběh syscallů na jeden target;
mount/bind/EXDEV/permission/IO faults jsou tam, kde je potřeba, explicitně
injektované. Simulace prověřuje refusal a uncertainty logic, nikoli skutečné
mount properties NASu. RPi5/ARM64, production ext4/btrfs, NAS/CIFS support,
power-loss durability, jiná namespaces a production exclusion jsou **NEOVĚŘENÉ**.
E2A nemá napojení na `generate-manifest`, `dry-run` ani jiné CLI.
**Executor, DB reconciliation, rollback a recovery NOT IMPLEMENTED.
NAS EXECUTION NOT AUTHORIZED.**

Linux podklady: [renameat2](https://www.man7.org/linux/man-pages/man2/renameat2.2.html),
[fsync](https://www.man7.org/linux/man-pages/man2/fsync.2.html),
[mountinfo/fdinfo](https://www.kernel.org/doc/html/latest/filesystems/proc.html).

## E2B: Scratch-only koordinace jedné action

[Koordinátor](../app/target_execution_coordination.py) má pouze factory
`ScratchActionSession.create(parent)`. E2A vytvoří nový privátní root pod `/tmp`;
session sama určí sousední journal path. Caller připraví izolované assets a
syntetický immutable manifest. `execute(manifest, action, action_index=0)` přijímá
výhradně jednoakční MOVE/QUARANTINE manifest. Neexistuje argument pro převzetí
existujícího rootu, session, journal writera či načtené historie. KEEP, DELETE,
dependencies, REVIEW a víceakční manifest jsou odmítnuté.

Po strict manifest/action validation a root/mount binding checks se zachytí
read-only pre-rename evidence. E3B nyní povinně doplňuje durable E3A identity
artefakt a kontroly popsané níže; teprve pak E1 vytvoří nový journal přes `O_EXCL`.
Nové read-only `DurableJournal.inspect_live(manifest)` kontroluje přesný envelope,
file/parent binding, canonical historii a shodu s durable acknowledged entries
živého writera. Nemění journal ani neotevírá existující writer pro resume.

E2B journal/filesystem lifecycle zůstává pevný; úplné pořadí s E3B gates je níže:

1. Durable `PENDING`: append, file fsync, potvrzení E1 a live history kontrola.
2. E2A rename s původní observation; až výsledek `DURABLE` povolí další krok.
3. Durable `FS_APPLIED` append a kontrola live historie.
4. Nezávislé read-only `observation.verify()`: source locator absent včetně
   symlinků, target regular file s původním device/inode, size/mtime_ns, bezpečné
   původní parents/root a stejný mount binding. Pouhá existence ani RenameResult
   nejsou verification evidence.
5. Durable `FS_VERIFIED` append a kontrola live historie; teprve pak
   `VerifiedScratchOutcome(outcome='FS_VERIFIED')`.

Živou kontinuitu původního inode poskytuje držený source descriptor. E3B také
uchovává pozorované device/inode/metadata a plný content hash v odděleném E3A
artefaktu. Journal frames se nemění a tuto evidence nenesou. Ani persistent
dev/inode a shodný obsah po ztrátě živého důkazu neprokazují původ fyzického inode.
Read-only restart inspekci poskytuje E4A níže; recovery není implementované.

Session je jednorázová i při odmítnutí; descriptors se při návratu/chybě zavřou
a artefakty zůstanou pro diagnostiku. Žádné automatické retry/continuation,
FAILED boolean assertions, DB_RECONCILED nebo COMPLETE se nezapisují.
FS_VERIFIED není dokončení celé action ani DB reconciliation. E1 původní LOW
continuation pravidla se nemění; E2B při každé chybě zastaví.

| Crash/failure window | Výsledek a zachovaná evidence |
| --- | --- |
| Před potvrzeným PENDING, včetně header/append/fsync chyby | Žádný rename. STOP; journal bytes mohou obsahovat neacknowledged nebo částečný frame. |
| Durable PENDING, před syscall | STOP; source může zůstat původní. Journal neuděluje resume authority. |
| Zahájený syscall, neznámý acknowledgement nebo FS fsync chyba | FS reconciliation required; PENDING může nesouhlasit s fyzickým stavem. Žádný druhý rename. |
| DURABLE rename, před FS_APPLIED acknowledgement | Journal reconciliation required při journal chybě; přesunutý soubor se zachová, žádný rollback. |
| Durable FS_APPLIED, před úspěšnou independent verification | FS reconciliation required; target existence neprokazuje správnou identitu. |
| Verification prošla, FS_VERIFIED append/fsync selhal | Journal reconciliation required; zachovaná celá frame není durable acknowledgement. |
| Process crash / poškozená historie při následné inspekci | E1 loader poskytuje jen diagnostic prefix, vždy reconciliation required; žádný recovery writer. |

`ScratchCoordinationStopped` je známé bezpečné odmítnutí/STOP;
`ScratchFilesystemReconciliationRequired` je nejednoznačný physical/evidence
výsledek; `ScratchJournalReconciliationRequired` je journal durability/integrity
chyba. Výjimky zachovávají příčinu a phase. `KeyboardInterrupt` a `SystemExit`
se propagují jako původní instance s phase/STOP note, nikoli jako běžné rejection;
session je spotřebovaná, žádný catch-all Exception je nezamlčuje. Po pokusu o
syscall E2A navíc zablokuje filesystem session a vyžaduje reconciliation.

Cleanup E1/E2A/E2B zachovává původní běžnou STOP chybu a další běžné chyby
připojí diagnosticky. Cleanup `KeyboardInterrupt`/`SystemExit` má před běžnou
STOP chybou přednost; propaguje se původní instance s nezměněným exit code
a původní STOP zůstává v exception cause. Pokud už je primární chybou přerušení,
zachová se právě toto přerušení i při dalším cleanup přerušení. Bez primární
chyby se cleanup přerušení propaguje přímo. Zbývající bezpečný cleanup proběhne,
diagnostika všech chyb se zachová a žádný nejistý close se neopakuje. E2A
zablokuje session při každém přerušení během observe/rename/verify, i před
syscall; žádný retry se neuděluje. Spotřebovaná E2B session si ponechá kategorii
a phase primární STOP chyby; cleanup přerušení ji nepřepíše.

Testy provádějí skutečné isolated scratch renames a read-only inode observations,
injektují chyby a deterministicky ukončují child proces na lifecycle boundaries.
To ověřuje ordering a STOP policy, nikoli power-loss durability NASu. Zůstávají
všechna omezení E2A: exclusive scratch ownership, viditelná mount namespace,
nedoložená produkční durability/storage/mount capability a ARM64/RPi5 chování.
Globální execution lock, produkční maintenance boundary, DB reconciliation,
crash recovery, rollback, resume a produkční executor **nejsou implementované**.
Generate-manifest/dry-run nemají cestu k této vrstvě; Naming/Hierarchy/Metadata,
D06 approval a schema se nemění. **NAS execution není autorizovaná.**

## E3A: Scratch-only durable file identity evidence

[E3A](../app/target_execution_identity.py) poskytuje samostatný artefakt identity,
strict načtení a read-only porovnání jedné MOVE/QUARANTINE action. Samostatná E3A
nezapisuje journal ani neprovádí rename; E3B ji používá jako povinnou podmínku
scratch coordination. Evidence format, journal frames a transitions se nemění.
Read-only restart inspekce E4A je popsaná níže. Evidence ani výsledek
MATCH nejsou execution authority, READY_FOR_EXECUTION, FS_VERIFIED, COMPLETE či
DB reconciliation.

Jediná factory je `ScratchIdentitySession.create(parent)`. E2A vytvoří nový
privátní scratch root pod `/tmp`; evidence path se pevně odvodí jako sousední
`<root-name>.identity.json`, mimo root/media i repo. Nelze předat existující root,
libovolnou output path ani převzít session jiného procesu. Caller připravuje
pouze izolované scratch assets a vlastní je výhradně. Veřejné metody jsou
`capture(manifest, action, action_index=...)`, `load(...)`, `compare(...)`
a `close()`/context manager; žádný rename, recovery, rollback ani resume API.
Každá session smí zkusit capture jen jednou, včetně odmítnutých pokusů.

### Evidence format 1

Jeden immutable UTF-8 canonical JSON record s přesnou závěrečnou newline:
`{"evidence":<body>,"sha256":<SHA-256 canonical body>}`. Keys jsou seřazené,
separators bez whitespace, Unicode escapované stejně jako v manifest/journal
kontraktu. Maximum je 64 KiB. Duplicate/unknown/missing keys, neplatné typy
(včetně bool místo int), necanonical bytes, další records, neúplná newline,
checksum mismatch nebo neznámá verze znamenají STOP. Checksum není podpis
a nechrání před cíleným falšováním jiným writerem stejného UID/privilegovaným
uživatelem. Žádná oprava, truncation ani přepsání poškozené evidence neexistuje.

Body má přesná pole frozen `FileIdentityEvidence`:

| Pole | Pozorovaná nebo immutable vazba |
| --- | --- |
| `schema`, `version` | `animedb-v6-file-identity`, integer `1` |
| `manifest_id`, `manifest_sha256` | Semantic manifest ID a SHA-256 canonical celého envelope včetně created_at/acknowledgements, stejně jako E1 binding |
| `action_index`, `action` | Přesný index a celý nezměněný `ManifestAction`: action ID, object kind/ID, schválené source/target, preconditions, criticality a další immutable payload |
| `binding` | Root path a root/source-parent/target-parent device/inode; mount ID, fstype/source/point/fs-root a SHA-256 canonical raw mount record/table values |
| `filesystem` | Skutečně pozorované `st_dev`, `st_ino`, size, mtime_ns, ctime_ns, regular-file type, nlink, mode, uid a gid |
| `content` | `algorithm='sha256'`, digest, `coverage='FULL'` a skutečně přečtený `bytes_hashed` rovnající se size |

Timestampy pocházejí pouze z file stats; artefakt nepřidává umělý capture čas,
maintenance/capability assertions ani falešný durable timestamp. Ctime pomáhá
odhalit změnu během čtení/capture; po rename se jako stabilní precondition
neporovnává, protože rename jej může změnit.

### Capture, durability a partial write

Capture validuje stávající strict immutable manifest/action contract a vazbu
na factory root/mount ještě před čtením souboru. E2A observation drží očekávaný
regular file a bezpečné parents. SHA-256 používá bounded `pread` po 64 KiB až
do EOF; počet bytes a stat včetně ctime se kontrolují před/po každém full read.
Verze 1 odmítá scratch soubory větší než **8 MiB** před content read; limit je
pouze scratch hranice, nikoli produkční video policy.

Artefakt používá E1 local-output/alias checks a nofollow directory descriptors,
E2A scratch mount allowlist/bind-crossing policy, `O_EXCL|O_NOFOLLOW`, mode 0600
a výhradní flock. Symlink/hardlink/special file a unsafe parent se odmítají.
Root a artefakt parent/mount binding se ověří ještě před vytvořením souboru.
Short writes se dokončí; write/fsync chyba či EINTR se automaticky neopakuje.
Po úplném zápisu následují **file fsync a parent directory fsync**.

Po barriers se znovu přečte celý source a porovná hash/metadata; ověří se jeho
skutečný nofollow locator, target absence, root/parent/mount binding a aktuální
artefakt bytes/identity. Úspěch capture se potvrzuje až po těchto kontrolách
a úspěšném cleanupu dočasných descriptors. Selhání před pokusem o vytvoření
artefaktu je `IdentityEvidenceRejected`, změna source během čtení jeho podtřída
`IdentityEvidenceReconciliationRequired`; po pokusu o vytvoření vždy tato
podtřída (přerušení se propaguje beze změny).
Dvojí čtení a stat/ctime kontroly odhalí pozorovatelné změny, ale stabilitu
source mimo okamžiky čtení a po capture nezaručují; bez maintenance boundary je
to neověřený předpoklad exkluzivního vlastnictví. Selhání zachová všechny existující
bytes pro diagnostiku, session zablokuje a neprovede rename ani journal append.
KeyboardInterrupt/SystemExit a cleanup precedence respektují E2B-MINOR-01.

Živá E3A session drží také read-only artefakt descriptor a jeho parent descriptor,
pořízené před capture acknowledgement. Read-only open má vlastní open-file
description; neduplikuje writerův exclusive flock. Původní record a pozorované
artefakt stats se uchovají jako immutable in-memory anchor. Následný `load`
odmítne změněný record, přepsání i výměnu artefaktu/parentu, včetně validního JSON
s přepočítaným checksumem. Po close se živé pins neužívají a nevzniká restart
provenance. Samotný parseable záznam nepředstavuje capture acknowledgement.

Pád před vytvořením může zanechat chybějící artefakt; partial write je neplatný
record. Celý record po write/fsync chybě může být strict parseable, ale sám
neprokazuje acknowledgement původního writera ani aktuální source stabilitu.
`load` proto potvrzuje pouze data/binding, nikoli durable acknowledgement či
continuation. Chyba `load` session zablokuje; další read-only diagnostika je
možná, ale ani externí obnovení původních bytes nemůže vrátit výsledek MATCH.
File/parent barriers nedokazují power-loss behavior storage ani
durable existenci všech ancestors nebo původních scratch assets.

### Read-only comparison a ztráta živé identity

`compare` znovu strict načte evidence a dirfd-relative otevře obě schválené
cesty. Ověří root, parents, mount, regular/single-link file, plný digest,
metadata a závěrečnou stabilitu locators i artefaktu. Neprovádí write/fsync,
nevytváří parents, nečte DB a nevybírá recovery akci.

Výsledek odděluje presence, skutečné file/content observations,
`filesystem_match` (shodu numerických attributes bez ctime), `content_match`
a `physical_identity_match`. Content digest prokazuje pouze shodu bytes.
Device/inode nejsou trvalým univerzálním identifikátorem: po uvolnění původního
file descriptoru mohou být recyklované, zvlášť napříč reboot/mount lifecycle.

E3A drží do `close()` původní file a parent descriptors. Celkový **MATCH**
vyžaduje právě jeden present locator a druhý absent, shodná metadata i obsah
a nepřerušený živý inode důkaz. Prokázaný rozdíl obsahu/metadata nebo jiný inode
oproti živému pinu znamená **MISMATCH**. Obě/žádná cesta, unsafe/unreadable
observations či ztráta původního pinu znamenají **UNKNOWN**. Stejné bytes
v jiném inode po ztrátě pinu stále znamenají UNKNOWN; shodný dev/inode bez
pinu jej rovněž nepovýší na MATCH. MISMATCH/UNKNOWN znamenají STOP.

Po `close()` zůstává ve stejném factory-minted objektu povolená pouze read-only
inspekce jeho původního scratch kontextu; živý inode důkaz je již ztracený.
To umožňuje deterministicky simulovat ztrátu in-memory evidence, **neotevírá
existující root po skutečném restartu**. Pro child-process crash testy zbývají
pouze persistent bytes a E1 diagnostic history; žádné restart writer API.
Samostatná E4A níže umožňuje read-only inspekci persistent scratch kontextu
v novém procesu a tuto nejistotu zachovává.

Plný hash velkých videí bude vyžadovat celý soubor přečíst; nynější capture jej
čte dvakrát a comparison znovu. Paměť zůstává bounded, ale I/O a čas jsou
lineární v délce obsahu. Produkční cost/storage policy se tím neschvaluje;
rychlý částečný fingerprint není náhradou za FULL content proof. Produkční
knihovna se nehashuje. Scratch/fault/process-exit testy nejsou NAS power-loss
ani ARM64/RPi5 capability proof. Produkční mount a platforma jsou **NEOVĚŘENÉ**.
Globální executor/lock, maintenance boundary, DB reconciliation, recovery,
rollback a resume stále chybějí. **NAS execution není autorizovaná.**

Otevřené INFO-1 (možný fd leak při `os.fdopen(os.dup(...))`) a INFO-2 (riziko
close recyklovaného fd v dřívějším testu) zůstávají neřešené; E3A tyto větve
nemění. Naming/Hierarchy/Metadata authority, D06 approval a schema se nemění.

## E3B: Povinná durable identity evidence ve scratch coordination

E3B integruje E1 durable journal, E2A scratch rename a E3A durable identity pro
jednu MOVE/QUARANTINE action, výhradně index 0. **Není produkční executor.**
Veřejná factory zůstává `ScratchActionSession.create(parent)`; nevzniká nový
root/session/journal adoption, reopen ani resume argument. Factory vytvoří jednu
`ScratchIdentitySession`, která vytvoří a vlastní jedinou `ScratchRenameSession`.
Koordinátor filesystem a capture pins pouze vypůjčuje a vlastní nový E1 writer.
Evidence a journal jsou sousední artefakty téhož nového privátního `/tmp` rootu.
Caller poté připraví scratch soubory a syntetický manifest; `execute` validuje
jeho přesnou vazbu na tento factory context před capture a jakýmkoli zápisem.
`evidence_path` je read-only odvozená property, nikoli volitelná output path.

### Pořadí a binding

1. Strict immutable single-action validation a E2A observation původního source,
   parents, rootu a mountu.
2. E3A capture: plný streaming SHA-256, skutečné file stats, přesný manifest
   envelope/action/index/root/mount binding; exclusive evidence create, file
   fsync, parent fsync, opakované source hash/stability checks a úspěšný cleanup
   dočasných descriptors. Artefakt/source/parent pins zůstávají u E3A vlastníka.
3. `EVIDENCE_VERIFY`: strict load roven potvrzenému capture; artefakt/pins beze
   změny. Capture filesystem evidence musí odpovídat původní E2A observation.
   Fresh E3A MATCH musí ukázat source PRESENT, target ABSENT, původní živý inode,
   shodný plný obsah i metadata.
4. Nový E1 journal, durable header; durable PENDING a `inspect_live` kontrola.
5. `PRE_RENAME_IDENTITY`: nové plné read-only E3A porovnání i artefakt checks,
   opět source PRESENT / target ABSENT / živý inode MATCH.
6. E2A `renameat2(RENAME_NOREPLACE)` s původní observation. Pouze potvrzený
   `DURABLE` výsledek po file/parent barriers dovolí pokračovat.
7. Durable FS_APPLIED a kontrola live journal historie.
8. Nezávislá E2B `observation.verify()` znovu otevře locators a ověří skutečný
   post-rename stav. Výsledek syscallu tuto kontrolu nenahrazuje.
9. `IDENTITY_VERIFICATION`: E3A opět strict načte původní capture a nově hashováním
   porovná skutečný soubor; povinné source ABSENT / target PRESENT, původní živý
   inode, odpovídající metadata/bindings a FULL content MATCH.
10. Teprve poté durable FS_VERIFIED a live history kontrola. `VerifiedScratchOutcome`
    znamená pouze ověřený scratch filesystem výsledek, **nikoli COMPLETE nebo DB
    reconciliation**.

Před i po každém journal append se znovu kontroluje původní artefakt. Podvržené
validní records se nesmějí stát novým capture anchor. Evidence není nahraditelná
tvrzením volajícího, boolean assertions nebo samotným `outcome='MATCH'`: gate
kontroluje i direction/presence a skutečná file/content observations. Porovnávání
je read-only, neprovádí další fsync, rename nebo změnu journalu.

### STOP a crash windows

| Window | Zachovaný stav / klasifikace |
| --- | --- |
| Validation nebo známé odmítnutí před evidence create | STOP, žádný journal ani rename. |
| Evidence create/write/fsync zahájeno, acknowledgement selže | Filesystem reconciliation required v EVIDENCE_CAPTURE; zachovat partial/full bytes, žádný rename. |
| Evidence durable, PENDING ještě nevzniklo | Zachovat artefakt, STOP při přerušení; žádný automatic resume. |
| PENDING append/header/fsync selže | Journal reconciliation required; evidence zůstává, žádný rename. |
| Evidence + PENDING durable, source/content/inode/parents nebo artefakt se změnily | STOP/filesystem reconciliation; žádný rename. |
| Přerušení po PENDING před syscall | Session spotřebovaná; PENDING neuděluje pokračování. |
| Syscall zahájen, výsledek nebo fsync nejasný | Filesystem reconciliation; historie může končit PENDING při fyzicky přesunutém souboru. |
| DURABLE rename, FS_APPLIED append selže | Journal reconciliation; target zachovat, žádný rollback. |
| FS_APPLIED durable, independent/E3A verification selže nebo vrátí UNKNOWN/MISMATCH | Filesystem reconciliation; žádný FS_VERIFIED. |
| E3A MATCH, FS_VERIFIED append/fsync selže | Journal reconciliation; nezaměňovat parseable bytes za acknowledgement. |
| FS_VERIFIED durable, další kontrola artefaktu selže nebo proces skončí | Zachovat historii a files; žádná oprava historie, COMPLETE ani DB_RECONCILED. |
| Source i target existují nebo oba chybějí | E3A UNKNOWN / STOP; žádný výběr locatoru odhadem. |

E3B při žádné chybě nepokračuje. Session mutex serializuje současná volání; jen
první smí zkusit action/capture a každá session je jednorázová i při odmítnutí.
Repeated execute zachová kategorii a phase původního STOP. Cleanup zavře writer,
potom identity vlastníka včetně všech pins a jediného filesystem root fd; borrowed
filesystem se nezavírá samostatně. Factory failure rovněž uvolní svého vlastníka.
Původní KeyboardInterrupt/SystemExit instance, exit code a příčina zůstávají podle
E2B-MINOR-01; bezpečný cleanup pokračuje a žádný nejistý close se neopakuje.

Crash testy deterministicky ukončují child proces nad tmp_path. Po jeho ztrátě
zbývá jen strict byte diagnostika a read-only E1 historie, nikdy restart execution.
E4A níže doplňuje read-only restart inspekci. **Recovery, rollback a resume stále
neexistují.** Persistent inode/metadata/hash neprokazují nepřerušenou historii inode.

Limit 8 MiB patří scratch mechanismu, není produkční omezení velikosti videa.
Capture čte celý obsah dvakrát, další identity gates jej čtou znovu; I/O cost
velkých videí a produkční policy nejsou vyřešené. Exclusive scratch ownership
zůstává předpoklad, nikoli globální writer/maintenance lock. Kontroly nejsou
atomické s každým možným cizím writerem; jiné UID/namespaces/privilegované změny
nejsou pokryté produkční výlukou. Scratch/fault testy nepotvrzují NAS power-loss
durability. **ARM64/RPi5 a NAS mount capabilities jsou neověřené.** Produkční
executor, maintenance boundary, globální lock a DB reconciliation chybějí.
Generate-manifest/dry-run/CLI nemají cestu k execution; Naming/Hierarchy/Metadata,
D06, schema a otevřené INFO-1/INFO-2 se nemění. **NAS execution není autorizovaná.**

## E4A: Read-only restart inspekce

[API](../app/target_execution_reconciliation.py)
`inspect_scratch_action(manifest, action, action_index=..., scratch_root=...,
journal_path=..., evidence_path=...)` otevře pouze existující E2A scratch root
v privátním adresáři pod `/tmp` a jeho původní sousední E3B artefakty.
Nevytváří execution session, writer ani adresář. Nemá CLI ani napojení na
produkční manifest/preflight, scanner, startup či DB. Nezávislé review E4A
dopadlo PASS.

Používá stejné strict manifest/action, E1 journal a E3A evidence validátory,
nofollow dirfds, private regular/single-link file checks a scratch mount policy.
Kořen a cesty artefaktů odmítne před validátory, které mohou lstatovat root;
symlinky nenásleduje. Artefakty drží pouze read-only se shared nonblocking flock,
kontroluje jejich file/parent binding a závěrečnou stabilitu observations.
Neexistující či unsafe artefakt nebrání bezpečné diagnostice ostatních vstupů.
Unsafe parent/root však neopravňuje k čtení obsahu přes tuto cestu.

Frozen report obsahuje manifest/action/index/root, journal status a poslední
validní zaznamenanou fázi konkrétní action, validitu a načtený record evidence,
obě locator observations, rozpory, důvody STOP a diagnostiku. `VALID` označuje
validované pozorované bytes; neprokazuje úplnost historie ani acknowledgement
původního writera/capture. Poškozená historie zachová pouze validovaný prefix.
Ztrátu celých posledních frames nelze ze zbývajícího validního prefixu dokázat:
`history_completeness` i `capture_acknowledgement` proto zůstávají `UNKNOWN`.
Checksum odhaluje poškození, nikoli cílené přepsání a přepočítání checksumu
po ztrátě původního živého artefakt anchor.

| Pole | Význam |
| --- | --- |
| `observed_state` | `SOURCE_ONLY`, `TARGET_ONLY`, `BOTH_PRESENT`, `BOTH_ABSENT`, `UNSAFE` nebo `UNKNOWN`; pouze aktuální pozorování |
| `content_match` | Plný SHA-256 a byte count proti validní evidence; bez ní `UNKNOWN` |
| `filesystem_match` | Shoda numerických metadat včetně dev/inode, bez ctime měněného rename; bez evidence `UNKNOWN` |
| `physical_identity_match`, `identity_continuity` | Vždy `UNKNOWN`: nový descriptor nenahrazuje původní živý inode pin |
| `outcome`, `manual_review_required` | Vždy `STOP`, `True`; ani shodný obsah a metadata neopravňují k pokračování |

Source i target present, oba absent, změněný obsah/metadata, unsafe soubor,
neplatný binding nebo nedostatek důkazů mají konkrétní STOP důvod. Target-only
při PENDING je nezaznamenaný filesystem progress k vyhodnocení, nikoli odhad
provedeného rename. Zaznamenané FS_APPLIED/FS_VERIFIED při chybějícím targetu
je rozpor. **PENDING nedokazuje rename, FS_APPLIED nedokazuje DB aktualizaci
a FS_VERIFIED nedokazuje COMPLETE.** Ani případné pozdější fáze v E1 historii
se nepotvrzují jako aktuální DB stav.

API nic nepřesouvá, nepřejmenovává, nevytváří parents, nevolá fsync,
nepíše journal/evidence/DB, nevybírá opravu a neprovádí rollback/resume.
Nevzniká COMPLETE ani DB_RECONCILED. Scratch hranice 8 MiB a exclusive ownership
zůstávají; nejde o produkční recovery, reboot/mount recovery ani NAS capability.

Testy ukončí původní proces přes SIGKILL při PENDING před rename, po rename před
FS_APPLIED, po FS_APPLIED a po FS_VERIFIED. Další nový proces provede inspekci;
parent vyžaduje úspěšný exit i závěrečný assertion marker, takže předčasný exit 0
není PASS. Audit odmítá write syscalls a vytvoření execution kontextu;
scratch bytes/hash, paths a metadata kromě kernelových read access times
se porovnávají před/po každé inspekci. **NAS execution není autorizovaná.**

## E5A: Jeden scratch Video MOVE a testovací SQLite

`ScratchActionSession.execute_video_move(manifest, action, action_index=0)`
integruje jednu syntetickou Video MOVE action s callerem připravenou SQLite
na read-only property `session.database_path`: sousední `<root-name>.sqlite`.
Přijímá pouze nově vytvořený privátní E2A scratch context pod `/tmp`.
Nezávislé review E5A dopadlo PASS.

Původní `execute()` dál končí na FS_VERIFIED a zavírá své prostředky.
Obě metody sdílejí původní jednorázové jádro koordinátoru; E5A pokračuje uvnitř
stejného volání, než se writer a identity pins uzavřou. Jediným vlastníkem
jediného nového E1 writeru je koordinátor. E1 frame format, hash chain,
transitions a strict validators se nemění. Žádné otevření uzavřeného writeru,
append mimo E1, převzetí existujícího rootu ani restart/resume API nevzniká.

### DB scope a předpoklady

[DB helper](../app/target_execution_database.py) přijímá pouze odvozenou
sousední testovací DB. Nezakládá schema, nemigruje a nespouští app/scanner.
Soubor musí již existovat jako privátní independent regular file na stejném
scratch mountu. Nofollow descriptors a původní parent/file/mount binding
zůstávají držené po celou operaci. Platí původní exclusive scratch ownership,
včetně DB a jejích sidecars; nejde o produkční maintenance boundary proti
cizím same-UID nebo privilegovaným změnám cest. Chybějící, prázdná nebo nepodporovaná SQLite,
WAL, `-wal`/`-shm` nebo existující `-journal` znamenají STOP před RW připojením.
Kontrola [SQLite headeru](https://sqlite.org/fileformat.html#file_format_version_numbers)
zabraňuje otevření WAL ještě před SQL PRAGMA kontrolou. Používá se `mode=rw`
bez implicitního vytvoření DB a DELETE transaction journal se synchronous FULL.
Sdílený E3A full-file reader omezuje také scratch DB fingerprint na 8 MiB.

Před rename se získá SQLite `BEGIN IMMEDIATE` writer reservation. Ověří se
celý manifest-bound DB fingerprint, jedno pozitivní integer Video.id,
source/size/mtime, původní relative_path/root_folder a cílová DB kolize.
Manifest musí mít právě tuto MOVE action a použitelný expected post-state hash.
Všechny locator patches musí přesně odpovídat skutečným změnám tohoto Video
na action.target a jeho první path component. Další Video/title/collection/
subtitle patch, vynechaná potřebná změna či nesprávné before/after znamenají
STOP; patches se nepřiřazují k action odhadem. Pokud root_folder zůstává stejný,
nový patch se nevymýšlí. Video.filename a všechny ostatní columns zůstávají.

### Pořadí a ověření

1. DB preconditions a writer reservation, pak nezměněné E3B capture/journal/
   rename/identity gates až po durable FS_VERIFIED.
2. Nová strict kontrola živého E1 journalu, independent filesystem verification
   a plný live inode/content MATCH. Teprve potom jeden podmíněný UPDATE videos,
   který nastaví pouze relative_path/root_folder podle přesných before hodnot.
3. Před commitem kontrola všech app rows a schema/user_version proti projekci
   právě těchto povolených fields jednoho Video; kontrolují se i trigger side
   effects. Vše je v jedné explicitní SQLite transakci, bez savepointů a ORM flush.
4. Jediný commit. Při nejasném výsledku žádný druhý pokus. Po potvrzení se získá
   nová prázdná writer reservation pro následné čtení a journal acknowledgement.
5. Jiná read-only connection ověří skutečně uložené rows/schema. Běžný
   `load_planner_context` a scratch inventory musí přes existující
   `post_state_hash_from_manifest` vrátit schválený expected hash. Znovu se ověří
   živý inode, obsah i manifest-bound journal; až potom E1 append DB_RECONCILED.
6. Nové DB/post-state/filesystem/identity/journal ověření, pak E1 COMPLETE.
   Teprve po acknowledged append a cleanupu se vrací CompletedScratchVideoOutcome.

Při DB chybě před commitem se neuložená transakce při uzavření zruší. Fyzický
přesun se nevrací. Po commitu se DB neopravuje zpět a ani nejistý commit se
neopakuje. Journal/evidence bytes a částečný filesystem stav se zachovají pro
inspekci. Celý parseable frame po ztrátě fsync acknowledgement stále není
potvrzení writera: E5A vrací STOP i při zachovaném DB_RECONCILED/COMPLETE frame.
Session je spotřebovaná po úspěchu i chybě; další execute nic neopakuje.

E4A zůstává beze změny read-only: po zániku původního procesu nezná kontinuitu
inode ani původní commit/journal acknowledgement a neposkytuje resume authority.
Kill testy pokrývají FS_VERIFIED před DB update, commit před DB_RECONCILED
a DB_RECONCILED před COMPLETE. Původní proces je skutečně SIGKILL/reaped;
nový inspekční proces musí dokončit assertions a vydat terminal marker.
Exit 0 před assertions není PASS. Inspekce porovnává bytes/hash a metadata
scratch artefaktů před/po; FD cleanup a ResourceWarning se kontrolují samostatně.

E5A neprovádí dávky, produkční DB reconciliation, recovery/rollback/resume,
NAS operace ani purge/delete actions. Standardní interní SQLite transaction
journal nenahrazuje E1 evidence. **NAS execution není autorizovaná.**

## E5B: Read-only FS/DB diagnostika po přerušení

**HOTOVO; nezávislé review dopadlo PASS.**

[API](../app/target_execution_diagnostics.py)
`inspect_scratch_video_move(manifest, action, action_index=0, scratch_root=...,
journal_path=..., evidence_path=...)` diagnostikuje jednu sealed immutable Video
MOVE action existujícího E5A scratch kontextu. Nepřijímá externí DB cestu:
jediná povolená SQLite je `<scratch-root>.sqlite` vedle rootu podle E5A.
FS a journal inspekce používají nezměněné E4A API a původní E1/E3A validátory.
Nevytváří se execution session, writer, nový manifest ani journal format.

Před SQLite připojením se ověřují lexical scratch boundary, přesný manifest,
action/index a patches, nofollow private root/parent/regular single-link files,
mount binding, stabilita stat/hash a původní SQLite header s rollback format
versions 1/1. Všechny `-journal`, `-wal`, `-shm` se zkoumají bez SQLite;
přítomný, prázdný, unsafe nebo nejistý sidecar znamená `SQLITE_SIDECAR`,
`DB_UNINSPECTED` / `UNINSPECTED`, žádné SQLite připojení k originálu. Report
zachová dostupná metadata a bezpečně čitelné hashe artefaktů. Existence sidecaru
**nedokazuje hot journal ani necommitnutou transakci**. Soubory se nemažou,
neopravují a neotevírají kvůli recovery, ani na kopii.

Pouze čistá stabilní SQLite se čte přes `mode=ro`, s `query_only`, běžným
SQLite locking a explicitní read transaction. `immutable` ani `nolock` se
nepoužívají. Ověřuje se DELETE mode, strukturální čitelnost, správné `Video.id`,
`relative_path`, `root_folder`, size/mtime preconditions a případné jiné Video
na source/target locatoru. Před a po čtení se kontrolují držené descriptors,
file/parent/root/mount bindings, hashe, nepřítomnost sidecarů a stabilita E1
journalu i identity evidence. Opakovaná E4A inspekce musí souhlasit s prvním
pozorováním. Změna či nejistota znamená STOP, nikoli nový pokus o opravu.
Stále platí exclusive scratch ownership z E4A/E5A; nejde o maintenance boundary
proti cizím same-UID či privilegovaným zásahům.

Limit WAL/SHM: E5B před SQLite připojením odmítá existující `-journal`, `-wal`
i `-shm` a předpokládá exclusive scratch ownership. Pokud jiný proces tento
předpoklad poruší a vytvoří `-wal` mezi kontrolou a otevřením, může i SQLite
`mode=ro` vytvořit nebo změnit `-shm`. Následná kontrola změnu detekuje a vrátí
STOP, ale již provedenému zápisu zpětně nezabrání. Limit nebrání dokončení
scratch-only E5B, nenahrazuje však budoucí produkční maintenance boundary
a writer exclusion.

Frozen report obsahuje manifest/action ID a index, E4A report včetně poslední
validní zaznamenané fáze, SQLite status, aktuální/očekávané Video locators,
schválené patches, size/mtime, dostupné SQLite artefakty, konkrétní rozpory
a STOP reasons. `locator_state` rozlišuje:

| Stav | Význam |
| --- | --- |
| `OLD_VALUES` | Locators odpovídají schváleným before hodnotám a preconditions |
| `NEW_VALUES` | Locators odpovídají schváleným after hodnotám a preconditions |
| `MISMATCH` | Jiný locator, size/mtime, chybějící/nesprávné Video.id či kolize |
| `UNINSPECTED` | Originál nebylo bezpečné otevřít přes SQLite |
| `UNKNOWN` | Čtení selhalo nebo nelze doložit stabilní výsledek |

TARGET_ONLY + OLD_VALUES znamená pozorovaný fyzický progress při původních DB
locators; SOURCE_ONLY + NEW_VALUES opačný rozpor. TARGET_ONLY + NEW_VALUES
popisuje souhlas pozorovaných cest, s inode continuity stále UNKNOWN.
BOTH_PRESENT/BOTH_ABSENT nevybírají správný soubor. Journal phase nikdy
nedokazuje aktuální FS/DB; parseable frame neprokazuje fsync acknowledgement,
hash neprokazuje inode continuity, FS_VERIFIED není DB_RECONCILED a DB_RECONCILED
není automaticky COMPLETE. Shoda dvou locator polí ani porovnání s původním DB
fingerprintem neprokazují plný DB post-state; `full_post_state` a
`transaction_state` zůstávají UNKNOWN.

`outcome=STOP` a `manual_review_required=True` platí i po zaznamenaném COMPLETE.
API nezapisuje DB/journal/evidence, nemění locators, nepřejmenovává soubory,
neprovádí resume/rollback/recovery ani neuděluje execution authority.
Nové procesní testy používají existující E5A kill/reap/snapshot harness, reálný
SIGKILL včetně otevřené SQLite transakce se zachovaným rollback journalem a nový
inspekční proces s povinným terminal assertion markerem. Předčasný exit 0 není
PASS. Kontrolují bytes/hash/metadata před/po, nulové SQLite připojení při
sidecarech, skutečné SQLite zamykání, změny během inspekce a FD/ResourceWarning.
Produkční DB/NAS, RPi5 ani produkční startup/scanner/executor se nepoužívají.

## E6A: Dvě navazující scratch Video MOVE akce

**HOTOVO; nezávislé review dopadlo PASS. NOT AUTHORIZED FOR NAS EXECUTION.**

Privátní [owner](../app/target_execution_pair.py)
`_ScratchVideoMovePair.create(parent)` vytvoří jediný čerstvý privátní scratch
root pod `/tmp`. Caller připraví pouze syntetické soubory, sousední testovací
SQLite a sealed immutable manifest; jediné `execute(manifest)` smí provést
přesně dvě PRIMARY Video MOVE akce. A má index 0 a žádné dependencies, B index 1
a právě dependency na A. Dvě různá Video.id a čtyři odlišné source/target
locators jsou povinné; pořadí se nepřeskupuje. Další akce, LOW, QUARANTINE,
neúplné/cizí patches nebo neplatný binding znamenají STOP před execution.

### Výhradní owner a autorizace B

Owner drží jeden nový E1 writer od prvního PENDING až po finální COMPLETE,
jednu konkrétní SQLite, původní filesystem context a obě živé observations.
Každá akce má vlastní E3A context, pevný action/index binding, vlastní capture
a sousední `<root>.identity-0.json` / `<root>.identity-1.json`. Evidence vzniká
přes O_EXCL, nofollow, file/parent fsync a původní plné content/inode kontroly;
žádná evidence se nepřepisuje. Identity contexts sdílený root pouze vypůjčují;
jediným vlastníkem filesystem lifecycle zůstává pair owner. Žádná jednorázová
session se neresetuje a žádný existující root/writer se nepřebírá.

Veřejné E2A/E3A dependency guards zůstávají zachované. Pouze privátní
filesystem context pair ownera rozumí jeho interní autorizaci B, vázané na
přesný manifest, aktivní jednorázové volání a původní proces/thread. Autorizaci
owner vydá až po novém `inspect_live` nad pěti acknowledged stavy A, nezměněném
E1 dependency validatoru, nezávislém ověření uloženého R1 a živém FS/identity
ověření A. Před PENDING B se tyto kontroly opakují. In-memory flag ani parseable
COMPLETE po ztrátě append acknowledgement nestačí.
Context exit i close čekají na stejný owner mutex jako execution; neuzavřou
živé SQLite prostředky z jiného threadu. Zděděný owner v jiném procesu odmítá
execution i SQLite cleanup, který by mohl zasáhnout transakci původního procesu.

E1 formát ani pravidla lifecycle se nemění. Jediná historie je:

```text
A: PENDING → FS_APPLIED → FS_VERIFIED → DB_RECONCILED → COMPLETE
B: PENDING → FS_APPLIED → FS_VERIFIED → DB_RECONCILED → COMPLETE
```

### Dvě transakce, tři ověřené stavy

Privátní DB lane sdílí původní bezpečné otevření, nofollow pins, private regular
file, mount/header/sidecar checks, DELETE journal mode a synchronous FULL.
Přijímá pouze odvozenou `<root>.sqlite`; nezakládá schema ani databázi. R0 musí
odpovídat původnímu sealed fingerprintu a úplnému známému aplikačnímu schema.
Patches všech ostatních objektů i fields jsou odmítnuté.

Z celého ověřeného R0 a přesných sealed patches se před rename připraví R1
(změněné locators A, původní B) a R2 (změněné locators A i B). Každý stav
obsahuje všechny aplikační rows a celé schema/user_version. Jednotlivá
podmíněná UPDATE mění jen `Video.relative_path` a `Video.root_folder` a kontrolují
rowcount, celkový počet změn, všechny rows a schema, včetně trigger side effects.
Filename, metadata, hierarchy, numbering a manual authority zůstávají nedotčené.

Každá akce má vlastní commit. Po něm se znovu získá BEGIN IMMEDIATE rezervace
a nezávislá read-only connection ověří uložené rows/schema proti původní
projekci. Nově pozorovaný fingerprint pouze hlídá stabilitu téhož souboru;
nenahrazuje schválený očekávaný obsah. Data-version kontrola odhalí cizí commit
v reservation gap. File/parent/mount pins zůstávají držené přes obě transakce.
Finální `expected_post_hash` přes běžný planner loader a scratch inventory je
podmínkou až pro R2. R1 se neporovnává s finálním stavem, ale nezávisle musí
přesně odpovídat projekci A bez změny B.

Před DB_RECONCILED a COMPLETE se kontroluje uložený DB stav, živý journal,
obě již přesunuté fyzické identity i původní evidence anchors. A zůstává pod
živou kontrolou během celé B. Platí původní exclusive scratch ownership;
nejde o produkční maintenance boundary proti cizím same-UID/privilegovaným
změnám ani o důkaz NAS power-loss durability.

### STOP a zachování částečného stavu

Při selhání A se B vůbec nezačne. Po acknowledged COMPLETE A se její přesun,
DB commit ani historie při selhání B nevrací. Zachová se skutečný FS/DB stav,
obě vzniklé identity evidence a všechny journal bytes; STOP uvádí action index,
fázi a příčinu. Necommitnutá SQLite transakce se při close zruší, potvrzený nebo
nejistý commit ani fyzický rename se neopakuje a automaticky nevrací.
Nejasný fsync, rename, commit či journal append neposkytuje continuation
authority ani tehdy, pokud zůstaly úplné parseable bytes. Owner se spotřebuje
po úspěchu i chybě a zavře všechny držené prostředky bez opakování nejistého
close. Artefakty se nemažou ani neopravují.

Původní `ScratchActionSession.execute()` a `execute_video_move()` zůstávají
jednoakční. E4A/E5B zůstávají read-only a E6A nedeklaruje jejich podporu pro
dvouakční recovery. Žádný N-action executor, reopen/adoption, restart/resume,
automatic recovery/rollback, scanner/startup ani produkční executor nevzniká.
**Produkční DB, NAS a RPi5 jsou mimo rozsah; NAS execution je NEAUTORIZOVANÁ.**

## E6B: Lokální spolupracující maintenance boundary

**HOTOVO; nezávislé review dopadlo PASS. NOT AUTHORIZED FOR NAS EXECUTION.**

[ScratchMaintenance](../app/maintenance.py) je explicitně zapínaná hranice pro
spolupracující procesy na jednom Linux hostiteli. Přijímá pouze absolutní,
nofollow ověřené SQLite/root cesty v privátním lokálním scratch stromu pod
`/tmp`; root musí ležet pod rodičem SQLite. Používá existující scratch mount
allowlist, nikoli NAS capability probe. Běžná konfigurace ani environment
maintenance nezapínají. Nevzniká CLI pro produkční aktivaci ani executor.

Jediný namespace je `<database>.maintenance.lock` vedle konkrétní SQLite.
Statický binding soubor obsahuje pouze verzi a přesnou SQLite/root dvojici,
nikoli PID, aktivní maintenance stav nebo safe/recovery marker. Soubor se při
release nemaže ani nepřejmenovává. Všichni účastníci musí použít totožný binding;
konfliktní root, symlink/hardlink a pozorovaná výměna namespace znamenají STOP.
Samotný lock nebrání úmyslné výměně souboru jiným neparticipujícím procesem.

Normální writer drží shared `flock`, maintenance owner exclusive `flock`.
Admission je neblokující: aktivní writer odmítne maintenance; aktivní
maintenance odmítne nové writery i druhého ownera. Žádný automatický retry.
SQLAlchemy admission začíná před DB BEGIN a končí až pool checkin po skutečném
DBAPI commit/rollback nebo ukončení spojení. Core spojení držené přes více
transakcí konzervativně drží lease až do close. Atomicita a SAVEPOINT chování
se nemění. Webový mutující request, scanner a veřejné migrace navíc drží
workflow lease včetně všech commit/rollback a fyzických side effectů.

### Inventura writerů a skutečné pokrytí

Následující ochrana platí **pouze při explicitním předání stejného scratch
bindingu** do `make_engine(..., maintenance=gate)` / `create_app(...,
maintenance=gate)`; výchozí produkční procesy tím chráněné nejsou.
R3 níže navíc přidává objevování známé scratch execution evidence do ordinary
scanneru a aplikačního startupu; nepřevádí všechny DB služby na production výluku.

| Vstupní cesta | Pokrytí při opt-in |
| --- | --- |
| Web POST/PUT/PATCH/DELETE, včetně metadata/link/candidate, hierarchy, numbering, naming/layout, video/subtitle locators a ručních editací | Shared admission před vstupem do handleru až po dokončení requestu; odmítnutí 503. Žádný refaktor jednotlivých handlerů. |
| `make_session_factory`, přímý `Session(engine)` a SQLAlchemy Core na opt-in enginu, včetně odvozených OptionEngine | Lease přes skutečnou DB transakci; chrání i přímé ORM/service volání. |
| `scan_library` | Workflow lease před inventory/probe, kontrola stejného rootu, přes commit nebo rollback. Scanner fyzickou knihovnu čte, locators a evidence mění v SQLite. |
| `create_app` / lifespan | Admission pro vytvoření artwork adresáře; samostatný lease přes migraci a `create_all`, aktivní maintenance startup odmítne. |
| `migrate_schema`, `migrate_schema_at_startup`, `migrate_title_locator_persistence` | Celý veřejný workflow včetně přímého DBAPI/table rebuild a verze schema. |
| `cache_cover` | Admission před download/mkdir/replace/unlink artworku, včetně přímého volání s read-only session; DB lease pokračuje do caller commit/rollback. |
| Běžné GET/HEAD/OPTIONS | Session factory používá `query_only` bez write lease, odmítá DML a vypíná autoflush; stabilní katalog/review lze číst při maintenance. Fyzický rename by vyžadoval samostatné read-consistency řešení. |
| Read-only planner/audit CLI | Existující SQLite `mode=ro`, žádná writer admission ani execution authority. |
| `app.tools.rebuild_hierarchy --apply`, vlastní skripty bez opt-in, jiné enginy/SQLite klienti, raw DBAPI mimo chráněné migrační entry points | **Nepokryté. Produkční blokující podmínka:** zastavit nebo doložit jejich účast před budoucí execution. |
| E1–E6A scratch execution/journal/identity a lokální manifest/evidence output | Původní samostatné kontrakty beze změny; E6B je neaktivuje ani na ně nenapojuje executor. |

### Owner, ukončení a pád

Scratch použití: `gate = ScratchMaintenance(database_path, library_root)`;
`with gate.maintenance() as owner:` drží exclusive výluku. Běžný zápis tohoto
procesu ji rovněž neobchází. Pouze explicitní `with owner.write_scope():` dovolí
DB operaci přes daný opt-in engine na owner threadu bez získání druhého flocku.
Borrowed admission drží původní exclusive fd: pokus ukončit ownera s aktivní DB
transakcí znamená STOP a lock zůstává držený do ukončení této transakce.

Lease/capability je vázaná na původní PID; DB write/commit/rollback a session
cleanup v potomkovi jsou odmítnuté. At-fork child zavře zděděné lock fd bez
`LOCK_UN`, takže neuvolní rodiče ani po jeho zániku nezachová falešný lock.
Nový proces vytváří vlastní binding/engine, nepřebírá živou session. Release
zavírá fd právě jednou; chyby propagují STOP, žádný nejistý close se neopakuje.
SIGKILL/exit uvolní kernelový flock, statický binding soubor zůstává.
**Volný lock není důkaz bezpečného FS/DB stavu, dokončení operace ani oprávnění
k automatickému recovery/resume/rollback.** E6B takový mechanismus nemá.

Advisory flock nebrání neparticipujícím programům, jinému NAS klientovi,
privilegovaným zásahům, změně mountu ani ztrátě napájení. Zastavení či zapojení
všech skutečných produkčních writerů, externí NAS/mount/capability kontroly,
snapshot/backup a post-state verification zůstávají samostatnými nesplněnými
produkčními readiness gates. Výsledek E6B neposkytuje production execution
authority ani nedokládá hardware durability.

## Ručně schválený rollback jedné scratch Video MOVE

**HOTOVO; nezávislé review dopadlo PASS. NOT AUTHORIZED FOR NAS EXECUTION.**
[Samostatné API](../app/target_execution_rollback.py) dovoluje jediný návrat
`target → source` v původním syntetickém E5A kontextu pod `/tmp`. Neotevírá
původní execution session ani E1 journal pro zápis a nemění SQLite. Podporuje
jen jeden sealed Video MOVE na indexu 0, chybějící source, očekávaný target,
platnou E3 evidence a neukončenou historii PENDING/FS_APPLIED/FS_VERIFIED.
DB musí mít OLD_VALUES a přesný původní sealed fingerprint; žádný SQLite
sidecar není přípustný. Jiné či nejasné stavy znamenají STOP.

`propose_scratch_video_rollback(...)` je pouze read-only návrh. Obsahuje hash
celého sealed manifestu, action ID/index, oba směry cest, úplnou E5B diagnózu
včetně DB fingerprintu a současných FS/artefaktových metadata/content důkazů.
`identity_continuity` zůstává **UNKNOWN**: shoda SHA-256 ani číselného inode po
restartu neprokazuje historickou fyzickou identitu. Návrh tento limit výslovně
zobrazuje v `identity_limitation`.

Volající musí odděleně předat `RollbackApproval(proposal,
'RETURN_TARGET_TO_SOURCE', IDENTITY_LIMITATION, approved_by)` po ručním
schválení konkrétního návrhu. Diagnostika schválení nikdy nevytváří. Chybějící
schválení, jiný návrh nebo nepotvrzený limit identity znamenají STOP.
`rollback_scratch_video_move(...)` navíc vyžaduje živého výhradního ownera
z `ScratchMaintenance(original_sqlite, original_root).maintenance()`; lease
zůstává v majetku volajícího. Shared, cizí, uzavřený či zděděný owner neplatí.
Jde o E6B spolupracující výluku na jednom hostiteli, nikoli globální ochranu.

Před zápisem se návrh znovu vypočte a musí přesně odpovídat schválení. Po celý
pokus jsou drženy read-only fd a sdílené artefaktové locks původního journalu,
identity a DB a nově zachycený živý target inode pin. SQLite read transaction
(`mode=ro`, `query_only`) trvá přes APPROVED, ATTEMPT, rename, durability
a ověření a končí před zápisem VERIFIED; následné kontroly hashů DB, journalu
a identity evidence i nepřítomnosti sidecarů zůstávají až do konce pokusu.
Sdílené E2A/E3/E4/E5 kontroly znovu ověřují root,
mount, parent bindings, kolize, hash, metadata, DB i sidecary. Fyzický návrat
použije existující `renameat2(RENAME_NOREPLACE)` bez copy/delete fallbacku.
Následuje nezávislé ověření a E2A durability pořadí: file fsync, cílový parent
fsync, původní parent fsync, další ověření. Obsah videa se nezapisuje.

Samostatný `<root>.rollback.jsonl` vedle původních artefaktů vzniká pouze
`O_EXCL`. Obsahuje kontrolované checksumem propojené záznamy APPROVED, ATTEMPT
a při potvrzeném výsledku VERIFIED. APPROVED i jeho directory entry jsou
fsyncnuté před durable ATTEMPT; ATTEMPT předchází jedinému rename syscallu.
APPROVED obsahuje celé schválení; ATTEMPT výslovně označuje výsledek jako
UNKNOWN_UNTIL_VERIFIED bez oprávnění k automatickému retry. Toto API soubor
nikdy znovu neotevírá pro append ani nemaže; i prázdný či částečný soubor
zablokuje další pokus. E1 formát ani původní historie se nemění, žádné zpětné
COMPLETE nevzniká.

Přerušení, nejasný rename/fsync nebo selhání ověření ponechá skutečné soubory
a dostupné důkazy. Bez potvrzené finální evidence se nevrací úspěch. Platný
prefix ani uvolnění maintenance po SIGKILL neprokazují dokončení nebo bezpečný
FS/DB stav a neautorizují resume; vyžadují novou ruční diagnostiku. Ani
parseovatelný finální frame sám neprokazuje, že původní proces obdržel fsync
acknowledgement. Neexistuje API pro pokračování v přerušeném rollbacku.

Rozsah nezahrnuje dvě akce E6A, jiné operace, celý plán, DB reconciliation,
produkční maintenance/executor ani NAS/RPi5. Obecný ani produkční rollback
a automatický rollback/recovery nadále neexistují. Externí readiness gates zůstávají
nesplněné. **NOT AUTHORIZED FOR NAS EXECUTION.**

## E7A: sekvenční execution jádro

**HOTOVO; nezávislé review dopadlo PASS. NOT AUTHORIZED FOR NAS EXECUTION.**
[Sekvenční owner](../app/target_execution_sequence.py) sdílí lifecycle akce
s původním E6A adaptérem; SQLite lane sdílí prefixové transakční operace.
`ScratchManifestExecution.create(private_tmp_parent)` vytváří pouze nový
E2A root. `execute(original_manifest, maintenance_owner=exclusive_lease)`
explicitně přijímá celý schválený sealed manifest a živého výhradního E6B
ownera pro přesný root a jeho původní sousední testovací SQLite. Lease vlastní
volající a musí ji držet až do návratu/STOP a cleanupu. API nemá produkční
přepínač, execution CLI, otevření existující session ani recovery oprávnění.

Podporované jsou PRIMARY Video MOVE a PRIMARY KEEP nad regular scratch
soubory. KEEP je rovněž svázaný s přesným záznamem celého fresh plánu;
nenahrazuje se pouhým testem existence. Má vlastní E3 durable identity evidence,
živý inode pin, ověření původního obsahu/metadata i DB beze změny. KEEP používá
také shodu ctime, protože na rozdíl od MOVE neprovádí rename. Zápis s obnoveným
obsahem a mtime je proto stále STOP. KEEP má
stejný E1 lifecycle; FS_APPLIED u něj potvrzuje ověřený no-op, nikoli rename.
Jeho commit boundary neprovádí DML a nezvyšuje počet DB změn.

Před první execution evidence/journal/DML se validuje celý původní manifest,
jeho úplnost proti fresh R0 plánu, přesné locator patches, criticality,
všechny source/target preconditions, lokální scratch binding a maintenance.
Všechny původní zdroje dostanou read-only pins bez oprávnění k rename.
Nepodporované operace/criticality, chybějící akce, cizí patches, kolize,
obsazené source chains a nevyřešené záznamy odmítnou celý plán před execution
zápisem. Sealed manifest se nepřepisuje ani nerozděluje na nové submanifesty.

E1 zachovává immutable manifest order. Pořadí proto musí být již topologické:
každá unikátní dependency směřuje k dřívější akci. Neznámé, duplicitní,
vlastní a dopředné/cyklické závislosti znamenají STOP před zápisem. Engine
nepřečísluje akce ani nemění E1 indexy. Před novým oprávněním se znovu ověří
durable COMPLETE prefix v jediném původním E1 writeru, Rn a fyzické důkazy
dokončených akcí. Samotný in-memory příznak nevytváří execution authority.

Každý MOVE používá existující E2A `RENAME_NOREPLACE` a durability pravidla,
samostatnou indexovanou E3 evidence a lifecycle
PENDING → FS_APPLIED → FS_VERIFIED → DB_RECONCILED → COMPLETE.
Veřejné E2A/E3 jednoakční API ani přísný E6A dvouakční kontrakt se nerozšiřují;
N indexů a KEEP observation jsou dostupné jen internímu živému ownerovi.

Rn je úplná row/schema projekce původního R0 a přesně schváleného prefixu
Video patches. KEEP opakuje stejný stav; žádný mezistav se znovu nebaselineuje.
Každý commit se nezávisle načte přes read-only SQLite spojení, porovná se celý
obsah známých tabulek a schema, data_version, fingerprint a držené path/mount
bindings. Finální sealed expected_post_hash se kontroluje až po poslední akci.

Selhání prostřední akce zachová předchozí COMPLETE, skutečný FS/DB stav a
všechny vytvořené důkazy; další akce nezačne. Necommitnutá transakce se při
close zruší, potvrzené commity se nevracejí. Nejasný rename, commit, append,
fsync, ztráta maintenance nebo cleanup znamená STOP bez retry/resume/rollback.
PID/thread a fd ownership zůstávají omezené na původního ownera. Jednoakční
ruční rollback neumí převzít N-akční manifest ani indexované důkazy.

Společné jádro je určeno k dalšímu využití budoucím executorem, ale jeho
scratch adaptér nadále vyžaduje nově vytvořený izolovaný root, lokální mount
allowlist a stávající malé scratch limity. Produkční capability, admission,
externí writery, NAS/mount/backup/readiness a power-loss důkazy se tím neřeší
ani neoznačují za splněné. Produkční executor a automatické recovery nevznikají.

## E7D: úplná locator-only DB projekce

**HOTOVO, nezávislé review PASS. NOT AUTHORIZED FOR NAS EXECUTION.**
Existující E7A SQLite větev používá sealed patches pro `video` → `videos`
(`relative_path`, `root_folder`), `subtitle` → `external_subtitles` a
`unmatched_subtitle` → `unresolved_external_subtitles` (`relative_path`),
`collection` → `catalog_collections` a `title` → `catalog_titles`
(`relative_root_path`). Action kinds `confirmed_no_match` a `unresolved_subtitle`
se mapují na patch kind `unmatched_subtitle`. Filesystem-only akce nevytvářejí DB
rows. Filename evidence, timestamps a všechna ostatní doménová pole se zachovávají.

Každé Rn vzniká z ověřeného R0 a přesného prefixu schválených patches; ID, pole
a before musí odpovídat R0. Změny polí jednoho řádku tvoří jeden parametrizovaný
UPDATE s before predicates. KEEP nemá vlastní file UPDATE. Všechny collection/title
patches se aplikují atomicky s DB krokem poslední akce, i když jde o KEEP:
existující coordinator před ním znovu ověřuje všechny fyzické operace. Při
necommitnutém selhání se vrátí celá poslední transakce včetně file i container
patches; předchozí potvrzené prefixy zůstávají. Nejasný commit znamená STOP bez
retry. Prázdný manifest nemá tuto transakční hranici a zůstává nepodporovaný.

Celé rows/schema, user_version, data_version, total_changes, fingerprint a
nezávislé read-only ověření commitů zůstávají povinné. Finální expected_post_hash
se kontroluje až po posledním commitu se všemi container patches. E6A a jeho
dvouakční DB větev se nemění; E7A physical admission nadále přijímá pouze PRIMARY
Video MOVE a PRIMARY KEEP. Subtitle MOVE a QUARANTINE DB projekce je ověřena
izolovaně, bez rozšíření fyzické execution authority.

Původní v1 neukládal Windows root, přestože ovlivňoval planner a post-state hash.
Tuto mezeru uzavírá [E7E](#e7e-verzovaný-windows-root-binding); E7D locator projekce
a její transakční hranice zůstávají beze změny.

## E7E: verzovaný Windows root binding

**HOTOVO, nezávislé review PASS. NOT AUTHORIZED FOR NAS EXECUTION.**
Nová generation vytváří pouze sealed manifest **v2**. Povinné payload pole
`windows_root` je string nebo explicitní JSON `null`; podléhá immutable boundary
a canonical SHA-256 stejně jako ostatní payload. `C:\Anime` a `D:\Anime` tedy
mají rozdílné `manifest_id`, i když mají stejné targets, budget result a post-state
hash. Čas vytvoření a warning acknowledgements zůstávají mimo payload hash.

Lexikální validace bez Windows runtime rozlišuje drive absolute root (`C:\Anime`,
případně `C:\`) a úplný UNC root (`\\server\share`, případně s podadresářem).
Odmítá relativní/drive-relative cesty, UNC bez serveru/share, device namespaces,
dot segments, prázdné vnitřní komponenty, nepovolené znaky, trailing dot/space
a reserved device names. Forward slash se při validaci připouští; přesný původní
zápis včetně case a separators je součástí sealed identity. Při runtime porovnání
se aliasy ani jiné zápisy automaticky nenormalizují.

Generation používá stejný root pro planner, post-state simulaci i expected hash.
Read-only preflight, E7A fresh R0 parity a finální post-state verifier jej berou
ze sealed payloadu. API argument `windows_root`, pokud je explicitně předaný,
musí přesně odpovídat payloadu, včetně `None`; nesoulad je `NOT_READY` v preflight
nebo STOP/exception při final verification. Vynechaný API argument používá sealed
kontext. `RuntimeEvidence.actual_windows_root` musí pro readiness doložit přesně
tuto hodnotu. Mismatch má diagnostiku `windows_root_mismatch`, bez resealingu
nebo přepočtu schváleného kontextu. Windows 240 UTF-16 budget se stále kontroluje.

`null` výslovně značí nesvázaný kontext: izolovaný scratch zůstává podporovaný,
ale preflight vrací `sealed_windows_root_not_bound` a nikdy produkční readiness.
Dodatečný CLI argument ani runtime evidence nemohou chybějící sealed binding doplnit.
Windows client budget je oddělený od skutečného Linux `library_root` a od
`mount_identity`, aktuálního RO stavu i write/no-replace capabilities.

Strict verzovaný dekodér nadále načítá **v1** v přesném historickém tvaru;
canonical JSON, původní hash i journal binding zůstávají zachované. Do jeho
reprezentace se nepřidává ani `windows_root: null`. V1 se automaticky neupgraduje
ani nepřesealuje. Preflight vrací `NOT_READY` s
`legacy_windows_binding_unavailable`; nová E7A execution a finální execution
hash binding odmítnou. E5A a E6A odmítají v1 již při admission, po kontrole
root/mount bindingu bez I/O na cizí root a před DB, evidence, journalem či rename.
Pro nové schválení je nutná nová v2 generation a review.

E7E nemění DB ani E1 journal schema, R0→Rn patches, nezávislé DB kontroly,
finální hash až po posledním commitu, scratch action admission ani dependency
gate. Nepřidává execution lifecycle ani produkční executor. Nexus (Raspberry Pi 5,
Debian 13 ARM64, plánovaný library root `/mnt/nas-anime` a SQLite na lokálním SSD)
je připravené cílové prostředí, nikoli automaticky autorizovaný executor;
aktuální read-only CIFS mount nesplňuje write capability gate.

## E7F: bezpečná příprava cílových adresářů

**HOTOVO, nezávislé review PASS. NOT AUTHORIZED FOR NAS EXECUTION.**
Existující scratch-only E7A owner připravuje chybějící parents výhradně z
původního `ManifestPayload.directories`, v jeho sealed parent-before-child
pořadí. Nevzniká další executor ani veřejná mkdir/recovery authority. Admission
v rozsahu E7F zůstává PRIMARY Video MOVE a PRIMARY KEEP (rozšíření viz E7G);
E5A/E6A a veřejná E2A/E3A API
přípravu adresářů nezískávají. Manifest v2, Windows binding, E1 journal formát
a E7D locator-only projekce zůstávají beze změny.

Nejprve musí projít původní kompletní E7A R0 parity: sealed manifest, fresh
planner a filesystem hash, původní DB fingerprint/rows/schema, přesné patches,
všechny akce, criticality, dependencies a kolize. Všechny původní zdroje dostanou
živé read-only pins a plný content hash. Existující adresáře mají držené dirfd;
jejich numerická identita, nofollow path binding, vlastnictví/permissions a
root/mount podmínky se znovu ověřují. Symlink, soubor místo adresáře, cizí root,
mount crossing či neplánovaná cesta znamenají STOP. Existující adresáře se
nepřejmenovávají ani neupravují přes chmod/chown.

Příprava zachovává přesný přijatý R0 filesystem snapshot. Pro každý vlastní
úspěšný `mkdir(..., 0700, dir_fd=held_parent)` eviduje pouze novou directory
entry a změnu timestampu jejího přímého parentu, zachycenou před fsync bariérami.
Root timestamp se během přípravy kontroluje samostatně. Ostatní entries,
directory timestamps a file stats musí zůstat shodné; source pins/content a
původní DB R0 se opakovaně ověřují před i po každém mkdir. Child i parent
vyžadují fsync a nezávislé znovuotevření cesty musí odpovídat drženému dirfd.
Vlastní přírůstek tak nezneplatňuje R0 ani neumožňuje přijmout obecný nový
baseline. Původní manifest, `plan_hash`, `filesystem_hash` a
`expected_post_hash` se nikdy nepřepisují, nenormalizují ani nepřesealují.
Po přípravě se directory bindings kontrolují i během původního action lifecycle;
finální hash nad skutečným mkdir + MOVE stavem zůstává původním E7A/E7D gate.

Lokální sousední `<scratch-root>.directories.jsonl` vzniká přes exclusive
nofollow create mimo scratch root, v jeho původním privátním lokálním parentu.
Jde o oddělenou minimální evidenci s canonical SHA-256 chain, nikoli nový E1
lifecycle. `BOUND` váže celý původní manifest, filesystem hash, directory set,
root dev/inode, mount a existující directory identities. `INTENT` pro konkrétní
schválenou cestu a parent identity musí mít potvrzený file i evidence-parent
fsync před mkdir. `VERIFIED` se zapisuje až po child/parent fsync, nezávislém
ověření directory bindingu a přesném ověření přírůstku vůči původnímu R0.
Teprve po všech adresářích a dalších kontrolách se zapíše a potvrdí `READY`.
Živý owner ověřuje i nezměněné bytes a binding evidence souboru.

Samotný `INTENT` bez navazujícího `VERIFIED` znamená nejasný výsledek: adresář
nemusel vzniknout, mohl vzniknout bez potvrzené durability nebo proces zanikl
před zaznamenáním ověření. Zachovaný `VERIFIED` popisuje ověřený jednotlivý
adresář, nikoli dokončenou přípravu. Po přerušení ani celý poslední frame či
`READY` neprokazují původní fsync acknowledgement, současný stav nebo kontinuitu
živých pins; prefix slouží jen ručnímu vyhodnocení a nikdy neopravňuje k resume.
Zápis, fsync, mkdir, změna identity/snapshotu/zdroje/DB, ztráta maintenance nebo
jakkoli nejasný výsledek znamenají STOP. Vytvořené adresáře a skutečné důkazy
zůstanou zachované. Neúplná příprava nezahájí žádnou E1 action, MOVE ani DB
UPDATE/commit a nevrací COMPLETE. Retry, rollback, resume, mazání starých
adresářů, LOW continuation a QUARANTINE execution se nepřidávají.

Platí dosavadní výhradní scratch ownership a omezení E2A/E6B: privilegovaní či
nespolupracující same-UID writeři nejsou globálně vyloučení. Scratch testy včetně
SIGKILL nejsou důkazem NAS/power-loss durability ani produkční readiness.

## E7G: úplné scratch fyzické file actions

**HOTOVO, nezávislé review PASS. NOT AUTHORIZED FOR NAS EXECUTION.**
Existující `ScratchManifestExecution` přijímá úplný fresh sealed manifest
následujících kombinací skutečně generovaných Target Plannerem:

| object_kind | action | DB file locator |
| --- | --- | --- |
| `video` | MOVE, KEEP, QUARANTINE | Video relative_path/root_folder |
| `subtitle` | MOVE, KEEP, QUARANTINE | ExternalSubtitle relative_path |
| `confirmed_no_match` | MOVE, KEEP | UnresolvedExternalSubtitle relative_path |
| `subtitle_archive` | MOVE, KEEP | žádný |
| `auxiliary` | MOVE, KEEP | žádný |
| `duplicate_side_asset` | QUARANTINE, KEEP | žádný |
| `keep_root` | KEEP | žádný |

Aktuální generátor ponechává `unresolved_subtitle` bez rozhodnutí v REVIEW,
stejně jako unknown assets a neprokázané duplicate candidates. E7G jim execution
autoritu nedává. Platné potvrzené duplicity mohou být přesunuté do quarantine
i při INCOMPLETE/UNKNOWN side accounting; nejde o oprávnění k purge.
QUARANTINE používá stejný `RENAME_NOREPLACE`, fsync, content/identity evidence
a E1 lifecycle jako MOVE. KEEP ověřuje původní soubor bez změny file locatoru.
Žádná akce soubor nemaže.

Před první directory evidencí, mkdir či file action musí projít původní R0
fingerprint, rows/schema, filesystem/plan hash, úplnost všech původních akcí a
patchů a přesná criticality včetně důvodu a sealed lidské authority. Carried
duplicate evidence se navíc přesně porovnává s projekcí původního fresh plánu:
vazby, side assets, jejich stat/hash/provenance a COMPLETE/INCOMPLETE/UNKNOWN
se nesmějí doplnit odhadem z cílové cesty. Unsafe source/target chains včetně
QUARANTINE, nekanonické dependencies a kolize se odmítají před execution efektem.

Přesně schválené PRIMARY i LOW procházejí stejným sekvenčním lifecycle
PENDING → FS_APPLIED → FS_VERIFIED → DB_RECONCILED → COMPLETE.
LOW failure policy v manifestu a E1 dependency gate se nemění. E7G při každém
selhání zastaví celou sekvenci; nedovoluje LOW continuation ani přeskočení akce.
Nejistý rename, fsync, DB commit či journal append zachovává skutečný stav a
důkazy a vyžaduje reconciliation. Dřívější potvrzený COMPLETE prefix zůstává
zachovaný; neprovedené akce se neoznačí COMPLETE. Retry/resume/rollback se
nepřidávají.

E7F připravuje pouze původní sealed directory set. E7D nadále odvozuje každý
Rn z původního R0 a přesných sealed patches; filesystem-only akce nevytvářejí
DB rows ani další locator patches. Collection/Title patches se aplikují až
v poslední transakci. Po každém commitu probíhá nezávislé ověření a původní
`expected_post_hash` se kontroluje až na konci s carried duplicate evidence.
Manifest, jeho hashes ani Windows binding se nepřesealují. E5A/E6A a veřejná
E2A/E3A autorita, E1 formát, manifest v2, schema i E7C coverage zůstávají beze
změny. Produkční executor, recovery, purge ani cleanup adresářů nevznikají.

## E7H: schválený úklid prázdných původních adresářů

**HOTOVO, nezávislé review PASS. NOT AUTHORIZED FOR NAS EXECUTION.**
Volitelný `cleanup_decision` v existujícím `ScratchManifestExecution.execute`
je samostatný rozhodovací krok po dokončení celého E7G manifestu, ještě uvnitř
původního živého owneru a E6B exclusive maintenance. Bez callbacku zůstává
dosavadní execution beze změny. Callback může vrátit `None` (PRESERVE), nebo
výslovné `approve_cleanup(proposal, ("old/child", "old"), actor=..., reason=...)`.
Není přidané adopt/reopen API, CLI, produkční executor ani automatický cleanup.

Admission znovu ověří durable COMPLETE všech E1 akcí, původní E7D finální Rn
projekci a původní `expected_post_hash`, všechny fyzické soubory a živé bindings.
`simulate_post_state` zachovává původní directory entries; odstranění adresáře
proto stojí **až za** původním hash gate. Manifest v2, jeho seal/hashes, E1 formát
ani E7F evidence se nemění a nepřesealují. Původní `expected_post_hash` nepopisuje
nový post-cleanup stav a po `rmdir` se jako jeho hash nepoužívá.

R0 coverage vzniká ze stejného přijatého snapshotu a fresh plánu jako E7D.
`CLEANUP_REVIEW` a `UNRESOLVED_EMPTY` jsou pouze kandidáti, nikoli delete authority.
Proposal váže celý původní sealed manifest přes ID a SHA-256, konkrétní úplný
E1 journal přes path/dev/inode/bytes hash, scratch root dev/inode a mount binding,
původní filesystem hash, očekávaný původní post hash, finální DB bytes hash,
R0 directory coverage a nový ověřený snapshot včetně identities a content hashes.
Schválení váže SHA-256 tohoto návrhu, jednotlivé přesné cesty, actor a reason.
Platí jen v témže nepřerušeném owneru; cizí či změněný návrh/schválení znamená STOP.
Schválení parentu nezahrnuje jeho children: každý odstraňovaný adresář musí být
vyjmenovaný. Neschválený prázdný adresář zůstává zachovaný.

Odstranitelné jsou jen původní plně inventarizované adresáře uvedených dvou
dispozic, mimo target/KEEP parents a technické namespaces. Library root,
`Subs`, `Duplicates` a jejich podstromy, system-excluded oblasti a jejich
parents nejsou oprávněné. Živá kontrola používá držené dirfd, nofollow traversal,
numerické identity a původní root/mount binding. Unknown entries, symlinky,
mount crossing, změna identity nebo neprázdný adresář zastaví cleanup.
Použije se pouze `rmdir(leaf, dir_fd=held_parent)`, od nejhlubších cest,
bez unlink, rekurzivního mazání nebo chmod/chown. Po syscallu se zachytí pouze
zmizení dané položky a okamžitý stat delta jejího přímého parentu; následuje
parent fsync, ověření nulového link count drženého inode, nezávislé znovuotevření
parentu a ověření absence a celého snapshotu.
Všechny ostatní directory entries a všechny soubory včetně identity a obsahu
musejí zůstat shodné. DB se jen read-only ověřuje proti původnímu finálnímu Rn,
schema i DB bytes zůstávají zachované, E1 journal se jen kontroluje.

Samostatná sousední `<scratch-root>.cleanup.jsonl` vzniká přes exclusive nofollow
create a používá canonical SHA-256 chain. `BOUND` ukládá návrh a schválení;
`INTENT` s identitou adresáře a parentu musí mít potvrzený file i evidence-parent
fsync **před** `rmdir`. `VERIFIED` následuje až po parent fsync a nezávislém
ověření přesného rozdílu. `COMPLETE` ukládá vlastní výsledný filesystem snapshot,
jeho samostatný hash a vyúčtování removed/preserved adresářů; úspěšný návrat
vyžaduje i potvrzenou durability evidence a poslední kontrolu.

Jakékoli přerušení, nejistý syscall, write/fsync failure nebo ztráta maintenance
znamenají STOP se zachováním všech bytes a skutečného dosaženého filesystem stavu.
E1 COMPLETE zůstává dokončením file actions, nikoli cleanupu. Dřívější ověřené
odstranění se nevrací; další adresáře se nepokusí odstranit. Samotný poslední
frame po pádu neprokazuje fsync acknowledgement a nikdy neopravňuje k retry,
resume či rollbacku. Dál platí scratch omezení vůči nespolupracujícím same-UID
a privilegovaným writerům; testy se SIGKILL nedokládají NAS/power-loss durability.

## P2C-R2: completion rescan pouze v živém scratch owneru

Volitelné `ScratchManifestExecution.execute(..., completion_rescan=True)` žádá
read-only P2C-R1 rescan po všech původních E7G file actions, před uzavřením
původních pins/writera a při nepřerušené exclusive E6B maintenance. `True` je
požadavek na kontrolu, nikoli tvrzení, že execution skončila. Kombinace s E7H
`cleanup_decision` je odmítnutá před execution efekty; post-cleanup admission
není přidaná. R2 argument nemění ostatní execution lifecycle; R3 níže ukládá
persistentní guard i bez požadavku na completion rescan.

Interní bridge přijímá jen původního živého ownera v určené závěrečné fázi,
přesný původní manifest envelope a writer/path binding. `inspect_live` a celá
acknowledged historie musí obsahovat všechny původní action IDs/indexy a všech
pět stavů v sealed pořadí, včetně DB_RECONCILED a COMPLETE. Nestačí poslední
COMPLETE ani caller assertion. E7D nezávisle ověří skutečně commitnuté finální
Rn rows/schema, commit acknowledgement/fingerprint a původní expected post hash;
E3/E7G ověří původní capture artefakty, živé identity/content pins, MOVE i KEEP.
Původní carried duplicate evidence zůstává v sealed manifestu a post-hash gate.

Následuje nezávislá SQLite `mode=ro` Session s konzistentní read transakcí a
původní `scan_library(..., execution_manifest=...)` P2C-R1 kontrolou. Potom se
opakuje celá kontrola živého owneru, maintenance, journalu, DB a identity. Jakákoli
nejistota znamená STOP a žádný přijatý completion rescan výsledek. Výsledek
`completion_rescan_result` je diagnostika, ne znovupoužitelná authority; při
execution/close chybě se nepublikuje. Nevzniká další journal ani lifecycle.

Pokud completion kontrola selže po provedených E7 akcích, výsledek je
reconciliation-required STOP. Původní `Scratch*ReconciliationRequired` se
nepřebaluje na slabší typ: zachová instanci, fázi a původní exception chaining.
Jiná pozdní chyba se povýší na existující
`ScratchFilesystemReconciliationRequired` ve fázi `P2C_COMPLETION`, s původní
výjimkou jako příčinou. Stejná hranice platí v sequence vrstvě i pro přímou
pozdní výjimku mimo bridge. Owner uchová výsledný typ a fázi pro odmítnutí
následného pokusu. Bezpečné odmítnutí před execution efekty zůstává oddělené.
Dosažený DB/FS/journal stav se nemění, COMPLETE se nepřeznačuje a nevzniká retry,
recovery, fyzický rollback ani oprávnění k další akci.

Načtený journal po close/restartu má nadále `reconciliation_required=True`.
Restart, interrupted owner, neúplné či neacknowledged frames, nejasný DB commit
nebo ztráta maintenance nepovolují auto-admission. R2 neprovádí recovery ani
resume. R1 samotný zůstává verifier projekce, nikoli completion authority.
Odmítnutí bridge nechrání ordinary scanner spuštěný nezávisle po pádu: MOVE před
commitem DB patchů může stále vést k nové Video identitě. Durable production
maintenance/startup ochrana všech write-capable scanner cest zůstává samostatný
prerequisite. Incremental routing, web/production ingress a NAS executor nejsou
implementované; NAS execution není autorizovaná. R2 poskytuje jen diagnostiku
v původním živém scratch owneru a celé P2C zůstává otevřené.

## P2C-R3: persistentní STOP pro ordinary scanner

[Guard](../app/target_execution_guard.py) přidává pouze denial latch pro původní
`ScratchManifestExecution`, nikoli recovery framework nebo production executor.
Po ověření celého sealed R0 plánu a zdrojů, před E7F přípravou adresářů a první
file action, živý exclusive E6B owner vytvoří
`<database>.execution-guard.json`. Původní E1 pořadí a R2 bridge se nemění.

Marker obsahuje verzi/schema, přesné DB/library cesty a jejich device/inode,
manifest ID, SHA-256 celého validovaného envelope, původní DB fingerprint,
přesnou E1 journal cestu a canonical checksum. Reuse E1 nofollow parent/file
kontrol, `O_CREAT|O_EXCL`, `0600`, úplného zápisu, file `fsync` a parent directory
`fsync` poskytuje kontrolovanou durable hranici. Původní owner drží descriptors
a ověřuje stejné bytes, file/parent binding a DB/root identity při dalších
execution kontrolách. Chyba vytvoření/zápisu/fsync/check znamená STOP před mkdir
či MOVE; partial evidence se neodstraňuje ani nepřepisuje. Kolize se odmítá.

Ordinary `scan_library`, včetně `confirm_deletions=True` a `POST /scan`,
kontroluje fixed DB namespace před SQL/autoflush, inventory, probe a write
lifecycle. Původní marker bez ověřeného explicitního přechodu znamená STOP s důvodem,
že reorganizace vyžaduje kontrolu; poškozený či konfliktní marker nemůže udělit povolení. Přítomnost
libovolného JSON ani samotný název souboru nejsou autorita k pokračování.
Kontrola nepotřebuje a záměrně nepřijímá loaded COMPLETE jako autoritu.
DB cesta se kanonizuje nezávisle na `ANIME_PATH`; relativní, symlink a podporované
SQLite URI varianty najdou tentýž označený DB namespace. Samotné `st_nlink != 1`
ani neobvyklý název či nonregular DB path nejsou globální R3 zákaz: bez relevantní
evidence zůstává běžná SQLite DB, včetně hardlinků, pod původním kontraktem driveru.
Původní E7 scratch binding nadále vyžaduje jedinečnou regular DB přes E6B.
U hardlinku pod `/tmp` se ve stejném DB adresáři porovná přesný device/inode
s původním E7 DB namespace. Pouze shoda identity spolu s existující guard/E1/E7
nebo E6B evidencí odkáže alias na původní namespace a zachová STOP. Samotné jméno
kandidáta ani evidence jiné DB nesmí aktivovat scratch režim. Nejde o Video
matching ani globální mapování inode aliasů; změny DB jména/přesuny mimo původní
binding stále nejsou podporované recovery/adoption workflow.

Library root admission zpracuje pouze lexikálně (`absolute()`), odmítá parent
traversal, NUL, backslash a nejednoznačnou dvojitou root kotvu. Nevolá nad caller
rootem `resolve`, `stat`, `lstat`, mount probe ani directory walk. Nedostupný CIFS
tím nezablokuje webový proces před původním `check_library_access`; fyzický přístup
ordinary scanneru dál ověřuje samostatný proces s timeoutem. Kanonickou caller
cestu scratch bindingu je nutné dodat přímo; alias library rootu se nerozlišuje
synchronním filesystem lookupem.

Původní E7 root se odvozuje jen z kanonické DB cesty
`/tmp/.../v6-e2a-<32 lowercase hex>.sqlite`, nikdy z caller rootu. Stejný odvozený
namespace řídí známé E1/E7 journal/directory/identity artefakty i automatickou
maintenance. Zbytky evidence bez markeru jsou STOP i při chybném caller rootu.
První scanner tohoto bindingu založí E6B i před executorem; nesouhlas rootu je STOP
ještě před vstupem do scanner workflow a nemůže vytvořit cizí lock binding.
Jméno vybere kandidáta výluky, nikoli execution authority: E6B ověří přesné DB/root
cesty, file identity/type/nlink, soukromé permissions, mounty a lock.

Plain engine také objeví již existující `.maintenance.lock` u lokální DB pod
`/tmp`: omezené nofollow čtení získá pouze kandidátní původní E6B root; canonical
bytes a celý binding nezávisle ověří `ScratchMaintenance`. Ani zde se root
nebere od scanneru. Neplatná/poškozená relevantní lokální evidence je STOP.
Obdobný lock mimo `/tmp` se nepokouší aktivovat scratch maintenance; ani název
DB připomínající E7 mimo `/tmp` nezapíná scratch heuristiku. Původní explicitní
E6B opt-in enginy nadále používají svůj ověřený binding.

Kontrola markeru se opakuje **po získání shared lease**, takže executor, který
v mezeře získal exclusive lease, vytvořil durable marker, provedl MOVE a havaroval,
nemůže skeneru nechat staré povolení. Shared lease pokrývá celý ordinary scan
přes commit/rollback a návrat. Běžné legacy knihovny bez tohoto scratch
kontraktu/evidence se na E6B automaticky nepřevádějí.

Globálním fail-closed pravidlem zůstává přítomnost pevného DB guard namespace
nebo nemožnost ověřit jeho identitu/čitelnost: guard nesmí při I/O či neplatném
DB URI nepozorovaně minout případnou execution evidenci. Nejde o novou kontrolu
kvality běžných DB dat. Výjimky `OSError`/`ValueError` vzniklé až uvnitř přijatého
scanner workflow se na execution chyby nepřekládají. Žádná evidence se při čtení
nemění a SQLite se v admission neotevírá.
`create_app` kontroluje stejné podmínky před artwork mkdir a lifespan před
startup migracemi/`create_all`. Evidence bez přechodu NORMAL zastaví nový startup. Již běžící
aplikace může dál číst katalog a `/health`; R3 nezavádí webové recovery UI.

R1 `execution_manifest=` zůstává samostatná read-only větev bez ordinary fallbacku.
R3 neblokuje jiné nezávislé DB služby; ty stále vyžadují E6B opt-in pro výluku
během execution. Přímé migrační CLI ani neparticipující raw SQL nejsou globálně
chráněné markerem. Starší jedno/dvouakční scratch primitives nemění svůj kontrakt.

**Marker se nikdy automaticky neuvolňuje.** Zůstává po close, SIGKILL, restartu,
uvolnění flocku, plném COMPLETE, úspěšném R2 rescanu i E7H cleanupu. Ani ověřený
R2 výsledek nepovoluje ordinary incremental routing. Explicitní scratch přechody
jsou popsány níže; obecná ruční reconciliation zůstává otevřená. R3 neposkytuje
unlink/reset příkaz, resume, rollback, locator repair ani post-cleanup admission.

Persistentní storage/retention, production DB/library identity, globální zapojení
produkčních writerů a chybějící production executor jsou nadále otevřené.
Úmyslné odstranění všech artefaktů, neparticipující klienti, změny mount namespace
a hardware power-loss nejsou prokázané. Scratch test SIGKILL dokládá process-crash
hranici, nikoli NAS durability. **NOT AUTHORIZED FOR NAS EXECUTION.**

### Explicitní SCANNING → NORMAL

`ScratchManifestExecution.execute(..., resume_scanning=True)` nejprve po živém
R2 ověření připojí SCANNING k původnímu guardu. Tento omezený režim dovoluje jen
scanner routing přes manifestové IDs a přesné target inode vazby; chybějící či
nahrazené původní soubory odmítá a startup zápisy nepovoluje.

Po skončení `execute` lze před dalšími změnami knihovny explicitně zavolat
`app.target_execution_guard.complete_reorganization(engine, root)`. Přijímá
Engine původní scratch SQLite/root dvojice, nikoli caller Session, loaded journal
nebo boolean completion assertion. Získá výhradní stávající E6B lease a ověří
SCANNING binding, úplné E1 pořadí všech COMPLETE actions, target inode identity,
nepřítomnost původních MOVE sources a retained E7 identity záznamy. Stávající R1
verifier nezávisle porovná commitnuté DB IDs/locator patches a původní post-state
hash s aktuálním DB/FS stavem. Neúplná execution, chybějící transition, odlišná
projekce nebo poškozená evidence znamenají STOP; žádná oprava dat se neprovádí.

```python
with maintenance.maintenance() as lease:
    owner.execute(manifest, maintenance_owner=lease, resume_scanning=True)
# Původní exclusive lease už skončila; potvrzení získá vlastní E6B výluku.
complete_reorganization(engine, owner.root)
```

Do stejného guardu připojí checksumovaný NORMAL frame vázaný na celý původní
prefix a SHA-256 identity historie. File i parent fsync jsou povinné; chyba
appendu či finalizace zanechá STOP. Původní bytes, journal i identity artefakty
zůstávají zachované. Opakované potvrzení platného NORMAL je no-op. Nevzniká nový
journal, guard, recovery ani oprávnění pokračovat v přesunech.

Nový startup i ordinary `POST /scan` pak ověřují completion historii, ale živé
soubory již neporovnávají s historickým manifestovým seznamem. Scanner používá
aktuální `relative_path` a DB IDs; technické změny a nové kopie na stejné známé
cestě zachovávají řádek, source filename, membership a ruční autority. Nové
soubory i M:N titulky používají stávající workflow; odstranění má původní
mount/empty/20% confirmation ochrany. Canonical cesty nepřepisují strukturu
existujících titles. Nejednoznačný locator zůstává Review. Admission se kontroluje
znovu před commitem. Neversionovaný NORMAL startup se odmítá před legacy
rekonstrukcí hierarchy; běžné versioned compatibility migrace dál drží E6B.
Produkční NAS, post-cleanup uzavření a obecná reconciliation zůstávají mimo scope.

## Externí pre-execution gates

Read-only preflight vrací `READY_FOR_EXECUTION`, `NOT_READY`, `STALE` nebo `ERROR`.
Current production audit má záměrně skončit `NOT_READY`, dokud nejsou doložené
externí prerequisites:

| Gate | Required evidence před první execution |
| --- | --- |
| Windows | Sealed v2 UNC nebo drive absolute client root, přesná shoda s runtime evidence a fresh 240 UTF-16 units budget. V1/null readiness nezískají. NAS path limit je samostatný. |
| Synology snapshot | Snapshot-capable anime shared folder a již vytvořený snapshot ID/time navázaný na manifest baseline. DSM, volume a Snapshot Replication configuration nejsou odhadované. |
| DB backup | Byte-copy DB mimo repo, SHA-256/size/user_version shodné s baseline a vazba na manifest. |
| Maintenance | Scanner i ordinary inventory writer zastavené po celou execution až do dokončení post-state verification. |
| Library write capability | Explicitně ověřená safe capability; současný read-only mount tuto podmínku nesplňuje. |
| Rename no-replace | Doložený mount support; platform support není důkaz konkrétního CIFS mountu. |

Tato foundation nevytváří snapshot ani backup, neprovádí write-permission
nebo rename probe a neautomatizuje DSM. Snapshot je druhá safety layer,
nenahrazuje manifest, journal a rollback. Maintenance je external/runtime
requirement; nový persisted maintenance DB stav se nepřidává.
`RuntimeEvidence` obsahuje externě doložené assertions; preflight kontroluje
jejich konzistenci a vazbu na manifest. Dodaný existující DB backup navíc čte
read-only a ověřuje SHA-256, size a SQLite user_version; neexistující či nesouhlasný
backup readiness nesplní. DSM snapshot a durable maintenance lock sám neověřuje.
Skutečné pořízení backupu/snapshotu a doložení těchto externích podmínek patří
samostatnému pre-execution kroku.

### Runtime evidence CLI composition

Official `dry-run --runtime-evidence <local-json-path>` skládá existující
`RuntimeEvidence`; library API ji nadále smí přijmout přímo. File musí být lokální
independent regular file, bez symlinků včetně ancestors a bez hardlink aliasu,
mimo `/mnt/nas-anime` i actual/manifest library root. Remote nebo neověřený
filesystem se odmítá. Output nesmí tento input přepsat.

Strict JSON object musí obsahovat všech těchto 14 keys a žádné další:

| External field | JSON type |
| --- | --- |
| `scanner_paused_through_verification` | boolean |
| `inventory_writer_paused_through_verification` | boolean |
| `snapshot_capable` | boolean |
| `snapshot_id` | string nebo null |
| `snapshot_at` | string nebo null |
| `snapshot_manifest_id` | string nebo null |
| `db_backup_path` | string nebo null |
| `db_backup_sha256` | string nebo null |
| `db_backup_size` | integer nebo null |
| `db_backup_user_version` | integer nebo null |
| `db_backup_manifest_id` | string nebo null |
| `mount_read_write_checked` | boolean |
| `safe_write_capability_proven` | boolean |
| `rename_noreplace_capability_proven` | boolean |

Missing/unknown/duplicate keys, nesprávné typy, malformed JSON nebo unsafe input
znamenají CLI error, bez optimistic fallback. Boolean není integer. Bez argumentu
zůstávají external fields na fail-closed `RuntimeEvidence` defaults; žádná
podmínka se automaticky nepotvrdí. Explicitní false/null rovněž nic neprokazuje.

Tyto čtyři live/current fields jsou v JSON zakázané:

| Live field | Autorita |
| --- | --- |
| `actual_windows_root` | Explicitní `--windows-root` při dry-run, přesně shodný s v2 sealed rootem z generation. Bez argumentu zůstává neprokázaný. |
| `mount_identity` | Fresh `observe_runtime()` nad skutečným library mountem. |
| `mount_read_only_now` | Fresh `observe_runtime()` nad skutečným library mountem. |
| `free_bytes` | Fresh `observe_runtime()` nad skutečným library mountem. |

JSON obsahuje pouze externí assertions. Stávající preflight dále ověřuje snapshot
time/capability a manifest binding, existující DB backup bytes/fingerprint a
binding, maintenance, capability assertions, mount identity, Windows budget
a manifest freshness. Capability assertion nepřebije fresh RO state. Evidence
není součást manifest payloadu, ID/hash, targets, actions, locator patches,
criticality, warning acknowledgements ani duplicate evidence.

Generation `--windows-root` pečetí do v2 payloadu. Dry-run plánuje vždy se sealed
hodnotou; jeho argument dodává pouze runtime důkaz, nikdy nový planning root.
Jiný validní root vrací `NOT_READY/windows_root_mismatch`, vynechaný argument
`actual_windows_root_not_proven`. Generation bez argumentu ukládá explicitní null.
CLI report uvádí sealed `windows_root`; manifest se při dry-run nemění.

Tento ingress nevytváří production artifacts, snapshot ani backup a neprovádí
remount nebo probes. Executor stále neexistuje; NAS execution není autorizovaná.
Operational runbook vytvořený pro starší HEAD vyžaduje po změně CLI revalidation
proti novému committed HEAD před jakoukoli budoucí write authorization.

P2C není prerequisite manifest generation, dry-run, physical reorganization
ani read-only verification. Je required před ordinary write-capable scanner
rescanem reorganizovaného canonical stromu. Preservation source evidence
v scanner create/update lifecycle zůstává P2C debt. Completeness UI je pending.

`#recycle` je SYSTEM_EXCLUDED mimo aktivní knihovnu: bez traversal, čtení obsahu,
manifest action, recovery nebo oprávnění. DB-known locator uvnitř zůstává
explicitní missing active asset (`source_missing_or_not_regular`), ne recovery
authority. Technical roots `Subs`/`Duplicates`/`#recycle` nejsou anime owner
locator authority.

## Read-only a verification boundary

Generation/preflight používají SQLite `mode=ro`, SELECT-only batch loader a
clean session. Filesystem checks sdílejí jeden nofollow inventory snapshot;
nejde o walk per action ani media-byte hashing celé knihovny. Size/mtime/stat
evidence neprokazuje nezměněné bytes při změně zachovávající všechny stat values.

Post-state prediction používá committed pure closure a explicitní duplicate
evidence. Regression gate zahrnuje real scratch locator-only reload přes normal
loader i negative reload bez evidence. Expected post-state hash normalizuje
pouze directory `mtime_ns`, protože po rename/mkdir nejsou předvídatelné;
directory paths/kinds, regular-file stat, records, statuses, collisions,
container locators a carried provenance zůstávají součástí ověření.
`post_state_hash_from_manifest` vypočte stejnou hodnotu nad fresh reloadem
a skutečným post-state snapshotem. Pre-execution filesystem fingerprint
a původní pure projection hash se tím nemění.
Budoucí executor musí těsně před každou
operací znovu kontrolovat preconditions a implementovat durable crash protocol,
atomic no-overwrite, reconciliation a recovery. Tento dokument nezavádí executor.

[CLI](../app/tools/execution_manifest.py) má pouze `generate-manifest` a `dry-run`.
Obě commands vyžadují explicitní output file na lokálním filesystemu mimo repo
a library/NAS; odmítají i symlink aliases a přepsání vstupní DB/manifestu přes
hardlink. Parent musí již existovat. Atomic local serialization není library
operation. Úspěšná generation pouze vytvoří review artifact; readiness hodnotí
dry-run. Runtime JSON není součást repozitáře.
