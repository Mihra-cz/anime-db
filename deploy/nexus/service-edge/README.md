# HTTP edge V6 (Nginx)

Samotný základní [Compose](../compose.yaml) vystavuje aplikaci přímo na LAN portu
8000 a odstavená aplikace by na něm nemohla odpovídat. Tento override přidává
nejmenší trvale dostupnou HTTP vrstvu: jeden Nginx worker, statické HTML/JS a RO
veřejný JSON. Žádný další Python server, job framework ani monitorovací daemon.

## Stav k 2026-11-09

| Oblast | Stav |
| --- | --- |
| Nginx nasazený a vlastní LAN port 8000 | Ověřeno procesem, naslouchajícím portem a HTTP hlavičkou |
| Proxy na `app`, `/health`, `/_service/app-health`, `service.js` | Ověřeno HTTP |
| ARM64 image | Ověřeno během na Nexusu |
| Nezávislost na restartu `app` | Doloženo: `edge` běží od 8. 11. 21:48, `app` byla naposledy spuštěna ve 22:14 |
| RO bindy `/service` a `/public-status`, restart count 0 | Ověřeno výpisem Docker API |
| Hostitelský adresář bindu `/service` běžícího `edge` | NEOVĚŘENO, výpis obsahoval jen cíl a RW příznak |
| Servisní stránka při nedostupné aplikaci | Plyne z konfigurace, v provozu NEOVĚŘENO |
| Živý `progress.json` a stránka během reálného E7 | NEOVĚŘENO, E7 neproběhlo a adresář stavu je prázdný |
| Publikace průběhu workerem do `/service-status` | NEOVĚŘENO |

## Routing

| Požadavek | Cíl |
| --- | --- |
| Vše mimo `/_service/` | `http://app:8000` přes Docker DNS, `proxy_read_timeout 300s` |
| `/_service/app-health` | `/health` aplikace, timeout 3 s |
| `/_service/service.js` | `/service/service.js` |
| `/_service/progress.json` | `/public-status/progress.json`, bez souboru 404 |
| Ostatní `/_service/*` | 404 |
| Chyba proxy 502/504 | HTTP 503 a `/service/index.html` |

Konfiguraci, stránku a skript čte Nginx z RO bindu
`$SRC/deploy/nexus/service-edge` → `/service`. Provozní stav čte z RO bindu
`$APPDATA_DIR/service` → `/public-status`. Při nedostupné aplikaci zobrazí
servisní přehled, který po 5 sekundách kontroluje stav a dostupnost aplikace.

## Bind mount po přesunu `src/`

Aktualizace přesouvá celý adresář `src/` do `releases/`. **Běžící Nginx dál
používá adresář, který existoval při jeho startu, tedy po přesunu archivovaný
checkout. Jeho bind se na nový `src/` automaticky nepřepne.** Host cesta sice znovu
existuje, ale kontejner drží původní adresář až do své recreace.

`edge` se při aktualizaci aplikace zbytečně nerecreatuje. Archiv používaný běžícím
`edge` se proto nesmí odstranit. Změny v `deploy/nexus/service-edge/` se projeví
až při samostatně vyhodnocené recreaci `edge`; [RELEASE_UPDATE](../docs/RELEASE_UPDATE.md)
je před aktivací vypíše.

## Soubory a provozní hranice

- `compose.edge.yaml`: aplikaci odebere publikovaný port přes Compose `!reset`,
  edge vlastní původní LAN port 8000; backend zůstává `app:8000` uvnitř
  Compose sítě. Vyžaduje Compose s podporou `!reset`.
- `nginx.conf`: dynamické Docker DNS dovoluje start i bez aplikace. Předává
  původní Host včetně portu a nemění Origin; CSRF kontrakt se nemění. Neopakuje
  POST. Vlastní chyby připojení/timeoutu proxy přepne na HTTP 503 servisní
  stránku; aplikační odpovědi včetně 504 se předávají beze změny. Velké odpovědi
  se neodkládají do dočasných souborů; tmpfs zůstává 8 MB. Servisní `no-store`
  platí jen pro statické `/_service/` lokace, cache hlavičky aplikace se zachovají.
  `/_service/app-health` pouze ověřuje `/health`.
- `index.html`, `service.js`: polling JSON/health po 5 sekundách, žádné DB/NAS
  dotazy ani command endpoint. Když backend znovu skutečně odpovídá, prohlížeč
  přejde na původní URL novým GET přes `location.replace`, bez opakování POST.
  Nic nespouští, nerestartuje ani neopakuje.

Edge běží UID/GID 101, bez capabilities, s RO rootfs a malým tmpfs. Má jen RO
bind statických souborů a samostatného veřejného adresáře; žádné SMB credentials,
Docker socket, DB, artwork, private state ani NAS. Pin image je multiarch digest
oficiálního Nginxu.
Viz [Compose merge/reset](https://docs.docker.com/reference/compose-file/merge/),
[Nginx proxy_pass](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_pass)
a [error_page](https://nginx.org/en/docs/http/ngx_http_core_module.html#error_page).

## Původní postup nasazení edge (historický návrh)

Výsledný stav je výše. Základní Compose se nepřepisuje.

1. Ověřit skutečné cesty, Compose podporu `!reset`, ARM64 image, LAN firewall
   a případnou dosud nezdokumentovanou vstupní proxy. Pokud již proxy existuje,
   použít ji pro stejné statické soubory místo přidání druhé.
2. Připravit lokální NVMe `$APPDATA_DIR/service` pouze pro veřejný souhrn, vlastník
   UID/GID workera (obvykle 1000), režim `0755`. Soubor vytvoří worker jako `0644`.
   Nikdy nemountovat jeho zapisovatelný private předek do edge.
3. Ověřit sloučenou konfiguraci bez startu, například:

   ```bash
   docker compose --env-file deploy/nexus/.env.example \
     -f deploy/nexus/compose.yaml -f deploy/nexus/service-edge/compose.edge.yaml config
   ```

   Výsledkem smí být jen port edge na LAN `:8000` → `8080`; app žádný publikovaný
   port nemá. Zdrojové soubory musí být pro UID 101 čitelné.
4. Při schválené změně nejprve řízeně odstavit/recreate app se všemi Compose
   soubory, aby uvolnila port, a spustit edge. Zkontrolovat LAN dostupnost,
   skutečné formuláře Origin/CSRF a chování bez backendu.

## Budoucí použití při reorganizaci E7

Samostatně schválený postup, nikoli povolení k E7:

1. Pro reorganizaci zastavit app a ostatní writery, **edge ponechat běžet**.
   Původní worker se spouští pouze ručně podle [runbooku](../../../docs/V6_WEB_APPROVAL.md#explicitní-hostitelský-start),
   se stejným runtime.lock, RW politikou a UID. Do workeru přidat RW bind pouze
   tohoto veřejného adresáře například na `/service-status` a CLI volbu
   `--service-status-dir /service-status`.
2. Použít `tmux` nebo `screen` a přímo připojené stdin **i stdout**; Python musí
   mít `readline`. **Žádné `| tee` ani přesměrování stdout**. Původní živý E7H
   vypíše přesný seznam a SHA; správce je schvaluje v terminálu.
3. Po skutečném NORMAL a kontrole výsledku správce obnoví původní RO aplikaci.
   Stránka se vrátí sama až podle dostupného HTTP backendu. Při STOP zůstává
   nutný ruční zásah podle původní evidence; edge nikdy neobchází runtime guard.

## Pravdivost a náklady souhrnu

`progress.json` není autoritativní evidence ani povolení k opakování. Neobsahuje
cesty akcí, credentials ani journal. Počty vycházejí z potvrzených E1 operací;
SQLite PASS čeká na dokončení všech akcí. NORMAL čeká na dokončený E7 a report.
Čekání na cleanup je výslovně zpráva pro správce v terminálu.

Heartbeat běží jen ve workeru, přibližně každých 5 s, stejně jako polling stránky.
Jednotlivé syscall/pokusy se nezapisují. JSON se atomicky nahrazuje bez fsync:
je záměrně obnovitelný pohled, nikoli druhý journal. Po ztrátě aktualizací či
stáří přes 16 s se zobrazí „Stav neověřen“ a poslední potvrzený stav. Chybějící
JSON při obyčejném pádu aplikace nehlásí reorganizaci. Staré NORMAL/STOP jsou
označeny jako poslední běh; starý aktivní záznam není živý běh. Čas se porovnává
s HTTP Date stejného hostitele, ne s hodinami uživatelova zařízení.

Lokální test používá oddělenou Docker síť, již stažený image, náhodný loopback
port a Chromium (`ANIMEDB_TEST_SERVICE_EDGE=1`); bez NAS/Nexusu. Náklady
pozorovatele jsou bounded: malý JSON a nejvýše heartbeat 0,2 zápisu/s plus hranice
fází. Test 3095 akcí ověřuje počet zápisů a CPU bez opakování E7 benchmarků.
Výkon na Pi 5 během reálného E7 tím ověřený není.
