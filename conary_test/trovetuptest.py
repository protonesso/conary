#
# Copyright (c) rPath, Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
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
        self.assertEquals(str(t), asStr)
        self.assertEquals(repr(t), r)
        self.assertEquals(t.name, name)
        self.assertEquals(t.version, version)
        self.assertEquals(str(t.flavor), str(flavor))

    def testParse(self):
        self.compare('foo', 'foo', 'foo', None, None)
        self.compare('foo=1.2', 'foo=1.2', 'foo', '1.2', None)
        self.compare('foo=1.2[asdf]', 'foo=1.2[asdf]',
            'foo', '1.2', deps.parseFlavor('asdf'))
        self.compare('foo[asdf]', 'foo[asdf]',
            'foo', None, deps.parseFlavor('asdf'))

    def testEmptyName(self):
        t = trovetup.TroveSpec('', version='1.2')
        self.assertEquals(str(t), '=1.2')
        self.assertEquals(t.name, '')
        self.assertEquals(t.version, '1.2')
        self.assertEquals(t.flavor, None)

        self.assertRaises(TroveSpecError,
            trovetup.TroveSpec, '', allowEmptyName=False)
    
    def testFromTuple(self):
        def check(t):
            self.assertEquals(t.name, 'a')
            self.assertEquals(t.version, 'b')
            self.assertEquals(str(t.flavor), 'c')

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
        self.assertEquals(repr(p(n, v, f)), expect)
        self.assertEquals(repr(p((n, v, f))), expect)
        self.assertEquals(repr(p(ex_str)), expect)
        self.assertEquals(repr(p(ex_str.decode('ascii'))), expect)
        self.assertEquals(repr(p(n, vo, fo)), expect)
        self.assertEquals(repr(p((n, vo, fo))), expect)

        self.assertEquals(repr(p('%s=%s' % (n, v))),
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
            self.assertRaises(ParseError, p, u'spam\xFF=foo[bar]')
        finally:
            p._thawVerFunc = tv

    def testStringify(self):
        tt = trovetup.TroveTuple(self.sample)
        self.assertEquals(str(tt), 'tmpwatch=/conary.rpath.com@rpl:devel//2/'
                '2.9.10-2-0.1[is: x86_64]')