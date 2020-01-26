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

import io
import sys

from conary.lib import formattrace

class ReprTest(testhelp.TestCase):
    def testFormatLocals(self):
        # The pretty printer works on the representation of the object, so
        # we need to include the quotes in the calculation
        stringobj1 = '0123456789' * 159 + 'abcdefgh'
        stringobj2 = stringobj1 + 'i'
        unicodeobj1 = '0123456789' * 159 + 'abcdefgh'
        unicodeobj2 = unicodeobj1 + 'i'
        listobj1 = [ 1 ] * 20
        listobj2 = listobj1 + [ 'a' ]
        sio = io.StringIO()
        frame = sys._getframe()
        formattrace.formatLocals(frame, sio)

        sio.seek(0)

        for line in sio:
            varName, varVal = [ x.strip() for x in line.split(':', 2) ]
            if varName == 'stringobj1':
                self.assertEqual(varVal, repr(stringobj1))
            elif varName == 'unocodeobj1':
                self.assertEqual(varVal, repr(unicodeobj1))
            elif varName == 'listobj1':
                self.assertEqual(varVal, repr(listobj1))
            elif varName == 'stringobj2':
                self.assertNotEqual(varVal, repr(stringobj2))
            elif varName == 'unocodeobj2':
                self.assertNotEqual(varVal, repr(unicodeobj2))
            elif varName == 'listobj2':
                self.assertNotEqual(varVal, repr(listobj2))
