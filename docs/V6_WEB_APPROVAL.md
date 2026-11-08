# V6 Web, část 2 – přesný manifest a schválení

Současná AnimeDB je kompletně administrátorská aplikace v důvěryhodné domácí
LAN, minimálně do konce V7.5. Tato část nepřidává účty, role ani přihlašování.
Jméno správce ve formuláři je evidenční údaj, nikoli ověření identity.
Budoucí oddělení administrace nemá přiřazenou verzi; rozsah V8–V10 se nemění.

**Webové schválení funguje. Fyzické spuštění z této větve je deaktivované.**
Administrativní převzetí pouze ověří a jednorázově spotřebuje schválení.
Nevzniká executor, daemon, job queue, runtime lock ani nový DB model.
Dosavadní [produkční E7 vstup](V6_PRODUCTION_REORGANIZATION.md) se nemění.

## Hranice procesu a úložiště

- Web má dosavadní RO media mount, lokální SQLite a samostatný lokální outbox.
  Nemá RW media mount, SMB credentials, Docker socket ani obecný command endpoint.
- Správce publikuje původní canonical manifest do lokálního inboxu. **Inbox se
  webu připojí samostatným read-only bindem.** Samotný chmod při stejném UID
  tuto hranici nenahrazuje. Nezpřístupňovat jeho zapisovatelného předka.
- Outbox je webu zapisovatelný. Host jeho obsah považuje za nedůvěryhodný:
  ověří jej proti vlastnímu původnímu manifestu a nezávisle zadanému finálnímu SHA.
- Jednorázový host ledger je oddělený od obou schránek a web jej vůbec nedostane.
  Host přidá informační acceptance receipt do inboxu až po zápisu ledgeru.
- Všechny schránky a evidence jsou mimo media root/repozitář na lokálním
  filesystemu, s kompatibilním efektivním UID a bez group/world write práv.
  Doporučené adresáře `0700`, soubory vytváří nástroj jako `0600`.
  Adresáře připravuje správce; web je automaticky nevytváří.

Například oddělené bindy `/approval-inbox:ro`, `/approval-outbox:rw` a pouze
hostitelský `/approval-ledger`. Nejde o provedený deploy ani změnu Compose.
Konfigurace webu je opt-in, bez ní jsou schvalovací routy nedostupné:

```ini
REORGANIZATION_APPROVAL_INBOX=/approval-inbox
REORGANIZATION_APPROVAL_OUTBOX=/approval-outbox
```

Web i administrativní prostředí používají stejné absolutní cesty k existující
SQLite a knihovně, například `/data/anime.db` a `/media/anime`. Jejich RO/RW
CIFS pohledy mohou mít různé dev/inode. Manifest se vytváří nad RW pohledem
workeru a tento binding se po webovém schválení nepřepisuje. Žádný mount se
kvůli schválení nepřepíná. Současná production mount policy vyžaduje právě
jediný autorizovaný CIFS pohled na daný share/podstrom bez viditelných aliasů.

## Příprava původního manifestu na hostiteli

Použít existující `app.tools.execution_manifest generate-manifest`, nikoli JSON
z orientačního webového preview. Generator používá `build_execution_manifest`,
aktuální Target Planner a potvrzené Naming/Layout/Hierarchy autority.
Obsahuje skutečné akce KEEP/MOVE/QUARANTINE, SOURCE/TARGET, directory dependencies,
DB fingerprint, filesystem a archivní evidence, worker mount binding a Windows root.

Běžná videa se nehashují. ZIP/RAR/7Z používají přesně dosavadní archive evidence
kontrakt generatoru/inventury. Lightweight preview nadále archivy nehashuje a
může mít jiný plan hash; nikdy se automaticky nepovyšuje na execution manifest.

Správce připraví existující 14polovou runtime evidence, skutečný snapshot a
nezávislou lokální DB backup kopii vázané na manifest. Backup musí být pro
webový RO preflight dostupný na stejném místě, ideálně samostatným RO bindem.
Evidence je omezená na daný manifest; neuděluje webu RW capability.

Následující příkazy jsou návod pro budoucí samostatně autorizovanou hostitelskou
operaci, nikoli povolení kontaktovat NAS nebo Nexus během vývoje:

```bash
python -m app.tools.reorganization_approval publish \
  --db /data/anime.db --library-root /media/anime \
  --share //SERVER/Anime --share-root / \
  --manifest /data/final-manifest.json \
  --runtime-evidence /data/runtime-evidence.json \
  --windows-root '\\SERVER\Anime' --inbox /approval-inbox
```

Publish načte a ověří canonical manifest, skutečný RW CIFS mount a fresh
read-only preflight. Povolí pouze ještě nepotvrzené konkrétní warningy;
jakýkoliv jiný NOT_READY, STALE, ERROR, BLOCKED nebo REVIEW znamená STOP.
Nevolá E7, startup, migrace ani scanner. Před otevřením SQLite kontroluje
existující schema-v9 rollback-mode header a nepřítomnost WAL/SHM/journalů,
stejně jako E7; žádnou recovery nezahajuje.

Vznikne create-once `<source_sha256>.proposal.json` s původním canonical JSON,
zachycenou runtime evidence, časem a počtem/přesnými cestami nových adresářů.
SHA se počítá z UTF-8 `manifest_to_json`, bez koncového newline.

## Webové review a jednorázové schválení

`/reorganization` ponechává dosavadní orientační preview a odkazuje na
`/reorganization/approval`. Otevření manifestu ani stažení canonical JSON
neprovádí filesystem inventuru a nemění SQLite či media.

1. Správce vidí přesné počty KEEP/MOVE/QUARANTINE, nové a potřebné adresáře,
   výslednou strukturu, skutečné SOURCE/TARGET, Windows/mount/DB/FS bindingy,
   konkrétní warning IDs i celý canonical envelope ke stažení.
2. Ve formuláři uvede evidenční jméno a jednotlivě potvrdí všechny warningy.
   POST `/prepare` vytvoří dosavadní `WarningAcknowledgement` objekty.
   Jejich actor/timestamp jsou součástí nového canonical SHA.
3. Omezený samostatný RO proces znovu načte DB a plnou filesystem inventory,
   použije existující `preflight_execution_manifest` nad přesným envelope a
   publikovanou hostitelskou runtime evidence. Vyžaduje READY_FOR_EXECUTION.
   Tento výsledek **neprokazuje aktuální RW runtime**; stránka to výslovně uvádí.
4. Uloží se create-once `<source_sha256>.review.json`. Správce si prohlédne
   výsledný canonical SHA a přesný envelope. Nejde ještě o schválení.
5. Samostatný POST `/approve` vyžaduje checkbox i přesný zobrazený SHA,
   znovu úspěšný fresh RO preflight a vytvoří jediný `.approval.json` receipt.
   Původní manifest se nepřepočítává a payload/timestamp se nemění; jediným
   dovoleným rozdílem proti původnímu envelope jsou konkrétní acknowledgements.

Akce, adresáře i warningy jsou stránkované po 50; filtry operací/stavů a hledání
se týkají celého manifestu. Výběr warningů přetrvává přes stránky.
POST vyžaduje shodný Origin a podepsaný CSRF token vázaný na source i canonical
SHA, s HttpOnly/SameSite=Strict cookie. CSRF secret je pouze v paměti procesu;
nejde o přihlášení ani uživatelskou session. Restart nebo přechod na jiný
webový proces vyžaduje nové otevření formuláře. Pro současnou jedinou instanci
není potřeba sdílená session služba.

Hostitelská evidence i CSRF mají platnost nejvýše 15 minut. Po vypršení se musí
připravit nový původní manifest a znovu provést review, nikoli přepsat existující
soubory. Timeout používá stávající `REORGANIZATION_PREVIEW_TIMEOUT_SECONDS`,
stejnou bounded admission a kill/reap policy; přežívající proces blokuje další
inventuru. Chyba neposkytne částečný platný plán nebo schválení.

## Hostitelské převzetí – execution stále vypnutá

Po webovém schválení správce odstaví aplikaci a všechny dotčené writery,
ověří skutečnou sdílenou cestu existujícího `/state/runtime.lock` a spustí
administrativní kontrolu v izolovaném RW prostředí se stejným efektivním UID
jako vlastník lokální SQLite/evidence. Zastavení vzdálených NAS writerů nelze
prokázat advisory flockem; zůstává výslovnou provozní povinností správce.

```bash
python -m app.tools.reorganization_approval accept \
  --db /data/anime.db --library-root /media/anime \
  --share //SERVER/Anime --share-root / \
  --inbox /approval-inbox --outbox /approval-outbox --ledger /approval-ledger \
  --source-sha256 PUVODNI_SHA --approve-sha256 ZOBRAZENY_SCHVALENY_SHA \
  --actor SPRAVCE --runtime-evidence /data/runtime-evidence.json \
  --runtime-lock /state/runtime.lock --windows-root '\\SERVER\Anime' \
  --writers-stopped
```

Host nepřebírá příkazy, paths, share, runtime assertions ani SHA z HTTP body.
Lokálně zadaný SHA i správce se musí shodovat s canonical review a receipt.
Ověří celý původní hostitelský envelope, stávajícím
`app.tools.reorganize.runtime_exclusion` drží původní runtime.lock, prověří
aktuální RW CIFS mount a fresh preflight s aktuální runtime evidence.
Teprve pak vytvoří host-private `<manifest_id>.accepted.json`. Tím stejné
payload identity nepomůže ani nový source envelope s jiným časem.

Všechny záznamy používají O_EXCL, nofollow, fsync souboru a adresáře.
Existující nebo neúplný záznam znamená STOP. Při přerušení mezi host ledgerem
a informačním receipt zůstává approval spotřebovaný; nic se automaticky
nemaže, neopakuje ani neobnovuje. SQLite a media se tímto převzetím nemění.

Web rozlišuje „Schváleno“ a „Převzato hostitelem, spuštění deaktivované“.
„Předáno ke spuštění: ne“ a „Spuštěno: ne“ zůstávají pravdivé i po převzetí.
Není zde `/execute`, `/start`, retry ani skrytý přechod na dosavadní CLI.
Převzetí není odložená úloha, kterou později někdo automaticky spustí.

## Návaznost části 3 a skutečný E7

Pro fyzické spuštění zbývá řízeně propojit schválený, jednorázově claimnutý
canonical envelope s existujícím
`app.tools.reorganize.execute_approved_reorganization`. Stále musí držet
stejný runtime lock a E6B, použít `ScratchManifestExecution.open_existing`,
E7 fresh preflight, původní journal/guard a completion do NORMAL. Nový executor
ani runtime lock nejsou potřeba. Převzetí z této deaktivované části se nesmí
vydávat za restart/resume authority nebo automaticky reaktivovat.

E7H vytváří živý cleanup proposal až po přesunech a kontrole původního
post-state. Část 3 musí řešit průběh, čekání na konkrétní rozhodnutí, STOP při
odpojení/přerušení, výsledky a jejich pravdivé zobrazení. První běh může použít
stávající interaktivní `_interactive_cleanup` přes administrátorský terminál:
konkrétní JSON seznam prázdných adresářů a přesný živý proposal SHA. Předběžný
webový výběr není cleanup approval. Tato část proto E7 nevolá s odpojeným stdin.

První reorganizace musí zahrnovat schválený cleanup nepotřebných prázdných
adresářů. IMPORT, Duplicates, Subs a systémové namespaces zůstávají chráněné;
V6 fyzické duplicity nemaže. Žádné automatické retry, rollback ani copy fallback.
Provozní audit skutečných Nexus bindů, runtime locku, UID/GID a CIFS capability
zůstává samostatný předpoklad prvního nasazení, nikoli provedená vývojová kontrola.

### Otevřené body pro část 3

Tyto body jsou po nezávislém review vědomě odložené. Fyzické spuštění v části 2
zůstává deaktivované.

- **N1, jméno schvalujícího:** u manifestu bez warningů přežije zadané jméno jen
  přímou odpověď na `/prepare`. Po GET formulář použije `administrator` a host
  `accept` se skutečným jménem skončí STOP. Řešit současně s N3, které z dalšího
  zobrazení vždy udělá GET.
- **N3, přesměrování po POST a STOP stránky:** obnovení stránky po POST dnes
  vrací JSON 403 nebo 405 bez dalšího účinku a STOP hlášky jsou také JSON.
  Doplnit přesměrování a uživatelsky srozumitelné STOP stránky.
- **N4, event loop:** zbývající `approval.sha256` a `validate_review` v POST
  routách přesunout mimo event loop.
- **Napojení na skutečný E7 worker:** převzetí musí uvolnit `runtime_exclusion`
  před voláním `execute_approved_reorganization`; stejný lock nelze vnořit.
- **Živý E7H cleanup, průběh, výsledky a STOP** podle předchozích odstavců.
- **Stav `ACCEPTED_EXECUTION_DISABLED`:** nesmí se automaticky převzít jako
  oprávnění ke spuštění. Záznam v ledgeru blokuje stejný `manifest_id`, proto
  do části 3 nepoužívat `accept` nad plánem určeným k reálnému běhu.
- **15minutové approval okno:** ověřit na Nexusu včetně ARM, CIFS a hashování
  archivů.
