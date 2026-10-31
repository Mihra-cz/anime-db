# V6 – diagnostika konkrétního SMB/CIFS mountu

Samostatný nástroj `app.tools.smb_probe` používá existující Linuxový helper
`renameat2(RENAME_NOREPLACE)`. Nevolá executor, scanner, SQLite ani execution
journal a nemění produkční mount allowlist. Výsledek není execution authority.
Skutečné spuštění na SMB vyžaduje samostatný souhlas uživatele; příprava nástroje
ani úspěšný lokální test tento souhlas nenahrazují.

## Pracovní adresář

- Existující, zcela prázdný adresář na již připojeném testovaném SMB mountu.
  Nástroj mount nevytváří. Kontrola prázdnosti zahrnuje skryté položky.
- Absolutní cesta bez `..` a bez symlinků v kterékoli komponentě. Adresář musí
  vlastnit UID spouštějícího uživatele a nesmí být zapisovatelný skupinou ani
  ostatními; doporučený režim je `0700`. CIFS musí tyto údaje prezentovat tak,
  aby kontrola prošla.
- Samostatný testovací prostor mimo anime knihovnu; nesmí být jejím aliasem.
  `/mnt/nas-anime` a všechny jeho podadresáře nástroj odmítá před otevřením.
  Ještě před otevřením pracovní cesty porovnává viditelnou mountinfo identitu
  s produkčním mountem. Shodný `major:minor` odmítá konzervativně i při různých
  zdrojích či filesystem roots; vysvětlení je v `mount_comparison.reason`.
  Stejný UNC share a jeho podcesty odmítá také při rozdílném device. Cesty a
  UNC komponenty porovnává po komponentách, nikoli textovým prefixem.
- Samostatný share na stejném serveru lze použít, pokud má odlišnou UNC share
  komponentu i device. Stejný device není dovoleno obejít tvrzením jiného zdroje.
  Pro SMB musí být produkční mount identifikovatelný z mountinfo ve stejném
  mount namespace. Chybějící reference, nejasný zdroj, nejednoznačné překryté
  mounty či možné alternativní jméno serveru stejného share znamenají odmítnutí
  bez zápisů. Produkční cestu nástroj kvůli porovnání neotevírá ani nestatuje.
  Známý lokální ext4/btrfs/overlay probe lze provést i bez produkční reference;
  porovnání mountů pak zůstává NEOVĚŘENO.
- Výhradní kontrola uživatelem po celou dobu testu, bez souběžných writerů
  nebo změn mountu/cesty. Diagnostika neposkytuje globální writer exclusion.

Nástroj vytvoří `source/` a `target/`, vlastní 64KiB payload a malý kolizní
soubor. Provede rename uvnitř `source/`, pokus o rename na existující soubor v
`target/` a následný MOVE do `target/moved.bin`. Ověří obsah obou souborů a
zaznamená stat údaje původního otevřeného souboru i nově otevřených cest.
`fsync` používá také read-only file descriptor jako existující V6 primitive;
po MOVE volá file → target parent → source parent.

Nic automaticky nemaže, ani při selhání. Po úspěchu zůstanou oba podadresáře,
`target/moved.bin` a `target/collision.bin`. Opakovaný běh ve stejném adresáři
odmítne. Další pokus vyžaduje nový prázdný pracovní adresář.

## Jediný příkaz pro pozdější schválené SMB spuštění

Až operátor samostatně schválí test a připraví uvedený prázdný adresář na
konkrétním SMB mountu, spustí z kořene repozitáře:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m app.tools.smb_probe --work-dir /mnt/synology-v6-probe/empty-work
```

Tento příkaz není součástí nynějšího lokálního ověření. Nevytváří adresář,
nepřipojuje share a neuděluje oprávnění k produkční reorganizaci.

## Interpretace JSON reportu

Každá kontrola má `PASS`, `FAIL` nebo `NEOVĚŘENO`. Report se vypisuje na stdout;
uložte jej případně mimo pracovní adresář, aby zůstal před spuštěním prázdný.

| Výsledek | Význam |
|---|---|
| PASS | Konkrétní syscall/kontrola prošla při tomto běhu a nastavení mountu. |
| FAIL | Volání selhalo nebo bylo porušeno očekávání. `errno`, jeho symbolický název a zpráva zachovávají konkrétní chybu. Odmítnutí přepsání je PASS pouze při `EEXIST` a obsah obou souborů se kontroluje samostatně. |
| NEOVĚŘENO | Kontrola nebyla dosažena po předchozí chybě, nebo ji tento test nedokáže provést. Není to PASS. |

`kernel_release` uvádí `os.uname().release`. `mount` a `production_mount`
obsahují ID, device, filesystem type, source, filesystem root, mount point,
`mount_options`, `superblock_options` a `mountinfo_record`. Produkční záznam je
`null`, pokud není v mountinfo viditelný vlastní mount `/mnt/nas-anime` ani
nadřazený SMB mount nebo záznam umožňující identifikovat bind alias projekcí
filesystem root. `mount_comparison.comparable`, `status` a `reason` výslovně
říkají, zda bylo možné identity porovnat a jak dopadla kontrola. Tento kontext
je oddělený od souhrnných syscall kontrol v `checks`.

Mountinfo záznam je původní záznam s redakcí citlivých částí, nikoli nezkrácený
dump: přihlašovací údaje v source, citlivé a neznámé hodnoty mount/superblock
options jsou `[REDACTED]`. Zachované jsou pouze známé veřejné volby, například
`vers`, `cache`, UID/GID a režimy. Pole `redacted` označuje upravený záznam.
Report stále obsahuje cesty a názvy serverů/share. Shodné mount options samy
neprokazují shodné serverové ACL, SMB nastavení ani durability.

Úspěch rename syscallu a zmizení SOURCE jsou samostatné kontroly
`source_absent_after_same_rename` a `source_absent_after_cross_rename`.
Pokud SOURCE zůstane, výsledek je FAIL s důvodem
`source name still present after rename`, bez syntetického errno 17.
Skutečná kolize TARGET nadále používá `EEXIST`.

`observations` obsahuje mount ID/typ/zdroj, SHA-256 a `st_dev`, `st_ino`, mode,
link count, UID/GID, velikost a nanosekundové atime/mtime/ctime. Identity kontrola
vyžaduje kladný stabilní inode a zachování ostatních stat polí kromě atime a
ctime: ty mohou legitimně změnit read/rename a jsou pouze zaznamenané.
Změna identity znamená FAIL vůči očekávání současného V6 executoru, nikoli sama
produkční závadu filesystemu. Neúspěšný `fsync` se hlásí jako FAIL a další
nezávislé syscall kontroly pokračují; neúspěšný rename další kroky zastaví.

Exit code `0` znamená PASS všech proveditelných kontrol, `1` chybu či nedokončenou
kontrolu po přijetí pracovního adresáře a `2` odmítnutí adresáře/argumentů.
`power_loss_durability` je vždy NEOVĚŘENO a je z podmínky exit code `0` vyňato.

## Co test neprokazuje

Úspěšný `fsync` je pouze potvrzení volání, nikoli důkaz trvalého uložení při
výpadku napájení. Nedestruktivní test neprokáže power-loss durability NASu.
Znovuotevření cest probíhá v jednom procesu se stejným klientem a cache;
neprokazuje chování po restartu, remountu, výpadku sítě ani z jiného klienta.
Test neověřuje souběžné writery, crash recovery, výkon velké knihovny,
SQLite/NAS workflow ani bezpečnost produkčního executoru. Mountinfo neodhalí
serverové share/DFS aliasy mířící pod různými názvy na stejná data, skryté mounty
nebo jiný mount namespace. Identifikované aliasy se odmítají. Samotnou existenci produkčního adresáře
test neověřuje. Bind alias jeho podcesty ale projekcí filesystem root odmítne.
Ani samé PASS
nepovolují CIFS do produkčního allowlistu.
