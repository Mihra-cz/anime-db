# V6 M10 — podklady pro nezávislou revalidaci

Datum: 2026-11-15. Výchozí lokální `main`/HEAD: `7b2c2eea425aa14643f999895bf0b158d539ffe6`.
Změny jsou necommitnuté. Produkční Nexus, Synology a skutečná knihovna nebyly
součástí implementace ani testů. Tento dokument není souhlas s nasazením/E7.

## Kontrakt a scope

`automatic` zůstává výchozí včetně původních snapshot/writer hooků.
`manual_one_shot` musí být výslovně nastavený ve stejné root-owned konfiguraci
koordinátoru i workeru. Nemění engine E7/E7H, scanner, parser, naming, schema,
manifest, journal, SQLite transakce, SOURCE/TARGET/kolize, rename no-replace,
filesystem identity, STOP ani completion do NORMAL.

Ruční režim používá původní plan a SQLite backup, potom čeká na Nginxu na
jednorázové potvrzení skutečného DSM snapshotu a odpojeného Windows SMB sdílení.
Windows může zůstat zapnutý. Správce potvrzuje nepřítomnost dalších writerů a
trvání výluky přes review a celé E7/E7H. `operator_attested` není nezávislé
strojové ověření; původní strojová snapshot pole zůstávají false/null.
Strojově se nadále ověřuje lokální odstavení app, runtime.lock, mount/session,
capability doklad, backup a aktuální DB/FS proti původnímu manifestu.

V existujícím ledgeru je jediný ruční záznam
`<request>.operator-attestation.json`; jeho obsah se přenáší v původním
RuntimeEvidence transportu a SHA váže approval/start. Nejde o další execution
journal. Host-private create-once claim zachovává původní hranici při nejistém
zápisu. Manifest/source/request/challenge a Origin/cookie/CSRF se kontrolují
před přijetím. Snapshot musí být post-baseline, timezone-aware a nebudoucí.

Samostatný monotónní snapshot deadline má výchozích 1800 s, rozsah 1–3600 s.
Pouze publish čerstvě převzaté přípravy po platném ručním čekání smí přesáhnout
900 s stáří původní přípravné žádosti. Žádný timestamp se nepřepisuje.
Proposal/approval/start/E7 admission nadále vyžadují platnost 900 s od publikace
po preflightu; návrat app tento čas spotřebovává. Externí výluku návrat app
k review ani NORMAL automaticky neuvolňuje. Po NORMAL správce ověří report,
SQLite, cílové soubory a app dostupnost před opětovným připojením Windows.

## Přesný seznam souborů

| Soubor | Důvod změny |
| --- | --- |
| `app/target_execution_manifest.py` | Typovaná operator attestation, striktní vazby/časy/potvrzení a kompatibilní čtení původního RuntimeEvidence. |
| `app/tools/reorganization_worker.py` | Explicitní mode, validace ručního publish a zachování evidence při původním E7 admission. |
| `app/reorganization_approval.py` | Neměnná vazba ruční evidence, její SHA v approval a kontrola při převzetí. |
| `app/reorganization_approval_web.py` | Kontrola evidence v approval a samostatné znovupotvrzení trvající ochrany při startu. |
| `app/templates/reorganization_approval.html` | Viditelný operator_attested původ, start checkbox a závěrečná Windows instrukce. |
| `deploy/nexus/reorganization_coordinator.py` | Default-compatible konfigurace, omezené čekání, jednorázový socket endpoint a místní odstavení bez předstírání řízení SMB. |
| `deploy/nexus/service-edge/nginx.conf` | Jediný omezený route `/protection` přes stávající Unix socket. |
| `deploy/nexus/service-edge/index.html` | Ruční DSM/Windows formulář na stávající servisní stránce. |
| `deploy/nexus/service-edge/service.js` | Polling, přesné odeslání potvrzení, reset cizí výzvy a pravdivé provozní instrukce. |
| `tests/test_reorganization_worker.py` | Ruční/automatický runtime, časové a provenance regrese, původní E7/E7H do NORMAL/STOP v obou režimech. |
| `tests/test_reorganization_coordinator.py` | Přesná jednorázová potvrzení, timeout, neúplné/cizí podmínky, režimy, heartbeat, zrušení živé výzvy při chybě a izolace virtuálních hodin restart testu od stdlib subprocess. |
| `tests/test_reorganization_approval_web.py` | Skutečné ASGI review/approval/start s vazbou evidence a nutným potvrzením výluky. |
| `tests/test_reorganization_browser_flow.py` | Existující syntetický web → coordinator → původní E7/E7H → NORMAL/STOP v obou režimech. |
| `tests/test_reorganization_ux.py` | Chromium formulář a reset potvrzení mezi výzvami na šesti desktop/tablet rozměrech. |
| `tests/test_reorganization_service_edge.py` | Existující skutečný Docker/Nginx transport ruční výzvy při odstaveném backendu. |
| `tests/v6_service_edge_browser.cjs` | Existující CDP infrastruktura: ruční odeslání, šest rozměrů, skutečný touch vstup a screenshoty. |
| `docs/V6_WEB_APPROVAL.md` | Uživatelský workflow a skutečné časové/provenance hranice. |
| `deploy/nexus/docs/V6_COORDINATOR.md` | Přesný root config fragment, worker mode, endpoint a provozní kontrakt. |
| `deploy/nexus/service-edge/README.md` | Nový úzký route ve stávající routing tabulce. |
| `docs/PROJECT_STATUS.md` | Současný ruční režim a hranice jeho evidence. |
| `docs/V6_M10_REVALIDATION.md` | Tento předávací dokument; nový soubor. |

## Ověření a reprodukce

Původní coordinator baseline nad čistým exportem HEAD: **57 PASS**.
První sériový cílený běh: **320 PASS**. Následné nezávislé review našlo dvě
chyby nového UI/lifecycle: přijetí potvrzení po chybě publikace průběhu a
přenesení formulářových hodnot přes mezeru mezi různými challenge. Obě mají
regresní reprodukci; opravy jsou součástí pracovního diffu.

Přímé doplňkové regrese změny DB/filesystemu a mount session v obou režimech:
**4 PASS**. Při jednom opakování společné sady selhal původní
`test_restart_wait_uses_health_and_runtime_without_repeating_execution[restarting]`
na přesném simulovaném elapsed (1.863 s místo 0 s), ostatních 320 prošlo.
Test patchuje sdílený stdlib `time.sleep`/`monotonic`, který používá také
subprocess čekání runtime publikace; není to měření skutečného app restartu.
Následné samostatné opakování celého coordinator modulu prošlo **62 PASS**,
ale společný běh timing problém znovu vyvolal. Samostatný stdlib reproduktor
prokázal posun těchto falešných hodin pouze čekáním na subprocess. Restart
test nyní nahrazuje clock objekt pouze v coordinator modulu, nikoli globální
stdlib hodiny; jeho původní assertions i produkční timeout zůstaly stejné.
Závěrečný společný běh po všech opravách: **324 PASS**, 272.89 s. Celý pytest
nebyl spuštěný podle výslovně omezeného testovacího scope M10.

Cílená sada, výhradně nad syntetickými daty (spustit z kořene repozitáře):

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider \
  tests/test_reorganization_worker.py tests/test_reorganization_coordinator.py \
  tests/test_reorganization_browser_flow.py tests/test_reorganization_approval.py \
  tests/test_reorganization_approval_web.py tests/test_v6_execution_manifest.py \
  tests/test_reorganization_web_jobs.py tests/test_reorganization_ux.py \
  tests/test_reorganization_service.py tests/test_reorganization_deployment.py -q --tb=short
```

Teprve po dokončení této sady spustit skutečný lokální Nginx/browser test:

```bash
ANIMEDB_TEST_SERVICE_EDGE=1 PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest \
  -p no:cacheprovider tests/test_reorganization_service_edge.py \
  -k 'manual-protection or cleanup-delete or approval or status' -q --tb=short
```

Závěrečný Docker/Nginx/Chromium výběr po všech opravách: **4 PASS**, 63.28 s;
používá pouze lokální Docker
context `default` a připnutý existující Nginx image, bez pull. Docker network
namespace mounty mohou porušit záměrně přísný syntetický FS fixture. Proto tyto
dvě skupiny nespouštět souběžně. První souběžný pokus takové environmentální
chyby vykázal; následný sériový cílený běh prošel bez změny production guardů.
Sandbox blokoval Unix sockety/TestClient: síťové lokální testy proto běžely
mimo sandbox, bez produkčních cest.

UI bylo ověřeno na 1366×768, 1600×900, 1920×1080, 2560×1440, 820×1180 a
1180×820, v Docker/CDP testu včetně dotyku. Původní engine proběhl nad skutečnými
syntetickými soubory a SQLite; Docker/Compose a snapshot hranice v tomto
E7 integračním fixture jsou simulované. Nginx test je skutečný transport,
nikoli nový plný V2 Docker-worker/CIFS LAB průchod. M8 evidence se tím nepřepisuje.

Další kontroly: `python -m compileall app tests` a coordinator syntax PASS
(pycache mimo repo), načtení všech **36 Jinja templates PASS**, `node --check`
obou změněných JS souborů PASS, Markdown fences/relativní souborové odkazy PASS
a `git diff --check` PASS. Původní untracked soubory a produkční `data/anime.db`
se porovnávají vůči baseline před prací: SHA-256, velikost a mtime beze změny.
Schema ani NAS se neměnily; neproběhl commit nebo push.

Lokální výstupy jsou pod `/tmp/animedb-m10-manual/`: závěrečné logy
`validated.log`, `edge-final.log`, baseline `baseline.json` a šest screenshotů
`screenshots/manual-<width>x<height>.png`. Předání je reprodukovatelné výše
uvedenými příkazy; tyto dočasné artefakty nejsou commitnuté do repozitáře.

Interní nezávislé read-only review nenašlo Critical/Important execution chybu;
oba jeho potvrzené lifecycle/UI nálezy jsou opravené s regresními testy.
Zbývající implementační blocker nebyl zjištěn. Nezávislá Claude revalidace,
produkční instalace/release a provozní schválení zůstávají samostatné budoucí
kroky; tento lokální výsledek jejich provedení ani produkční E7 nedokládá.

## Pokyny pro Claude review

Číst `git diff HEAD` a tento dokument proti zadání M10; nepřebírat PASS bez
kontroly příkazů/výstupů. Zkontrolovat zejména výchozí automatic a staré
RuntimeEvidence, nemožnost přepnout provenance, immutable SHA vazby, čerstvý
mount/DB/FS a zálohu, samostatný snapshot timeout bez obnovy approval, STOP při
chybě zápisu, anti-replay a reset UI mezi challenge. Ruční odpojení konkrétního
Windows sdílení je schválený provozní model, ne požadavek na nový strojový fence.

Neprovádět produkční startup, scan, migraci, restart, snapshot ani E7. Zachovat
14 původních untracked položek. Necommitovat a nepushovat. Zkontrolovat diff
scope, testy a nezávisle reportovat zbývající problémy. Produkční instalace,
aktuální provozní doklady, lidský snapshot a přesné schválení/spuštění jsou
samostatné pozdější kroky s výslovným souhlasem uživatele.

## Nezávislá revalidace Claude (2026-11-15)

Výsledek: **implementační PASS s poznámkami**. Nejde o připravenost k nasazení
na Nexus ani o souhlas s produkčním E7. Commit na výslovný pokyn uživatele
obsahuje přesně 21 souborů M10 včetně níže doplněných regresí.

Ověřeno nad diffem a kódem: `automatic` zůstává výchozí v obou konfiguracích
(chybějící `protection_mode` znamená automatic, neshoda je STOP). E7/E7H engine,
journal, SQLite transakce, rename a integrity checks se nemění; `_runtime_checks`
je při `operator_attestation=None` beze změny. Starší RuntimeEvidence dostane
pouze `null`, nikdy `operator_attested`. Automatický worker ruční evidenci
odmítne. Ruční záznam se přenáší jen v proposal RuntimeEvidence a jeho SHA
váže approval receipt i start request. Ledger soubor
`<request>.operator-attestation.json` žádný kód nečte, není tedy druhou
autoritou. Nginx route `/_service/control/protection` nezávisí na `app`.

Doplněné regrese:

- Coordinator start s cizím nebo chybějícím SHA, jinou provenance nebo
  automatickým režimem s ručním klíčem končí STOP bez E7. Shodné SHA přenese
  evidenci do execute runtime bez hooků.
- Nejistý zápis ledger záznamu končí STOP bez publish a bez retry.
- Worker při neshodě režimu, start/approval SHA nebo aktuální hostitelské
  evidence zastaví před původní E7 admission.

Pět mutací odstraňujících příslušné guardy bylo RED; ve worker případech běh
dosáhl E7 admission. Výsledky: cílená sada M10 324 PASS, nové případy 11 PASS,
celé moduly coordinator a worker 109 PASS, všech 14 Docker/Nginx/Chromium edge
scénářů PASS, compileall, 36 Jinja, `node --check` a `git diff --check` PASS.
Produkční `data/anime.db` a 14 původních untracked položek jsou beze změny.

Poznámky (neblokující):

1. V ručním režimu `inventory_writer_paused_through_verification` odráží jen
   odstavenou lokální aplikaci; externí výluku nese pouze `operator_attested`
   záznam, který každá admission vyžaduje.
2. Root-private claim obsahuje jen request a source. Obsah záznamu je v ledgeru
   a v proposal, tedy ve stejné důvěryhodnostní doméně jako automatický
   snapshot receipt v proposal.
3. Stránka `/reorganization/jobs/<id>` se při odstavení aplikace sama
   neobnoví; formulář zobrazí až ruční obnovení stránky (Nginx servisní
   stránka). Průchod přes tuto URL není v prohlížeči testovaný.
4. Chybové odpovědi `/protection` přebírají texty úklidu a UI neukáže konkrétní
   důvod odmítnutí, například budoucí čas snapshotu; výzva zůstává otevřená.
5. Čas snapshotu se porovnává s hodinami Nexusu; DSM a Nexus musí mít
   synchronizovaný čas.

Zbývající integrační gate: čtyři Docker/browser testy dokazují jen webovou a
Nginx část ruční výzvy; scénář volá `wait_for_protection` přímo, bez workeru a
E7. `browser_flow` dokazuje web → coordinator → původní E7/E7H → NORMAL/STOP v
obou režimech nad syntetickými soubory, ale bez Docker/Compose, `approved_root`,
CIFS a skutečných mount kontrol. Nejmenší doplnění je jeden `manual_one_shot`
průchod existujícím M8 LAB (approved_root V2, Samba, Nginx, Chromium) až do
NORMAL na image z tohoto commitu, volitelně s jedním krátkým timeout STOP.
