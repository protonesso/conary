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
import signal
import stat
import io
import tempfile
import time
import zlib
import base64
import subprocess
from conary.lib import util
from conary.lib.ext import file_utils
from conary_test import resources


class UtilTest(testhelp.TestCase):
    def testBraceGlob(self):
        d = tempfile.mkdtemp()
        expected = []
        for sub in ('foo', 'fred'):
            subdir = os.sep.join((d, sub))
            os.mkdir(subdir)
            path = os.sep.join((subdir, 'bar'))
            expected.append(path)
            f = open(path, 'w')
            f.write('hello\n')
            f.close()
        f = open(os.sep.join((d, 'file')), 'w')
        f.write('hello\n')
        f.close()
        actual = util.braceGlob(os.sep.join((d, '*', 'bar')))
        expected.sort()
        actual.sort()
        if expected != actual:
            self.fail('glob did not yield expected results.  expected "%s" got "%s"', expected, actual)
        util.rmtree(d)

    def testBraceExpand(self):
        data = [
            ('', []),
            ('a', ['a']),
            ('{a}', ['{a}']),
            ('{{a}}', ['{{a}}']),
            ('{a,b}', ['a', 'b']),
            ('{a,{b,c}}', ['a', 'b', 'c']),
            ('{a,{{b,c},d}}', ['a', 'b', 'c', 'd']),
            ('{a{b,c}}', ['{ab}', '{ac}']),
            ('{a{b},c}', ['a{b}', 'c']),
            ('{{a,b}{c,d}}', ['{ac}', '{ad}', '{bc}', '{bd}']),
            ('{{a,b}{c,d},{e,f}{g,h}}',
                ['ac', 'ad', 'bc', 'bd', 'eg', 'eh', 'fg', 'fh',]),
            ('{,a}', ['a']),
            ('a{,b}', ['a', 'ab']),
            ('a{b,}', ['ab', 'a']),
            ('{a}{b}', ['{a}{b}']),
            ('{{ab,}}', ['{ab}', '{}']),
            ('{{a,}}', ['{a}', '{}']),
            ('abc', ['abc']),
            ('aa{bb,cc}dd', ['aabbdd', 'aaccdd']),
            ('aa{bb,cc}dd{e}', ['aabbdd{e}', 'aaccdd{e}']),
            ('{a,b}}', ['a}', 'b}']),
            ('{{a,b}', ['{a', '{b']),
            ('{{{a,b}', ['{{a', '{{b']),
            ('c{{{a,b}', ['c{{a', 'c{{b']),
            ('a{,{a,b}', ['a{,a', 'a{,b']),
            (r'\{a,b}', ['{a,b}']),
            (r'{a,b\}', ['{a,b}']),
            (r'{a,b\\}', ['a', 'b\\']),
            (r'{a\,b}', ['{a,b}']),
            (r'{a\\,b}', ['a\\', 'b']),
            (r'a\b', ['ab']),
            (r'{a\b}', ['{ab}']),
        ]
        for inString, expected in data:
            self.assertEqual(util.braceExpand(inString), expected)

        # This is so we have coverage for __repr__
        l = util.BraceExpander.Alternative(['a', 'b'])
        self.assertEqual(repr(l), "Alternative['a', 'b']")
        l = util.BraceExpander.Product(['a', 'b'])
        self.assertEqual(repr(l), "Product['a', 'b']")

    def testRmtree(self):
        # test that rmtree on a directory that doesn't exist fails
        d = tempfile.mkdtemp()
        os.rmdir(d)
        self.assertRaises(OSError, util.rmtree, d)
        
        # test that rmtree on a directory that does not exist does not
        # raise an error if ignore_errors is true
        d = tempfile.mkdtemp()
        os.rmdir(d)
        util.rmtree(d, ignore_errors=True)
        assert(not os.path.exists(d))

        # test that rmtree on a file works
        d = tempfile.mkdtemp()
        fn = os.sep.join((d, 'hello'))
        f = open(fn, 'w')
        f.write('hello')
        f.close()
        util.rmtree(fn)
        assert(not os.path.exists(fn) and os.path.isdir(d))

        # test that rmtree on a dangling symlink works
        d = tempfile.mkdtemp()
        fn = os.sep.join((d, 'dangle'))
        os.symlink('dangle', fn)
        util.rmtree(fn)
        assert(not os.path.exists(fn) and os.path.isdir(d))

        # test that rmtree works
        d = tempfile.mkdtemp()
        fn = os.sep.join((d, 'hello'))
        f = open(fn, 'w')
        f.write('hello')
        f.close()
        util.rmtree(d)
        assert(not os.path.exists(d))

    def testRemove(self):
        # test removing a file while keeping the subdir
        d = tempfile.mkdtemp()
        fn = os.sep.join((d, 'hello'))
        f = open(fn, 'w')
        f.write('hello')
        subdir = os.sep.join((d, 'subdir'))
        os.mkdir(subdir)
        self.logFilter.add()
        util.remove(os.sep.join((d, '*')))
        self.logFilter.remove()
        self.logFilter.compare(('warning: Not removing directory %s' %subdir))
        assert(not os.path.exists(fn) and os.path.isdir(subdir))
        util.rmtree(d)

    def testRemoveSymlinkToDir(self):
        # test removing a symlink to a dir -- it should not follow symlink
        # even when recursive = True
        d = tempfile.mkdtemp()
        fn = os.sep.join((d, 'hello'))
        f = open(fn, 'w')
        f.write('hello')
        subdir = os.sep.join((d, 'subdir'))
        os.mkdir(subdir)
        subdir2 = os.sep.join((d, 'subdir2'))
        os.mkdir(subdir2)
        os.symlink(subdir, subdir2 + '/symlink')
        util.rmtree(subdir2 + '/*')
        assert(os.path.exists(subdir))
        assert(os.path.exists(subdir2))
        assert(not os.path.exists(subdir2 + '/symlink'))
        os.symlink(subdir, subdir2 + '/symlink')
        util.remove(subdir2 + '/*')
        assert(os.path.exists(subdir))
        assert(os.path.exists(subdir2))
        assert(not os.path.exists(subdir2 + '/symlink'))
        util.rmtree(d)

    def testCopyTree(self):
        # test copying tree with different syntaxes
        d = tempfile.mkdtemp()
        subdir = os.sep.join((d, 'subdir'))
        os.mkdir(subdir)
        fn = os.sep.join((subdir, 'hello'))
        f = open(fn, 'w')
        f.write('hello')
        d2 = tempfile.mkdtemp()
        subdir2 = os.sep.join((d2, 'subdir'))
        fn2 = os.sep.join((subdir2, 'hello'))
        util.copytree(subdir, d2)
        assert(os.path.isdir(subdir2) and os.path.exists(fn2))
        util.rmtree(subdir2)
        util.copytree(subdir + '/', d2)
        assert(os.path.isdir(subdir2) and os.path.exists(fn2))
        util.rmtree(d)

    def testTupleListBsearchInsert(self):
        def fn(a, b):
            if a[1] == b[1]:
                return 0
            elif a[1] < b[1]:
                return -1
            return 1

        # this runs all of the inserts twice to make sure duplicates don't
        # get added
        l = []

        util.tupleListBsearchInsert(l, ('v', 5), fn)
        assert(l == [('v', 5)])
        util.tupleListBsearchInsert(l, ('v', 5), fn)
        assert(l == [('v', 5)])

        util.tupleListBsearchInsert(l, ('e', 22), fn)
        assert(l == [('v', 5), ('e', 22)])
        util.tupleListBsearchInsert(l, ('e', 22), fn)
        assert(l == [('v', 5), ('e', 22)])

        util.tupleListBsearchInsert(l, ('b', 25), fn)
        assert(l == [('v', 5), ('e', 22), ('b', 25)])
        util.tupleListBsearchInsert(l, ('b', 25), fn)
        assert(l == [('v', 5), ('e', 22), ('b', 25)])

        util.tupleListBsearchInsert(l, ('y', 2), fn)
        assert(l == [('y', 2), ('v', 5), ('e', 22), ('b', 25)])
        util.tupleListBsearchInsert(l, ('y', 2), fn)
        assert(l == [('y', 2), ('v', 5), ('e', 22), ('b', 25)])

        util.tupleListBsearchInsert(l, ('g', 20), fn)
        assert(l == [('y', 2), ('v', 5), ('g', 20), ('e', 22), ('b', 25)])
        util.tupleListBsearchInsert(l, ('g', 20), fn)
        assert(l == [('y', 2), ('v', 5), ('g', 20), ('e', 22), ('b', 25)])

        util.tupleListBsearchInsert(l, ('t', 18), fn)
        assert(l == [('y', 2), ('v', 5), ('t', 18), ('g', 20), ('e', 22), ('b', 25)])
        util.tupleListBsearchInsert(l, ('t', 18), fn)
        assert(l == [('y', 2), ('v', 5), ('t', 18), ('g', 20), ('e', 22), ('b', 25)])

    def testSeekableNestedFile(self):
        (fd, name) = tempfile.mkstemp()
        f = util.ExtendedFile(name, "w++", buffering = False)
        os.close(fd)
        os.unlink(name)

        s = [ "hello world", "foo bar bang" ]
        fs = []

        f.write(s[0])
        fs.append(util.SeekableNestedFile(f, len(s[0]), 0))
        fs.append(util.SeekableNestedFile(f, len(s[1])))
        f.write(s[1])

        assert(fs[0].read() == s[0])
        assert(fs[1].read() == s[1])
        assert(fs[0].read() == "")
        assert(fs[1].read() == "")

        assert(fs[0].pread(offset = 0) == s[0])
        assert(fs[0].read() == "")

        fs[0].seek(0)
        assert(fs[1].read() == "")
        assert(fs[0].read() == s[0])
        assert(fs[0].read() == "")

        fs[0].seek(5)
        assert(fs[0].read() == s[0][5:])
        fs[0].seek(5 - len(s[0]), 2)
        assert(fs[0].read() == s[0][5:])
        fs[0].seek(5)
        fs[0].seek(2, 1)
        assert(fs[0].read() == s[0][7:])

    def testSeekableNestedFileNested(self):
        # Nested nested files
        s = "0123456789"

        (fd, name) = tempfile.mkstemp()
        f = util.ExtendedFile(name, "w+", buffering = False)
        os.close(fd)
        os.unlink(name)

        f.write(s)
        # Start from the second byte, make sure pread works
        f1 = util.SeekableNestedFile(f, 9, 1)
        first = f1.pread(1, 0)
        self.assertEqual(first, '1')

        # Create nested files within the first nested file
        f21 = util.SeekableNestedFile(f1, 5, 1)

        # Make sure pread, read, tell all work as expected
        first = f21.pread(1, 0)
        self.assertEqual(first, '2')
        self.assertEqual(f21.read(), '23456')
        self.assertEqual(f21.tell(), 5)

        f31 = util.SeekableNestedFile(f21, 3, 4)
        self.assertEqual(f31.read(), '6')

    def testPushIterator(self):
        p = util.PushIterator(x for x in range(3))
        assert(next(p) == 0)
        p.push(None)
        assert(next(p) == None)
        p.push(-1)
        p.push(-2)
        assert(next(p) == -2)
        assert(next(p) == -1)
        assert(next(p) == 1)
        assert(next(p) == 2)
        self.assertRaises(StopIteration, p.__next__)

    def testPeekIterator(self):
        p = util.PeekIterator(x for x in range(5))
        assert(p.peek() == 0)
        assert(p.peek() == 0)
        assert(next(p) == 0)
        assert(p.peek() == 1)
        assert(next(p) == 1)
        assert(next(p) == 2)
        assert(next(p) == 3)
        assert(p.peek() == 4)
        assert(next(p) == 4)
        self.assertRaises(StopIteration, p.__next__)
        self.assertRaises(StopIteration, p.peek)
        
        p = util.PeekIterator(x for x in range(5))
        [ x for x in p ] == [ 0, 1, 2, 3, 4 ]

    def testIterableQueue(self):
        q = util.IterableQueue()
        q.add(1)
        last = 0
        for item in q:
            last += 1
            assert(item == last)
            if item < 10:
                q.add(last + 1)


    def testObjectCache(self):
        class TestObject:
            def __init__(self, hash):
                self.hash = hash

            def __eq__(self, other):
                return other.hash == self.hash

            def __hash__(self):
                return self.hash
        cache = util.ObjectCache()
        obj1 = TestObject(1)
        cache[obj1] = obj1
        self.assertFalse(cache[obj1] != obj1)

        obj1copy = TestObject(1)
        cached = cache.setdefault(obj1copy, obj1copy)
        self.assertFalse(repr(cached) != repr(obj1))
        del cached

        self.assertTrue(obj1 in cache)
        self.assertTrue(obj1 in cache)
        del obj1
        self.assertFalse(list(cache.keys()) != [])

        obj2 = TestObject(2)
        cache[obj2] = obj2
        del cache[obj2]
        self.assertFalse(obj2 in cache)

        obj3 = TestObject(3)
        cached = cache.setdefault(obj3, obj3)
        self.assertTrue(cached == obj3)
        del cached
        del obj3
        self.assertFalse(list(cache.keys()) != [])

    def testRecurseDirectoryList(self):
        dirstruct = [
            ('a1', 'F'),
            ('d1', 'D'),
            ('d1/f11', 'F'),
            ('d1/f12', 'F'),
            ('d1/f13', 'L', '/tmp'),
            ('d1/f14', 'L', '/dev/null'),
            ('d12', 'F'),
            ('d2', 'D'),
            ('d2/d21', 'D'),
            ('d2/d21/d31', 'D'),
            ('f3', 'F'),
        ]
        topdir = tempfile.mkdtemp()
        # Create the directory structure
        for tup in dirstruct:
            fname, ftype = tup[:2]
            fullfname = os.path.join(topdir, fname)
            if ftype == 'D':
                os.mkdir(fullfname)
                continue
            if ftype == 'F':
                open(fullfname, "w+")
                continue
            # Link
            linksrc = tup[2]
            os.symlink(linksrc, fullfname)


        expected = ['a1', 'd1/f11', 'd1/f12', 'd1/f13', 'd1/f14', 'd12', 'f3']
        expected = [ os.path.join(topdir, f) for f in expected ]

        actual = [ f for f in util.recurseDirectoryList(topdir) ]
        self.assertEqual(actual, expected)

        expected = ['a1', 'd1', 'd1/f11', 'd1/f12', 'd1/f13', 'd1/f14', 'd12',
            'd2', 'd2/d21', 'd2/d21/d31', 'f3']
        expected = [ os.path.join(topdir, f) for f in expected ]
        expected[0:0] = [ topdir ]

        actual = [ f for f in util.recurseDirectoryList(topdir, withDirs=True) ]
        self.assertEqual(actual, expected)

        # Cleanup
        util.rmtree(topdir)

    def testNormURL(self):
        urls = (('http://example.com//a/b/c', 'http://example.com/a/b/c'),
                ('http://example.com:123/a//b/', 'http://example.com:123/a/b/'),
                ('http://example.com/a//index.html', 'http://example.com/a/index.html'),
                ('http://example.com', 'http://example.com/'),
                ('http://example.com/', 'http://example.com/'),
                ('https://conary-commits.rpath.com:443//conary/?tmpuAq85R.ccs',
                 'https://conary-commits.rpath.com:443/conary/?tmpuAq85R.ccs'))
        for input, expected in urls:
            self.assertEqual(util.normurl(input), expected)

    def testLineReader(self):
        p = os.pipe()
        pipeSize = os.fpathconf(p[0], os.pathconf_names['PC_PIPE_BUF'])

        rdr = util.LineReader(p[0])
        writeFd = p[1]

        os.write(writeFd, "hel")
        assert(rdr.readlines() == [ ])
        os.write(writeFd, "lo\n")
        assert(rdr.readlines() == [ "hello\n" ])

        os.write(writeFd, "hello\nworld\n")
        assert(rdr.readlines() == [ "hello\n", "world\n" ])

        os.write(writeFd, "hello\nthere")
        assert(rdr.readlines() == [ "hello\n" ])
        os.write(writeFd, "\nbig")
        assert(rdr.readlines() == [ "there\n" ])
        os.write(writeFd, "\nwide\nworld\n")
        assert(rdr.readlines() == [ "big\n", "wide\n", "world\n" ])

        os.close(writeFd)
        assert(rdr.readlines() == None )

        os.close(p[0])

    def testLazyFileCache(self):
        lfc = util.LazyFileCache(1000)
        procdir = "/proc/self/fd"

        # Opening file that doesn't exist fails
        self.assertRaises(IOError, lfc.open, "/dev/null/bar")

        def getOpenFiles():
            fdlist = os.listdir(procdir)
            fdlist = ((x, os.path.join(procdir, x)) for x in fdlist)
            fdlist = set((x[0], os.readlink(x[1])) for x in fdlist 
                        if os.path.exists(x[1]))
            return fdlist
        origFdCount = fdCount = len(getOpenFiles())

        fd, fn = tempfile.mkstemp()
        try:
            os.close(fd)
            f = open(fn, 'w')

            # create a sparse file
            f.seek(10000)
            f.write('\0')
            f.close()

            lf = lfc.open(fn)
            lf.read(10000)
            self.assertEqual(fdCount + 1, len(getOpenFiles()))
            self.assertEqual(fdCount + 1, lfc._getFdCount())
            self.assertEqual(lf.tell(), 10000)
            lf.close()
            self.assertEqual(fdCount, len(getOpenFiles()))
            self.assertEqual(fdCount, lfc._getFdCount())

            # Open a bunch of files
            fdlist = getOpenFiles()
            fdCount = len(fdlist)
            count = 5000
            arr = []
            for i in range(count):
                arr.append(lfc.open(fn))
            fdlist2 = getOpenFiles()
            self.assertTrue(len(set(fdlist2) - set(fdlist)) <= lfc.threshold)

            for i, fd in enumerate(arr):
                fd.read(i + 1)
                self.assertEqual(fd.tell(), i + 1)
            # Some should have been closed
            openedFds = len(getOpenFiles()) - origFdCount
            self.assertFalse(lfc.threshold < openedFds)

            for i, fd in enumerate(arr):
                self.assertEqual(fd.tell(), i + 1)
            lfc.close()
            del lfc

            self.assertFalse(origFdCount < len(getOpenFiles()))
            # All the files in the array should be closed
            for fd in arr:
                self.assertEqual(None, fd._realFd)
                self.assertEqual(None, fd._cache)
        finally:
            os.unlink(fn)

    def testLazyFileCacheKernelBug(self):
        # CNY-2571

        lfc = util.LazyFileCache(1000)
        self.assertTrue(lfc._getFdCount() > 0)

        def _dummyCountOpenFileDescriptors():
            raise OSError(util.errno.EINVAL, "Invalid argument")

        self.mock(util, 'countOpenFileDescriptors',
            _dummyCountOpenFileDescriptors)
        self.assertEqual(lfc._getFdCount(), 0)

    def testLazyFileDoubleRelease(self):
        lfc = util.LazyFileCache(1000)
        f = lfc.open("/etc/passwd")
        f._release()
        f._release()
        self.assertEqual(f._realFd, None)

    def testpread(self):
        fd, fn = tempfile.mkstemp()
        try:
            os.close(fd)
            f = open(fn, 'r+')
            f.write('hello, world!\n')
            f.flush()
            # seek the file back to the start
            os.lseek(f.fileno(), 0, 0)
            s = util.pread(f.fileno(), 6, 3)
            self.assertEqual(s, 'lo, wo')

            s = util.pread(f.fileno(), int(6), int(3))
            self.assertEqual(s, 'lo, wo')

            # make sure that pread doesn't affect the current file pos
            self.assertEqual(os.lseek(f.fileno(), 0, 1), 0)

            tmp = open('/dev/null')
            badf = tmp.fileno()
            tmp.close()
            try:
                s = util.pread(badf, 1, 0)
            except OSError as e:
                self.assertEqual(str(e), '[Errno 9] Bad file descriptor')

            f.seek(0x80000001, 0)
            f.write('1')
            f.flush()
            os.lseek(f.fileno(), 0, 0)
            s = util.pread(f.fileno(), 1, 0x80000001)
            self.assertEqual(s, '1')

            s = util.pread(f.fileno(), 1, 2**32 + 1024)

            try:
                s = util.pread(f.fileno(), 1, 0x8000000000000000)
            except OverflowError:
                pass
            try:
                s = util.pread(f.fileno(), 0x8000000000000000, 1)
            except (OverflowError, MemoryError):
                pass
        finally:
            os.unlink(fn)

    def testExtendedFile(self):
        fd, fn = tempfile.mkstemp()
        try:
            os.write(fd, "hello world")
            os.close(fd)
            f = util.ExtendedFile(fn, buffering=False)

            assert(f.read(5) == 'hello')
            assert(f.pread(5, 6) == 'world')
            assert(f.tell() == 5)
        finally:
            os.unlink(fn)

    def testFlags(self):
        class FlagTest(util.Flags):

            __slots__ = [ 'a', 'b' ]

        f = FlagTest()
        self.assertRaises(AttributeError, setattr, f, 'c', True)
        assert(not f.a)
        assert(not f.b)
        f.a = True
        assert(f.a)
        self.assertRaises(TypeError, setattr, f, 'a', 1)

        f = FlagTest(b = True)
        assert(not f.a)
        assert(f.b)

        self.assertRaises(TypeError, FlagTest, b = 7)

    # CNY-1382
    def testExecuteNoUserInput(self):
        util.execute('bash') # should return instantly with no exit code

    def testExecuteStatus(self):
        try:
            rc, s = self.captureOutput(util.execute, 'exit 1')
        except RuntimeError as e:
            self.assertEqual('Shell command "exit 1" exited with exit code 1',
                                 str(e))
        else:
            self.fail('expected exception')
        try:
            rc, s = self.captureOutput(util.execute, 'kill -9 $$')
        except RuntimeError as e:
            self.assertEqual('Shell command "kill -9 $$" killed with signal 9',
                                 str(e))
        else:
            self.fail('expected exception')


    def testStripUserPassFromUrl(self):
        self.assertEqual(util.stripUserPassFromUrl(
            'http://user:pass@host:port/path?query'),
            'http://host:port/path?query')
        self.assertEqual(util.stripUserPassFromUrl(
            'http://host:port/path?query'),
            'http://host:port/path?query')

    def testFileIgnoreEpipe(self):
        p = os.pipe()
        out = util.FileIgnoreEpipe(os.fdopen(p[1], 'w'))
        os.close(p[0])
        out.write('hello')
        out.close()

    def testBoundedStringIO(self):
        x = util.BoundedStringIO(maxMemorySize=256)
        self.assertEqual(x.getBackendType(), 'memory')
        self.assertTrue(isinstance(x._backend, io.StringIO))

        x.write("0123456789" * 30)
        self.assertEqual(x.getBackendType(), 'file')
        self.assertTrue(isinstance(x._backend, file))

        # Test truncate
        x.truncate(298)
        self.assertEqual(x.getBackendType(), 'file')
        self.assertTrue(isinstance(x._backend, file))

        # Truncate some more
        x.truncate(255)

        self.assertEqual(x.getBackendType(), 'memory')
        self.assertTrue(isinstance(x._backend, io.StringIO))

    def testProtectedTemplate(self):
        t = util.ProtectedTemplate("$foo is the new $bar", foo='a', bar='b')
        self.assertEqual(t, "a is the new b")
        self.assertEqual(str(t), "a is the new b")
        self.assertEqual(t.__safe_str__(), "a is the new b")

        t = util.ProtectedTemplate("$foo is the new $bar", foo='a', 
            bar=util.ProtectedString('b'))
        self.assertEqual(t, "a is the new b")
        self.assertEqual(str(t), "a is the new b")
        self.assertEqual(t.__safe_str__(), "a is the new <BAR>")

    def testXMLRPCbinary(self):
        # CNY-1932
        # Make sure we properly encode and decode XMLRPC Binary objects on the
        # fly
        marshaller = util.XMLRPCMarshaller("utf-8", allow_none=False)
        srcdata = "abc\x80"
        data = marshaller.dumps((srcdata, ))
        self.assertEqual(data,
            "<params>\n<param>\n<value><base64>\nYWJjgA==\n</base64></value>\n"
            "</param>\n</params>\n")

        data = util.xmlrpcDump((srcdata, ), methodresponse = True)
        self.assertEqual(data,
            "<?xml version='1.0'?>\n"
            "<methodResponse>\n"
            "<params>\n<param>\n<value><base64>\nYWJjgA==\n</base64></value>\n"
            "</param>\n</params>\n"
            "</methodResponse>\n")

        srcdata = ["abc\x80", util.ProtectedString("abc\x80")]
        data = util.xmlrpcDump((srcdata, ), methodresponse = True)

        sio = io.StringIO(data)
        params, methodname = util.xmlrpcLoad(sio)
        self.assertEqual(params, (srcdata, ))
        self.assertEqual(methodname, None)

        # Produce a very large string representation
        srcdata = [ "abc\x80" ] * 4096
        sio = util.BoundedStringIO()
        util.xmlrpcDump((srcdata, ), methodname = "somemethod", stream = sio)
        sio.seek(0)
        params, methodname = util.xmlrpcLoad(sio)
        self.assertEqual(params, (srcdata, ))
        self.assertEqual(methodname, 'somemethod')

        sio.seek(0)
        params, methodname = util.xmlrpcLoad(sio.read())
        self.assertEqual(params, (srcdata, ))
        self.assertEqual(methodname, 'somemethod')

        # Test a Fault too
        x = util.xmlrpclib.Fault(1001, "blah")
        repr1 = util.xmlrpclib.dumps(x)
        repr2 = util.xmlrpcDump(x)
        self.assertEqual(repr1, repr2)

        try:
            util.xmlrpcLoad(repr1)
        except util.xmlrpclib.Fault as x2:
            self.assertEqual(x.faultCode, x2.faultCode)
            self.assertEqual(x.faultString, x2.faultString)
        except:
            self.fail()
        else:
            self.fail()

    def testCompressDecompressStream(self):
        # Test that compressing and uncompressing streams produces the same
        # data
        fd, tempf = tempfile.mkstemp()
        os.unlink(tempf)
        sio = os.fdopen(fd, "w+")

        # Some data we will compress
        for fn in ['distcache-1.4.5-2.src.rpm', 'distcc-2.9.tar.bz2',
                      'initscripts-10-11.src.rpm', 'jcd.iso']:
            util.copyStream(file(os.path.join(resources.get_archive(), fn)), sio)
        sio.seek(0)

        cstr = util.compressStream(sio)
        cstr.seek(0)
        dstr = util.decompressStream(cstr)
        dstr.seek(0)
        sio.seek(0)
        self.assertEqual(sio.read(), dstr.read())

    def testDecompressStream(self):
        data = os.urandom(16 * 1024)
        compressed = zlib.compress(data)
        fp = io.StringIO(compressed)
        dfo = util.decompressStream(fp)
        check = dfo.read()
        self.assertEqual(check, data)
        fp = io.StringIO(compressed)
        dfo = util.decompressStream(fp)
        chunk = dfo.read(333)
        self.assertEqual(chunk,  data[:333])

        # test readline
        data = 'hello world\nhello world line 2\n'
        compressed = zlib.compress(data)
        fp = io.StringIO(compressed)
        dfo = util.decompressStream(fp)
        line = dfo.readline()
        self.assertEqual(line, 'hello world\n')
        line = dfo.readline()
        self.assertEqual(line, 'hello world line 2\n')

        fp = io.StringIO(compressed)
        dfo = util.decompressStream(fp)
        line = dfo.readline(5)
        self.assertEqual(line, 'hello')
        line = dfo.readline(5)
        self.assertEqual(line, ' worl')
        line = dfo.readline()
        self.assertEqual(line, 'd\n')


    def testMassCloseFileDescriptors(self):
        # Open /dev/null
        s = open("/dev/null")
        # Start with fd 500
        start = 500
        # Open file descriptors, spaced apart
        def openFDs():
            os.dup2(s.fileno(), start + 0)
            os.dup2(s.fileno(), start + 2)
            os.dup2(s.fileno(), start + 5)
            os.dup2(s.fileno(), start + 9)
            os.dup2(s.fileno(), start + 13)
            os.dup2(s.fileno(), start + 17)
            os.dup2(s.fileno(), start + 27)

        openFDs()
        util.massCloseFileDescriptors(start, 4)
        # 17 should be closed
        self.assertRaises(OSError, os.read, start + 17, 1)
        # 27 should still be open
        os.read(start + 27, 1)
        os.close(start + 27)

        openFDs()
        util.massCloseFileDescriptors(start, 10)
        # 27 should be closed now
        self.assertRaises(OSError, os.read, start + 27, 1)

        # Test for low-level misc function
        openFDs()
        file_utils.massCloseFileDescriptors(start, 0, start + 20)
        # 27 should still be open
        os.read(start + 27, 1)
        file_utils.massCloseFileDescriptors(start, 0, start + 30)
        # 27 should be closed now
        self.assertRaises(OSError, os.read, start + 27, 1)

    def testNullifyFileDescriptor(self):
        # CNY-2143

        # Find an unused fd
        f1 = open("/dev/null")
        f2 = open("/dev/null")
        ofd1 = f1.fileno()
        ofd2 = f2.fileno()
        f1.close()
        f2.close()

        # /dev/null exists, it will (most likely) open directly on top of
        # ofd1
        util.nullifyFileDescriptor(ofd1)
        self.assertEqual(os.read(ofd1, 10), '')
        os.close(ofd1)

        # ofd1 is empty and smaller than ofd2, the function should dup() it
        util.nullifyFileDescriptor(ofd2)
        self.assertEqual(os.read(ofd2, 10), '')
        os.close(ofd2)

        oldMkstemp = tempfile.mkstemp
        ofds = []
        def mockMkstemp(*args, **kwargs):
            fd, fn = oldMkstemp(*args, **kwargs)
            ofds.append(fd)
            return fd, fn

        oldOpen = os.open
        def mockOpen(filename, *args, **kwargs):
            if filename == '/dev/null':
                raise OSError("Some random error")
            return oldOpen(filename, *args, **kwargs)

        self.mock(os, "open", mockOpen)
        self.mock(tempfile, "mkstemp", mockMkstemp)

        util.nullifyFileDescriptor(ofd2)
        self.unmock()

        self.assertEqual(os.read(ofd2, 10), '')
        self.assertEqual(len(ofds), 1)

        # Anything open by mkstemp should be closed
        try:
            os.close(ofds[0])
        except OSError as e:
            self.assertEqual(e.errno, 9)
        else:
            self.fail("File descriptor open by mkstemp should have been closed")
        os.close(ofd2)

    def testMkdirChain(self):
        try:
            d = tempfile.mkdtemp()
            util.mkdirChain(d + '/some/nested/path')
            assert(stat.S_ISDIR(os.stat(d + '/some/nested/path').st_mode))

            # this shouldn't fail
            util.mkdirChain(d + '/some/nested/path')

            open(d + '/file', "w").write("something")
            self.assertRaises(OSError, util.mkdirChain, d + '/file/sub')

            # now try making more than one directory
            util.mkdirChain(d + '/some/nested/path2',
                            d + '/some/nested/path3')
            assert(stat.S_ISDIR(os.stat(d + '/some/nested/path2').st_mode))
            assert(stat.S_ISDIR(os.stat(d + '/some/nested/path3').st_mode))

        finally:
            util.rmtree(d)

    def testCountOpenFileDescriptors(self):
        # CNY-2536
        startCount = util.countOpenFileDescriptors()
        fdarr = [ open('/dev/null') for x in range(200) ]
        endCount = util.countOpenFileDescriptors()
        self.assertEqual(startCount + len(fdarr), endCount)

        fdarr = None
        endCount = util.countOpenFileDescriptors()
        self.assertEqual(startCount, endCount)

    def testConvertPackageNameToClassName(self):
        data = {'foo': 'Foo',
                'foobar': 'Foobar',
                'foo-bar': 'FooBar'}

        for input, expectedOutput in data.items():
            self.assertEqual(util.convertPackageNameToClassName(input),
                              expectedOutput)


    def testBadXmlrpcData(self):
        string = "<Blah"
        e = self.assertRaises(util.xmlrpclib.ResponseError,
            util.xmlrpcLoad, string)

        # Simulate sgmlop missing
        self.mock(util.xmlrpclib, "SgmlopParser", None)
        e = self.assertRaises(util.xmlrpclib.ResponseError,
            util.xmlrpcLoad, string)

    def testServerProxyHidingPassword(self):
        sp = util.ServerProxy("http://user:sikrit_pass@host:1234/XMLRPD", None)
        self.assertEqual(repr(sp), '<ServerProxy for http://user:<PASSWD>@host:1234/XMLRPD>')
        self.assertEqual(str(sp), '<ServerProxy for http://user:<PASSWD>@host:1234/XMLRPD>')

    def testPreferXZoverUNLZMA(self):
        # CNY-3231
        # Make sure if both xz and unlzma are present, that we prefer xz
        workDir = tempfile.mkdtemp(prefix="utiltest-")
        oldPath = os.getenv('PATH')
        xzPath = os.path.join(workDir, "xz")
        unlzmaPath = os.path.join(workDir, "unlzma")
        dumbFilePath = os.path.join(workDir, "some-file")
        scriptContents = "#!/bin/bash\n\n/bin/cat"
        file(xzPath, "w").write(scriptContents)
        file(unlzmaPath, "w").write(scriptContents)
        data = "Feed dog to cat"
        file(dumbFilePath, "w").write(data)
        os.chmod(xzPath, 0o755)
        os.chmod(unlzmaPath, 0o755)

        try:
            os.environ['PATH'] = workDir
            decompressor = util.LZMAFile(file(dumbFilePath))
            self.assertEqual(decompressor.read(), data)
            decompressor.close()
            # Make sure we prefer xz over unlzma
            self.assertEqual(decompressor.executable, xzPath)
            # But if xz is not available, we can use unlzma
            os.unlink(xzPath)
            decompressor = util.LZMAFile(file(dumbFilePath))
            self.assertEqual(decompressor.read(), data)
            decompressor.close()
            self.assertEqual(decompressor.executable, unlzmaPath)
        finally:
            os.environ['PATH'] = oldPath
            util.rmtree(workDir)

    def testFnmatchTranslate(self):
        tests = [
            ('foo.recipe', r'foo\.recipe'),
            ('foo*', r'foo.*'),
            ('foo?bar', r'foo.bar'),
        ]
        for teststr, exp in tests:
            self.assertEqual(util.fnmatchTranslate(teststr), exp)

    def testLockedFile(self):
        # pipe1 is used by the parent to unblock the child
        # pipe2 is used by the child to report back
        tempdir = tempfile.mkdtemp(prefix = "lockedfile-")
        util.mkdirChain(tempdir)
        pipe1 = os.pipe()
        pipe2 = os.pipe()
        pid = os.fork()
        fileName = os.path.join(tempdir, "file")
        lf = util.LockedFile(fileName)
        if pid == 0:
            try:
                # Child reads from pipe1 and writes to pipe2
                os.close(pipe1[1])
                os.close(pipe2[0])
                # Block the child until the parent send something on pipe1
                ret = os.read(pipe1[0], 1)
                self.assertEqual(ret, "g")
                os.write(pipe2[1], "START")
                fileobj = lf.open()
                os.write(pipe2[1], "UNLCK")
                # We now have the lock
                lf.close()
                os.write(pipe2[1], "CLOSD")
                ret = os.read(pipe1[0], 1)
                self.assertEqual(ret, "o")

                # Try to acquire lock
                os.write(pipe2[1], "GETLK")
                fileobj = lf.open()
                self.assertNotEqual(fileobj, None)

                ret = os.read(pipe1[0], 1)
                self.assertEqual(ret, "g")
                # We shouldn't lock
                fileobj = lf.open()
                self.assertNotEqual(fileobj, None)
                self.assertEqual(fileobj.read(), "Blah")
                os.write(pipe2[1], "NOLCK")

                ret = os.read(pipe1[0], 1)
                self.assertEqual(ret, "o")

                # We shouldn't lock
                fileobj = lf.open(shouldLock = False)
                self.assertEqual(fileobj, None)

                os.write(pipe2[1], "ByBye")
                
            finally:
                os._exit(1)
        try:
            # Parent writes to pipe1 and reads from pipe2
            os.close(pipe1[0])
            os.close(pipe2[1])
            fileobj = lf.open()
            self.assertEqual(fileobj, None)
            # We now hold the lock
            # Launch child process
            os.write(pipe1[1], "g")
            # Wait for client to write START
            ret = os.read(pipe2[0], 5)
            self.assertEqual(ret, "START")
            # Make sure the client didn't send anything else
            import select
            p = select.poll()
            p.register(pipe2[0], select.POLLIN)
            ret = p.poll(0.1)
            self.assertEqual(ret, [])
            # Unlock the child, no data is created
            lf.unlock()
            ret = os.read(pipe2[0], 5)
            self.assertEqual(ret, "UNLCK")
            # Wait for child to close locked file
            ret = os.read(pipe2[0], 5)
            self.assertEqual(ret, "CLOSD")
            # The lock file should be still present
            self.assertTrue(os.path.exists(fileName + '.lck'))

            # Lock again
            fileobj = lf.open()
            self.assertEqual(fileobj, None)
            lf.write("Blah")
            os.write(pipe1[1], "o")

            ret = os.read(pipe2[0], 5)
            self.assertEqual(ret, "GETLK")
            # Unlock the client
            lf.commit()

            # We should not lock anymore, we have the data file
            fileobj = lf.open()
            self.assertNotEqual(fileobj, None)

            # Let the child run, it should return immediately
            os.write(pipe1[1], "g")

            ret = os.read(pipe2[0], 5)
            self.assertEqual(ret, "NOLCK")
            self.assertEqual(fileobj.read(), "Blah")

            # Get rid of the data file
            os.unlink(fileName)
            fileobj = lf.open()

            fileobj = lf.open(shouldLock = False)
            self.assertEqual(fileobj, None)
            os.write(pipe1[1], 'o')

            ret = os.read(pipe2[0], 5)
            self.assertEqual(ret, "ByBye")

            lf.close()
        finally:
            os.close(pipe1[1])
            os.close(pipe2[0])
            ret = os.waitpid(pid, os.WNOHANG)
            if not os.WIFEXITED(ret[1]):
                os.kill(pid, signal.SIGTERM)
                time.sleep(.1)
                os.kill(pid, signal.SIGKILL)
                os.waitpid(self.pid, 0)
            util.rmtree(tempdir)

    def testTimestampedMap(self):
        delta = 10
        tsmap = util.TimestampedMap(delta = delta)
        now = time.time()
        key, val = 'a', 'aval'
        tsmap.set(key, val)
        now2 = time.time()
        v = tsmap.get(key)
        self.assertEqual(val, v)
        # Reach inside, make sure timestamp is set right
        v, ts = tsmap._map.get(key)
        self.assertEqual(val, v)
        self.assertTrue(ts >= now + delta)
        self.assertTrue(ts <= now2 + delta)

        # Make the entry stale
        tsmap._map[key] = (val, now - delta - 1)
        missing = object()

        v = tsmap.get(key, default = missing)
        self.assertTrue(v is missing)

        # Fetch stale object
        v = tsmap.get(key, default = missing, stale = True)
        self.assertEqual(val, v)

        # Clear object
        tsmap.clear()
        v = tsmap.get(key, default = missing, stale = True)
        self.assertTrue(v is missing)


    def testBz2File(self):
        fobj = file(os.path.join(resources.get_archive(),
                                 'distcc-2.9.tar.bz2'))
        b = util.BZ2File(fobj)
        s = 'distcc-2.9'
        out = b.read(len(s))
        self.assertEqual(out,s)

        out = b.read(100000)
        self.assertEqual(len(out),100000)

        out = b.read(10000000)
        self.assertEqual(len(out),1169750)

        out = b.read(1)
        self.assertEqual(out,None)

    def testLZMAFile(self):
        # CNY-3564 - test for short reads
        xzbin = "/usr/bin/xz"
        if not os.path.exists(xzbin):
            raise testhelp.SkipTestException(
                "Skipping test, %s not found" % xzbin)
        fobj = tempfile.TemporaryFile()
        p = subprocess.Popen([xzbin, "-zc"],
            stdin=subprocess.PIPE, stdout=fobj)
        data = "0" * 1024
        for i in range(1024):
            p.stdin.write(data)
        p.stdin.close()
        p.wait()
        fobj.flush()

        # Make sure something did get written
        self.assertTrue(fobj.tell() > 0)
        fobj.seek(0)

        b = util.LZMAFile(fobj)
        # Read a large amount of data, hopefully larger than the pipe buffer
        limit = 128000
        buf = b.read(limit)
        self.assertEqual(len(buf), limit)
        self.assertEqual(list(set(buf)), ["0"])

    def testSplitExact(self):
        pad = object()
        Tests = [
            # Degenerate case, splitting None returns all None
            ((None, ' ', 1, None), [None, None]),
            (('a b', ' ', 1, None), ['a', 'b']),
            # Classic split resulting in a string too short
            (('a', ' ', 1, None), ['a', None]),
            ((' b', ' ', 1, None), ['', 'b']),
            (('  c', ' ', 2, None), ['', '', 'c']),
            # Last split has extra seps in it
            ((' b c d', ' ', 1, None), ['', 'b c d']),
            # Different padding
            (('a', ' ', 1, pad), ['a', pad]),
        ]
        for (string, sep, split, pad), tup in Tests:
            self.assertEqual(util.splitExact(string, sep, split, pad), tup)

class UrlTests(testhelp.TestCase):
    Tests = [
        (("http", None, None, "localhost", None, "/path", "q", "f"),
          "http://localhost/path?q#f"),
        (("http", 'u', 'p', "localhost", None, "/path", None, None),
          "http://u:p@localhost/path"),
        (("http", 'u', 'p', "localhost", 103, "/path", None, None),
          "http://u:p@localhost:103/path"),
        (("http", 'u', 'p a s s', "localhost", 103, "/path", None, None),
          "http://u:p%20a%20s%20s@localhost:103/path"),
        (("http", 'u', 'p', "dead::beef", None, "/path", None, None),
          "http://u:p@[dead::beef]/path"),
        (("http", 'u', 'p', "dead::beef", 8080, "/path", None, None),
          "http://u:p@[dead::beef]:8080/path"),
        # According to RFC2617, the password can be any character,
        # including newline. However, urllib's regex will stop at the new
        # line.
        #(("http", 'u', 'p\nq', "localhost", 103, "/path", None, None),
        #  "http://u:p%0Aq@localhost:103/path"),
        (("http", 'u\tv', 'p\tq', "localhost", 103, "/path", None, None),
          "http://u%09v:p%09q@localhost:103/path"),
    ]

    def testUrlSplitUnsplit(self):
        for tup, url in self.Tests:
            nurl = util.urlUnsplit(tup)
            self.assertEqual(nurl, url)
            ntup = util.urlSplit(url)
            self.assertEqual(ntup, tup)

        # One-way tests
        tests = [
            (("http", None, None, "localhost", "10", "/path", "", ""),
              "http://localhost:10/path"),
            ((None, None, None, None, None, "/path", "q", "f"),
                "/path?q#f"),
        ]
        for tup, url in tests:
            nurl = util.urlUnsplit(tup)
            self.assertEqual(nurl, url)


class SystemIdFactoryTests(testhelp.TestCase):
    def setUp(self):
        testhelp.TestCase.setUp(self)
        self.workDir = tempfile.mkdtemp()

    def tearDown(self):
        util.rmtree(self.workDir)
        testhelp.TestCase.tearDown(self)

    def testNoScript(self):
        factory = util.SystemIdFactory(None)
        id1 = factory.getId()
        id2 = factory.getId()
        self.assertEqual(id1, id2)

    def _writeScript(self, script, systemId, exitCode=0):
        open(script, 'w').write("""\
#!/bin/bash
echo -n "%(systemId)s"
exit %(exitCode)s
""" % {'systemId': systemId, 'exitCode': exitCode})
        os.chmod(script, 0o755)

    def testScript(self):
        script = os.path.join(self.workDir, 'script.sh')
        factory = util.SystemIdFactory(script)

        for systemId in ['abc', '123', 'asdfklajsdfasdgfalgklh']:
            self._writeScript(script, systemId)
            self.assertEqual(factory.getId(), base64.b64encode(systemId))
            factory.systemId = None

    def testScriptFail(self):
        script = os.path.join(self.workDir, 'script.sh')
        factory = util.SystemIdFactory(script)

        self._writeScript(script, '12345', 1)
        systemId = factory.getId()
        self.assertEqual(systemId, None)
