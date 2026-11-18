from contextlib import closing
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
import sqlite3,threading,json

import studio_adaptive as adaptive
import studio_cache as cache
import studio_core as core
import studio_methods as methods
import studio_queue as queue
import studio_parallel as parallel
import studio_pipeline as pipeline
import studio_export as export_module
import studio_tasks as tasks
import test_adaptive_flow as fixture


class ParallelFlow(TestCase):
    def test_worker_failure_retries_uncommitted_slice_serially(self):
        plan=methods.Method('test','xanim','test',('xanim',),1,list(range(300)),lambda n:iter((f'name_{n}',)))
        generator=parallel.Generator(workers=2)
        try:
            with patch.object(generator,'_generate',side_effect=parallel.BrokenProcessPool('simulated child exit')):
                actual=generator.generate(plan,set(),5,10,'',lambda:None,queue.generate,queue.MAX_SEEDS,queue.MAX_BYTES)
            expected=queue.generate(plan,set(),5,10,'',lambda:None)
            self.assertEqual(actual[:2],expected[:2])
            self.assertEqual(generator.workers,1)
        finally:generator.close()

    def test_spawned_generators_preserve_exact_order_and_cursor(self):
        def emit(seed):
            yield 'shared'
            for i in range(seed%9):yield f'candidate_{seed}_{i}'
        plan=methods.Method('test','xanim','test',('xanim',),1,list(range(700)),emit)
        known={'candidate_8_1'}
        producer=parallel.Generator(workers=2)
        try:
            expected=queue.generate(plan,known,0,1000,'',lambda:None)
            actual=producer.generate(plan,known,0,1000,'',lambda:None,queue.generate,queue.MAX_SEEDS,queue.MAX_BYTES)
            self.assertEqual(actual[:2],expected[:2])
            self.assertIsNotNone(producer.pool)
            # Resume from the committed prefix; speculative suffixes must not skip seeds.
            expected=queue.generate(plan,known,actual[0],300,'',lambda:None)
            resumed=producer.generate(plan,known,actual[0],300,'',lambda:None,queue.generate,queue.MAX_SEEDS,queue.MAX_BYTES)
            self.assertEqual(resumed[:2],expected[:2])
        finally:producer.close()

    def test_background_export_overlaps_search_and_flushes_latest_hits(self):
        with TemporaryDirectory() as folder:
            root=Path(folder);ctx,names,keys=fixture.AdaptiveFlow().fixture(root)
            task=tasks.create(ctx,tasks.Pause(),lambda _:None)
            exporting=threading.Event();second_batch=threading.Event()
            original=core.results_export;calls=[]
            def export(*args,**kwargs):
                if threading.current_thread().name.startswith('cdb-export') and not exporting.is_set():
                    exporting.set()
                    if not second_batch.wait(10):raise AssertionError('search blocked behind export')
                return original(*args,**kwargs)
            def plans(*_):return [methods.Method('test','xanim','test',('xanim',),1,names[1:],lambda n:iter((n,)))]
            def engine(work,ctx,kinds,mode,opens,ends,words,log):
                calls.extend(words)
                if names[2] in words:
                    self.assertTrue(exporting.wait(10));second_batch.set()
                return [('xanim',f'{keys[n]:016x}',n) for n in words]
            class Small:
                target=1
                def observe(self,*_):pass
            class Inline:
                def __init__(self,*args):self.args=args
                def render(self,payload):return export_module.render_snapshot(*self.args,payload)
                def close(self):pass
            logs=[]
            with patch.object(tasks,'ExportWorker',Inline),patch.object(methods,'prepare',plans),patch.object(core,'results_export',export),patch.object(queue,'BatchSize',Small),patch.object(queue,'MAX_SEEDS',1):
                out,*_=tasks.resume(task,ctx,root/'profile',tasks.Pause(),logs.append,engine=engine,override_claims=set())
            self.assertEqual(calls,names[1:])
            self.assertEqual(core.read_cdb(task/'live/saluki-ready/hash_pkg'/(core.TABLE['xanim']+'.cdb')),{keys[n]:n for n in names})
            self.assertEqual(json.loads((task/'live/saved.json').read_text())['hits'],2)
            report=json.loads((out/'performance.json').read_text(encoding='utf-8'))
            self.assertEqual(report,json.loads((task/'performance.json').read_text(encoding='utf-8')))
            self.assertEqual(report['batches'],{'fixed':0,'adaptive':2,'total':2})
            self.assertEqual(report['candidates']['total'],2)
            self.assertTrue(any('本轮提交 2 批（固定 0，自动 2）' in line for line in logs))
            self.assertEqual(report['stages']['gpu_verification']['calls'],0)
            self.assertEqual(report['stages']['cpu_verification']['calls'],2)
            for stage in ('candidate_generation','sqlite_dedup','background_export','final_export'):
                self.assertGreater(report['stages'][stage]['seconds'],0)

    def test_file_and_validated_corpus_cache_invalidate_and_recover(self):
        with TemporaryDirectory() as folder,closing(sqlite3.connect(':memory:')) as db:
            root=Path(folder);ctx,names,keys=fixture.AdaptiveFlow().fixture(root)
            db.execute('create table hit(kind,key,name)')
            cached=root/'cache'
            before=adaptive.seed_corpus(ctx,db,None,lambda:None,lambda _:None,cached)
            adaptive._corpus_memory=None
            with patch.object(adaptive,'_base_corpus',side_effect=AssertionError('must reuse disk corpus')):
                again=adaptive.seed_corpus(ctx,db,None,lambda:None,lambda _:None,cached)
                self.assertEqual(before,again)
            adaptive._corpus_memory=None
            (cached/'corpus.json.gz').write_bytes(b'broken')
            rebuilt=adaptive.seed_corpus(ctx,db,None,lambda:None,lambda _:None,cached)
            self.assertEqual(before,rebuilt)
            table=root/(core.TABLE['xanim']+'.csv')
            with table.open('a',encoding='utf-8') as f:f.write(f'{keys[names[1]]:x},{names[1]}\n')
            changed=adaptive.seed_corpus(ctx,db,None,lambda:None,lambda _:None,cached)
            self.assertIn(names[1],changed[0]['xanim'])
            cdb=root/'original.cdb';core.write_cdb(cdb,{1:'first'})
            self.assertEqual(core.read_cdb(cdb),{1:'first'})
            cdb.unlink();core.write_cdb(cdb,{2:'replacement'})
            self.assertEqual(core.read_cdb(cdb),{2:'replacement'})

