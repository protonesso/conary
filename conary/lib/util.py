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


import base64
import bdb
import bz2
from . import debugger
import errno
import fnmatch
import gzip
import hashlib
import os
import re
import select
import shutil
import stat
import string
import io
import struct
import subprocess
import sys
import tempfile
import time
import types
import urllib.request, urllib.parse, urllib.error
import uuid
import weakref
import xmlrpc.client
import zlib

from conary.lib import fixedglob, log, api, urlparse
from conary.lib import networking
from conary.lib.ext import digest_uncompress
from conary.lib.ext import file_utils
from conary.lib.ext import system

# Imported for the benefit of older code,
from conary.lib.formattrace import formatTrace


# Simple ease-of-use extensions to python libraries

def normpath(path):
    s = os.path.normpath(path)
    if s.startswith('//'):
        return s[1:]
    return s

def realpath(path):
    # returns the real path of a file, if and only if it is not a symbolic
    # link
    if not os.path.exists(path):
        return path
    if stat.S_ISLNK(os.lstat(path)[stat.ST_MODE]):
        return path
    return os.path.realpath(path)

def isregular(path):
    return stat.S_ISREG(os.lstat(path)[stat.ST_MODE])


def _mkdirs(path, mode=0o777):
    """
    Recursive helper to L{mkdirChain}. Internal use only.
    """
    head, tail = os.path.split(path)
    if head and tail and not os.path.exists(head):
        _mkdirs(head, mode)

    # Make the directory while ignoring errors about it existing.
    file_utils.mkdirIfMissing(path)


@api.developerApi
def mkdirChain(*paths):
    """
    Make one or more directories if they do not already exist, including any
    needed parent directories. Similar to L{os.makedirs} except that it does
    not error if the requested directory already exists, and it is more
    resilient to race conditions.
    """
    for path in paths:
        path = normpath(os.path.abspath(path))
        if not os.path.exists(path):
            _mkdirs(path)

def searchPath(filename, basepath):
    path = os.path.join(basepath,filename)
    for root, dirs, files in os.walk(basepath):
        if filename in files:
            return os.path.join(root,filename)

def searchFile(file, searchdirs, error=None):
    for dir in searchdirs:
        s = joinPaths(dir, file)
        if os.path.exists(s):
            return s
    if error:
        raise OSError(errno.ENOENT, os.strerror(errno.ENOENT))
    return None

def findFile(file, searchdirs):
    return searchFile(file, searchdirs, error=1)

def which (filename):
    if 'PATH' not in os.environ or os.environ['PATH'] == '':
        p = os.defpath
    else:
        p = os.environ['PATH']

    pathlist = p.split (os.pathsep)

    for path in pathlist:
        f = os.path.join(path, filename)
        if os.access(f, os.X_OK):
            return f
    return None

def recurseDirectoryList(topdir, withDirs=False):
    """Recursively list all files in the directory"""
    items = [topdir]
    while items:
        item = items.pop()
        if os.path.islink(item) or os.path.isfile(item):
            yield item
            continue
        # Directory
        listdir = os.listdir(item)
        # Add the contents of the directory in reverse order (we use pop(), so
        # last element in the list is the one popped out)
        listdir.sort()
        listdir.reverse()
        listdir = [ os.path.join(item, x) for x in listdir ]
        items.extend(listdir)

        if withDirs:
            # This is useful if one wants to catch empty directories
            yield item

def normurl(url):
    surl = list(urlparse.urlsplit(url))
    if surl[2] == '':
        surl[2] = '/'
    elif surl[2] != '/':
        tail = ''
        if surl[2].endswith('/'):
            tail = '/'
        surl[2] = normpath(surl[2]) + tail
    return urlparse.urlunsplit(surl)

errorMessage = '''
ERROR: An unexpected condition has occurred in Conary.  This is
most likely due to insufficient handling of erroneous input, but
may be some other bug.  In either case, please report the error at
http://opensource.sas.com/its/ and attach to the issue the file
%(stackfile)s

Then, for more complete information, please run the following script:
conary-debug "%(command)s"
You can attach the resulting archive to your issue report at
http://opensource.sas.com/its/  For more information, or if you have
trouble with the conary-debug command, go to:
https://opensource.sas.com/conarywiki/index.php/Conary:How_To_File_An_Effective_Bug_Report

To get a debug prompt, rerun the command with --debug-all

Error details follow:

%(filename)s:%(lineno)s
%(errtype)s: %(errmsg)s

The complete related traceback has been saved as %(stackfile)s
'''
_debugAll = False

@api.developerApi
def genExcepthook(debug=True,
                  debugCtrlC=False, prefix='conary-error-',
                  catchSIGUSR1=True, error=errorMessage):
    def SIGUSR1Handler(signum, frame):
        global _debugAll
        _debugAll = True
        print('<Turning on KeyboardInterrupt catching>', file=sys.stderr)

    def excepthook(typ, value, tb):
        if typ is bdb.BdbQuit:
            sys.exit(1)
        #pylint: disable-msg=E1101
        sys.excepthook = sys.__excepthook__
        if not _debugAll and (typ == KeyboardInterrupt and not debugCtrlC):
            sys.exit(1)

        out = BoundedStringIO()
        formatTrace(typ, value, tb, stream = out, withLocals = False)
        out.write("\nFull stack:\n")
        formatTrace(typ, value, tb, stream = out, withLocals = True)
        out.seek(0)
        tbString = out.read()
        del out
        if log.syslog is not None:
            log.syslog("command failed\n%s", tbString)

        if debug or _debugAll:
            formatTrace(typ, value, tb, stream = sys.stderr,
                        withLocals = False)
            if sys.stdout.isatty() and sys.stdin.isatty():
                debugger.post_mortem(tb, typ, value)
            else:
                sys.exit(1)
        elif log.getVerbosity() is log.DEBUG:
            log.debug(tbString)
        else:
            cmd = sys.argv[0]
            if cmd.endswith('/commands/conary'):
                cmd = cmd[:len('/commands/conary')] + '/bin/conary'
            elif cmd.endswith('/commands/cvc'):
                cmd = cmd[:len('/commands/cvc')] + '/bin/cvc'

            origTb = tb
            cmd = normpath(cmd)
            sys.argv[0] = cmd
            while tb.tb_next: tb = tb.tb_next
            lineno = tb.tb_frame.f_lineno
            filename = tb.tb_frame.f_code.co_filename
            tmpfd, stackfile = tempfile.mkstemp('.txt', prefix)
            os.write(tmpfd, tbString)
            os.close(tmpfd)

            sys.stderr.write(error % dict(command=' '.join(sys.argv),
                                                 filename=filename,
                                                 lineno=lineno,
                                                 errtype=typ.__name__,
                                                 errmsg=value,
                                                 stackfile=stackfile))

    #if catchSIGUSR1:
    #    signal.signal(signal.SIGUSR1, SIGUSR1Handler)
    return excepthook



def _handle_rc(rc, cmd):
    if rc:
        if not os.WIFEXITED(rc):
            info = 'Shell command "%s" killed with signal %d' \
                    %(cmd, os.WTERMSIG(rc))
        if os.WEXITSTATUS(rc):
            info = 'Shell command "%s" exited with exit code %d' \
                    %(cmd, os.WEXITSTATUS(rc))
        log.error(info)
        raise RuntimeError(info)

def execute(cmd, destDir=None, verbose=True):
    """
    similar to os.system, but raises errors if exit code != 0 and closes stdin
    so processes can never block on user input
    """
    if verbose:
        log.info(cmd)
    rc = subprocess.call(cmd, shell=True, cwd=destDir, stdin=open(os.devnull))
    # form the rc into a standard exit status
    if rc < 0:
        # turn rc positive
        rc = rc * -1
    else:
        # shift the return code into the high bits
        rc = rc << 8
    _handle_rc(rc, cmd)

class popen:
    """
    Version of popen() that throws errors on close(), unlike os.popen()
    """
    # unfortunately, can't derive from os.popen.  Add methods as necessary.
    def __init__(self, *args):
        self.p = os.popen(*args)
        self.write = self.p.write
        self.read = self.p.read
        self.readline = self.p.readline
        self.readlines = self.p.readlines
        self.writelines = self.p.writelines

    def close(self, *args):
        rc = self.p.close(*args)
        _handle_rc(rc, self.p.name)
        return rc

# string extensions

def find(s, subs, start=0):
    ret = -1
    found = None
    for sub in subs:
        this = string.find(s, sub, start)
        if this > -1 and ( ret < 0 or this < ret):
            ret = this
            found = s[this:this+1]
    return (ret, found)

def literalRegex(s):
    return re.escape(s)


# shutil module extensions, with {}-expansion and globbing
class BraceExpander(object):
    """Class encapsulating the logic required by the brace expander parser"""
    class Alternative(list):
        def __repr__(self):
            return "Alternative%s" % list.__repr__(self)
    class Product(list):
        def __repr__(self):
            return "Product%s" % list.__repr__(self)
    class Comma(object):
        "Comma operator"
    class Concat(object):
        "Concatenation operator"

    @classmethod
    def _collapseNode(cls, node):
        if isinstance(node, str):
            # Char data
            return [ node ]
        if not node:
            return []
        components = [ cls._collapseNode(x) for x in node ]
        if isinstance(node, cls.Product):
            ret = cls._cartesianProduct(components)
            return ret
        ret = []
        for comp in components:
            ret.extend(comp)
        if not isinstance(node, cls.Alternative) or len(components) != 1:
            return ret
        # CNY-3158 - single-length items should not be expanded
        return [ '{%s}' % x for x in ret ]

    @classmethod
    def _cartesianProduct(cls, components):
        ret = list(components.pop())
        while components:
            comp = components.pop()
            nret = []
            for j in comp:
                nret.extend("%s%s" % (j, x) for x in ret)
            ret = nret
        return ret

    @classmethod
    def _reversePolishNotation(cls, listObj):
        haveComma = False
        haveText = False
        # Sentinel
        listObj.append(None)
        outputQ = []
        operators = []
        lastWasLiteral = False
        for item in listObj:
            if isinstance(item, str):
                if not haveText:
                    text = []
                    outputQ.append(text)
                    haveText = True
                else:
                    text = outputQ[-1]
                text.append(item)
                continue
            if haveText:
                topNode = outputQ.pop()
                topNode = ''.join(topNode)
                haveText = False
                outputQ.append(topNode)
                lastWasLiteral = True

            if item is None:
                # We've reached the sentinel
                break
            if item is cls.Comma:
                haveComma = True
                lastWasLiteral = False
                while operators:
                    op = operators.pop()
                    outputQ.append(op)
                operators.append(item)
                continue
            outputQ.append(item)
            if not lastWasLiteral:
                lastWasLiteral = True
                continue
            # Concatenation
            while operators and operators[-1] is not cls.Comma:
                op = operators.pop()
                outputQ.append(op)
            operators.append(cls.Concat)
        while operators:
            op = operators.pop()
            outputQ.append(op)
        # Now collapse into meaningful nodes
        stack = []
        opMap = {
            cls.Comma: cls.Alternative,
            cls.Concat: cls.Product,
        }
        for item in outputQ:
            if not (item is cls.Comma or item is cls.Concat):
                stack.append(item)
                continue
            op2 = stack.pop()
            op1 = stack.pop()
            ncls = opMap[item]
            if isinstance(op1, ncls):
                op1.append(op2)
                stack.append(op1)
            elif isinstance(op2, ncls):
                op2[0:0] = [op1]
                stack.append(op2)
            else:
                nobj = ncls()
                nobj.extend([op1, op2])
                stack.append(nobj)
        ret = stack[0]
        if not haveComma:
            ret = cls.Alternative([ret])
        return ret

    @classmethod
    def removeComma(cls, l):
        for item in l:
            if item is cls.Comma:
                yield ','
            else:
                yield item

    @classmethod
    def braceExpand(cls, path):
        stack = [ cls.Product() ]
        isEscaping = False
        for c in path:
            if isEscaping:
                isEscaping = False
                stack[-1].append(c)
                continue
            if c == '\\':
                isEscaping = True
                continue
            if c == '{':
                stack.append([])
                continue
            if not stack:
                raise ValueError('path %s has unbalanced {}' %path)
            if c == '}':
                if len(stack) == 1:
                    # Unbalanced }; add it as literal
                    stack[-1].append(c)
                    continue
                n = stack.pop()
                # ,} case
                if n and n[-1] is cls.Comma:
                    n.append("")
                stack[-1].append(cls._reversePolishNotation(n))
                continue
            if c == ',':
                # Mark the comma separator, but only if a previous { was
                # found, otherwise treat it as a regular character
                if len(stack) > 1:
                    # {,a} case - leading comma will produce an empty string
                    if not stack[-1]:
                        stack[-1].append("")
                    c = cls.Comma
            stack[-1].append(c)
        if len(stack) > 1:
            # Unbalanced {; add it as literal
            node = stack[0]
            for onode in stack[1:]:
                node.append('{')
                node.extend(cls.removeComma(onode))
        node = stack[0]
        del stack
        # We need to filter empty strings from the output:
        # a{,b} should produce a ab while {,a} should produce a
        return [ x for x in cls._collapseNode(node) if x]

def braceExpand(path):
    return BraceExpander.braceExpand(path)

@api.publicApi
def braceGlob(paths):
    """
    @raises ValueError: raised if paths has unbalanced braces
    @raises OSError: raised in some cases where lstat on a path fails
    """
    pathlist = []
    for path in braceExpand(paths):
        pathlist.extend(fixedglob.glob(path))
    return pathlist

@api.developerApi
def rmtree(paths, ignore_errors=False, onerror=None):
    for path in braceGlob(paths):
        log.debug('deleting [tree] %s', path)
        # act more like rm -rf -- allow files, too
        if (os.path.islink(path) or
                (os.path.exists(path) and not os.path.isdir(path))):
            os.remove(path)
        else:
            os.path.walk(path, _permsVisit, None)
            shutil.rmtree(path, ignore_errors, onerror)

def _permsVisit(arg, dirname, names):
    for name in names:
        path = joinPaths(dirname, name)
        mode = os.lstat(path)[stat.ST_MODE]
        # has to be executable to cd, readable to list, writeable to delete
        if stat.S_ISDIR(mode) and (mode & 0o700) != 0o700:
            log.warning("working around illegal mode 0%o at %s", mode, path)
            mode |= 0o700
            os.chmod(path, mode)

def remove(paths, quiet=False):
    for path in braceGlob(paths):
        if os.path.isdir(path) and not os.path.islink(path):
            log.warning('Not removing directory %s', path)
        elif os.path.exists(path) or os.path.islink(path):
            if not quiet:
                log.debug('deleting [file] %s', path)
            os.remove(path)
        else:
            log.warning('file %s does not exist when attempting to delete [file]', path)

def copyfile(sources, dest, verbose=True):
    for source in braceGlob(sources):
        if verbose:
            log.info('copying %s to %s', source, dest)
        shutil.copy2(source, dest)

def copyfileobj(source, dest, callback = None, digest = None,
                abortCheck = None, bufSize = 128*1024, rateLimit = None,
                sizeLimit = None, total=0):
    if hasattr(dest, 'send'):
        write = dest.send
    else:
        write = dest.write

    if rateLimit is None:
        rateLimit = 0

    if not rateLimit == 0:
        if rateLimit < 8 * 1024:
            bufSize = 4 * 1024
        else:
            bufSize = 8 * 1024

        rateLimit = float(rateLimit)

    starttime = time.time()

    copied = 0

    if abortCheck and hasattr(source, 'fileno'):
        pollObj = select.poll()
        pollObj.register(source.fileno(), select.POLLIN)
    else:
        pollObj = None

    while True:
        if sizeLimit and (sizeLimit - copied < bufSize):
            bufSize = sizeLimit - copied

        if abortCheck:
            # if we need to abortCheck, make sure we check it every time
            # read returns, and every five seconds
            l = []
            while not l:
                if abortCheck():
                    return None
                if pollObj:
                    l = pollObj.poll(5000)
                else:
                    break

        buf = source.read(bufSize)
        if not buf:
            break

        total += len(buf)
        copied += len(buf)
        write(buf)

        if digest:
            digest.update(buf)

        now = time.time()
        if now == starttime:
            rate = 0 # don't bother limiting download until now > starttime.
        else:
            rate = copied / ((now - starttime))

        if callback:
            callback(total, rate)

        if copied == sizeLimit:
            break

        if rateLimit > 0 and rate > rateLimit:
            time.sleep((copied / rateLimit) - (copied / rate))

    return copied

def rename(sources, dest):
    for source in braceGlob(sources):
        log.debug('renaming %s to %s', source, dest)
        os.rename(source, dest)

def _copyVisit(arg, dirname, names):
    sourcelist = arg[0]
    sourcelen = arg[1]
    dest = arg[2]
    filemode = arg[3]
    dirmode = arg[4]
    if dirmode:
        os.chmod(dirname, dirmode)
    for name in names:
        if filemode:
            os.chmod(joinPaths(dirname, name), filemode)
        sourcelist.append(joinPaths(dest, dirname[sourcelen:], name))

def copytree(sources, dest, symlinks=False, filemode=None, dirmode=None):
    """
    Copies tree(s) from sources to dest, returning a list of
    the filenames that it has written.
    """
    sourcelist = []
    for source in braceGlob(sources):
        if os.path.isdir(source):
            if source[-1] == '/':
                source = source[:-1]
            thisdest = joinPaths(dest, os.path.basename(source))
            log.debug('copying [tree] %s to %s', source, thisdest)
            shutil.copytree(source, thisdest, symlinks)
            if dirmode:
                os.chmod(thisdest, dirmode)
            os.path.walk(source, _copyVisit,
                         (sourcelist, len(source), thisdest, filemode, dirmode))
        else:
            log.debug('copying [file] %s to %s', source, dest)
            shutil.copy2(source, dest)
            if dest.endswith(os.sep):
                thisdest = joinPaths(dest, os.path.basename(source))
            else:
                thisdest = dest
            if filemode:
                os.chmod(thisdest, filemode)
            sourcelist.append(thisdest)
    return sourcelist

def checkPath(binary, root=None):
    """
    Examine $PATH to determine if a binary exists, returns full pathname
    if it exists; otherwise None.
    """
    path = os.environ.get('PATH', '')
    if binary[0] == '/':
        # handle case where binary starts with / seperately
        # because os.path.join will not do the right
        # thing with root set.
        if root:
            if os.path.exists(root + binary):
                return root + binary
        elif os.path.exists(binary):
            return binary
        return None

    for path in path.split(os.pathsep):
        if root:
            path = joinPaths(root, path)
        candidate = os.path.join(path, binary)
        if os.access(candidate, os.X_OK):
            if root:
                return candidate[len(root):]
            return candidate
    return None

def joinPaths(*args):
    return normpath(os.sep.join(args))

def splitPathReverse(path):
    """Split the path at the operating system's separators.
    Returns a list with the path components in reverse order.
    Empty path components are stripped out.
    Example: 'a//b//c/d' -> ['d', 'c', 'b', 'a']
    """
    while 1:
        path, tail = os.path.split(path)
        if not tail:
            break
        yield tail

def splitPath(path):
    """Split the path at the operating system's separators
    Empty path components are stripped out
    Example: 'a//b//c/d' -> ['a', 'b', 'c', 'd']
    """
    ret = list(splitPathReverse(path))
    ret.reverse()
    return ret

def assertIteratorAtEnd(iter):
    try:
        next(iter)
        raise AssertionError
    except StopIteration:
        return True

ref = weakref.ref
class ObjectCache(dict):
    """
    Implements a cache of arbitrary (hashable) objects where an object
    can be looked up and have its cached value retrieved. This allows
    a single copy of immutable objects to be kept in memory.
    """
    def __init__(self, *args):
        dict.__init__(self, *args)

        def remove(k, selfref=ref(self)):
            self = selfref()
            if self is not None:
                return dict.__delitem__(self, k)
        self._remove = remove

    def __setitem__(self, key, value):
        return dict.__setitem__(self, ref(key, self._remove), ref(value))

    def __contains__(self, key):
        return dict.__contains__(self, ref(key))

    def has_key(self, key):
        return key in self

    def __delitem__(self, key):
        return dict.__delitem__(self, ref(key))

    def __getitem__(self, key):
        return dict.__getitem__(self, ref(key))()

    def setdefault(self, key, value):
        return dict.setdefault(self, ref(key, self._remove), ref(value))()

def memsize(pid = None):
    return memusage(pid = pid)[0]

def memusage(pid = None):
    """Get the memory usage.
    @param pid: Process to analyze (None for current process)
    """
    if pid is None:
        pfn = "/proc/self/statm"
    else:
        pfn = "/proc/%d/statm" % pid
    line = open(pfn).readline()
    # Assume page size is 4k (true for i386). This can be adjusted by reading
    # resource.getpagesize()
    arr = [ 4 * int(x) for x in line.split()[:6] ]
    vmsize, vmrss, vmshared, text, lib, data = arr

    # The RHS in the following description is the fields in /proc/self/status
    # text is VmExe
    # data is VmData + VmStk
    return vmsize, vmrss, vmshared, text, lib, data

def createLink(src, to):
    name = os.path.basename(to)
    path = os.path.dirname(to)
    mkdirChain(path)
    tmpfd, tmpname = tempfile.mkstemp(name, '.ct', path)
    os.close(tmpfd)
    os.remove(tmpname)
    os.link(src, tmpname)
    os.rename(tmpname, to)

def tupleListBsearchInsert(haystack, newItem, cmpFn):
    """
    Inserts newItem into haystack, maintaining the sorted order. The
    cmpIdx is the item number in the list of tuples to base comparisons on.
    Duplicates items aren't added. Returns True if the item was added,
    False if it was already present.

    @param haystack: list of tuples.
    @type haystack: list
    @param newItem: The item to be inserted
    @type newItem: tuple
    @param cmpFn: Comparison function
    @type cmpFn: function
    @rtype: bool
    """
    start = 0
    finish = len(haystack) - 1
    while start < finish:
        i = (start + finish) / 2

        rc = cmpFn(haystack[i], newItem)
        if rc == 0:
            start = i
            finish = i
            break
        elif rc < 0:
            start = i + 1
        else:
            finish = i - 1

    if start >= len(haystack):
        haystack.append(newItem)
    else:
        rc = cmpFn(haystack[start], newItem)
        if rc < 0:
            haystack.insert(start + 1, newItem)
        elif rc > 0:
            haystack.insert(start, newItem)
        else:
            return False

    return True

_tempdir = tempfile.gettempdir()
def settempdir(tempdir):
    # XXX add locking if we ever go multi-threadded
    global _tempdir
    _tempdir = tempdir

def mkstemp(suffix="", prefix=tempfile.template, dir=None, text=False):
    """
    a wrapper for tempfile.mkstemp that uses a common prefix which
    is set through settempdir()
    """
    if dir is None:
        global _tempdir
        dir = _tempdir
    return tempfile.mkstemp(suffix=suffix, prefix=prefix, dir=dir, text=text)


def setCloseOnExec(fd):
    try:
        import fcntl
    except ImportError:
        return
    if hasattr(fd, 'fileno'):
        fd = fd.fileno()
    flags = fcntl.fcntl(fd, fcntl.F_GETFD)
    flags |= fcntl.FD_CLOEXEC
    fcntl.fcntl(fd, fcntl.F_SETFD, flags)


class ExtendedFdopen(object):

    __slots__ = [ 'fd' ]

    def __init__(self, fd):
        self.fd = fd
        setCloseOnExec(fd)

    def fileno(self):
        return self.fd

    def close(self):
        os.close(self.fd)
        self.fd = None

    def __del__(self):
        if self.fd is not None:
            try:
                self.close()
            except OSError:
                self.fd = None

    def read(self, bytes = -1):
        # -1 is not a valid argument for os.read(); we have to
        # implement "read all data available" ourselves
        if bytes == -1:
            bufSize = 8 * 1024
            l = []
            while 1:
                s = os.read(self.fd, bufSize)
                if not s:
                    return ''.join(l)
                l.append(s)
        return os.read(self.fd, bytes)

    def truncate(self, offset=0):
        return os.ftruncate(self.fd, offset)

    def write(self, s):
        return os.write(self.fd, s)

    def pread(self, bytes, offset):
        if bytes < 0:
            raise ValueError("Invalid byte count %s" % bytes)
        return file_utils.pread(self.fd, bytes, offset)

    def pwrite(self, data, offset):
        return file_utils.pwrite(self.fd, data, offset)

    def seek(self, offset, whence = 0):
        return os.lseek(self.fd, offset, whence)

    def tell(self):
        # 1 is SEEK_CUR
        return os.lseek(self.fd, 0, 1)


class ExtendedFile(ExtendedFdopen):

    __slots__ = [ 'fObj', 'name' ]

    def close(self):
        if not self.fObj:
            return
        self.fObj.close()
        self.fd = None
        self.fObj = None

    def __repr__(self):
        return '<ExtendedFile %r>' % (self.name,)

    def __init__(self, path, mode = "r", buffering = True):
        self.fd = None

        assert(not buffering)
        # we use a file object here to avoid parsing the mode ourself, as well
        # as to get the right exceptions on open. we have to keep the file
        # object around to keep it from getting garbage collected though
        self.fObj = file(path, mode)
        self.name = path
        fd = self.fObj.fileno()
        ExtendedFdopen.__init__(self, fd)

class ExtendedStringIO(io.StringIO):

    def pread(self, bytes, offset):
        pos = self.tell()
        self.seek(offset, 0)
        data = self.read(bytes)
        self.seek(pos, 0)
        return data

    def pwrite(self, data, offset):
        pos = self.tell()
        self.seek(offset, 0)
        rc = self.write(data)
        self.seek(pos, 0)
        return rc


class SeekableNestedFile:

    def __init__(self, file, size, start = -1):
        self.file = file
        self.size = size
        self.end = self.size
        self.pos = 0

        if start == -1:
            self.start = file.tell()
        else:
            self.start = start

    def _fdInfo(self):
        if hasattr(self.file, '_fdInfo'):
            fd, start, size = self.file._fdInfo()
            start += self.start
            size = self.size
        elif hasattr(self.file, 'fileno'):
            fd, start, size = self.file.fileno(), self.start, self.size
        else:
            return (None, None, None)

        return (fd, start, size)

    def close(self):
        pass

    def read(self, bytes = -1, offset = None):
        if offset is None:
            readPos = self.pos
        else:
            readPos = offset

        if bytes < 0 or (self.end - readPos) <= bytes:
            # return the rest of the file
            count = self.end - readPos
            newPos = self.end
        else:
            count = bytes
            newPos = readPos + bytes

        buf = self.file.pread(count, readPos + self.start)

        if offset is None:
            self.pos = newPos

        return buf

    pread = read

    def seek(self, offset, whence = 0):
        if whence == 0:
            newPos = offset
        elif whence == 1:
            newPos = self.pos + offset
        else:
            newPos = self.size + offset

        if newPos > self.size or newPos < 0:
            raise IOError("Position %d is outside file (len %d)"
                    % (newPos, self.size))

        self.pos = newPos
        return self.pos

    def tell(self):
        return self.pos


class BZ2File:
    def __init__(self, fobj):
        self.decomp = bz2.BZ2Decompressor()
        self.fobj = fobj
        self.leftover = ''

    def read(self, bytes):
        while 1:
            buf = self.fobj.read(2048)
            if not buf:
                # ran out of compressed input
                if self.leftover:
                    # we have some uncompressed stuff left, return
                    # it
                    if len(self.leftover) > bytes:
                        rc = self.leftover[:bytes]
                        self.leftover = self.leftover[bytes:]
                    else:
                        rc = self.leftover[:]
                        self.leftover = None
                    return rc
                # done returning all data, return None as the EOF
                return None
            # decompressed the newly read compressed data
            self.leftover += self.decomp.decompress(buf)
            # if we have at least what the caller asked for, return it
            if len(self.leftover) > bytes:
                rc = self.leftover[:bytes]
                self.leftover = self.leftover[bytes:]
                return rc
            # read some more data and try to get enough uncompressed
            # data to return

class PushIterator:

    def push(self, val):
        self.head.insert(0, val)

    def __next__(self):
        if self.head:
            val = self.head.pop(0)
            return val

        return next(self.iter)

    def __init__(self, iter):
        self.head = []
        self.iter = iter

class PeekIterator:

    def _next(self):
        try:
            self.val = next(self.iter)
        except StopIteration:
            self.done = True

    def peek(self):
        if self.done:
            raise StopIteration

        return self.val

    def __next__(self):
        if self.done:
            raise StopIteration

        val = self.val
        self._next()
        return val

    def __iter__(self):
        while True:
            yield next(self)

    def __init__(self, iter):
        self.done = False
        self.iter = iter
        self._next()

class IterableQueue:

    def add(self, item):
        self.l.append(item)

    def peekRemainder(self):
        return self.l

    def __iter__(self):
        while self.l:
            yield self.l.pop(0)

        raise StopIteration

    def __init__(self):
        self.l = []

def lstat(path):
    """
    Return None if the path doesn't exist.
    """
    if not file_utils.lexists(path):
        return None

    try:
        sb = os.lstat(path)
    except OSError as e:
        if e.errno != errno.ENOENT:
            raise
        return None

    return sb

class LineReader:

    def readlines(self):
        s = os.read(self.fd, 4096)
        if not s:
            if self.buf:
                s = self.buf
                self.buf = ''
                return [ s ]

            return None

        self.buf += s

        lines = self.buf.split('\n')
        self.buf = lines[-1]
        del lines[-1]

        return [ x + "\n" for x in lines ]

    def __init__(self, fd):
        self.fd = fd
        self.buf = ''

exists = file_utils.lexists
removeIfExists = file_utils.removeIfExists
pread = file_utils.pread
res_init = system.res_init
sha1Uncompress = digest_uncompress.sha1Uncompress
fchmod = file_utils.fchmod
fopenIfExists = file_utils.fopenIfExists

def _LazyFile_reopen(method):
    """Decorator to perform the housekeeping of opening/closing of fds"""
    def wrapper(self, *args, **kwargs):
        if self._realFd is not None:
            # Object is already open
            # Mark it as being used
            self._timestamp = time.time()
            # Return the real method
            return getattr(self._realFd, method.__name__)(*args, **kwargs)
        if self._cache is None:
            raise Exception("Cache object is closed")
        try:
            self._cache()._getSlot()
        except ReferenceError:
            # re-raise for now, until we decide what to do
            raise
        self._reopen()
        return getattr(self._realFd, method.__name__)(*args, **kwargs)
    return wrapper


class _LazyFile(object):
    __slots__ = ['path', 'marker', 'mode', '_cache', '_hash', '_realFd',
                 '_timestamp']
    def __init__(self, cache, path, mode):
        self.path = path
        self.mode = mode
        self.marker = (0, 0)
        self._hash = cache._getCounter()
        self._cache = weakref.ref(cache, self._closeCallback)
        self._realFd = None
        self._timestamp = time.time()

    def _reopen(self):
        # Initialize the file descriptor
        self._realFd = ExtendedFile(self.path, self.mode, buffering = False)
        self._realFd.seek(*self.marker)
        self._timestamp = time.time()

    def _release(self):
        self._close()

    def _closeCallback(self, cache):
        """Called when the cache object gets destroyed"""
        self._close()
        self._cache = None

    @_LazyFile_reopen
    def read(self, bytes):
        pass

    @_LazyFile_reopen
    def pread(self, bytes, offset):
        pass

    @_LazyFile_reopen
    def seek(self, loc, type):
        pass

    @_LazyFile_reopen
    def tell(self):
        pass

    @_LazyFile_reopen
    def trucate(self):
        pass

    @_LazyFile_reopen
    def fileno(self):
        pass

    def _close(self):
        # Close only the file descriptor
        if self._realFd is not None:
            self.marker = (self._realFd.tell(), 0)
            self._realFd.close()
            self._realFd = None

    def close(self):
        self._close()
        if self._cache is None:
            return
        cache = self._cache()
        if cache is not None:
            try:
                cache._closeSlot(self)
            except ReferenceError:
                # cache object is already gone
                pass
        self._cache = None

    def __hash__(self):
        return self._hash

    def __del__(self):
        self.close()

class LazyFileCache:
    """An object tracking open files. It will serve file-like objects that get
    closed behind the scene (and reopened on demand) if the number of open
    files in the current process exceeds a threshold.
    The objects will close automatically when they fall out of scope.
    """
    # Assuming maxfd is 1024, this should be ok
    threshold = 900

    @api.publicApi
    def __init__(self, threshold=None):
        if threshold:
            self.threshold = threshold
        # Counter used for hashing
        self._fdCounter = 0
        self._fdMap = {}

    @api.publicApi
    def open(self, path, mode="r"):
        """
        @raises IOError: raised if there's an I/O error opening the fd
        @raises OSError: raised on other errors opening the fd
        """
        fd = _LazyFile(self, path, mode=mode)
        self._fdMap[fd._hash] = fd
        # Try to open the fd, to push the errors up early
        fd.tell()
        return fd

    def _getFdCount(self):
        try:
            return countOpenFileDescriptors()
        except OSError as e:
            # We may be hitting a kernel bug (CNY-2571)
            if e.errno != errno.EINVAL:
                raise
            # Count the open file descriptors this instance has
            return len([ x for x in list(self._fdMap.values())
                           if x._realFd is not None])

    def _getCounter(self):
        ret = self._fdCounter;
        self._fdCounter += 1;
        return ret;

    def _getSlot(self):
        if self._getFdCount() < self.threshold:
            # We can open more file descriptors
            return
        # There are several ways we can obtain a slot if the object is full:
        # 1. free one slot
        # 2. free a batch of slots
        # 3. free all slots
        # Running tests which are not localized (i.e. walk over the list of
        # files and do some operation on them) shows that 1. is extremely
        # expensive. 2. and 3. are comparatively similar if we're freeing 10%
        # of the threshold, so that's the current implementation.

        # Sorting would be expensive for selecting just the oldest fd, but
        # when selecting the oldest m fds, performance is m * n. For m large
        # enough, log n will be smaller. For n = 5k, 10% is 500, while log n
        # is about 12. Even factoring in other sorting constants, you're still
        # winning.
        l = sorted([ x for x in list(self._fdMap.values()) if x._realFd is not None],
                   lambda a, b: cmp(a._timestamp, b._timestamp))
        for i in range(int(self.threshold / 10)):
            l[i]._release()

    def _closeSlot(self, fd):
        del self._fdMap[fd._hash]

    @api.publicApi
    def close(self):
        """
        @raises IOError: could be raised if tell() fails prior to close()
        """
        # No need to call fd's close(), we're destroying this object
        for fd in list(self._fdMap.values()):
            fd._close()
            fd._cache = None
        self._fdMap.clear()

    def release(self):
        """Release the file descriptors kept open by the LazyFile objects"""
        for fd in list(self._fdMap.values()):
            fd._close()

    __del__ = close

class Flags(object):

    # set the slots to the names of the flags to support

    __slots__ = []

    def __init__(self, **kwargs):
        for flag in self.__slots__:
            setattr(self, flag, False)

        for (flag, val) in kwargs.items():
            setattr(self, flag, val)

    def __setattr__(self, flag, val):
        if type(val) != bool:
            raise TypeError('bool expected')
        object.__setattr__(self, flag, val)

    def __repr__(self):
        return "%s(%s)" % (self.__class__.__name__,
                ", ".join( "%s=%r" % (flag, getattr(self, flag))
                    for flag in self.__slots__ if getattr(self, flag) ) )

    def copy(self):
        new = type(self)()
        for flag in self.__slots__:
            value = getattr(self, flag)
            object.__setattr__(new, flag, value)
        return new


def stripUserPassFromUrl(url):
    arr = list(urlparse.urlparse(url))
    hostUserPass = arr[1]
    userPass, host = urllib.parse.splituser(hostUserPass)
    arr[1] = host
    return urlparse.urlunparse(arr)


def _FileIgnoreEpipe_ignoreEpipe(fn):
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except IOError as e:
            if e.errno != errno.EPIPE:
                raise
        return
    return wrapper


class FileIgnoreEpipe(object):

    @_FileIgnoreEpipe_ignoreEpipe
    def write(self, *args):
        return self.f.write(*args)

    @_FileIgnoreEpipe_ignoreEpipe
    def close(self, *args):
        return self.f.close(*args)

    def __getattr__(self, name):
        return getattr(self.f, name)

    def __init__(self, f):
        self.f = f

class BoundedStringIO(object):
    """
    An IO object that behaves like a StringIO.
    Data is stored in memory (just like in a StringIO) if shorter than
    maxMemorySize, or in a temporary file.
    """
    defaultMaxMemorySize = 65536
    __slots__ = ['_backend', '_backendType', 'maxMemorySize']
    def __init__(self, buf='', maxMemorySize=None):
        if maxMemorySize is None:
            maxMemorySize = object.__getattribute__(self, 'defaultMaxMemorySize')
        self.maxMemorySize = maxMemorySize
        # Store in memory by default
        self._backend = io.StringIO(buf)
        self._backendType = "memory"

    def _writeImpl(self, s):
        backend = object.__getattribute__(self, '_backend')
        if isinstance(backend, file):
            # File backend
            return backend.write(s)
        # StringIO backend

        maxMemorySize = object.__getattribute__(self, 'maxMemorySize')

        # Save current position
        curPos = backend.tell()
        if curPos + len(s) < maxMemorySize:
            # No danger to overflow the limit
            return backend.write(s)

        fd, name = tempfile.mkstemp(suffix=".tmp", prefix="tmpBSIO")
        # Get rid of the file from the filesystem, we'll keep an open fd to it
        os.unlink(name)
        setCloseOnExec(fd)
        backendFile = os.fdopen(fd, "w+")
        # Copy the data from the current StringIO (up to the current position)
        backend.seek(0)
        backendFile.write(backend.read(curPos))
        ret = backendFile.write(s)
        self._backend = backendFile
        self._backendType = "file"
        return ret

    def _truncateImpl(self, size=None):
        if size is None:
            # Truncate to current position by default
            size = self.tell()
        backend = object.__getattribute__(self, '_backend')
        maxMemorySize = object.__getattribute__(self, 'maxMemorySize')

        if not isinstance(backend, file):
            # Memory backend
            # Truncating always reduces size, so we will not switch to a file
            # for this case
            return backend.truncate(size)

        # File backend
        if size > maxMemorySize:
            # truncating a file to a size larger than the memory limit - just
            # pass it through
            return backend.truncate(size)

        # Need to go from file to memory
        # Read data from file first
        backend.seek(0)
        backendMem = io.StringIO(backend.read(size))
        self._backendType = "memory"
        self._backend = backendMem
        backend.close()

    def getBackendType(self):
        return object.__getattribute__(self, '_backendType')

    def __getattribute__(self, attr):
        # Passing calls to known local objects through
        locs = ['_backend', '_backendType', 'getBackendType', 'maxMemorySize']
        if attr in locs:
            return object.__getattribute__(self, attr)

        if attr == 'write':
            # Return the real implementation of the write method
            return object.__getattribute__(self, '_writeImpl')

        if attr == 'truncate':
            # Return the real implementation of the truncate method
            return object.__getattribute__(self, '_truncateImpl')

        backend = object.__getattribute__(self, '_backend')
        return getattr(backend, attr)

class ProtectedString(str):
    """A string that is not printed in tracebacks"""
    def __safe_str__(self):
        return "<Protected Value>"

    __repr__ = __safe_str__

class ProtectedTemplate(str):
    _substArgs = None
    _templ = None

    """A string template that hides parts of its components.
    The first argument is a template (see string.Template for a complete
    documentation). The values that can be filled in are using the format
    ${VAR} or $VAR. The keyword arguments are expanding the template.
    If one of the keyword arguments has a __safe_str__ method, its value is
    going to be hidden when this object's __safe_str__ is called."""
    def __new__(cls, templ, **kwargs):
        tmpl = string.Template(templ)
        s = str.__new__(cls, tmpl.safe_substitute(kwargs))
        s._templ = tmpl
        s._substArgs = kwargs
        return s

    def __safe_str__(self):
        nargs = {}
        for k, v in self._substArgs.items():
            if hasattr(v, '__safe_str__'):
                v = "<%s>" % k.upper()
            nargs[k] = v
        return self._templ.safe_substitute(nargs)

    __repr__ = __safe_str__

def urlSplit(url, defaultPort = None):
    """A function to split a URL in the format
    <scheme>://<user>:<pass>@<host>:<port>/<path>;<params>#<fragment>
    into a tuple
    (<scheme>, <user>, <pass>, <host>, <port>, <path>, <params>, <fragment>)
    Any missing pieces (user/pass) will be set to None.
    If the port is missing, it will be set to defaultPort; otherwise, the port
    should be a numeric value.
    """
    scheme, netloc, path, query, fragment = urlparse.urlsplit(url)
    userpass, hostport = urllib.parse.splituser(netloc)
    if scheme == 'lookaside':
        # Always a local path, sometimes the first part will have a colon in it
        # but it isn't a port, e.g. "lp:lightdm".
        host, port = hostport, None
    else:
        host, port = networking.splitHostPort(hostport)
    if port is None:
        port = defaultPort

    if userpass:
        user, passwd = urllib.parse.splitpasswd(userpass)
        if sys.version_info[:2] == (2, 7):
            # splituser is considered internal and changed
            # behavior in 2.7.  New behavior is right because
            # it allows : in password, but we must deal with
            # the old 2.6 behavior and not double-unquote
            user = urllib.parse.unquote(user)
            if passwd:
                passwd = urllib.parse.unquote(passwd)
        if passwd:
            passwd = ProtectedString(passwd)
    else:
        user, passwd = None, None
    return scheme, user, passwd, host, port, path, \
        query or None, fragment or None

def urlUnsplit(urlTuple):
    """Recompose a split URL as returned by urlSplit into a single string
    """
    scheme, user, passwd, host, port, path, query, fragment = urlTuple
    userpass = None
    if user:
        if passwd:
            userpass = "%s:${passwd}" % (urllib.parse.quote(user))
        else:
            userpass = urllib.parse.quote(user)
    if host and ':' in host:
        # Support IPv6 addresses as e.g. [dead::beef]:80
        host = '[%s]' % (host,)
    if port is not None:
        hostport = urllib.parse.quote("%s:%s" % (host, port), safe = ':[]')
    else:
        hostport = host
    netloc = hostport
    if userpass:
        netloc = "%s@%s" % (userpass, hostport)
    urlTempl = urlparse.urlunsplit((scheme, netloc, path, query, fragment))
    if passwd is None:
        return urlTempl
    return ProtectedTemplate(urlTempl, passwd = ProtectedString(urllib.parse.quote(passwd)))

def splitExact(s, sep, maxsplit, pad=None):
    """
    Split string using the specified separator, just like string.split()
    Return a list of exactly maxsplit+1 elements.
    If the normal split returns fewer than maxsplit elements, pad the rest of
    the list with the specified pad (defaulting to None)
    """
    if s is None:
        arr = []
    else:
        arr = s.split(sep, maxsplit)
    arrLen = len(arr)
    arr.extend(pad for x in range(maxsplit + 1 - arrLen))
    return arr

class XMLRPCMarshaller(xmlrpc.client.Marshaller):
    """Marshaller for XMLRPC data"""
    dispatch = xmlrpc.client.Marshaller.dispatch.copy()
    def dump_string(self, value, write, escape=xmlrpc.client.escape):
        try:
            value = value.encode("ascii")
        except UnicodeError:
            sio = io.StringIO()
            xmlrpc.client.Binary(value).encode(sio)
            write(sio.getvalue())
            return
        return xmlrpc.client.Marshaller.dump_string(self, value, write, escape)

    def dump(self, values, stream):
        write = stream.write
        if isinstance(values, xmlrpc.client.Fault):
            # Fault instance
            write("<fault>\n")
            self._dump({'faultCode' : values.faultCode,
                        'faultString' : values.faultString},
                       write)
            write("</fault>\n")
        else:
            write("<params>\n")
            for v in values:
                write("<param>\n")
                self._dump(v, write)
                write("</param>\n")
            write("</params>\n")

    def dumps(self, values):
        sio = io.StringIO()
        self.dump(values, sio)
        return sio.getvalue()

    def _dump(self, value, write):
        # Incorporates Patch #1070046: Marshal new-style objects like
        # InstanceType
        try:
            f = self.dispatch[type(value)]
        except KeyError:
            # check if this object can be marshalled as a structure
            try:
                value.__dict__
            except:
                raise TypeError("cannot marshal %s objects" % type(value))
            # check if this class is a sub-class of a basic type,
            # because we don't know how to marshal these types
            # (e.g. a string sub-class)
            for type_ in type(value).__mro__:
                if type_ in list(self.dispatch.keys()):
                    raise TypeError("cannot marshal %s objects" % type(value))
            f = self.dispatch[types.InstanceType]
        f(self, value, write)

    dispatch[str] = dump_string
    dispatch[ProtectedString] = dump_string
    dispatch[ProtectedTemplate] = dump_string

class XMLRPCUnmarshaller(xmlrpc.client.Unmarshaller):
    dispatch = xmlrpc.client.Unmarshaller.dispatch.copy()
    def end_base64(self, data):
        value = xmlrpc.client.Binary()
        value.decode(data)
        self.append(value.data)
        self._value = 0

    dispatch["base64"] = end_base64

    def _stringify(self, data):
        try:
            return data.encode("ascii")
        except UnicodeError:
            return xmlrpc.client.Binary(data)

def xmlrpcGetParser():
    parser, target = xmlrpc.client.getparser()
    # Use our own marshaller
    target = XMLRPCUnmarshaller()
    # Reuse the parser class as computed by xmlrpclib
    parser = parser.__class__(target)
    return parser, target

def xmlrpcDump(params, methodname=None, methodresponse=None, stream=None,
               encoding=None, allow_none=False):
    assert isinstance(params, tuple) or isinstance(params, xmlrpc.client.Fault),\
           "argument must be tuple or Fault instance"
    if isinstance(params, xmlrpc.client.Fault):
        methodresponse = 1
    elif methodresponse and isinstance(params, tuple):
        assert len(params) == 1, "response tuple must be a singleton"

    if not encoding:
        encoding = "utf-8"

    m = XMLRPCMarshaller(encoding, allow_none)
    if encoding != "utf-8":
        xmlheader = "<?xml version='1.0' encoding='%s'?>\n" % str(encoding)
    else:
        xmlheader = "<?xml version='1.0'?>\n" # utf-8 is default

    if stream is None:
        io = io.StringIO(stream)
    else:
        io = stream

    # standard XML-RPC wrappings
    if methodname:
        if not isinstance(methodname, str):
            methodname = methodname.encode(encoding)
        io.write(xmlheader)
        io.write("<methodCall>\n")
        io.write("<methodName>%s</methodName>\n" % methodname)
        m.dump(params, io)
        io.write("</methodCall>\n")
    elif methodresponse:
        io.write(xmlheader)
        io.write("<methodResponse>\n")
        m.dump(params, io)
        io.write("</methodResponse>\n")
    else:
        # Return as-is
        m.dump(params, io)

    if stream is None:
        return io.getvalue()
    return ""

def xmlrpcLoad(stream):
    p, u = xmlrpcGetParser()
    if hasattr(stream, "read"):
        # A real stream
        while 1:
            data = stream.read(16384)
            if not data:
                break
            p.feed(data)
    else:
        # Assume it's a string
        p.feed(stream)
    # This is not the most elegant solution, we could accommodate more parsers
    if hasattr(xmlrpclib, 'expat'):
        try:
            p.close()
        except xmlrpc.client.expat.ExpatError:
            raise xmlrpc.client.ResponseError
    else:
        p.close()
    return u.close(), u.getmethodname()


class ServerProxyMethod(object):

    def __init__(self, send, name):
        self._send = send
        self._name = name

    def __getattr__(self, name):
        if name.startswith('_'):
            raise AttributeError(name)
        return self.__class__(self._send, "%s.%s" % (self._name, name))

    def __call__(self, *args):
        return self._send(self._name, args)


class ServerProxy(object):
    # This used to inherit from xmlrpclib but it replaced everything anyway...

    def __init__(self, url, transport, encoding=None, allow_none=False):
        if isinstance(url, str):
            # Have to import here to avoid an import loop -- one of the many
            # dangers of having a monolithic util.py
            from conary.lib.http.request import URL
            url = URL.parse(url)
        self._url = url
        self._transport = transport
        self._encoding = encoding
        self._allow_none = allow_none

    def _request(self, methodname, params):
        # Call a method on the remote server
        request = xmlrpcDump(params, methodname,
            encoding = self._encoding, allow_none=self._allow_none)

        return self._transport.request(self._url, request)

    def __getattr__(self, name):
        # magic method dispatcher
        if name.startswith('_'):
            raise AttributeError(name)
        return self._createMethod(name)

    def _createMethod(self, name):
        return ServerProxyMethod(self._request, name)

    def __repr__(self):
        return "<ServerProxy for %s>" % (self._url,)

    __str__ = __repr__


def copyStream(src, dest, length = None, bufferSize = 16384):
    """Copy from one stream to another, up to a specified length"""
    amtread = 0
    while amtread != length:
        if length is None:
            bsize = bufferSize
        else:
            bsize = min(bufferSize, length - amtread)
        buf = src.read(bsize)
        if not buf:
            break
        dest.write(buf)
        amtread += len(buf)
    return amtread

def decompressStream(src, bufferSize = 8092):
    sio = BoundedStringIO()
    z = zlib.decompressobj()
    while 1:
        buf = src.read(bufferSize)
        if not buf:
            break
        sio.write(z.decompress(buf))
    sio.write(z.flush())
    sio.seek(0)
    return sio

def compressStream(src, level = 5, bufferSize = 16384):
    sio = BoundedStringIO()
    z = zlib.compressobj(level)
    while 1:
        buf = src.read(bufferSize)
        if not buf:
            break
        sio.write(z.compress(buf))
    sio.write(z.flush())
    return sio

def decompressString(s):
    return zlib.decompress(s, 31)

def massCloseFileDescriptors(start, unusedCount):
    """Close all file descriptors starting with start, until we hit
    unusedCount consecutive file descriptors that were already closed"""
    return file_utils.massCloseFileDescriptors(start, unusedCount, 0)

def nullifyFileDescriptor(fdesc):
    """Connects the file descriptor to /dev/null or an open file (if /dev/null
    does not exist)"""
    try:
        fd = os.open('/dev/null', os.O_RDONLY)
    except OSError:
        # in case /dev/null does not exist
        fd, fn = tempfile.mkstemp()
        os.unlink(fn)
    if fd != fdesc:
        os.dup2(fd, fdesc)
        os.close(fd)


class Timer:

    def start(self):
        self.started = time.time()

    def stop(self):
        self.total += (time.time() - self.started)
        self.started = None

    def get(self):
        if self.started:
            running = time.time() - self.started
        else:
            running = 0

        return self.total + running

    def __init__(self, start = False):
        self.started = None
        self.total = 0
        if start:
            self.start()

def countOpenFileDescriptors():
    """Return the number of open file descriptors for this process."""
    return file_utils.countOpenFileDescriptors()

def convertPackageNameToClassName(pkgname):
    return ''.join([ x.capitalize() for x in pkgname.split('-') ])

class LZMAFile:

    def read(self, limit = 4096):
        # Read exactly the specified amount of bytes. Since the underlying
        # file descriptor is a pipe, os.read may return with fewer than
        # expected bytes, so we need to iterate
        buffers = []
        pos = 0
        while pos < limit:
            buf = os.read(self.infd, limit - pos)
            if not buf:
                break
            buffers.append(buf)
            pos += len(buf)
        return ''.join(buffers)

    def close(self):
        if self.childpid:
            os.close(self.infd)
            os.waitpid(self.childpid, 0)
        self.childpid = None

    def __del__(self):
        self.close()

    def __init__(self, fileobj = None):
        self.executable = None
        for executable, args in (('xz', ('-dc',)), ('unlzma', ('-dc',))):
            for pathElement in os.getenv('PATH', '').split(os.path.pathsep):
                fullpath = joinPaths(pathElement, executable)
                if os.path.exists(fullpath):
                    self.executable = fullpath
                    commandLine = (executable,) + args
                    break
            if self.executable:
                break
        if self.executable is None:
            raise RuntimeError('xz or unlzma is required to decompress this file')

        [ self.infd, outfd ] = os.pipe()
        self.childpid = os.fork()
        if self.childpid == 0:
            try:
                os.close(self.infd)
                if isinstance(fileobj, gzip.GzipFile):
                    # We can't rely on the underlying file descriptor to feed
                    # correct data.
                    # This should really be made to use the read() method of
                    # fileobj
                    f = tempfile.TemporaryFile()
                    copyfileobj(fileobj, f)
                    f.seek(0)
                    fileobj.close()
                    fileobj = f
                os.close(0)
                os.close(1)

                fd = fileobj.fileno()
                # this undoes any buffering
                os.lseek(fd, fileobj.tell(), 0)

                os.dup2(fd, 0)
                fileobj.close() # This closes fd
                os.dup2(outfd, 1)
                os.close(outfd)
                os.execv(self.executable, commandLine)
            finally:
                os._exit(1)

        os.close(outfd)


class SavedException(object):

    def __init__(self, exc_info=None):
        if not exc_info:
            exc_info = sys.exc_info()
        elif isinstance(exc_info, Exception):
            exc_info = exc_info.__class__, exc_info, None
        self.type, self.value, self.tb = exc_info

    def __repr__(self):
        return "<saved %s exception>" % self.getName()

    def getName(self):
        return '.'.join((self.type.__module__, self.type.__name__))

    def format(self):
        return self.getName() + ': ' + str(self.value)

    def throw(self):
        raise self.type(self.value).with_traceback(self.tb)

    def clear(self):
        """Free the exception and traceback to avoid reference loops."""
        self.value = self.tb = None

    def replace(self, value):
        """Replace the saved exception with a new one. The traceback is
        preserved.
        """
        self.type = value.__class__
        self.value = value

    def check(self, *types):
        for type_ in types:
            if issubclass(self.type, type_):
                return True
        return False


def rethrow(newClassOrInstance, prependClassName=True, oldTup=None):
    '''
    Re-throw an exception, either from C{sys.exc_info()} (the default)
    or from C{oldTup} (when set). If C{newClassOrInstance} is a class,
    the original traceback will be stringified and used as the parameter
    to the new exception, otherwise it should be an instance which will
    be thrown as-is. In either case, the original traceback will be
    preserved. Additionally, if it is a class and C{prependClassName} is
    C{True} (the default), the resulting exception will after
    stringification be prepended with the name of the original class.

    Note that C{prependClassName} should typically be set to C{False}
    when re-throwing a re-thrown exception so that the intermediate
    class is not prepended to a value that already has the original
    class name in it.

    @param newClassOrInstance: Class of the new exception to be thrown,
        or the exact exception instance to be thrown.
    @type  newClassOrInstance: subclass or instance of Exception
    @param prependClassName: If C{True}, prepend the original class
        name to the new exception
    @type  prependClassName: bool
    @param oldTup: Exception triple to use instead of the current
        exception
    @type  oldTup: (exc_class, exc_value, exc_traceback)
    '''

    if oldTup is None:
        oldTup = sys.exc_info()
    exc_class, exc_value, exc_traceback = oldTup

    if isinstance(newClassOrInstance, Exception):
        newClass = newClassOrInstance.__class__
        newValue = newClassOrInstance
    else:
        newClass = newClassOrInstance
        newStr = str(exc_value)
        if prependClassName:
            exc_name = getattr(exc_class, '__name__', 'Unknown Error')
            newStr = '%s: %s' % (exc_name, newStr)
        newValue = newClass(newStr)

    raise newClass(newValue).with_traceback(exc_traceback)

class Tick:
    def __init__(self):
        self.last = self.start = time.time()
    def log(self, m = ''):
        now = time.time()
        print("tick: +%.2f %s total=%.3f" % (now-self.last, m, now-self.start))
        self.last = now

class GzipFile(gzip.GzipFile):

    # fix gzip implementation to not seek. i'll probably end up in a
    # hot, firey place for this
    def __init__(self, *args, **kwargs):
        self._first = True
        gzip.GzipFile.__init__(self, *args, **kwargs)

    def _read_gzip_header(self):
        magic = self.fileobj.read(2)
        if magic == '':
            return False

        elif magic != '\037\213':
            raise IOError('Not a gzipped file')
        method = ord( self.fileobj.read(1) )
        if method != 8:
            raise IOError('Unknown compression method')
        flag = ord( self.fileobj.read(1) )
        # modtime = self.fileobj.read(4)
        # extraflag = self.fileobj.read(1)
        # os = self.fileobj.read(1)
        self.fileobj.read(6)

        if flag & gzip.FEXTRA:
            # Read & discard the extra field, if present
            xlen = ord(self.fileobj.read(1))
            xlen = xlen + 256*ord(self.fileobj.read(1))
            self.fileobj.read(xlen)
        if flag & gzip.FNAME:
            # Read and discard a null-terminated string containing the filename
            while True:
                s = self.fileobj.read(1)
                if not s or s=='\000':
                    break
        if flag & gzip.FCOMMENT:
            # Read and discard a null-terminated string containing a comment
            while True:
                s = self.fileobj.read(1)
                if not s or s=='\000':
                    break
        if flag & gzip.FHCRC:
            self.fileobj.read(2)     # Read & discard the 16-bit header CRC

        return True

    def _read(self, size=1024):
        if self.fileobj is None:
            raise EOFError("Reached EOF")

        if self._new_member:
            # If the _new_member flag is set, we have to
            # jump to the next member, if there is one.
            self._init_read()
            if not self._read_gzip_header():
                raise EOFError("Reached EOF")
            self.decompress = zlib.decompressobj(-zlib.MAX_WBITS)
            self._new_member = False

        # Read a chunk of data from the file
        buf = self.fileobj.read(size)

        # If the EOF has been reached, flush the decompression object
        # and mark this object as finished.

        if buf == "":
            uncompress = self.decompress.flush()
            eof = self.decompress.unused_data
            if len(eof) < 8:
                raise IOError("gzip file is truncated or corrupt")
            self._read_eof(eof)
            self._add_read_data( uncompress )
            raise EOFError('Reached EOF')

        uncompress = self.decompress.decompress(buf)
        self._add_read_data( uncompress )

        if self.decompress.unused_data != "":
            eof = self.decompress.unused_data
            eof += self.fileobj.read(8 - len(eof))

            # Check the CRC and file size, and set the flag so we read
            # a new member on the next call
            self._read_eof(eof)
            self._new_member = True

    def _read_eof(self, eof):
        # We've read to the end of the file, so we have to rewind in order
        # to reread the 8 bytes containing the CRC and the file size.
        # We check the that the computed CRC and size of the
        # uncompressed data matches the stored values.  Note that the size
        # stored is the true file size mod 2**32.
        #self.fileobj.seek(-8, 1)
        crc32, isize = struct.unpack("<LL", eof)

        actualCrc = (self.crc & 0xffffffff)
        if crc32 != actualCrc:
            raise IOError("CRC check failed %s != %s" % (hex(crc32),
                                                         hex(actualCrc)))
        elif isize != (self.size & 0xffffffff):
            raise IOError("Incorrect length of data produced")


class DeterministicGzipFile(gzip.GzipFile):
    """
    Patch GzipFile to not write mtimes into output.

    Python 2.7 and later take a mtime argument.
    """

    class _fake_time(object):
        @staticmethod
        def time():
            return 0

    def _write_gzip_header(self):
        # Patch the gzip module, not time.time directly, so other threads
        # calling time.time() by other means are not affected.
        orig_time = gzip.time
        try:
            gzip.time = self._fake_time
            gzip.GzipFile._write_gzip_header(self)
        finally:
            gzip.time = orig_time


# yields sorted paths and their stat bufs
def walkiter(dirNameList, skipPathSet = set(), root = '/'):
    dirNameList.sort()

    for dirName in dirNameList:
        try:
            entries = os.listdir(root + dirName)
        except:
            return

        entries.sort()
        for entry in entries:
            fullPath = os.path.join(dirName, entry)
            if fullPath in skipPathSet:
                continue

            sb = os.lstat(root + fullPath)
            yield fullPath, sb

            if stat.S_ISDIR(sb.st_mode):
                for x in walkiter([fullPath], root = root,
                                  skipPathSet = skipPathSet):
                    yield x

class noproxyFilter(object):
    '''Reads the no-proxy environment variable and can be used to decide
    if the proxy should be bypassed for a specific URL'''
    alwayBypass = False
    no_proxy_list = []
    def __init__(self):
        # From python 2.6's urllib (lynx also seems to obey NO_PROXY)
        no_proxy = os.environ.get('no_proxy', '') or \
            os.environ.get('NO_PROXY', '')
        # '*' is special case for always bypass
        self.alwaysBypass = no_proxy == '*'

        for name in no_proxy.split(','):
            name = name.strip()
            if name:
                self.no_proxy_list.append(name)

    def bypassProxy(self,urlStr):
        if self.alwaysBypass:
            return True
        for x in self.no_proxy_list:
            if urlStr.endswith(x):
                return True
        return False

def fnmatchTranslate(pattern):
    "Like fnmatch.translate, but do not add the end-of-string character(s)"
    patt = fnmatch.translate(pattern)
    # Python 2.6.5 appends \Z(?ms) instead of $
    if patt.endswith('$'):
        return patt[:-1]
    if patt.endswith(r'\Z(?ms)'):
        return patt[:-7]
    raise RuntimeError("Unrecognized end-of-string in %s" % patt)

class LockedFile(object):
    """
    A file protected by a lock.
    To use it::

        l = LockedFile("filename")
        fileobj = l.open()
        if fileobj is None:
            # The target file does not exist. Create it.
            l.write("Some content")
            fileobj = l.commit()
        else:
            # The target file exists
            pass
        print fileobj.read()
    """
    __slots__ = ('fileName', 'lockFileName', '_lockfobj', '_tmpfobj')

    def __init__(self, fileName):
        self.fileName = fileName
        self.lockFileName = self.fileName + '.lck'
        self._lockfobj = None
        self._tmpfobj = None

    def open(self, shouldLock = True):
        """
        Attempt to open the file.

        Returns a file object if the file exists.

        Returns None if the file does not exist, and needs to be created.
        At this point the lock is acquired.  Use write() and commit() to
        have the file created and the lock released.
        """
        import fcntl

        if self._lockfobj is not None:
            self.close()

        fobj = fopenIfExists(self.fileName, "r")
        if fobj is not None or not shouldLock:
            return fobj

        self._lockfobj = open(self.lockFileName, "w")

        # Attempt to lock file in write mode
        fcntl.lockf(self._lockfobj, fcntl.LOCK_EX)
        # If we got this far, we now have the lock. Check if the data file was
        # created
        fobj = fopenIfExists(self.fileName, "r")
        if fobj is not None:
            # The other process committed (and we probably hold a link to a
            # removed file).
            self.unlock()
            return fobj

        if not os.path.exists(self.lockFileName):
            # The original caller returned without creating the data file, and
            # it also removed the lock file - so now we hold a lock on an
            # orphaned fd
            # This should normally not happen, since a close() will not remove
            # the lock file after releasing the lock
            return self.open()
        # We now hold the lock
        return None

    def write(self, data):
        if self._tmpfobj is None:
            # Create temporary file
            self._tmpfobj = AtomicFile(self.fileName)
        self._tmpfobj.write(data)

    def commit(self):
        # It is important that we move the file into place first, before
        # releasing the lock. This make sure that any process that was blocked
        # will see the file immediately, instead of retrying to lock
        if self._tmpfobj is None:
            fileobj = None
        else:
            fileobj = self._tmpfobj.commit(returnHandle = True)
            self._tmpfobj = None
        self.unlock()
        return fileobj

    def unlock(self):
        removeIfExists(self.lockFileName)
        self.close()

    def close(self):
        """Close without removing the lock file"""
        if self._tmpfobj is not None:
            self._tmpfobj.close()
            self._tmpfobj = None
        # This also releases the lock
        if self._lockfobj is not None:
            self._lockfobj.close()
            self._lockfobj = None

    __del__ = close

class AtomicFile(object):
    """
    Open a temporary file adjacent to C{path} for writing. When
    C{f.commit()} is called, the temporary file will be flushed and
    renamed on top of C{path}, constituting an atomic file write.
    """

    fObj = None

    def __init__(self, path, mode='w+b', chmod=0o644, tmpsuffix = "",
                 tmpprefix = None):
        self.finalPath = os.path.realpath(path)
        self.finalMode = chmod

        if tmpprefix is None:
            tmpprefix = os.path.basename(self.finalPath) + '.tmp.'
        fDesc, self.name = tempfile.mkstemp(dir=os.path.dirname(self.finalPath),
            suffix=tmpsuffix, prefix=tmpprefix)
        self.fObj = os.fdopen(fDesc, mode)

    def __getattr__(self, name):
        return getattr(self.fObj, name)

    def commit(self, returnHandle=False):
        """
        C{flush()}, C{chmod()}, and C{rename()} to the target path.
        C{close()} afterwards.
        """
        if self.fObj.closed:
            raise RuntimeError("Can't commit a closed file")

        # Flush and change permissions before renaming so the contents
        # are immediately present and accessible.
        self.fObj.flush()
        os.chmod(self.name, self.finalMode)
        os.fsync(self.fObj)

        # Rename to the new location. Since both are on the same
        # filesystem, this will atomically replace the old with the new.
        os.rename(self.name, self.finalPath)

        # Now close the file.
        if returnHandle:
            fObj, self.fObj = self.fObj, None
            return fObj
        else:
            self.fObj.close()

    def close(self):
        if self.fObj and not self.fObj.closed:
            removeIfExists(self.name)
            self.fObj.close()
    __del__ = close

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, exc_tb):
        if exc_type:
            self.close()
        else:
            self.commit()


class TimestampedMap(object):
    """
    A map that timestamps entries, to cycle them out after delta seconds.
    If delta is set to None, new entries will never go stale.
    """
    __slots__ = [ 'delta', '_map' ]
    _MISSING = object()
    def __init__(self, delta = None):
        self.delta = delta
        self._map = dict()

    def get(self, key, default = None, stale = False):
        v = self._map.get(key, None)
        if v is not None:
            v, ts = v
            if stale or ts is None or time.time() <= ts:
                return v
        return default

    def set(self, key, value):
        if self.delta is None:
            ts = None
        else:
            ts = time.time() + self.delta
        self._map[key] = (value, ts)
        return self

    def clear(self):
        self._map.clear()

    def iteritems(self, stale=False):
        now = time.time()
        ret = sorted(list(self._map.items()), key = lambda x: x[1][1])
        ret = [ (k, v[0]) for (k, v) in ret
            if stale or now <= v[1] ]
        return ret

    def __reduce__(self):
        return (type(self), (self.delta,))


def statFile(pathOrFile, missingOk=False, inodeOnly=False):
    """Return a (dev, inode, size, mtime, ctime) tuple of the given file.

    Accepts paths, file descriptors, and file-like objects with a C{fileno()}
    method.

    @param pathOrFile: A file path or file-like object
    @type  pathOrFile: C{basestring} or file-like object or C{int}
    @param missingOk: If C{True}, return C{None} if the file is missing.
    @type  missingOk: C{bool}
    @param inodeOnly: If C{True}, return just (dev, inode).
    @type  inodeOnly: C{bool}
    @rtype: C{tuple}
    """
    try:
        if isinstance(pathOrFile, str):
            st = os.lstat(pathOrFile)
        else:
            if hasattr(pathOrFile, 'fileno'):
                pathOrFile = pathOrFile.fileno()
            st = os.fstat(pathOrFile)
    except OSError as err:
        if err.errno == errno.ENOENT and missingOk:
            return None
        raise

    if inodeOnly:
        return (st.st_dev, st.st_ino)
    else:
        return (st.st_dev, st.st_ino, st.st_size, st.st_mtime, st.st_ctime)


def iterFileChunks(fobj):
    """Yield chunks of data from the given file object."""
    while True:
        data = fobj.read(16384)
        if not data:
            break
        yield data


class cachedProperty(object):
    """A decorator that creates a memoized property. The first time the
    property is accessed, the decorated function is called and the return value
    is used as the value of the property. It is also stored so that future
    accesses bypass the function.

    The memoized value is stored into the instance dictionary. Because __set__
    is not implemented, this is a "non-data descriptor" and thus the instance
    dictionary overrides the descriptor.
    """

    def __init__(propself, func):
        propself.func = func
        try:
            propself.__doc__ = func.__doc__
        except AttributeError:
            pass

    def __get__(propself, ownself, owncls):
        if ownself is None:
            return propself
        ret = propself.func(ownself)
        setattr(ownself, propself.func.__name__, ret)
        return ret


class SystemIdFactory(object):
    def __init__(self, script):
        self.script = script
        self.systemId = None

    def _run(self):
        try:
            p = subprocess.Popen(self.script, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE)
            stdout, stderr = p.communicate()

            if p.returncode != 0:
                log.warning('SystemId script exited with %s', p.returncode)
                return None

            return base64.b64encode(stdout)
        except OSError as e:
            err, msg = e.args
            log.warning('SystemId script failed with the following error: '
                    '%s', msg)
            return None

    def getId(self):
        if self.systemId:
            return self.systemId

        if self.script and os.path.exists(self.script):
            self.systemId = self._run()
        else:
            sha = hashlib.sha256()
            sha.update(str(uuid.getnode()))
            self.systemId = base64.b64encode(sha.hexdigest())

        return self.systemId
