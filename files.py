#
# Copyright (c) 2004 Specifix, Inc.
# All rights reserved
#
import string
import copy
import os
import sha1helper
import stat
import pwd
import grp
import util
import types
import time
import lookaside
import socket
import sys

_FILE_FLAG_CONFIG = 1 << 0

class FileMode:
    def merge(self, mode):
        """merge another instance of a FileMode into this one"""
        if mode.thePerms is not None:
            self.thePerms = mode.thePerms
        if mode.theOwner is not None:
            self.theOwner = mode.theOwner 
        if mode.theGroup is not None:
            self.theGroup = mode.theGroup
        if mode.thePerms is not None:
            self.thePerms = mode.thePerms
        if mode.theMtime is not None:
            self.theMtime = mode.theMtime
        if mode.theSize is not None:
            self.theSize = mode.theSize
        if mode.theFlags is not None:
            self.theFlags = mode.theFlags

    def triplet(self, code, setbit = 0):
	l = [ "-", "-", "-" ]
	if code & 4:
	    l[0] = "r"
	    
	if code & 2:
	    l[1] = "w"

	if setbit:
	    if code & 1:
		l[2] = "s"
	    else:
		l[2] = "S"
	elif code & 1:
	    l[2] = "x"
	    
	return l

    def sizeString(self):
	return "%8d" % self.theSize

    def timeString(self):
	timeSet = time.localtime(int(self.theMtime))

	nowSet = time.localtime(time.time())

	# if this file is more then 6 months old, use the year
	monthDelta = nowSet[1] - timeSet[1]
	yearDelta = nowSet[0] - timeSet[0]

	if monthDelta < 0:
	    yearDelta = yearDelta - 1
	    monthDelta = monthDelta + 12

	monthDelta = monthDelta + 12 * yearDelta

	if nowSet[2] < timeSet[2]:
	    monthDelta = monthDelta - 1

	if monthDelta < 6:
	    return time.strftime("%b %e %H:%M", timeSet)
	else:
	    return time.strftime("%b %e  %Y", timeSet)

    def perms(self, new = None, addbits = None):
	if (new != None and new != "-"):
	    self.thePerms = new
	
	if addbits:
	    # primarily useful for adding setuid/setgid bits
	    self.thePerms &= addbits

	return self.thePerms

    def acls(self, new = None):
	# we need to implement storing ACLs
	pass

    def eas(self, new = None):
	# we need to implement storing EAs
	pass

    def owner(self, new = None):
	if (new != None and new != "-"):
	    self.theOwner = new

	return self.theOwner

    def group(self, new = None):
	if (new != None and new != "-"):
	    self.theGroup = new

	return self.theGroup

    def size(self, new = None):
	if (new != None and new != "-"):
	    if type(new) == types.IntType:
		self.theSize = new
	    else:
		self.theSize = int(new)

	return self.theSize

    def mtime(self, new = None):
	if (new != None and new != "-"):
	    if type(new) == types.IntType:
		self.theMtime = new
	    else:
		self.theMtime = int(new)

	return self.theMtime

    def flags(self, new = None):
	if (new != None and new != "-"):
	    self.theFlags = new

        if self.theFlags is not None:
            return self.theFlags
        else:
            return 0

    def isConfig(self, set = None):
	if set != None:
            if self.theFlags is None:
                self.theFlags = 0x0
	    if set:
		self.theFlags |= _FILE_FLAG_CONFIG
	    else:
		self.theFlags &= ~(_FILE_FLAG_CONFIG)

	return (self.theFlags and self.theFlags & _FILE_FLAG_CONFIG)

    def infoLine(self):
	return "0%o %s %s %s %s 0x%x" % (self.thePerms, self.theOwner, 
                                         self.theGroup, self.theSize,
                                         self.theMtime, self.flags())
    
    def diff(self, them):
	if not them:
	    return self.infoLine()

	selfLine = self.infoLine().split()
	themLine = them.infoLine().split()

	if selfLine[0] == themLine[0] and len(selfLine) == len(themLine):
	    rc = selfLine[0]
	    for i in range(1, len(selfLine)):
		if selfLine[i] == themLine[i]:
		    rc +=  " -"
		else:
		    rc +=  " " + selfLine[i]

	    return rc
	else:
	    return self.infoLine()

    def same(self, other):
	if self.__class__ != other.__class__: return 0

	if (self.thePerms == other.thePerms and
            self.theOwner == other.theOwner and
            self.theGroup == other.theGroup and
            self.theFlags == other.theFlags and
            self.theSize == other.theSize):
	    return 1

	return 0

    def _applyChangeLine(self, line):
	(p, o, g, s, m, f) = line.split()
	if p == "-": 
	    p = None
	else:
	    p = int(p, 8)

	if f == "-":
	    f = None
	else:
	    f = int(f, 16)

	self.perms(p)
	self.owner(o)
	self.group(g)
	self.mtime(m)
	self.size(s)
	self.flags(f)

    def __init__(self, info = None):
	if info:
	    self._applyChangeLine(info)
	else:
	    self.thePerms = None
	    self.theOwner = None
	    self.theGroup = None
	    self.theMtime = None
	    self.theSize = None
	    self.theFlags = None
	
class File(FileMode):
    def modeString(self):
	l = self.triplet(self.thePerms >> 6, self.thePerms & 04000)
	l = l + self.triplet(self.thePerms >> 3, self.thePerms & 02000)
	l = l + self.triplet(self.thePerms >> 0)
	
	if self.thePerms & 01000:
	    if l[8] == "x":
		l[8] = "t"
	    else:
		l[8] = "T"

	return self.lsTag + string.join(l, "")

    def copy(self):
	return copy.deepcopy(self)

    def infoLine(self):
	return self.infoTag + " " + FileMode.infoLine(self)

    def id(self, new = None):
	if new:
	    self.theId = new

	return self.theId

    def remove(self, target):
	os.unlink(target)

    def restore(self, target, restoreContents, skipMtime = 0):
	self.setOwnerGroup(target)
	self.chmod(target)

	if not skipMtime:
	    os.utime(target, (self.theMtime, self.theMtime))

    def chmod(self, target):
	os.chmod(target, self.thePerms)

    def setOwnerGroup(self, target):
	if os.getuid(): return

        try:
            uid = pwd.getpwnam(self.owner())[2]
        except KeyError:
            print "warning: user %s does not exist - using root" %self.owner()
            uid = 0
        try:
            gid = grp.getgrnam(self.group())[2]
        except KeyError:
            print "warning: group %s does not exist - using root" %self.group()
            gid = 0

	# FIXME: this needs to use lchown, which is in 2.3, and
	# this should happen unconditionally
	os.chown(target, uid, gid)

    # public interface to _applyChangeLine
    #
    # returns 1 if the change worked, 0 if the file changed too much for
    # the change to apply (which means this is a different file type)
    def applyChange(self, line):
	(tag, line) = line.split(None, 1)
	assert(tag == self.infoTag)
	self._applyChangeLine(line)

    def __init__(self, fileId, info = None, infoTag = None):
        assert(self.__class__ is not File)
	self.theId = fileId
	self.infoTag = infoTag
	FileMode.__init__(self, info)

class SymbolicLink(File):

    lsTag = "l"

    def linkTarget(self, newLinkTarget = None):
	if (newLinkTarget and newLinkTarget != "-"):
	    self.theLinkTarget = newLinkTarget

	return self.theLinkTarget

    def infoLine(self):
	return "l %s %s" % (self.theLinkTarget, FileMode.infoLine(self))

    def same(self, other):
	if self.__class__ != other.__class__: return 0

	if self.theLinkTarget == other.theLinkTarget:
	    # recursing does a permission check, which doens't apply 
	    # to symlinks under Linux
	    return 1

	return 0

    def chmod(self, target):
	# chmod() on a symlink follows the symlink
	pass

    def setOwnerGroup(self, target):
	# chmod() on a symlink follows the symlink
	pass

    def restore(self, changeSet, target, restoreContents):
	if os.path.exists(target) or os.path.islink(target):
	    os.unlink(target)
	os.symlink(self.theLinkTarget, target)
	File.restore(self, target, restoreContents, skipMtime = 1)

    def _applyChangeLine(self, line):
	(target, line) = line.split(None, 1)
	self.linkTarget(target)
	File._applyChangeLine(self, line)

    def __init__(self, fileId, line = None):
	if (line):
	    self._applyChangeLine(line)
	else:
	    self.theLinkTarget = None

	File.__init__(self, fileId, line, infoTag = "l")

class Socket(File):

    lsTag = "s"

    def same(self, other):
	return File.same(self, other)

    def restore(self, changeSet, target, restoreContents):
	if os.path.exists(target) or os.path.islink(target):
	    os.unlink(target)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM, 0);
        sock.bind(target)
        sock.close()
	File.restore(self, target, restoreContents)

    def __init__(self, fileId, info = None):
	File.__init__(self, fileId, info, infoTag = "s")

class NamedPipe(File):

    lsTag = "p"

    def same(self, other):
	return File.same(self, other)

    def restore(self, changeSet, target, restoreContents):
	if os.path.exists(target) or os.path.islink(target):
	    os.unlink(target)
	os.mkfifo(target)
	File.restore(self, target, restoreContents)

    def __init__(self, fileId, info = None):
	File.__init__(self, fileId, info, infoTag = "p")

class Directory(File):

    lsTag = "d"

    def same(self, other):
	return File.same(self, other)

    def restore(self, changeSet, target, restoreContents):
	if not os.path.isdir(target):
	    util.mkdirChain(target)

	File.restore(self, target, restoreContents)

    def remove(self, target):
        try:
            os.rmdir(target)
        except OSError, err:
            # XXX
            print "WARNING: rmdir %s failed" %target, err

    def __init__(self, fileId, info = None):
	File.__init__(self, fileId, info, infoTag = "d")

class DeviceFile(File):

    def sizeString(self):
	return "%3d, %3d" % (self.major, self.minor)

    def infoLine(self):
	return "%c %d %d %s" % (self.infoTag, self.major, self.minor,
				  FileMode.infoLine(self))

    def same(self, other):
	if self.__class__ != other.__class__: return 0

	if (self.infoTag == other.infoTag and self.major == other.major and
            self.minor == other.minor):
	    return File.same(self, other)
	
	return 0

    def restore(self, changeSet, target, restoreContents):
	if os.path.exists(target) or os.path.islink(target):
	    os.unlink(target)

	if os.getuid(): return

	if sys.hexversion >= 0x20300f0:
            if self.infoTag == 'c':
                flags = stat.S_IFCHR
            else:
                flags = stat.S_IFBLK
            os.mknod(target, flags, os.makedev(self.major, self.minor))
        else:
            os.system("mknod %s %c %d %d" % (target, self.infoTag, self.major,
                                             self.minor))
            
	File.restore(self, target, restoreContents)

    def majorMinor(self, major = None, minor = None):
	if major is not None:
	    self.major = major
	if minor is not None:
	    self.minor = minor
	
	return (self.infoTag, self.major, self.minor)

    def _applyChangeLine(self, line):
	(ma, mi, line) = line.split(None, 2)

	if ma == "-":
	    ma = None
	else:
	    ma = int(ma)
	    
	if mi == "-":
	    mi = None
	else:
	    mi = int(mi)

	self.majorMinor(ma, mi)
	File._applyChangeLine(self, line)

    def __init__(self, fileId, info = None):
	if (info):
	    self._applyChangeLine(info)

	File.__init__(self, fileId, info, infoTag = self.infoTag)

class BlockDevice(DeviceFile):

    lsTag = "b"

    def __init__(self, fileId, info = None):
	self.infoTag = "b"
	DeviceFile.__init__(self, fileId, info)

class CharacterDevice(DeviceFile):

    lsTag = "c"
    
    def __init__(self, fileId, info = None):
	self.infoTag = "c"
	DeviceFile.__init__(self, fileId, info)

class RegularFile(File):

    lsTag = "-"

    def sha1(self, sha1 = None):
	if sha1 and sha1 != "-":
	    self.thesha1 = sha1

	return self.thesha1

    def infoLine(self):
	return "%s %s %s" % (self.infoTag, self.thesha1, 
			     FileMode.infoLine(self))

    def same(self, other):
	if self.__class__ != other.__class__: return 0

	if self.thesha1 == other.thesha1:
	    return File.same(self, other)

	return 0

    def restore(self, changeSet, target, restoreContents):
	if restoreContents:
	    if os.path.exists(target) or os.path.islink(target):
		os.unlink(target)
	    else:
		path = os.path.dirname(target)
		util.mkdirChain(path)

	    f = open(target, "w")
	    src = changeSet.getFileContents(self.sha1())
	    f.write(src.read())
	    f.close()
	    src.close()

	File.restore(self, target, restoreContents)

    def _applyChangeLine(self, line):
	(sha, line) = line.split(None, 1)
	self.sha1(sha)
	File._applyChangeLine(self, line)

    def __init__(self, fileId, info = None, infoTag = "f"):
	if (info):
	    self._applyChangeLine(info)
	else:
	    self.thesha1 = None

	self.infoTag = infoTag

	File.__init__(self, fileId, info, infoTag = self.infoTag)

class SourceFile(RegularFile):

    def __init__(self, fileId, info = None):
	RegularFile.__init__(self, fileId, info, infoTag = "src")

def FileFromFilesystem(path, fileId, type = None):
    s = os.lstat(path)

    if type == "src":
	f = SourceFile(fileId)
	f.sha1(sha1helper.hashFile(path))
    elif (stat.S_ISREG(s.st_mode)):
	f = RegularFile(fileId)
	f.sha1(sha1helper.hashFile(path))
    elif (stat.S_ISLNK(s.st_mode)):
	f = SymbolicLink(fileId)
	f.linkTarget(os.readlink(path))
    elif (stat.S_ISDIR(s.st_mode)):
	f = Directory(fileId)
    elif (stat.S_ISSOCK(s.st_mode)):
	f = Socket(fileId)
    elif (stat.S_ISFIFO(s.st_mode)):
	f = NamedPipe(fileId)
    elif (stat.S_ISBLK(s.st_mode)):
	f = BlockDevice(fileId)
	f.majorMinor(s.st_rdev >> 8, s.st_rdev & 0xff)
    elif (stat.S_ISCHR(s.st_mode)):
	f = CharacterDevice(fileId)
	f.majorMinor(s.st_rdev >> 8, s.st_rdev & 0xff)
    else:
	raise TypeError, "unsupported file type for %s" % path

    f.perms(s.st_mode & 07777)
    f.owner(pwd.getpwuid(s.st_uid)[0])
    f.group(grp.getgrgid(s.st_gid)[0])
    f.mtime(s.st_mtime)
    f.size(s.st_size)
    f.flags(0)

    return f

def FileFromInfoLine(infoLine, fileId):
    (type, infoLine) = infoLine.split(None, 1)
    if type == "f":
	return RegularFile(fileId, infoLine)
    elif type == "l":
	return SymbolicLink(fileId, infoLine)
    elif type == "d":
	return Directory(fileId, infoLine)
    elif type == "p":
	return NamedPipe(fileId, infoLine)
    elif type == "c":
	return CharacterDevice(fileId, infoLine)
    elif type == "b":
	return BlockDevice(fileId, infoLine)
    elif type == "s":
	return Socket(fileId, infoLine)
    elif type == "src":
	return SourceFile(fileId, infoLine)
    else:
	raise KeyError, "bad infoLine %s" % infoLine
