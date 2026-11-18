"""End-to-end check with the real Slasher and cod-name-db executables."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

import studio_core as core
import studio_tasks as tasks
from test_pr_flow import snapshot


class Pipeline(TestCase):
    def test_original_engines_all_five_pools_and_local_baseline(self):
        with TemporaryDirectory() as temporary:
            root=Path(temporary)
            names=dict(xanim='vm_pipeline_reload_03',image='pipeline_rifle_color',
                       material='m/pipeline_rifle',sound_asset='wpn/pipeline_fire_01.vnn.85.48000.all',
                       sound_alias='wpn_pipeline_fire')
            keys={k:core.name_key(n,k) for k,n in names.items()}
            ids,census=snapshot(**{k:{v} for k,v in keys.items()})
            capture=root/'modwar7.ids';capture.write_bytes(ids)
            capture.with_suffix('.pools.txt').write_bytes(census)
            (root/'sound_alias.raw64.txt').write_text(f'{keys["sound_alias"]:016x}\n')
            tables=root/'csv';tables.mkdir()
            # Slasher requires one million stored exclusion keys. This fixture
            # puts unrelated keys in the bank table, never in the target pools.
            (tables/'fnv1a_soundbanks_v2.csv').write_text(
                ''.join(f'{i:x},fixture_bank\n' for i in range(1_000_000)),encoding='ascii')
            baseline=root/'saluki/hash_pkg'
            for kind in names:
                (tables/(core.TABLE[kind]+'.csv')).touch()
                core.write_cdb(baseline/(core.TABLE[kind]+'.cdb'),{17:'existing_local_name'})
            candidates=root/'candidates.txt';candidates.write_text('\n'.join(names.values()))
            src=core.Inputs(str(capture),str(tables),str(baseline),str(root/'results'),
                            str(candidates),engine='cpu')
            ctx=core.inspect(src)
            logs=[]
            task=tasks.create(ctx,tasks.Pause(),logs.append)
            try:
                out,rows,*_=tasks.resume(task,ctx,root/'cache',tasks.Pause(),logs.append,override_claims=set())
            except Exception:
                self.fail('\n'.join(logs))
            self.assertEqual(rows,{k:{keys[k]:n} for k,n in names.items()})
            for kind,name in names.items():
                txt=out/'txt'/(kind+'.txt')
                self.assertEqual(dict(core.parse_finding_rows(txt.read_text())),{keys[kind]:name})
                merged=out/'saluki-ready/hash_pkg'/(core.TABLE[kind]+'.cdb')
                self.assertEqual(core.read_cdb(merged),{17:'existing_local_name',keys[kind]:name})
            self.assertEqual(len(json.loads((out/'pipeline.json').read_text())),5)
            self.assertTrue((out/'pr-preview.txt').exists())
