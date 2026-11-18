"""Verify saved search progress survives pauses, failed batches and mode changes."""

from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

import studio_core as core
import studio_community as community
import studio_tasks as tasks


class ResumeTest(TestCase):
    def test_resume_adds_cast_names_to_current_task_and_keeps_cursor(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            ids=root/'modwar7.ids'
            ids.write_bytes(b'fixture')
            ids.with_suffix('.pools.txt').write_text('fixture')
            candidate_file=root/'candidates.txt'
            candidate_file.write_text('previous_candidate\n')
            saluki=root/'saluki/hash_pkg'
            saluki.mkdir(parents=True)
            (root/(core.TABLE['xanim']+'.csv')).touch()
            name='rex_vm_ar_qz9_v07_selectsemi_on'
            key=core.name_key(name,'xanim')
            source=core.Inputs(str(ids),str(root),str(saluki),str(root/'out'),str(candidate_file),kinds=('xanim',))
            ctx=core.Context(source,ids,root,saluki,root/'out','MODWAR7',{'xanim':{key,1}},set(),2)
            task=tasks.create(ctx,tasks.Pause(),lambda _:None)
            opts=dict(engine=lambda *_:[],override_claims=set())
            tasks.resume(task,ctx,root/'profile',tasks.Pause(),lambda _:None,**opts)
            self.assertEqual(tasks.overview(task)[1:3],(1,1))

            exports=saluki.parent/'exported_files/mw7'
            pkg=exports/'animpkgs'
            clips=exports/'animations'
            pkg.mkdir(parents=True)
            clips.mkdir()
            target=f'anim_{key:016x}'
            (pkg/'sample.json').write_text(json.dumps({'anims':{'1':target,'2':'anim_1'}}))
            (clips/f'{target}.cast').write_bytes(b'cast\0wfoly_rex_plr_ar_qz9_v07_selectsemi_on_01\0')
            (clips/'anim_1.cast').write_bytes(b'cast\0end\0')
            active=replace(ctx,source=replace(source,adaptive=True))
            logs=[]
            with patch.object(tasks.adaptive,'run',return_value=(0,'完成')):
                out,rows,done,maximum,_=tasks.resume(task,active,root/'profile',tasks.Pause(),logs.append,**opts)
                self.assertEqual((done,maximum),(1,1))
                self.assertEqual(rows,{'xanim':{key:name}})
                self.assertIn(name,(out/'txt/xanim.txt').read_text())
                self.assertEqual(core.read_cdb(out/'saluki-ready/hash_pkg'/(core.TABLE['xanim']+'.cdb')),{key:name})
                self.assertTrue((out/'pr-preview.txt').is_file())
                tasks.resume(task,active,root/'profile',tasks.Pause(),logs.append,**opts)
            self.assertIn('MW7 动画包与 CAST 线索已验证，沿用任务记录。',logs)

    def test_incremental_exclusions_keep_shared_ids_until_every_spelling_is_known(self):
        high = core.MASK+1
        pools = {'material':{1,2,3}, 'image':{1,5}, 'sound_alias':{1,2,4}}
        ctx = core.Context(None,None,None,None,None,'MODWAR7',pools,{1,high|1,high|2,4},8)
        baseline = {('image',1),('material',1),('sound_alias',1),
                    ('sound_alias',high|2),('material',3)}
        found = {('image',5)}
        claims = community.SearchExclusions(ctx,baseline,found)
        self.assertEqual(claims.safe,{3,5})
        for batch,wanted in (
                ([('sound_alias',1)],{3,5}),
                ([('sound_alias',high|1)],{1,3,5}),
                ([('material',2),('sound_alias',4)],{1,2,3,4,5})):
            old = claims.safe.copy()
            self.assertEqual(claims.add(iter(batch)), wanted-old)
            found.update(batch)
            self.assertEqual(claims.safe,wanted)
            self.assertEqual(claims.safe,community.safe_ids(ctx,baseline|found))

    def test_failed_batch_is_retried_without_repeating_committed_results(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            names = ['wpn_alpha_01', 'wpn_beta_01', 'wpn_gamma_01']
            kind = 'xanim'
            keys = {name: core.name_key(name, kind) for name in names}
            ids = root/'modwar7.ids'
            ids.write_bytes(b'fixture')
            ids.with_suffix('.pools.txt').write_text('fixture', encoding='ascii')
            candidates = root/'names.txt'
            candidates.write_text('\n'.join(names), encoding='ascii')
            (root/(core.TABLE[kind]+'.csv')).touch()
            source = core.Inputs(str(ids), str(root), '', str(root/'out'),
                                 str(candidates), kinds=(kind,))
            ctx = core.Context(source, ids, root, None, root/'out', 'MODWAR7',
                               {kind: set(keys.values())}, set(), len(keys))
            task = tasks.create(ctx, tasks.Pause(), lambda _: None)
            attempted = []
            def engine(_work, _ctx, _kinds, _mode, _opens, _ends, words, _log):
                name, = words
                attempted.append(name)
                if name == names[1] and attempted.count(name) == 1:
                    raise RuntimeError('interrupted before committing')
                return [(kind, f'{keys[name]:016x}', name)]

            with patch.object(tasks, 'BATCH', 1):
                opts = dict(engine=engine, override_claims=set())
                with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                    tasks.resume(task, ctx, root/'profile', tasks.Pause(), lambda _: None, **opts)
                self.assertEqual(tasks.overview(task)[1:3], (1, 3))
                candidates.write_text('replacement', encoding='ascii')
                out, rows, done, maximum, _ = tasks.resume(
                    task, ctx, root/'profile', tasks.Pause(), lambda _: None, **opts)

            self.assertEqual(attempted, [names[0], names[1], names[1], names[2]])
            self.assertEqual((done, maximum), (3, 3))
            expected = {value: name for name, value in keys.items()}
            self.assertEqual(rows[kind], expected)
            self.assertEqual(core.read_cdb(out/'saluki-ready/hash_pkg'/(
                core.TABLE[kind]+'.cdb')), expected)

    def test_continuous_resume_passes_round_limit_and_reaches_next_pool(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            ids = root/'modwar7.ids'
            ids.write_bytes(b'fixture')
            ids.with_suffix('.pools.txt').write_text('fixture', encoding='ascii')
            source = core.Inputs(str(ids), str(root), '', str(root/'out'),
                                 kinds=('xanim', 'sound_asset'), millions=1)
            ctx = core.Context(source, ids, root, None, root/'out', 'MODWAR7',
                               {'xanim': {1}, 'sound_asset': {2}}, set(), 2)
            plan = ([f'a{i}_' for i in range(48)], [f'_b{i}' for i in range(128)],
                    [f'animation{i:03d}' for i in range(550)])
            audio = ([f's{i}_' for i in range(99)], [f'_d{i}' for i in range(19)],
                     ['sound001'])
            with patch.object(core, 'plan_words', side_effect=(plan, audio)):
                task = tasks.create(ctx, tasks.Pause(), lambda _: None)
            scanned = []

            def engine(_work, _ctx, kinds, _mode, _opens, _ends, words, _log):
                scanned.extend((kinds[0], word) for word in words)
                return []

            def run(context, pause, progress=lambda *_: None):
                return tasks.resume(task, context, root/'profile', pause, lambda _: None,
                                    progress, engine=engine, override_claims=set())

            limited = replace(ctx, source=replace(source, continuous=False))
            _, _, done, maximum, status = run(limited, tasks.Pause())
            self.assertEqual((done, status), (998718, '本轮预算已用完，仍可继续'))

            pause = tasks.Pause()
            def pause_after_next_batch(current, _):
                if current > done:
                    pause.stop()
            _, _, paused_at, _, status = run(ctx, pause, pause_after_next_batch)
            self.assertEqual((paused_at, status), (1997436, '已暂停'))
            self.assertEqual(tasks.overview(task)[1:3], (paused_at, maximum))

            _, _, completed, _, status = run(ctx, tasks.Pause())
            self.assertGreater(completed-paused_at, 1_000_000)
            self.assertEqual((completed, status), (maximum, '候选已跑完'))
            self.assertEqual(scanned, [('xanim', word) for word in plan[2]]+
                             [('sound_asset', audio[2][0])])
