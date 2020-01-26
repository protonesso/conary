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


from conary_test import rephelp

from conary.build import macros

class TestUse(rephelp.RepositoryHelper):

    def testMacros(self):
        m1 = macros.Macros()
        m1.a = 'foo'
        assert(m1.a == 'foo')
        m2 = m1.copy()
        m2.a = 'bar'
        assert(m1.a == 'foo')
        assert(m2.a == 'bar')
        m3 = m2.copy(False)
        m3.a = 'baz'
        assert(m2.a == 'bar')
        assert(m3.a == 'baz')
        m4 = m3
        m4.a = 'blah'
        assert(m3.a == 'blah')
        m1.b = '%(a)s/asdf'
        assert(m1.b == 'foo/asdf')
        m1.trackChanges()
        m1.c = 'foo'
        assert(m1.getTrackedChanges() == ['c'])
        m1.trackChanges(False)
        m1.d = 'bar'
        assert(m1.getTrackedChanges() == ['c'])
        m1.e = '1'
        m1._override('e', '2')
        m1.e = '3'
        assert(m1.e == '2')
        m1.r = 'foo++'
        assert(m1.r == 'foo++')
        assert(m1['r'] == 'foo++')
        assert(str(m1['r.literalRegex']) == 'foo\+\+')
        assert(str("%(r.literalRegex)s" % m1) == 'foo\+\+')

    def testIterItems(self):
        m1 = macros.Macros()
        m1.a = 'a'
        m1.b = 'b'
        m2 = m1.copy()
        m2.c = 'c'
        iterkeys = [ x for x in m2.keys() ]
        iterkeys.sort()
        assert(iterkeys == ['a', 'b', 'c'])
        keys = list(m2.keys())
        keys.sort()
        assert(keys == ['a', 'b', 'c'])
        iteritems = [ x for x in m2.items() ]
        iteritems.sort()
        assert(iteritems == [('a', 'a'), ('b', 'b'), ('c', 'c')])

    def testUpdate(self):
        m1 = macros.Macros()
        m1.a = 'a'
        m1.b = 'b'
        m2 = m1.copy()
        m2.c = 'c'
        m3 = macros.Macros()
        m3.d = 'd'
        m3.e = 'e'
        m4 = m3.copy()
        m4.f = 'f'
        m2.update(m4)
        keys = list(m2.keys())
        keys.sort()
        assert(keys == ['a', 'b', 'c', 'd', 'e', 'f'])

    def testGet(self):
        m1 = macros.Macros()
        m1.march = 'i386'
        m1.target = '%(march)s-unknown-linux'
        assert(m1.target == 'i386-unknown-linux')
        assert(m1._get('target') == '%(march)s-unknown-linux')

    def testCallback(self):
        a = [1]
        m1 = macros.Macros()
        def myfun(name):
            a.append(2)
        m1.setCallback('foo', myfun)
        m1.foo = 'hello'
        assert('%(foo)s' % m1 == 'hello')
        assert(a == [1,2])
