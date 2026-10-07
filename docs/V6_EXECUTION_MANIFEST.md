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

Generic Bonus nemá automaticky LOW. Chybějící explicitní authority vyžaduje
criticality review; samotný název adresáře nebo Bonus kontejner ji nevytváří.
Source/parser PV evidence se odlišuje od authoritative story Preview.
Konkrétní human criticality evidence je execution rozhodnutí, nikoli změna
doménových polí.

Subtitle přes platné M:N compatibility přebírá nejvyšší criticality svých
compatible Videos. Pokud má pouze LOW relationships, je LOW.
Owner-less/unmatched a D06 `confirmed_no_match` mají mandatory PRIMARY safety
preservation bez vymyšleného Video ownera. Auxiliary preservation rovněž
nevytváří anime content identity.

Future PRIMARY failure znamená STOP. LOW continuation vyžaduje nedotčený source,
žádný částečně vytvořený target, journaled failure a žádnou dependent PRIMARY
action. Journal foundation definuje pouze datové stavy a serialization;
filesystem state transitions ani jejich execution nejsou implementované.

## Warning approval

Acknowledgement obsahuje konkrétní stable warning ID, class, object identity,
actor a čas. Neexistuje class wildcard ani `ignore_warnings` bypass.
Neacknowledged warning blokuje readiness. Nový warning po schválení vyžaduje
nové review; acknowledgement staré class jej automaticky nepokrývá.
API `WarningAcknowledgement` ukládá tato rozhodnutí do approved envelope;
generování je samo nepotvrzuje. `CriticalityAuthority` obdobně nese konkrétní
human severity/reason/actor pro unresolved content. Mandatory PRIMARY nelze
takovým rozhodnutím snížit na LOW.

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
