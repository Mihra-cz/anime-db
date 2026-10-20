# Instalační podmínky a provoz

## Rozsah N2

Pouze lokální podklady a statická validace. Žádný build, pull, spuštění,
kopírování DB, inicializace B, migrace, scanner ani změna Nexusu/NAS.
Následující kroky popisují budoucí instalaci po samostatném schválení.

## Příprava instalace

1. Znovu přečíst skutečný Cookbook a registr. Ověřit volné projektové cesty,
   Docker oprávnění správce, ARM64 a dostupnost zamýšlených localhost portů.
2. Umístit zdrojový repozitář do `/srv/nexus/projects/anime-db/src/` a ověřit
   schválenou revizi; základ N2 je `4ae510ce94aa2e7922fc8b449ae460224518d165`.
   Instalační revize musí obsahovat také zkontrolované změny N2. Nezahrnovat
   lokální runtime data ani originální DB z notebooku.
3. Umístit podklady podle tabulky v projektovém README a doplnit položku
   `/srv/nexus/PROJECTS.md`. Manifest ponechat `planned / not-deployed`.
4. Připravit pouze potřebné adresáře z architektury. DB i její rodič potřebují
   zápis kvůli SQLite journal/WAL; artwork potřebuje zápis kvůli importnímu
   `mkdir` a pozdějšímu cache workflow. Ověřit vlastníka a přístup pro runtime
   UID/GID; nepoužívat plošné `chmod 777` ani rekurzivní změny práv jiných dat.
5. Připravit pracovní A a referenci samostatným schváleným DB krokem. B zatím
   nepřipravovat ani nespouštět; čisté schema dostane až při samostatně
   schválené inicializaci. Prázdný soubor není připravená DB. Spolu s referenční
   DB inventarizovat existující artwork na notebooku podle pravidel níže.
6. Před prvním startup zápisem ověřit aktuální Restic výjimky a způsob obnovy.
   Celý `/srv/nexus` je podle Cookbooku zálohovaný, ale prostá záloha živých
   SQLite není zárukou konzistence. Živé A/B a sidecary se mají vyloučit a
   nahrazovat konzistentními snapshoty.

Instalační `.env` obsahuje pouze UID/GID a localhost porty. Aplikační cesty,
read-only NAS a `REQUIRE_MOUNT=true` jsou explicitní v Compose. Pro první
ověření jsou AniList, metadata download, vzdálené obaly a auto-confirm vypnuté.
Šablona neobsahuje tajemství a `.env` nevstupuje do image.

## Read-only ověření mountu a konfigurace

Na hostiteli musí být skutečný zdroj `//192.168.11.149/Anime` připojený jako
CIFS read-only. Samotné `autofs`, existence adresáře, `os.path.ismount` ani
`REQUIRE_MOUNT=true` skutečný CIFS nedokazují. Nepokračovat při chybějícím
CIFS, nesprávném zdroji nebo chybějícím `ro`:

```bash
findmnt -n -M /mnt/nas-anime -t cifs -o TARGET,SOURCE,FSTYPE,VFS-OPTIONS,FS-OPTIONS
docker compose --env-file /srv/nexus/stacks/anime-db/.env \
  -f /srv/nexus/stacks/anime-db/compose.yaml --profile a --profile b config -q
```

Tyto příkazy neprovádějí scan NAS ani nespojují aplikaci s DB.
`ro` musí být v **obou** sloupcích `VFS-OPTIONS` i `FS-OPTIONS`; guard kontroluje
oba údaje, nestačí read-only bind nad zapisovatelným podkladovým filesystemem.
`config -q` neověřuje existenci bindů, oprávnění, image ani dostupnost NAS.
Automount se tímto návodem nekonfiguruje ani ručně neaktivuje.

## Read-only kontrola zdrojových host cest

Před budoucím startem porovnat rozbalený Compose s aktuálním Cookbookem:
build context musí být `/srv/nexus/projects/anime-db/src/`, NAS bind
`/mnt/nas-anime` a pracovní bindy přesně čtyři adresáře A/B uvedené
v [architektuře](ARCHITECTURE.md). Nestačí podobný název nebo správný prefix.

Pouze čtením ověřit každý komponent zdrojových cest, včetně jejich rodičů:

```bash
namei -l -- /srv/nexus/projects/anime-db/src /mnt/nas-anime \
  /srv/nexus/appdata/anime-db/A/db /srv/nexus/appdata/anime-db/A/artwork \
  /srv/nexus/appdata/anime-db/B/db /srv/nexus/appdata/anime-db/B/artwork
stat -Lc '%F %d:%i %n' -- \
  /srv/nexus/appdata/anime-db/A/db /srv/nexus/appdata/anime-db/A/artwork \
  /srv/nexus/appdata/anime-db/B/db /srv/nexus/appdata/anime-db/B/artwork
```

Žádný komponent nesmí být symlink. Všechny čtyři pracovní cesty musí být
adresáře s navzájem různými dvojicemi device/inode (`%d:%i`); shodné dvojice
odhalí také alias přes bind mount, který samotná kontrola symlinků neodhalí.
DB ani artwork A/B nesmějí sdílet týž adresář. Nezávislost samotných DB souborů
navíc vyžaduje oddělené soubory bez symlinků a hardlinků, jak kontroluje guard.

Chybějící cesta, nedostatečné oprávnění, symlink, alias nebo nesoulad s Cookbookem
znamená STOP. Tyto kontroly nic nevytvářejí ani neopravují; adresáře v tomto
kroku nezakládat, nepřesměrovávat, nepřesouvat ani neměnit jejich oprávnění.

## Kontrola před Uvicornem

Nexus Compose používá `preflight.py` jako povinný `entrypoint`; `command`
obsahuje původní Uvicorn a jeho argumenty. Běžné `docker compose run` s jiným
command proto stále projde guardem. Po úspěšné kontrole guard předá celé argv
do `os.execvp` bez shellu. Explicitní administrátorské `--entrypoint` je mimo
tuto ochranu; další ochranný framework se nezavádí.
Guard neimportuje aplikaci a neotvírá SQLite connection. Čte mount tabulku
a hlavičku už připraveného pracovního souboru. Odmítne:

- jinou než předepsanou DB/artwork/library konfiguraci nebo root UID/GID;
- chybějící či nejednoznačný mount, autofs nebo lokální náhradní filesystem;
- CIFS bez `ro` v kontejnerovém mountu i podkladovém filesystemu;
- chybějící DB, neplatnou hlavičku, schema marker 0 nebo symlink/hardlink DB;
- nedostatečná práva pracovní DB, jejího rodiče nebo artwork adresáře.

Kontrola hlavičky není integrity check ani důkaz úplnosti schema. Guard
nevytváří DB, adresáře ani sidecary; jakákoli chyba znamená STOP před importem
aplikace. `restart: "no"` zabrání automatickému opakování chyby.
Po pozdějším výpadku NAS působí existující scanner safety/timeout lifecycle;
guard není průběžný dohled nad mountem.

## DB a první start

Originální DB nikdy neotevírat aplikací. Při samostatně schválené přípravě DB
použít SQLite Online Backup API s read-only zdrojem, evidencí WAL/SHM/journalu
a bez checkpointu či recovery zápisu do originálu. Kopii ověřit pomocí
`PRAGMA integrity_check` a `PRAGMA foreign_key_check`, zaznamenat SHA-256,
velikost, čas a původ. Referenci chránit proti zápisu aplikačním účtem;
pracovní A musí být nezávislý soubor, nikoli link. B vzniká bez kopie A.

Spolu s referenční DB při schválené přípravě inventarizovat existující artwork
na notebooku: doložit skutečnou konfiguraci `METADATA_ARTWORK_DIRECTORY`,
pracovní adresář pro relativní cesty, umístění existujících cache a platnost
DB odkazů na originály a thumbnails. Nepředpokládat jedinou zdrojovou cestu
ani automaticky použít výchozí `data/artwork`. Evidovat původ, relativní cesty,
velikosti a kontrolní součty; chybějící nebo nejednoznačné soubory vyjasnit
před přenosem.

Při pozdějším samostatně schváleném přenosu znovu ověřit skutečné zdrojové
umístění a zkopírovat ověřený artwork do
`/srv/nexus/appdata/anime-db/A/artwork/`, se zachováním relativních cest
očekávaných DB. B začne s vlastním prázdným
`/srv/nexus/appdata/anime-db/B/artwork/`, bez převzetí cache A. N2 artwork
neinventarizuje na produkci ani nic nekopíruje.

První start se schvaluje zvlášť a volí konkrétní službu. B je zablokovaná
chybějící/neinicializovanou pracovní DB i při explicitním výběru `app-b`.
Bez nové autorizace neobcházet guard přes `--entrypoint` a nespouštět oba profily.
Aktivace profilu ani úspěšný guard neopravňuje ke scanneru.

`create_app` může vytvořit adresáře, lifespan spouští migrace a `create_all`.
Na aktuálním schema markeru 9 migrační rekonstrukci přeskočí, stále ale může
doplnit chybějící tabulky. Pracovní A/B jsou záměrně zapisovatelné; žádný
aplikační přepínač zde neslibuje read-only web. Žádný scanner při startupu
neběží, ale explicitní `POST /scan` zůstává dostupný. `/health` vrací pouze
aplikační `ok`, nikoli důkaz zdraví DB nebo NAS.

## Ověření a návratový postup

Po budoucím schváleném nasazení ověřit skutečné mounty kontejnerů bez výpisu
celého prostředí: NAS musí mít `RW=false`, DB/artwork patřit pouze vlastní
instanci; reference, originál ani společný appdata rodič nesmějí být připojené.
Ověřit UID/GID, localhost porty, logy a funkčnost po kontrolovaném restartu.
ARM64 build/runtime, obnovu a Restic snapshot doložit před změnou manifestu
na `deployed`; údaje neodhadovat ze staticky validního Compose.

V N2 není co vracet na serveru. Při budoucí chybě prvního startu zastavit pouze
dotčenou AnimeDB instanci a zachovat její pracovní DB i logy pro diagnostiku.
Originál a reference zůstávají stranou. Nepřepisovat pracovní ani originální
DB starou zálohou a nepoužívat mazání volumes jako automatický rollback.
