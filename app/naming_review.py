"""Shared pure Physical Naming candidates, work queue and summary badges."""
from __future__ import annotations

from dataclasses import dataclass, replace, asdict
from decimal import Decimal
import hashlib
import json
import re
import unicodedata
from typing import Mapping
from urllib.parse import urlencode

from .hierarchy import parse_explicit_part
from .hierarchy_types import MAIN_CONTENT_PART_TYPES
from .physical_naming import PhysicalNamingContext, PhysicalNameResolution, resolve_physical_name, create_basis_snapshot
from .physical_naming_components import SanitizedComponent, ComponentDiagnostic, sanitize_component, COMPONENT_POLICY_ID, WindowsPathBudget
from .physical_naming_formatters import EpisodeIdentity, SupplementaryIdentity, format_episode_component, format_supplementary_component
from .physical_naming_service import validate_physical_text


KIND_LABELS = {'romaji':'Romaji','english':'English','synonym':'AniList synonym','current':'Současný název','custom':'Vlastní text','parent_prefix':'Název nadřazené části'}
REASON_LABELS = {
    'readability':'Název je dlouhý. Zkontroluj fyzické pojmenování.',
    'shorter_current':'Současný název je výrazně kratší než výchozí Romaji.',
    'authority_ambiguous':'Fyzický root vyžaduje lidskou volbu; metadata anchor se tím nemění.',
    'season_only_multiple_parts':'Season obsahuje více Partů. Vyber textový prefix nebo vlastní název.',
    'basis_mismatch':'Metadata kontext se změnil od posledního potvrzení názvu.',
    'structural_context_changed':'Strukturální kontext se změnil od posledního potvrzení názvu.',
    'unavailable':'Výchozí název není jednoznačně dostupný. Vyber použitelný fyzický text.',
    'component_invalid':'Fyzický název není použitelný pro známou component.',
    'planner_warning':'Plán cílového pojmenování vyžaduje kontrolu.',
}
_PERIOD = r'[ZJLP]\s*\d{2}(?:\s*-\s*[ZJLP]?\s*\d{2})*'
_TERMINAL_PERIOD = re.compile(r'(?:\s*\(\s*'+_PERIOD+r'(?:\s+cz-xx%)?\s*\)|\s+'+_PERIOD+r'(?:\s+cz-xx%)?)\s*$',re.I)
_TECHNICAL_TITLE = re.compile(r'(?:Sezóna\s*[–-]\s*)?Serie\s*(?:\d+|Alternative)',re.I)


@dataclass(frozen=True)
class NamingCollectionEvidence:
    id: int
    local_title: str
    manual_display_title: str | None = None


@dataclass(frozen=True)
class KnownNamingFile:
    video_id: int
    identity: EpisodeIdentity | SupplementaryIdentity
    extension: str


@dataclass(frozen=True)
class NamingTitleEvidence:
    id: int
    local_title: str
    manual_display_title: str | None
    english: str | None
    synonyms: tuple[str, ...]
    files: tuple[KnownNamingFile, ...] = ()
    deferred_file_count: int = 0


@dataclass(frozen=True)
class NamingReviewContext:
    physical: PhysicalNamingContext
    collections: Mapping[int, NamingCollectionEvidence]
    titles: Mapping[int, NamingTitleEvidence]


@dataclass(frozen=True)
class PlannerNamingWarning:
    code: str
    actual_units: int | None = None
    target_units: int | None = None
    overflow_units: int | None = None

    @property
    def message(self):
        return ('Výsledná cesta je příliš dlouhá. Urči kratší název.' if self.code=='windows_path_budget'
                else 'Cílové názvy kolidují. Zkontroluj fyzické pojmenování.')

    @classmethod
    def from_path_budget(cls, budget: WindowsPathBudget):
        return cls('windows_path_budget',budget.utf16_units,budget.target_units,budget.overflow_units) if budget.requires_shorter_name else None


@dataclass(frozen=True)
class NamingCandidate:
    key: str
    kind: str
    raw_text: str
    source_title_id: int | None
    source_label: str
    sanitized: SanitizedComponent
    max_component_bytes: int | None
    can_confirm: bool
    diagnostics: tuple[ComponentDiagnostic, ...]
    diagnostic_messages: tuple[str, ...]
    selected: bool = False
    is_default: bool = False

    @property
    def kind_label(self):
        return KIND_LABELS[self.kind]

    @property
    def transformation_labels(self):
        labels={'normalized_nfc':'Unicode NFC','normalized_unicode_space':'Unicode mezera → běžná mezera','collapsed_spaces':'Sloučené opakované mezery','trimmed_spaces':'Odstraněné vnější mezery','removed_trailing_dots':'Odstraněné koncové tečky a mezery','escaped_device_name':'Rezervovaný název zařízení → prefix _','escaped_leading_dot':'Počáteční tečka → prefix _'}
        return tuple((f'{t.original} → {t.replacement}' if t.original is not None else labels.get(t.code,t.code)) for t in self.sanitized.transformations)


@dataclass(frozen=True)
class NamingReviewUnit:
    owner_key: tuple[str,int]
    collection_id: int | None
    collection_label: str
    title_label: str | None
    scope_label: str
    resolution: PhysicalNameResolution
    candidates: tuple[NamingCandidate,...]
    effective: NamingCandidate | None
    reasons: tuple[str,...]
    actionable: bool
    status: str
    fingerprint: str
    dependents: tuple[tuple[str,int],...] = ()
    dependent_labels: tuple[str,...] = ()
    planner_warnings: tuple[PlannerNamingWarning,...] = ()
    deferred_file_count: int = 0

    @property
    def scope(self):
        return self.owner_key[0]
    @property
    def owner_id(self):
        return self.owner_key[1]
    @property
    def owner_tag(self):
        return f'{self.scope}:{self.owner_id}'
    @property
    def anchor(self):
        return f'naming-{self.scope}-{self.owner_id}'
    @property
    def dependency(self):
        return self.resolution.dependency
    @property
    def reason_messages(self):
        return tuple(REASON_LABELS.get(x,REASON_LABELS['unavailable']) for x in self.reasons)
    @property
    def state_label(self):
        return {'human_choice':'Člověkem potvrzený','derived_default':'Automatický výchozí','inherited':'Zděděný název'}.get(self.resolution.authority,'Vyžaduje lidskou volbu')


@dataclass(frozen=True)
class NamingBadge:
    state: str
    label: str
    severity: str
    review_count: int
    href: str


@dataclass(frozen=True)
class NamingReviewIndex:
    units: Mapping[tuple[str,int],NamingReviewUnit]
    pending: tuple[NamingReviewUnit,...]
    collection_badges: Mapping[int,NamingBadge]
    title_badges: Mapping[int,NamingBadge]
    context: NamingReviewContext


def _json(value):
    # ASCII JSON also represents rejected surrogate input safely in opaque keys.
    return json.dumps(value,ensure_ascii=True,sort_keys=True,separators=(',',':'),default=lambda x:format(x,'f') if isinstance(x,Decimal) else x.isoformat())


def _hash(value):
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def current_semantic_name(text: str, *, title_prefix=False) -> str | None:
    """Only explicit local/user evidence; never filename/title abbreviation."""
    text=_TERMINAL_PERIOD.sub('',text.strip()).strip()
    if not text or text.casefold() in {'anime','.','..','season','seasons'}:
        return None
    if title_prefix and (parse_explicit_part(text) is not None or _TECHNICAL_TITLE.fullmatch(text)):
        return None
    return text


def _current(context,key):
    scope,id=key
    evidence=context.collections[id] if scope=='collection' else context.titles[id]
    if scope=='title' and evidence.manual_display_title:
        return evidence.manual_display_title.strip(), 'Ruční display text (historická nabídka)'
    return current_semantic_name(evidence.local_title,title_prefix=scope=='title'), 'Lokální název (historická nabídka)'


def _index_files(context, resolutions):
    """Propagate each known file to its current naming dependencies once."""
    result = {key: [] for key in resolutions}
    for id,evidence in context.titles.items():
        source=('title',id)
        while source is not None:
            result[source].extend(evidence.files)
            source=resolutions[source].dependency
    return {key: tuple(files) for key, files in result.items()}


def diagnostic_messages(diagnostics):
    messages=[]
    for d in diagnostics:
        if d.code=='component_byte_limit':
            text=f'Název je příliš dlouhý. Urči kratší název. Aktuální délka: {d.actual_utf8_bytes} / {d.max_utf8_bytes} B. Překročeno o {d.overflow_bytes} B.'
        elif d.code=='repeated_separator': text='Opakovaný separator zůstal zachovaný.'
        elif d.code=='invalid_extension': text='Přípona známého souboru není použitelná.'
        elif d.code=='invalid_logical_input': text='Název musí obsahovat 1–500 Unicode znaků bez řídicích znaků a zalomení řádků.'
        elif d.code=='empty_or_separator_only': text='Název po úpravách neobsahuje použitelný text. Urči náhradní název.'
        else: text='Název není použitelný. Zkontroluj text a kontext položky.'
        if text not in messages: messages.append(text)
    return tuple(messages)


def make_naming_candidate(context,key,raw_text,kind,*,source_title_id=None,source_label='',candidate_key=None,resolutions=None,files_by_owner=None):
    sanitized=sanitize_component(raw_text)
    diagnostics=list(sanitized.diagnostics)
    try: validate_physical_text(raw_text)
    except ValueError: diagnostics.append(ComponentDiagnostic('invalid_logical_input','error'))
    resolutions=resolutions or {k:resolve_physical_name(context.physical,*k) for k in [('collection',x) for x in context.collections]+[('title',x) for x in context.titles]}
    if files_by_owner is None:
        files_by_owner = _index_files(context, resolutions)
    max_bytes=sanitized.utf8_bytes
    for file in files_by_owner[key]:
        formatter=format_episode_component if isinstance(file.identity,EpisodeIdentity) else format_supplementary_component
        formatted=formatter(sanitized,file.identity,file.extension)
        if formatted.utf8_bytes is not None: max_bytes=max(max_bytes or 0,formatted.utf8_bytes)
        diagnostics.extend(d for d in formatted.diagnostics if d not in diagnostics)
    choice=resolutions[key].choice
    identity=context.physical.titles[source_title_id].metadata_identity if source_title_id in context.physical.titles else None
    return NamingCandidate(candidate_key or _hash([kind,source_title_id,identity,raw_text]),kind,raw_text,source_title_id,source_label,sanitized,max_bytes,
        sanitized.valid and not any(d.severity=='error' for d in diagnostics),tuple(diagnostics),diagnostic_messages(diagnostics),
        selected=bool(choice and choice.choice_kind==kind and choice.physical_text==raw_text),
        is_default=kind=='romaji' and resolutions[key].default_candidate==raw_text and resolutions[key].metadata_source_title_id==source_title_id)


def _candidates(context,key,resolutions,files_by_owner):
    r=resolutions[key]
    sources=[]
    if r.metadata_source_title_id is not None:
        sources=[r.metadata_source_title_id]
    elif key[0]=='collection':
        ids=context.physical.collections[key[1]]
        main=[id for id in ids if context.physical.titles[id].part_type in MAIN_CONTENT_PART_TYPES]
        sources=main or [id for id in ids if context.physical.titles[id].part_type=='film']
    candidates=[]
    def add(text,kind,source=None,label='',explicit_key=None):
        if isinstance(text,str) and text.strip():
            c=make_naming_candidate(context,key,text.strip(),kind,source_title_id=source,source_label=label,candidate_key=explicit_key,resolutions=resolutions,files_by_owner=files_by_owner)
            if not any(old.key==c.key for old in candidates): candidates.append(c)
    for id in sorted(sources):
        t=context.physical.titles[id];e=context.titles[id]
        if not t.metadata_identity or 'metadata_payload_mismatch' in t.metadata_diagnostics: continue
        label=f'{e.local_title} · {t.metadata_identity[0]} #{t.metadata_identity[1]} · část #{id}'
        add(t.romaji,'romaji',id,label)
        add(e.english,'english',id,label)
        for synonym in sorted(set(e.synonyms)): add(synonym,'synonym',id,label)
    current,label=_current(context,key)
    add(current,'current',label=label)
    if 'season_only_multiple_parts' in r.diagnostics or (key[0]=='title' and r.choice and r.choice.choice_kind=='parent_prefix'):
        # The shared basis contract defines current eligibility, including own
        # metadata and effective Part changes. Historical source IDs stay only
        # in the saved snapshot and remain available for explicit reconfirm.
        structure=json.loads(create_basis_snapshot(context.physical,*key))['structure']
        for id in structure['parent_title_ids'] if structure is not None else ():
            p=resolutions['title',id]
            if p.ready: add(p.effective_text,'parent_prefix',id,f'{context.titles[id].local_title} · část #{id}')
    if r.choice and not any(c.selected for c in candidates):
        add(r.choice.physical_text,r.choice.choice_kind,label='Dříve potvrzený snapshot',explicit_key='saved')
    return tuple(candidates)


def _fingerprint(context,key,candidates):
    cid=key[1] if key[0]=='collection' else context.physical.titles[key[1]].collection_id
    title_ids=context.physical.collections.get(cid,()) if cid is not None else (key[1],)
    keys=[('title',id) for id in title_ids]+([('collection',cid)] if cid is not None else [])
    choices=[(k,asdict(context.physical.choices[k])) for k in keys if k in context.physical.choices]
    evidence=[asdict(context.titles[id]) for id in title_ids]
    for item in evidence: item['synonyms']=sorted(set(item['synonyms']))
    return _hash({'policy':COMPONENT_POLICY_ID,'unicode':unicodedata.unidata_version,'basis':create_basis_snapshot(context.physical,*key),'titles':[asdict(context.physical.titles[id]) for id in title_ids],
        'evidence':evidence,'collection':asdict(context.collections[cid]) if cid is not None else None,'choices':choices,
        'candidates':sorted(c.key for c in candidates)})


def _badge(state,count,href):
    return NamingBadge(state,{'ok':'Pojmenování OK','review':'Pojmenování: kontrola','problem':'Pojmenování: problém'}[state],{'ok':'success','review':'warning','problem':'error'}[state],count,href)


def build_naming_review(context: NamingReviewContext, *, planner_warnings=None) -> NamingReviewIndex:
    planner_warnings=planner_warnings or {}
    keys=[('collection',id) for id in sorted(context.collections)]+[('title',id) for id in sorted(context.titles)]
    resolutions={k:resolve_physical_name(context.physical,*k) for k in keys}
    files_by_owner = _index_files(context, resolutions)
    units={}
    for key in keys:
        r=resolutions[key]
        cid=key[1] if key[0]=='collection' else context.physical.titles[key[1]].collection_id
        c=context.collections.get(cid)
        label=(c.manual_display_title or c.local_title) if c else 'Bez kolekce'
        title=context.physical.titles[key[1]] if key[0]=='title' else None
        candidates=_candidates(context,key,resolutions,files_by_owner)
        effective=make_naming_candidate(context,key,r.effective_text,r.choice.choice_kind if r.choice else 'romaji',candidate_key='effective',resolutions=resolutions,files_by_owner=files_by_owner) if r.effective_text else None
        reasons=[]
        inherited=r.authority=='inherited' and r.choice is None
        if not inherited:
            if effective is not None and not effective.can_confirm: reasons.append('component_invalid')
            if r.choice:
                if not r.basis_matches: reasons.append('structural_context_changed' if r.diagnostics==('structural_context_changed',) else 'basis_mismatch')
            else:
                if r.diagnostics:
                    reasons.append(next((x for x in ('authority_ambiguous','season_only_multiple_parts') if x in r.diagnostics),'unavailable'))
                romaji=next((c for c in candidates if c.is_default),None)
                current=next((c for c in candidates if c.kind=='current' and c.can_confirm),None)
                if romaji and romaji.sanitized.requires_readability_review: reasons.append('readability')
                if romaji and current and romaji.sanitized.chars and current.sanitized.chars:
                    a,b=romaji.sanitized.chars,current.sanitized.chars
                    if a-b>=10 or b*10<=a*7: reasons.append('shorter_current')
        warnings=tuple(planner_warnings.get(key,()))
        if warnings and not inherited: reasons.append('planner_warning')
        state='problem' if 'component_invalid' in reasons else 'review' if reasons else 'ok'
        units[key]=NamingReviewUnit(key,cid,label,context.titles[key[1]].local_title if title else None,
            'Root' if title is None else 'Title prefix' if title.part_type in MAIN_CONTENT_PART_TYPES else 'Supplementary prefix',
            r,candidates,effective,tuple(reasons),bool(reasons) and not inherited,state,_fingerprint(context,key,candidates),
            planner_warnings=warnings,deferred_file_count=context.titles[key[1]].deferred_file_count if title else 0)
    dependents={}
    for key,u in tuple(units.items()):
        if u.resolution.dependency:
            parent=u.resolution.dependency
            dependents.setdefault(parent,[]).append(key)
            units[key]=replace(u,status=units[parent].status)
    for key,children in dependents.items():
        units[key]=replace(units[key],dependents=tuple(children),dependent_labels=tuple(units[child].title_label or units[child].collection_label for child in children))
    pending=tuple(u for u in units.values() if u.actionable)
    collection_badges={}
    for id in context.collections:
        work=[u for u in pending if u.collection_id==id]
        state='problem' if any(u.status=='problem' for u in work) else 'review' if work else 'ok'
        label=context.collections[id].manual_display_title or context.collections[id].local_title
        href='/naming-review?'+urlencode({'q':label,'collection_id':id,**({'status':'all'} if state=='ok' else {})})
        collection_badges[id]=_badge(state,len(work),href)
    title_badges={}
    for id in context.titles:
        u=units['title',id];parent=units[u.resolution.dependency] if u.resolution.dependency else u
        href='/naming-review?'+urlencode({'title_id':id,**({'collection_id':u.collection_id} if u.collection_id else {}),**({'status':'all'} if u.status=='ok' else {})})
        title_badges[id]=_badge(u.status,int(parent.actionable),href)
    return NamingReviewIndex(units,pending,collection_badges,title_badges,context)


def filter_naming_units(index, *, status='pending',q='',collection_id=None,title_id=None):
    if status not in {'pending','roots','titles','confirmed','all'}: raise ValueError('Neznámý filtr pojmenování.')
    title_keys=None
    if title_id is not None:
        key=('title',title_id)
        title_keys={key}
        while key in index.units and index.units[key].resolution.dependency:
            key=index.units[key].resolution.dependency;title_keys.add(key)
    rows=[]
    for u in index.units.values():
        if collection_id is not None and u.collection_id!=collection_id: continue
        if title_keys is not None and u.owner_key not in title_keys: continue
        haystack=' '.join([u.collection_label,u.title_label or '',index.context.collections[u.collection_id].local_title if u.collection_id in index.context.collections else '',*(c.raw_text for c in u.candidates)])
        if q.strip().casefold() not in haystack.casefold(): continue
        if status=='pending' and not u.actionable: continue
        if status=='roots' and u.scope!='collection': continue
        if status=='titles' and (u.scope!='title' or u.resolution.authority=='inherited'): continue
        if status=='confirmed' and u.resolution.choice is None: continue
        rows.append(u)
    return tuple(rows)
