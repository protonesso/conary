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


from conary.errors import CvcError
from conary.lib import log

class RecipeFileError(CvcError):
    pass

class CookError(CvcError):
    pass

class LoadRecipeError(RecipeFileError):
    pass

class RecipeDependencyError(RecipeFileError):
    pass

class BadRecipeNameError(RecipeFileError):
    pass

class GroupPathConflicts(CookError):
    def __init__(self, conflicts, groupDict):
        self.conflicts = conflicts
        self.groupDict = groupDict
        errStrings = []
        for groupName, conflictSets in conflicts.items():
            group = groupDict[groupName]
            errStrings.append('%s:' % groupName)
            for conflictSet, paths in conflictSets:
                errStrings.append('  The following %s troves share %s conflicting paths:' % (len(conflictSet), len(paths)))
                errStrings.append('\n    Troves:')
                for (n,v,f) in conflictSet:
                    incReason = group.getReasonString(n,v,f)
                    errStrings.append('     %s=%s[%s]\n       (%s)' % (n,v,f,incReason))
                errStrings.append('\n    Conflicting Files:')
                errStrings.extend('      %s' % x for x in sorted(paths)[0:11])
                if len(paths) > 10:
                    errStrings.append('      ... (%s more)' % (len(paths) - 10))
                errStrings.append('')

        # CNY-3079: self.args has to be an array or tuple
        self.args = ("""
The following troves in the following groups have conflicts:

%s""" % ('\n'.join(errStrings)), )

class GroupDependencyFailure(CookError):
    def __init__(self, groupName, failedDeps):
        lns = ["Dependency failure\n"]
        lns.append("Group %s has unresolved dependencies:" % groupName)
        for (name, depSet) in failedDeps:
            lns.append("\n" + name[0])
            lns.append('\n\t')
            lns.append("\n\t".join(str(depSet).split("\n")))
        self.args = (''.join(lns),)


class GroupCyclesError(CookError):
    def __init__(self, cycles):
        lns = ['cycle in groups:']
        lns.extend(str(sorted(x)) for x in cycles)
        self.args = ('\n  '.join(lns),)

class GroupAddAllError(CookError):
    def __init__(self, parentGroup, troveTup, groupTups ):
        groupNames = [ x[0] for x in groupTups ]
        repeatedGroups = sorted(set(x for x in groupNames \
                                                if groupNames.count(x) > 1))

        repeatedGroups = "'" + "', '".join(repeatedGroups) + "'"

        lns = ['Cannot recursively addAll from group "%s":' % troveTup[0]]
        lns.append('Multiple groups with the same name(s) %s' % repeatedGroups)
        lns.append('are included.')

        self.args = ('\n  '.join(lns),)

class GroupImplicitReplaceError(CookError):
    def __init__(self, parentGroup, troveTups):
        lns = ['Cannot replace the following troves in %s:\n\n' % parentGroup.name]
        for troveTup in troveTups:
            lns.append('   %s=%s[%s]\n' % troveTup)
            lns.append('   (%s)\n' % parentGroup.getReasonString(*troveTup))
        lns.append('\nYou are not building the containing group, so conary does not know where to add the replacement.\n')
        lns.append('To resolve this problem, use r.addCopy for the containing group instead of r.add.\n')
        self.args = (''.join(lns),)

class _UnmatchedSpecs(CookError):
    def __init__(self, msg, troveSpecs):
        lns = [msg]
        for troveSpec in troveSpecs:
            ver = flavor = ''
            if troveSpec[1]:
                ver = '=%s' % troveSpec[1]
            if troveSpec[2] is not None:
                flavor = '[%s]' % troveSpec[2]
            lns.append('    %s%s%s\n' % (troveSpec[0], ver, flavor))
        self.args = (''.join(lns),)

class GroupUnmatchedRemoves(_UnmatchedSpecs):
    def __init__(self, troveSpecs, group):
        msg = 'Could not find troves to remove in %s:\n' % group.name
        _UnmatchedSpecs.__init__(self, msg, troveSpecs)

class GroupUnmatchedReplaces(_UnmatchedSpecs):
    def __init__(self, troveSpecs, group):
        msg = 'Could not find troves to replace in %s:\n' % group.name
        _UnmatchedSpecs.__init__(self, msg, troveSpecs)

class GroupUnmatchedGlobalReplaces(_UnmatchedSpecs):
    def __init__(self, troveSpecs):
        msg = 'Could not find troves to replace in any group:\n'
        _UnmatchedSpecs.__init__(self, msg, troveSpecs)

class GroupFlavorChangedError(CookError):
    pass

class MacroKeyError(KeyError):
    def __str__(self):
        return 'Unknown macro "%s" - check for spelling mistakes' % self.args[0]

class MirrorError(CvcError):
    pass

class CheckinError(CvcError):
    'Checkin Error'
    def __init__(self, *a):
        cls = self.__class__
        self.msg = cls.__doc__ % a
        CvcError.__init__(self, *a)

    def __str__(self):
        return self.msg

    def _log(self, logger):
        logger(str(self))

    def logError(self): self._log(log.error)
    def logInfo(self): self._log(log.info)
    #def logWarning(self): self._log(log.warning) # Not currently used
    #def logDebug(self): self._log(log.debug) # Not currently used

class CheckinErrorList(CheckinError):
    '''The followin errors occurred:\n'''
    def __init__(self, errlist):
        self.errlist = errlist
        assert isinstance(errlist, (tuple, list)), 'Invalid arguments for %s' % self.__name__
        assert len(errlist) > 0, 'This exception should not be raised with an empty list'
        CheckinError.__init__(self)

    def _log(self, logger):
        for x in self.errlist:
            logger(x)

class UpToDate(CheckinError):
    'working directory %s is already based on head of branch'

class NotCheckedInError(CheckinError):
    "cannot update source directory for package '%s' - it was created with newpkg and has never been checked in."

class MultipleSourceVersions(CheckinError):
    "%s specifies multiple versions"

class NoSuchSourceVersion(CheckinError):
    "unable to find source component %s with version %s"

class NoSourceTroveFound(CheckinError):
    "cannot find source trove: %s"
