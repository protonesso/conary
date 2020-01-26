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


""" Tools for printing out extended information about frame variables """

import inspect
import smtplib
import sys
import string
import tempfile
import traceback
import xmlrpc.client

from reprlib import Repr
_repr = Repr()
_repr.maxstring = 3000
_saferepr = _repr.repr

def printTraceBack(tb=None, output=sys.stderr, exc_type=None, exc_msg=None):
    if isinstance(output, str):
        output = open(output, 'w')

    exc_info = sys.exc_info()
    if tb is None:
        tb = exc_info[2]

    if exc_type is None:
        exc_type = exc_info[0]

    if exc_msg is None:
        exc_msg = exc_info[1]

    if exc_type is not None:
        output.write('Exception: ')
        exc_info = '\n'.join(traceback.format_exception_only(exc_type, exc_msg))
        output.write(exc_info)
        output.write('\n\n')

    lines = traceback.format_exception(exc_type, exc_msg, tb)
    output.write(string.joinfields(lines, ""))

    while tb:
        _printFrame(tb.tb_frame, output=output)
        tb = tb.tb_next

def printFrame(frame=0, output=sys.stderr):
    # if output is a path, assume it is a writable one
    # otherwise, it must be an already opened file
    if isinstance(output, str):
        output = open(output, 'w')
    # skip this frame because who cares about the printFrame func?
    if isinstance(frame, int):
        # stack was given in depth form
        # (skip the current frame when counting depth)
        frame = sys._getframe(frame + 1)
    _printFrame(frame, output)

def printStack(frame=0, output=sys.stderr):
    if isinstance(output, str):
        output = open(output, 'w')
    if isinstance(frame, int):
        # stack was given in depth form
        # (skip the current frame when counting depth)
        frame = sys._getframe(frame + 1)
    while(frame):
        output.write("*************************************\n")
        _printFrame(frame, output)
        frame = frame.f_back

def mailStack(frame, recips, sender, subject, extracontent=None):
    file = tempfile.TemporaryFile()
    file.write('Subject: ' +  subject + '\n\n')
    if extracontent:
        file.write(extracontent)
    printStack(frame, file)
    server = smtplib.SMTP('localhost')
    file.seek(0)
    server.sendmail(sender,
                    recips,
                    file.read())
    server.close()
    file.close()

def _printFrame(f, output=sys.stderr):
    c = f.f_code
    argcount =  c.co_argcount
    varnames = c.co_varnames
    args = varnames[:argcount]
    locals = f.f_locals
    globals = f.f_globals
    output.write(">> %s:%s: %s.%s(%s)\n" % ( c.co_filename, f.f_lineno, globals['__name__'], c.co_name, ', '.join(args) ))

    localkeys = [ l for l in list(f.f_locals.keys()) if not inspect.ismodule(locals[l] ) ]
    if argcount > 0:
        output.write("  Params: \n")
        for var in varnames[:argcount]:
            if var in locals:
                val = locals[var]
                val = _getStringValue(val)
                localkeys.remove(var)
            else:
                val = '<Unknown>'

            output.write("    %s = %s\n" % (var, _saferepr(val)))
    for hidden in ('__file__', '__name__', '__doc__'):
        if hidden in localkeys:
            localkeys.remove(hidden)
    localkeys.sort()
    if localkeys:
        output.write("  Locals: \n")
        for key in localkeys:
            if key in locals:
                val = locals[key]
                val = _getStringValue(val)
            else:
                val = '<Unknown>'
            output.write("    %s = %r\n" % (key, _saferepr(val)))

def _getStringValue(val):
    try:
        if isinstance(val, xmlrpc.client.ServerProxy):
            rval = "<Server Proxy>"
        elif hasattr(val, 'asString'):
            rval = val.asString()
        elif inspect.isclass(val):
            rval = '<Class %s.%s>' % (val.__module__, val.__name__)
        elif not hasattr(val, '__str__'):
            if hasattr(val, '__class__'):
                rval = '<unprintable of class %s>' % val.__class__
            else:
                rval = '<unprintable>'
        else:
            rval = val
        return rval
    except Exception as e:
        try:
            return '<Exception occurred while converting %s to string: %s' %(repr(val), e)
        except Exception as e:
            return '<Exception occurred while converting to repr: %s' %(e)
