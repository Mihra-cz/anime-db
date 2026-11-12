# V6 – webové dokončení reorganizace

Běžný postup je **náhled → příprava hostitelského manifestu → opravy a nové
plánování → potvrzení WARNING → schválení přesného SHA → samostatné spuštění
→ Nginx/E7H → NORMAL → výsledný report v AnimeDB**. CLI zůstává diagnostickým
administrátorským vstupem. Běžná reorganizace po jednorázové instalaci
koordinátoru nevyžaduje terminál.

Implementace používá původní Target Planner, canonical manifest, approval
inbox/outbox/ledger, E1/E7, CleanupProposal/CleanupApproval a NORMAL guard.
Nevytváří další plánovací databázi, execution journal ani reorganizační engine.
Lokální syntetická verifikace není dokladem provozní připravenosti Synology.
Nexus nasazení a ověření skutečných ACL, snapshotů a CIFS capability jsou
samostatné instalační kroky v [runbooku koordinátoru](../deploy/nexus/docs/V6_COORDINATOR.md).
Fyzická reorganizace NAS tímto vývojem neproběhla; V6 zůstává otevřená.

## Hranice oprávnění

AnimeDB je administrátorská aplikace v důvěryhodné domácí LAN. Jméno správce
je evidenční údaj, nikoli přihlášení. Nevznikají nové aplikační ani Synology účty.

| Proces | Přístup a odpovědnost |
| --- | --- |
| `app` | Dosavadní RO NAS účet a bind, lokální SQLite, RO approval inbox, RW outbox, RO kopie záloh. Vytváří konkrétní žádosti a lidské review. |
| Hostitelský koordinátor | Rootem řízená konfigurace, pevné Docker/Compose operace a integrační hooky; jednorázově přebírá žádosti. Žije nezávisle na aplikaci. |
| Izolovaný worker | Stejný verzovaný image, jiný entrypoint, jediný samostatný RW CIFS mount s existujícím druhým NAS účtem. Sdílená lokální SQLite, runtime.lock a původní execution evidence. |
| `edge` | Nezávislý Nginx, statická servisní stránka, RO `progress.json` a úzký Unix HTTP socket pouze pro živý E7H návrh a rozhodnutí. |

`app` ani `edge` nemají Docker socket nebo RW NAS credentials. `app` nedostává
ani řídicí Unix socket. Nginx neprovádí execution, nečte SQLite/NAS a nemůže
spouštět obecné příkazy. Koordinátor nepřijímá příkazy, image, cesty, runtime
assertions ani Docker options z prohlížeče.

Worker a aplikace sdílejí vnitřní cesty `/data/anime.db`, `/media/anime`,
`/state/runtime.lock`. Jejich oddělené RO/RW pohledy mohou mít jiné inode/dev.
Manifest se vytváří pouze nad skutečným RW pohledem a jeho binding se po
schválení nepřepisuje. Běžný RO mount zůstává funkční a beze změny.

## Náhled a příprava

GET `/reorganization` čte databázové počty bez inventury a semantic writes.
Explicitní preview používá existující omezený read-only helper. Při úplné
inventuře a vyúčtovaných souborech bez BLOCKED/REVIEW/kolizí nabídne tlačítko
**Připravit finální manifest**. WARNING a directory cleanup review přípravu
neblokují; jejich potvrzení má vlastní pozdější krok.

Schopnost požádat o přípravu je podepsaná a váže konkrétní úspěšný preview
na DB fingerprint. Stejný Origin a CSRF chrání POST. Žádost se atomicky
publikuje do stávajícího outboxu; dlouhá práce neběží v HTTP požadavku.
Stránka požadavku průběžně čte hostitelský stav. Při dočasném odstavení aplikace
zůstává dostupný Nginx. Bez převzetí nehlásí připravený manifest.

Host ověří konfiguraci, přesný image a skutečný RW mount, odstaví aplikaci
a nakonfigurované writery. Worker pod původním runtime.lock:

1. Ověří existující rollback-mode SQLite v9 bez sidecarů; nic nemigruje.
2. Načte aktuální autority a plnou filesystem evidenci existujícím generátorem.
3. Vytvoří canonical manifest s živým RW bindingem a nezávislou lokální kopií DB.
4. Hostitelská snapshot integrace vytvoří skutečný snapshot po manifest baseline
   a vrátí jeho ID, čas a vazbu na manifest. Chybějící integrace znamená STOP.
5. Publikace znovu ověří aktuální stav, zálohu, runtime a preflight. Připouští
   jen dosud nepotvrzené konkrétní WARNING, nikoli jiné NOT_READY/STALE/ERROR.
6. Do RO inboxu uloží původní canonical proposal a host obnoví aplikaci k review.

Příprava nevolá E7 a neprovádí media MOVE/QUARANTINE ani cleanup. Při chybě
přípravy koordinátor může obnovit aplikaci přes její původní startup guard,
aby byly dostupné editory; nepřekonává nebezpečný STOP. Schvalovací doba zůstává
**900 sekund od publikace po úspěšném preflightu**, nikoli od začátku dlouhé přípravy.
UI ukazuje zbývající čas a žádné otevření stránky jej neprodlužuje. Backend
platnost kontroluje také po dlouhém schvalovacím preflightu, před spotřebováním
execution claimu a znovu při původní E7 admission. Pro změnu délky okna zatím
neexistuje skutečné měření na Nexusu; jeho délka se nezvyšuje.

## Opravy, WARNING a schválení

Manifestová stránka zobrazuje SOURCE → TARGET, MOVE/KEEP/QUARANTINE, adresáře,
varování, mount/Windows/DB/FS bindingy, přesný canonical JSON a SHA-256.
Seznamy mají filtry a stránkování. Oprava hierarchie, pojmenování nebo jiných
lidských autorit patří do existujících Naming/Layout/Hierarchy editorů a detailu
objektu, poté se vytvoří nový preview a manifest. JSON nelze libovolně editovat.

Původní webový approval backend se nemění v alternativní autoritu:

1. Každý WARNING se potvrzuje jednotlivě jménem správce.
2. Fresh read-only preflight ověří aktuální DB/FS proti datované hostitelské
   runtime evidence. RO aplikace nevymýšlí vlastní RW binding.
3. Vznikne canonical `.review.json` s původními WarningAcknowledgement a metadata
   správce. Nový SHA zahrnuje přesné acknowledgements.
4. Samostatný POST schválí právě tento SHA; immutable `.approval.json` vzniká
   jednou. GET ani stažení manifestu neinventarizují a nezapisují business data.

Změněný, starý nebo expirovaný manifest nedědí schválení. Dosavadní CSRF s browserovou session cookie a samostatnými tokeny pro jednotlivé
formuláře; preview ani druhá záložka nezneplatňují původní formulář. PRG, HTML STOP stránky, preflight timeout a kill/reap policy zůstávají.

## Samostatné spuštění a průběh

Teprve platně schválený manifest nabízí **Spustit reorganizaci NAS**.
Samostatný POST vyžaduje výslovné potvrzení SHA a správce; schválení samo
execution nespouští. Start request obsahuje přesný source SHA, approved SHA,
manifest ID a správce, žádné execution options.

Koordinátor ověří stáří žádosti, platnost a přesnou identitu schválení,
aktuální read-only preflight a provozní předpoklady ještě před odstávkou.
Host-private claim je jednorázový; po úspěšných kontrolách následuje odstavení aplikace/writerů. Worker porovná původní proposal, review, receipt a žádost,
pod původním lockem spotřebuje existující approval ledger a zavolá původní
`execute_from_approval` → `execute_approved_reorganization` → E7. Kontrolní
approval lock se před vstupem do E7 uvolní; execution jej získá sama.

Původní E1 potvrzené události poskytují počty MOVE/QUARANTINE a SQLite práce;
`progress.json` je pouze odvozený atomický souhrn. Host zobrazuje přípravu
manifestu a odstavení aplikace; worker adresáře, přesuny, SQLite, čekání na
cleanup a finální verifikaci. Heartbeat udržuje čerstvý stav i během čekání
na člověka. Ztráta aktualizací je neověřený stav, nikoli úspěch.

Dosavadní `ACCEPTED_EXECUTION_DISABLED` ani již spotřebovaný claim nelze
aktivovat. Po pádu služby se žádost označí STOP a případný osiřelý worker se
odstaví; nesmí se adoptovat, opakovat ani automaticky opravovat.

## Živý E7H během odstavení aplikace

Původní E7 po všech akcích ověří skutečný post-state a vytvoří původní
CleanupProposal. Webový callback uloží tentýž diagnostický
`anime.cleanup-proposal.json` a publikuje přesný proposal do soukromého
hostitelského mailboxu. Worker čeká; žádný kandidát se automaticky nemaže.

Nginx přes úzký hostitelský socket pravidelně načítá pouze malý stav, identitu
a SHA proposal a počty. Kandidáty a CSRF načte samostatně při otevření
formuláře nebo změně SHA; celý before_snapshot ani canonical text se běžným
pollingem nepřenáší. Všechny způsobilé kandidáty předvybere k odstranění,
ukáže jejich počet a souhrn odstranit X / zachovat Y. Předvýběr není schválení.
Seznam adresářů je standardně sbalený v nativním rozbalovacím prvku; jeho
záhlaví ukazuje celkový počet. Souhrn odstranit/zachovat a finální potvrzení
zůstávají mimo seznam. Rozbalení ani sbalení výběr nemění; po validační chybě
zůstává výběr stejného návrhu zachovaný.
Výběr drží v rámci přesného SHA přes stránky, filtr i polling. Vybrat vše /
Zrušit výběr platí pro celý návrh, včetně skrytých položek. Uživatel může:

- výslovně zachovat všechny adresáře;
- vybrat konkrétní prázdné adresáře a potvrdit přesný aktuální proposal;
- odmítnout pokračování a zvolit STOP.

Rozhodnutí chrání přesný Origin, HttpOnly/SameSite cookie a proposal-bound
CSRF. Vzniká jednou, atomicky a s vazbou na source/approved/proposal SHA.
Neplatný/stale/spotřebovaný návrh nemůže získat rozhodnutí pro jiný běh.

Worker používá živý objekt proposal, nikoli jeho případný přepis z webu.
Neprázdný výběr projde původním `approve_cleanup`, následně původním E7H
ověřením identit, obsahu adresářů a evidence. Prázdný výběr používá dosavadní
preserve-all význam. Zachování potomka v UI zachová jeho kandidátní rodiče;
výběr rodiče viditelně zahrne jeho potomky v návrhu. Backend nadále odmítne
parent bez schválených potomků. Změna výběru zruší potvrzovací checkbox.
IMPORT, Duplicates, cílové parents a systémové namespaces zůstávají chráněné.
Subs se zachovává podle skutečných canonical cílů, nikoli pouhého názvu. Používá
se původní `rmdir`, žádné rekurzivní mazání médií nebo duplicit.

CLI `_interactive_cleanup` zůstává dostupný pro administrátorskou diagnostiku,
včetně ochrany dlouhého vstupu přes readline. Webový worker TTY nepotřebuje.

## Uživatelské popisy a technické podklady

Náhled i manifest používají společné české prezentační popisy: krátký název,
příčinu, dopad a případný další krok. Závažnost přebírají ze stávajícího
výsledku; nevytvářejí nové rozhodovací stavy. Kódy, ID a původní podklady
zůstávají v rozbalovacích technických podrobnostech a přesných JSON datech.
Neznámý kód má opatrný obecný popis a zachované původní znění.

Souhrn manifestu odděluje přesuny, ponechání a karanténu i počet potvrzených
upozornění. Schválení a fyzické spuštění zůstávají samostatnými kroky.

## NORMAL, STOP a návrat

Po cleanupu proběhnou původní finální kontroly a completion guardu do NORMAL.
Výsledná stránka ukazuje dokončené operace, počet odstraněných prázdných
adresářů a ověřené změny cest v databázi. Návrat aplikace označí za potvrzený
pouze při odpovídajícím hostitelském dokladu NORMAL/normal/available; samotné
dokončení reorganizace takový doklad nenahrazuje. Obsahuje návrat do katalogu
a přístup ke schválenému návrhu i původní technické evidenci.
Původní immutable `<source>.result.json` dokládá výsledek, počty a původní
E1/guard/E7H evidence. Host obnovuje běžnou aplikaci pouze po ověření shodného
NORMAL výsledku. Nginx kontroluje skutečný `/health`; návrat k reportu vyžaduje
hostitelské `app_ready` i dostupnou aplikaci. NORMAL s neúspěšným restartem
zůstává NORMAL výsledkem E7 s nepotvrzenou dostupností aplikace.

Pokud příprava startu selže a worker pozitivně prokáže, že nevznikl execution
claim ani nejistá E7/SQLite evidence, koordinátor řízeně obnoví guarded aplikaci.
To není retry reorganizace. Při skutečném E7 STOP nebo nejistotě se aplikace automaticky neobnovuje. Nginx zůstává
k dispozici, zobrazuje příčinu/fázi a zachovává všechny evidence. Neexistuje
retry, resume, copy fallback, automatický rollback ani přepis SQLite.
Nejasný stav může vyžadovat administrátorskou diagnostiku; úspěšný běžný
průchod však terminál nevyžaduje.

## Aktuální runtime a měření

Tři konfigurační capability příznaky jsou pouze povolení workflow. Koordinátor
čte rootem chráněný existující RuntimeEvidence doklad a porovnává jeho binding
s aktuálním hostitelským mountem, mount ID, mountinfo a boot ID. Remount nebo
reboot vyžaduje nové skutečné ověření. Statické `true` nestačí. Pro každý krok
workeru předá tentýž RuntimeEvidence typ, včetně skutečného výsledku odstavení
app a writer hooku; worker nepředpokládá odstavení zapisovatelů.

Hostitelský souhrn `timings_seconds` měří odděleně přípravu, publikaci, online
check-start a execution. App INFO logy `V6 timing` rozlišují warning_preflight,
approval_preflight a start_preflight. Lidský interval lze vyhodnotit z původních
`published_at`, acknowledgement timestamps a `approved_at`; přijetí z
`accepted_at`. Tyto údaje nejsou novou execution evidencí. Dosavadní lokální
časy nepředstavují měření Raspberry Pi 5.
