# Změny instalačních podkladů

## 2026-10-21 — N4B2, lokální CIFS kompatibilita a write hranice

- Web preflight přijímá RO VFS nad RO i RW CIFS superblockem, ověřuje přesný
  NAS source/root a odmítá RW aliasy, nested mounts a neověřitelnou tabulku.
- Doplněny izolované regrese a daemon-free kontrola rozbaleného Compose:
  library bind A/B zůstává trvale RO, bez dalších host bindů.
- Popsány tři fáze účtu/host mountu, nezávislý RW NAS Backup a oddělené
  konkrétně autorizované V6/V7 write joby; produkční executor ani import
  nevznikly. Host RW nepředává write authority webu.
- N4A/N4B1 kód zůstal beze změny. Návrh namespace SQLite copieru řeší
  Landlock ENOSYS bez změny kernelu, ale není implementovaný ani prokázaný.
  Projekt zůstává `planned / not-deployed`, bez zásahu do Nexusu/produkce.

## 2026-10-21 — N4B1, lokální Restic integrační adaptér

- Připraven úzký obal existujícího Restic příkazu s explicitní A-only/A+B
  konfigurací, kontrolou N4A JSON a bundle, výjimkami živých DB a `.partial`.
- Chyba AnimeDB zachová neúspěch a umožní původní systémový/InfluxDB backup;
  nejisté publikace jsou vyloučené. Restic nenulový kód včetně 3 je neúspěch.
- Přesné ID skutečného běhu, repository ID, listing a vzdálené bytes všech
  souborů jsou nutné pro lokální content doklad. Bez automatické retence.
- Izolované regrese používají dočasné SQLite DB a simulovaný Restic. Popsán
  minimální instalační patch, zachování lock/OnFailure a neověřené ARM64/FS
  podmínky. Stav `planned / not-deployed`, bez změn Nexusu nebo skutečné obnovy.

## 2026-10-21 — N4A, izolovaný lokální SQLite helper

- Přidán standard-library helper s explicitními očekávanými instancemi A/B,
  read-only Online Backup API, procesovým timeoutem, kontrolou skutečného
  schema kontraktu, integrity, FK a samostatnosti cílové kopie.
- Snapshot generace obsahuje DB, validační manifest a checksumy; publikace
  používá fsync a atomický no-overwrite rename. Chyby a přerušení mají oddělené
  strojové výsledky a případná vlastní generace se stáhne do `.partial`.
- Doplněny izolované regresní testy včetně WAL, souběžných zápisů, práv, aliasů,
  timeoutu, kolize a chyb publikace. Testy používají dočasné DB a filesystem.
- Interní review odhalilo TOCTOU riziko SQLite sidecarů, chybějící mount-ID
  kontrolu souborů a přerušení při zakládání supervisoru. Doplněny regrese,
  čerstvý confined SQLite proces s Landlock ABI >= 3 a ochrana FD/signal lifecycle.
- Projekt zůstává `planned / not-deployed`; bez instalace na Nexus, změn
  Resticu/systemd/Dockeru, skutečného backupu/restore, retence nebo N4B.
  ARM64 runtime ani vzdálená obnovitelnost nejsou ověřené.

## 2026-10-20 — N2, pouze lokální příprava

- Připraven Nexus Compose s izolovanými pracovními DB A/B, persistentním
  artworkem, localhost porty, read-only NAS a kontrolou před Uvicornem.
- Připraven manifest podle skutečné šablony Cookbook v1.0 a návod umístění
  souborů, přípravy dat, validace a bezpečného zastavení při chybě.
- Stav je `planned / not-deployed`. Na Nexusu se nic neinstalovalo;
  DB, NAS, služby ani zálohování se neměnily. ARM64 build/runtime a obnova
  zůstávají neověřené.
