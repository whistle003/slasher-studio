import tempfile
from pathlib import Path
from types import SimpleNamespace
import struct
from unittest import TestCase, main

import studio_core as core
import studio_community as pr
from studio_vendor.github_api import GitHubError, _blob_sha


def snapshot(**pools):
    census=[]
    records=[]
    for i,(kind,keys) in enumerate(pools.items()):
        ids={k&core.MASK for k in keys}
        census.append(f'{i} {kind} {len(ids)}\n')
        records.extend((k,i) for k in ids)
    return (b'CODIDS'+struct.pack('<HH',1,7)+b'MODWAR7'+struct.pack('<Q',len(records))
            +b''.join(struct.pack('<QH',k,i) for k,i in sorted(records))),''.join(census).encode()


class GitHub:
    def __init__(self, upstream, csv_bytes, existing):
        self.now='1'*40
        self.database='2'*40
        self.base_tree='3'*40
        self.reviewed=existing
        self.tree_call=None
        self.posts=[]
        self.highest=[]
        self.upstream_entries={}
        self.db_entries={}
        self.data={}
        self._add(self.upstream_entries,'snapshots/modwar7.ids',upstream[0])
        self._add(self.upstream_entries,'snapshots/modwar7.pools.txt',upstream[1])
        self._add(self.db_entries,'csv/fnv1a_soundbanks_aliases_v2.csv',csv_bytes)
        self.already={'filename':'submissions/Someone_MODWAR7_20261117-120000/sound_alias_20261117-120000.txt','status':'added','sha':_blob_sha(existing)}
        self.data[self.already['sha']]=existing

    def _add(self,target,path,data):
        sha=_blob_sha(data)
        target[path]={'path':path,'type':'blob','sha':sha,'size':len(data)}
        self.data[sha]=data

    def tree(self,repo):
        docs=self.upstream_entries if repo==pr.UPSTREAM else self.db_entries
        return {'commit':self.now if repo==pr.UPSTREAM else self.database,'tree_sha':self.base_tree,'entries':list(docs.values())}

    def blobs(self,repo,files):
        return {e['sha']:self.data[e['sha']] for e in files}

    def paginate(self,path):
        if path.endswith('/pulls/1/files'):return [self.already]
        if 'state=open' in path:return [{'number':1,'head':{'sha':'4'*40}}]
        if 'state=all&head=' in path:return self.highest
        raise AssertionError(path)

    def request(self,method,path,payload=None):
        if method=='GET':
            if path=='/user':return {'login':'TestAccount'}
            if path.endswith('/commits/HEAD'):return {'sha':self.now if pr.UPSTREAM in path else self.database}
            if path.endswith('/pulls/1'):return {'head':{'sha':'4'*40},'state':'open','changed_files':1}
            if '/git/ref/heads/' in path:raise GitHubError('fixture not found',404)
            if path=='/repos/TestAccount/hash-slinging-slasher':return {'fork':True,'parent':{'full_name':pr.UPSTREAM}}
            if path==pr.BASE:return {'default_branch':'main'}
            raise AssertionError(path)
        self.posts.append((path,payload))
        if path.endswith('/git/trees'):
            self.tree_call=payload
            return {'sha':'5'*40}
        if path.endswith('/git/commits'):return {'sha':'6'*40}
        if path.endswith('/git/refs'):return {'object':{'sha':'6'*40}}
        if path.endswith('/pulls'):
            result={'html_url':f'https://github.com/{pr.UPSTREAM}/pull/4321','head':{'ref':payload['head'].split(':',1)[1]}}
            self.highest.append(result)
            return result
        raise AssertionError(path)


class PRFlow(TestCase):
    def test_preview_and_public_payload(self):
        kind='sound_alias'
        words=('studio_fixture_fresh', 'studio_fixture_claimed', 'studio_fixture_missing', 'studio_fixture_database')
        fresh,claimed,missing,known=keys=tuple(core.name_key(n,kind) for n in words)
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)
            table=f'{known:x},{words[3]}\n'.encode()
            (path/(core.TABLE[kind]+'.csv')).write_bytes(table)
            pkg='rex_studio_test_animpackage'
            pkg_key=core.name_key(pkg,'animpkg')
            model='rex_studio_test_model'
            model_key=core.name_key(model,'xmodel')
            pools={kind:{k&core.MASK for k in keys},'animpkg':{pkg_key},'xmodel':{model_key}}
            ctx=SimpleNamespace(game='MODWAR7', pools=pools, raw=set(keys), table_dir=path)
            api=GitHub(snapshot(sound_alias=[fresh,claimed,known],animpkg=[pkg_key],xmodel=[model_key]),
                       table,f'{claimed:x},{words[1]}\n'.encode())
            api._add(api.db_entries,'csv/fnv1a_animpkgs_v2.csv',b'')
            (path/(core.TABLE['animpkg']+'.csv')).write_bytes(b'')
            review=pr.prepare(ctx, {kind:dict(zip(keys,words)),'animpkg':{pkg_key:pkg},'xmodel':{model_key:model}},
                              api, path, lambda _:None)
            self.assertEqual(review.count,2)
            self.assertFalse(api.posts)
            self.assertEqual(review.excluded,{'模型结果仅本地使用':1,'上游快照未收录':1,'已合并或开放 PR 已有':1,'社区名称表已有':1})
            self.assertIn(words[0],(path/'pr-preview.txt').read_text(encoding='utf-8'))
            self.assertNotIn(folder,str(review.files))
            api.now='7'*40
            with self.assertRaisesRegex(ValueError,'已变更'):pr.publish(review,api,lambda _:None)
            self.assertFalse(api.posts)
            api.now='1'*40
            self.assertTrue(pr.publish(review,api,lambda _:None).endswith('/4321'))
            self.assertEqual(api.tree_call['base_tree'],'3'*40)
            rows=[e['content'] for e in api.tree_call['tree'] if e['path'].endswith('.txt')]
            self.assertCountEqual(rows,[f'{fresh:x},{words[0]}\n',f'{pkg_key:x},{pkg}\n'])
            self.assertFalse(any('/xmodel_' in e['path'] for e in api.tree_call['tree']))
            self.assertEqual(len(api.posts),4)
            self.assertTrue(pr.publish(review,api,lambda _:None).endswith('/4321'))
            self.assertEqual(len(api.posts),4)


if __name__=='__main__':main()
