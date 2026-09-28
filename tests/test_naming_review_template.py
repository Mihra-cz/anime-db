"""Pure rendering tests: naming decisions use scalar read models only."""
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from html.parser import HTMLParser

from jinja2 import Environment, FileSystemLoader, select_autoescape


def candidate(**changes):
    values = dict(key='romaji-key', kind='romaji', kind_label='Romaji',
                  raw_text='Raw: title', source_title_id=2, source_label='Potvrzená metadata',
                  sanitized=NS(preview_text='Raw- title', chars=10, utf8_bytes=10),
                  max_component_bytes=24, can_confirm=True, diagnostics=(),
                  diagnostic_messages=(), transformation_labels=('Dvojtečka → pomlčka',),
                  selected=False, is_default=True)
    values.update(changes)
    return NS(**values)


def unit(**changes):
    c = candidate()
    values = dict(scope='collection', owner_id=1, owner_tag='collection:1',
                  anchor='naming-collection-1', collection_id=1, collection_label='Collection',
                  title_label=None, scope_label='Root', fingerprint='basis',
                  resolution=NS(authority='derived_default', effective_text=c.raw_text,
                                choice=None, basis_matches=None, metadata_identity=('anilist', '1'),
                                metadata_source_title_id=2),
                  state_label='Automatický výchozí', actionable=True, status='review',
                  reasons=(), reason_messages=(), candidates=(c,), effective=c,
                  dependents=(), dependent_labels=('NCOP',), planner_warnings=(), deferred_file_count=0)
    values.update(changes)
    return NS(**values)


def render(rows, **extra):
    templates = Path(__file__).resolve().parents[1] / 'app' / 'templates'
    env = Environment(loader=FileSystemLoader(templates), autoescape=select_autoescape())
    env.globals['url_for'] = lambda name, path: '/static' + path
    context = dict(rows=rows, status='pending', q='',
                   filter_links=[('pending','K vyřízení','/naming-review',1)],
                   return_to='/naming-review', error=None, submitted={}, custom_preview=None, message=None)
    context.update(extra)
    return env.get_template('naming_review.html').render(**context)


class Forms(HTMLParser):
    def __init__(self, html):
        super().__init__(); self.forms=[]; self.current=None; self.feed(html)
    def handle_starttag(self, tag, attrs):
        attrs=dict(attrs)
        if tag=='form': self.current={'attrs':attrs,'inputs':[]}; self.forms.append(self.current)
        if tag=='input' and self.current is not None: self.current['inputs'].append(attrs)
    def handle_endtag(self, tag):
        if tag=='form': self.current=None


def test_unreviewed_default_requires_explicit_radio_choice():
    html = render((unit(),))
    save = next(f for f in Forms(html).forms if 'data-edit-form' in f['attrs'])
    radios = [i for i in save['inputs'] if i.get('type')=='radio']
    assert radios and all('checked' not in i for i in radios)
    assert {i['name'] for i in save['inputs']} <= {'fingerprint','return_to','candidate_key','custom_text'}
    assert 'Filesystem' in html and 'UTF-8' in html and 'NCOP' in html
    assert 'data-naming-dirty' in html


def test_saved_choice_selection_and_separate_reset_reconfirm_forms():
    c = candidate(selected=True)
    choice = NS(physical_text=c.raw_text, choice_kind='romaji',
                confirmed_at=datetime(2026,9,28,tzinfo=timezone.utc))
    resolution=NS(authority='human_choice', effective_text=c.raw_text, choice=choice,
                  basis_matches=False, metadata_identity=('anilist','2'), metadata_source_title_id=2)
    html = render((unit(candidates=(c,), resolution=resolution),))
    forms = Forms(html).forms
    save = next(f for f in forms if f['attrs'].get('action','').endswith('/save'))
    assert any(i.get('value')=='romaji-key' and 'checked' in i for i in save['inputs'])
    for action in ('reset','reconfirm'):
        separate=next(f for f in forms if f['attrs'].get('action','').endswith('/'+action))
        assert 'data-edit-form' not in separate['attrs']
    assert 'Používat automatický/default název' in html
    assert 'Potvrdit tento název znovu' in html
    assert '28. 09. 2026' in html


def test_invalid_custom_submission_is_preserved_with_explicit_byte_overflow():
    custom=candidate(key='custom', kind='custom', kind_label='Vlastní název',
                     raw_text='<script>x</script>', can_confirm=False,
                     sanitized=NS(preview_text='長'*86, chars=86, utf8_bytes=258),
                     max_component_bytes=272,
                     diagnostic_messages=('Název je příliš dlouhý. Urči kratší název.',))
    html=render((unit(),), error='Název nelze potvrdit.',
                submitted={'owner_tag':'collection:1','candidate_key':'custom','custom_text':'<script>x</script>'},
                custom_preview=custom)
    save=next(f for f in Forms(html).forms if f['attrs'].get('action','').endswith('/save'))
    assert save['attrs'].get('data-dirty-on-load')=='true'
    assert any(i.get('name')=='custom_text' and i['value']=='<script>x</script>' for i in save['inputs'])
    assert 'Název je příliš dlouhý. Urči kratší název.' in html
    assert '255' in html and 'Překročeno o' in html and '272' in html
    assert '<script>x</script>' not in html


def test_inherited_unit_is_compact_and_has_no_decision_form():
    resolution=NS(authority='inherited', effective_text='Parent', choice=None, basis_matches=None)
    html=render((unit(scope='title',owner_id=3,owner_tag='title:3',
                      resolution=resolution,state_label='Zděděný název',actionable=False),))
    assert 'Zděděný název' in html
    assert not any('data-edit-form' in f['attrs'] for f in Forms(html).forms)
