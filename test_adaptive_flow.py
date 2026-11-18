from collections import defaultdict
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

import studio_adaptive as adaptive
import studio_core as core
import studio_methods as methods
import studio_tasks as tasks


class AdaptiveFlow(TestCase):
    def fixture(self,root):
        names=['vm_test_weapon_idle_1','vm_test_weapon_idle_2','vm_test_weapon_reload_2']
        keys={n:core.name_key(n,'xanim') for n in names}
        ids=root/'modwar7.ids'
        ids.write_bytes(b'fixture')
        ids.with_suffix('.pools.txt').write_text('0 xanim 3')
        (root/(core.TABLE['xanim']+'.csv')).write_text(f'{keys[names[0]]:x},{names[0]}\n')
        src=core.Inputs(str(ids),str(root),'',str(root/'output'),kinds=('xanim',),adaptive=True)
        ctx=core.Context(src,ids,root,None,root/'output','MODWAR7',{'xanim':set(keys.values())},set(),3)
        return ctx,names,keys

    def test_feedback_pause_resume_duplicate_guard_and_local_preview(self):
        with TemporaryDirectory() as folder:
            root=Path(folder)
            ctx,names,keys=self.fixture(root)
            task=tasks.create(ctx,tasks.Pause(),lambda _:None)
            batches=[]
            calls=[]
            def plans(corpus,training,held,game,wanted,check):
                calls.append(set(held['xanim']))
                result=[methods.Method('first','xanim','first',('xanim',),5,[names[1]],lambda n:iter((n,)))]
                if names[1] in held['xanim']:
                    result.append(methods.Method('unlocked','xanim','unlocked',('xanim',),6,[names[2]],lambda n:iter((n,))))
                return result
            def engine(work,ctx,kinds,mode,opens,ends,words,log):
                # Export may still be running; final live contents are checked
                # after resume has drained the exporter below.
                batches.extend(words)
                return [('xanim',f'{keys[n]:016x}',n) for n in words if n in keys]
            with patch.object(adaptive.methods,'prepare',plans):
                out,rows,done,maximum,status=tasks.resume(task,ctx,root/'sources',tasks.Pause(),lambda _:None,
                                                        engine=engine,override_claims=set())
                self.assertEqual(set(rows['xanim']),{keys[n] for n in names[1:]})
                self.assertEqual(batches,names[1:])
                self.assertTrue((out/'method_results.json').is_file())
                self.assertTrue((out/'pr-preview.txt').is_file())
                self.assertEqual(done,2)
                self.assertEqual(set(core.read_cdb(task/'live/saluki-ready/hash_pkg'/(
                    core.TABLE['xanim']+'.cdb'))),set(keys.values()))
                tasks.resume(task,ctx,root/'sources',tasks.Pause(),lambda _:None,engine=engine,override_claims=set())
                self.assertEqual(batches,names[1:])
                self.assertTrue(any(names[1] in held for held in calls))

    def test_live_export_rolls_back_if_publishing_fails(self):
        with TemporaryDirectory() as folder:
            root=Path(folder)
            live=root/'live';live.mkdir()
            (live/'.studio-live').touch()
            (live/'saved.txt').write_text('previous valid export')
            missing=root/'missing-stage'
            with self.assertRaises(OSError):tasks.publish_live(root,missing)
            self.assertEqual((live/'saved.txt').read_text(),'previous valid export')
            unrelated=root/'live.previous';unrelated.mkdir()
            (unrelated/'user-file.txt').touch()
            with self.assertRaises(ValueError):tasks.publish_live(root,missing)
            self.assertTrue((unrelated/'user-file.txt').exists())

    def test_failed_batch_keeps_queued_candidates_for_resume(self):
        with TemporaryDirectory() as folder:
            root=Path(folder)
            ctx,names,keys=self.fixture(root)
            task=tasks.create(ctx,tasks.Pause(),lambda _:None)
            def plans(*_):
                return [methods.Method('first','xanim','first',('xanim',),5,names[1:],lambda n:iter((n,)))]
            def failed(*_):raise RuntimeError('device failed')
            def engine(work,ctx,kinds,mode,opens,ends,words,log):
                return [('xanim',f'{keys[n]:016x}',n) for n in words]
            with patch.object(adaptive.methods,'prepare',plans):
                with self.assertRaisesRegex(RuntimeError,'device failed'):
                    tasks.resume(task,ctx,root/'sources',tasks.Pause(),lambda _:None,engine=failed,override_claims=set())
                with closing(tasks.connect(task)) as db:
                    self.assertEqual(db.execute('select count(*) from adaptive_queue').fetchone()[0],2)
                    self.assertEqual(db.execute('select count(*) from hit').fetchone()[0],0)
                out,rows,*_=tasks.resume(task,ctx,root/'sources',tasks.Pause(),lambda _:None,engine=engine,override_claims=set())
                self.assertEqual(len(rows['xanim']),2)

    def test_actual_inverse_and_numbered_and_audio_tail_methods(self):
        a,b='vm_rig_idle_01','vm_rig_idle_03'
        audio='wpn/rig/rig_fire_01.vnn.85.48000.all'
        sounds={f'wpn/rig/rig_fire_0{i}.vnn.85.48000.all' for i in range(1,4)}
        sources={'xanim':{a,b},'sound_asset':sounds,'sound_alias':set()}
        wanted={'xanim':{core.name_key('vm_rig_idle_02','xanim')},'sound_asset':{1}}
        plans=methods.prepare(sources,sources,sources,'MODWAR7',wanted,lambda:None)
        inverse=next(p for p in plans if p.key=='final-byte' and p.kind=='xanim')
        self.assertIn('vm_rig_idle_02',{n for s in inverse.seeds for n in inverse.emit(s)})
        numbered=next(p for p in plans if p.key=='numbered' and p.kind=='xanim')
        self.assertIn('vm_rig_idle_02',{n for s in numbered.seeds for n in numbered.emit(s)})
        tail=next(p for p in plans if p.key=='sound-tails')
        self.assertIn(audio,{n for s in tail.seeds for n in tail.emit(s)})
