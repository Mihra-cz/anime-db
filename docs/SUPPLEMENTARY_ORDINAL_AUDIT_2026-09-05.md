# Supplementary ordinal audit – 5. září 2026

> Historický snapshot před navazujícím parserovým doplněním. Aktuální výsledky,
> všech 52 videí devíti kandidátních skupin a jejich rozlišení na unknown versus
> ordinal collisions obsahuje [navazující re-audit](SUPPLEMENTARY_CANDIDATE_REAUDIT_2026-09-05.md).

Výchozí HEAD `5211758`, `main = origin/main`, pracovní strom čistý. Autoritou jsou aktuální data,
ne historický checkpoint. Audit otevřel produkční SQLite výhradně přes `mode=ro`, bez startupu aplikace,
migrace nebo scanu. Reprodukce: `.venv/bin/python -m app.tools.audit_supplementary data/anime.db`.

## Zjištění před implementací

- Parser (`app/catalog.py`) vrací `EpisodeNumberDetection(kind="supplementary", number=…)`;
  `supplementary_number` je odvozená property, nikoli sloupec Video. Explicitní OP, ED, NCOP,
  NCED, OVA/OAD, Special, Preview/PV, CM mají subtype a volitelné celé číslo. PV je alias
  `preview`, scanner jej persistuje jako `file_type=pv`. Recap má navíc přesné fractional/manual
  semantics. Bonus/Extra a Menu parser také umí očíslovat; Other nemá vlastní supplementary
  namespace. Interview/making-of se v tomto úkolu nově nečíslují.
- `Video` nemá generické persistentní `episode_number` ani `supplementary_number`. Má filename,
  `file_type`, `local_episode_number`, `season_episode_number`, `absolute_episode_number`,
  `external_episode_number`, `episode_number_manual_override`, source/confidence/verified_at
  a samostatné `recap_episode_number_manual_tenths`. Canonical pole jsou přepočítané projekce;
  supplementary marker při přepočtu typicky znamená NULL v canonical polích. Filename se neztrácí.
- `automatic_supplementary_numbering()` kombinoval explicitní parser a scanner file_type
  s generic standard číslem. Poslední fallback mohl použít i číslo získané jen díky adresáři.
  Raw helper zůstává diagnostickou/scan evidencí; nový effective resolver takový automatický
  převod source Episode 14 na OVA14 nepřebírá.
- `effective_video_numbering()` určoval classification podle manual video/title, ale pro supplementary
  číslo používal automatický hint; explicitní manual číslo se ve supplementary větvi neuplatnilo.
  `video_numbering_identity()` měl další vlastní rozhodování a explicitní parser mohl přebít manual
  číslo. Title-level classification sama ordinal nepersistovala ani nezahazovala filename.
- `effective_video_content_display()` zobrazoval effective typ kontejneru a jen fractional/nonstandard
  pozici. Supplementary číslo do labelu vůbec nepřidával. Šablona detailu četla canonical season
  číslo, takže OVA 02 mohla skončit jako prosté OVA; NCOP03 pod Bonus jako Bonus.
- Supplementary duplicate grouping ignoroval variant lanes. Metadata count používal množinu čísel
  bez potvrzení, zda více fyzických reprezentací stejného čísla skutečně tvoří jednu položku.
  Zde se ambiguity mohla tiše ztratit, přestože metadata-range warning už některé Media Part konflikty
  uměl ukázat. Tři regresní očekávání nyní místo falešné count shody požadují unavailable.

## Výsledný kontrakt

Nový čistý in-memory resolver `app/supplementary.py` čte existující pole; **bez schema změny**.
Lokální identita je `(CatalogTitle, supplementary subtype, ordinal)`. OP01 a ED01 jsou různé identity.
Číslo není automaticky providerové ani canonical. Žádné persistentní backfilly ani inference podle
pořadí, abecedy nebo počtu videí nejsou zavedeny.

1. Non-NULL video manual classification má přednost. Pokud ruší parserový typ, jeho ordinal se
   nepřenese do jiného typu. Explicitní ruční číslo pro účinný supplementary typ vyhrává.
2. Přesný parser subtype má přednost před broad Bonus/Special/Season kontejnerem. Číslo pochází
   z odpovídajícího explicitního supplementary markeru, případně z dříve podporovaného generic
   čísla s odpovídajícím markerem přímo ve filename (`Title OVA - 01`). Pouhý raw typ z adresáře nestačí.
3. Bez této evidence zůstává ordinal unknown. Ani singleton nedostane automaticky 01.
   Starší explicitní canonical override generic čísla uvnitř Season zůstává canonical;
   explicitní supplementary filename nebo supplementary manual/title authority jej používá lokálně.

Podporované opakovatelné typy: OP, ED, NCOP, NCED, OVA, Special, Preview/PV a CM. PV/Preview zůstává
jediný existující namespace. Recap fractional semantics se nemění. Existující Bonus/Menu parser hints
zůstávají v legacy cestách, ale nejsou nově přidány do bezpečného ordinal kontraktu.

Media Parts jsou fyzické segmenty uvnitř ordinalu a variant lane. `OVA01 Part 1 of 2` / `Part 2 of 2`
parser zachová jako ordinal 1, `media_part_number` se stále nastavuje pouze ručně. Legacy `OVA P1/P2`
s aktivní Media Part autoritou bez ručního logického čísla není bezpečný ordinal. Úplná ruční sada
1..N stejné identity tvoří jednu reprezentaci. UI počítá denominator uvnitř konkrétního ordinalu/lane.

Dvě distinct potvrzené variant groups stejného ordinalu tvoří jednu logickou položku. NULL není default
lane; NULL+known, NULL+NULL nebo více neoznačených kopií ve stejné lane znamená review. Potvrzená
secondary copy počet nezvýší. Poškozený primary reference, konflikt variant nebo Media Part vazby
zůstávají explicitní review. Inventory `logical_count=None` při nejistotě nikdy nevydává neověřené
sloučení za skutečný počet. Starší hierarchy duplicate view navíc zachovává explicitní season/context
rozlišení pro dosud smíšené supplementary titles; inventory pro V6 konzervativně kontroluje celý title/type.

Prezentace typu a čísla, řazení a numbering identity používají stejný ordinal. Detail zobrazuje lokální
identitu před starší canonical projekcí a nabízí existující manual field jako lokální pořadí. Media Part
sloupec a variant badge zůstávají oddělené. Hierarchy Review používá effective label před raw filename hintem.

**V6 invariant:** před fyzickým rename musí každá opakovatelná supplementary položka mít bezpečný
ordinal nebo explicitní unresolved/review stav. Nevyřešené stejné ordinaly, varianty a fyzické části
musí být také rozlišeny; samotné číslo ještě nezaručuje unikátní target path. V6 nesmí generovat
kolidující cílové cesty. `supplementary_inventory()` je pouze read-only precondition, nikoli rename planner.

## Produkční inventura

Počty jsou fyzické Video podle effective ordinal subtype, s manual classification jako autoritou.
„Effective“ znamená bezpečnou číselnou evidenci konkrétního videa; kolize jsou samostatná kontrola
reprezentací, nikoli další přidělené číslo. Media groups jsou sady s ručním Media Part markerem;
variant groups jsou distinct potvrzená group ID použitá u typu (stejná group může pokrýt více ordinalů).
PV řádek používá raw `pv` bez výslovného Preview ve filename; jde o společný Preview/PV namespace,
nikoli devátý nezávislý typ. V aktuálních datech nejsou explicitní Preview filenames.

| Typ | Video | Raw parser ordinal | Manual číslo | Effective ordinal | Bez ordinalu | Ordinal kolize | Media Part skupiny | Variant groups |
|---|---|---|---|---|---|---|---|---|
| OP | 9 | 8 | 0 | 8 | 1 | 0 | 0 | 0 |
| ED | 5 | 2 | 0 | 2 | 3 | 0 | 0 | 0 |
| NCOP | 18 | 8 | 0 | 8 | 10 | 0 | 0 | 2 |
| NCED | 20 | 10 | 0 | 10 | 10 | 0 | 0 | 1 |
| OVA | 31 | 12 | 15 | 30 | 1 | 0 | 1 | 0 |
| SPECIAL | 66 | 18 | 28 | 36 | 30 | 0 | 0 | 1 |
| PREVIEW | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| PV | 8 | 2 | 0 | 2 | 6 | 0 | 0 | 0 |
| CM | 4 | 3 | 0 | 3 | 1 | 0 | 0 | 0 |

Celkem 161 sledovaných videí z 3100; **62 bez bezpečného ordinalu**, 99 s číslem. Nevyřešené kolize známého ordinalu: 0.

## Nande Koko ni Sensei ga – všechny supplementary položky

| Video.id | Path | Raw type | Raw ordinal | Manual číslo | Effective ordinal | Variant group / lane | CatalogTitle |
|---|---|---|---|---|---|---|---|
| 1851 | Nande Koko ni Sensei ga (J19)/Nande Koko ni Sensei ga! - 13.mp4 | episode | — | — | — | 7 / BD / uncensored | 286 – Specials - Nande Koko ni Sensei ga!? Special |
| 1852 | Nande Koko ni Sensei ga (J19)/Nande Koko ni Sensei ga! - NCED1.mp4 | nced | 1 | — | 1 | 8 / BD / uncensored | 285 – NC - Nande Koko ni Sensei ga |
| 1853 | Nande Koko ni Sensei ga (J19)/Nande Koko ni Sensei ga! - NCED2.mp4 | nced | 2 | — | 2 | 8 / BD / uncensored | 285 – NC - Nande Koko ni Sensei ga |
| 1854 | Nande Koko ni Sensei ga (J19)/Nande Koko ni Sensei ga! - NCED3.mp4 | nced | 3 | — | 3 | 8 / BD / uncensored | 285 – NC - Nande Koko ni Sensei ga |
| 1855 | Nande Koko ni Sensei ga (J19)/Nande Koko ni Sensei ga! - NCED4.mp4 | nced | 4 | — | 4 | 8 / BD / uncensored | 285 – NC - Nande Koko ni Sensei ga |
| 1856 | Nande Koko ni Sensei ga (J19)/Nande Koko ni Sensei ga! - NCED5.mp4 | nced | 5 | — | 5 | 8 / BD / uncensored | 285 – NC - Nande Koko ni Sensei ga |
| 1857 | Nande Koko ni Sensei ga (J19)/Nande Koko ni Sensei ga! - NCOP Ver.TV1.mp4 | ncop | — | — | — | 9 / TV / censored | 285 – NC - Nande Koko ni Sensei ga |
| 1858 | Nande Koko ni Sensei ga (J19)/Nande Koko ni Sensei ga! - NCOP Ver.TV2.mp4 | ncop | — | — | — | 9 / TV / censored | 285 – NC - Nande Koko ni Sensei ga |
| 1859 | Nande Koko ni Sensei ga (J19)/Nande Koko ni Sensei ga! - NCOP Ver.TV3.mp4 | ncop | — | — | — | 9 / TV / censored | 285 – NC - Nande Koko ni Sensei ga |
| 1860 | Nande Koko ni Sensei ga (J19)/Nande Koko ni Sensei ga! - NCOP Ver.TV4.mp4 | ncop | — | — | — | 9 / TV / censored | 285 – NC - Nande Koko ni Sensei ga |
| 1861 | Nande Koko ni Sensei ga (J19)/Nande Koko ni Sensei ga! - NCOP1.mp4 | ncop | 1 | — | 1 | 8 / BD / uncensored | 285 – NC - Nande Koko ni Sensei ga |
| 1862 | Nande Koko ni Sensei ga (J19)/Nande Koko ni Sensei ga! - NCOP2.mp4 | ncop | 2 | — | 2 | 8 / BD / uncensored | 285 – NC - Nande Koko ni Sensei ga |
| 1863 | Nande Koko ni Sensei ga (J19)/Nande Koko ni Sensei ga! - NCOP3.mp4 | ncop | 3 | — | 3 | 8 / BD / uncensored | 285 – NC - Nande Koko ni Sensei ga |
| 1864 | Nande Koko ni Sensei ga (J19)/Nande Koko ni Sensei ga! - NCOP4.mp4 | ncop | 4 | — | 4 | 8 / BD / uncensored | 285 – NC - Nande Koko ni Sensei ga |

Title 285 obsahuje 13 NC videí: 4 NCOP s ordinaly 1..4 v group 8 (BD / uncensored),
4 NCOP v group 9 (TV / censored) bez bezpečného ordinalu a 5 NCED 1..5 v group 8.
`Ver.TV1` až `Ver.TV4` parser chápe jako unnumbered NCOP, neprokazuje význam trailing čísla.
**4 logické NCOP × 2 lanes proto zatím bezpečně potvrdit nelze**, přestože variant authority existuje.
Potřebná je explicitní ruční číselná autorita nebo zvlášť ověřené parserové pravidlo.
NCED1..5 už představují pět bezpečných identit. Samostatný Special 1851 v title 286 má raw Episode 13,
nikoli Special13; jeho ordinal zůstává unknown. Produkční data nebyla upravena.

## Všechny V6 kandidáty: více videí typu v title a alespoň jedno bez ordinalu

| Title ID | Title | Typ | Fyzická videa | Bez ordinalu |
|---|---|---|---|---|
| 66 | Specials – High School DxD - Specials | special | 11 | 5 |
| 279 | Interview - Isekai Maou to Shoukan Shoujo no Dorei Majutsu | special | 2 | 2 |
| 276 | NC - Isekai Maou to Shoukan Shoujo no Dorei Majutsu | nced | 5 | 1 |
| 276 | NC - Isekai Maou to Shoukan Shoujo no Dorei Majutsu | ncop | 3 | 1 |
| 285 | NC - Nande Koko ni Sensei ga | ncop | 8 | 4 |
| 147 | Extras – Drama CD | special | 4 | 4 |
| 150 | Season 2 | special | 13 | 13 |
| 203 | CM&PV | preview | 3 | 3 |
| 271 | NC - Tensei Shitara Slime Datta Ken | nced | 3 | 2 |

Úplný výpis dotčených videí bez ordinalu:

| Video.id | Title ID | Typ | Path |
|---|---|---|---|
| 878 | 66 | special | High School DxD (Z12-J18)/Specials/High School DxD - Specials/high_scool_dxd_bd_spec_02.mp4 |
| 879 | 66 | special | High School DxD (Z12-J18)/Specials/High School DxD - Specials/high_scool_dxd_bd_spec_03.mp4 |
| 880 | 66 | special | High School DxD (Z12-J18)/Specials/High School DxD - Specials/high_scool_dxd_bd_spec_04.mp4 |
| 881 | 66 | special | High School DxD (Z12-J18)/Specials/High School DxD - Specials/high_scool_dxd_bd_spec_05.mp4 |
| 882 | 66 | special | High School DxD (Z12-J18)/Specials/High School DxD - Specials/high_scool_dxd_bd_spec_06.mp4 |
| 985 | 279 | special | Isekai Maou to Shoukan Shoujo no Dorei Majutsu (L18-J21)/SPs/[Anipakku] Isekai Maou to Shoukan Shoujo no Dorei Majutsu [IV01][Ma10p_1080p][x265_aac].mkv |
| 986 | 279 | special | Isekai Maou to Shoukan Shoujo no Dorei Majutsu (L18-J21)/SPs/[Anipakku] Isekai Maou to Shoukan Shoujo no Dorei Majutsu [IV02][Ma10p_1080p][x265_aac].mkv |
| 1010 | 276 | nced | Isekai Maou to Shoukan Shoujo no Dorei Majutsu (L18-J21)/Serie 1 (L18)/[Judas] How Not To Summon A Demon Lord - Clean Ending.mkv |
| 1011 | 276 | ncop | Isekai Maou to Shoukan Shoujo no Dorei Majutsu (L18-J21)/Serie 1 (L18)/[Judas] How Not To Summon A Demon Lord - Clean Opening.mkv |
| 1857 | 285 | ncop | Nande Koko ni Sensei ga (J19)/Nande Koko ni Sensei ga! - NCOP Ver.TV1.mp4 |
| 1858 | 285 | ncop | Nande Koko ni Sensei ga (J19)/Nande Koko ni Sensei ga! - NCOP Ver.TV2.mp4 |
| 1859 | 285 | ncop | Nande Koko ni Sensei ga (J19)/Nande Koko ni Sensei ga! - NCOP Ver.TV3.mp4 |
| 1860 | 285 | ncop | Nande Koko ni Sensei ga (J19)/Nande Koko ni Sensei ga! - NCOP Ver.TV4.mp4 |
| 1968 | 147 | special | OVERLORD (L15-L22)/Extras/Drama CD/Overlord Drama CD Special Edition [The Maid Tea Party].mkv |
| 1969 | 147 | special | OVERLORD (L15-L22)/Extras/Drama CD/Overlord II Special Voice Drama CD [Visual Version].mkv |
| 1970 | 147 | special | OVERLORD (L15-L22)/Extras/Drama CD/Overlord Special Voice Drama CD Vol.1 [Visual Version].mkv |
| 1971 | 147 | special | OVERLORD (L15-L22)/Extras/Drama CD/Overlord Special Voice Drama CD Vol.2 [Visual Version].mkv |
| 1981 | 150 | special | OVERLORD (L15-L22)/Extras/Specials/Season 2/Overlord II - Ple Ple Pleiades 2/Overlord II - Ple Ple Pleiades 2 - 01.mkv |
| 1982 | 150 | special | OVERLORD (L15-L22)/Extras/Specials/Season 2/Overlord II - Ple Ple Pleiades 2/Overlord II - Ple Ple Pleiades 2 - 02.mkv |
| 1983 | 150 | special | OVERLORD (L15-L22)/Extras/Specials/Season 2/Overlord II - Ple Ple Pleiades 2/Overlord II - Ple Ple Pleiades 2 - 03.mkv |
| 1984 | 150 | special | OVERLORD (L15-L22)/Extras/Specials/Season 2/Overlord II - Ple Ple Pleiades 2/Overlord II - Ple Ple Pleiades 2 - 04.mkv |
| 1985 | 150 | special | OVERLORD (L15-L22)/Extras/Specials/Season 2/Overlord II - Ple Ple Pleiades 2/Overlord II - Ple Ple Pleiades 2 - 05.mkv |
| 1986 | 150 | special | OVERLORD (L15-L22)/Extras/Specials/Season 2/Overlord II - Ple Ple Pleiades 2/Overlord II - Ple Ple Pleiades 2 - 06.mkv |
| 1987 | 150 | special | OVERLORD (L15-L22)/Extras/Specials/Season 2/Overlord II - Ple Ple Pleiades 2/Overlord II - Ple Ple Pleiades 2 - 07.mkv |
| 1988 | 150 | special | OVERLORD (L15-L22)/Extras/Specials/Season 2/Overlord II - Ple Ple Pleiades 2/Overlord II - Ple Ple Pleiades 2 - 08.mkv |
| 1989 | 150 | special | OVERLORD (L15-L22)/Extras/Specials/Season 2/Overlord II - Ple Ple Pleiades 2/Overlord II - Ple Ple Pleiades 2 - 09.mkv |
| 1990 | 150 | special | OVERLORD (L15-L22)/Extras/Specials/Season 2/Overlord II - Ple Ple Pleiades 2/Overlord II - Ple Ple Pleiades 2 - 10.mkv |
| 1991 | 150 | special | OVERLORD (L15-L22)/Extras/Specials/Season 2/Overlord II - Ple Ple Pleiades 2/Overlord II - Ple Ple Pleiades 2 - 11.mkv |
| 1992 | 150 | special | OVERLORD (L15-L22)/Extras/Specials/Season 2/Overlord II - Ple Ple Pleiades 2/Overlord II - Ple Ple Pleiades 2 - 12.mkv |
| 1993 | 150 | special | OVERLORD (L15-L22)/Extras/Specials/Season 2/Overlord II - Ple Ple Pleiades 2/Overlord II - Ple Ple Pleiades 2 - 13.mkv |
| 2715 | 203 | preview | Tenki no Ko (FILM)/CM&PV/[Beatrice-Raws] Tenki no Ko (PV 01) [BDRip 1920x1080 HEVC FLAC].mkv |
| 2716 | 203 | preview | Tenki no Ko (FILM)/CM&PV/[Beatrice-Raws] Tenki no Ko (PV 02) [BDRip 1920x1080 HEVC FLAC].mkv |
| 2717 | 203 | preview | Tenki no Ko (FILM)/CM&PV/[Beatrice-Raws] Tenki no Ko (PV 03) [BDRip 1920x1080 HEVC FLAC].mkv |
| 2808 | 271 | nced | Tensei Shitara Slime Datta Ken (P18-L21)/Extras/[Judas] Tensei Shitara Slime Datta Ken - NCED 02a.mkv |
| 2809 | 271 | nced | Tensei Shitara Slime Datta Ken (P18-L21)/Extras/[Judas] Tensei Shitara Slime Datta Ken - NCED 02b.mkv |

Zbývající nečíslované singletony jsou také unresolved pro budoucí rename:

| Video.id | Title ID | Typ | Path |
|---|---|---|---|
| 51 | 245 | preview | Ansatsu Kyoushitsu (Z15-Z16)/Serie 1 (Z15)/Ansatsu Kyoushitsu 00.mp4 |
| 115 | 287 | special | Arifureta Shokugyou de Sekai Saikyou (L19-Z22)/Arifureta Shokugyou de Sekai Saikyou SP Episode 00 Prologue.mkv |
| 142 | 290 | special | Arifureta Shokugyou de Sekai Saikyou (L19-Z22)/Season 2 (Z22)/Arifureta Shokugyou de Sekai Saikyou S2 - 13.mkv |
| 217 | 273 | nced | Bikini Warriors (L15)/Bikini Warriors - NCED.mkv |
| 506 | 246 | preview | Fate Grand Order (Z19)/Fate Grand Order - Absolute Demonic Front Babylonia - 00.mkv |
| 625 | 294 | nced | Getsuyoubi no Tawawa P16-P21/Season 1 P16/NC/[Beatrice-Raws] Getsuyoubi no Tawawa (Creditless ED) [BDRip 1920x1080 HEVC TrueHD].mkv |
| 773 | 275 | special | Hataraku Saibou (L18-Z21)/Serie 1 (L18)/S01E14 [SP]-The Common Cold.mkv |
| 834 | 299 | special | High School DxD (Z12-J18)/High School DxD Hero (J18)/High School DxD Hero - 00.mkv |
| 860 | 62 | ed | High School DxD (Z12-J18)/NC/High School DxD/ED.mkv |
| 861 | 62 | op | High School DxD (Z12-J18)/NC/High School DxD/OP.mkv |
| 862 | 233 | ed | High School DxD (Z12-J18)/NC/High School DxD Born/ED.mkv |
| 865 | 64 | ed | High School DxD (Z12-J18)/NC/High School DxD Hero/ED.mkv |
| 1530 | 251 | special | Kono Yo no Hate de Koi wo Utau Shoujo YU-NO J19 cz-xx%/S01E26.5-SP.mkv |
| 1544 | 239 | nced | Kore wa Zombie Desuka (Z11-J12)/Kore wa Zombie Desuka (Z11)/[Exiled-Destiny]_Is_This_A_Zombie_NCED_[BD_1080p_10bit]_(8CF17F43).mkv |
| 1545 | 239 | ncop | Kore wa Zombie Desuka (Z11-J12)/Kore wa Zombie Desuka (Z11)/[Exiled-Destiny]_Is_This_A_Zombie_NCOP_[BD_1080p_10bit]_(D22BF84B).mkv |
| 1851 | 286 | special | Nande Koko ni Sensei ga (J19)/Nande Koko ni Sensei ga! - 13.mp4 |
| 1980 | 149 | ova | OVERLORD (L15-L22)/Extras/Specials/Season 1/OVA/Overlord Ple Ple Pleiades - OVA.mkv |
| 2009 | 153 | nced | OVERLORD (L15-L22)/Overlord (L15)/NC/NCED.mkv |
| 2010 | 153 | ncop | OVERLORD (L15-L22)/Overlord (L15)/NC/NCOP.mkv |
| 2024 | 155 | nced | OVERLORD (L15-L22)/Overlord II (Z18)/NC/NCED.mkv |
| 2025 | 155 | ncop | OVERLORD (L15-L22)/Overlord II (Z18)/NC/NCOP.mkv |
| 2039 | 157 | nced | OVERLORD (L15-L22)/Overlord III (L18)/NC/NCED.mkv |
| 2040 | 157 | ncop | OVERLORD (L15-L22)/Overlord III (L18)/NC/NCOP.mkv |
| 2622 | 266 | preview | Sword Art Online (L12-L20)/Serie 4 (P19-L20)/Sword Art Online - Alicization - War of Underworld - 00.mkv |
| 2714 | 203 | cm | Tenki no Ko (FILM)/CM&PV/[Beatrice-Raws] Tenki no Ko (CM collection) [BDRip 1920x1080 HEVC FLAC].mkv |
| 2805 | 272 | nced | Tensei Shitara Slime Datta Ken (P18-L21)/Extras/[Asakura] Tensei Shitara Slime Datta Ken 2nd Season NCED [BDRip 1920x1080 x265 10bit FLAC].mkv |
| 2806 | 272 | ncop | Tensei Shitara Slime Datta Ken (P18-L21)/Extras/[Asakura] Tensei Shitara Slime Datta Ken 2nd Season NCOP [BDRip 1920x1080 x265 10bit FLAC].mkv |

## Ochrana dat

Produkční DB: size `5939200`, mtime_ns `1788626923084703246`,
SHA-256 `cf34189609f3f4d750a3238e69a198127c6da76d8f06b09086f1eacd2d8fe627`. Otisk před/po read-only auditu je shodný.
NAS nebyl skenován ani zapisován; pouze stat kořenových cest pro neinvazivní baseline.

Nálezy mimo scope: existující zařazení Interview/Drama CD a 13 Special videí v title Season 2 může vyžadovat ruční kontrolu klasifikace; tento úkol ji nemění. Provider count fallbacky pro jedno fyzické supplementary video nebo starší uložené číslování zůstávají advisory a nejsou V6 ordinal authority.

## Validace a změněné soubory

- Nezměněný cílený baseline: 150 passed.
- Cílené numbering/parser/variants/metadata/hierarchy/performance kontroly: 556 passed;
  navazující regression sady: 139 a 259 passed. Závěrečný celý suite nad finálním kódem:
  **1 251 passed za 155,70 s**.
- Nové testy pokrývají autoritu, kontejnery, raw Episode 14, typové namespaces,
  Media Parts, varianty, potvrzené kopie, kolize, chybějící primary, title scope,
  unnumbered Nande, in-memory read a skutečný title GET. Parserová práce inventory je
  přesně N volání pro N=10 a N=100. Resolver nad raiseload daty provádí 0 SQL;
  GET provádí 0 DML a zachovává úplný sémantický snapshot. Existing query-count
  a read-only invariants prošly v celém suite.
- Python compileall pro app/tests a načtení všech 17 Jinja2 šablon prošly.
  `git diff --check` prošel. Schéma/migrace nebyly změněny.
- Finální produkční inventura je shodná s úvodní. Size, mtime_ns i SHA-256 DB
  odpovídají baseline uvedené výše; kontrolované NAS root stat údaje jsou shodné.
  Produkční scan, zápisy do NAS, commit ani push neproběhly.

Upraveno: `README.md`, `app/catalog.py`, `app/main.py`, `app/metadata/candidates.py`,
`app/numbering.py`, `app/templates/hierarchy_review_detail.html`,
`app/templates/series.html`, `docs/PROJECT_STATUS.md`, `tests/test_metadata_split.py`,
`tests/test_performance_invariants.py`, `tests/test_video_variant_logic.py`.
Nové: `app/supplementary.py`, `app/tools/audit_supplementary.py`,
`tests/test_supplementary_ordinals.py` a tento auditní dokument.
