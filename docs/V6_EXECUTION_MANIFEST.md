# V6 immutable execution manifest a read-only preflight

[Manifest foundation](../app/target_execution_manifest.py) zachycuje jeden fresh
Target Planner result. Workflow končí `GENERATE → REVIEW → PREFLIGHT →
READY_FOR_EXECUTION`. Skutečný filesystem executor, DB reconciliation,
rollback/recovery ani purge nejsou implementované.

## Immutable approval boundary

Versioned canonical JSON payload má stabilní SHA-256 identity. Čas vytvoření
a konkrétní warning acknowledgements jsou envelope evidence mimo semantic hash.
Změna envelope nevytváří nové targets. Datový model obsahuje immutable scalar
values a tuples; JSON loader odmítá neznámé verze, neznámá pole a chybný hash.

Manifest nese planner/policy identifiers, DB fingerprint, filesystem baseline,
library/mount identity, ordered actions, target directory set, locator patches,
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
| `KeyboardInterrupt` / `SystemExit` | Původní přerušení se propaguje beze změny třídy a instance. Po pokusu o syscall je session zablokovaná a exception note vyžaduje STOP/reconciliation. |

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
read-only pre-rename evidence. E1 vytvoří nový journal výhradně přes `O_EXCL`.
Nové read-only `DurableJournal.inspect_live(manifest)` kontroluje přesný envelope,
file/parent binding, canonical historii a shodu s durable acknowledged entries
živého writera. Nemění journal ani neotevírá existující writer pro resume.

Pořadí je pevné:

1. Durable `PENDING`: append, file fsync, potvrzení E1 a live history kontrola.
2. E2A rename s původní observation; až výsledek `DURABLE` povolí další krok.
3. Durable `FS_APPLIED` append a kontrola live historie.
4. Nezávislé read-only `observation.verify()`: source locator absent včetně
   symlinků, target regular file s původním device/inode, size/mtime_ns, bezpečné
   původní parents/root a stejný mount binding. Pouhá existence ani RenameResult
   nejsou verification evidence.
5. Durable `FS_VERIFIED` append a kontrola live historie; teprve pak
   `VerifiedScratchOutcome(outcome='FS_VERIFIED')`.

Původní device/inode evidence existuje pouze v paměti živé session (včetně
drženého source descriptoru) a ve vráceném `VerifiedScratchOutcome`. Journal
frames ji neukládají a manifest nese jen size/mtime_ns, které záměnu souboru se
shodnými metadaty neodhalí. Durable journal s manifestem proto po pádu nejsou
dostatečným identity důkazem pro budoucí recovery.

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

Testy provádějí skutečné isolated scratch renames a read-only inode observations,
injektují chyby a deterministicky ukončují child proces na lifecycle boundaries.
To ověřuje ordering a STOP policy, nikoli power-loss durability NASu. Zůstávají
všechna omezení E2A: exclusive scratch ownership, viditelná mount namespace,
nedoložená produkční durability/storage/mount capability a ARM64/RPi5 chování.
Globální execution lock, produkční maintenance boundary, DB reconciliation,
crash recovery, rollback, resume a produkční executor **nejsou implementované**.
Generate-manifest/dry-run nemají cestu k této vrstvě; Naming/Hierarchy/Metadata,
D06 approval a schema se nemění. **NAS execution není autorizovaná.**

## Externí pre-execution gates

Read-only preflight vrací `READY_FOR_EXECUTION`, `NOT_READY`, `STALE` nebo `ERROR`.
Current production audit má záměrně skončit `NOT_READY`, dokud nejsou doložené
externí prerequisites:

| Gate | Required evidence před první execution |
| --- | --- |
| Windows | Skutečný UNC nebo mapped-drive absolute client root; fresh 240 UTF-16 units budget. NAS path limit je samostatný. |
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
| `actual_windows_root` | Explicitní `--windows-root`, stejný při generation i dry-run. |
| `mount_identity` | Fresh `observe_runtime()` nad skutečným library mountem. |
| `mount_read_only_now` | Fresh `observe_runtime()` nad skutečným library mountem. |
| `free_bytes` | Fresh `observe_runtime()` nad skutečným library mountem. |

JSON obsahuje pouze externí assertions. Stávající preflight dále ověřuje snapshot
time/capability a manifest binding, existující DB backup bytes/fingerprint a
binding, maintenance, capability assertions, mount identity, Windows budget
a manifest freshness. Capability assertion nepřebije fresh RO state. Evidence
není součást manifest payloadu, ID/hash, targets, actions, locator patches,
criticality, warning acknowledgements ani duplicate evidence.

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
