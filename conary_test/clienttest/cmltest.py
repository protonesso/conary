#
# Copyright (c) SAS Institute Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#


"""
Tests for functions in the cml module
"""

from testutils import mock
from testrunner.testhelp import context

from conary_test import rephelp

from conary import conaryclient, versions
from conary.conaryclient import cml
from conary.deps import deps
from conary.repository.errors import TroveNotFound


class CMCoreTest(rephelp.RepositoryHelper):
    @context('sysmodel')
    def testCMOperation(self):
        self.assertRaises(AssertionError, cml._CMOperation)
        self.assertRaises(NotImplementedError, cml._CMOperation,
            text='foo')

        item = ('a', '1', None)
        sm = cml._CMOperation(item=item, modified=False)
        self.assertEqual(sm.item, item)
        self.assertEqual(sm.modified, False)
        item2 = ('b', '2', None)
        self.assertRaises(NotImplementedError, sm.update, item2)

    @context('sysmodel')
    def testCMLocation(self):
        loc = cml.CMLocation(line=1, context='foo', op=None)
        self.assertEqual(str(loc), 'foo:1')
        self.assertEqual(loc.asString(), 'foo:1')
        self.assertEqual(repr(loc),
            "CMLocation(line=1, context='foo', op=None, spec=None)")
        loc = cml.CMLocation(line=None, context='foo', op=None)
        self.assertEqual(str(loc), 'foo:new-line')
        self.assertEqual(loc.asString(), 'foo:new-line')
        self.assertEqual(repr(loc),
            "CMLocation(line=None, context='foo', op=None, spec=None)")
        loc = cml.CMLocation(line=1, context=None, op=None)
        self.assertEqual(str(loc), '1')
        self.assertEqual(loc.asString(), '1')
        self.assertEqual(repr(loc),
            "CMLocation(line=1, context=None, op=None, spec=None)")
        loc = cml.CMLocation(line=None, context=None, op=None)
        self.assertEqual(str(loc), 'new-line')
        self.assertEqual(loc.asString(), 'new-line')
        self.assertEqual(repr(loc),
            "CMLocation(line=None, context=None, op=None, spec=None)")

    @context('sysmodel')
    def testCMTroveSpec(self):
        ts = cml.CMTroveSpec('foo', 'a@b:c', 'baz')
        self.assertEqual(str(ts), 'foo=a@b:c[baz]')
        self.assertEqual(ts.format(), 'foo=a@b:c[baz]')
        self.assertEqual(ts.asString(), 'foo=a@b:c[baz]')
        self.assertEqual(ts.pinned, False)

        ts = cml.CMTroveSpec('foo=a@b:c[baz]')
        self.assertEqual(str(ts), 'foo=a@b:c[baz]')
        self.assertEqual(ts.format(), 'foo=a@b:c[baz]')

        ts = cml.CMTroveSpec('foo==a@b:c[baz]')
        self.assertEqual(str(ts), 'foo==a@b:c[baz]')
        self.assertEqual(ts.format(), 'foo==a@b:c[baz]')
        self.assertEqual(ts.asString(), 'foo==a@b:c[baz]')
        self.assertEqual(ts.pinned, True)
        self.assertEqual(ts._has_branch, False)
        self.assertEqual(ts.snapshot, False)

        ts = cml.CMTroveSpec('foo=/a@b:c[baz]')
        self.assertEqual(str(ts), 'foo=/a@b:c[baz]')
        self.assertEqual(ts.format(), 'foo=/a@b:c[baz]')
        self.assertEqual(ts.asString(), 'foo=/a@b:c[baz]')
        self.assertEqual(ts.pinned, False)
        self.assertEqual(ts._has_branch, False)
        self.assertEqual(ts.snapshot, False)

        ts = cml.CMTroveSpec('foo=a@b:c/1.2-1-1[baz]')
        self.assertEqual(str(ts), 'foo=a@b:c/1.2-1-1[baz]')
        self.assertEqual(ts.format(), 'foo=a@b:c/1.2-1-1[baz]')
        self.assertEqual(ts.asString(), 'foo=a@b:c/1.2-1-1[baz]')
        self.assertEqual(ts.pinned, False)
        self.assertEqual(ts._has_branch, True)
        self.assertEqual(ts.snapshot, True)


    @context('sysmodel')
    def testSearchOperation(self):
        self.assertRaises(NotImplementedError, cml.SearchOperation,
            text='foo@bar:baz')

    @context('sysmodel')
    def testSearchTrove(self):
        s1 = cml.SearchTrove(text='foo=foo@bar:baz[~blah]')
        self.assertEqual(repr(s1),
            "SearchTrove(text='foo=foo@bar:baz[~blah]', modified=True, index=None)")
        self.assertEqual(s1.format(), 'search foo=foo@bar:baz[~blah]')
        self.assertEqual(s1.asString(), 'foo=foo@bar:baz[~blah]')
        self.assertEqual(str(s1), 'foo=foo@bar:baz[~blah]')
        self.assertEqual(s1.item, ('foo', 'foo@bar:baz', deps.parseFlavor('~blah')))

        s2 = cml.SearchTrove(item = s1.item, modified=True, index=1)
        self.assertEqual(repr(s2),
            "SearchTrove(text='foo=foo@bar:baz[~blah]', modified=True, index=1)")
        self.assertEqual(s2.format(), 'search foo=foo@bar:baz[~blah]')
        self.assertEqual(s2.asString(), 'foo=foo@bar:baz[~blah]')
        self.assertEqual(str(s2), 'foo=foo@bar:baz[~blah]')

        st = cml.SearchTrove('foo=bar:baz[blah]', modified=False)
        self.assertEqual(st.modified, False)
        item2 = (st.item[0], 'bar:blah', st.item[2])
        st.update(item2)
        self.assertEqual(st.item, item2)
        self.assertEqual(st.modified, True)

    @context('sysmodel')
    def testSearchLabel(self):
        s1 = cml.SearchLabel(text='foo@bar:baz')
        self.assertEqual(repr(s1),
            "SearchLabel(text='foo@bar:baz', modified=True, index=None)")
        self.assertEqual(s1.format(), 'search foo@bar:baz')
        self.assertEqual(s1.asString(), 'foo@bar:baz')

        s2 = cml.SearchLabel(item = s1.item, modified=False, index=1)
        self.assertEqual(repr(s2),
            "SearchLabel(text='foo@bar:baz', modified=False, index=1)")
        self.assertEqual(s2.format(), 'search foo@bar:baz')
        self.assertEqual(s2.asString(), 'foo@bar:baz')
        self.assertEqual(str(s2), 'foo@bar:baz')

    @context('sysmodel')
    def testIncludeOperation(self):
        s1 = cml.IncludeOperation(text='foo=foo@bar:baz[~blah]')
        self.assertEqual(repr(s1),
            "IncludeOperation(text='foo=foo@bar:baz[~blah]', modified=True, index=None)")
        self.assertEqual(s1.format(), 'include foo=foo@bar:baz[~blah]')
        self.assertEqual(s1.asString(), 'foo=foo@bar:baz[~blah]')
        self.assertEqual(str(s1), 'foo=foo@bar:baz[~blah]')
        self.assertEqual(s1.item, ('foo', 'foo@bar:baz', deps.parseFlavor('~blah')))

        s2 = cml.IncludeOperation(item = s1.item, modified=True, index=1)
        self.assertEqual(repr(s2),
            "IncludeOperation(text='foo=foo@bar:baz[~blah]', modified=True, index=1)")
        self.assertEqual(s2.format(), 'include foo=foo@bar:baz[~blah]')
        self.assertEqual(s2.asString(), 'foo=foo@bar:baz[~blah]')
        self.assertEqual(str(s2), 'foo=foo@bar:baz[~blah]')

        st = cml.IncludeOperation('foo=bar:baz[blah]', modified=False)
        self.assertEqual(st.modified, False)
        item2 = (st.item[0], 'bar:blah', st.item[2])
        st.update(item2)
        self.assertEqual(st.item, item2)
        self.assertEqual(st.modified, True)

    @context('sysmodel')
    def testNoOperation(self):
        t1 = cml.NoOperation('')
        t2 = cml.NoOperation(text='')
        t3 = cml.NoOperation(item='')
        self.assertEqual(repr(t1), repr(t2))
        self.assertEqual(repr(t2), repr(t3))
        self.assertEqual(repr(t1), 
            "NoOperation(text='', modified=True, index=None)")
        t4 = cml.NoOperation('foo', modified=False, index=1)
        self.assertEqual(repr(t4), 
            "NoOperation(text='foo', modified=False, index=1)")

    @context('sysmodel')
    def testVersionOperation(self):
        t1 = cml.VersionOperation('1.0')
        self.assertEqual(t1.asString(), '1.0')
        self.assertEqual(str(t1), '1.0')
        self.assertEqual(t1.format(), 'version 1.0')
        self.assertEqual(repr(t1), 
            "VersionOperation(text='1.0', modified=True, index=None)")
        t2 = cml.VersionOperation('foo', modified=False, index=1)
        self.assertEqual(t2.asString(), 'foo')
        self.assertEqual(str(t2), 'foo')
        self.assertEqual(t2.format(), 'version foo')
        self.assertEqual(repr(t2), 
            "VersionOperation(text='foo', modified=False, index=1)")

    @context('sysmodel')
    def testTroveOperation(self):
        t1 = cml.TroveOperation('foo')
        self.assertEqual(t1.item, [('foo', None, None)])
        self.assertEqual(t1.asString(), 'foo')
        self.assertEqual(repr(t1),
            "TroveOperation(text=['foo'], modified=True, index=None)")

        t2 = cml.TroveOperation(['foo'])
        self.assertEqual(t2.item, [('foo', None, None)])
        self.assertEqual(t2.asString(), 'foo')

        t3 = cml.TroveOperation(['foo', 'bar=a@b:c'])
        self.assertEqual(t3.item, [('foo', None, None),
                                    ('bar', 'a@b:c', None)])
        iterList = [x for x in t3]
        self.assertEqual(iterList, [('foo', None, None),
                                     ('bar', 'a@b:c', None)])
        self.assertEqual(t3.asString(), 'foo bar=a@b:c')

    @context('sysmodel')
    def testTroveOperations(self):
        t1 = cml.UpdateTroveOperation('foo', index=1)
        self.assertEqual(str(t1.getLocation()), '1')
        self.assertEqual(repr(t1.getLocation()),
              "CMLocation(line=1, context=None,"
              " op=UpdateTroveOperation(text=['foo'], modified=True, index=1),"
              " spec=None)")
        self.assertEqual(t1.getLocation().op, t1)
        self.assertEqual(t1.item, [('foo', None, None)])
        self.assertEqual(t1.asString(), 'foo')
        self.assertEqual(str(t1), 'foo')
        self.assertEqual(t1.format(), 'update foo')
        self.assertEqual(repr(t1),
            "UpdateTroveOperation(text=['foo'], modified=True, index=1)")

        t2 = cml.EraseTroveOperation(['foo'], index=2, context='foo')
        self.assertEqual(str(t2.getLocation()), 'foo:2')
        self.assertEqual(repr(t2.getLocation()),
              "CMLocation(line=2, context='foo',"
              " op=EraseTroveOperation(text=['foo'], modified=True, index=2),"
              " spec=None)")
        self.assertEqual(t2.getLocation().op, t2)
        self.assertEqual(t2.item, [('foo', None, None)])
        self.assertEqual(t2.asString(), 'foo')
        self.assertEqual(str(t2), 'foo')
        self.assertEqual(t2.format(), 'erase foo')
        self.assertEqual(repr(t2),
            "EraseTroveOperation(text=['foo'], modified=True, index=2)")

        t3 = cml.InstallTroveOperation(['foo', 'bar=a@b:c'])
        self.assertEqual(t3.item, [('foo', None, None),
                                    ('bar', 'a@b:c', None)])
        self.assertEqual(t3.asString(), 'foo bar=a@b:c')
        self.assertEqual(str(t3), 'foo bar=a@b:c')
        self.assertEqual(t3.format(), 'install foo bar=a@b:c')
        self.assertEqual(repr(t3),
            "InstallTroveOperation(text=['foo', 'bar=a@b:c'], modified=True, index=None)")

        t4 = cml.PatchTroveOperation(['foo', 'bar=a@b:c', 'baz[f]'])
        self.assertEqual(t4.item, [('foo', None, None),
                                    ('bar', 'a@b:c', None),
                                    ('baz', None, deps.parseFlavor('f'))])
        self.assertEqual(t4.asString(), 'foo bar=a@b:c baz[f]')
        self.assertEqual(str(t4), 'foo bar=a@b:c baz[f]')
        self.assertEqual(t4.format(), 'patch foo bar=a@b:c baz[f]')
        self.assertEqual(repr(t4),
            "PatchTroveOperation(text=['foo', 'bar=a@b:c', 'baz[f]'], modified=True, index=None)")

        t5 = cml.OfferTroveOperation(['foo', 'bar=a@b:c'])
        self.assertEqual(t5.item, [('foo', None, None),
                                    ('bar', 'a@b:c', None)])
        self.assertEqual(t5.asString(), 'foo bar=a@b:c')
        self.assertEqual(str(t5), 'foo bar=a@b:c')
        self.assertEqual(t5.format(), 'offer foo bar=a@b:c')
        self.assertEqual(repr(t5),
            "OfferTroveOperation(text=['foo', 'bar=a@b:c'], modified=True, index=None)")


class CMTest(rephelp.RepositoryHelper):

    @staticmethod
    def getCM():
        cfg = mock.MockObject()
        cfg._mock.set(installLabelPath = ['a@b:c', 'd@e:f' ])
        cfg._mock.set(flavor = deps.parseFlavor(''))
        cfg._mock.set(modelPath = '/etc/conary/system-model')
        return cml.CM(cfg)

    @context('sysmodel')
    def testCMOperations(self):
        m = self.getCM()
        self.assertEqual(m.SearchTrove,
                cml.SearchTrove)
        self.assertEqual(m.SearchLabel,
                cml.SearchLabel)
        self.assertEqual(m.SearchOperation,
                cml.SearchOperation)
        self.assertEqual(m.IncludeOperation,
                cml.IncludeOperation)
        self.assertEqual(m.NoOperation,
                cml.NoOperation)
        self.assertEqual(m.VersionOperation,
                cml.VersionOperation)
        self.assertEqual(m.UpdateTroveOperation,
                cml.UpdateTroveOperation)
        self.assertEqual(m.EraseTroveOperation,
                cml.EraseTroveOperation)
        self.assertEqual(m.InstallTroveOperation,
                cml.InstallTroveOperation)
        self.assertEqual(m.OfferTroveOperation,
                cml.OfferTroveOperation)
        self.assertEqual(m.PatchTroveOperation,
                cml.PatchTroveOperation)
        
    @context('sysmodel')
    def testCMRepresentation(self):
        m = self.getCM()
        gs = cml.SearchTrove(text='group-foo=g@h:i',
            modified=False, index=1)
        m.appendOp(gs)
        gl = cml.SearchLabel(text='j@k:l', modified=False, index=2)
        m.appendOp(gl)
        nop = cml.NoOperation('# comment', modified=False, index=3)
        m.appendNoOperation(nop)
        at = cml.UpdateTroveOperation(text=['bar', 'blah'],
            modified=False, index=4)
        m.appendOp(at)
        rt = cml.EraseTroveOperation(text='baz',
            modified=False, index=5)
        m.appendOp(rt)
        inc = cml.IncludeOperation(text='cml-foo', modified=False, index=6)
        m.appendOp(inc)

        self.assertEqual(m.getVersion(), None)
        ver = cml.VersionOperation('2.0', modified=False, index=7)
        m.setVersion(ver)
        self.assertEqual(str(m.getVersion()), '2.0')

        self.assertEqual(len(m.modelOps), 5)
        self.assertEqual(m.modelOps[0], gs)
        self.assertEqual(m.modelOps[1], gl)
        self.assertEqual(m.modelOps[2], at)
        self.assertEqual(m.modelOps[3], rt)
        self.assertEqual(m.modelOps[4], inc)
        self.assertEqual(m.noOps[0], nop)
        self.assertEqual(len(m.noOps), 1)
        self.assertEqual(sorted(m.indexes.keys()), [1,2,3,4,5,6,7])
        self.assertEqual(m.indexes[1], [gs])
        self.assertEqual(m.indexes[2], [gl])
        self.assertEqual(m.indexes[3], [nop])
        self.assertEqual(m.indexes[4], [at])
        self.assertEqual(m.indexes[5], [rt])
        self.assertEqual(m.indexes[6], [inc])
        self.assertEqual(m.indexes[7], [ver])
        self.assertEqual(m.modified(), False)

    @context('sysmodel')
    def testAddNoOpByText(self):
        m = self.getCM()
        m.appendNoOpByText('#foo', modified=False, index=1)
        self.assertEqual(len(m.noOps), 1)
        self.assertEqual(str(m.noOps[0]), '#foo')
        self.assertEqual(sorted(m.indexes.keys()), [1])

    @context('sysmodel')
    def testAddOperationsByName(self):
        m = self.getCM()
        m.appendOpByName('install', text=['group-foo'],
            modified=False, index=1)
        m.appendOpByName('update', text=['bar', 'blah'],
            modified=False, index=2)
        m.appendOpByName('patch', text='group-errata-1234',
            modified=False, index=3)
        m.appendOpByName('erase', text='baz',
            modified=False, index=4)
        m.appendOpByName('offer', text='optional',
            modified=False, index=5)
        m.appendOpByName('include', text='cml-foo',
            modified=False, index=6)

        self.assertEqual(len(m.modelOps), 6)
        self.assertEqual(m.modelOps[0].format(), 'install group-foo')
        self.assertEqual(m.modelOps[1].format(), 'update bar blah')
        self.assertEqual(m.modelOps[2].format(), 'patch group-errata-1234')
        self.assertEqual(m.modelOps[3].format(), 'erase baz')
        self.assertEqual(m.modelOps[4].format(), 'offer optional')
        self.assertEqual(m.modelOps[5].format(), 'include cml-foo')
        self.assertEqual(sorted(m.indexes.keys()), [1,2,3,4,5,6])

    @context('sysmodel')
    def testRemoveOp(self):
        m = self.getCM()
        m.appendOpByName('install', text=['group-foo'],
            modified=False, index=1)
        op = m.appendOpByName('update', text=['bar', 'blah'],
            modified=False, index=2)
        m.appendOpByName('patch', text='group-errata-1234',
            modified=False, index=3)

        self.assertEqual(len(m.modelOps), 3)
        m.removeOp(op)
        self.assertEqual(len(m.modelOps), 2)

    @context('sysmodel')
    def testAddEraseOperation(self):
        m = self.getCM()
        # as if from the existing system model
        m.appendOpByName('install', text='group-foo',
            modified=False, index=1)
        m.appendOpByName('erase', text='baz',
            modified=False, index=2)

        self.assertEqual(len(m.modelOps), 2)
        self.assertEqual(str(m.modelOps[0]), 'group-foo')
        self.assertEqual(str(m.modelOps[1]), 'baz')
        self.assertEqual(m.modelOps[0].format(), 'install group-foo')
        self.assertEqual(m.modelOps[1].format(), 'erase baz')

        m.appendOpByName('update', text=['bar', 'blah'],
            modified=False, index=3)
        self.assertEqual(m.modelOps[2].format(), 'update bar blah')

        m.appendOpByName('erase', text='bar')
        self.assertEqual(m.modelOps[2].format(), 'update bar blah')
        self.assertEqual(m.modelOps[3].format(), 'erase bar')

    @context('sysmodel')
    def testRefreshVersionSnapshots(self):
        m = self.getCM()

        mockClient = mock.MockObject()
        self.mock(conaryclient, 'ConaryClient', mockClient)
        repos = mockClient().getRepos()
        repos.findTroves._mock.setDefaultReturn(
            {('group-foo', 'g@h:i', None):
                 [('group-foo', 
                   versions.VersionFromString('/g@h:i/1.0-1-2'),
                   deps.parseFlavor('foo'))],
             ('group-foo', 'g@h:i/1.0-1-1', None):
                 [('group-foo',
                   versions.VersionFromString('/g@h:i/1.0-1-1'),
                   deps.parseFlavor('foo'))],
             ('bar', 'g@h:i', None):
                 [('bar', 
                   versions.VersionFromString('/g@h:i/2.0-1-2'),
                   deps.parseFlavor('foo'))],
             ('bar', 'g@h:i/2.0-1-1', None):
                 [('bar',
                   versions.VersionFromString('/g@h:i/2.0-1-1'),
                   deps.parseFlavor('foo'))],
             ('pinned', 'g@h:i', None):
                 [('pinned', 
                   versions.VersionFromString('/g@h:i/2.0-1-2'),
                   deps.parseFlavor('foo'))],
             ('pinned', 'g@h:i/2.0-1-1', None):
                 [('pinned',
                   versions.VersionFromString('/g@h:i/2.0-1-1'),
                   deps.parseFlavor('foo'))],
             ('cml-inc', 'g@h:i', None):
                 [('cml-inc', 
                   versions.VersionFromString('/g@h:i/1.0-1-2'),
                   deps.parseFlavor('foo'))],
             ('cml-inc', 'g@h:i/1.0-1-1', None):
                 [('cml-inc',
                   versions.VersionFromString('/g@h:i/1.0-1-1'),
                   deps.parseFlavor('foo'))],
            }
        )

        gs = cml.SearchTrove(text='group-foo=g@h:i/1.0-1-1',
            modified=False, index=1)
        m.appendOp(gs)
        gl = cml.SearchLabel(text='j@k:l', modified=False, index=2)
        m.appendOp(gl)
        up = cml.UpdateTroveOperation(text='bar=g@h:i/2.0-1-1',
            modified=False, index=3)
        m.appendOp(up)
        pi = cml.InstallTroveOperation(text='pinned==g@h:i/2.0-1-1',
            modified=False, index=4)
        m.appendOp(pi)
        inc = cml.IncludeOperation(text='cml-inc=g@h:i/1.0-1-1',
            modified=False, index=5)
        m.appendOp(inc)

        self.assertEqual(m.modified(), False)
        self.assertEqual([x.format() for x in m.modelOps],
            ['search group-foo=g@h:i/1.0-1-1',
             'search j@k:l',
             'update bar=g@h:i/2.0-1-1',
             'install pinned==g@h:i/2.0-1-1',
             'include cml-inc=g@h:i/1.0-1-1'])
        m.refreshVersionSnapshots()
        self.assertEqual(m.modified(), True)
        self.assertEqual([x.format() for x in m.modelOps],
            ['search group-foo=g@h:i/1.0-1-2',
             'search j@k:l',
             'update bar=g@h:i/2.0-1-2',
             'install pinned==g@h:i/2.0-1-1',
             'include cml-inc=g@h:i/1.0-1-2'])

        # and now, if it doesn't change:
        repos.findTroves._mock.setDefaultReturn(
            {('group-foo', 'g@h:i', None):
                 [('group-foo', 
                   versions.VersionFromString('/g@h:i/1.0-1-2'),
                   deps.parseFlavor('foo'))],
             ('group-foo', 'g@h:i/1.0-1-2', None):
                 [('group-foo',
                   versions.VersionFromString('/g@h:i/1.0-1-2'),
                   deps.parseFlavor('foo'))],
             ('bar', 'g@h:i', None):
                 [('bar', 
                   versions.VersionFromString('/g@h:i/2.0-1-2'),
                   deps.parseFlavor('foo'))],
             ('bar', 'g@h:i/2.0-1-2', None):
                 [('bar',
                   versions.VersionFromString('/g@h:i/2.0-1-2'),
                   deps.parseFlavor('foo'))],
             ('pinned', 'g@h:i', None):
                 [('pinned', 
                   versions.VersionFromString('/g@h:i/2.0-1-2'),
                   deps.parseFlavor('foo'))],
             ('pinned', 'g@h:i/2.0-1-1', None):
                 [('pinned',
                   versions.VersionFromString('/g@h:i/2.0-1-1'),
                   deps.parseFlavor('foo'))],
             ('cml-inc', 'g@h:i', None):
                 [('cml-inc', 
                   versions.VersionFromString('/g@h:i/1.0-1-2'),
                   deps.parseFlavor('foo'))],
             ('cml-inc', 'g@h:i/1.0-1-2', None):
                 [('cml-inc',
                   versions.VersionFromString('/g@h:i/1.0-1-2'),
                   deps.parseFlavor('foo'))],
            }
        )
        m.refreshVersionSnapshots()
        self.assertEqual(m.modified(), True)
        self.assertEqual([x.format() for x in m.modelOps],
            ['search group-foo=g@h:i/1.0-1-2',
             'search j@k:l',
             'update bar=g@h:i/2.0-1-2',
             'install pinned==g@h:i/2.0-1-1',
             'include cml-inc=g@h:i/1.0-1-2'])

        # Old group is missing
        findtroves = {
             ('group-foo', 'g@h:i', None):
                 [('group-foo', 
                   versions.VersionFromString('/g@h:i/1.0-1-2'),
                   deps.parseFlavor('foo'))],
             #('group-foo', 'g@h:i/1.0-1-2', None):
             ('bar', 'g@h:i', None):
                 [('bar', 
                   versions.VersionFromString('/g@h:i/2.0-1-2'),
                   deps.parseFlavor('foo'))],
             ('bar', 'g@h:i/2.0-1-2', None):
                 [('bar',
                   versions.VersionFromString('/g@h:i/2.0-1-2'),
                   deps.parseFlavor('foo'))],
             ('pinned', 'g@h:i', None):
                 [('pinned', 
                   versions.VersionFromString('/g@h:i/2.0-1-2'),
                   deps.parseFlavor('foo'))],
             ('pinned', 'g@h:i/2.0-1-1', None):
                 [('pinned',
                   versions.VersionFromString('/g@h:i/2.0-1-1'),
                   deps.parseFlavor('foo'))],
             ('cml-inc', 'g@h:i', None):
                 [('cml-inc', 
                   versions.VersionFromString('/g@h:i/1.0-1-2'),
                   deps.parseFlavor('foo'))],
             ('cml-inc', 'g@h:i/1.0-1-2', None):
                 [('cml-inc',
                   versions.VersionFromString('/g@h:i/1.0-1-2'),
                   deps.parseFlavor('foo'))],
            }
        repos.findTroves._mock.setDefaultReturn(findtroves)
        m.refreshVersionSnapshots()
        self.assertEqual(m.modified(), True)
        self.assertEqual([x.format() for x in m.modelOps],
            ['search group-foo=g@h:i/1.0-1-2',
             'search j@k:l',
             'update bar=g@h:i/2.0-1-2',
             'install pinned==g@h:i/2.0-1-1',
             'include cml-inc=g@h:i/1.0-1-2'])

        # New version is missing
        del findtroves[('group-foo', 'g@h:i', None)]
        err = self.assertRaises(TroveNotFound, m.refreshVersionSnapshots)
        self.assertEqual(str(err), "Trove not found: group-foo=g@h:i")
        del findtroves[('bar', 'g@h:i', None)]
        err = self.assertRaises(TroveNotFound, m.refreshVersionSnapshots)
        self.assertEqual(str(err),
                "2 troves not found:\nbar=g@h:i\ngroup-foo=g@h:i")

    def testNoRefreshLocalVersions(self):
        m = self.getCM()

        mockClient = mock.MockObject()
        self.mock(conaryclient, 'ConaryClient', mockClient)
        repos = mockClient().getRepos()

        pi = cml.InstallTroveOperation(text='foo=/local@local:COOK/2.0-1-1',
            modified=False, index=4)
        m.appendOp(pi)

        self.assertEqual(m.modified(), False)
        self.assertEqual([x.format() for x in m.modelOps],
            ['install foo=/local@local:COOK/2.0-1-1'])
        m.refreshVersionSnapshots()
        repos.findTroves._mock.assertNotCalled()
        self.assertEqual([x.format() for x in m.modelOps],
            ['install foo=/local@local:COOK/2.0-1-1'])
        self.assertEqual(m.modified(), False)


class CMLTest(rephelp.RepositoryHelper):

    def getCML(self, *args):
        cfg = mock.MockObject()
        cfg._mock.set(installLabelPath = ['a@b:c', 'd@e:f' ])
        cfg._mock.set(flavor = deps.parseFlavor(''))
        cfg._mock.set(root = self.rootDir)
        cfg._mock.set(modelPath = '/etc/conary/system-model')
        return cml.CML(cfg, *args)

    @context('sysmodel')
    def testInit(self):
        smf = self.getCML()
        self.assertEqual(smf.filedata, [])
        smf.parse(fileData=['# comment\n'])
        self.assertEqual(smf.filedata, ['# comment\n'])
        self.assertEqual(repr(smf.noOps[0]),
            repr(cml.NoOperation('# comment', modified=False, index=1)))
        smf.parse() # does not raise an exception

    @context('sysmodel')
    def testCMReset(self):
        smf = self.getCML()
        smf.parse(fileData=['# comment\n', 'search foo\n', 'install bar\n'])
        self.assertEqual(smf.format(),
            '# comment\n'
            'search foo\n'
            'install bar\n')
        smf.parse(fileData=['# comment\n', 'search foo\n'])
        self.assertEqual(smf.format(),
            '# comment\n'
            'search foo\n')
        smf.reset()
        self.assertEqual(smf.format(), '')
        self.assertEqual(smf.modelOps, [])

    @context('sysmodel')
    def testQuotedData(self):
        "Ensure that data that will be split by shlex is saved quoted"
        smf = self.getCML()
        
        smf.parse(fileData=[
            'search group-foo=bar:baz/1.2[a, b is: x86_64]',
            'search group-foo=bar:baz/1.2[a, b is: x86(i386,i486,i586,cmov)]',
            'install foo',])
        smf.modelOps[0].modified = True
        smf.modelOps[1].modified = True
        smf.appendOpByName('install',
            'blah[~foo, !bar, ~!baz is: x86(cmov)]')
        self.assertEqual(smf.format(), '\n'.join((
            "search 'group-foo=bar:baz/1.2[a,b is: x86_64]'",
            "search 'group-foo=bar:baz/1.2[a,b is: x86(cmov,i386,i486,i586)]'",
            "install foo",
            "install 'blah[!bar,~!baz,~foo is: x86(cmov)]'",
            ""
        )))

    @context('sysmodel')
    def testQuotedDataMultiItemLine(self):
        "Ensure that data that will be split by shlex is saved quoted"
        smf = self.getCML()
        trvspecs = [ 'foo[is: x86 x86_64]', 'bar[is: x86 x86_64]' ]
        smf.appendOpByName('install', text = trvspecs)
        expected = '\n'.join([
            "install %s" % ' '.join("'%s'" % x for x in trvspecs),
            "",
        ])
        self.assertEqual(smf.format(), expected)
        # Parse it again, make sure we get the same results
        smf = self.getCML()
        smf.parse(fileData=expected.strip().split('\n'))
        self.assertEqual(smf.format(), expected)
        self.assertEqual(
                [ len(x.item) for x in smf.modelOps ],
                [ 2 ])

    @context('sysmodel')
    def testStartFromScratch(self):
        smf = self.getCML()

        smf.appendNoOpByText('# an initial comment')
        self.assertEqual(smf.format(),
            '# an initial comment\n')

        smf.appendOpByName('update', 'foo')
        self.assertEqual(smf.format(),
            '# an initial comment\n'
            'update foo\n')

        smf.appendOp(cml.SearchLabel('a@b:c'))
        self.assertEqual(smf.format(),
            '# an initial comment\n'
            'update foo\n'
            'search a@b:c\n')

        smf.setVersion(cml.VersionOperation('1.0'))
        self.assertEqual(smf.format(),
            '# an initial comment\n'
            'version 1.0\n'
            'update foo\n'
            'search a@b:c\n')

        smf.appendOpByName('offer', 'optional')
        self.assertEqual(smf.format(),
            '# an initial comment\n'
            'version 1.0\n'
            'update foo\n'
            'search a@b:c\n'
            'offer optional\n')

        smf.appendOpByName('include', 'cml-inc')
        self.assertEqual(smf.format(),
            '# an initial comment\n'
            'version 1.0\n'
            'update foo\n'
            'search a@b:c\n'
            'offer optional\n'
            'include cml-inc\n')

        smf.appendNoOpByText('# a trailing comment', index=999)
        self.assertEqual(smf.format(),
            '# an initial comment\n'
            'version 1.0\n'
            'update foo\n'
            'search a@b:c\n'
            'offer optional\n'
            'include cml-inc\n'
            '# a trailing comment\n')

        smf.appendNoOpByText('# another trailing comment', index=999)
        self.assertEqual(smf.format(),
            '# an initial comment\n'
            'version 1.0\n'
            'update foo\n'
            'search a@b:c\n'
            'offer optional\n'
            'include cml-inc\n'
            '# a trailing comment\n'
            '# another trailing comment\n')

        smf2 = smf.copy()
        self.assertEqual(smf.format(), smf2.format())

    @context('sysmodel')
    def testParseWrite(self):
        fileData = '\n'.join((
            '# Initial comment',
            'search group-foo=a@b:c/1-1-1',
            '# comment 2',
            'offer optional',
            'update foo #disappearing act',
            '# comment 3',
            'install bar',
            '# comment 4',
            'patch baz',
            '# comment 5',
            'erase blah',
            '# comment 6',
            'include cml-inc',
            '# comment 7',
            'version 1.0 # ensure it does not move',
            '',
        ))
        smf = self.getCML()
        smf.parse(fileData=[x+'\n' for x in fileData.split('\n')][:-1])
        self.assertEqual(smf.format(), fileData)
        self.assertEqual(str(smf.modelOps[3].getLocation(
                                  smf.modelOps[3].item[0])), '7:bar')
        self.assertEqual(smf.modelOps[3].format(), 'install bar')
        smf.modelOps[2].modified=True
        self.assertEqual(smf.modified(), True)
        modFileData = fileData.replace(' #disappearing act', '')
        self.assertEqual(smf.format(), modFileData)
        smf.appendOp(cml.UpdateTroveOperation('newtrove'))
        modFileData = modFileData.replace('include cml-inc\n',
                                          'include cml-inc\nupdate newtrove\n')
        self.assertEqual(smf.format(), modFileData)
        smf.appendOp(cml.SearchLabel('d@e:f'))
        modFileData = modFileData.replace('update newtrove\n',
                                          'update newtrove\nsearch d@e:f\n')
        self.assertEqual(smf.format(), modFileData)

        mockFile = mock.MockObject()
        smf.write(mockFile)
        mockFile.write._mock.assertCalled(modFileData)

        smf.parse(fileData=[x+'\n' for x in fileData.split('\n')][:-1],
                  context='foo')
        self.assertEqual(str(smf.modelOps[3].getLocation(
                                    smf.modelOps[3].item[0])),
                          'foo:7:bar')
        self.assertEqual(smf.modelOps[3].format(), 'install bar')


    @context('sysmodel')
    def testParseFail(self):
        smf = self.getCML()
        e =self.assertRaises(cml.CMError,
            smf.parse, fileData=['badverb noun'], context='/foo')
        self.assertEqual(str(e), '/foo:1: Unrecognized command "badverb"')
        e =self.assertRaises(cml.CMError,
            smf.parse, fileData=['badverb'], context='/foo')
        self.assertEqual(str(e), '/foo:1: Invalid statement "badverb"')
        e =self.assertRaises(cml.CMError,
            smf.parse, fileData=['search foo=bar=baz@blah@blah:1-1-1-1-1'])
        self.assertEqual(str(e),
            '/foo:1: Error with spec "foo=bar=baz@blah@blah:1-1-1-1-1":'
            " Too many ='s")
        e = self.assertRaises(cml.CMError,
            smf.parse, fileData=['install "'], context='/foo')
        self.assertEqual(str(e), '/foo:1: No closing quotation')
        e = self.assertRaises(cml.CMError,
            smf.parse, fileData=['search "'], context='/foo')
        self.assertEqual(str(e), '/foo:1: No closing quotation')

    @context('sysmodel')
    def testEmptyEverything(self):
        smf = self.getCML()
        self.assertEqual(smf.format(), '')

    @context('sysmodel')
    def testImmediateErasureInModelData(self):
        fileData = '\n'.join((
            'search group-foo=a@b:c/1-1-1',
            'install foo',
            'erase foo',
            '',
        ))
        smf = self.getCML()
        smf.parse(fileData=[x+'\n' for x in fileData.split('\n')][:-1])
        # ensure that erasure is not short-circuited -- model should
        # not be truncated to just the search line.
        self.assertEqual(smf.format(), fileData)
