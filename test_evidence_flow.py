import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import studio_core as core
import studio_evidence as evidence
import studio_zone as zone


class EvidenceFlow(TestCase):
    def test_linked_sound_match_and_full_width_alias(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            bank = base/'sndbanks/json'
            bank.mkdir(parents=True)
            alias = 'wpn_rig_test_bash'
            alias_key = core.name_key(alias,'sound_alias')
            sibling = f'mp/wpn/{alias}_01.vnn.85.48000.all'
            desired = f'mp/wpn/{alias}_03.vnn.85.48000.all'
            unrelated = f'mp/wpn/{alias}_02.vnn.85.48000.all'
            k1,k2,k3 = [core.name_key(n,'sound_asset') for n in (sibling,desired,unrelated)]
            (bank/'example.json').write_text(json.dumps([
                {'alias':alias,'snd':sibling},
                {'alias':alias,'snd':f'sound_{k2:x}'},
            ]),encoding='utf-8')
            ctx = SimpleNamespace(pools={'sound_asset':{k1,k2,k3},'sound_alias':{alias_key&core.MASK,1}},
                                  raw={alias_key^(core.MASK+1)},table_dir=base,saluki_dir=None)
            self.assertEqual(evidence.read(ctx,base),{'sound_asset':{k1:sibling,k2:desired}})
            ctx.raw = {alias_key}
            rows = evidence.read(ctx,base)
            self.assertEqual(rows['sound_alias'],{alias_key:alias})
            report = evidence.coverage(ctx,set(),rows,base)
            self.assertEqual(report['sound_alias']['without_raw64'],1)
            self.assertIn('sound_alias,0000000000000001,63',(base/'unresolved_names.csv').read_text())

    def test_zone_cipher_preserves_records_across_block_boundary(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base/'zones').mkdir()
            name = b'vm_fixture_anim_run'
            key = 0x17
            rotate_left = 3
            (base/'strings.cipher.txt').write_text('select 80 4\ncipher 4 '+f'{rotate_left:x}{key:02x}'*len(name))
            cipher = bytes((((v>>3)|(v<<5))&255)^key for v in name)
            (base/'zones/demo.zone').write_bytes(b'\0'*9 + bytes((0x80,0,len(name)+1))+cipher+b'\0')
            ident = core.name_key(name.decode(),'xanim')
            ctx = SimpleNamespace(pools={'xanim':{ident}},raw=set(),table_dir=base,saluki_dir=None)
            with patch.object(zone,'BLOCK',11):
                self.assertEqual(evidence.read(ctx,base),{'xanim':{ident:name.decode()}})
