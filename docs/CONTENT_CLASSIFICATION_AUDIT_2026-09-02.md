# Read-only audit klasifikace obsahu – 2. září 2026

Tento report je snapshot produkční `data/anime.db` před commitem Media Check
policy pro OP/ED/NCOP/NCED. Databáze byla otevřena pouze přes SQLite URI
`mode=ro`. Audit neprovedl scan NAS, semantic write, změnu hierarchie ani změnu
parserových dat.

Použité významy:

- **raw** = `Video.file_type`, tedy uložená parserová evidence;
- **manual** = `Video.content_type_manual`;
- **title effective** = `CatalogTitle.effective_part_type` se zachováním úplné
  ruční hierarchy authority;
- **shared effective** = současný `effective_video_content_type()`:
  video manual → supplementary title effective → raw file type.

Současný `classify_video(relative_path)` se shoduje s uloženým `file_type` u
všech 3 100 videí. Strukturální názvy byly posouzeny existujícím
`parse_explicit_part()`; audit nepřidal ad-hoc parser ani regex.

## A. Classification matrix

V produkční DB reálně existuje těchto 31 kombinací (součet 3 100 videí):

| raw `file_type` | video manual | title effective | shared effective | počet |
|---|---|---|---|---:|
| bonus | NULL | bonus | bonus | 3 |
| cm | NULL | bonus | bonus | 1 |
| cm | NULL | special | special | 3 |
| ed | NULL | bonus | bonus | 5 |
| episode | NULL | film | film | 3 |
| episode | NULL | season | episode | 2 886 |
| episode | recap | season | recap | 1 |
| menu | NULL | special | special | 3 |
| nced | NULL | bonus | bonus | 10 |
| nced | NULL | season | nced | 6 |
| nced | NULL | special | special | 4 |
| ncop | NULL | bonus | bonus | 7 |
| ncop | NULL | season | ncop | 9 |
| ncop | NULL | special | special | 2 |
| op | NULL | bonus | bonus | 9 |
| other | NULL | bonus | bonus | 17 |
| other | NULL | film | film | 13 |
| other | NULL | ova | ova | 1 |
| other | NULL | preview | preview | 2 |
| other | NULL | season | other | 14 |
| other | NULL | special | special | 2 |
| other | preview | preview | preview | 2 |
| other | recap | season | recap | 9 |
| ova | NULL | ova | ova | 16 |
| ova | NULL | season | ova | 10 |
| pv | NULL | bonus | bonus | 3 |
| pv | NULL | special | special | 2 |
| special | NULL | bonus | bonus | 4 |
| special | NULL | ova | ova | 2 |
| special | NULL | season | special | 1 |
| special | NULL | special | special | 50 |

Kontrolní nulové skupiny: žádný raw Recap mimo Recap, žádný raw Bonus mimo
Bonus, žádný Film/Movies path hint mimo Film a žádná raw standardní epizoda
uvnitř Bonus/OVA/Preview/Recap/Special. Tři raw `episode` v effective Film jsou
ověřená ruční hierarchie a jsou uvedena v informativní sekci.

## B. Strong mismatch candidates

**13 videí** bez video manual override a bez ruční title hierarchy. Jde o
konkrétní supplementary parser evidence zamíchanou do automatické Season.
`shared effective` je v těchto řádcích stejný jako raw typ.

Společná pole každé skupiny uvádějí CatalogTitle, effective season, collection a
authority stav; každý řádek obsahuje Video ID, relativní cestu, raw/manual/shared
typ a důvod.

### CatalogTitle 25 – `Serie 2 (P09)`

- title effective: `season`, season 2; collection 19 `Darker than Black (J07-P09)`;
  collection hierarchy `automatic`, title manual authority `none`.
- `370` — `Darker than Black (J07-P09)/Serie 2 (P09)/Darker than Black - Ryuusei no Gemini OVA - 01.mkv` — raw `ova`, manual NULL, shared `ova`.
- `371` — `Darker than Black (J07-P09)/Serie 2 (P09)/Darker than Black - Ryuusei no Gemini OVA - 02.mkv` — raw `ova`, manual NULL, shared `ova`.
- `372` — `Darker than Black (J07-P09)/Serie 2 (P09)/Darker than Black - Ryuusei no Gemini OVA - 03.mkv` — raw `ova`, manual NULL, shared `ova`.
- `373` — `Darker than Black (J07-P09)/Serie 2 (P09)/Darker than Black - Ryuusei no Gemini OVA - 04.mkv` — raw `ova`, manual NULL, shared `ova`.
- důvod: OVA evidence uvnitř automatické Season, která obsahuje i standardní epizody.

### CatalogTitle 54 – `Serie 1 (L18)`

- title effective: `season`, season 1; collection 44 `Hataraku Saibou (L18-Z21)`;
  collection hierarchy `automatic`, title manual authority `none`.
- `773` — `Hataraku Saibou (L18-Z21)/Serie 1 (L18)/S01E14 [SP]-The Common Cold.mkv` — raw `special`, manual NULL, shared `special`.
- důvod: Special evidence uvnitř automatické Season se standardními epizodami.

### CatalogTitle 77 – `Serie 1 (L18)`

- title effective: `season`, season 1; collection 56
  `Isekai Maou to Shoukan Shoujo no Dorei Majutsu (L18-J21)`; collection
  hierarchy `automatic`, title manual authority `none`.
- `1010` — `Isekai Maou to Shoukan Shoujo no Dorei Majutsu (L18-J21)/Serie 1 (L18)/[Judas] How Not To Summon A Demon Lord - Clean Ending.mkv` — raw `nced`, manual NULL, shared `nced`.
- `1011` — `Isekai Maou to Shoukan Shoujo no Dorei Majutsu (L18-J21)/Serie 1 (L18)/[Judas] How Not To Summon A Demon Lord - Clean Opening.mkv` — raw `ncop`, manual NULL, shared `ncop`.
- důvod: NCED/NCOP jsou přímo ve standardní Season; jsou kandidáty na pozdější ruční přesun do Bonus.

### CatalogTitle 124 – `Mahoutsukai no Yome (P17)`

- title effective: `season`, season 1; collection 96 `Mahoutsukai no Yome (P17)`;
  collection hierarchy `automatic`, title manual authority `none`.
- `1632` — `Mahoutsukai no Yome (P17)/Mahoutsukai no Yome OVA - 01.mkv` — raw/manual/shared `ova`/NULL/`ova`.
- `1633` — `Mahoutsukai no Yome (P17)/Mahoutsukai no Yome OVA - 02.mkv` — raw/manual/shared `ova`/NULL/`ova`.
- `1634` — `Mahoutsukai no Yome (P17)/Mahoutsukai no Yome OVA - 03.mkv` — raw/manual/shared `ova`/NULL/`ova`.
- důvod: OVA evidence uvnitř automatické Season se standardními epizodami.

### CatalogTitle 135 – `Monster Musume no Iru Nichijou L15`

- title effective: `season`, season 1; collection 104
  `Monster Musume no Iru Nichijou L15`; collection hierarchy `automatic`, title
  manual authority `none`.
- `1775` — `Monster Musume no Iru Nichijou L15/Monster Musume no Iru Nichijou - OVA 01.mp4` — raw/manual/shared `ova`/NULL/`ova`.
- `1776` — `Monster Musume no Iru Nichijou L15/Monster Musume no Iru Nichijou - OVA 02.mp4` — raw/manual/shared `ova`/NULL/`ova`.
- důvod: OVA evidence uvnitř automatické Season se standardními epizodami.

### CatalogTitle 231 – `Yuusha, Yamemasu (J22)`

- title effective: `season`, season 1; collection 171 `Yuusha, Yamemasu (J22)`;
  collection hierarchy `automatic`, title manual authority `none`.
- `3088` — `Yuusha, Yamemasu (J22)/Yuusha, Yamemasu - OVA1.mkv` — raw/manual/shared `ova`/NULL/`ova`.
- důvod: OVA evidence uvnitř automatické Season se standardními epizodami.

## C. Review candidates

**11 videí.** Rozdíl sám o sobě není chyba; cesta i současný strukturální parser
podporují zvolený kontejner.

### CatalogTitle 76 – `SPs`

- title effective: `special`, bez season; collection 56
  `Isekai Maou to Shoukan Shoujo no Dorei Majutsu (L18-J21)`; collection
  hierarchy `automatic`, title manual authority `none`.
- title obsahuje `cm=3`, `menu=3`, `nced=4`, `ncop=2`, `other=2`, `pv=2`.
- `990` — `Isekai Maou to Shoukan Shoujo no Dorei Majutsu (L18-J21)/SPs/[Anipakku] Isekai Maou to Shoukan Shoujo no Dorei Majutsu [NCED01][Ma10p_1080p][x265_flac].mkv` — raw/manual/shared `nced`/NULL/`special`.
- `991` — `Isekai Maou to Shoukan Shoujo no Dorei Majutsu (L18-J21)/SPs/[Anipakku] Isekai Maou to Shoukan Shoujo no Dorei Majutsu [NCED02][Ma10p_1080p][x265_flac].mkv` — raw/manual/shared `nced`/NULL/`special`.
- `992` — `Isekai Maou to Shoukan Shoujo no Dorei Majutsu (L18-J21)/SPs/[Anipakku] Isekai Maou to Shoukan Shoujo no Dorei Majutsu [NCED03][Ma10p_1080p][x265_flac].mkv` — raw/manual/shared `nced`/NULL/`special`.
- `993` — `Isekai Maou to Shoukan Shoujo no Dorei Majutsu (L18-J21)/SPs/[Anipakku] Isekai Maou to Shoukan Shoujo no Dorei Majutsu [NCED04][Ma10p_1080p][x265_flac].mkv` — raw/manual/shared `nced`/NULL/`special`.
- `994` — `Isekai Maou to Shoukan Shoujo no Dorei Majutsu (L18-J21)/SPs/[Anipakku] Isekai Maou to Shoukan Shoujo no Dorei Majutsu [NCOP01][Ma10p_1080p][x265_flac].mkv` — raw/manual/shared `ncop`/NULL/`special`.
- `995` — `Isekai Maou to Shoukan Shoujo no Dorei Majutsu (L18-J21)/SPs/[Anipakku] Isekai Maou to Shoukan Shoujo no Dorei Majutsu [NCOP02][Ma10p_1080p][x265_flac].mkv` — raw/manual/shared `ncop`/NULL/`special`.
- `996` — `Isekai Maou to Shoukan Shoujo no Dorei Majutsu (L18-J21)/SPs/[Anipakku] Isekai Maou to Shoukan Shoujo no Dorei Majutsu [PV01][Ma10p_1080p][x265_flac].mkv` — raw/manual/shared `pv`/NULL/`special`.
- `997` — `Isekai Maou to Shoukan Shoujo no Dorei Majutsu (L18-J21)/SPs/[Anipakku] Isekai Maou to Shoukan Shoujo no Dorei Majutsu [PV02][Ma10p_1080p][x265_flac].mkv` — raw/manual/shared `pv`/NULL/`special`.
- důvod: heterogenní SPs kontejner; přesné NC položky jsou kandidáty na Bonus,
  PV vyžadují lidské posouzení. Název `SPs` současný helper legitimně čte jako
  Special, proto nejde o automatickou chybu.

### CatalogTitle 203 – `CM&PV`

- title effective: `bonus`, bez season; collection 147 `Tenki no Ko (FILM)`;
  collection hierarchy `automatic`, title manual authority `none`.
- `2715` — `Tenki no Ko (FILM)/CM&PV/[Beatrice-Raws] Tenki no Ko (PV 01) [BDRip 1920x1080 HEVC FLAC].mkv` — raw/manual/shared `pv`/NULL/`bonus`.
- `2716` — `Tenki no Ko (FILM)/CM&PV/[Beatrice-Raws] Tenki no Ko (PV 02) [BDRip 1920x1080 HEVC FLAC].mkv` — raw/manual/shared `pv`/NULL/`bonus`.
- `2717` — `Tenki no Ko (FILM)/CM&PV/[Beatrice-Raws] Tenki no Ko (PV 03) [BDRip 1920x1080 HEVC FLAC].mkv` — raw/manual/shared `pv`/NULL/`bonus`.
- důvod: raw PV v Bonus spolu s jedním CM. Existující strukturální helper
  explicitně podporuje `CM&PV` jako Bonus; pravděpodobně legitimní směs.

## D. Manual-authority informational

**34 odlišných videí**: 12 s explicitním `Video.content_type_manual` a 22 pod
úplnou ruční title hierarchy. Navíc existují **2 title-level path-hint rozdíly**.
Nejsou označeny jako chyby a audit je nijak neměnil.

### Explicitní video manual override – 12 videí

Formát: `Video ID` — cesta — raw → manual/shared; CatalogTitle ID, local title,
effective type/season; collection ID, display/local název, hierarchy status;
title authority.

- `51` — `Ansatsu Kyoushitsu (Z15-Z16)/Serie 1 (Z15)/Ansatsu Kyoushitsu 00.mp4` — other → preview; title 245 `Preview - Ansatsu Kyoushitsu`, preview/S1; collection 3 `Ansatsu Kyoushitsu (Z15-Z16)`, verified; complete manual.
- `120` — `Arifureta Shokugyou de Sekai Saikyou (L19-Z22)/Season 1 (L19)/Arifureta Shokugyou de Sekai Saikyou - 05.5.mkv` — other → recap; title 8 `Season 1 (L19)`, season/S1; collection 5 `Arifureta Shokugyou de Sekai Saikyou (L19-Z22)`, automatic; no title manual authority.
- `427` — `Dokyuu Hentai HxEros (L20)/Dokyuu Hentai HxEros - 07.5.mp4` — other → recap; title 30, season/S1; collection 24 `Dokyuu Hentai HxEros (L20)`, verified; complete manual.
- `834` — `High School DxD (Z12-J18)/High School DxD Hero (J18)/High School DxD Hero - 00.mkv` — other → preview; title 237 `Preview - High School DxD Hero`, preview/S4; collection 48 `High School DxD (Z12-J18)`, verified; complete manual.
- `1281` — `Kandagawa Jet Girls P19/Kandagawa Jet Girls - 04.5.mkv` — other → recap; title 100, season/S1; collection 74 `Kandagawa Jet Girls P19`, verified; complete manual.
- `2110` — `Ore wo Suki nano wa Omae dake ka yo P19/Ore wo Suki nano wa Omae dake ka yo - 09.5.mkv` — other → recap; title 163, season/S1; collection 120 `Ore wo Suki nano wa Omae dake ka yo P19`, verified; complete manual.
- `2281` — `Saihate no Paladin (P21)/Saihate no Paladin - 07.5.mkv` — other → recap; title 173, season/S1; collection 129 `Saihate no Paladin (P21)`, verified; complete manual.
- `2382` — `Shinchou Yuusha P19/Shinchou Yuusha - 09.5.mkv` — other → recap; title 181, season/S1; collection 137 `Shinchou Yuusha P19`, verified; complete manual.
- `2586` — `Sword Art Online (L12-L20)/Serie 2 (L14)/Sword Art Online II - 15.mkv` — episode → recap; title 194, season/S2; collection 143 `Sword Art Online (L12-L20)`, automatic; no title manual authority.
- `2802` — `Tensei Shitara Slime Datta Ken (P18-L21)/_Tensei Shitara Slime Datta Ken - 24.5.mkv` — other → recap; title 207, season/S1; collection 151 `Tensei Shitara Slime Datta Ken (P18-L21)`, verified; complete manual.
- `2803` — `Tensei Shitara Slime Datta Ken (P18-L21)/_Tensei Shitara Slime Datta Ken - 24.9.mkv` — other → recap; title 207, season/S1; collection 151, verified; complete manual.
- `2804` — `Tensei Shitara Slime Datta Ken (P18-L21)/_Tensei Shitara Slime Datta Ken - 36.5.mkv` — other → recap; title 267 `Tensei Shitara Slime Datta Ken 2nd Season`, season/S2; collection 151, verified; complete manual.

### Úplná ruční title hierarchy – 22 videí

- title 139 `Nande Koko ni Sensei ga (J19)`, effective season/S1, collection 108
  stejného názvu (`verified`, complete manual): videa `1852–1864`, raw směs
  `nced=5`, `ncop=8`, společně se standardními epizodami. Přesné cesty jsou v
  úplné OP/ED/NC inventuře níže.
- `115` — `Arifureta Shokugyou de Sekai Saikyou (L19-Z22)/Arifureta Shokugyou de Sekai Saikyou SP Episode 00 Prologue.mkv` — raw `special`, manual NULL, shared `ova`; title 7, effective ova/S2; collection 5, automatic; complete manual.
- `1530` — `Kono Yo no Hate de Koi wo Utau Shoujo YU-NO J19 cz-xx%/S01E26.5-SP.mkv` — raw `special`, manual NULL, shared `ova`; title 251 `OVA - Kono Yo no Hate de Koi wo Utau Shoujo YU-NO`, effective ova/S1; collection 89, verified; complete manual.
- `1760` — `Monogatari/Kizumonogatari - 01.mp4` — raw `episode`, manual NULL, shared `film`; title 253 `Kizumonogatari`, Film; collection 103 `Monogatari`, verified; complete manual.
- `1761` — `Monogatari/Kizumonogatari - 02.mp4` — raw `episode`, manual NULL, shared `film`; title 254 `Kizumonogatari II`, Film; collection 103, verified; complete manual.
- `1762` — `Monogatari/Kizumonogatari - 03.mkv` — raw `episode`, manual NULL, shared `film`; title 255 `Kizumonogatari III`, Film; collection 103, verified; complete manual.
- `1968` — `OVERLORD (L15-L22)/Extras/Drama CD/Overlord Drama CD Special Edition [The Maid Tea Party].mkv` — raw `special`, manual NULL, shared `bonus`.
- `1969` — `OVERLORD (L15-L22)/Extras/Drama CD/Overlord II Special Voice Drama CD [Visual Version].mkv` — raw `special`, manual NULL, shared `bonus`.
- `1970` — `OVERLORD (L15-L22)/Extras/Drama CD/Overlord Special Voice Drama CD Vol.1 [Visual Version].mkv` — raw `special`, manual NULL, shared `bonus`.
- `1971` — `OVERLORD (L15-L22)/Extras/Drama CD/Overlord Special Voice Drama CD Vol.2 [Visual Version].mkv` — raw `special`, manual NULL, shared `bonus`.
- Poslední čtyři: title 147 `Extras – Drama CD`, effective Bonus; collection 115
  `OVERLORD (L15-L22)`, verified; complete manual. Slovo „Special“ je součástí
  názvu konkrétního Drama CD, proto je ruční Bonus věrohodný.

### Title-level path hints – 2 části

- title 148 `Season 1`, cesta `OVERLORD (L15-L22)/Extras/Specials/Season 1`:
  path helper dává Season, úplná manual authority dává Special/S1; 8 videí;
  collection 115, verified.
- title 150 `Season 2`, cesta `OVERLORD (L15-L22)/Extras/Specials/Season 2`:
  path helper dává Season, úplná manual authority dává Special/S2; 13 videí;
  collection 115, verified.

## E. OP/ED/NC inventory

Celkem **52** fyzických videí: `OP=9`, `ED=5`, `NCOP=18`, `NCED=20`.
Žádné nemá `Video.content_type_manual`.

| title effective | OP | ED | NCOP | NCED | celkem |
|---|---:|---:|---:|---:|---:|
| Bonus | 9 | 5 | 7 | 10 | 31 |
| Special | 0 | 0 | 2 | 4 | 6 |
| Season | 0 | 0 | 9 | 6 | 15 |
| jinde | 0 | 0 | 0 | 0 | 0 |

Všech **21 položek mimo Bonus** je kandidátem k lidskému posouzení pro
případný pozdější přesun v Hierarchy Review. Videa pod verified/manual title
139 nejsou automatickým mismatch kandidátem a ruční authority se nesmí obejít.

### Special – 6 videí

- `990–995`, title 76 `SPs`, effective Special, collection 56 automatic:
  přesné cesty a typy jsou v sekci C. Jde o čtyři NCED a dvě NCOP.

### Season – 15 videí

- `1010` — `Isekai Maou to Shoukan Shoujo no Dorei Majutsu (L18-J21)/Serie 1 (L18)/[Judas] How Not To Summon A Demon Lord - Clean Ending.mkv` — raw/shared `nced`, manual NULL; title 77 `Serie 1 (L18)`, season/S1, no manual authority; collection 56 automatic.
- `1011` — `Isekai Maou to Shoukan Shoujo no Dorei Majutsu (L18-J21)/Serie 1 (L18)/[Judas] How Not To Summon A Demon Lord - Clean Opening.mkv` — raw/shared `ncop`, manual NULL; stejný title/collection stav.
- `1852` — `Nande Koko ni Sensei ga (J19)/Nande Koko ni Sensei ga! - NCED1.mp4` — raw/shared `nced`, manual NULL.
- `1853` — `Nande Koko ni Sensei ga (J19)/Nande Koko ni Sensei ga! - NCED2.mp4` — raw/shared `nced`, manual NULL.
- `1854` — `Nande Koko ni Sensei ga (J19)/Nande Koko ni Sensei ga! - NCED3.mp4` — raw/shared `nced`, manual NULL.
- `1855` — `Nande Koko ni Sensei ga (J19)/Nande Koko ni Sensei ga! - NCED4.mp4` — raw/shared `nced`, manual NULL.
- `1856` — `Nande Koko ni Sensei ga (J19)/Nande Koko ni Sensei ga! - NCED5.mp4` — raw/shared `nced`, manual NULL.
- `1857` — `Nande Koko ni Sensei ga (J19)/Nande Koko ni Sensei ga! - NCOP Ver.TV1.mp4` — raw/shared `ncop`, manual NULL.
- `1858` — `Nande Koko ni Sensei ga (J19)/Nande Koko ni Sensei ga! - NCOP Ver.TV2.mp4` — raw/shared `ncop`, manual NULL.
- `1859` — `Nande Koko ni Sensei ga (J19)/Nande Koko ni Sensei ga! - NCOP Ver.TV3.mp4` — raw/shared `ncop`, manual NULL.
- `1860` — `Nande Koko ni Sensei ga (J19)/Nande Koko ni Sensei ga! - NCOP Ver.TV4.mp4` — raw/shared `ncop`, manual NULL.
- `1861` — `Nande Koko ni Sensei ga (J19)/Nande Koko ni Sensei ga! - NCOP1.mp4` — raw/shared `ncop`, manual NULL.
- `1862` — `Nande Koko ni Sensei ga (J19)/Nande Koko ni Sensei ga! - NCOP2.mp4` — raw/shared `ncop`, manual NULL.
- `1863` — `Nande Koko ni Sensei ga (J19)/Nande Koko ni Sensei ga! - NCOP3.mp4` — raw/shared `ncop`, manual NULL.
- `1864` — `Nande Koko ni Sensei ga (J19)/Nande Koko ni Sensei ga! - NCOP4.mp4` — raw/shared `ncop`, manual NULL.
- Videa `1852–1864`: title 139 `Nande Koko ni Sensei ga (J19)`, effective
  Season/S1 s complete manual authority; collection 108 stejného názvu,
  hierarchy `verified`.

Pro úzkou Media Check policy je všech 52 subtitle-not-required: explicitní raw
OP/ED/NCOP/NCED se použije před title kontejnerem, protože žádné z videí nemá
manual video override. To není změna jejich shared effective ani hierarchie.

## F. Mixed CatalogTitle candidates

### Strong

- title 25: `episode=12` + `ova=4`;
- title 54: `episode=13` + `special=1`;
- title 77: `episode=12` + `nced=1` + `ncop=1`;
- title 124: `episode=24` + `ova=3`;
- title 135: `episode=12` + `ova=2`;
- title 231: `episode=12` + `ova=1`.

### Review

- title 76 `SPs`: `cm=3`, `menu=3`, `nced=4`, `ncop=2`, `other=2`, `pv=2`;
- title 203 `CM&PV`: `cm=1`, `pv=3`; pravděpodobně legitimní Bonus.

### Manual-authority informational

- title 139: standardní epizody + 5 NCED + 8 NCOP, complete manual Season;
- title 7 a 251: raw Special uvnitř complete manual OVA;
- title 147: raw Special + Other uvnitř complete manual Bonus;
- titles 253–255: raw Episode uvnitř complete manual Film;
- titles 8, 30, 100, 163, 173, 181, 194, 207 a 267: standardní část s
  explicitně ručně klasifikovaným Recap videem.

Koherentní Bonus části obsahující pouze příbuznou OP/ED/NC rodinu nejsou
označeny jako podezřelé.

## G. Doporučené ruční kroky

1. V Hierarchy Review nejdřív posoudit šest automatických Season titulů ze
   sekce B a podle obsahu ručně oddělit OVA/Special/Bonus části.
2. U title 76 `SPs` posoudit rozdělení NCOP/NCED do Bonus a samostatně rozhodnout
   PV/CM/menu obsah. Neodvozovat přesun pouze ze slova `SPs`.
3. U title 77 přesunout clean opening/ending do Bonus jen po lidském potvrzení.
4. U verified/manual title 139 nejprve ověřit původní záměr authority; nic
   nepřesouvat automaticky.
5. Případy v sekci D ponechat beze změny, dokud lidská kontrola nepotvrdí opak.

Audit nedoporučuje globální přepis title typu, parser persistence ani pravidlo
`supplementary => subtitle not required`. Jde pouze o seznam pro ruční kontrolu.
