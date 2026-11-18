import json
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

import studio_adaptive as adaptive
import studio_backend as backend
import studio_core as core
import studio_methods as methods
import studio_tasks as tasks
import test_adaptive_flow as fixture


class TaskBackendSelection(TestCase):
    def test_plan_commits_once_then_uses_the_faster_matching_backend(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            ctx, _, _ = fixture.AdaptiveFlow().fixture(root)
            ctx = replace(ctx, source=replace(ctx.source,engine='auto',adaptive=False,continuous=True))
            endings=[f'_tail_{i}' for i in range(8)]
            with patch.object(core,'plan_words',return_value=([],endings,['one','two','three','four'])):
                task=tasks.create(ctx,tasks.Pause(),lambda _:None)
            calls=[]
            class TestGPU:
                def __init__(self,*_):self.targets=ctx.pools
                def batch(self,_ctx,kinds,mode,opens,ends,words,safe,log):
                    calls.append(('GPU',tuple(words)))
                    return []
            def peel(work,ctx,kind,opens,ends,words,targets,log):
                calls.append(('CPU',tuple(words)))
                return []
            # Force a faster CPU measurement without relying on OS scheduling.
            with patch.object(tasks,'GpuSearch',TestGPU),patch.object(tasks,'PLAN_BATCH',18), \
                 patch.object(backend,'eligible',return_value=True),patch.object(backend,'peel_batch',peel), \
                 patch.object(backend,'perf_counter',side_effect=[10.0,10.0]):
                export,_,done,maximum,_=tasks.resume(task,ctx,root/'profile',tasks.Pause(),lambda _:None,override_claims=set())
            report=json.loads((export/'performance.json').read_text(encoding='utf-8'))
            self.assertEqual(calls,[('GPU',('one','two')),('CPU',('one','two')),('CPU',('three','four'))])
            self.assertEqual((done,maximum,report['batches']['total']),(36,36,2))
            self.assertEqual(report['backend_decisions'][0]['backend'],'cpu-peel')

    def test_mismatching_calibration_keeps_the_verified_gpu_results(self):
        gpu_rows=[('image','00','first_name')]
        with patch.object(backend,'perf_counter',side_effect=[1.0,1.1]):
            rows,decision=backend.compare(gpu_rows,1.0,lambda:[],200_000)
        self.assertEqual(rows,gpu_rows)
        self.assertFalse(decision['matches_equal'])
        self.assertEqual(decision['backend'],'gpu-forward')

    def test_existing_adaptive_cursor_survives_registration_of_new_method(self):
        with TemporaryDirectory() as folder:
            root=Path(folder);ctx,names,keys=fixture.AdaptiveFlow().fixture(root)
            task=tasks.create(ctx,tasks.Pause(),lambda _:None)
            with closing(tasks.connect(task)) as db:
                adaptive.setup(db)
                with db:
                    db.execute("insert into adaptive_run(method,kind,label,fingerprint,snapshot,seed_pos,seeds,attempted) "
                               "values('legacy','xanim','legacy','frozen','old',1,2,5)")
                pack={'corpus':{'xanim':[]},'training':{'xanim':[]},'held':{'xanim':[]},'wanted':{'xanim':[keys[names[1]]]}}
                directory=task/'methods';directory.mkdir()
                import gzip
                (directory/'old.json.gz').write_bytes(gzip.compress(json.dumps(pack).encode()))
                def prepare(*_):
                    return [methods.Method('legacy','xanim','legacy',('xanim',),1,['skip_me','legacy_next'],lambda n:[n]),
                            methods.Method('visual-variants','xanim','new',('xanim',),10,['new_candidate'],lambda n:[n])]
                pause=tasks.Pause()
                checked=[]
                def execute(kinds,words):
                    checked.extend(words);pause.stop();return []
                with patch.object(methods,'prepare',prepare):
                    adaptive.run(task,db,ctx,None,set(),pause,lambda _:None,execute,20)
                self.assertEqual(checked,['new_candidate'])
                self.assertEqual(db.execute("select seed_pos,attempted from adaptive_run where fingerprint='frozen'").fetchone(),(1,5))
