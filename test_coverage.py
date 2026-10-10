"""Offline controls: no network calls or changes to production history."""
import json
import tempfile
import unittest
from contextlib import ExitStack
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import aggregate as agg


class CoverageTests(unittest.TestCase):
    def source(self, **kwargs):
        return dict(name='Fixture',url='https://example.org/jobs',mode='automated',**kwargs)

    def scrape(self, source, prior=0, responses=None):
        with patch.object(agg,'fetch',side_effect=responses or [SimpleNamespace(text='<h1>Careers</h1>')]), patch.object(agg,'candidates_from_html',return_value=[]):
            return agg.scrape_source(source,date(2026,10,10),prior)[1]

    def test_auto_alias_executes_and_zero_is_not_success(self):
        source=self.source();source['mode']='auto'
        h=self.scrape(source)
        self.assertEqual(h['mode'],'automated')
        self.assertIsNotNone(h['attempted_at'])
        self.assertFalse(h['ok'])
        self.assertEqual(h['pages_parsed'],1)

    def test_manual_is_not_a_check_and_unknown_mode_fails(self):
        for mode in ('manual','typo'):
            source=self.source();source['mode']=mode
            with patch.object(agg,'fetch') as fetch:
                h=agg.scrape_source(source,date.today(),1)[1]
            fetch.assert_not_called()
            self.assertIsNone(h['checked_at'])
            self.assertIsNot(h['ok'],True)
            self.assertTrue(h['preserved'])
        self.assertEqual(h['status'],'failed')

    def test_partial_pagination_is_not_success(self):
        h=self.scrape(self.source(urls=['https://example.org/1','https://example.org/2'],allow_zero=True),responses=[SimpleNamespace(text='<h1>Careers</h1>'),RuntimeError('timeout')])
        self.assertEqual(h['status'],'partial-suspected')
        self.assertEqual(h['pages_expected'],2)
        self.assertEqual(h['pages_parsed'],1)

    def test_authoritative_parser_cannot_bypass_collapse(self):
        h=self.scrape(self.source(authoritative_parser=True,allow_zero=True),prior=10)
        self.assertFalse(h['ok'])
        self.assertTrue(h['preserved'])

    def test_blocked_is_never_zero_success(self):
        h=self.scrape(self.source(allow_zero=True),responses=[SimpleNamespace(text='please verify you are a human')])
        self.assertEqual(h['status'],'blocked')
        self.assertFalse(h['ok'])

    def test_advertised_pagination_cannot_pass_as_complete(self):
        h=self.scrape(self.source(allow_zero=True),responses=[SimpleNamespace(text='Showing 1-12 of 31 jobs')])
        self.assertFalse(h['ok'])
        self.assertIn('pagination',h['error'])
        self.assertFalse(agg.incomplete_listing_page('Showing 1-31 of 31 jobs'))

    def test_coverage_exposes_manual_and_blocked(self):
        h=agg.coverage_summary([dict(source='A',mode='automated',ok=True),dict(source='B',mode='manual',ok=None),dict(source='C',mode='automated',ok=False)])
        self.assertEqual(h['sources_requiring_review'],['B','C'])
        self.assertEqual(h['automated_healthy'],1)
        self.assertEqual(h['status'],'incomplete')
        self.assertEqual(agg.coverage_summary([])['status'],'incomplete')

    def test_manual_history_preserved_and_writes_read_back(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            paths={name:Path(tmp)/filename for name,filename in [('SOURCES_PATH','sources.json'),('JOBS_PATH','jobs.json'),('HISTORY_PATH','history.json'),('CHANGES_PATH','changes.json'),('META_PATH','meta.json')]}
            for name,path in paths.items():stack.enter_context(patch.object(agg,name,path))
            source=self.source();source['mode']='manual'
            paths['SOURCES_PATH'].write_text(json.dumps([source]))
            role=dict(id='kept',source='Fixture',status='open',title='CEO',organization='Example Foundation',url='https://example.org/jobs/ceo',missing_runs=1,last_seen='2026-10-01T00:00:00Z')
            paths['HISTORY_PATH'].write_text(json.dumps({'jobs':[role]}))
            paths['META_PATH'].write_text(json.dumps({'last_healthy_automated_check_at':'2026-10-01T00:00:00Z'}))
            stack.enter_context(patch.object(agg,'enrich_nonprofit_990'))
            stack.enter_context(patch.object(agg,'backfill_compensation'))
            stack.enter_context(patch.object(agg,'build_market_take',return_value={}))
            stack.enter_context(patch.object(agg,'fetch',side_effect=AssertionError('No network permitted')))
            self.assertEqual(agg.main(),0)
            result=json.loads(paths['HISTORY_PATH'].read_text())['jobs'][0]
            self.assertEqual(result['status'],'open')
            self.assertEqual(result['missing_runs'],1)
            self.assertEqual(result['last_seen'],role['last_seen'])
            self.assertTrue(result['carried_over'])
            meta=json.loads(paths['META_PATH'].read_text())
            self.assertEqual(meta['write_readback'],'passed')
            self.assertEqual(meta['last_healthy_automated_check_at'],'2026-10-01T00:00:00Z')
            stamps={json.loads(paths[n].read_text())['generated_at'] for n in ('JOBS_PATH','HISTORY_PATH','CHANGES_PATH','META_PATH')}
            self.assertEqual(len(stamps),1)

    def test_readback_failure_does_not_replace_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'meta.json';path.write_text('{"old":true}')
            with patch.object(agg.json,'loads',return_value={'wrong':True}):
                with self.assertRaises(ValueError):agg.write_verified_json(path,{'new':True})
            self.assertEqual(json.loads(path.read_text()),{'old':True})


if __name__=='__main__':unittest.main()
