import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

import studio_animpkg as animpkg
import studio_core as core
import studio_evidence as evidence


class AnimationExportTest(unittest.TestCase):
    def test_cast_and_learned_slots_propagate_between_packages(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            packages=root/'animpkgs'
            clips=root/'animations'
            packages.mkdir()
            clips.mkdir()
            seed='rex_vm_ar_qz9_v07_drum_selectsemi_off'
            first='rex_vm_ar_qz9_v07_drum_fixture_turn'
            second='rex_wm_ar_qz9_v07_drum_fingers_fixture'
            stub=lambda name:f'anim_{core.name_key(name,"xanim"):016x}'
            (packages/'first.json').write_text(json.dumps({'anims':{'10':stub(seed),'9901':stub(first)}}))
            (packages/'second.json').write_text(json.dumps({'anims':{'9901':stub(first),'9902':stub(second)}}))
            samples={'9901':'rex_vm_ar_peer_fixture_turn','9902':'rex_wm_ar_peer_fingers_fixture'}
            (packages/'words.json').write_text(json.dumps({'anims':samples}))
            note=b'AudioOneShot@wfoly_rex_plr_ar_qz9_v07_drum_selectsemi_off_03'
            (clips/f'{stub(seed)}.cast').write_bytes(b'cast\0'+note+b'\0')
            values={core.name_key(name,'xanim'):name for name in (seed,first,second,*samples.values())}
            ctx=SimpleNamespace(game='MODWAR7',pools={'xanim':set(values)},raw=set(),table_dir=root,saluki_dir=None)
            log=[]
            self.assertEqual(evidence.read(ctx,root,log=log.append),{'xanim':values})
            self.assertIn('已验证同包扩展：确认 2 个。',log)

    def test_cast_event_keeps_inspect_variant_and_checks_exact_reference(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            packages=root/'animpkgs'
            packages.mkdir()
            clips=root/'animations'
            clips.mkdir()
            expected={}
            payload={}
            for slot,(action,name) in enumerate((
                ('inspect_02','rex_vm_ar_qz9_v07_inspect_02'),
                ('inspect_on','rex_vm_ar_qz9_v07_xmag_inspect_on'),
                ('inspect_04','rex_vm_ar_qz9_v07_inspect'),
                ('inspect_off','rex_vm_ar_qz9_v07_reload'),
            )):
                key=core.name_key(name,'xanim')
                stem=f'anim_{key:016x}'
                payload[str(slot)]=stem
                notify=f'AudioOneShot@wfoly_rex_plr_ar_qz9_v07_{action}_09'
                (clips/f'{stem}.cast').write_bytes(b'cast\x00'+notify.encode()+b'\x00')
                if action!='inspect_off':
                    expected[key]=name
            (packages/'sample.json').write_text(json.dumps({'anims':payload}),encoding='utf-8')
            ctx=SimpleNamespace(game='MODWAR7',pools={'xanim':{int(n[5:],16) for n in payload.values()}},
                                raw=set(),table_dir=root,saluki_dir=None)
            log=[]
            self.assertEqual(evidence.read(ctx,root,log=log.append),{'xanim':expected})
            self.assertIn('CAST 动作音效：4 个待查引用有事件线索，直接确认 3 个；同包槽位再确认 0 个。',log)

    def test_reload_notifies_supply_verified_fire_prefix_to_same_package(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            packages=root/'animpkgs'
            packages.mkdir()
            clips=root/'animations'
            clips.mkdir()
            reload='rex_vm_ar_qz9_apex_drum_branched_reload_empty_01_ads'
            fire='rex_vm_ar_qz9_apex_drum_fire_last'
            reload_key=core.name_key(reload,'xanim')
            fire_key=core.name_key(fire,'xanim')
            reload_stem=f'anim_{reload_key:016x}'
            fire_stem=f'anim_{fire_key:016x}'
            notify=b'AudioOneShot@wfoly_rex_plr_ar_qz9_apex_drum_branched_reload_empty_01_13'
            (clips/f'{reload_stem}.cast').write_bytes(b'cast\0'+notify+b'\0')
            (packages/'sample.json').write_text(json.dumps({'anims':{
                '69':'rex_vm_ar_borrowed_idle_active','77':fire_stem,'1606':reload_stem,
            }}),encoding='utf-8')
            (packages/'unrelated.json').write_text(json.dumps({'anims':{
                '77':f'anim_{core.name_key("rex_vm_ar_qz9_fire_last", "xanim"):016x}',
            }}),encoding='utf-8')
            unrelated=core.name_key('rex_vm_ar_qz9_fire_last','xanim')
            ctx=SimpleNamespace(game='MODWAR7',pools={'xanim':{fire_key,reload_key,unrelated}},
                                raw=set(),table_dir=root,saluki_dir=None)
            log=[]
            self.assertEqual(evidence.read(ctx,root,log=log.append),{'xanim':{fire_key:fire,reload_key:reload}})
            self.assertIn('CAST 动作音效：1 个待查引用有事件线索，直接确认 1 个；同包槽位再确认 1 个。',log)

    def test_bundled_slot_and_sibling_words_reach_both_entry_points(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            packages=root/'export/animpkgs'
            packages.mkdir(parents=True)
            local=root/'hash_pkg'
            named='rex_vm_ar_qz9_inspect_v17'
            paired='rex_vm_ar_qz9_raise_v17'
            slot_word='rex_vm_sm_mno_v27_pose_l_gripvertshort'
            wrong_target='rex_vm_ar_qz9_fire_v17'
            not_referenced='rex_vm_sm_mno_v27_pose_l_gripvert'
            clip_key=lambda name:core.name_key(name,'xanim')
            placeholder=lambda name:f'anim_{clip_key(name):016x}'
            (packages/'rex_ar_qz9_v17.json').write_text(json.dumps({'anims':{
                '2352':named,'1374':placeholder(paired),'1380':placeholder(wrong_target),
            }}),encoding='utf-8')
            (packages/'rex_sm_mno_v27.json').write_text(json.dumps({'anims':{
                '999999':'rex_vm_sm_mno_v27_idle','349':placeholder(slot_word),
            }}),encoding='utf-8')
            anim_cdb=core.TABLE['xanim']+'.cdb'
            core.write_cdb(local/anim_cdb,{clip_key(named):named})
            core.write_cdb(local/(core.TABLE['animpkg']+'.cdb'),{1:'untouched_package'})

            expected={clip_key(paired):paired,clip_key(slot_word):slot_word}
            log=[]
            output,report=animpkg.run(root/'export',None,root/'result',saluki=local,log=log.append)
            self.assertEqual(report.clips_added,2)
            self.assertEqual(animpkg.read_confirmed(output/animpkg.CONFIRMED)[1]['xanim'],expected)
            self.assertEqual(core.read_cdb(output/animpkg.CDB_DIR/anim_cdb),{clip_key(named):named}|expected)
            self.assertIn('用词源确认 2 个。', '\n'.join(log))

            ctx=SimpleNamespace(game='MODWAR7',pools={'xanim':set(expected)|{clip_key(not_referenced)}},
                                raw=set(),table_dir=root,saluki_dir=local)
            self.assertEqual(evidence.read(ctx,root/'export'),{'xanim':expected})
            ctx.game='MODWAR22'
            self.assertEqual(evidence.read(ctx,root/'export'),{})

    def test_package_family_and_variant_resolve_two_asset_types(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            exports=root/'exported_files/mw7'
            packages=exports/'animpkgs'
            packages.mkdir(parents=True)
            clips=exports/'animations/sample'
            clips.mkdir(parents=True)
            pkg_name='rex_ar_qz9_v17'
            clip_name='rex_vm_ar_qz9_v17_inspect'
            base_name='rex_ar_qz9_animpackage'
            pkg_key=core.name_key(pkg_name,'animpkg')
            clip_key=core.name_key(clip_name,'xanim')
            placeholder=f'anim_{clip_key:016x}'
            pkg=packages/f'animpkg_{pkg_key:016x}.json'
            payload={'anims':{'69':'rex_vm_ar_qz9_idle','2352':placeholder,'2356':placeholder},'other':placeholder}
            pkg.write_text(json.dumps(payload),encoding='utf-8')
            (packages/f'{base_name}.json').write_text(json.dumps({'anims':{
                '69':'rex_vm_ar_qz9_idle','2352':'rex_vm_ar_qz9_inspect'}}),encoding='utf-8')
            unknown=packages/'animpkg_0000000000000001.json'
            unknown.write_bytes(b'{"anims":{"222":"anim_0000000000000002"}}')
            clip=clips/f'{placeholder}.cast'
            clip.write_bytes(b'cast\0opaque\0animation')
            hashes=root/'hash_pkg'
            package_base={core.name_key(base_name,'animpkg'):base_name}
            existing={core.name_key('unrelated_idle','xanim'):'unrelated_idle'}
            pdbc=hashes/'fnv1a_animpkgs_v2.cdb'
            core.write_cdb(pdbc,package_base)
            cdb=hashes/'fnv1a_xanims_v2.cdb'
            core.write_cdb(cdb,existing)

            out,result=animpkg.run(exports,None,root/'result')
            verified,added=animpkg.read_confirmed(out/animpkg.CONFIRMED)
            self.assertEqual(verified,{'animpkg':{pkg_key:pkg_name},'xanim':{clip_key:clip_name}})
            self.assertEqual(added,verified)
            self.assertEqual({p.relative_to(out).as_posix() for p in out.rglob('*') if p.is_file()},{
                animpkg.CONFIRMED,f'saluki-ready/hash_pkg/{cdb.name}',f'saluki-ready/hash_pkg/{pdbc.name}',
                'txt/xanim.txt','txt/animpkg.txt','merged-csv/fnv1a_xanims_v2.csv',
                'merged-csv/fnv1a_animpkgs_v2.csv','pipeline.json'})
            self.assertEqual(json.loads(pkg.read_bytes()),payload)
            self.assertEqual(clip.read_bytes(),b'cast\0opaque\0animation')
            self.assertEqual((result.packages_added,result.clips_added,result.packages_unresolved,
                              result.clips_unresolved),(1,1,1,1))
            self.assertEqual(core.read_cdb(cdb),existing)
            self.assertEqual(core.read_cdb(out/'saluki-ready/hash_pkg'/pdbc.name),package_base|{pkg_key:pkg_name})
            self.assertEqual(core.read_cdb(out/'saluki-ready/hash_pkg'/cdb.name),existing|{clip_key:clip_name})

    def test_synced_match_is_added_to_local_base_and_unrelated_remote_record_is_not(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            packages=root/'exports';packages.mkdir()
            local=root/'saluki/hash_pkg'
            remote=root/'synced'
            xanim='synced_vm_fr_foo_jump'
            clip=core.name_key(xanim,'xanim')
            (packages/'named.json').write_text(json.dumps({'anims':{'14':f'anim_{clip:016x}'}}),encoding='utf-8')
            package_cdb=core.TABLE['animpkg']+'.cdb'
            anim_cdb=core.TABLE['xanim']+'.cdb'
            core.write_cdb(local/package_cdb,{core.name_key('named','animpkg'):'named'})
            old={core.name_key('existing_animation','xanim'):'existing_animation'}
            core.write_cdb(local/anim_cdb,old)
            core.write_cdb(remote/anim_cdb,{clip:xanim,core.name_key('remote_only','xanim'):'remote_only'})

            out,result=animpkg.run(packages,None,root/'result',saluki=local.parent,cdb_dirs=(remote,))
            self.assertEqual(result.clips_added,1)
            self.assertEqual(core.read_cdb(local/anim_cdb),old)
            self.assertEqual(core.read_cdb(out/animpkg.CDB_DIR/anim_cdb),old|{clip:xanim})
            self.assertFalse((out/animpkg.CDB_DIR/package_cdb).exists())
            second,result=animpkg.run(packages,None,root/'result',saluki=local)
            self.assertEqual(result.clips_added,0)
            self.assertFalse((second/animpkg.CDB_DIR).exists())
            with self.assertRaisesRegex(ValueError,'原库缺少'):
                animpkg.run(packages,None,root/'result',saluki=remote)


if __name__=='__main__':
    unittest.main()
