"""Exercise OpenCL parity and make sure a failed GPU batch stays resumable."""

from contextlib import ExitStack
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

import studio_core as core
import studio_gpu as gpu
import studio_tasks as tasks


class GPUFlow(TestCase):
    def test_device_search_checks_both_bases_and_full_alias_keys(self):
        with TemporaryDirectory() as temp, ExitStack() as resources:
            root = Path(temp)
            snapshot = root/'modwar7.ids'
            snapshot.with_suffix('.pools.txt').write_text('3 material 1\n7 sound_alias 2\n')
            mat = 'm/gpu_verified_material'
            alias = next(n for i in range(100) if core.name_key(
                n:=f'rex_gpu_check_{i}', 'sound_alias')>core.MASK)
            decoy = 'rex_gpu_wrong_high_bit'
            h = core.name_key(alias, 'sound_alias')
            other = core.name_key(decoy, 'sound_alias')
            m = core.name_key(mat,'material')
            ctx = core.Context(None,snapshot,root,None,root,'MODWAR7',
                               {'material':{m},'sound_alias':{h&core.MASK,other&core.MASK}},
                               {h,other^(1<<63)},3)
            if not (Path(os.environ.get('SystemRoot',''))/'System32/OpenCL.dll').is_file():
                self.skipTest('No OpenCL runtime')
            try:
                session = gpu.GpuSearch(resources, ctx, set(), lambda _: None)
            except gpu.GpuError as exc:
                if '未找到' in str(exc) or '没有可用' in str(exc):
                    self.skipTest(str(exc))
                raise
            with patch.object(gpu,'WORK_SIZE',2):
                actual = session.batch(ctx,('material','sound_alias'),'manual',[],[],
                                       ['unmatched',r'M\GPU_VERIFIED_MATERIAL',decoy,alias],set(),lambda _:None)
                self.assertCountEqual(actual, [('material',f'{m:016x}',mat),
                                               ('sound_alias',f'{h:016x}',alias)])
                matched = session.batch(ctx,('material',),'plan',['m/'],['_material'],
                                        ['gpu_verified'],set(),lambda _:None)
                self.assertEqual(matched, [('material',f'{m:016x}',mat)])

    def test_gpu_failure_uses_cpu_before_advancing_cursor(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            ids = root/'modwar7.ids'
            ids.write_bytes(b'fixture')
            ids.with_suffix('.pools.txt').write_text('fixture')
            (root/(core.TABLE['material']+'.csv')).touch()
            manual = root/'manual.txt'
            names = ['candidate1','candidate2','candidate3']
            manual.write_text('\n'.join(names))
            src = core.Inputs(str(ids),str(root),'',str(root/'output'),str(manual),
                              kinds=('material',),engine='gpu')
            keys = {name:core.name_key(name,'material') for name in names}
            ctx = core.Context(src,ids,root,None,root/'output','MODWAR7',
                               {'material':set(keys.values())},set(),len(names))
            task = tasks.create(ctx,tasks.Pause(),lambda _:None)
            attempts=[]
            class BrokenGPU:
                def __init__(self,*_):pass
                def batch(self,_ctx,_kinds,_mode,_opens,_ends,words,*_):
                    attempts.append(('gpu',list(words)))
                    if words==names[:1]:
                        return [('material',f'{keys[names[0]]:016x}',names[0])]
                    raise gpu.GpuError('mock device lost')
            def cpu(work,_ctx,_kinds,_mode,_opens,_ends,words,_log):
                attempts.append(('cpu',list(words)))
                known={int(x,16) for x in (work/'state/claimed.txt').read_text(encoding='ascii').splitlines()}
                self.assertEqual(known,{keys[n] for n in names[:names.index(words[0])]})
                return [('material',f'{keys[name]:016x}',name) for name in words]
            with patch.object(tasks,'GpuSearch',BrokenGPU), patch.object(tasks,'process_batch',cpu), patch.object(tasks,'BATCH',1):
                _,rows,done,maximum,_=tasks.resume(task,ctx,root/'profile',tasks.Pause(),
                                                  lambda _:None,engine=cpu,override_claims=set())
            self.assertEqual(attempts,[('gpu',[names[0]]),('gpu',[names[1]]),
                                       ('cpu',[names[1]]),('cpu',[names[2]])])
            self.assertEqual((done,maximum,len(rows['material'])),(3,3,3))
