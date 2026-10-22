# Cílené review — Claude Opus 5.5

Claude Opus 5.5 provedl nezávislé review dne 2026-10-22 s výsledkem
**PASS WITH MINOR FIXES**. Nenašel chybu aplikačního startup kódu ani SQLite
lifecycle. M1–M4 byly následně upraveny; jejich nezávislé closure review
**dosud neproběhlo**. Předchozí verdikt tyto nové úpravy nepotvrzuje.

## Krátké closure review

Ověřit pouze M1–M4 a provozní doplnění S1–S4:

- M1: README nemá Markdown odkazy na necommitované N5B/N5C drafty; odkazy
  ověřit vůči plánovanému commitu, nikoli všem souborům working tree.
- M2: skutečný instalační tar obsahuje potřebné First Run soubory a explicitně
  vylučuje oba N5B/N5C Python moduly i oba draft dokumenty, bez lokálních příměsí.
- M3: každý ze tří historických N4 dokumentů získal pouze jeden koncový LF bajt.
- M4: rozlišuje se původní verdikt a zatím neprovedené ověření nových oprav.
- S1–S4: surové mountinfo pro root a `/srv/nexus`, LAN forward pravidlo pouze
  pro port 8000 a `192.168.11.0/24` trvalé přes reboot, LAN smoke až po firewall
  gate a soulad instalačních příkazů s nakonfigurovaným UID/GID `1000:1000`.

Scope budoucího First Run commitu je 15 změněných tracked souborů a přesně
čtyři nové: `deploy/nexus/first_run.py`, `deploy/nexus/docs/FIRST_RUN.md`,
`deploy/nexus/docs/FIRST_RUN_REVIEW.md`, `tests/test_nexus_first_run.py`.
Šest N5B/N5C untracked souborů se nezahrnuje. Samotný `git diff` nové soubory
nezobrazuje. Kvůli kontrole nestagovat a neměnit větev ani historii.

Předchozí cílené testy: **125 PASS**. Aplikační kód ani testovací kontrakt se
při minor opravách nemění; znovu ověřit pouze Compose config, TOML, odkazy
v plánovaném obsahu commitu, shell syntax, skutečný `tar -tzf`, whitespace,
EOF a `git diff --check`. Krátké closure review ani tyto statické kontroly
nepotvrzují ARM64 běh, firewall, NAS či deploy.

## Původní rozsah review (kontext)

Ověř minimal single-instance Nexus First Run podle AGENTS.md a tohoto
schváleného cíle: jedna služba `app`, jeden pracovní SQLite v9 soubor, artwork,
jeden backup root s `daily/` a `pre-change/`, LAN adresa
`192.168.11.238:8000:8000`. Žádné provozní rozdělení do srovnávacích instancí,
profily, další port nebo snapshot mount ve webu. Zachovat Cookbook v1.0,
non-root ARM64 image, RO NAS, read-only root FS a ostatní Docker izolaci.

Čti zejména:

- `deploy/nexus/compose.yaml`, `.env.example`, `manifest.toml`, `README.md`;
- `deploy/nexus/first_run.py`, dosavadní `preflight.py`, `Dockerfile`, `.dockerignore`;
- `app/database.py`, existující `app/migrations.py` a startup v `app/main.py`;
- `deploy/nexus/docs/ARCHITECTURE.md`, `OPERATIONS.md`, `FIRST_RUN.md`, `CHANGELOG.md`;
- `tests/test_nexus_first_run.py`, `tests/test_nexus_preflight.py`, relevantní DB/startup regrese;
- kořenový `README.md` a označení historických N4 dokumentů.

## Konkrétní kontrolní otázky

1. Je v rozbaleném Compose právě jedna služba bez profilů a druhé explicitní
   sítě, s přesným jediným LAN bindem, RO NAS a předem existujícími bindy?
2. Jsou datové cesty, manifest, `.env`, install/start/stop příkazy a dokumentace
   shodné? Nezávisí žádný aktivní krok na starém helperu či porovnávacím profilu?
3. Vytváří provision pouze jednorázovou trvalou evidenci? Je durable intent
   zapsán před tvorbou DB a vytvoří existující migrace skutečně prázdné v9?
4. Znamená ztracená DB, chybějící evidence nebo přerušený bootstrap STOP bez
   nového souboru/retry, včetně ztráty před connectem a pozdějšího pool spojení?
5. Drží jediný Uvicorn proces runtime flock i po exec? Zůstává stabilní restart
   bez semantic writes a bez scanu či metadata enrichmentu?
6. Je LAN forward firewall gate výslovně neověřený a blokující LAN start?
   Nenahrazuje se důkaz firewallu privátní IP nebo pouhým INPUT/UFW pravidlem?
7. Zůstávají snapshot časy 02:00 / kolem 03:30 plánem a staré helpery neaktivní?
   Není lokální test zaměňovaný za ARM64 runtime, deployment nebo restore důkaz?

Read-only scope: **žádný Nexus/NAS, produkční `data/anime.db`, Docker start,
scan, Linux/firewall změna, commit, push ani deploy**. N5B/N5C rozpracované
soubory ani historické helpery neměnit. Nevyžaduj nový backup framework,
pokročilý N5C coordinator, V6 executor, P2C ani Completeness.

## Předchozí funkční validace (neopakovat pro minor dokumentační opravy)

```bash
docker compose --env-file deploy/nexus/.env.example -f deploy/nexus/compose.yaml config -q
docker compose --env-file deploy/nexus/.env.example -f deploy/nexus/compose.yaml config --services
PYTHONDONTWRITEBYTECODE=1 timeout -k 5s 180s .venv/bin/pytest -p no:cacheprovider \
  tests/test_nexus_first_run.py tests/test_nexus_preflight.py tests/test_database.py \
  tests/test_migrations.py::test_stable_application_startup_skips_compatibility_rebuild_and_writes -q
PYTHONPYCACHEPREFIX=/tmp/animedb-first-run-pycache .venv/bin/python -m compileall -q app tests deploy/nexus/first_run.py
git diff --check
git status --short --branch
```

Testy používají dočasné DB a syntetické mount tabulky. TestClient se v tomto
prostředí v omezeném sandboxu zasekává; cílené testy byly úspěšně spuštěné mimo
sandbox, stále bez produkčních dat nebo vzdáleného přístupu. Full pytest se
v souladu se zadáním nepouští. Výsledek 125 PASS patří k původnímu review;
výsledky aktuálních statických kontrol jsou v předávacím reportu.

Výstup: Critical / Important / Minor s konkrétním souborem a řádkem, důvodem
a minimální opravou. Zvlášť vypiš neověřené host gates a věci odložené mimo
scope. Verdikt zní „lokální příprava připravena/nepřipravena k dalšímu kroku“;
review samo neopravňuje k prvnímu spuštění.
