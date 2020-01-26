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

from conary import trovetup
from conary import versions
from conary.deps import deps
from conary.errors import ParseError, TroveSpecError


class TroveSpecTest(testhelp.TestCase):

    def compare(self, spec, asStr, name, version, flavor, **kwargs):
        t = trovetup.TroveSpec(spec, **kwargs)
        r = "TroveSpec('%s')" % asStr
        self.assertEqual(str(t), asStr)
        self.assertEqual(repr(t), r)
        self.assertEqual(t.name, name)
        self.assertEqual(t.version, version)
        self.assertEqual(str(t.flavor), str(flavor))

    def testParse(self):
        self.compare('foo', 'foo', 'foo', None, None)
        self.compare('foo=1.2', 'foo=1.2', 'foo', '1.2', None)
        self.compare('foo=1.2[asdf]', 'foo=1.2[asdf]',
            'foo', '1.2', deps.parseFlavor('asdf'))
        self.compare('foo[asdf]', 'foo[asdf]',
            'foo', None, deps.parseFlavor('asdf'))

    def testEmptyName(self):
        t = trovetup.TroveSpec('', version='1.2')
        self.assertEqual(str(t), '=1.2')
        self.assertEqual(t.name, '')
        self.assertEqual(t.version, '1.2')
        self.assertEqual(t.flavor, None)

        self.assertRaises(TroveSpecError,
            trovetup.TroveSpec, '', allowEmptyName=False)
    
    def testFromTuple(self):
        def check(t):
            self.assertEqual(t.name, 'a')
            self.assertEqual(t.version, 'b')
            self.assertEqual(str(t.flavor), 'c')

        t = trovetup.TroveSpec(('a', 'b', 'c'))
        check(t)

        c = deps.parseFlavor('c').freeze()
        t = trovetup.TroveSpec('a', 'b', c, withFrozenFlavor=True)
        check(t)

class TroveTupleTest(testhelp.TestCase):

    sample = ('tmpwatch',
            '/conary.rpath.com@rpl:devel//2/1210225682.938:2.9.10-2-0.1',
            'is: x86_64')

    def testNewTuple(self):
        n, v, f = self.sample
        vo = versions.ThawVersion(v)
        fo = deps.parseFlavor(f)
        ex_str = '%s=%s[%s]' % (n, v, f)
        expect = "TroveTuple(%r)" % (ex_str,)

        p = trovetup.TroveTuple
        self.assertEqual(repr(p(n, v, f)), expect)
        self.assertEqual(repr(p((n, v, f))), expect)
        self.assertEqual(repr(p(ex_str)), expect)
        self.assertEqual(repr(p(ex_str.decode('ascii'))), expect)
        self.assertEqual(repr(p(n, vo, fo)), expect)
        self.assertEqual(repr(p((n, vo, fo))), expect)

        self.assertEqual(repr(p('%s=%s' % (n, v))),
                "TroveTuple('%s=%s[]')" % (n, v))

    def testParser(self):
        p = trovetup.TroveTuple
        tv = p.__dict__['_thawVerFunc']
        try:
            # Fail if it ever gets to calling ThawVersion
            p._thawVerFunc = staticmethod(
                    lambda *a: self.fail("Should have failed to parse"))

            self.assertRaises(ParseError, p, 'spam')
            self.assertRaises(ParseError, p, 'spam=')
            self.assertRaises(ParseError, p, 'spam=foo=bar')
            self.assertRaises(ParseError, p, 'spam=foo[bar')
            self.assertRaises(ParseError, p, 'spam=foo]')
            self.assertRaises(ParseError, p, 'spam=foo[bar]x')
            self.assertRaises(ParseError, p, 'spam\xFF=foo[bar]')
        finally:
            p._thawVerFunc = tv

    def testStringify(self):
        tt = trovetup.TroveTuple(self.sample)
        self.assertEqual(str(tt), 'tmpwatch=/conary.rpath.com@rpl:devel//2/'
                '2.9.10-2-0.1[is: x86_64]')


class JobTupleTest(testhelp.TestCase):

    old = (versions.ThawVersion('/conary.rpath.com@rpl:devel//2/1000000000.000:2.9.10-2-0.1'),
            deps.parseFlavor('is: x86_64'))
    new = (versions.ThawVersion('/conary.rpath.com@rpl:devel//2/1200000000.000:3.0.00-1-0.1'),
            deps.parseFlavor('is: x86_64'))

    def testNewTuple(self):
        n = 'tmpwatch'
        a = self.old
        b = self.new
        atup = (n, a[0], a[1])
        btup = trovetup.TroveTuple(n, b[0], b[1])
        self.assertEqual((n, a, b, False), trovetup.JobTuple((n, a, b, False)))
        self.assertEqual((n, a, b, False), trovetup.JobTuple(n, a, b, False))
        self.assertEqual((n, a, b, False), trovetup.JobTuple(n, a, b))
        self.assertEqual((n, a, b, False), trovetup.JobTuple(n, atup, btup))
        self.assertEqual((n, (None, None), b, True), trovetup.JobTuple(n, new=b))
        self.assertEqual((n, (None, None), b, True), trovetup.JobTuple(n, new=btup))
        self.assertEqual((n, a, (None, None), False), trovetup.JobTuple(n, old=a))
        self.assertEqual((n, a, (None, None), False), trovetup.JobTuple(n, old=atup))
        self.assertEqual((n, (None, None), b, True), btup.asJob())

    def testStringify(self):
        x = trovetup.JobTuple('tmpwatch', new=self.new)
        self.assertEqual(str(x), "tmpwatch=/conary.rpath.com@rpl:devel//2/3.0.00-1-0.1[is: x86_64]")
        self.assertEqual(repr(x), "JobTuple('tmpwatch=/conary.rpath.com@rpl:devel//2/1200000000.000:3.0.00-1-0.1[is: x86_64]')")
        x = x._replace(absolute=False)
        self.assertEqual(str(x), "tmpwatch=/conary.rpath.com@rpl:devel//2/3.0.00-1-0.1[is: x86_64]")
        self.assertEqual(repr(x), "JobTuple('tmpwatch=/conary.rpath.com@rpl:devel//2/1200000000.000:3.0.00-1-0.1[is: x86_64]', absolute=False)")
        x = trovetup.JobTuple('tmpwatch', self.old, self.new)
        self.assertEqual(str(x), "tmpwatch=/conary.rpath.com@rpl:devel//2/2.9.10-2-0.1[is: x86_64]"
                "--/conary.rpath.com@rpl:devel//2/3.0.00-1-0.1[is: x86_64]")
        self.assertEqual(repr(x), "JobTuple('tmpwatch=/conary.rpath.com@rpl:devel//2/1000000000.000:2.9.10-2-0.1[is: x86_64]"
                "--/conary.rpath.com@rpl:devel//2/1200000000.000:3.0.00-1-0.1[is: x86_64]')")
        x = x._replace(absolute=True)
        self.assertEqual(str(x), "tmpwatch=/conary.rpath.com@rpl:devel//2/2.9.10-2-0.1[is: x86_64]"
                "--/conary.rpath.com@rpl:devel//2/3.0.00-1-0.1[is: x86_64]")
        self.assertEqual(repr(x), "JobTuple('tmpwatch=/conary.rpath.com@rpl:devel//2/1000000000.000:2.9.10-2-0.1[is: x86_64]"
                "--/conary.rpath.com@rpl:devel//2/1200000000.000:3.0.00-1-0.1[is: x86_64]', absolute=True)")
        x = trovetup.JobTuple('tmpwatch', self.old)
        self.assertEqual(str(x), "tmpwatch=/conary.rpath.com@rpl:devel//2/2.9.10-2-0.1[is: x86_64]--")
        self.assertEqual(repr(x), "JobTuple('tmpwatch=/conary.rpath.com@rpl:devel//2/1000000000.000:2.9.10-2-0.1[is: x86_64]--')")

    def testParser(self):
        p = trovetup.JobTuple
        self.assertRaises(ParseError, p, 'spam\xFF=foo[bar]')
        self.assertEqual(p('tmpwatch=/conary.rpath.com@rpl:devel//2/1200000000.000:3.0.00-1-0.1[is: x86_64]'),
                ('tmpwatch', (None, None), self.new, True))
        self.assertEqual(p('tmpwatch=/conary.rpath.com@rpl:devel//2/1000000000.000:2.9.10-2-0.1[is: x86_64]'
                '--/conary.rpath.com@rpl:devel//2/1200000000.000:3.0.00-1-0.1[is: x86_64]'),
                ('tmpwatch', self.old, self.new, False))
        self.assertEqual(p('tmpwatch=/conary.rpath.com@rpl:devel//2/1000000000.000:2.9.10-2-0.1[is: x86_64]--'),
                ('tmpwatch', self.old, (None, None), False))
