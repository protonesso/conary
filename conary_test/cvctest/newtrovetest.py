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


from io import StringIO
from conary_test import rephelp

from conary import changelog
from conary import trove
from conary import versions
from conary.conaryclient import filetypes
from conary.deps import deps
from conary.repository import changeset


class ClientNewTroveTest(rephelp.RepositoryHelper):
    def testNewFileRegularFile(self):
        repos = self.openRepository()
        client = self.getConaryClient()
        foo1 = filetypes.RegularFile(contents = 'foo1')
        foo2 = filetypes.RegularFile(contents = StringIO('foo2' * 8192))
        foo3 = filetypes.RegularFile(contents = StringIO('foo3\n'))
        files = {'/foo1': foo1, '/foo2': foo2, '/foo3': foo3}
        cs = client.createSourceTrove( \
                'foo:source', self.cfg.buildLabel, '1.0',
                files, changelog.ChangeLog('foo', 'bar'))
        repos.commitChangeSet(cs)
        n,v,f = cs.iterNewTroveList().next().getNewNameVersionFlavor()
        results = client.getFilesFromTrove(n,v,f, ['/foo1', '/foo3'])
        assert(not '/foo2' in results)
        contents = foo1.getContents()
        assert(contents.read() == 'foo1')

        contents = foo3.getContents()
        assert(contents.read() == 'foo3\n')

    def testNewFileContents(self):
        repos = self.openRepository()
        client = self.getConaryClient()
        fil = filetypes.RegularFile(contents = 'foo')
        dir = filetypes.Directory()
        sym = filetypes.Symlink('../file')
        files = {'file': fil, '/dir': dir, '/dir/sym': sym}
        cs = client.createSourceTrove( \
                'foo:source', self.cfg.buildLabel, '1.0',
                files, changelog.ChangeLog('foo', 'bar'))
        repos.commitChangeSet(cs)
        n,v,f = cs.iterNewTroveList().next().getNewNameVersionFlavor()

        trv = repos.getTrove(n, v, f)

        processed = 0
        for pathId, path, fileId, vers in trv.iterFileList():
            fileObj = repos.getFileVersion(pathId, fileId, vers)
            if path == '/dir':
                processed += 1
                self.assertEqual(fileObj.lsTag, 'd')
                self.assertEqual(fileObj.inode.perms(), 0o755)
                self.assertEqual(fileObj.hasContents, False)
            elif path == '/dir/sym':
                processed += 1
                self.assertEqual(fileObj.lsTag, 'l')
                self.assertEqual(fileObj.target(), '../file')
                self.assertEqual(fileObj.hasContents, False)
            elif path == 'file':
                processed += 1
                self.assertEqual(fileObj.lsTag, '-')
                self.assertEqual(fileObj.inode.perms(), 0o644)
                self.assertEqual(fileObj.hasContents, True)

        # make sure we looked at all the files
        self.assertEqual(processed, 3)

        fileDict = client.getFilesFromTrove(n, v, f)

        # we don't want to see dir or sym in the list. they don't have contents
        # that can be retrieved
        self.assertEqual(list(fileDict.keys()), ['file'])
        self.assertEqual(fileDict['file'].read(), 'foo')

    def testNewFileTwice(self):
        repos = self.openRepository()
        client = self.getConaryClient()
        fil = filetypes.RegularFile(contents = 'foo')
        fil2 = filetypes.RegularFile(contents = 'foo')
        files = {'file': fil, 'file2': fil2}
        cs = client.createSourceTrove( \
                'foo:source', self.cfg.buildLabel, '1.0', files,
                changelog.ChangeLog('foo', 'bar'))
        n, v, f = cs.iterNewTroveList().next().getNewNameVersionFlavor()
        self.assertEqual(str(v), '/localhost@rpl:linux/1.0-1')
        repos.commitChangeSet(cs)

        files = {'file': fil}
        # repeat the creation to show the source count gets bumped
        cs = client.createSourceTrove( \
                'foo:source', self.cfg.buildLabel, '1.0', files,
                changelog.ChangeLog('foo', 'bar'))
        n2, v2, f2 = cs.iterNewTroveList().next().getNewNameVersionFlavor()
        self.assertEqual(str(v2), '/localhost@rpl:linux/1.0-2')
        repos.commitChangeSet(cs)

        # repeat the creation to show the source count gets bumped
        cs = client.createSourceTrove( \
                'foo:source', self.cfg.buildLabel, '1.0', files,
                changelog.ChangeLog('foo', 'bar'))
        n2, v2, f2 = cs.iterNewTroveList().next().getNewNameVersionFlavor()
        self.assertEqual(str(v2), '/localhost@rpl:linux/1.0-3')
        repos.commitChangeSet(cs)

        # prove that the source count gets reset for a new upstream version
        cs = client.createSourceTrove( \
                'foo:source', self.cfg.buildLabel, '1.1', files,
                changelog.ChangeLog('foo', 'bar'))
        n2, v2, f2 = cs.iterNewTroveList().next().getNewNameVersionFlavor()
        self.assertEqual(str(v2), '/localhost@rpl:linux/1.1-1')

    def testNewFileNotSource(self):
        client = self.getConaryClient()
        fil = filetypes.RegularFile(contents = 'foo')
        files = {'file': fil}
        self.assertRaises(RuntimeError, client.createSourceTrove, \
                'foo:runtime', self.cfg.buildLabel, '1.0', files,
                changelog.ChangeLog('foo', 'bar'))

    def testNewTroveNotSource(self):
        class DummyTrove(object):
            def getNameVersionFlavor(self):
                return 'foo:runtime', None, None
        client = self.getConaryClient()
        self.assertRaises(RuntimeError, client._targetNewTroves, [DummyTrove()])

    def testNewTroveDupVersion(self):
        class DummyTrove(object):
            def getNameVersionFlavor(self):
                return 'foo:source', self.getVersion(), None
            def getVersion(self):
                return versions.VersionFromString('/localhost@rpl:linux/1.0-1')
            def changeVersion(*args, **kwargs):
                pass
        repos = self.openRepository()
        client = self.getConaryClient()

        res = self.assertRaises(RuntimeError, client._targetNewTroves,
                [DummyTrove(), DummyTrove()])

    def testNewFileFlavor(self):
        repos = self.openRepository()
        client = self.getConaryClient()
        fil = filetypes.RegularFile(contents = 'foo',
                flavor = deps.parseFlavor('xen,domU, is:x86'))
        files = {'file': fil}
        cs = client.createSourceTrove( \
                'foo:source', self.cfg.buildLabel, '1.0', files,
                changelog.ChangeLog('foo', 'bar'))
        n, v, f = cs.iterNewTroveList().next().getNewNameVersionFlavor()

        # source troves don't have a flavor
        self.assertEqual(f, deps.Flavor())

    def testRemoveOldPathIds(self):
        class DummyTroveObj(object):
            def __init__(x):
                x.tracked = []
            def iterFileList(x):
                return [['a'], ['b'], ['c']]
            def removePath(x, pathId):
                x.tracked.append(pathId)

        client = self.getConaryClient()
        trv = DummyTroveObj()
        client._removeOldPathIds(trv)
        self.assertEqual(trv.tracked, ['a', 'b', 'c'])

    def testPreservePathIds(self):
        self.openRepository()
        client = self.getConaryClient()
        repos = client.getRepos()
        fil = filetypes.RegularFile(contents = 'foo')
        cs = client.createSourceTrove( \
                'foo:source', self.cfg.buildLabel, '1.0', {'file': fil},
                changelog.ChangeLog('foo', 'bar'))

        n, v, f = cs.iterNewTroveList().next().getNewNameVersionFlavor()
        repos.commitChangeSet(cs)
        trv = repos.getTrove(n, v, f)
        fileList1 = list(trv.iterFileList())

        # repeat without changing the file, but bump the upstream version
        cs = client.createSourceTrove( \
                'foo:source', self.cfg.buildLabel, '1.1', {'file': fil},
                changelog.ChangeLog('foo', 'bar'))
        n, v, f = cs.iterNewTroveList().next().getNewNameVersionFlavor()
        repos.commitChangeSet(cs)
        trv = repos.getTrove(n, v, f)
        fileList2 = list(trv.iterFileList())

        # repeat but change the file, also bump the upstream version
        fil = filetypes.RegularFile(contents = 'bar')
        cs = client.createSourceTrove( \
                'foo:source', self.cfg.buildLabel, '1.2', {'file': fil},
                changelog.ChangeLog('foo', 'bar'))
        n, v, f = cs.iterNewTroveList().next().getNewNameVersionFlavor()
        repos.commitChangeSet(cs)
        trv = repos.getTrove(n, v, f)
        fileList3 = list(trv.iterFileList())
        self.assertEqual(fileList1[0][0], fileList2[0][0])
        self.assertEqual(fileList2[0][0], fileList3[0][0])

    def testNewFactory(self):
        repos = self.openRepository()
        client = self.getConaryClient()
        fil = filetypes.RegularFile(contents = 'foo')
        files = {'file': fil}
        cs = client.createSourceTrove( \
                'foo:source', self.cfg.buildLabel, '1.0', files,
                changelog.ChangeLog('foo', 'bar'), factory = 'factory-foo')
        n, v, f = cs.iterNewTroveList().next().getNewNameVersionFlavor()
        self.assertEqual(str(v), '/localhost@rpl:linux/1.0-1')
        repos.commitChangeSet(cs)

        trv = repos.getTrove(n, v, f)

        self.assertEqual(trv.troveInfo.factory(), 'factory-foo')

        # repeat without factory
        cs = client.createSourceTrove( \
                'foo:source', self.cfg.buildLabel, '1.0', files,
                changelog.ChangeLog('foo', 'bar'))
        n, v, f = cs.iterNewTroveList().next().getNewNameVersionFlavor()
        self.assertEqual(str(v), '/localhost@rpl:linux/1.0-2')
        repos.commitChangeSet(cs)
        trv = repos.getTrove(n, v, f)
        self.assertEqual(trv.troveInfo.factory(), '')

    def testChangelog(self):
        repos = self.openRepository()
        client = self.getConaryClient()
        fil = filetypes.RegularFile(contents = 'foo')
        files = {'file': fil}
        cs = client.createSourceTrove( \
                'foo:source', self.cfg.buildLabel, '1.0', files,
                changelog.ChangeLog('user', 'foo'))
        n, v, f = cs.iterNewTroveList().next().getNewNameVersionFlavor()
        self.assertEqual(str(v), '/localhost@rpl:linux/1.0-1')
        repos.commitChangeSet(cs)

        trv = repos.getTrove(n, v, f)

        self.assertEqual(trv.changeLog.freeze(),
                changelog.ChangeLog('user', 'foo').freeze())

    def testDuplicateFileObj(self):
        # re-use the exact same fileoj and prove that it gets tracked properly
        repos = self.openRepository()
        client = self.getConaryClient()
        fil = filetypes.RegularFile(contents = 'foo')
        files = {'file1': fil, 'file2': fil}
        cs = client.createSourceTrove( \
                'foo:source', self.cfg.buildLabel, '1.0', files,
                changelog.ChangeLog('user', 'foo'))
        n, v, f = cs.iterNewTroveList().next().getNewNameVersionFlavor()
        repos.commitChangeSet(cs)

        trv = repos.getTrove(n, v, f)

        self.assertEqual(sorted([x[1] for x in trv.iterFileList()]),
                ['file1', 'file2'])

    def testSourceFlag(self):
        # prove that the createSourceTrove process marks each file as source
        repos = self.openRepository()
        client = self.getConaryClient()
        fil = filetypes.RegularFile(contents = 'foo', config = True)
        fileObj = fil.get('1234567890ABCDEF')
        self.assertEqual(bool(fileObj.flags.isConfig()), True)

        files = {'file1': fil}
        cs = client.createSourceTrove( \
                'foo:source', self.cfg.buildLabel, '1.0', files,
                changelog.ChangeLog('user', 'foo'))
        n, v, f = cs.iterNewTroveList().next().getNewNameVersionFlavor()
        repos.commitChangeSet(cs)

        trv = repos.getTrove(n, v, f)
        pathId, path, fileId, fileVersion = list(trv.iterFileList())[0]
        fileObj = repos.getFileVersion(pathId, fileId, fileVersion)

        self.assertEqual(bool(fileObj.flags.isConfig()), True)

    def testPackageCreatorData(self):
        repos = self.openRepository()
        client = self.getConaryClient()

        cs = client.createSourceTrove(
                'foo:source', self.cfg.buildLabel, '1.0', {},
                changelog.ChangeLog('user', 'foo'),
                pkgCreatorData = 'FOO')
        repos.commitChangeSet(cs)

        cs = client.createSourceTrove(
                'bar:source', self.cfg.buildLabel, '1.0', {},
                changelog.ChangeLog('user', 'foo'))
        repos.commitChangeSet(cs)

        l = repos.getPackageCreatorTroves('localhost')
        assert(len(l) == 1)
        assert(l[0][0][0] == 'foo:source')
        assert(l[0][1] == 'FOO')

    def testNewTroveVersionSelection(self):
        # CNY-3028 - make sure version selection
        # picks the right version, given our constraints.
        repos = self.openRepository()
        client = self.getConaryClient()
        self.addComponent('foo:source', '1.0-1')
        self.addComponent('foo:source', '/localhost@rpl:shadow//linux/1.0-2')
        self.addComponent('foo:source', '2.0-1')
        cs = client.createSourceTrove(
                'foo:source', self.cfg.buildLabel, '1.0', {},
                changelog.ChangeLog('user', 'foo'))
        trvCs = next(cs.iterNewTroveList())
        assert(str(trvCs.getNewVersion().trailingRevision()) == '1.0-3')

    def testCreateSourceTroveWithMetadata(self):
        repos = self.openRepository()
        client = self.getConaryClient()
        metadata = dict(key1="val1", key2="val2")
        cs = client.createSourceTrove(
                'foo:source', self.cfg.buildLabel, '1.0', {},
                changelog.ChangeLog('user', 'foo'),
                metadata=metadata)
        trvCs = next(cs.iterNewTroveList())
        trv = trove.Trove(trvCs)

        self.assertEqual(
            dict(trv.troveInfo.metadata.flatten()[0].keyValue),
            metadata)

    def testCreateSourceTroveRemoved(self):
        repos = self.openRepository()
        client = self.getConaryClient()
        foo1 = filetypes.RegularFile(contents = 'foo1')
        files = {'/foo1': foo1}
        cs = client.createSourceTrove( \
                'foo:source', self.cfg.buildLabel, '1.0',
                files, changelog.ChangeLog('foo', 'bar'))
        repos.commitChangeSet(cs)
        n,v,f = cs.iterNewTroveList().next().getNewNameVersionFlavor()
        # markremove it
        cs = changeset.ChangeSet()
        trv = trove.Trove(n, v, f, type=trove.TROVE_TYPE_REMOVED)
        trv.computeDigests()
        cs.newTrove(trv.diff(None, absolute=True)[0])
        repos.commitChangeSet(cs)
        # try again
        cs = client.createSourceTrove('foo:source', self.cfg.buildLabel, '1.0',
                files, changelog.ChangeLog('foo', 'bar'))
        repos.commitChangeSet(cs)
