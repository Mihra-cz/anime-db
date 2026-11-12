"""Czech read-only explanations; never infer authority or change planner states."""

MESSAGES = {}


def _group(titles, reason, impact, action):
    for line in titles.strip().splitlines():
        code, title = line.split('|', 1)
        MESSAGES[code] = dict(title=title, reason=reason, impact=impact, action=action)


_group('''
collection_naming_unavailable|Název kolekce zatím nelze určit
collection_unavailable|Chybí jednoznačná kolekce
confirmed_metadata_unavailable|Chybí potvrzená metadata pro název
confirmed_romaji_unavailable|Chybí potvrzený výchozí název
metadata_payload_mismatch|Metadata názvu nesouhlasí s potvrzenou vazbou
metadata_identity_changed|Podklady názvu se změnily
structural_context_changed|Struktura se od potvrzení názvu změnila
structural_context_unavailable|Chybí struktura potřebná pro název
authority_ambiguous|Název nemá jednoznačný podklad
dependency_unresolved|Název závisí na nevyřešené hlavní části
invalid_basis_snapshot|Podklad ručního názvu nelze ověřit
season_only_multiple_parts|Pro tuto strukturu je potřeba ruční název
title_naming_unavailable|Název části zatím nelze určit
own_folder_naming_unavailable|Chybí bezpečný název samostatného adresáře
invalid_default_text|Výchozí název není použitelný
''', 'Současné podklady názvu nejsou úplné nebo již neodpovídají potvrzenému rozhodnutí.',
    'Bez jednoznačného názvu nelze bezpečně určit cílovou cestu.',
    'Otevřete kontrolu názvů, ověřte metadata a potvrďte odpovídající název. Poté vytvořte nový náhled.')

_group('''
layout_unavailable|Rozložení souborů není určené
human_layout_required|Rozložení vyžaduje vaše rozhodnutí
invalid_layout_kind|Zvolené rozložení není pro obsah platné
basis_mismatch|Podklady uloženého rozložení se změnily
direct_season_requires_season|Uložení v sezóně vyžaduje potvrzenou sezónu
invalid_structural_attachment|Není určené připojení k hlavní části
not_supplementary_layout_owner|Tato část nepoužívá rozložení doplňků
logical_count_unresolved|Počet samostatných položek není vyřešený
owner_unavailable|Chybí vlastník rozložení
unapproved_container_fanout|Část by se rozdělila do více nepotvrzených umístění
title_locator_unresolved|Cílové umístění části není jednoznačné
''', 'Uložená struktura nebo rozložení nedokládá jednoznačné cílové umístění.',
    'Dotčené soubory čekají na kontrolu; aplikace jejich strukturu nebude odhadovat.',
    'Ověřte hierarchii a kontrolu rozložení. Potvrďte zamýšlené umístění a obnovte náhled.')

_group('''
video_owner_unavailable_or_conflicting|Přiřazení videa není jednoznačné
invalid_structural_hierarchy|Hierarchie části není platná
incomplete_manual_snapshot|Ruční hierarchie je neúplná
ambiguous_split_season|Rozdělení sezóny vyžaduje kontrolu
episode_season_unavailable|Epizoda nemá potvrzený kontext sezóny
recap_season_unavailable|Rekapitulace nemá potvrzený kontext sezóny
variant_owner_conflict|Varianta nesouhlasí s přiřazenou částí
variant_label_unavailable|Varianta nemá použitelný název
invalid_season|Číslo sezóny není použitelné
invalid_identity|Číslování není úplné nebo platné
unsupported_identity|Tento tvar číslování nelze použít
video_authority_unavailable|Chybí podklady identity videa
missing_supplementary_ordinal|Chybí pořadové číslo doplňkového obsahu
supplementary_ordinal_collision|Pořadové číslo sdílí nevyřešené položky
broken_supplementary_identity|Vazba duplicity nevede na bezpečnou identitu doplňku
''', 'Současná databázová evidence nestačí k bezpečnému určení identity nebo strukturálního zařazení.',
    'Položka zůstává ke kontrole; její identita se z fyzického názvu nevymýšlí.',
    'Ověřte hierarchii, číslování a variantu v detailu videa. Potom připravte nový náhled.')

_group('''
manual_duplicate_suspected|Podezření na duplicitu čeká na kontrolu
unresolved_duplicate_identity|Stejnou identitu sdílí více nevyřešených souborů
duplicate_primary_target_unavailable|Primární video nemá bezpečný cíl
duplicate_primary_physical_safety_gate|Primární kopie neprošla fyzickým ověřením
duplicate_relation_|Vazbu potvrzené duplicity nelze použít
''', 'Evidence zatím nedokládá použitelnou potvrzenou duplicitu nebo platnou vazbu na primární soubor.',
    'Bez ověření nelze rozhodnout o umístění kopie do karantény. Podezření samo není potvrzenou duplicitou.',
    'V detailu videa ověřte primární soubor a duplicitní vazbu. Obsah knihovny ručně nemažte.')

_group('''
subtitle_placement_unresolved|Titulky nemají jednoznačné umístění
subtitle_scope_unresolved|U titulků není vyřešené cílové zařazení
''', 'Pro tento fyzický soubor titulků není doložená použitelná kompatibilita nebo cílový kontext.',
    'Titulky se automaticky nepřiřadí jinému videu ani variantě.',
    'V kontrole médií ověřte kompatibilitu a cílové zařazení titulků. Poté obnovte náhled.')

MESSAGES['subtitle_present_video_missing'] = dict(
    title='Titulky bez odpovídajícího videa',
    reason='U tohoto souboru titulků je ručně potvrzeno, že v evidované knihovně nemá odpovídající video. Nejde o důkaz, že video neexistuje jinde.',
    impact='Návrh zachová soubor titulků; podle uvedených cest může jít o přesun nebo ponechání na místě. Sám tím nepotvrzuje kompatibilitu s jiným videem.',
    action='Zkontrolujte současné a cílové umístění a potvrďte upozornění, pokud chcete titulky tímto způsobem zachovat.')
MESSAGES['windows_path_budget'] = dict(
    title='Dlouhá cesta pro Windows', reason='Úplná cílová cesta včetně Windows kořene přesahuje limit délky, který reorganizace pro Windows vyžaduje.',
    impact='Dokud cesta limit přesahuje, aktuální ověření návrh zablokuje a reorganizaci nelze schválit ani spustit. Potvrzení upozornění na tom nic nemění. Soubor sám poškozený není.',
    action='Cestu je potřeba zkrátit: v kontrole názvů zkraťte potvrzený název, případně upravte rozložení. Poté vytvořte nový náhled a nový návrh.')

# Only these archive states get the Archive Review form in the preview
# (reorganization_archive_review.archive_review_rows); other side-asset states do not.
_group('''
duplicate_side_asset_association_unresolved|Archiv u duplicit nemá potvrzené přiřazení
''', 'Archiv leží u potvrzených duplicitních videí, ale chybí rozhodnutí, zda patří konkrétní kopii, nebo se má zachovat samostatně.',
    'Archiv se bez rozhodnutí nepřesune do umístění odvozeného od nepotvrzené vazby.',
    'U této položky použijte tlačítko Vyřešit: přiřaďte archiv konkrétní duplicitní kopii, nebo jej zachovejte samostatně v Subs.')

_group('''
archive_review_source_changed|Archiv se od rozhodnutí změnil
archive_review_context_changed|Změnil se kontext přiřazení archivu
archive_review_duplicate_changed|Změnila se vazba archivu na duplicitní kopii
archive_review_target_invalid|Cíl uloženého rozhodnutí o archivu již není platný
''', 'Uložené ruční rozhodnutí o archivu již neodpovídá aktuálnímu souboru, kolekci, duplicitní vazbě nebo cíli.',
    'Uložené rozhodnutí zůstává zachované, ale plán je nepoužije; archiv se podle něj nepřesune.',
    'U této položky použijte tlačítko Změnit / zrušit rozhodnutí: rozhodnutí znovu ověřte, změňte nebo zrušte.')

_group('''
known_side_asset_not_in_inventory|Doložený doprovodný soubor není v inventuře
duplicate_side_asset_evidence_mismatch|Důkaz o doprovodném souboru nesouhlasí
carried_side_asset_evidence_mismatch|Historický důkaz o doprovodném souboru nesouhlasí
side_asset_evidence_conflicts_with_subs_lane|Přiřazení archivu je v konfliktu s umístěním v Subs
accounting_evidence_unavailable|Chybí podklady zachování doprovodných souborů
''', 'Uložená evidence doprovodného souboru (například archivu titulků u duplicitní kopie) nesouhlasí s aktuální inventurou: soubor chybí, změnil se, nebo jeho umístění odporuje zaznamenané vazbě.',
    'Soubor zůstává ke kontrole. Reorganizace jej podle nesouhlasící evidence nepřesune, neumístí do karantény ani nesmaže.',
    'Nejde o rozhodnutí o přiřazení archivu. Na uvedené cestě ověřte, zda soubor existuje a odpovídá údajům v technických podrobnostech, a vyjasněte příčinu rozdílu. Poté vytvořte nový náhled.')

_group('''
unmanaged_or_ambiguous_asset|Soubor není jednoznačně zařazený
unprovenanced_or_unexpected_technical_lane_asset|Soubor v technickém adresáři nemá doložený původ
uncovered_file|Inventura obsahuje soubor bez vyřešeného plánu
plan_record_unresolved|Položka plánu vyžaduje kontrolu
plan_record_not_actionable|Pro položku není bezpečná proveditelná akce
source_missing_or_not_regular|Zdroj chybí nebo není běžným souborem
source_not_regular|Zdroj není ověřený běžný soubor
plan_source_not_in_active_inventory|Zdroj plánu není v aktuální inventuře
duplicate_plan_source|Pro stejný zdroj existuje více plánovaných položek
duplicate_inventory_path|Stejná cesta je v inventuře nejednoznačná
target_or_source_unresolved|Zdrojová nebo cílová cesta není určená
keep_not_justified|Ponechání souboru nemá potřebný podklad
move_without_relocation|Navržený přesun nemění umístění
''', 'Inventura a plán nemají pro tuto položku jednoznačný společný podklad.',
    'Nejasnost brání bezpečnému schválení. Samotná nepřítomnost záznamu v databázi neopravňuje ke smazání.',
    'Zkontrolujte zdroj, jeho evidenci a navržené umístění. Neznámý soubor nejprve ručně posuďte a vytvořte nový náhled.')

_group('''
target_collision|Cílové cesty spolu kolidují
collection_root_collision|Kolekce mají kolidující cílové kořeny
auxiliary_managed_namespace_collision|Doprovodný soubor koliduje s evidovaným obsahem
exact_key|Cílové cesty jsou shodné
fold_key|Cíle se liší jen normalizací nebo velikostí písmen
uppercase_guard|Cíle nejsou rozlišitelné při kontrole velikosti písmen
file_directory|Soubor a adresář mají kolidující cíl
target_occupied|Cílová cesta je obsazená
target_parent_not_directory|Cílový rodič není adresář
unplanned_target_directory|Cílový adresář není v plánu
''', 'Kontrola cílového prostoru zjistila kolizi nebo nepoužitelný rodičovský adresář.',
    'Přesun by nebyl jednoznačný; žádný existující obsah se kvůli tomu nepřepisuje ani nemaže.',
    'Ověřte obě dotčené cesty a potvrzené názvy či rozložení. Vyřešte kolizi a připravte nový náhled.')

_group('''
unsafe_source_locator|Zdrojová cesta není bezpečná
unsafe_locator|Cesta nesplňuje bezpečnostní pravidla
reserved_library_namespace|Název zasahuje do vyhrazeného adresáře
system_excluded_target|Cíl leží v oblasti vyloučené z reorganizace
target_in_system_excluded_area|Plán vyžaduje chráněný adresář
source_symlink|Zdroj je symbolický odkaz
target_symlink|Cíl je symbolický odkaz
invalid_component|Název adresáře nebo souboru není použitelný
invalid_prefix|Prefix názvu není použitelný
invalid_extension|Přípona souboru není podporovaná
invalid_logical_input|Text nebo identita nelze bezpečně převést na cestu
empty_or_separator_only|Název po úpravě zůstal prázdný
component_byte_limit|Část názvu překračuje podporovanou délku
runtime_name_max|Název překračuje limit aktuálního filesystemu
runtime_path_max|Cesta překračuje limit aktuálního filesystemu
''', 'Cesta nebo název neprošly bezpečnostní kontrolou pro cílové uložení.',
    'Dotčená operace je blokovaná; reorganizace nepoužije nebezpečnou náhradní cestu.',
    'Ověřte zdroj a potvrzený název v kontrole názvů. Odkazy ani chráněné adresáře tímto workflow neobcházejte.')

_group('''
filesystem_not_checked|Filesystem nebyl ověřen
library_root_not_directory|Kořen knihovny není dostupný adresář
inaccessible_inventory|Část knihovny nebylo možné prohlédnout
subtree_not_fully_inventoried|Obsah podstromu není úplně ověřený
unsupported_inventory_kind|Inventura obsahuje nepodporovaný typ položky
listing_error|Adresář nebylo možné vypsat
stat_or_read_error|Položku nebylo možné ověřit
symlink|Položka je symbolický odkaz
inaccessible|Položka není přístupná
nonregular|Položka není běžný soubor
pc_name_max_not_checked|Limit délky názvu nebyl ověřen
pc_path_max_not_checked|Limit délky cesty nebyl ověřen
archive_changed_during_inventory|Archiv se během inventury změnil
archive_no_longer_regular|Archiv již není běžný soubor
''', 'Read-only inventura nemohla bezpečně ověřit všechny potřebné položky nebo limity.',
    'Neúplný výpis neznamená prázdný adresář. Chybějící důkaz brání bezpečnému pokračování.',
    'Ověřte připojení a přístupová práva, vyčkejte na dokončení jiných zápisů a připravte nový náhled.')

_group('''
cleanup_review_required|O původním adresáři se rozhodne až po přesunech
all_known_files_planned_to_leave_cleanup_not_authorized|Známé soubory z původního adresáře odejdou
empty_directory_disposition_unresolved|Prázdný adresář čeká na živé posouzení
directory_disposition_unresolved|Využití adresáře zatím není dořešené
''', 'Náhled popisuje původní strukturu; finální prázdnost se ověří až po přesunech.',
    'Tato položka sama nepovoluje odstranění. Živý návrh cleanupu bude vyžadovat samostatné schválení.',
    'Po přesunech zkontrolujte souhrn a podle potřeby rozbalte seznam prázdných adresářů.')

_group('''
system_excluded_contents_not_inventoried|Obsah chráněné oblasti se neprochází
contains_system_excluded_area|Adresář obsahuje chráněnou oblast
contains_justified_keep|Adresář obsahuje soubor ponechaný na místě
parent_of_planned_target|Adresář je potřebný pro cílovou strukturu
planned_parent_requires_creation|Plán vyžaduje vytvoření cílového adresáře
approved_root_txt_keep|Textový soubor v kořeni zůstane na místě
archive_source_preserved_contents_not_classified|Zdrojový archiv bude zachován
exclusive_secondary_directory_and_identical_primary_archive|Kopie archivu byla doložena přiřazením a shodou obsahu
explicit_human_standalone_archive|Archiv se zachová samostatně podle ručního rozhodnutí
explicit_human_secondary_archive|Archiv je ručně přiřazen konkrétní duplicitní kopii
''', 'Uvedené zachování nebo umístění vychází z aktuálního plánu a doložených podkladů.',
    'Jde o informaci o navrženém zachování, nikoli o svolení odstranit obsah nebo potvrzení jeho úplnosti.',
    'Není potřeba další rozhodnutí.')

_group('''
scanner_maintenance_not_proven|Odstavení scanneru není potvrzené
inventory_writer_maintenance_not_proven|Odstavení zapisovatelů není potvrzené
snapshot_time_invalid_or_prebaseline|Čas snapshotu není platný pro tento návrh
bound_snapshot_not_proven|Snapshot není doložený pro tento návrh
bound_db_backup_not_proven|Záloha databáze není doložená pro tento návrh
db_backup_local_path_not_proven|Chybí doložená lokální záloha databáze
db_backup_not_outside_repo_and_library|Záloha není v povoleném samostatném umístění
db_backup_file_missing_or_not_regular|Soubor zálohy nelze bezpečně použít
db_backup_not_independent_copy|Záloha není nezávislá kopie
db_backup_bytes_mismatch|Záloha nesouhlasí s plánovanou databází
db_backup_unreadable|Zálohu databáze nelze přečíst
sealed_windows_root_not_bound|Návrh nemá potvrzený Windows kořen
legacy_windows_binding_unavailable|Historickou vazbu Windows nelze použít
actual_windows_root_not_proven|Provozní Windows kořen nebyl doložen
actual_windows_root_invalid|Provozní Windows kořen není platný
windows_root_mismatch|Windows kořen nesouhlasí s návrhem
mount_read_write_not_proven|Zapisovatelný mount nebyl doložen
current_mount_read_only|Provozní pohled knihovny je pouze pro čtení
current_mount_state_not_proven|Stav provozního mountu není doložen
safe_write_capability_not_proven|Bezpečný zápis nebyl doložen
rename_noreplace_not_proven|Bezpečné přejmenování bez přepsání nebylo doloženo
mount_identity_not_proven|Identita mountu není doložená
free_bytes_not_reported|Chybí údaj o dostupném prostoru
''', 'Pro tento přesný manifest není potvrzená některá z vyžadovaných provozních podmínek.',
    'Spuštění je zablokované. Potvrzení upozornění tyto provozní podmínky nenahrazuje.',
    'Správce musí ověřit provozní podklady na hostiteli. Z prohlížeče nepřepisujte evidence ani neopakujte fyzickou operaci.')

_group('''
database_fingerprint_changed|Databáze se od návrhu změnila
library_root_changed|Kořen knihovny se od návrhu změnil
plan_hash_changed|Aktuální plán již není původní návrh
source_changed|Zdrojový soubor se od návrhu změnil
manifest_projection_mismatch|Aktuální projekce nesouhlasí s manifestem
post_state_not_converged|Navržený výsledek není stabilní
post_state_unavailable|Výsledný stav se nepodařilo ověřit
expected_post_state_unavailable|Chybí bezpečně ověřitelný výsledný stav
post_state_collisions|Výsledná struktura obsahuje kolize
post_state_blocked_or_review|Výsledný stav má nevyřešené položky
post_state_target_drift|Výsledné cíle se odchylují od návrhu
post_state_classification_or_coverage_drift|Výsledné zařazení nebo pokrytí nesouhlasí
post_state_container_locator_drift|Výsledná umístění kolekcí nebo částí nesouhlasí
invalid_manifest|Manifest není platný
planner_error|Plánování se nepodařilo bezpečně dokončit
reference_projection_error|Kontrolní projekci se nepodařilo dokončit
''', 'Aktuální podklady nejsou shodné s návrhem nebo nepotvrdily jeho očekávaný výsledek.',
    'Původní návrh nelze považovat za aktuální souhlas. Žádná oprava ani opakování přesunů se neprovádí automaticky.',
    'Zachovejte původní evidence, ověřte příčinu a před spuštěním připravte nový aktuální návrh. Při již zahájeném běhu je potřeba zásah správce.')

_group('''
move_cycle|Přesuny tvoří nepodporovaný cyklus
occupied_by_source_requires_staging|Cíl je obsazen jiným přesouvaným zdrojem
unresolved_move_graph|Pořadí přesunů není bezpečně vyřešené
criticality_review_required|Důležitost obsahu vyžaduje potvrzení
bonus_content_authority_unresolved|Podklady samostatného bonusu nejsou potvrzené
content_criticality_unresolved|Důležitost obsahu nelze určit
preview_vs_pv_unresolved|Není rozlišen dějový náhled a propagační video
warning_unacknowledged|Upozornění ještě nebylo potvrzené
unknown_warning_acknowledgement|Potvrzení neodpovídá upozornění v návrhu
''', 'Pro bezpečné spuštění chybí jednoznačné pořadí nebo přesně odpovídající rozhodnutí.',
    'Manifest zatím není připravený ke spuštění. Samotné označení souboru jako méně důležitého nesnižuje jeho ochranu.',
    'Zkontrolujte konkrétní položku a požadované rozhodnutí. Potvrzujte jen upozornění tohoto přesného návrhu.')

# Component transformations are informative evidence, never a content classification.
_group('''
normalized_nfc|Text názvu byl sjednocen
normalized_unicode_space|Mezery v názvu byly sjednoceny
collapsed_spaces|Opakované mezery byly sloučeny
trimmed_spaces|Okrajové mezery byly odstraněny
removed_trailing_dots|Koncové tečky byly upraveny
repeated_separator|Opakované oddělovače byly upraveny
replaced_separator|Oddělovač v názvu byl bezpečně upraven
replaced_angle_bracket|Lomené závorky v názvu byly upraveny
replaced_asterisk|Hvězdička v názvu byla upravena
replaced_question|Otazník v názvu byl upraven
replaced_quote|Uvozovky v názvu byly upraveny
escaped_device_name|Vyhrazený systémový název byl bezpečně upraven
escaped_leading_dot|Úvodní tečka v názvu byla bezpečně upravena
''', 'Text názvu prošel podporovanou úpravou pro bezpečné fyzické uložení.',
    'Mění se pouze podoba navrženého názvu, nikoli identita nebo metadata obsahu.',
    'Porovnejte současný a cílový název. Samostatné rozhodnutí se tím nevytváří.')

LABELS = {'READY': 'Připraveno v návrhu', 'WARNING': 'Upozornění', 'REVIEW': 'Vyžaduje kontrolu',
    'BLOCKED': 'Blokováno', 'BLOCKER': 'Blokující problém', 'SYSTEM_EXCLUDED': 'Chráněná oblast',
    'MOVE': 'Přesunout', 'KEEP': 'Ponechat na místě', 'QUARANTINE': 'Přesunout do karantény',
    'WAIT': 'Čeká se', 'WORK': 'Právě probíhá', 'PASS': 'Dokončeno', 'STOP': 'Zastaveno, vyžaduje zásah',
    'TARGET_REQUIRED': 'Potřebný cílový adresář', 'MISSING_TARGET_PARENT': 'Nový cílový adresář',
    'PRESERVED': 'Zachovat', 'EXCLUDED': 'Chráněná oblast', 'CLEANUP_REVIEW': 'Posouzení po přesunech',
    'UNRESOLVED_EMPTY': 'Prázdný adresář k posouzení', 'NORMAL': 'Reorganizace dokončena'}


def describe(code, severity='review'):
    key = code.split(':', 1)[0]
    if key == 'inventory_diagnostic':
        return describe(code.split(':', 1)[1], severity)
    message = MESSAGES.get(key) or (MESSAGES['duplicate_relation_'] if key.startswith('duplicate_relation_') else None)
    fallback = dict(title='Podklad vyžaduje kontrolu',
        reason='Tento podklad nemá samostatný uživatelský popis; jeho přesné znění zůstává v technických podrobnostech.',
        impact='Závažnost a možnost pokračovat určuje původní stav položky, nikoli tento popis.',
        action='Prohlédněte technické podrobnosti a ověřte konkrétní položku. Nevyvozujte z neznámého kódu souhlas k přesunu.')
    if severity == 'info':
        fallback.update(title='Informace k položce', action='Podle potřeby ověřte technické podrobnosti. Tato informace sama nevyžaduje nové rozhodnutí.')
    return dict(message or fallback, code=code, severity=severity)


def catalog():
    return dict(messages=MESSAGES, labels=LABELS,
        fallbacks={level: describe('', level) for level in ('info', 'review', 'warning', 'error')})


def explain_error(detail):
    """Separate a public error's diagnostic suffix without changing its outcome."""
    import re
    if detail.startswith('STOP: preflight '):
        _, separator, tail = detail[len('STOP: preflight '):].partition(': ')
        heading = 'Návrh neprošel aktuálním ověřením.'
    else:
        heading, separator, tail = detail.partition(': ')
    codes = tail.split(', ') if separator else []
    if codes and all(re.fullmatch(r'[a-z][a-z0-9_]*(?::.*)?', code)
            and ('_' in code.split(':', 1)[0] or code.split(':', 1)[0] in MESSAGES) for code in codes):
        return heading, [describe(code, 'error') for code in codes]
    return detail, []
