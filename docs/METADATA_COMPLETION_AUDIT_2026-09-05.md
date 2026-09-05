# Read-only audit metadata completion — 5. září 2026

Zdroj: produkční `data/anime.db`, SQLite `mode=ro` + `query_only=ON`, jeden konzistentní read snapshot. Bez aplikačního startupu, migrace nebo čtení NAS.

Relevantní title má alespoň jedno evidované Video. Prázdné titles jsou vynechány; model nemá soft-delete/active flag. Confirmed vyžaduje `linked_manual` a primární ruční ExternalTitleLink s neprázdným `verified_at`.

| Metrika | Počet |
|---|---:|
| Relevantní CatalogTitle | 280 |
| Prázdné CatalogTitle mimo aggregate | 0 |
| Confirmed metadata | 139 |
| Automatic not_required (včetně případně confirmed) | 16 |
| Resolved pouze not_required | 16 |
| Required + missing | 125 |
| Collections Metadata OK | 88 |
| Collections Metadata chybí | 76 |

Relevantní title bez existující collection: 0.
Potvrzená vazba bez odpovídajícího normalizovaného metadata recordu: 0.

Výchozí fingerprint produkce:
`size=4624384`, `mtime=2026-09-05 00:29:55.501568868 +0200`,
SHA-256 `c0dde3fba0e8d3a50a9644ddc7b5e969b1731d0bd12b66778cdaeb075b1031cd`.

## Všechny automatic not_required části

| Title ID | Collection | Lokální title | Raw file_type mix | Requirement typy |
|---:|---|---|---|---|
| 62 | High School DxD (Z12-J18) | NC – High School DxD | ed=1, op=1 | ed=1, op=1 |
| 64 | High School DxD (Z12-J18) | NC – High School DxD Hero | ed=1, op=2 | ed=1, op=2 |
| 65 | High School DxD (Z12-J18) | NC – High School DxD New | ed=2, op=2 | ed=2, op=2 |
| 153 | OVERLORD (L15-L22) | NC | nced=1, ncop=1 | nced=1, ncop=1 |
| 155 | OVERLORD (L15-L22) | NC | nced=1, ncop=1 | nced=1, ncop=1 |
| 157 | OVERLORD (L15-L22) | NC | nced=1, ncop=1 | nced=1, ncop=1 |
| 233 | High School DxD (Z12-J18) | NC – High School DxD Born | ed=1, op=2 | ed=1, op=2 |
| 239 | Kore wa Zombie Desuka (Z11-J12) | NC - Kore wa Zombie Desuka | nced=1, ncop=1 | nced=1, ncop=1 |
| 240 | Kore wa Zombie Desuka (Z11-J12) | NC - Kore wa Zombie Desuka 2 | op=2 | op=2 |
| 271 | Tensei Shitara Slime Datta Ken (P18-L21) | NC - Tensei Shitara Slime Datta Ken | nced=3, ncop=2 | nced=3, ncop=2 |
| 272 | Tensei Shitara Slime Datta Ken (P18-L21) | NC - Tensei Shitara Slime Datta Ken | nced=1, ncop=1 | nced=1, ncop=1 |
| 273 | Bikini Warriors (L15) | NC - Bikini Warriors | nced=1 | nced=1 |
| 276 | Isekai Maou to Shoukan Shoujo no Dorei Majutsu (L18-J21) | NC - Isekai Maou to Shoukan Shoujo no Dorei Majutsu | nced=5, ncop=3 | nced=5, ncop=3 |
| 278 | Isekai Maou to Shoukan Shoujo no Dorei Majutsu (L18-J21) | Menu - Isekai Maou to Shoukan Shoujo no Dorei Majutsu | menu=3 | menu=3 |
| 285 | Nande Koko ni Sensei ga (J19) | NC - Nande Koko ni Sensei ga | nced=5, ncop=8 | nced=5, ncop=8 |
| 294 | Getsuyoubi no Tawawa P16-P21 | NC - Getsuyoubi no Tawawa | nced=1 | nced=1 |

Automatic not_required titles s raw nebo requirement typem mimo přesnou safe množinu: **žádné**.

## Collections vhodné pro ruční smoke

Pro krátký cílený průchod:

- `/collections/1`: dvě potvrzené Season části, čisté Metadata OK.
- `/collections/48`, `/titles/299`: High School DxD, jediná required/missing část je HERO Special. Ruční not_required má vyřešit jen metadata workflow collection.
- `/titles/60`: HERO parent nadále upozorňuje na rozdíl provider 13 / lokálně 12, i po not_required na Specialu 299.
- `/titles/64` nebo `/titles/273`: čisté OP/ED či NCED, automatic not_required; manual required má vrátit část do fronty, Automaticky výjimku obnovit.
- `/collections/56`, `/titles/279`: Isekai Maou Interview, raw `other=2`; zůstává required do explicitního ručního rozhodnutí. Ve stejné collection jsou NC 276, Menu 278 a required CM+PV Trailer 277.
- `/collections/86`, `/titles/114`: Kobayashi/Mini Dra, více částí a confirmed Bonus s vlastní metadata identitou.

Další ověřené multi-part příklady:

- Collection 1: 100-man no Inochi no Ue ni Ore wa Tatteiru (P20-L21) — Metadata OK; 1: Season 1 (P20); 2: Season 2 (L21)
- Collection 3: Ansatsu Kyoushitsu (Z15-Z16) — Metadata OK; 4: Serie 1 (Z15); 5: Serie 2 (Z16); 245: Preview - Ansatsu Kyoushitsu
- Collection 5: Arifureta Shokugyou de Sekai Saikyou (L19-Z22) — Metadata OK; 8: Season 1 (L19); 9: Season 2 (Z22); 287: Arifureta Shokugyou de Sekai Saikyou; 288: Special - Arifureta Shokugyou de Sekai Saikyou; 289: OVA - Arifureta Shokugyou de Sekai Saikyou; 290: Specials - Arifureta Shokugyou de Sekai Saikyou Season 2
- Collection 11: Bikini Warriors (L15) — Metadata OK; 15: Bikini Warriors (L15); 273: NC - Bikini Warriors; 291: Specials - Bikini Warriors; 292: OVA - Bikini Warriors
- Collection 14: Bokutachi wa Benkyou ga Dekinai (J19-P19) — Metadata OK; 18: serie1 (J19); 19: serie2 (P19)
- Collection 19: Darker than Black (J07-P09) — Metadata OK; 24: Serie 1 (J07); 25: Serie 2 (P09); 283: Specials - DARKER THAN BLACK: Kuro no Keiyakusha - Sakura no Hana no Mankai no Shita; 284: Specials - DARKER THAN BLACK: Kuro no Keiyakusha - Gaiden
- Collection 29: Fate Grand Order (Z19) — Metadata OK; 35: Fate Grand Order (Z19); 246: Preview - Fate Grand Order - Absolute Demonic Front Babylonia; 247: Film - Fate Grand Order - First Order
- Collection 34: Genjitsu Shugi Yuusha no Oukoku Saikenki (L21-Z22) — Metadata OK; 40: Genjitsu Shugi Yuusha no Oukoku Saikenki (L21-Z22); 248: Genjitsu Shugi Yuusha no Oukoku Saikenki
- Collection 35: Getsuyoubi no Tawawa P16-P21 — Metadata OK; 41: Season 1 P16; 43: Season 2 P21; 293: Specials - Getsuyoubi no Tawawa; 294: NC - Getsuyoubi no Tawawa
- Collection 39: Go-toubun no Hanayome (Z19-Z21) — Metadata OK; 47: S1 (Z19); 48: S2 (Z21)
- Collection 43: Hataraku Maou-sama! (J13-L22) — Metadata OK; 52: Season 1 (J13); 53: Season 2 (L22)
- Collection 44: Hataraku Saibou (L18-Z21) — Metadata OK; 54: Serie 1 (L18); 55: Serie 2 (Z21); 275: Specials - Hataraku Saibou
- Collection 48: High School DxD (Z12-J18) — Metadata chybí; 58: High School DxD (Z12); 59: High School DxD Born (J15); 60: High School DxD Hero (J18); 61: High School DxD New (L13); 62: NC – High School DxD; 64: NC – High School DxD Hero; 65: NC – High School DxD New; 66: Specials – High School DxD - Specials; 67: Specials – High School DxD Born - Specials; 233: NC – High School DxD Born; 234: OVA - High School DxD; 235: OVA - High School DxD New; 236: OVA - High School DxD Born; 299: Specials – High School DxD Hero
- Collection 54: Ichiban Ushiro no Daimaou (J10) — Metadata OK; 73: Ichiban Ushiro no Daimaou (J10); 74: Specials
- Collection 59: Isekai Quartet — Metadata OK; 81: Isekai Quartet; 82: Serie 1; 83: Serie 2
- Collection 63: Itai no wa Iya nano de Bougyoryoku ni Kyokufuri Shitai to Omoimasu (Z20-Z23) — Metadata OK; 87: Serie1; 88: Serie2
- Collection 73: Kami-tachi ni Hirowareta Otoko (P20-Z23) — Metadata OK; 98: Serie1; 99: Serie2
- Collection 82: Kimetsu no Yaiba J19 — Metadata OK; 106: Kimetsu no Yaiba - Mugen Ressha-hen P21; 107: Kimetsu no Yaiba - Yuukaku-hen Z22; 108: Kimetsu no Yaiba J19
- Collection 86: Kobayashi-san Chi no Maid Dragon (Z17-L21) — Metadata OK; 112: Season 1 (Z17); 113: Season 2 (L21); 114: Season 2 Shorts (L21); 296: OVA - Kobayashi-san Chi no Maid Dragon
- Collection 88: Kono Subarashii Sekai ni Shukufuku wo! — Metadata OK; 116: Kono Subarashii Sekai ni Shukufuku wo!; 249: Kono Subarashii Sekai ni Shukufuku wo!; 250: Kono Subarashii Sekai ni Shukufuku wo!
- Collection 91: Kore wa Zombie Desuka (Z11-J12) — Metadata OK; 118: Kore wa Zombie Desuka (Z11); 119: Kore wa Zombie Desuka 2 (J12); 239: NC - Kore wa Zombie Desuka; 240: NC - Kore wa Zombie Desuka 2; 297: OVA - Kore wa Zombie desu ka?; 298: OVA - Kore wa Zombie desu ka? 2

## Migrace a write smoke pouze na kopii

SQLite backup do `/tmp/anime-metadata-completion-FsL91f/migration.db` byl vytvořen
z produkčního spojení `mode=ro`. Upgrade compatibility 1→2 doplnil pouze nullable
sloupec a `user_version=2`: všechny existující tabulkové hodnoty před/po zůstaly
shodné a všech 280 requirement hodnot bylo NULL. Opakovaný startup měl 0 DML.

Na kopii GET homepage měl 7 SQL, `/catalog/all` 5 SQL a Metadata Check 8 SQL;
všechny bez DML. Homepage i katalog shodně vrátily 88 OK / 76 missing.
POST not_required na Special 299 změnil collection 48 na OK, ale parent 60
zachoval přesně stejný metadata-range warning a episode-count comparison.
Clear override vrátil collection 48 do missing. Produkce tyto POST nedostala.

Mimo scope: explicitní plná maintenance `migrate_schema()` na této aktuální
kopii přepočítává 1 210 Videos. Totéž bylo ověřeno s původním migračním kódem
checkpointu `3685e90` na další kopii v `/tmp`: external_episode_number 1 210,
absolute_episode_number 107, episode_number_source/confidence 120 a
season_episode_number 13. Jde o existující derived numbering rekonstrukci,
nikoli potřebný krok k přidání requirement. Proto nový upgrade z markeru 1
provádí pouze DDL a marker, bez volání této rekonstrukce. Produkční hodnoty
nebyly opravovány ani přepočítávány.

Závěrečná validace implementace: celý suite **1 211 passed**, cílené sady prošly,
compileall a načtení všech 17 Jinja2 šablon bez chyb. Produkční velikost, mtime
a SHA-256 zůstaly přesně shodné s výchozím fingerprintem uvedeným výše;
produkce stále má `user_version=1` a nový sloupec neobsahuje. NAS nebyl čten ani
měněn, žádný produkční scan, commit ani push nebyl proveden.
