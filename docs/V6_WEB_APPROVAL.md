# V6 – webové schválení, administrativní provedení a výsledky

AnimeDB zůstává kompletně administrátorskou aplikací v důvěryhodné domácí LAN,
minimálně do konce V7.5. Nepřidávají se účty, role ani přihlašování. Jméno správce
je evidenční údaj, nikoli ověření identity. Budoucí oddělení administrace nemá
přiřazenou verzi; rozsah V8–V10 se nemění.

Funkční řetězec tvoří canonical manifest → webové review/schválení → explicitní
hostitelský `execute` → původní E7 → živé interaktivní E7H rozhodnutí → NORMAL →
webový report po obnovení aplikace. Webová část je od 8. 11. 2026 nasazená; k
2026-11-09 nebyl publikován žádný manifest a `execute` neproběhlo. Hostitelský
řetězec je ověřený jen lokálně synteticky a nejde o povolení k produkční
reorganizaci NAS. Provozní předpoklady zůstávají v
[produkčním runbooku](V6_PRODUCTION_REORGANIZATION.md).

## Oddělení procesů a dat

- Web má dosavadní RO media mount a lokální SQLite. Nemá RW media mount,
  SMB credentials, Docker socket ani command/execute endpoint.
- Host publikuje původní canonical manifest do lokálního inboxu. Web dostane
  **samostatný read-only bind inboxu**, bez zapisovatelného předka. Chmod při
  stejném UID tuto hranici nenahrazuje.
- Web zapisuje jen lokální outbox review a approval artefaktů. Host jim
  nedůvěřuje: porovnává je s původním manifestem a lokálně zadaným finálním SHA.
- Host-private ledger je oddělený od obou schránek a web k němu nemá přístup.
  Stejný create-once ledger chrání dosavadní `accept` i skutečný `execute`.
- Evidence zůstávají mimo knihovnu/repozitář na lokálním filesystemu, se stejným
  efektivním UID a bez group/world write práv. Doporučené adresáře `0700`;
  nástroj vytváří soubory `0600`. Adresáře připravuje správce.

V produkci jsou `/approval-inbox` (RO) a `/approval-outbox` (RW) připojené
lokálním V6 override. Host-private `/approval-ledger` web nevidí. Web je opt-in:

```ini
REORGANIZATION_APPROVAL_INBOX=/approval-inbox
REORGANIZATION_APPROVAL_OUTBOX=/approval-outbox
```

Web i worker mají stejné absolutní cesty SQLite/knihovny, například
`/data/anime.db` a `/media/anime`. Jejich RO a RW CIFS pohledy mohou mít různé
inode/dev. Finální manifest se vytváří nad RW pohledem workeru a jeho binding
se po schválení nepřepisuje. Worker vyžaduje jediný autorizovaný CIFS pohled
na přesný server/share/podstrom bez viditelných aliasů. Credentials a mount
připravuje správce mimo web i tento nástroj.

## Původní canonical manifest a publikace

Použít dosavadní `app.tools.execution_manifest generate-manifest`, nikoli JSON
z orientačního preview. Generator používá `build_execution_manifest`, Target
Planner a potvrzené Naming/Layout/Hierarchy autority. Obsahuje skutečné
SOURCE/TARGET, KEEP/MOVE/QUARANTINE, directory dependencies, DB fingerprint,
filesystem/archivní evidence, worker mount binding a Windows root.

Běžná videa se nehashují. ZIP/RAR/7Z používají přesně současný archive evidence
kontrakt. Lightweight preview selektivně hashuje jen odpovídající kandidátní
archivy podle potvrzených secondary/primary DB vazeb. Prokázané kopie řeší stejný
planner automaticky; bez důkazu zůstává ruční REVIEW. Plné archive evidence
a Windows root mohou nadále změnit plan hash; preview nezakládá execution authority.

Správce dodá existující 14polovou runtime evidence, skutečný snapshot a nezávislou
lokální backup kopii SQLite vázané na manifest. Backup musí být pro webový RO
preflight dostupný na stejném místě, ideálně samostatným RO bindem.

Příklad pro budoucí samostatně autorizovaný hostitelský provoz:

```bash
python -m app.tools.reorganization_approval publish \
  --db /data/anime.db --library-root /media/anime \
  --share //SERVER/Anime --share-root / --manifest /data/final-manifest.json \
  --runtime-evidence /data/runtime-evidence.json --windows-root '\\SERVER\Anime' \
  --inbox /approval-inbox
```

Publish ověří canonical manifest, aktuální RW mount a fresh read-only preflight.
Připustí jen dosud nepotvrzené konkrétní warningy; ostatní NOT_READY, STALE,
ERROR, BLOCKED nebo REVIEW znamenají STOP. Nevolá worker, startup, migrace ani
scanner. SQLite se před otevřením kontroluje na existující schema-v9 rollback
header a nepřítomnost WAL/SHM/journalů; žádná implicitní recovery.

Vznikne immutable `<source_sha256>.proposal.json` s původním canonical JSON,
zachycenou runtime evidence, časem a přesnými novými adresáři. SHA je UTF-8
`manifest_to_json` bez koncového newline.

## Webové review a schválení

`/reorganization` zachovává orientační preview a odkazuje na
`/reorganization/approval`. GET/stažení neinventarizují media a nemění SQLite.

1. Správce vidí počty operací, nové/potřebné adresáře, SOURCE/TARGET, strukturu,
   warning IDs a přesné Windows/mount/DB/FS bindingy i canonical JSON ke stažení.
2. Zadá jméno a jednotlivě potvrdí warningy. POST `/prepare` vytvoří stávající
   `WarningAcknowledgement` objekty a vyžaduje úspěšný fresh preflight.
3. Uloží se canonical `.review.json` a malý `.review-actor.json`, vázaný na
   původní source SHA a finální canonical SHA. Metadata správce nemění manifest
   ani execution authority; umožňují zachovat jméno i po GET bez warningů.
   Neúplný zápis se neopravuje/přepisuje automaticky. Historické review bez
   warningů a bez metadata správce vyžaduje nový manifest; jméno se nevymýšlí.
4. Úspěšný POST přesměruje HTTP 303 na původní GET manifestu. Finální formulář
   schvaluje právě zobrazený canonical SHA, včetně acknowledgements.
5. POST `/approve` znovu ověří fresh DB/FS preflight, explicitní checkbox, SHA
   i správce review, vytvoří jediný `.approval.json` a přesměruje 303 na GET.

Preflight webu pracuje s aktuální DB/plnou inventurou a **zachycenou hostitelskou
runtime evidence**, nikoli s fiktivním RW bindingem vlastního RO mountu. Host
musí skutečné podmínky ověřit znovu. Payload a původní timestamp se nemění;
jediný povolený rozdíl envelope jsou konkrétní acknowledgements.

Akce, adresáře a warningy se stránkují po 50; filtry a hledání platí pro celý
manifest, výběr warningů přežívá stránkování. Design a preview zůstávají stejné.
POST chrání shodný Origin a podepsaný CSRF vázaný na source/canonical SHA,
s HttpOnly/SameSite=Strict cookie a `Referrer-Policy: same-origin`.
`Origin: null` se odmítá. CSRF secret je v paměti procesu; restart nebo jiný
webový proces vyžaduje nové otevření formuláře. Všechna blokující práce POSTu
včetně konfigurace, SHA, načítání, zápisů a renderu běží ve Starlette threadpoolu.
Chyby zobrazují HTML STOP stránku se zachovaným HTTP statusem, bez přeměny
chyby na úspěch nebo automatického retry.

## Časová platnost

Zachovává se původních **900 sekund od publikace** pro přípravu, schválení
a hostitelské převzetí. CSRF má stejnou maximální délku platnosti. Změna
lhůty nebyla zavedena: lokální syntetický průchod neprokázal její nedostatečnost
a měření není odhad doby NASu nebo lidského review celé knihovny.

Host kontroluje lhůtu při převzetí a znovu pod původním runtime lockem před
vstupem do E7. Jde o admission, nikoli timeout rozběhnuté reorganizace:
platně zahájený běh může trvat déle než 15 minut, bez umělého STOP uprostřed
přesunů/cleanupu. Expirovaný plán se nespouští; vyžaduje nový manifest a review.
Čekání na živý cleanup nevytváří nové schválení reorganizace ani retry authority.

Preview/inspect dál používají vlastní `REORGANIZATION_PREVIEW_TIMEOUT_SECONDS`
a dosavadní kill/reap policy; přežívající proces blokuje další inventuru.
Execution není automaticky zabíjena tímto webovým timeoutem.

## Explicitní hostitelský start

Správce odstaví celou aplikaci a všechny dotčené writery, ověří totožný lokální
`/state/runtime.lock`, RW mount, UID/GID a ostatní provozní podmínky. Vzdálené
NAS writery advisory flock neblokuje; jejich odstavení je provozní povinnost.
Příkaz ještě před spotřebováním schválení vyžaduje skutečný interaktivní **stdin
i stdout** a dostupný Python modul `readline`. Ten se načte před execution, aby
dlouhý JSON seznam cleanupu neomezoval kanonický terminál na 4095 znaků.
Použijte přímo připojený terminál v `tmux` nebo `screen`; **žádný `| tee` ani
přesměrovaný stdout**. Chybějící TTY/readline znamená STOP před claimem.

```bash
python -m app.tools.reorganization_approval execute \
  --db /data/anime.db --library-root /media/anime \
  --share //SERVER/Anime --share-root / \
  --inbox /approval-inbox --outbox /approval-outbox --ledger /approval-ledger \
  --source-sha256 PUVODNI_SHA --approve-sha256 SCHVALENY_CANONICAL_SHA \
  --actor SPRAVCE --runtime-evidence /data/runtime-evidence.json \
  --runtime-lock /state/runtime.lock --windows-root '\\SERVER\Anime' \
  --writers-stopped
```

Host porovná originál, canonical review, receipt, lokálně zadaný SHA/správce,
aktuální stav, mount a preflight. Pod existujícím runtime lockem vytvoří
`<manifest_id>.accepted.json` v původním soukromém ledgeru se stavem
`EXECUTION_CLAIMED` a informační acceptance receipt v inboxu. **Tento kontrolní
lock se uvolní před vstupem do workeru.** `execute_approved_reorganization`
si původní runtime lock získá znovu, zkontroluje stáří při dispatchi, vstoupí do
`ScratchManifestExecution.open_existing`, původního E6B a fresh E7 preflightu.
Při změně podmínek mezi kontrolami je STOP; nic se automaticky neopakuje.

Dosavadní příkaz `accept` pouze spotřebuje schválení do
`ACCEPTED_EXECUTION_DISABLED`. Takový záznam **nikdy není oprávněním ke startu**.
`execute` jej nepřebírá ani neaktivuje. Stejný ledger blokuje i jiný source
envelope stejné payload identity. Není zde retry/resume/reset přepínač.
Soubor vzniká O_EXCL s nofollow a fsync; neúplný/nejistý zápis zůstává STOP.

## Terminál, živý cleanup a původní evidence

Terminál ukazuje fázi a počty dokončených/zbývajících akcí. Callback pozoruje
potvrzený E1 append, counts vycházejí z původního COMPLETE prefixu. Nevzniká
paralelní evidence jednotlivých přesunů. Běžný worker používá METADATA_RENAME
bez čtení video obsahu; volitelná FULL verifikace z původního CLI se nemění.

E7 po MOVE/KEEP/QUARANTINE nejprve ověří původní post-state. Potom původní
`_interactive_cleanup` uloží skutečný `.cleanup-proposal.json`, vypíše kandidáty
a čeká na JSON seznam konkrétních prázdných adresářů a přesný živý proposal SHA.
Terminál výslovně hlásí `WAITING_FOR_CLEANUP_APPROVAL`. Nejde o předem schválený
webový výběr. E7H zachovává IMPORT, Duplicates, Subs a systémové namespaces;
V6 fyzické duplicity nemaže. První reorganizace má zahrnout ručně schválené
odstranění nepotřebných prázdných adresářů. Prázdný seznam má nadále dosavadní
význam zachování adresářů; aplikace nevytváří nové cleanup pravidlo.

Po cleanupu proběhne současná completion do NORMAL. Ctrl+C, SIGTERM, EOF,
nesouhlas SHA nebo skutečná chyba znamenají STOP a původní guard/evidence se
zachovají. Žádný automatický rollback, copy fallback ani opakované spuštění.

## Výsledný webový report a přerušení

Po návratu původního workeru se do RO inboxu uloží create-once
`<source_sha256>.result.json`: canonical identity, správce, konečný stav/fáze,
počty, potvrzený cleanup a odkazy na původní E1/guard/E7H evidence. Je to
souhrn, ne druhý journal. NORMAL se publikuje až po dokončení a uzavření
původního workeru. STOP nepředstírá nulový cleanup: jeho výsledek může být
nepotvrzený a podrobnosti zůstávají v E7H evidence.

Po běžném RO restartu se výsledek zobrazí na původním GET manifestu, zachová
canonical plán, filtry a stránkování. Není potřebný nový route/server/daemon.
SIGKILL nebo neúspěšná publikace může zanechat pouze spotřebovaný claim bez
výsledku. Web ukáže „výsledek zatím nepotvrzený“, nikoli úspěch nebo retry.
Historický report není potvrzením aktuálního budoucího DB/FS stavu.

Pokud přerušený E7 guard blokuje běžný startup, aplikace tuto výluku kvůli
reportu neobchází. Správce nejprve vyhodnotí lokální `.result.json` a původní
evidence v terminálu. Bez vyřešeného STOP nelze slibovat dostupný běžný web.

## Živý odvozený přehled při odstavené aplikaci

Volitelný `execute --service-status-dir /service-status` (nebo prostředí
`REORGANIZATION_SERVICE_STATUS_DIR`) publikuje malý atomicky nahrazovaný
`progress.json` na lokálním NVMe. Adresář připraví správce mimo media/repozitář,
vlastník je UID workera, režim `0755`; veřejný soubor má `0644`. Jde o samostatný
adresář obsahující pouze tento necitlivý souhrn, ne inbox/ledger či DB evidence.
Jeho první publikace se ověří před claimem. Pozdější výpadek publikování nemění
execution authority: stránka přestane považovat zprávy za aktuální.

Souhrn zobrazuje WAIT/WORK/PASS/STOP pro manifest, adresáře, rename, SQLite,
cleanup a verifikaci. Počet dokončených akcí je původní potvrzený E1 COMPLETE
prefix; rename počítá FS_VERIFIED, SQLite fáze se stává PASS až po potvrzení
všech akcí. Další události pouze oznamují dokončenou přípravu adresářů,
dokončený E7H a začátek finální verifikace. NORMAL je až po návratu a uzavření
E7 a uložení výsledku. Nevzniká druhý journal, další DB query ani NAS inventura.
Zápis probíhá na hranicích fází a nejvýše po pěti sekundách při postupu;
vlákno pouze uvnitř běžícího workeru obnovuje timestamp i při čekání na správce.
Přerušení bez konečného výsledku neznamená PASS ani automatický STOP.

LAN port 8000 vlastní [nasazený Nginx edge se servisní stránkou](../deploy/nexus/service-edge/README.md),
malá statická proxy. Ta má jen statické soubory a RO souhrn, bez SQLite/NAS/state/socketu.
Při nedostupném backendu vrací servisní HTML, které přibližně po pěti sekundách
načítá JSON a kontroluje skutečnou dostupnost aplikace. Načtení aplikace ji
nespouští. STOP nic nerestartuje a guard zůstává rozhodující.

Chybějící evidence nehlásí reorganizaci; starý aktivní souhrn nebo nedostupné
aktualizace znamenají „Stav neověřen“. NORMAL/STOP záznam je výslovně poslední
běh, nikdy důkaz další živé reorganizace. Lokální izolované proxy/browser testy
nenahrazují ověření firewallu a chování servisní stránky během reálného E7.
