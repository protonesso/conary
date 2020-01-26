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


from testrunner import testhelp

import os
from io import StringIO
import tempfile

from conary.conaryclient import filetypes
from conary.deps import deps
from conary.lib import util

pathId = 16 * chr(0)

class ClientNewTroveTest(testhelp.TestCase):
    def testRegularFileBasics(self):
        foo = filetypes.RegularFile(contents = 'foo1')
        fileObj = foo.get(pathId)
        f = foo.getContents()
        self.assertEqual(f.read(), 'foo1')
        self.assertEqual(fileObj.flags(), 0)
        self.assertEqual(fileObj.flavor(), deps.Flavor())
        self.assertEqual(fileObj.provides(), deps.DependencySet())
        self.assertEqual(fileObj.requires(), deps.DependencySet())
        self.assertEqual(fileObj.inode.perms(), 0o644)
        self.assertEqual(fileObj.inode.owner(), 'root')
        self.assertEqual(fileObj.inode.group(), 'root')
        self.assertEqual(fileObj.lsTag, '-')
        self.assertEqual(fileObj.linkGroup(), None)
        self.assertEqual(fileObj.fileId(),
              '(\x01\x9a\xcbz\xbb\x93\x15\x01c\xcf\xd5\x14\xef\xf7,S\xbb\xf8p')

        requires = deps.ThawDependencySet('4#foo::runtime')
        provides = deps.ThawDependencySet('11#foo')
        flv = deps.parseFlavor('xen,domU is: x86')
        bar = filetypes.RegularFile(contents = StringIO('bar'),
                config = True, provides = provides, requires = requires,
                flavor = flv, owner = 'foo', group = 'bar', perms = 0o700,
                mtime = 12345, tags = ['tag1', 'tag2'])
        fileObj = bar.get(pathId)
        self.assertEqual(bool(fileObj.flags.isInitialContents()), False)
        self.assertEqual(bool(fileObj.flags.isTransient()), False)
        self.assertEqual(bool(fileObj.flags.isConfig()), True)
        self.assertEqual(fileObj.requires(), requires)
        self.assertEqual(fileObj.provides(), provides)
        self.assertEqual(fileObj.flavor(), flv)
        self.assertEqual(fileObj.inode.perms(), 0o700)
        self.assertEqual(fileObj.inode.owner(), 'foo')
        self.assertEqual(fileObj.inode.group(), 'bar')
        self.assertEqual(fileObj.inode.mtime(), 12345)
        self.assertEqual(fileObj.tags(), ['tag1', 'tag2'])

    def testRegularFileDeps(self):
        reqStr = 'trove: bar:lib'
        provStr = 'python: tarfile(2.4 lib64)'
        flavorStr = '~sse2 is: x86_64'
        foo = filetypes.RegularFile(requires = reqStr,
                provides = provStr, flavor = flavorStr)
        fileObj = foo.get(pathId)
        self.assertEqual(fileObj.flavor(), deps.parseFlavor(flavorStr))
        self.assertEqual(fileObj.requires(), deps.parseDep(reqStr))
        self.assertEqual(fileObj.provides(), deps.parseDep(provStr))

    def testRegularFileContents(self):
        foo = filetypes.RegularFile(contents = StringIO('foo1'))
        fileObj = foo.get(pathId)
        f = foo.getContents()
        self.assertEqual(f.read(), 'foo1')

        tmpDir = tempfile.mkdtemp()
        try:
            tmpPath = os.path.join(tmpDir, 'foo.txt')
            f = open(tmpPath, 'w')
            f.write('foo2')
            f.close()
            f = open(tmpPath)
            foo = filetypes.RegularFile(contents = f)
            f = foo.getContents()
            self.assertEqual(f.read(), 'foo2')
        finally:
            util.rmtree(tmpDir)

    def testSimpleTypes(self):
        for klass, lsTag in ((filetypes.Directory, 'd'),
                             (filetypes.NamedPipe, 'p'),
                             (filetypes.Socket, 's')):
            foo = klass(owner = 'foo', group = 'foo')
            fileObj = foo.get(pathId)
            self.assertEqual(fileObj.inode.perms(), 0o755)
            self.assertEqual(fileObj.lsTag, lsTag)

            self.assertRaises(filetypes.ParameterError, klass,
                    initialContents = True)
            self.assertRaises(filetypes.ParameterError, klass,
                    transient = True)
            self.assertEqual(foo.getContents(), None)

    def testSymlink(self):
        foo = filetypes.Symlink('/bar')
        fileObj = foo.get(pathId)
        self.assertEqual(fileObj.target(), '/bar')
        self.assertEqual(fileObj.lsTag, 'l')
        self.assertRaises(filetypes.ParameterError, filetypes.Symlink,
                '/bar', initialContents = True)
        self.assertRaises(filetypes.ParameterError, filetypes.Symlink,
                '/bar', transient = True)
        self.assertRaises(filetypes.ParameterError, filetypes.Symlink,
                '/bar', perms = 0o600)
        self.assertRaises(filetypes.ParameterError, filetypes.Symlink,
                '/bar', mode = 0o755)
        self.assertEqual(foo.getContents(), None)

    def testBlockDevice(self):
        foo = filetypes.BlockDevice(8, 1)
        fileObj = foo.get(pathId)
        self.assertEqual(fileObj.lsTag, 'b')
        self.assertEqual(fileObj.devt.major(), 8)
        self.assertEqual(fileObj.devt.minor(), 1)
        self.assertEqual(foo.getContents(), None)

        requires = deps.ThawDependencySet('4#foo::runtime')
        provides = deps.ThawDependencySet('11#foo')
        foo = filetypes.BlockDevice(8, 1, provides = provides,
                requires = requires)
        fileObj = foo.get(pathId)
        self.assertEqual(fileObj.provides(), provides)
        self.assertEqual(fileObj.requires(), requires)

    def testCharacterDevice(self):
        foo = filetypes.CharacterDevice(1, 5)
        fileObj = foo.get(pathId)
        self.assertEqual(fileObj.lsTag, 'c')
        self.assertEqual(fileObj.devt.major(), 1)
        self.assertEqual(fileObj.devt.minor(), 5)
        self.assertEqual(foo.getContents(), None)

        requires = deps.ThawDependencySet('4#foo::runtime')
        provides = deps.ThawDependencySet('11#foo')
        foo = filetypes.CharacterDevice(1, 5, provides = provides,
                requires = requires)
        fileObj = foo.get(pathId)
        self.assertEqual(fileObj.provides(), provides)
        self.assertEqual(fileObj.requires(), requires)

    def testLinkGroup(self):
        foo = filetypes.RegularFile(linkGroup = '12345')
        fileObj = foo.get(pathId)
        self.assertEqual(fileObj.linkGroup(), '12345')

    def testTags(self):
        # tags is a list. ensure nothing sloppy is done wrt class attributes
        foo = filetypes.RegularFile(tags = ['1', '2', '3'])
        fileObj = foo.get(pathId)
        self.assertEqual(fileObj.tags(), ['1', '2', '3'])

        # test that each invocation is separate
        foo = filetypes.RegularFile(tags = ['4', '5'])
        fileObj = foo.get(pathId)
        self.assertEqual(fileObj.tags(), ['4', '5'])

        # test that we didn't affect the default
        foo = filetypes.RegularFile()
        fileObj = foo.get(pathId)
        self.assertEqual(fileObj.tags(), [])

    def testConflictingFlags(self):
        self.assertRaises(filetypes.ConflictingFlags,
                filetypes.RegularFile, config = True, transient = True)
        self.assertRaises(filetypes.ConflictingFlags,
                filetypes.RegularFile, config = True, initialContents = True)
        self.assertRaises(filetypes.ConflictingFlags,
                filetypes.RegularFile, transient = True,
                initialContents = True)
        self.assertRaises(filetypes.ConflictingFlags,
                filetypes.RegularFile, config = True, transient = True,
                initialContents = True)

    def testModeAlias(self):
        foo = filetypes.RegularFile(mode = 0o777)
        bar = filetypes.RegularFile(perms = 0o777)
        self.assertEqual(foo.get(pathId).inode.perms(),
                bar.get(pathId).inode.perms())

        self.assertRaises(filetypes.ParameterError, filetypes.RegularFile,
                mode = 0o600, perms = 0o600)

        self.assertRaises(filetypes.ParameterError, filetypes.RegularFile,
                mode = 0o600, perms = 0o700)

    def testPathIdParam(self):
        pathId1 = 16 * '1'
        pathId2 = 16 * '2'

        foo = filetypes.RegularFile(mode = 0o777, mtime = 1)

        fileObj1 = foo.get(pathId1)
        fileObj2 = foo.get(pathId2)

        self.assertEqual(fileObj1.freeze(), fileObj2.freeze())
        self.assertNotEqual(fileObj1.pathId(), fileObj2.pathId())
