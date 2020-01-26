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


import os
import sys

import pydoc

from conary_test import rephelp

from conary.build import explain

class ExplainTest(rephelp.RepositoryHelper):
    def testTextPager(self):
        class MockStdout(object):
            "Dummy doc string"
            isatty = lambda x: False
            write = lambda x, y: None
        def goodPager(*args, **kwargs):
            self.called = True
        def badPager(*args, **kwargs):
            raise RuntimeError("Pipe pager should not have been called")
        self.called = False
        self.mock(sys, 'stdout', MockStdout())
        self.mock(pydoc, 'pager', goodPager)
        self.mock(pydoc, 'pipepager', badPager)
        explain._formatDoc('Stdout', MockStdout)

    def testPipePager(self):
        class MockStdout(object):
            "Dummy doc string"
            isatty = lambda x: True
            write = lambda x, y: None
        def goodPager(*args, **kwargs):
            self.called = True
        def badPager(*args, **kwargs):
            raise RuntimeError("Text pager should not have been called")
        self.called = False
        self.mock(sys, 'stdout', MockStdout())
        self.mock(pydoc, 'pager', badPager)
        self.mock(pydoc, 'pipepager', goodPager)
        explain._formatDoc('Stdout', MockStdout)

    def testFormatDoc(self):
        'test various formatting: reindent, headers, tags'
        class MockStdout(object):
            """
            NAME
            ====
            B{Dummy} {doc} I{string} B{with B{nested} text}
            """
            isatty = lambda x: False
            write = lambda x, y: None
        def mockPager(text):
            refText = (
              'Conary API Documentation: \x1b[1mStdout.MockStdout\x1b[21m\n\n'
              '\x1b[1mNAME\x1b[21m\n'
              '    \x1b[1mDummy\x1b[21m {doc} \x1b[7mstring\x1b[27m \x1b[1mwith nested text\x1b[21m\n    ')
            self.assertEqual(text, refText)
        self.mock(sys, 'stdout', MockStdout())
        self.mock(pydoc, 'pager', mockPager)
        explain._formatDoc('Stdout', MockStdout)

    def testFormatString(self):
        oldPager = os.environ.pop('PAGER', '')
        # test that italic formatting uses reverse video
        self.assertEqual(explain._formatString('I{italic}'),
            '\x1b[7mitalic\x1b[27m')
        # test that constant width formatting uses underscores
        self.assertEqual(explain._formatString('C{cw}'),
            '\x1b[4mcw\x1b[24m')
        # test that bold formatting uses bold
        self.assertEqual(explain._formatString('B{foo}'),
            '\x1b[1mfoo\x1b[21m')
        # test that formatting stacks
        self.assertEqual(explain._formatDocString('C{B{foo}}'),
            '\x1b[4m\x1b[1mfoo\x1b[21m\x1b[24m')
        # inner bold is erased by outer bold because CSR codes do not nest
        self.assertEqual(explain._formatDocString('B{C{B{foo}}}'),
            '\x1b[1m\x1b[4mfoo\x1b[24m\x1b[21m')
        # test that bold text is not re-bolded
        self.assertEqual(explain._formatString('B{\x1b[1mfoo\x1b[21m}'),
                '\x1b[1mfoo\x1b[21m')
        self.assertEqual(explain._formatDocString('B{B{foo}}'),
                '\x1b[1mfoo\x1b[21m')
        os.environ['PAGER'] = oldPager

    def testNoDoc(self):
        class NoDocs(object):
            pass
        oldPager = os.environ.pop('PAGER', '')
        rc, output = self.captureOutput(explain._formatDoc, 'NoDocs', NoDocs)
        expected = ('Conary API Documentation: \x1b[1mNoDocs.NoDocs\x1b[21m\n'
                    'No documentation available.')
        self.assertEqual(output, expected)
        os.environ['PAGER'] = 'more'
        rc, output = self.captureOutput(explain._formatDoc, 'NoDocs', NoDocs)
        expected = ('Conary API Documentation: NoDocs.NoDocs\n'
                    'No documentation available.')
        os.environ['PAGER'] = oldPager
        self.assertEqual(output, expected)

    def testExplainRecipe(self):
        def mockFormatDoc(className, obj):
            self.assertFalse(not 'r.Move' in obj.__doc__)
            self.assertEqual(obj.__module__, 'conary.build.build')
        self.mock(explain, '_formatDoc', mockFormatDoc)
        explain.docObject(self.cfg, 'Move')

    def testCvcExplainSource(self):
        def mockFormatDoc(className, obj):
            self.assertFalse(not 'r.addSource' in obj.__doc__)
            self.assertEqual(obj.__module__, 'conary.build.source')
        self.mock(explain, '_formatDoc', mockFormatDoc)
        res = self.captureOutput(explain.docObject, self.cfg, 'addSource')
        self.assertFalse('Ambiguous recipe method "addSource"' in res[1])

    def testCvcExplainPolicy(self):
        def mockFormatDoc(className, obj):
            self.assertFalse(not 'r.Config' in obj.__doc__)
            self.assertEqual(obj.__module__, 'conary.build.packagepolicy')
        self.mock(explain, '_formatDoc', mockFormatDoc)
        explain.docObject(self.cfg, 'Config')

    def testCvcExplainGroup(self):
        def mockFormatDoc(className, obj):
            self.assertFalse(not 'r.addCopy' in obj.__doc__)
            self.assertEqual(obj.__module__, 'conary.build.grouprecipe')
        self.mock(explain, '_formatDoc', mockFormatDoc)
        explain.docObject(self.cfg, 'addCopy')

    def testCvcExplainGroupPolicy(self):
        def mockFormatDoc(className, obj):
            self.assertFalse(not 'r.VersionConflicts' in obj.__doc__)
            self.assertEqual(obj.__module__, 'group_versionconflicts')
        self.mock(explain, '_formatDoc', mockFormatDoc)
        explain.docObject(self.cfg, 'VersionConflicts')

    def testCvcExplainInfo(self):
        def mockFormatDoc(className, obj):
            self.assertFalse(not 'r.User' in obj.__doc__)
            self.assertEqual(obj.__module__, 'conary.build.build')
        self.mock(explain, '_formatDoc', mockFormatDoc)
        explain.docObject(self.cfg, 'UserInfoRecipe.User')

    def testCvcExplainFailure(self):
        #rc, txt = self.captureOutput(explain.docObject, self.cfg,
                                     #'AClassNotImplemented')
        #self.assertEqual(txt,
                #'Unknown recipe method "AClassNotImplemented"\n')

        rc, txt = self.captureOutput(explain.docObject, self.cfg,
                                     'Requires')
        self.assertEqual(txt,
                     'Ambiguous recipe method "Requires" is defined by the '
                     'following classes:\n'
                     '    GroupRecipe, PackageRecipe\n'
                     'Specify one of: GroupRecipe.Requires, '
                     'PackageRecipe.Requires\n')

        rc, txt = self.captureOutput(explain.docObject, self.cfg,
                                     'User.foo.bar')
        self.assertEqual(txt,
                             'Too may "." specified in "User.foo.bar"\n')

    def testExplainAll(self):
        def mockPageDoc(className, obj):
            self.assertEqual('All Classes', className)
            self.assertFalse('Available Classes' not in obj)
        self.mock(explain, '_pageDoc', mockPageDoc)
        explain.docAll(self.cfg)

    def testExplainPkgRecipe(self):
        def mockPageDoc(className, obj):
            self.assertEqual('PackageRecipe', className)
            self.assertFalse('PackageRecipe' not in obj)
        self.mock(explain, '_pageDoc', mockPageDoc)
        explain.docObject(self.cfg, 'PackageRecipe')

    def testExplainGroupRecipe(self):
        def mockPageDoc(className, obj):
            self.assertEqual('GroupRecipe', className)
            self.assertFalse('GroupRecipe' not in obj)
        self.mock(explain, '_pageDoc', mockPageDoc)
        explain.docObject(self.cfg, 'GroupRecipe')

    def testExplainUserInfoRecipe(self):
        def mockPageDoc(className, obj):
            self.assertEqual('UserInfoRecipe', className)
            self.assertFalse('Build Actions' not in obj)
        self.mock(explain, '_pageDoc', mockPageDoc)
        explain.docObject(self.cfg, 'UserInfoRecipe')

    def testExplainGroupInfoRecipe(self):
        def mockPageDoc(className, obj):
            self.assertEqual('GroupInfoRecipe', className)
            self.assertFalse('Build Actions' not in obj)
        self.mock(explain, '_pageDoc', mockPageDoc)
        explain.docObject(self.cfg, 'GroupInfoRecipe')
