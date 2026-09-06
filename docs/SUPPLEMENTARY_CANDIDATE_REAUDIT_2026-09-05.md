# Úzký re-audit devíti supplementary skupin – 5. září 2026

Navazuje na [původní audit](SUPPLEMENTARY_ORDINAL_AUDIT_2026-09-05.md), který je historickým
snapshotem před tímto parserovým doplněním. Výchozí HEAD `5211758`, `main = origin/main`;
předchozí necommitnutý worktree byl zachován. Produkční DB byla otevřena jen přes SQLite
`mode=ro`; NAS nebyl skenován ani zapisován. Žádná schema změna, hierarchy cleanup,
manual authority, rename nebo V6 planner.

## Výsledek všech devíti původních skupin

„Chybí“ níže znamená počet fyzických videí bez bezpečného ordinalu.
Číslo samo nepotvrzuje, že shodně očíslované fyzické soubory jsou varianty nebo kopie.

| Title/type | Video | Chybí před → po | Číselná evidence | Výsledek | Proč |
|---|---|---|---|---|---|
| 66 / special | 11 | 5 → 0 | Ano: Special 01–06 a _bd_spec_02–06 | Review: ordinal kolize | Parser neznal úzký legacy suffix _bd_spec_NN. Nově ordinal 2–6; pět kolizí s existujícími Special02–06 bez variant/duplicate authority. |
| 279 / special | 2 | 2 → 2 | IV01/02: číslo jiného/nepotvrzeného subtype | Review: chybí ordinal | IV je v existujícím parseru záměrně unknown marker. Interview title není autorita převodu IV01 na Special01; review zůstává. |
| 276 / nced | 5 | 1 → 1 | Ano u [NCOPnn]/[NCEDnn]; ne u Clean Opening/Ending | Review: chybí ordinal | Číslované soubory se parsovaly už dříve. Clean Opening/Ending neobsahují ordinal; Serie 1 a L18 jsou season/period context, nikoli pořadí. Bez vazby nelze přiřadit k očíslovaným souborům. |
| 276 / ncop | 3 | 1 → 1 | Ano u [NCOPnn]/[NCEDnn]; ne u Clean Opening/Ending | Review: chybí ordinal | Číslované soubory se parsovaly už dříve. Clean Opening/Ending neobsahují ordinal; Serie 1 a L18 jsou season/period context, nikoli pořadí. Bez vazby nelze přiřadit k očíslovaným souborům. |
| 285 / ncop | 8 | 4 → 0 | Ano: NCOP1–4 a NCOP Ver.TV1–4 | Vyřešeno | Doplněn přesný ending suffix TYPE Ver.TVn pouze pro OP/ED/NCOP/NCED. Potvrzené lanes 8/9 ve stejném title dovolují čtyři logical identity; žádný write pairing. |
| 147 / special | 4 | 4 → 4 | Vol.1/2 a II mají jiný význam | Review: chybí ordinal | Heterogenní Drama CD: Special Edition, sequel II a volume 1/2. Není bezpečná jediná Special sekvence; review zůstává. |
| 150 / special | 13 | 13 → 13 | Ano jako standardní source episode 01–13; ne explicitní Special ordinal | Review: chybí ordinal | Parser už vrací standard číslo 1–13. Special pochází z cesty; II, Season 2 a Pleiades 2 jsou jiné osy. Převod na lokální Special by překročil raw-vs-ordinal invariant; bez ruční autority zůstává review. |
| 203 / preview | 3 | 3 → 0 | Ano: samostatné (PV 01), (PV 02), (PV 03) | Vyřešeno | Parser znal hranaté [PV 01] a bare PV01, ale ne uzavřené kulaté (PV 01) před technickými tagy. Nově Preview/PV ordinaly 1–3. CM collection se nečísluje společně s PV. |
| 271 / nced | 3 | 2 → 0 | Ano: NCED 01, NCED 02a/02b | Review: ordinal kolize | Ordinal 2 je oddělen od raw A/B markeru. Bez confirmed variant nebo Media Part authority zůstává jedna kolize NCED02, žádné tiché sloučení. |

Původní auditní filtr `více videí stejného typu + chybějící ordinal`: **9 → 5 skupin**.
Z tohoto filtru vypadly 66/Special, 285/NCOP, 203/Preview-PV a 271/NCED, protože mají čísla.
**K review ale zůstává 7 původních skupin**: pět bez ordinalu a dvě se známými kolizemi.
Úplně se vyřešily pouze 285/NCOP a 203/Preview-PV.

Celá produkční knihovna: nadále 3 100 videí, z toho 161 sledovaných supplementary videí.
S bezpečnou ordinal evidencí **99 → 113**, bez ordinalu **62 → 48**. Změnila se pouze
read-only interpretace 14 filenames. Je evidováno šest kolizí známého ordinalu:
pět Special02–06 v title 66 a jedna NCED02 v title 271. Nic nebylo sloučeno ani persistováno.

## Přesná příčina a minimální oprava parseru

Původní `SUPPLEMENTARY_SEQUENCE` dovoloval číslo hned za typem (případně P/EPISODE/EP/E),
nikoli vložené `Ver.TV`. `_exact_supplementary_detection()` proto u Nande zachytil pouze
`EXPLICIT_NCOP_NCED_MARKER` a vrátil unnumbered NCOP dříve, než se mohlo uplatnit jiné pravidlo.

Změna je pouze v existujícím parseru `app/catalog.py`; supplementary resolver, numbering,
variant workflow, Media Part authority i schéma zůstávají v tomto follow-upu beze změny:

- Tokenově ohraničené `OP|ED|NCOP|NCED` + mezera + přesné `Ver.TV` + kladné číslo 1–99
  na konci filename stemu. Vrací supplementary ordinal a existující `version_hint="Ver.TV"`.
  Nezpracovává samotné `Ver.TV1`, OVA/Special kontext, release tagy, codec/rozlišení,
  další suffixy ani čísla 1080/264. TV nikdy není číslem a nevytváří canonical episode.
- Uzavřený kulatý `(PV nn)` marker, včetně `(PV01)`. Bere výhradně číslo uvnitř markeru;
  `[BDRip 1920x1080 HEVC FLAC]` mimo něj je ignorováno. Namespace je stále Preview/PV.
- Přesný koncový `_bd_spec_NN` suffix. Alias `spec` se nepřidává globálně; neplatí pro
  obecné `spec`, volume, season ani release číslo. V reálné skupině je explicitní řada
  těchto Special suffixů 02–06 vedle již označených Special01–06. Nové číslo odhalí kolizi,
  ale nepotvrdí totožnost dvou fyzických souborů.
- Přesný koncový OP/ED/NCOP/NCED + dvouciferné číslo + A/B suffix. Číslo je ordinal,
  písmeno zůstává v existujícím `structural_marker`. Jeho význam (varianta či segment)
  se nepotvrzuje; bez ručních vztahů se dvě NCED02 nesloučí.

Generic source epizody, IV, volume, season a roky/period hints z cest nejsou převedeny
na supplementary ordinal. Manual číslo i manual classification mají nad novou evidencí přednost.

## Nande Koko – rozpoznání a stávající autorita

Title 285 má dvě již uložené ručně ověřené groups:

- group 8: `catalog_title_id=285`, label BD, release_source bd, content_variant uncensored,
  verified_at `2026-09-05 15:53:32.846081`;
- group 9: `catalog_title_id=285`, label TV, release_source tv, content_variant censored,
  verified_at `2026-09-05 15:53:45.376352`.

NCOP s ordinalem **4/8 → 8/8**. TV Video 1857–1860 nyní mají ordinaly **1, 2, 3, 4**
a se stávajícími BD videi 1861–1864 představují čtyři logické NCOP identity se dvěma
potvrzenými lanes. Title obsahuje navíc pět již očíslovaných NCED; celkově tedy
9 bezpečných logických supplementary identit / 13 fyzických NC videí.

Změnilo se **pouze ordinal recognition**. Nevznikla nová group, assignment, verified_at
ani pairing autorita. Read-model využil původní autoritu. Bez ní stejná filename dvojice
zůstává review; nový regression test tuto hranici ověřuje.

## Všechna videa devíti skupin

`—` znamená NULL / nepřítomnou hodnotu. Raw type, manual číslo, Media Part, duplicate relation
ani variant lane se neměnily. Parser a effective hodnoty jsou uvedeny před → po opravě.
U všech 52 videí je Media Part NULL, duplicate_of_video_id NULL, duplicate_primary_missing false.
Video 878–882 mají navíc ruční `duplicate_status_manual=suspected`; ostatní sledovaná videa NULL.
Ruční podezření není confirmed duplicate relation, a proto kolize Special02–06 zůstávají review.

### 66 / special – Specials – High School DxD - Specials

| Video.id | relative_path | Raw type | Parser před → po | Manual číslo | Effective před → po | Media Part | Duplicate relation | Variant group / lane | Token / pattern | Důvod |
|---|---|---|---|---|---|---|---|---|---|---|
| 872 | High School DxD (Z12-J18)/Specials/High School DxD - Specials/[Anime Time] High School DxD - Special 01.mkv | special | 1 → 1 | 1 | 1 → 1 | — | — | — | Special 01 | Ruční číslo 1 má přednost před parserem. |
| 873 | High School DxD (Z12-J18)/Specials/High School DxD - Specials/[Anime Time] High School DxD - Special 02.mkv | special | 2 → 2 | 2 | 2 → 2 | — | — | — | Special 02 | Ruční číslo 2 má přednost před parserem. |
| 874 | High School DxD (Z12-J18)/Specials/High School DxD - Specials/[Anime Time] High School DxD - Special 03.mkv | special | 3 → 3 | 3 | 3 → 3 | — | — | — | Special 03 | Ruční číslo 3 má přednost před parserem. |
| 875 | High School DxD (Z12-J18)/Specials/High School DxD - Specials/[Anime Time] High School DxD - Special 04.mkv | special | 4 → 4 | 4 | 4 → 4 | — | — | — | Special 04 | Ruční číslo 4 má přednost před parserem. |
| 876 | High School DxD (Z12-J18)/Specials/High School DxD - Specials/[Anime Time] High School DxD - Special 05.mkv | special | 5 → 5 | 5 | 5 → 5 | — | — | — | Special 05 | Ruční číslo 5 má přednost před parserem. |
| 877 | High School DxD (Z12-J18)/Specials/High School DxD - Specials/[Anime Time] High School DxD - Special 06.mkv | special | 6 → 6 | 6 | 6 → 6 | — | — | — | Special 06 | Ruční číslo 6 má přednost před parserem. |
| 878 | High School DxD (Z12-J18)/Specials/High School DxD - Specials/high_scool_dxd_bd_spec_02.mp4 | special | — → 2 | — | — → 2 | — | primary=—; manual suspected | — | _bd_spec_02 | Chyběl suffix _bd_spec_NN; nově Special ordinal, ale stejné číslo už má další video bez confirmed vztahu. |
| 879 | High School DxD (Z12-J18)/Specials/High School DxD - Specials/high_scool_dxd_bd_spec_03.mp4 | special | — → 3 | — | — → 3 | — | primary=—; manual suspected | — | _bd_spec_03 | Chyběl suffix _bd_spec_NN; nově Special ordinal, ale stejné číslo už má další video bez confirmed vztahu. |
| 880 | High School DxD (Z12-J18)/Specials/High School DxD - Specials/high_scool_dxd_bd_spec_04.mp4 | special | — → 4 | — | — → 4 | — | primary=—; manual suspected | — | _bd_spec_04 | Chyběl suffix _bd_spec_NN; nově Special ordinal, ale stejné číslo už má další video bez confirmed vztahu. |
| 881 | High School DxD (Z12-J18)/Specials/High School DxD - Specials/high_scool_dxd_bd_spec_05.mp4 | special | — → 5 | — | — → 5 | — | primary=—; manual suspected | — | _bd_spec_05 | Chyběl suffix _bd_spec_NN; nově Special ordinal, ale stejné číslo už má další video bez confirmed vztahu. |
| 882 | High School DxD (Z12-J18)/Specials/High School DxD - Specials/high_scool_dxd_bd_spec_06.mp4 | special | — → 6 | — | — → 6 | — | primary=—; manual suspected | — | _bd_spec_06 | Chyběl suffix _bd_spec_NN; nově Special ordinal, ale stejné číslo už má další video bez confirmed vztahu. |

### 279 / special – Interview - Isekai Maou to Shoukan Shoujo no Dorei Majutsu

| Video.id | relative_path | Raw type | Parser před → po | Manual číslo | Effective před → po | Media Part | Duplicate relation | Variant group / lane | Token / pattern | Důvod |
|---|---|---|---|---|---|---|---|---|---|---|
| 985 | Isekai Maou to Shoukan Shoujo no Dorei Majutsu (L18-J21)/SPs/[Anipakku] Isekai Maou to Shoukan Shoujo no Dorei Majutsu [IV01][Ma10p_1080p][x265_aac].mkv | other | — → — | — | — → — | — | — | — | [IV01] | IV není potvrzený Special subtype; 1080p/x265 jsou technické tagy. |
| 986 | Isekai Maou to Shoukan Shoujo no Dorei Majutsu (L18-J21)/SPs/[Anipakku] Isekai Maou to Shoukan Shoujo no Dorei Majutsu [IV02][Ma10p_1080p][x265_aac].mkv | other | — → — | — | — → — | — | — | — | [IV02] | IV není potvrzený Special subtype; 1080p/x265 jsou technické tagy. |

### 276 / nced – NC - Isekai Maou to Shoukan Shoujo no Dorei Majutsu

| Video.id | relative_path | Raw type | Parser před → po | Manual číslo | Effective před → po | Media Part | Duplicate relation | Variant group / lane | Token / pattern | Důvod |
|---|---|---|---|---|---|---|---|---|---|---|
| 990 | Isekai Maou to Shoukan Shoujo no Dorei Majutsu (L18-J21)/SPs/[Anipakku] Isekai Maou to Shoukan Shoujo no Dorei Majutsu [NCED01][Ma10p_1080p][x265_flac].mkv | nced | 1 → 1 | — | 1 → 1 | — | — | — | [NCED01] | Explicitní supplementary marker s číslem rozpoznán již před opravou. |
| 991 | Isekai Maou to Shoukan Shoujo no Dorei Majutsu (L18-J21)/SPs/[Anipakku] Isekai Maou to Shoukan Shoujo no Dorei Majutsu [NCED02][Ma10p_1080p][x265_flac].mkv | nced | 2 → 2 | — | 2 → 2 | — | — | — | [NCED02] | Explicitní supplementary marker s číslem rozpoznán již před opravou. |
| 992 | Isekai Maou to Shoukan Shoujo no Dorei Majutsu (L18-J21)/SPs/[Anipakku] Isekai Maou to Shoukan Shoujo no Dorei Majutsu [NCED03][Ma10p_1080p][x265_flac].mkv | nced | 3 → 3 | — | 3 → 3 | — | — | — | [NCED03] | Explicitní supplementary marker s číslem rozpoznán již před opravou. |
| 993 | Isekai Maou to Shoukan Shoujo no Dorei Majutsu (L18-J21)/SPs/[Anipakku] Isekai Maou to Shoukan Shoujo no Dorei Majutsu [NCED04][Ma10p_1080p][x265_flac].mkv | nced | 4 → 4 | — | 4 → 4 | — | — | — | [NCED04] | Explicitní supplementary marker s číslem rozpoznán již před opravou. |
| 1010 | Isekai Maou to Shoukan Shoujo no Dorei Majutsu (L18-J21)/Serie 1 (L18)/[Judas] How Not To Summon A Demon Lord - Clean Ending.mkv | nced | — → — | — | — → — | — | — | — | Clean Ending | Clean Opening/Ending označuje typ bez čísla. Číslo v Serie 1 je season context. |

### 276 / ncop – NC - Isekai Maou to Shoukan Shoujo no Dorei Majutsu

| Video.id | relative_path | Raw type | Parser před → po | Manual číslo | Effective před → po | Media Part | Duplicate relation | Variant group / lane | Token / pattern | Důvod |
|---|---|---|---|---|---|---|---|---|---|---|
| 994 | Isekai Maou to Shoukan Shoujo no Dorei Majutsu (L18-J21)/SPs/[Anipakku] Isekai Maou to Shoukan Shoujo no Dorei Majutsu [NCOP01][Ma10p_1080p][x265_flac].mkv | ncop | 1 → 1 | — | 1 → 1 | — | — | — | [NCOP01] | Explicitní supplementary marker s číslem rozpoznán již před opravou. |
| 995 | Isekai Maou to Shoukan Shoujo no Dorei Majutsu (L18-J21)/SPs/[Anipakku] Isekai Maou to Shoukan Shoujo no Dorei Majutsu [NCOP02][Ma10p_1080p][x265_flac].mkv | ncop | 2 → 2 | — | 2 → 2 | — | — | — | [NCOP02] | Explicitní supplementary marker s číslem rozpoznán již před opravou. |
| 1011 | Isekai Maou to Shoukan Shoujo no Dorei Majutsu (L18-J21)/Serie 1 (L18)/[Judas] How Not To Summon A Demon Lord - Clean Opening.mkv | ncop | — → — | — | — → — | — | — | — | Clean Opening | Clean Opening/Ending označuje typ bez čísla. Číslo v Serie 1 je season context. |

### 285 / ncop – NC - Nande Koko ni Sensei ga

| Video.id | relative_path | Raw type | Parser před → po | Manual číslo | Effective před → po | Media Part | Duplicate relation | Variant group / lane | Token / pattern | Důvod |
|---|---|---|---|---|---|---|---|---|---|---|
| 1857 | Nande Koko ni Sensei ga (J19)/Nande Koko ni Sensei ga! - NCOP Ver.TV1.mp4 | ncop | — → 1 | — | — → 1 | — | — | 9 / TV / censored | NCOP Ver.TV1 | Ver.TV přerušovalo původní TYPE + číslo regex; unnumbered NCOP fallback ukončil parser. Nově 1–4 + version_hint Ver.TV; lane už potvrzena. |
| 1858 | Nande Koko ni Sensei ga (J19)/Nande Koko ni Sensei ga! - NCOP Ver.TV2.mp4 | ncop | — → 2 | — | — → 2 | — | — | 9 / TV / censored | NCOP Ver.TV2 | Ver.TV přerušovalo původní TYPE + číslo regex; unnumbered NCOP fallback ukončil parser. Nově 1–4 + version_hint Ver.TV; lane už potvrzena. |
| 1859 | Nande Koko ni Sensei ga (J19)/Nande Koko ni Sensei ga! - NCOP Ver.TV3.mp4 | ncop | — → 3 | — | — → 3 | — | — | 9 / TV / censored | NCOP Ver.TV3 | Ver.TV přerušovalo původní TYPE + číslo regex; unnumbered NCOP fallback ukončil parser. Nově 1–4 + version_hint Ver.TV; lane už potvrzena. |
| 1860 | Nande Koko ni Sensei ga (J19)/Nande Koko ni Sensei ga! - NCOP Ver.TV4.mp4 | ncop | — → 4 | — | — → 4 | — | — | 9 / TV / censored | NCOP Ver.TV4 | Ver.TV přerušovalo původní TYPE + číslo regex; unnumbered NCOP fallback ukončil parser. Nově 1–4 + version_hint Ver.TV; lane už potvrzena. |
| 1861 | Nande Koko ni Sensei ga (J19)/Nande Koko ni Sensei ga! - NCOP1.mp4 | ncop | 1 → 1 | — | 1 → 1 | — | — | 8 / BD / uncensored | NCOP1 | Explicitní supplementary marker s číslem rozpoznán již před opravou. |
| 1862 | Nande Koko ni Sensei ga (J19)/Nande Koko ni Sensei ga! - NCOP2.mp4 | ncop | 2 → 2 | — | 2 → 2 | — | — | 8 / BD / uncensored | NCOP2 | Explicitní supplementary marker s číslem rozpoznán již před opravou. |
| 1863 | Nande Koko ni Sensei ga (J19)/Nande Koko ni Sensei ga! - NCOP3.mp4 | ncop | 3 → 3 | — | 3 → 3 | — | — | 8 / BD / uncensored | NCOP3 | Explicitní supplementary marker s číslem rozpoznán již před opravou. |
| 1864 | Nande Koko ni Sensei ga (J19)/Nande Koko ni Sensei ga! - NCOP4.mp4 | ncop | 4 → 4 | — | 4 → 4 | — | — | 8 / BD / uncensored | NCOP4 | Explicitní supplementary marker s číslem rozpoznán již před opravou. |

### 147 / special – Extras – Drama CD

| Video.id | relative_path | Raw type | Parser před → po | Manual číslo | Effective před → po | Media Part | Duplicate relation | Variant group / lane | Token / pattern | Důvod |
|---|---|---|---|---|---|---|---|---|---|---|
| 1968 | OVERLORD (L15-L22)/Extras/Drama CD/Overlord Drama CD Special Edition [The Maid Tea Party].mkv | special | — → — | — | — → — | — | — | — | Special Edition | Special Edition je popis vydání; pořadové číslo chybí. |
| 1969 | OVERLORD (L15-L22)/Extras/Drama CD/Overlord II Special Voice Drama CD [Visual Version].mkv | special | — → — | — | — → — | — | — | — | II | II je sequel/season context, ne Special02. |
| 1970 | OVERLORD (L15-L22)/Extras/Drama CD/Overlord Special Voice Drama CD Vol.1 [Visual Version].mkv | special | — → — | — | — → — | — | — | — | Vol.1 | Číslo označuje volume Drama CD, ne Special ordinal celé heterogenní části. |
| 1971 | OVERLORD (L15-L22)/Extras/Drama CD/Overlord Special Voice Drama CD Vol.2 [Visual Version].mkv | special | — → — | — | — → — | — | — | — | Vol.2 | Číslo označuje volume Drama CD, ne Special ordinal celé heterogenní části. |

### 150 / special – Season 2

| Video.id | relative_path | Raw type | Parser před → po | Manual číslo | Effective před → po | Media Part | Duplicate relation | Variant group / lane | Token / pattern | Důvod |
|---|---|---|---|---|---|---|---|---|---|---|
| 1981 | OVERLORD (L15-L22)/Extras/Specials/Season 2/Overlord II - Ple Ple Pleiades 2/Overlord II - Ple Ple Pleiades 2 - 01.mkv | special | — → — | — | — → — | — | — | — |  - 01 | Standardní source episode suffix se již parsuje; raw file_type Special je z adresáře. Chybí explicitní lokální Special authority. |
| 1982 | OVERLORD (L15-L22)/Extras/Specials/Season 2/Overlord II - Ple Ple Pleiades 2/Overlord II - Ple Ple Pleiades 2 - 02.mkv | special | — → — | — | — → — | — | — | — |  - 02 | Standardní source episode suffix se již parsuje; raw file_type Special je z adresáře. Chybí explicitní lokální Special authority. |
| 1983 | OVERLORD (L15-L22)/Extras/Specials/Season 2/Overlord II - Ple Ple Pleiades 2/Overlord II - Ple Ple Pleiades 2 - 03.mkv | special | — → — | — | — → — | — | — | — |  - 03 | Standardní source episode suffix se již parsuje; raw file_type Special je z adresáře. Chybí explicitní lokální Special authority. |
| 1984 | OVERLORD (L15-L22)/Extras/Specials/Season 2/Overlord II - Ple Ple Pleiades 2/Overlord II - Ple Ple Pleiades 2 - 04.mkv | special | — → — | — | — → — | — | — | — |  - 04 | Standardní source episode suffix se již parsuje; raw file_type Special je z adresáře. Chybí explicitní lokální Special authority. |
| 1985 | OVERLORD (L15-L22)/Extras/Specials/Season 2/Overlord II - Ple Ple Pleiades 2/Overlord II - Ple Ple Pleiades 2 - 05.mkv | special | — → — | — | — → — | — | — | — |  - 05 | Standardní source episode suffix se již parsuje; raw file_type Special je z adresáře. Chybí explicitní lokální Special authority. |
| 1986 | OVERLORD (L15-L22)/Extras/Specials/Season 2/Overlord II - Ple Ple Pleiades 2/Overlord II - Ple Ple Pleiades 2 - 06.mkv | special | — → — | — | — → — | — | — | — |  - 06 | Standardní source episode suffix se již parsuje; raw file_type Special je z adresáře. Chybí explicitní lokální Special authority. |
| 1987 | OVERLORD (L15-L22)/Extras/Specials/Season 2/Overlord II - Ple Ple Pleiades 2/Overlord II - Ple Ple Pleiades 2 - 07.mkv | special | — → — | — | — → — | — | — | — |  - 07 | Standardní source episode suffix se již parsuje; raw file_type Special je z adresáře. Chybí explicitní lokální Special authority. |
| 1988 | OVERLORD (L15-L22)/Extras/Specials/Season 2/Overlord II - Ple Ple Pleiades 2/Overlord II - Ple Ple Pleiades 2 - 08.mkv | special | — → — | — | — → — | — | — | — |  - 08 | Standardní source episode suffix se již parsuje; raw file_type Special je z adresáře. Chybí explicitní lokální Special authority. |
| 1989 | OVERLORD (L15-L22)/Extras/Specials/Season 2/Overlord II - Ple Ple Pleiades 2/Overlord II - Ple Ple Pleiades 2 - 09.mkv | special | — → — | — | — → — | — | — | — |  - 09 | Standardní source episode suffix se již parsuje; raw file_type Special je z adresáře. Chybí explicitní lokální Special authority. |
| 1990 | OVERLORD (L15-L22)/Extras/Specials/Season 2/Overlord II - Ple Ple Pleiades 2/Overlord II - Ple Ple Pleiades 2 - 10.mkv | special | — → — | — | — → — | — | — | — |  - 10 | Standardní source episode suffix se již parsuje; raw file_type Special je z adresáře. Chybí explicitní lokální Special authority. |
| 1991 | OVERLORD (L15-L22)/Extras/Specials/Season 2/Overlord II - Ple Ple Pleiades 2/Overlord II - Ple Ple Pleiades 2 - 11.mkv | special | — → — | — | — → — | — | — | — |  - 11 | Standardní source episode suffix se již parsuje; raw file_type Special je z adresáře. Chybí explicitní lokální Special authority. |
| 1992 | OVERLORD (L15-L22)/Extras/Specials/Season 2/Overlord II - Ple Ple Pleiades 2/Overlord II - Ple Ple Pleiades 2 - 12.mkv | special | — → — | — | — → — | — | — | — |  - 12 | Standardní source episode suffix se již parsuje; raw file_type Special je z adresáře. Chybí explicitní lokální Special authority. |
| 1993 | OVERLORD (L15-L22)/Extras/Specials/Season 2/Overlord II - Ple Ple Pleiades 2/Overlord II - Ple Ple Pleiades 2 - 13.mkv | special | — → — | — | — → — | — | — | — |  - 13 | Standardní source episode suffix se již parsuje; raw file_type Special je z adresáře. Chybí explicitní lokální Special authority. |

### 203 / preview – CM&PV

| Video.id | relative_path | Raw type | Parser před → po | Manual číslo | Effective před → po | Media Part | Duplicate relation | Variant group / lane | Token / pattern | Důvod |
|---|---|---|---|---|---|---|---|---|---|---|
| 2715 | Tenki no Ko (FILM)/CM&PV/[Beatrice-Raws] Tenki no Ko (PV 01) [BDRip 1920x1080 HEVC FLAC].mkv | pv | — → 1 | — | — → 1 | — | — | — | (PV 01) | Kulátý PV marker nebyl podporován. Nově číslo z uzavřeného (PV nn), technické tagy mimo marker ignorovány. |
| 2716 | Tenki no Ko (FILM)/CM&PV/[Beatrice-Raws] Tenki no Ko (PV 02) [BDRip 1920x1080 HEVC FLAC].mkv | pv | — → 2 | — | — → 2 | — | — | — | (PV 02) | Kulátý PV marker nebyl podporován. Nově číslo z uzavřeného (PV nn), technické tagy mimo marker ignorovány. |
| 2717 | Tenki no Ko (FILM)/CM&PV/[Beatrice-Raws] Tenki no Ko (PV 03) [BDRip 1920x1080 HEVC FLAC].mkv | pv | — → 3 | — | — → 3 | — | — | — | (PV 03) | Kulátý PV marker nebyl podporován. Nově číslo z uzavřeného (PV nn), technické tagy mimo marker ignorovány. |

Sousední Video **2714** má raw `cm`, filename `[Beatrice-Raws] Tenki no Ko (CM collection) [BDRip 1920x1080 HEVC FLAC].mkv`, parser/effective ordinal NULL a žádnou manual/variant/Media Part/duplicate autoritu. Zůstává samostatné nečíslované CM; nepatří do PV sekvence.

### 271 / nced – NC - Tensei Shitara Slime Datta Ken

| Video.id | relative_path | Raw type | Parser před → po | Manual číslo | Effective před → po | Media Part | Duplicate relation | Variant group / lane | Token / pattern | Důvod |
|---|---|---|---|---|---|---|---|---|---|---|
| 2807 | Tensei Shitara Slime Datta Ken (P18-L21)/Extras/[Judas] Tensei Shitara Slime Datta Ken - NCED 01.mkv | nced | 1 → 1 | — | 1 → 1 | — | — | — | NCED 01 | Explicitní supplementary marker s číslem rozpoznán již před opravou. |
| 2808 | Tensei Shitara Slime Datta Ken (P18-L21)/Extras/[Judas] Tensei Shitara Slime Datta Ken - NCED 02a.mkv | nced | — → 2 | — | — → 2 | — | — | — | NCED 02a | A/B suffix blokoval integer regex; nově číslo 2 + zachovaný structural_marker. Bez group/Media Part authority zůstává kolize. |
| 2809 | Tensei Shitara Slime Datta Ken (P18-L21)/Extras/[Judas] Tensei Shitara Slime Datta Ken - NCED 02b.mkv | nced | — → 2 | — | — → 2 | — | — | — | NCED 02b | A/B suffix blokoval integer regex; nově číslo 2 + zachovaný structural_marker. Bez group/Media Part authority zůstává kolize. |

## Fingerprint a ověření

Produkční DB před i po auditu: size `5939200`, mtime_ns `1788626923084703246`,
SHA-256 `cf34189609f3f4d750a3238e69a198127c6da76d8f06b09086f1eacd2d8fe627`.


Ověření dokončení (6. září 2026):

- Cílené parser/supplementary/numbering/variant/Media Part/Recap/hierarchy/metadata
  a performance/read-only testy: **605 passed**; po obnovení prostředí znovu 605 passed.
- Celý aktuální suite po obnovení prostředí: **1 293 passed** (158,99 s).
  Stejná sada prošla i před přerušením (1 293 passed, 159,08 s).
- Porovnání parseru před/po nad všemi 3 100 produkčními filenames změnilo právě
  očekávaných 14 detekcí (878–882, 1857–1860, 2715–2717, 2808–2809).
  Klasifikace `classify_video(relative_path)` se nezměnila u žádného videa.
- Compileall pro app/tests a načtení všech 17 Jinja2 šablon prošly.
  `git diff --check` i whitespace kontrola všech pěti untracked souborů prošly.
- Re-audit produkční DB po obnovení prostředí potvrdil stejné počty, zbývající
  skupiny a kolize. Size, mtime_ns a SHA-256 přesně souhlasí s původním otiskem.
  NAS nebyl skenován ani zapisován; root stat údaje zůstaly shodné.

Tento follow-up změnil `app/catalog.py` (+32 řádků v existujícím parseru),
`tests/test_supplementary_ordinals.py`, `README.md`, `docs/PROJECT_STATUS.md`,
přidal tento re-audit a označil původní audit jako historický snapshot.
Číslovací resolver, variantní/duplicate zápisy, Media Part autorita a schéma
se v tomto kroku neměnily. Předchozí necommitnutá implementace zůstala zachována.
Commit ani push nebyly provedeny.

Závěrečný git stav: `main = origin/main`, HEAD `5211758`, 11 modified a 5 untracked
souborů (včetně předchozího supplementary úkolu). Nic nebylo staged, commitnuto ani pushnuto.
