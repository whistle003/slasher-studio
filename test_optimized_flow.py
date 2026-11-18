from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
import hashlib,json,sqlite3,threading

import studio_adaptive as adaptive
import studio_core as core
import studio_methods as methods
import studio_pipeline as pipeline
import studio_queue as queue
import studio_tasks as tasks
import test_adaptive_flow as fixture


class OptimizedFlow(TestCase):
    def test_bulk_queue_rollback_and_duplicate_compatibility(self):
        with closing(sqlite3.connect(':memory:')) as db:
            adaptive.setup(db)
            db.execute("insert into adaptive_run(id,kind,seeds) values(1,'xanim',2)");db.commit()
            rows=[(hashlib.sha256(n.encode()).digest(),n) for n in ('a','b')]
            db.execute("create trigger fail_queue before insert on adaptive_queue when new.name='b' begin select raise(abort,'simulated disk failure'); end")
            with self.assertRaises(sqlite3.DatabaseError):queue.enqueue(db,1,'xanim',2,rows,0)
            self.assertEqual(db.execute('select count(*) from adaptive_seen').fetchone()[0],0)
            self.assertEqual(db.execute('select seed_pos from adaptive_run').fetchone()[0],0)
            db.execute('drop trigger fail_queue')
            self.assertEqual(queue.enqueue(db,1,'xanim',2,rows,0),2)
            self.assertEqual(queue.enqueue(db,1,'xanim',2,rows,0),0)

    def test_bulk_queue_keeps_fifo_and_rolls_back_across_chunks(self):
        with closing(sqlite3.connect(':memory:')) as db,patch.object(queue,'SQLITE_INSERT_CHUNK',2):
            adaptive.setup(db)
            with db:
                db.execute("insert into adaptive_run(id,kind,seed_pos,seeds) values(1,'xanim',4,10)")
                db.execute('insert into adaptive_seen values (?,?)',('xanim',b'known'))
                db.execute('insert into adaptive_seen values (?,?)',('material',b'z'))
                db.execute("create trigger fail_late before insert on adaptive_queue when new.name='third' "
                           "begin select raise(abort,'late failure'); end")
            rows=[(b'z','first'),(b'known','excluded'),(b'a','second'),(b'y','third')]
            with self.assertRaises(sqlite3.DatabaseError):queue.enqueue(db,1,'xanim',9,rows,.25)
            self.assertEqual(db.execute('select kind,digest from adaptive_seen order by kind').fetchall(),
                             [('material',b'z'),('xanim',b'known')])
            self.assertEqual(db.execute('select name from adaptive_queue').fetchall(),[])
            self.assertEqual(db.execute('select seed_pos,seconds from adaptive_run').fetchone(),(4,0))
            db.execute('drop trigger fail_late')
            self.assertEqual(queue.enqueue(db,1,'xanim',9,rows,.25),3)
            self.assertEqual(db.execute('select name from adaptive_queue order by id').fetchall(),
                             [('first',),('second',),('third',)])
            self.assertEqual(db.execute('select seed_pos,seconds from adaptive_run').fetchone(),(9,.25))

    def test_feedback_preempts_backlog_and_producer_overlaps_engine(self):
        with TemporaryDirectory() as folder:
            root=Path(folder)
            ctx,names,keys=fixture.AdaptiveFlow().fixture(root)
            task=tasks.create(ctx,tasks.Pause(),lambda _:None)
            generated=threading.Event();entered=threading.Event();attempts=[]
            filler=['unused_seed_'+str(i) for i in range(5)]
            def emit(seed):
                if seed==names[0]:yield names[1]
                elif seed==names[1]:yield names[2]
                elif seed==filler[0]:
                    generated.set()
                    if not entered.wait(5):raise AssertionError('consumer never overlapped producer')
                    yield seed
                else:yield seed
            def plans(*_):return [methods.Method('slot-edits','xanim','edit',('xanim',),5,[names[0],*filler],emit)]
            def execute(work,ctx,kinds,mode,opens,ends,words,log):
                attempts.extend(words)
                if names[1] in words:
                    self.assertTrue(generated.wait(5))
                    entered.set()
                return [('xanim',f'{keys[n]:016x}',n) for n in words if n in keys]
            with patch.object(methods,'prepare',plans),patch.object(queue,'MAX_SEEDS',1):
                tasks.resume(task,ctx,root/'profile',tasks.Pause(),lambda _:None,engine=execute,override_claims=set())
            self.assertLess(attempts.index(names[2]),attempts.index(filler[0]))
            self.assertEqual(attempts.count(names[1]),1)
            self.assertEqual(attempts.count(names[2]),1)

    def test_export_reuses_unchanged_types_and_invalidates_changed_base(self):
        with TemporaryDirectory() as folder:
            root=Path(folder);ctx,names,keys=fixture.AdaptiveFlow().fixture(root)
            rows={'xanim':{keys[names[1]]:names[1]}}
            before=root/'before';before.mkdir()
            core.results_export(ctx,rows,before,lambda _:None)
            after=root/'after';after.mkdir()
            with patch.object(pipeline,'convert',wraps=pipeline.convert) as convert:
                core.results_export(ctx,rows,after,lambda _:None,previous=before)
                convert.assert_not_called()
                base=ctx.table_dir/(core.TABLE['xanim']+'.csv')
                with base.open('a') as f:f.write('11,another_local_name\n')
                changed=root/'changed';changed.mkdir()
                core.results_export(ctx,rows,changed,lambda _:None,previous=after)
                self.assertEqual(convert.call_count,1)
                self.assertIn(17,core.read_cdb(changed/'saluki-ready/hash_pkg'/(core.TABLE['xanim']+'.cdb')))
                (changed/'txt/xanim.txt').write_text('tampered')
                repaired=root/'repaired';repaired.mkdir()
                core.results_export(ctx,rows,repaired,lambda _:None,previous=changed)
                self.assertEqual(convert.call_count,2)
                self.assertIn(names[1],(repaired/'txt/xanim.txt').read_text())

    def test_old_schema_is_backed_up_and_completed_cursor_preserved(self):
        with TemporaryDirectory() as folder:
            root=Path(folder);ctx,names,keys=fixture.AdaptiveFlow().fixture(root)
            task=tasks.create(ctx,tasks.Pause(),lambda _:None)
            with closing(tasks.connect(task)) as db:
                with db:db.execute("update meta set value='2' where name='schema'")
            pause=tasks.Pause();pause.stop()
            tasks.resume(task,ctx,root/'profile',pause,lambda _:None,override_claims=set())
            with closing(tasks.connect(task)) as db:
                self.assertEqual(tasks.record(db)['schema'],'4')
            with closing(sqlite3.connect(task/'progress-before-1.10.sqlite3')) as db:
                self.assertEqual(tasks.record(db)['schema'],'2')
