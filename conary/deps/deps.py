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


import itertools
import re
import weakref
from conary.lib import api
from conary.lib.ext import dep_freeze
from conary.errors import ParseError
import sys

DEP_CLASS_ABI           = 0
DEP_CLASS_IS            = 1
DEP_CLASS_OLD_SONAME    = 2
DEP_CLASS_FILES         = 3
DEP_CLASS_TROVES        = 4
DEP_CLASS_USE           = 5
DEP_CLASS_SONAME        = 6
DEP_CLASS_USERINFO      = 7
DEP_CLASS_GROUPINFO     = 8
DEP_CLASS_CIL           = 9
DEP_CLASS_JAVA          = 10
DEP_CLASS_PYTHON        = 11
DEP_CLASS_PERL          = 12
DEP_CLASS_RUBY          = 13
DEP_CLASS_PHP           = 14
DEP_CLASS_TARGET_IS     = 15
DEP_CLASS_RPM           = 16
DEP_CLASS_RPMLIB        = 17
DEP_CLASS_SENTINEL      = 18

DEP_CLASS_NO_FLAGS      = 0
DEP_CLASS_HAS_FLAGS     = 1
DEP_CLASS_OPT_FLAGS     = 2

FLAG_SENSE_UNSPECIFIED  = 0         # used FlavorScore indices
FLAG_SENSE_REQUIRED     = 1
FLAG_SENSE_PREFERRED    = 2
FLAG_SENSE_PREFERNOT    = 3
FLAG_SENSE_DISALLOWED   = 4

DEP_MERGE_TYPE_NORMAL         = 1    # conflicts are reported
DEP_MERGE_TYPE_OVERRIDE       = 2    # new data wins
DEP_MERGE_TYPE_PREFS          = 3    # like override, but a new !ssl loses
                                     # to an old ~!ssl and a new ~!ssl
                                     # loses to an old !ssl
DEP_MERGE_TYPE_DROP_CONFLICTS = 4    # conflicting flags are removed

senseMap = { FLAG_SENSE_REQUIRED   : "",
             FLAG_SENSE_PREFERRED  : "~",
             FLAG_SENSE_PREFERNOT  : "~!",
             FLAG_SENSE_DISALLOWED : "!" }

toStrongMap = { FLAG_SENSE_REQUIRED    : FLAG_SENSE_REQUIRED,
                FLAG_SENSE_PREFERRED   : FLAG_SENSE_REQUIRED,
                FLAG_SENSE_PREFERNOT   : FLAG_SENSE_DISALLOWED,
                FLAG_SENSE_DISALLOWED  : FLAG_SENSE_DISALLOWED }

toWeakMap = {   FLAG_SENSE_REQUIRED    : FLAG_SENSE_PREFERRED,
                FLAG_SENSE_PREFERRED   : FLAG_SENSE_PREFERRED,
                FLAG_SENSE_PREFERNOT   : FLAG_SENSE_PREFERNOT,
                FLAG_SENSE_DISALLOWED  : FLAG_SENSE_PREFERNOT }
strongSenses = set((FLAG_SENSE_REQUIRED, FLAG_SENSE_DISALLOWED))

senseReverseMap = {}
for key, val in senseMap.items():
    senseReverseMap[val] = key

class DependencyClassRegistry(dict):
    def __getitem__(self, key):
        cls = self.get(key)
        if cls is None:
            cls = self[key] = UnknownDependencyFactory(key)
        return cls

dependencyClasses = DependencyClassRegistry()
dependencyClassesByName = {}

def _registerDepClass(classObj):
    global dependencyClasses
    classObj.compileRegexp()
    dependencyClasses[classObj.tag] = classObj
    dependencyClassesByName[classObj.tagName] = classObj

class BaseDependency(object):

    __slots__ = ( '__weakref__' )

    """
    Implements a single dependency. This is relative to a DependencyClass,
    which is part of a DependencySet. Dependency Sets can be frozen and
    thawed.

    These are hashable, directly comparable, and implement a satisfies()
    method.
    """

    def __hash__(self):
        raise NotImplementedError

    def __eq__(self, other):
        raise NotImplementedError

    def __str__(self):
        raise NotImplementedError

    def freeze(self):
        raise NotImplementedError

    def satisfies(self, required):
        raise NotImplementedError

    def mergeFlags(self, other):
        raise NotImplementedError

    def getName(self):
        raise NotImplementedError

    def getFlags(self):
        raise NotImplementedError

    def __init__(self):
        raise NotImplementedError

class Dependency(BaseDependency):

    __slots__ = ( 'name', 'flags', )

    def __hash__(self):
        val = hash(self.name)
        for flag in self.flags.keys():
            val ^= hash(flag)
        return val

    def __eq__(self, other):
        return other.name == self.name and other.flags == self.flags

    def __cmp__(self, other):
        return (cmp(self.name, other.name)
                or cmp(sorted(self.flags.items()),
                       sorted(other.flags.items())))

    def __str__(self):
        if self.flags:
            flags = list(self.flags.items())
            flags.sort()
            return "%s(%s)" % (self.name,
                    " ".join([ "%s%s" % (senseMap[x[1]], x[0]) for x in flags]))
        else:
            return self.name

    def __repr__(self):
        if self.flags:
            return "Dependency('%s', flags=%s)" % (self.name, self.flags)
        else:
            return "Dependency('%s')" % (self.name)

    def score(self, required):
        """
        Returns a flavor matching score. This dependency is considered
        the "system" and the other is the flavor of the trove. In terms
        of dependencies, this set "provides" and the other "requires".

        False is returned if the two dependencies conflict.
        """
        if self.name != required.name:
            return False

        score = 0
        for (requiredFlag, requiredSense) in required.flags.items():
            thisSense = self.flags.get(requiredFlag, FLAG_SENSE_UNSPECIFIED)
            thisScore = flavorScores[(thisSense, requiredSense)]
            if thisScore is None:
                return False
            score += thisScore

        return score

    def emptyDepsScore(self):
        """
        Like score where this trove is the "requires" and the other trove
        provides nothing.  If all the requires are negative, (!foo)
        this could return something other than False
        """
        score = 0
        if not self.flags:
            # if there are no flags associated with this dependency,
            # then missing the base dep has to be enough to disqualify this
            # flavor
            return False
        for (requiredFlag, requiredSense) in self.flags.items():
            thisScore = flavorScores[(FLAG_SENSE_UNSPECIFIED, requiredSense)]
            if thisScore is None:
                return False
            score += thisScore
        return score

    def satisfies(self, required):
        """
        Returns whether or not this dependency satisfies the argument
        (which is a requires).

        @type required: Dependency
        """
        return self.score(required) is not False

    def toStrongFlavor(self):
        newFlags = self.flags.copy()
        for (flag, sense) in self.flags.items():
            newFlags[flag] = toStrongMap[sense]
        return Dependency(self.name, newFlags)

    def intersection(self, other, strict=True):
        """
        Performs the intersection between the two dependencies, returning
        a dependency with only those flags in both dependencies.

        If strict is False, ignore the difference between ~foo and foo,
        returning with the flag set as it is in self.
        """
        intFlags = {}
        for (flag, sense) in other.flags.items():
            if flag in self.flags:
                if strict:
                    if self.flags[flag] == sense:
                        intFlags[flag] = sense
                elif toStrongMap[self.flags[flag]] == toStrongMap[sense]:
                    intFlags[flag] = toStrongMap[sense]
        if not intFlags:
            if self.flags != other.flags:
                return None
        return Dependency(self.name, intFlags)

    def __and__(self, other):
        return self.intersection(other)

    def difference(self, other, strict=True):
        """
        Performs the difference between the two dependencies, returning
        a dependency with only those flags in self but not in other.
        If strict is false, also remove flags that differ only in the
        strength of the sense, but not its direction (e.g. ~!foo and !foo).
        """

        diffFlags = self.flags.copy()
        if not strict:
            unseenFlags = set(self.flags.keys())
        else:
            unseenFlags = set()

        for flag, sense in other.flags.items():
            if flag in diffFlags:
                if strict:
                    if sense == diffFlags[flag]:
                        del diffFlags[flag]
                elif toStrongMap[sense] == toStrongMap[diffFlags[flag]]:
                    del diffFlags[flag]
                unseenFlags.discard(flag)
        #for flag in unseenFlags:
        #    if diffFlags[flag] in (FLAG_SENSE_PREFERNOT, FLAG_SENSE_PREFERRED):
        #        del diffFlags[flag]
        if not diffFlags:
            return None
        else:
            return Dependency(self.name, diffFlags)

    def __sub__(self, other):
        return self.difference(other)

    def mergeFlags(self, other, mergeType = DEP_MERGE_TYPE_NORMAL):
        """
        Returns a new Dependency which merges the flags from the two
        existing dependencies. We don't want to merge in place as this
        Dependency could be shared between many objects (via a
        DependencyGroup).
        """
        allFlags = self.flags.copy()
        for (flag, otherSense) in other.flags.items():
            if mergeType == DEP_MERGE_TYPE_OVERRIDE or flag not in allFlags:
                allFlags[flag] = otherSense
                continue

            thisSense = allFlags[flag]

            if thisSense == otherSense:
                # same flag, same sense
                continue

            thisStrong = thisSense in strongSenses
            otherStrong = otherSense in strongSenses

            if thisStrong == otherStrong:
                if mergeType == DEP_MERGE_TYPE_DROP_CONFLICTS:
                    del allFlags[flag]
                    continue
                elif mergeType == DEP_MERGE_TYPE_PREFS:
                    # in cases where there's a conflict, new wins
                    allFlags[flag] = otherSense
                    continue
                thisFlag = "%s%s" % (senseMap[thisSense], flag)
                otherFlag = "%s%s" % (senseMap[otherSense], flag)
                raise RuntimeError("Invalid flag combination in merge:"
                                     " %s and %s"  % (thisFlag, otherFlag))

            if mergeType == DEP_MERGE_TYPE_PREFS:
                if thisStrong and toStrongMap[otherSense] == thisSense:
                    continue
                allFlags[flag] = toWeakMap[otherSense]
                continue

            # know they aren't the same, and they are compatible
            elif thisStrong:
                continue
            elif otherStrong:
                allFlags[flag] = otherSense
                continue

            # we shouldn't end up here
            assert(0)

        return Dependency(self.name, allFlags)

    def getName(self):
        return (self.name,)

    def getFlags(self):
        return (list(self.flags.items()),)

    def __init__(self, name, flags = []):
        self.name = name
        if type(flags) == dict:
            self.flags = flags
        else:
            self.flags = {}
            for (flag, sense) in flags:
                self.flags[flag] = sense

class DependencyClass(object):

    __slots__ = ( 'members', )

    depFormat = 'WORD'
    flagFormat = 'WORD'
    WORD = '(?:[.0-9A-Za-z_+-]+)'
    IDENT = '(?:[0-9A-Za-z_-]+)'
    flags = DEP_CLASS_NO_FLAGS

    depNameSignificant = True
    # if True, means that the name of the dependencies in the class hold
    # significance.  This is important for comparing a dep set with all
    # negative flags for this dependency class (say use(!krb)) against
    # no dependencies of this dependency class.  In the use flag case,
    # the dep name is not significant, for other dep classes, the dep name
    # does matter.

    allowParseDep = True

    @classmethod
    def compileRegexp(class_):
        """ Class method that takes the abstract information about the format
            of this dependency class and turns it into a regexp that will
            match dep strings that can be parsed into a dependency of this
            class.
        """
        if not class_.allowParseDep:
            return

        d = dict(flagFormat=class_.flagFormat, depFormat=class_.depFormat,
                 WORD=class_.WORD, IDENT=class_.IDENT)

        # zero or more space-separated flags
        flagFmt = '(?:\( *(%(flagFormat)s?(?: +%(flagFormat)s)*) *\))?'
        # add ^ and $ to ensure we match the entire string passed in
        regexp = ('^ *(%(depFormat)s) *' + flagFmt + ' *$') % d
        # word is a slightly larger group of chars than ident -
        # includes . and +, because those are used in paths and
        # sonames.  May need to be larger some day, and probably
        # could be more restrictive for some groups.  Should not contain
        # /, as that's used as a special char in many dep classes.
        regexp = regexp.replace('WORD', d['WORD'])
        regexp = regexp.replace('IDENT',d['IDENT'])
        class_.regexpStr = regexp
        class_.regexp = re.compile(regexp)

    @classmethod
    def parseDep(class_, s):
        """ Parses a dependency string of this class and returns the
            result.  Raises a ParseError on failure.
        """
        if not class_.allowParseDep:
            raise ParseError("Invalid dependency class %s" % class_.tagName)

        match = class_.regexp.match(s)
        if match is None:
            raise ParseError("Invalid %s dependency: '%s'" % (class_.tagName,
                                                               s))

        depName, flagStr = match.groups() # a dep is <depName>[(<flagStr>)]
                                          # flagStr is None if () not
                                          # in the depStr

        flags = []
        if class_.flags == DEP_CLASS_NO_FLAGS:
            if flagStr is not None:
                # the dep string specified at least () -
                # not allowed when the dep has no flags
                raise ParseError("bad %s dependency '%s':"
                                   " flags not allowed" % (class_.tagName, s))
        elif flagStr:
            flags = [ (x, FLAG_SENSE_REQUIRED) for x in flagStr.split()]
        elif class_.flags == DEP_CLASS_HAS_FLAGS:
            raise ParseError("bad %s dependency '%s':"
                               " flags required" % (class_.tagName, s))
        else:
            assert(class_.flags == DEP_CLASS_OPT_FLAGS)

        return Dependency(depName, flags)

    def addDep(self, dep, mergeType = DEP_MERGE_TYPE_NORMAL):
        assert(dep.__class__.__name__ == self.depClass.__name__)

        if dep.name in self.members:
            # this is a little faster then doing all of the work when
            # we could otherwise avoid it
            if dep == self.members[dep.name]: return

            # merge the flags, and add the newly created dependency
            # into the class
            dep = self.members[dep.name].mergeFlags(dep, mergeType = mergeType)

        self.members[dep.name] = dep
        assert(not self.justOne or len(self.members) == 1)

    def hasDep(self, name):
        return name in self.members

    def score(self, requirements):
        if self.tag != requirements.tag:
            return False

        score = 0
        for requiredDep in requirements.members.values():
            if requiredDep.name not in self.members:
                if self.depNameSignificant:
                    # dependency names are always 'requires', so if the
                    # dependency class name is significant (i.e. the dep
                    # class is only defined by its flags) the empty deps cannot
                    # match.  Otherwise, we use the empty deps score for the
                    # flags
                    return False
                thisScore = requiredDep.emptyDepsScore()
            else:
                thisScore = self.members[requiredDep.name].score(requiredDep)
            if thisScore is False:
                return False

            score += thisScore
            if self.depNameSignificant:
                score += 1

        return score

    def emptyDepsScore(self):
        score = 0
        if self.depNameSignificant:
            # dependency names are always 'requires', so if the
            # dependency class name is significant (i.e. the dep
            # class is only defined by its flags) the empty deps cannot
            # match.  Otherwise, we use the empty deps score for the flags
            return False
        for requiredDep in self.members.values():
            thisScore = requiredDep.emptyDepsScore()
            if thisScore is False:
                return False
            score += thisScore
        return thisScore

    def toStrongFlavor(self):
        newDepClass = self.__class__()
        a = newDepClass.addDep
        for dep in list(self.members.values()):
            a(dep.toStrongFlavor())
        return newDepClass

    def satisfies(self, requirements):
        return self.score(requirements) is not False

    def union(self, other, mergeType = DEP_MERGE_TYPE_NORMAL):
        if other is None: return
        a = self.addDep
        for otherdep in other.members.values():
            # calling this for duplicates is a noop
            a(otherdep, mergeType = mergeType)

    def __and__(self, other):
        return self.intersection(other)

    def intersection(self, other, strict=True):
        newDepClass = self.__class__()
        a = newDepClass.addDep
        found = False
        for tag, dep in self.members.items():
            if tag in other.members:
                dep = dep.intersection(other.members[tag], strict=strict)
                if dep is None:
                    a(Dependency(tag))
                else:
                    a(dep)
                found = True
        if found:
            return newDepClass
        return None

    def difference(self, other, strict=True):
        newDepClass = self.__class__()
        a = newDepClass.addDep
        found = False
        for tag, dep in self.members.items():
            if tag in other.members:
                diff = dep.difference(other.members[tag], strict=strict)
                if diff is None:
                    continue
                a(diff)
            else:
                newDepClass.addDep(dep)
            found = True
        if found:
            return newDepClass
        else:
            return None

    def __sub__(self, other):
        return self.difference(other)

    def getDeps(self):
        # sort by name
        for name, dep in sorted(self.members.items()):
            yield dep

    @staticmethod
    def thawDependency(frozen):
        cached = dependencyCache.get(frozen, None)
        if cached:
            return cached

        name, flags = dep_freeze.depSplit(frozen)

        for i, flag in enumerate(flags):
            kind = flag[0:2]

            if kind == '~!':
                flags[i] = (flag[2:], FLAG_SENSE_PREFERNOT)
            elif kind[0] == '!':
                flags[i] = (flag[1:], FLAG_SENSE_DISALLOWED)
            elif kind[0] == '~':
                flags[i] = (flag[1:], FLAG_SENSE_PREFERRED)
            else:
                flags[i] = (flag, FLAG_SENSE_REQUIRED)

        d = Dependency(name, flags)
        dependencyCache[frozen] = d

        return d

    @staticmethod
    def thawRawDep(name, flags):
        parsed = []
        for flag in flags:
            kind = flag[0:2]
            if kind == '~!':
                parsed.append((flag[2:], FLAG_SENSE_PREFERNOT))
            elif kind[0] == '!':
                parsed.append((flag[1:], FLAG_SENSE_DISALLOWED))
            elif kind[0] == '~':
                parsed.append((flag[1:], FLAG_SENSE_PREFERRED))
            else:
                parsed.append((flag, FLAG_SENSE_REQUIRED))
        return Dependency(name, parsed)

    def __hash__(self):
        val = self.tag
        for dep in self.members.values():
            val ^= hash(dep)

        return val

    def __eq__(self, other):
        if other is None:
            return False
        return self.tag == other.tag and \
               self.members == other.members

    def __cmp__(self, other):
        rv = cmp(sorted(self.members), sorted(other.members))
        if rv:
            return rv
        for name, dep in self.members.items():
            rv = cmp(dep, other.members[name])
            if rv:
                return rv
        return 0

    def __ne__(self, other):
        return not self == other

    def __str__(self):
        memberList = list(self.members.items())
        memberList.sort()
        return "\n".join([ "%s: %s" % (self.tagName, dep[1])
                    for dep in memberList ])

    def __init__(self):
        self.members = {}

class AbiDependency(DependencyClass):

    __slots__ = ()
    tag = DEP_CLASS_ABI
    tagName = "abi"
    justOne = False
    depClass = Dependency
    flags = DEP_CLASS_HAS_FLAGS
_registerDepClass(AbiDependency)


class InstructionSetDependency(DependencyClass):

    __slots__ = ()
    tag = DEP_CLASS_IS
    tagName = "is"
    justOne = False
    depClass = Dependency
    allowParseDep = False
    flags = DEP_CLASS_HAS_FLAGS
_registerDepClass(InstructionSetDependency)

class TargetInstructionSetDependency(DependencyClass):

    __slots__ = ()
    tag = DEP_CLASS_TARGET_IS
    tagName = "target"
    justOne = False
    depClass = Dependency
    allowParseDep = False
    flags = DEP_CLASS_HAS_FLAGS
_registerDepClass(TargetInstructionSetDependency)

class OldSonameDependencies(DependencyClass):

    __slots__ = ()
    tag = DEP_CLASS_OLD_SONAME
    tagName = "oldsoname"
    justOne = False
    depClass = Dependency
    allowParseDep = False
_registerDepClass(OldSonameDependencies)

class SonameDependencies(DependencyClass):

    __slots__ = ()
    tag = DEP_CLASS_SONAME
    tagName = "soname"
    justOne = False
    depClass = Dependency
    depFormat = 'IDENT(?:/WORD)*/WORD'
    flags = DEP_CLASS_HAS_FLAGS
_registerDepClass(SonameDependencies)

class UserInfoDependencies(DependencyClass):

    __slots__ = ()
    tag = DEP_CLASS_USERINFO
    tagName = "userinfo"
    justOne = False
    depClass = Dependency
    flags = DEP_CLASS_NO_FLAGS
_registerDepClass(UserInfoDependencies)

class GroupInfoDependencies(DependencyClass):

    __slots__ = ()
    tag = DEP_CLASS_GROUPINFO
    tagName = "groupinfo"
    justOne = False
    depClass = Dependency
    flags = DEP_CLASS_NO_FLAGS
_registerDepClass(GroupInfoDependencies)

class CILDependencies(DependencyClass):

    __slots__ = ()
    tag = DEP_CLASS_CIL
    tagName = "CIL"
    justOne = False
    depClass = Dependency
    flags = DEP_CLASS_HAS_FLAGS
    depFormat = 'IDENT(?:\.IDENT)*' # foo[.bar]*
    flagFormat = '[0-9.]+'          # 0-9[.0-9]*
_registerDepClass(CILDependencies)

class JavaDependencies(DependencyClass):

    __slots__ = ()
    tag = DEP_CLASS_JAVA
    tagName = "java"
    justOne = False
    depClass = Dependency
    flags = DEP_CLASS_OPT_FLAGS
_registerDepClass(JavaDependencies)

class PythonDependencies(DependencyClass):

    __slots__ = ()
    tag = DEP_CLASS_PYTHON
    tagName = "python"
    justOne = False
    depClass = Dependency
    flags = DEP_CLASS_OPT_FLAGS
_registerDepClass(PythonDependencies)

class PerlDependencies(DependencyClass):

    __slots__ = ()
    tag = DEP_CLASS_PERL
    tagName = "perl"
    justOne = False
    depClass = Dependency
    depFormat = 'WORD(?:::WORD)*' # foo[::bar]* including foo::bar::baz
    flags = DEP_CLASS_OPT_FLAGS
_registerDepClass(PerlDependencies)

class RubyDependencies(DependencyClass):

    __slots__ = ()
    tag = DEP_CLASS_RUBY
    tagName = "ruby"
    justOne = False
    depClass = Dependency
    depFormat = 'IDENT(?:/WORD)*'
    flags = DEP_CLASS_OPT_FLAGS
_registerDepClass(RubyDependencies)

class PhpDependencies(DependencyClass):

    __slots__ = ()
    tag = DEP_CLASS_PHP
    tagName = "php"
    justOne = False
    depClass = Dependency
    flags = DEP_CLASS_OPT_FLAGS
_registerDepClass(PhpDependencies)

class FileDependencies(DependencyClass):

    __slots__ = ()
    tag = DEP_CLASS_FILES
    tagName = "file"
    justOne = False
    depClass = Dependency
    flags = DEP_CLASS_NO_FLAGS
    depFormat = '(?:/WORD)+' # /path[/path]*

_registerDepClass(FileDependencies)

class TroveDependencies(DependencyClass):

    __slots__ = ()
    tag = DEP_CLASS_TROVES
    tagName = "trove"
    justOne = False
    depClass = Dependency
    flags = DEP_CLASS_OPT_FLAGS
    depFormat = 'WORD(?::IDENT)?' # trove[:comp]

_registerDepClass(TroveDependencies)

class UseDependency(DependencyClass):

    __slots__ = ()
    tag = DEP_CLASS_USE
    tagName = "use"
    justOne = True
    depClass = Dependency
    allowParseDep = False
    depNameSignificant = False
_registerDepClass(UseDependency)

class RpmDependencies(DependencyClass):

    __slots__ = ()
    tag = DEP_CLASS_RPM
    tagName = "rpm"
    justOne = False
    depClass = Dependency
    flags = DEP_CLASS_OPT_FLAGS
    WORD = '(?:[:.0-9A-Za-z_+-]+)'          # allow colons in flags
    IDENT = r'(?:[][:.0-9A-Za-z_-]+)'       # allow [] and colons in dep names
    flagFormat = "WORD"
    depFormat = "IDENT"
_registerDepClass(RpmDependencies)

class RpmLibDependencies(DependencyClass):

    __slots__ = ()
    tag = DEP_CLASS_RPMLIB
    tagName = "rpmlib"
    justOne = False
    depClass = Dependency
    flags = DEP_CLASS_OPT_FLAGS
_registerDepClass(RpmLibDependencies)

def UnknownDependencyFactory(intTag):
    # Factory for unknown classes
    class _UnknownDependency(DependencyClass):
        tag = intTag
        tagName = "unknown-%s" % intTag
        depClass = Dependency
        justOne = False
    return _UnknownDependency

#ld = {}

class DependencySet(object):

    __slots__ = ( '_members', '_hash' )

    def _getMembers(self):
        m = self._members
        if type(m) == str:
            #if False:
            #    import sys, os
            #    f = sys._getframe(2)
            #    fn = f.f_code.co_filename
            #    ln = f.f_lineno
            #    global ld
            #    if (fn, ln) not in ld:
            #        l = open(fn).readlines()[ln - 1]
            #        ld[(fn, ln)] = "%s:%s %s" % (os.path.basename(fn), ln, l)
            #        if not l.strip():
            #            import epdb;epdb.st()
            #    print ld[(fn, ln)],
            self._thaw()
            m = self._members

        return m

    members = property(_getMembers)

    def addDep(self, depClass, dep):
        assert(isinstance(dep, Dependency))
        self._hash = None

        tag = depClass.tag
        c = self.members.setdefault(tag, depClass())
        c.addDep(dep)

    def addDeps(self, depClass, deps):
        self._hash = None
        tag = depClass.tag
        c = self.members.setdefault(tag, depClass())

        for dep in deps:
            c.addDep(dep)

    def iterDeps(self, sort=False):
        # since this is in an tight loop in some places, avoid overhead
        # of continual checks on the sort variable.
        if sort:
            for _, depClass in sorted(self.members.items()):
                for _, dep in sorted(depClass.members.items()):
                    yield depClass.__class__, dep
        else:
            for depClass in self.members.values():
                for dep in depClass.members.values():
                    yield depClass.__class__, dep

    def iterDepsByClass(self, depClass):
        if depClass.tag in self.members:
            c = self.members[depClass.tag]
            for dep in c.members.values():
                yield dep

    def iterRawDeps(self):
        # returns deps as (classId, name, flags) tuples
        if type(self._members) == str:
            next = 0
            end = len(self._members)
            while next < end:
                (next, classId, depStr) = dep_freeze.depSetSplit(
                        next, self._members)
                (name, flags) = dep_freeze.depSplit(depStr)
                yield (classId, name, flags)
        else:
            for depClass, oneDep in self.iterDeps():
                flags = [senseMap[sense] + name
                        for (name, sense) in oneDep.getFlags()[0]]
                yield (depClass.tag, oneDep.getName()[0], flags)

    def hasDepClass(self, depClass):
        return depClass.tag in self.members

    def removeDeps(self, depClass, deps, missingOkay = False):
        self._hash = None

        if missingOkay and depClass.tag not in self.members:
            return

        c = self.members[depClass.tag]

        if missingOkay:
            for dep in deps:
                c.members.pop(dep.name, None)
        else:
            for dep in deps:
                del c.members[dep.name]

        if not self.members[depClass.tag].members:
            del self.members[depClass.tag]

    def removeDepsByClass(self, depClass):
        self._hash = None
        self.members.pop(depClass.tag, None)

    def addEmptyDepClass(self, depClass):
        """ adds an empty dependency class, which for flavors has
            different semantics when merging than not having a dependency
            class.  See mergeFlavors """
        self._hash = None
        tag = depClass.tag
        assert(tag not in self.members)
        self.members[tag] = depClass()

    def _thaw(self):
        frz = self._members
        self._members = dict()

        if not frz:
            return

        i = 0
        a = self.addDep
        depSetSplit = dep_freeze.depSetSplit
        while i < len(frz):
            (i, tag, frozen) = depSetSplit(i, frz)
            depClass = dependencyClasses[tag]
            a(depClass, depClass.thawDependency(frozen))

    def copy(self):
        new = self.__class__()
        if type(self._members) == str:
            new.thaw(self._members)
        else:
            add = new.addDep
            for depClass in self.members.values():
                cls = depClass.__class__
                for dep in depClass.members.values():
                    add(cls, dep)

        return new

    __copy__ = lambda s, x: s.copy()
    __deepcopy__ = lambda s, x: s.copy()

    def getDepClasses(self):
        return self.members

    def union(self, other, mergeType = DEP_MERGE_TYPE_NORMAL):
        if other is None:
            return
        assert(isinstance(other, self.__class__)
                or isinstance(self, other.__class__))
        self._hash = None
        a = self.addDep
        for tag, members in other.members.items():
            c = members.__class__
            if tag in self.members:
                self.members[tag].union(members, mergeType = mergeType)

                # If we're dropping conflicts, we might drop this class
                # of troves all together.
                if (mergeType == DEP_MERGE_TYPE_DROP_CONFLICTS
                    and c.justOne and not
                    list(self.members[tag].members.values())[0].flags):
                    del self.members[tag]
            else:
                for dep in members.members.values():
                    a(c, dep)

    def intersection(self, other, strict=True):
        assert(hasattr(other, '_members'))
        newDep = self.__class__()
        for tag, depClass in self.members.items():
            if tag in other.members:
                dep = depClass.intersection(other.members[tag], strict=strict)
                if dep is None:
                    continue
                newDep.members[depClass.tag] = dep
        return newDep

    def __and__(self, other):
        return self.intersection(other)

    def difference(self, other, strict=True):
        assert(isinstance(other, self.__class__)
               or isinstance(self, other.__class__))
        newDep = self.__class__()
        a = newDep.addDep
        for tag, depClass in self.members.items():
            c = depClass.__class__
            if tag in other.members:
                dep = depClass.difference(other.members[tag], strict=strict)
                if dep is not None:
                    newDep.members[tag] = dep
            else:
                for dep in depClass.members.values():
                    a(c, dep)
        return newDep

    def __sub__(self, other):
        return self.difference(other)

    def score(self, other):
        # XXX this should force the classes to be the same, but the
        # flavor and DependencySet split would cause too much breakage
        # right now if we enforced that. We test for DependencySet
        # instead of self.__class__
        assert(isinstance(other, DependencySet))
        score = 0
        for tag in other.members:
            # ignore empty dep classes when scoring
            if not other.members[tag].members:
                continue
            if tag not in self.members:
                thisScore = other.members[tag].emptyDepsScore()
            else:
                thisScore = self.members[tag].score(other.members[tag])
            if thisScore is False:
                return False

            score += thisScore

        return score

    def satisfies(self, other):
        return self.score(other) is not False

    def __eq__(self, other, skipSet = None):
        if other is None:
            return False
        # No much sense in comparing stuff that is not the same class as ours;
        # it also breaks epydoc (CNY-1772). We don't compare the classes
        # here (even though it would be far more sensible) because conary
        # compares DependencySet objects to Flavors sometimes. Which is,
        # of course, awful.
        if not hasattr(other, '_members'):
            return False

        if type(self._members) == dict and type(other._members) == dict:
            if set(other.members.keys()) != set(self.members.keys()):
                return False
            for tag in other.members:
                if not self.members[tag] == other.members[tag]:
                    return False
            return True

        if type(self._members) == dict:
            self._members = self.freeze()
        elif type(other._members) == dict:
            other._members = other.freeze()

        return self._members == other._members

    def __cmp__(self, other):
        if other is None:
            return -1
        # XXX this should force the classes to be the same, but the
        # flavor and DependencySet split would cause too much breakage
        # right now if we enforced that. We test for DependencySet
        # instead of self.__class__
        assert(isinstance(other, DependencySet))
        myMembers = self.members
        otherMembers = other.members
        tags = []
        for tag in range(DEP_CLASS_SENTINEL):
            if tag in myMembers:
                if tag in otherMembers:
                    tags.append(tag)
                else:
                    return -1
            elif tag in otherMembers:
                return 1

        # at this point we know we have the same dep classes.
        for tag in tags:
            myDepClass = myMembers[tag]
            otherDepClass = otherMembers[tag]
            # depClass compares keys first, then values,
            # exactly what we want here.
            rv = cmp(myDepClass, otherDepClass)
            if rv:
                return rv
        return 0

    def __ne__(self, other):
        return not self == other

    def __hash__(self):
        if self._hash is None:
            self._hash = hash(self.freeze())

        return self._hash

    def __bool__(self):
        return not(not(self._members))

    def __str__(self):
        memberList = list(self.members.items())
        memberList.sort()
        return "\n".join([ str(x[1]) for x in memberList])

    def freeze(self, skipSet = None):
        if type(self._members) == str:
            return self._members
        else:
            return dep_freeze.depSetFreeze(self.members)

    def isEmpty(self):
        return not(self._members)

    def __repr__(self):
        return "ThawDep('%s')" % self.freeze()

    def __getstate__(self):
        # If this method returns a false value (like the empty string) then
        # __setstate__ is not called, so we have to return something non-empty.
        return (self.freeze(),)

    def __setstate__(self, frozen):
        self.thaw(frozen[0])

    def __init__(self, frz = None):
        if frz is not None:
            frz = sys.intern(frz)
            self._members = frz
        else:
            self._members = ''

        self._hash = None

    thaw = __init__


# A special class for representing Flavors
class Flavor(DependencySet):
    __slots__ = ()
    def __repr__(self):
        return "Flavor('%s')" % formatFlavor(self)
    def __str__(self):
        return formatFlavor(self)
    def __bool__(self):
        # prohibit evaluating Flavor instances in boolean contexts
        raise SyntaxError("Flavor objects can't be evaluated in a boolean context")

    @api.developerApi
    def toStrongFlavor(self):
        newDep = self.__class__()
        for tag, depClass in self.members.items():
            newDep.members[tag] = depClass.toStrongFlavor()
        return newDep

    @api.developerApi
    def stronglySatisfies(self, other):
        return self.toStrongFlavor().score(
                    other.toStrongFlavor()) is not False

def ThawDependencySet(frz):
    return DependencySet(frz)

@api.publicApi
def ThawFlavor(frz):
    """
    @param frz: the frozen representation of a flavor
    @return: a thawed Flavor object
    @rtype: L{deps.deps.Flavor}
    @raises TypeError: could be raised if frozen object is malformed
    @raises ValueError: could be raised if frozen object is malformed
    """
    f = Flavor()
    if isinstance(frz, str):
        try:
            frz = frz.encode("ascii")
        except UnicodeEncodeError:
            raise ParseError("invalid characters in flavor '%s'" % frz)
    f.thaw(frz)
    return f

@api.developerApi
def overrideFlavor(oldFlavor, newFlavor, mergeType=DEP_MERGE_TYPE_OVERRIDE):
    """
    Performs overrides of flavors as expected when the new flavor is
    specified by a user -- the user's flavor overrides use flags, and
    if the user specifies any instruction sets, only those instruction
    sets will be in the final flavor.  Flags for the specified instruction
    sets are merged with the old flavor.
    """
    flavor = oldFlavor.copy()
    ISD = InstructionSetDependency
    TISD = TargetInstructionSetDependency
    for depClass in  (ISD, TISD):
        if (flavor.hasDepClass(depClass) and newFlavor.hasDepClass(depClass)):

            arches = set()

            for dep in newFlavor.iterDepsByClass(depClass):
                arches.add(dep.name)

            oldArches = []
            for dep in oldFlavor.iterDepsByClass(depClass):
                if dep.name not in arches:
                    oldArches.append(dep)
            flavor.removeDeps(depClass, oldArches)

    flavor.union(newFlavor, mergeType=mergeType)
    return flavor


def _mergeDeps(depList, mergeType):
    """
    Returns a new Dependency which merges the flags from the two
    existing dependencies. We don't want to merge in place as this
    Dependency could be shared between many objects (via a
    DependencyGroup).
    """
    name = depList[0].name

    flags = {}
    for dep in depList:
        assert(dep.name == name)
        for flag, sense in dep.flags.items():
            flags.setdefault(flag, []).append(sense)

    finalFlags = {}
    for flag, senses in flags.items():
        if mergeType == DEP_MERGE_TYPE_OVERRIDE:
            finalFlags[flag] = senses[-1]
            continue

        if FLAG_SENSE_REQUIRED in senses:
            posSense = FLAG_SENSE_REQUIRED
            strongestPos = 2
        elif FLAG_SENSE_PREFERRED in senses:
            posSense = FLAG_SENSE_PREFERRED
            strongestPos = 1
        else:
            strongestPos = 0
            posSense = FLAG_SENSE_UNSPECIFIED

        if FLAG_SENSE_DISALLOWED in senses:
            negSense = FLAG_SENSE_DISALLOWED
            strongestNeg = 2
        elif FLAG_SENSE_PREFERNOT in senses:
            negSense = FLAG_SENSE_PREFERNOT
            strongestNeg = 1
        else:
            strongestNeg = 0
            negSense = FLAG_SENSE_UNSPECIFIED

        if strongestNeg == strongestPos:
            if mergeType == DEP_MERGE_TYPE_DROP_CONFLICTS:
                continue
            if mergeType == DEP_MERGE_TYPE_PREFS:
                for sense in reversed(senses):
                    if sense in (posSense, negSense):
                        finalFlags[flag] = sense
                        break
                continue
            else:
                thisFlag = "%s%s" % (senseMap[negSense], flag)
                otherFlag = "%s%s" % (senseMap[posSense], flag)
                raise RuntimeError("Invalid flag combination in merge:"
                                     " %s and %s"  % (thisFlag, otherFlag))
        elif mergeType == DEP_MERGE_TYPE_PREFS:
            origSense = senses[0]
            if (toStrongMap[origSense] == origSense
                    and FLAG_SENSE_UNSPECIFIED in (posSense, negSense)):
                finalFlags[flag] = origSense
            else:
                finalFlags[flag] = toWeakMap[senses[-1]]
        else:
            finalFlags[flag] = max((strongestPos, posSense),
                                   (strongestNeg, negSense))[1]

    return Dependency(name, finalFlags)



def mergeFlavorList(flavors, mergeType=DEP_MERGE_TYPE_NORMAL):
    for flavor in flavors:
        assert(isinstance(flavor, Flavor))
    finalDep = Flavor()

    depClasses = set()
    for flavor in flavors:
        depClasses.update([ dependencyClasses[x] for x in flavor.getDepClasses()])

    a = finalDep.addDep
    for depClass in depClasses:
        depsByName = {}
        for flavor in flavors:
            if flavor.hasDepClass(depClass):
                for dep in flavor.iterDepsByClass(depClass):
                    depsByName.setdefault(dep.name, []).append(dep)
        for depList in depsByName.values():
            dep = _mergeDeps(depList, mergeType)
            if (depClass.justOne
                and mergeType == DEP_MERGE_TYPE_DROP_CONFLICTS and
                not dep.flags):
                continue
            a(depClass, dep)
    return finalDep



def mergeFlavor(flavor, mergeBase):
    """
    Merges the given flavor with the mergeBase - if flavor
    doesn't contain use flags, then include the mergeBase's
    use flags.  If flavor doesn't contain an instruction set, then
    include the mergeBase's instruction set(s)
    """
    if flavor is None:
        return mergeBase
    if mergeBase is None:
        return flavor
    needsIns = not flavor.hasDepClass(InstructionSetDependency)
    needsUse = not flavor.hasDepClass(UseDependency)
    if not (needsIns or needsUse):
        return flavor

    mergedFlavor = flavor.copy()
    if needsIns:
        insSets = list(mergeBase.iterDepsByClass(InstructionSetDependency))
        if insSets:
            mergedFlavor.addDeps(InstructionSetDependency, insSets)

    if needsUse:
        useSet = list(mergeBase.iterDepsByClass(UseDependency))
        if useSet:
            mergedFlavor.addDeps(UseDependency, useSet)
    return mergedFlavor

def filterFlavor(depSet, filters):
    """
    Returns a DependencySet based on an existing DependencySet object and a list
    of filters (which are themselves DependencySet objects). The new object will
    contain only dependencies which are in one or more of the filters, and the
    flags of those dependencies will be the flags of the object which also appear
    in one or more of the filters. Note that this uses _filterFlavorFlags(),
    and some of the specifics of the results are explained in the documentation
    for that function.

    This is equvalent to ( depSet & [ filter1 | filter 2 | ... | filterN ]) if the
    logical operators are not sense specific.

    @param depSet: Object to filter
    @type depSet: DependencySet
    @param filters: List of objects to filter based on
    @type filters: [ DependencySet ]
    @rtype: DependencySet
    """

    if not isinstance(filters, (list, tuple)):
        filters = [filters]
    finalDepSet = Flavor()
    for depTag, depClass in list(depSet.members.items()):
        # Build a list of the DependencyClasses for this tag in each filter.
        filterClasses = [ x.members.get(depClass.tag, None) for x in filters ]
        filterClasses = [ x for x in filterClasses if x is not None ]
        if not filterClasses:
            # There is no overlap in the classes between this dep and the
            # filters, so we're done. This is just a shortcut; the rest of the
            # logic would noop anyway.
            continue

        depList = []
        for dep in depClass.getDeps():
            # Get all the DependencyClasses from the filters with the same
            # name as this dep
            filterDeps = [ x.members.get(dep.name, None) for x in filterClasses]
            filterDeps = [ x for x in filterDeps if x is not None ]
            if filterDeps:
                finalDep = _filterFlavorFlags(depClass, dep, filterDeps)
                if finalDep is not None:
                    depList.append(finalDep)
        if depList:
            finalDepSet.addDeps(depClass.__class__, depList)
    return finalDepSet

def _filterFlavorFlags(depClass, dep, filterDeps):
    """
    Return a new Dependency object based on an existing Dependency object and
    a list of filters (which are themselves Dependency objects). The new
    object will have the same name as the original Dependency, but only flags
    which are present in both the original and one or more of the filters
    will be present. The sense is not used for matching; the sense from the
    original dependency is used in the return value.

    This is equvalent to ( dep & [ filter1 | filter 2 | ... | filterN ]) if the
    logical operators are not sense specific.

    While this may seem general purpose, it is only used for filtering flavor
    flags, and the name reflects this.

    @param depClass: Class object used to construct the returned dependency. Should
    be a child object of Dependency class.
    @type depClass: class
    @param dep: Object to filter flags from
    @type dep: Dependency
    @param filterDeps: Objects whose flags will be used to filter the dep parameter
    @type filterDeps: [ Dependency ]
    @rtype: Dependency
    """
    filterFlags = set(itertools.chain(*(x.flags for x in filterDeps)))
    finalFlags = [ x for x in dep.flags.items() if x[0] in filterFlags ]
    if not depClass.depNameSignificant and not finalFlags:
        return None
    return Dependency(dep.name, finalFlags)

def getInstructionSetFlavor(flavor):
    if flavor is None:
        return None
    newFlavor = Flavor()
    targetISD = TargetInstructionSetDependency
    ISD = InstructionSetDependency

    # get just the arches, not any arch flags like mmx
    newFlavor.addDeps(ISD,
                      [Dependency(x[1].name) for x in flavor.iterDeps()
                       if x[0] is ISD])
    targetDeps = [ Dependency(x[1].name) for x in flavor.iterDeps()
                  if x[0] is targetISD ]

    if targetDeps:
        newFlavor.addDeps(targetISD, targetDeps)
    return newFlavor

def formatFlavor(flavor):
    """
    Formats a flavor and returns a string which parseFlavor can
    handle.
    """
    def _singleClass(deps):
        l = []
        for dep in deps:
            flags = dep.getFlags()[0]

            if flags:
                flags.sort()
                l.append("%s(%s)" % (dep.getName()[0],
                           ",".join([ "%s%s" % (senseMap[x[1]], x[0])
                                                for x in flags])))
            else:
                l.append(dep.getName()[0])

        l.sort()
        return " ".join(l)

    classes = flavor.getDepClasses()
    insSet = list(flavor.iterDepsByClass(InstructionSetDependency))
    targetSet = list(flavor.iterDepsByClass(TargetInstructionSetDependency))
    useFlags = list(flavor.iterDepsByClass(UseDependency))

    if insSet:
        insSet = _singleClass(insSet)
    if targetSet:
        targetSet = _singleClass(targetSet)

    if useFlags:
        # strip the use() bit
        useFlags = _singleClass(useFlags)[4:-1]

    flavors = []
    if useFlags:
        flavors.append(useFlags)
    if insSet:
        flavors.append('is: %s' % insSet)
    if targetSet:
        flavors.append('target: %s' % targetSet)
    return ' '.join(flavors)

@api.developerApi
def parseFlavor(s, mergeBase = None, raiseError = False):
    # return a Flavor dep set for the string passed. format is
    # [arch[(flag,[flag]*)]] [use:flag[,flag]*]
    #
    # if mergeBase is set, the parsed flavor is merged into it. The
    # rules for the merge are different than those for union() though;
    # the parsed flavor is assumed to set the is:, use:, or both. If
    # either class is unset, it's taken from mergeBase.

    def _fixup(flag):
        flag = flag.strip()
        if flag[0:2] in senseReverseMap:
            sense = senseReverseMap[flag[0:2]]
            flag = flag[2:]
        elif flag[0] in senseReverseMap:
            sense = senseReverseMap[flag[0]]
            flag = flag[1:]
        else:
            sense = FLAG_SENSE_REQUIRED

        return (flag, sense)

    # make it a noop if we get a Flavor object in here
    if isinstance(s, DependencySet):
        return s

    if isinstance(s, str):
        try:
            s = s.encode("ascii")
        except UnicodeEncodeError:
            raise ParseError("invalid characters in flavor '%s'" % s)
    s = s.strip()
    s = s.decode('utf-8')
    match = flavorRegexp.match(s)
    if not match:
        if raiseError:
            raise ParseError("invalid flavor '%s'" % s)
        return None

    groups = match.groups()

    set = Flavor()

    if groups[3]:
        # groups[3] is base instruction set, groups[4] is the flags, and
        # groups[5] is the next instruction set
        # groups[6] is a side effect of the matching groups, but isn't used
        # for anything

        # set up the loop for the next pass
        baseInsSet, insSetFlags, nextGroup, _, _ = groups[3:8]
        while baseInsSet:
            if insSetFlags:
                insSetFlags = insSetFlags.split(",")
                for i, flag in enumerate(insSetFlags):
                    insSetFlags[i] = _fixup(flag)
            else:
                insSetFlags = []

            set.addDep(InstructionSetDependency, Dependency(baseInsSet,
                                                            insSetFlags))

            if not nextGroup:
                break

            match = archGroupRegexp.match(nextGroup)
            # this had to match, or flavorRegexp wouldn't have
            assert(match)
            baseInsSet, insSetFlags, nextGroup, _, _ = match.groups()

    elif groups[2]:
        # mark that the user specified "is:" without any instruction set
        # by adding a placeholder instruction set dep class here.
        set.addEmptyDepClass(InstructionSetDependency)

    # 8 is target: 9 is target architecture.  10 is target flags
    # 11 is the next instruction set.  12 is just a side effect.
    if groups[9]:
        baseInsSet, insSetFlags, nextGroup = groups[9], groups[10], groups[11]
        while baseInsSet:
            if insSetFlags:
                insSetFlags = insSetFlags.split(",")
                for i, flag in enumerate(insSetFlags):
                    insSetFlags[i] = _fixup(flag)
            else:
                insSetFlags = []

            set.addDep(TargetInstructionSetDependency, Dependency(baseInsSet,
                                                                  insSetFlags))
            if not nextGroup:
                break

            match = archGroupRegexp.match(nextGroup)
            # this had to match, or flavorRegexp wouldn't have
            assert(match)
            baseInsSet, insSetFlags, nextGroup, _, _ = match.groups()
    elif groups[8]:
        # mark that the user specified "target:" without any instruction set
        # by adding a placeholder instruction set dep class here.
        set.addEmptyDepClass(TargetInstructionSetDependency)



    if groups[1]:
        useFlags = groups[1].split(",")
        for i, flag in enumerate(useFlags):
            useFlags[i] = _fixup(flag)

        set.addDep(UseDependency, Dependency("use", useFlags))
    elif groups[0]:
        # mark that the user specified "use:" without any instruction set
        # by adding a placeholder instruction set dep class here.
        set.addEmptyDepClass(UseDependency)

    return mergeFlavor(set, mergeBase)

def parseDep(s):
    """
    Parses dependency strings (not flavors) of the format
    (<depClass>: dep[(flags)])* and returns a dependency set
    containing those dependencies.
    Raises ParseError if the parsing fails.
    """

    depSet = DependencySet()
    while s:
        match = depRegexp.match(s)

        if not match:
            raise ParseError('depString starting at %s'
                               ' is not a valid dep string' % s)

        tagName = match.groups()[0]
        depClause = match.groups()[1]
        wholeMatch = match.group()
        s = s[len(wholeMatch):]

        if tagName not in dependencyClassesByName:
            raise ParseError('no such dependency class %s' % tagName)

        depClass = dependencyClassesByName[tagName]

        # depRegexp matches a generic depClass: dep(flags) set
        # - pass the dep to the given depClass for parsing
        dep = depClass.parseDep(depClause)
        assert(dep is not None)
        depSet.addDep(depClass, dep)
    return depSet

def flavorDifferences(flavors, strict=True):
    """ Takes a set of flavors, returns a dict of flavors such that
        the value of a flavor's dict entry is a flavor that includes
        only the information that differentiates that flavor from others
        in the set

        @param strict: if False, ignore differences between flags where the
                       difference is in strength of the flag, but not in
                       direction, e.g. ignore ~foo vs. foo, but not ~foo
                       vs. ~!foo.
    """
    if not flavors:
        return {}

    diffs = {}
    flavors = list(flavors)
    base = flavors[0].copy()
    # the intersection of all the flavors will provide the largest common
    # flavor that is shared between all the flavors given
    for flavor in flavors[1:]:
        base = base.intersection(flavor, strict=strict)
    # remove the common flavor bits
    for flavor in flavors:
        diffs[flavor] = flavor.difference(base, strict=strict)
    return diffs


def compatibleFlavors(flavor1, flavor2):
    """
        Return True if flavor1 does not have any flavor that switches
        polarity from ~foo to ~!foo, or foo to !foo, and flavor1
        does not have any architectures not in flavor2 and vice versa.
    """
    for depClass in list(flavor1.members.values()):
        otherDepClass = flavor2.members.get(depClass.tag, None)
        if otherDepClass is None:
            continue

        for name, dep in depClass.members.items():
            otherDep = otherDepClass.members.get(name, None)
            if otherDep is None:
                if depClass.justOne:
                    continue
                return False
            for flag, sense in dep.flags.items():
                otherSense = otherDep.flags.get(flag, None)
                if otherSense is None:
                    continue
                if toStrongMap[sense] != toStrongMap[otherSense]:
                    return False
    return True

def getMinimalFlagChanges(dep, depToMatch):
    if not dep:
        return [ (flag, FLAG_SENSE_PREFERRED)
                 for (flag,sense) in depToMatch.getFlags()[0]
                 if sense == FLAG_SENSE_REQUIRED ]
    toAdd = []
    for flag, sense in depToMatch.getFlags()[0]:
        mySense = dep.flags.get(flag, FLAG_SENSE_UNSPECIFIED)
        if sense == FLAG_SENSE_REQUIRED:
            # we must provide this flag and it must not be
            # DISALLOWED
            if mySense in (FLAG_SENSE_UNSPECIFIED, FLAG_SENSE_DISALLOWED):
                toAdd.append((flag, FLAG_SENSE_PREFERRED))
        elif sense == FLAG_SENSE_PREFERRED:
            if mySense == FLAG_SENSE_DISALLOWED:
                toAdd.append((flag, FLAG_SENSE_PREFERRED))
        elif sense == FLAG_SENSE_PREFERNOT:
            if mySense == FLAG_SENSE_REQUIRED:
                toAdd.append((flag, FLAG_SENSE_PREFERNOT))
        elif sense == FLAG_SENSE_DISALLOWED:
            if mySense in (FLAG_SENSE_PREFERRED, FLAG_SENSE_REQUIRED):
                toAdd.append((flag, FLAG_SENSE_PREFERNOT))
    return toAdd


def getMinimalCompatibleChanges(flavor, flavorToMatch, keepArch=False):
    useFlags = list(flavorToMatch.iterDepsByClass(UseDependency))
    insDeps = list(flavorToMatch.iterDepsByClass(InstructionSetDependency))
    targetDeps = list(flavorToMatch.iterDepsByClass(
                                            TargetInstructionSetDependency))
    myUseFlags = list(flavor.iterDepsByClass(UseDependency))
    myInsDeps = list(flavor.iterDepsByClass(InstructionSetDependency))
    myTargetDeps = list(flavor.iterDepsByClass(
                                          TargetInstructionSetDependency))
    finalFlavor = Flavor()
    if useFlags:
        useFlags = useFlags[0]
        if myUseFlags:
            myUseFlags = myUseFlags[0]
        flagsNeeded = getMinimalFlagChanges(myUseFlags, useFlags)
        if flagsNeeded:
            useDep = Dependency('use', flagsNeeded)
            finalFlavor.addDep(UseDependency, useDep)
    for (depClass, toMatchDeps, myDeps) in ((InstructionSetDependency,
                                             insDeps, myInsDeps),
                                            (TargetInstructionSetDependency,
                                             targetDeps, myTargetDeps)):
        myDeps = dict((x.name, x) for x in myDeps)
        for dep in toMatchDeps:
            myDep = myDeps.get(dep.name, None)
            flagsNeeded = getMinimalFlagChanges(myDep, dep)
            if myDep is None or flagsNeeded or keepArch:
                insDep = Dependency(dep.name, flagsNeeded)
                finalFlavor.addDep(depClass, insDep)
    return finalFlavor

def getUseFlags(flavor):
    deps = list(flavor.iterDepsByClass(UseDependency))
    if not deps:
        return {}
    return deps[0].getFlags()[0]

@api.developerApi
def getMajorArch(flavor):
    from conary.deps import arch
    majorArch = arch.getMajorArch(
                    flavor.iterDepsByClass(InstructionSetDependency))
    if majorArch:
        return majorArch.name

@api.developerApi
def getShortFlavorDescriptors(flavors):
    contextStr = {}
    descriptors = {}
    for flavor in flavors:
        majorArch = getMajorArch(flavor)
        if majorArch:
            descriptors[flavor] = (majorArch,)
        else:
            descriptors[flavor] = ()
    if len(set(descriptors.values())) != len(descriptors):
        differences = flavorDifferences(flavors, strict=False)
        for flavor, shortenedFlavor in differences.items():
            useFlags = getUseFlags(shortenedFlavor)
            positiveFlags = sorted(x[0] for x in useFlags
                                    if x[1] in (FLAG_SENSE_PREFERRED,
                                                FLAG_SENSE_REQUIRED))
            descriptors[flavor] = descriptors[flavor] + tuple(positiveFlags)
    if len(set(descriptors.values())) != len(descriptors):
        # at this point the only differences are between
        # prefers + requires and prefernot and requirenot and missing
        differences = flavorDifferences(flavors, strict=True)
        for flavor, shortenedFlavor in differences.items():
            majorArch = getMajorArch(flavor)
            veryShortFlavor = Flavor()
            useFlags = list(shortenedFlavor.iterDepsByClass(UseDependency))
            if useFlags:
                veryShortFlavor.addDeps(UseDependency, useFlags)
            if majorArch:
                veryShortFlavor.addDep(InstructionSetDependency,
                                        Dependency(majorArch))
            descriptors[flavor] = tuple(str(veryShortFlavor).split(','))
    if len(set(descriptors.values())) == len(set(descriptors)):
        return dict((x[0], '-'.join(x[1])) for x in descriptors.items())

    raise NotImplementedError


class DependencyMatcher(object):

    def __init__(self, ignoreDepClasses=()):
        self.ignoreDepClasses = set()
        for depClass in ignoreDepClasses:
            if not isinstance(depClass, int):
                depClass = depClass.tag
            self.ignoreDepClasses.add(depClass)
        self.depMap = {}

    def add(self, depSet, data=None):
        for depClassId, depName, depFlags in depSet.iterRawDeps():
            if depClassId in self.ignoreDepClasses:
                continue
            depValue = (depClassId, depFlags, data)
            values = self.depMap.get(depName)
            if values is not None:
                values.append(depValue)
            else:
                self.depMap[depName] = [depValue]

    def find(self, depSet):
        setMatches = []
        for depClassId, depName, depFlags in depSet.iterRawDeps():
            depMatches = []
            for provClassId, provFlags, provData in self.depMap.get(depName, ()):
                if provClassId != depClassId:
                    continue
                if provFlags == depFlags:
                    depMatches.append(provData)
                    continue
                for depFlag in depFlags:
                    assert depFlag[0] not in '~!'
                    if depFlag not in provFlags:
                        break
                else:
                    depMatches.append(provData)
            setMatches.append(depMatches)
        return setMatches

    def check(self, depSet):
        unsatisfied = None
        for depClassId, depName, depFlags in depSet.iterRawDeps():
            if depClassId in self.ignoreDepClasses:
                continue
            found = False
            for provClassId, provFlags, provData in self.depMap.get(depName, ()):
                if provClassId != depClassId:
                    continue
                if provFlags == depFlags:
                    found = True
                    break
                for depFlag in depFlags:
                    assert depFlag[0] not in '~!'
                    if depFlag not in provFlags:
                        break
                else:
                    found = True
                    break
            if not found:
                if unsatisfied is None:
                    unsatisfied = DependencySet()
                depClass = dependencyClasses[depClassId]
                dep = DependencyClass.thawRawDep(depName, depFlags)
                unsatisfied.addDep(depClass, dep)
        return unsatisfied

    def clear(self):
        self.depMap.clear()


dependencyCache = weakref.WeakValueDictionary()

ident = '(?:[0-9A-Za-z_-]+)'
flag = '(?:~?!?IDENT)'
useFlag = '(?:!|~!)?FLAG(?:\.IDENT)?'
archFlags = '\(( *FLAG(?: *, *FLAG)*)\)'
archClause = ' *(?:(IDENT)(?:ARCHFLAGS)?)?'
archGroup = '(?:ARCHCLAUSE(?:((?:  *ARCHCLAUSE)*))?)'
useClause = '(USEFLAG *(?:, *USEFLAG)*)?'


depFlags = ' *(?:\([^)]*\))? *' # anything inside parens
depName = r'(?:[^ (]+)' # anything except for a space or an opening paren
depClause = depName + depFlags
depRegexpStr = r'(IDENT): *(DEPCLAUSE) *'

flavorRegexpStr = '^(use:)? *(?:USECLAUSE)? *(?:(is:) *ARCHGROUP)? *(?:(target:) *ARCHGROUP)?$'

flavorRegexpStr = flavorRegexpStr.replace('ARCHGROUP', archGroup)
flavorRegexpStr = flavorRegexpStr.replace('ARCHCLAUSE', archClause)
flavorRegexpStr = flavorRegexpStr.replace('ARCHFLAGS', archFlags)
flavorRegexpStr = flavorRegexpStr.replace('USECLAUSE', useClause)
flavorRegexpStr = flavorRegexpStr.replace('USEFLAG', useFlag)
flavorRegexpStr = flavorRegexpStr.replace('FLAG', flag)
flavorRegexpStr = flavorRegexpStr.replace('IDENT', ident)
flavorRegexp = re.compile(flavorRegexpStr)

archGroupStr = archGroup.replace('ARCHCLAUSE', archClause)
archGroupStr = archGroupStr.replace('ARCHFLAGS', archFlags)
archGroupStr = archGroupStr.replace('USECLAUSE', useClause)
archGroupStr = archGroupStr.replace('USEFLAG', useFlag)
archGroupStr = archGroupStr.replace('FLAG', flag)
archGroupStr = archGroupStr.replace('IDENT', ident)
archGroupRegexp = re.compile(archGroupStr)

depRegexpStr = depRegexpStr.replace('DEPCLAUSE', depClause)
depRegexpStr = depRegexpStr.replace('IDENT', ident)
depRegexp = re.compile(depRegexpStr)


del ident, flag, useFlag, archClause, useClause, flavorRegexpStr
del depFlags, depName, depClause, depRegexpStr
del archGroupStr

# None means disallowed match
flavorScores = {
      (FLAG_SENSE_UNSPECIFIED, FLAG_SENSE_REQUIRED ) : None,
      (FLAG_SENSE_UNSPECIFIED, FLAG_SENSE_DISALLOWED):    0,
      (FLAG_SENSE_UNSPECIFIED, FLAG_SENSE_PREFERRED) :   -1,
      (FLAG_SENSE_UNSPECIFIED, FLAG_SENSE_PREFERNOT) :    1,

      (FLAG_SENSE_REQUIRED,    FLAG_SENSE_REQUIRED ) :    2,
      (FLAG_SENSE_REQUIRED,    FLAG_SENSE_DISALLOWED): None,
      (FLAG_SENSE_REQUIRED,    FLAG_SENSE_PREFERRED) :    1,
      (FLAG_SENSE_REQUIRED,    FLAG_SENSE_PREFERNOT) : None,

      (FLAG_SENSE_DISALLOWED,  FLAG_SENSE_REQUIRED ) : None,
      (FLAG_SENSE_DISALLOWED,  FLAG_SENSE_DISALLOWED):    2,
      (FLAG_SENSE_DISALLOWED,  FLAG_SENSE_PREFERRED) : None,
      (FLAG_SENSE_DISALLOWED,  FLAG_SENSE_PREFERNOT) :    1,

      (FLAG_SENSE_PREFERRED,   FLAG_SENSE_REQUIRED ) :    1,
      (FLAG_SENSE_PREFERRED,   FLAG_SENSE_DISALLOWED): None,
      (FLAG_SENSE_PREFERRED,   FLAG_SENSE_PREFERRED) :    2,
      (FLAG_SENSE_PREFERRED,   FLAG_SENSE_PREFERNOT) :   -1,

      (FLAG_SENSE_PREFERNOT,   FLAG_SENSE_REQUIRED ) :   -2,
      (FLAG_SENSE_PREFERNOT,   FLAG_SENSE_DISALLOWED):    1,
      (FLAG_SENSE_PREFERNOT,   FLAG_SENSE_PREFERRED) :   -1,
      (FLAG_SENSE_PREFERNOT,   FLAG_SENSE_PREFERNOT) :    1
}
