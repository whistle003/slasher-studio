from contextlib import closing
from dataclasses import replace
import json,sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from zipfile import ZipFile

import studio_core as core
import studio_tasks as tasks
import studio_sync as sync
import studio_history as history
import studio_sources as sources
import studio_community as community
import studio_methods as methods
import studio_pipeline as pipeline
import test_adaptive_flow as fixture


class PreflightFlow(TestCase):
    def test_legacy_model_cdb_names_are_rehashed_for_modern_pool(self):
        with TemporaryDirectory() as folder:
            root=Path(folder);ctx,names,keys=fixture.AdaptiveFlow().fixture(root)
            model='wpn_verified_model';key=core.name_key(model,'xmodel')
            (root/(core.TABLE['material']+'.csv')).touch()
            source=replace(ctx.source,kinds=('xmodel',))
            original=root/'saluki';original.mkdir()
            txt=pipeline.export_names(original,'xmodel',{123:model})
            pipeline.convert(txt,original/'merged-csv/fnv1a_xmodels.csv',original/'saluki-ready/hash_pkg/fnv1a_xmodels.cdb',{},lambda _:None)
            ctx=replace(ctx,source=source,pools={'xmodel':{key}},saluki_dir=original/'saluki-ready/hash_pkg')
            task=root/'task';task.mkdir()
            with patch.object(history,'roots_for',return_value={root}):
                _,models,_=history.prepare(ctx,root/'profile',task,None,lambda _:None)
            self.assertEqual(models,{('xmodel',key)})

    def test_public_refresh_checks_remote_and_drops_closed_pr_claims(self):
        with TemporaryDirectory() as folder:
            root=Path(folder);ctx,names,keys=fixture.AdaptiveFlow().fixture(root)
            archive=root/'bundled.zip';commit='a'*40;head='b'*40
            with ZipFile(archive,'w') as z:z.writestr('survey.json',json.dumps({'commit':commit}))
            path='submissions/Test_MODWAR7_20261118/xanim_20261118-010000.txt'
            diff=f'diff --git a/{path} b/{path}\n--- /dev/null\n+++ b/{path}\n@@ -0,0 +1 @@\n+{keys[names[1]]:x},{names[1]}\n'.encode()
            opened=True;calls=[]
            def get(url,*_):
                calls.append(url)
                if 'info/refs' in url:return (commit+' refs/heads/main\n').encode()
                if url.endswith('.diff'):return diff
                rows=[dict(number=1,headSha=head,state='REPO_PULL_REQUEST_STATE_OPEN',repoNameWithOwner=sync.REPO)] if opened else []
                data=dict(results=rows,currentPage=1,totalPages=1,totalCount=len(rows),openCount=len(rows))
                return ('<script data-target="react-app.embeddedData">'+json.dumps({'payload':{'repoPullsDashboardContentRoute':data}})+'</script>').encode()
            with patch.object(community,'report_zip',lambda:ZipFile(archive)):
                claims,stamp=sync.sync(ctx,root/'profile',lambda _:None,get=get)
                self.assertEqual(claims,{('xanim',keys[names[1]])})
                calls.clear()
                again,_=sync.sync(ctx,root/'profile',lambda _:None,get=get)
                self.assertEqual(again,claims)
                self.assertEqual(sum('info/refs' in s for s in calls),2)
                self.assertFalse(any(s.endswith('.diff') for s in calls))
                opened=False
                closed,_=sync.sync(ctx,root/'profile',lambda _:None,get=get)
                self.assertEqual(closed,set())
            with self.assertRaises(ValueError):sync.added_rows(diff.replace(b'+1 @@',b'+1,2 @@'))

    def test_history_union_and_current_task_exclusion_and_cache(self):
        with TemporaryDirectory() as folder:
            root=Path(folder);ctx,names,keys=fixture.AdaptiveFlow().fixture(root)
            task=tasks.create(ctx,tasks.Pause(),lambda _:None)
            old=root/'history';old.mkdir()
            original=root/'saluki';original.mkdir()
            stem=core.TABLE['xanim']
            for dest,name in ((old,names[1]),(original,names[0]),(task,names[2])):
                txt=pipeline.export_names(dest,'xanim',{keys[name]:name})
                pipeline.convert(txt,dest/'merged-csv'/(stem+'.csv'),dest/'saluki-ready/hash_pkg'/(stem+'.cdb'),{},lambda _:None)
            ctx=replace(ctx,saluki_dir=original/'saluki-ready/hash_pkg')
            before=(ctx.saluki_dir/(stem+'.cdb')).read_bytes()
            with patch.object(history,'roots_for',return_value={root}):
                combined,models,info=history.prepare(ctx,root/'profile',task,None,lambda _:None)
                self.assertEqual(core.read_cdb(combined/(stem+'.cdb')),{keys[n]:n for n in names[:2]})
                with patch.object(history,'convert',side_effect=AssertionError('cached libraries should be reused')):
                    self.assertEqual(history.prepare(ctx,root/'profile',task,None,lambda _:None)[0],combined)
                out=root/'export';out.mkdir()
                history.complete_export(ctx,out,combined)
                self.assertEqual(core.read_cdb(out/'saluki-ready/hash_pkg'/(stem+'.cdb')),core.read_cdb(combined/(stem+'.cdb')))
            self.assertEqual((ctx.saluki_dir/(stem+'.cdb')).read_bytes(),before)

    def test_sync_failure_keeps_checkpoint_and_never_starts_engine(self):
        with TemporaryDirectory() as folder:
            root=Path(folder);ctx,*_=fixture.AdaptiveFlow().fixture(root)
            task=tasks.create(ctx,tasks.Pause(),lambda _:None)
            before=(task/tasks.QUEUE).read_bytes()
            with patch.object(sync,'sync',side_effect=OSError('offline')),patch.object(tasks,'process_batch') as engine:
                with self.assertRaisesRegex(ValueError,'任务进度保留'):
                    tasks.resume(task,ctx,root/'profile',tasks.Pause(),lambda _:None,engine=engine)
                engine.assert_not_called()
            self.assertEqual((task/tasks.QUEUE).read_bytes(),before)

    def test_upgrade_preserves_legacy_cursors_and_adds_model_only(self):
        with TemporaryDirectory() as folder:
            root=Path(folder);ctx,*_=fixture.AdaptiveFlow().fixture(root)
            task=tasks.create(ctx,tasks.Pause(),lambda _:None)
            with closing(tasks.connect(task)) as db:
                with db:
                    db.execute("update meta set value='3' where name='schema'")
                    db.execute('insert into groups(kinds,mode,width,count,pos,words) values (?,?,?,?,?,?)',
                               (json.dumps(['xanim','material','image']),'manual',1,7,4,''))
                    db.execute("insert into manual(name) values('model_name')")
                pools={k:{1} for k in ('xmodel','xanim','material','image')}
                changed=replace(ctx,pools=pools,source=replace(ctx.source,kinds=tuple(pools)))
                meta=tasks.record(db);old=json.loads(meta['inputs']);old['kinds']=['xanim','material','image'];meta['inputs']=json.dumps(old)
                tasks.upgrade(task,db,meta,changed,tasks.Pause(),lambda _:None)
                groups={tuple(json.loads(k)):(n,p) for k,n,p in db.execute('select kinds,count,pos from groups')}
                for k in ('xanim','material','image'):self.assertEqual(groups[(k,)],(7,4))
                self.assertEqual(groups[('xmodel',)],(1,0))
                self.assertTrue((task/'progress-before-1.11.sqlite3').exists())

    def test_priority_spans_fixed_and_adaptive_methods(self):
        with TemporaryDirectory() as folder:
            root=Path(folder);ctx,names,keys=fixture.AdaptiveFlow().fixture(root)
            kinds=('image','material','sound_alias','sound_asset','xanim','xmodel')
            pools={k:{core.name_key('unknown_'+k,k)} for k in kinds}
            for k in kinds:
                if k in core.TABLE:(root/(core.TABLE[k]+'.csv')).touch(exist_ok=True)
            ctx=replace(ctx,pools=pools,raw=pools['sound_alias'],source=replace(ctx.source,kinds=kinds))
            with patch.object(core,'plan_words',return_value=([],[],['model_candidate'])):
                task=tasks.create(ctx,tasks.Pause(),lambda _:None)
            # A legacy static image group must not run ahead of adaptive animation/audio.
            with closing(tasks.connect(task)) as db:
                with db:db.execute('insert into groups(kinds,mode,width,count,words) values (?,?,?,?,?)',
                                   ('["image"]','plan',1,1,json.dumps([[],[],['image_candidate']])))
            order=[]
            def plans(*_):
                return [methods.Method('test',k,k,(k,),100 if k=='image' else 1,[k+'_candidate'],lambda n:iter((n,))) for k in kinds if k!='xmodel']
            def engine(work,ctx,kinds,mode,opens,ends,words,log):order.append(kinds[0]);return []
            with patch.object(methods,'prepare',plans):
                tasks.resume(task,ctx,root/'profile',tasks.Pause(),lambda _:None,engine=engine,override_claims=set())
            self.assertEqual([core.PRIORITY[k] for k in order],sorted(core.PRIORITY[k] for k in order))
            self.assertEqual(order[0],'xmodel')
            self.assertTrue(set(kinds).issubset(order))

