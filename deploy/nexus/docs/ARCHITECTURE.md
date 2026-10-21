# Architektura plánovaného A/B provozu

Jeden image s Pythonem 3.12, FFmpeg/FFprobe a dependencies z `pyproject.toml`
obsluhuje dva nezávislé procesy Uvicornu. Nexus Compose cílí na `linux/arm64`;
Dockerfile umožňuje také nativní build pro lokální vývojovou platformu.
MediaInfo není součástí scanner pipeline ani povinnou závislostí image.

| Instance | Profil / služba | Přístup z hostitele |
| --- | --- | --- |
| A: pracovní kopie současné DB | `a` / `app-a` | `127.0.0.1:8000` |
| B: samostatně inicializovaná čistá DB | `b` / `app-b` | `127.0.0.1:8001` |

Porty lze změnit v instalačním `.env`; aktualizovat podle nich i manifest.
Bez zvoleného profilu nebo explicitní služby neběží žádná instance.
Explicitní volba služby v Compose aktivuje i její profil, takže profil sám
není bezpečnostní zámek. Obě služby používají samostatnou bridge síť.

| Hostitelská cesta | Kontejner | Přístup |
| --- | --- | --- |
| `/mnt/nas-anime` | `/media/anime` v A i B | read-only CIFS |
| `/srv/nexus/appdata/anime-db/A/db/` | `/data` pouze v A | read-write |
| `/srv/nexus/appdata/anime-db/A/artwork/` | `/artwork` pouze v A | read-write |
| `/srv/nexus/appdata/anime-db/B/db/` | `/data` pouze v B | read-write |
| `/srv/nexus/appdata/anime-db/B/artwork/` | `/artwork` pouze v B | read-write |
| `/srv/nexus/appdata/anime-db/reference/<UTC>/anime.db` | žádný mount | archiv mimo dosah aplikace |
| `/srv/nexus/appdata/anime-db/backups/{A,B}/` | žádný mount | konzistentní snapshoty mimo aplikaci |
| `/srv/nexus/appdata/anime-db/backups/.partial/` | žádný mount | nepublikované generace, budoucí Restic exclude |
| `/mnt/nas-backup` | žádný mount | existující zálohovací infrastruktura |

Obě pracovní DB mají uvnitř vlastní instance cestu `/data/anime.db`.
Není připojen společný appdata rodič, kód hostitele, originální DB, reference
ani Docker socket. Všechny bindy mají `create_host_path: false`.

Runtime používá UID/GID `1000:1000` podle N1, případně explicitně upravené
hodnoty z instalační šablony. Image root filesystem je read-only;
zapisovat lze do vlastní DB, artworku a dočasného `/tmp` v RAM. Artwork/cache
je persistentní i při vypnutém stahování. Logy zapisuje Docker driver `local`
odděleně podle kontejneru, s rotací 3 × 10 MB; nejsou součástí NAS anime.

Build context je vždy `/srv/nexus/projects/anime-db/src`, nezávisle na cestě
Compose. `.dockerignore` propouští pouze balíček `app`, jeho build metadata,
README, licenci a guard. Runtime data, DB i sidecary, cache, `.env`, zálohy
a lokální pracovní soubory jsou z contextu vyloučené.

Dockerfile neurčuje konkrétní digest a Python dependencies používají současné
verzové rozsahy projektu. Úspěch budoucího ARM64 buildu a runtime zůstává
**UNVERIFIED**; lokální prostředí není jeho náhradou.

N4A přidává pouze samostatný standard-library SQLite helper; jeho
[kontrakt](SQLITE_BACKUP.md) vyžaduje explicitní očekávané instance, proto
počáteční A-only provoz nevyžaduje B. Worker s procesovým timeoutem vytvoří
ověřenou samostatnou kopii a atomicky publikuje celou generaci bez overwrite.
Helper není v Compose ani image a na serveru není instalovaný. Neexistuje
napojení do Resticu ani automatická lokální retence.
