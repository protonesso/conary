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


"""
Base classes and data used for all policy
"""

import imp
import itertools
import os
import sys
import types

from conary.lib import util, log, graph, sha1helper
from conary.build import action, errors, filter, trovefilter

from conary.build.action import TARGET_LINUX
from conary.build.action import TARGET_WINDOWS

# buckets (enum -- but may possibly work someday as bitmask for policy
# that could run more than once in different contexts)
TESTSUITE            = 1 << 0
DESTDIR_PREPARATION  = 1 << 1
DESTDIR_MODIFICATION = 1 << 2
PACKAGE_CREATION     = 1 << 3
PACKAGE_MODIFICATION = 1 << 4
ENFORCEMENT          = 1 << 5
ERROR_REPORTING      = 1 << 6
GROUP_ENFORCEMENT    = 1 << 7

# requirements (sparse bitmask, 5 sensible states)
REQUIRED               = 1 << 0
ORDERED                = 1 << 1
PRIOR                  = 1 << 2
REQUIRED_PRIOR         = REQUIRED|ORDERED|PRIOR
REQUIRED_SUBSEQUENT    = REQUIRED|ORDERED
CONDITIONAL_PRIOR      = ORDERED|PRIOR
CONDITIONAL_SUBSEQUENT = ORDERED

# file trees
NO_FILES         = 0 << 0
DESTDIR          = 1 << 0
BUILDDIR         = 1 << 1
PACKAGE          = 1 << 2
CAPSULESCRIPTDIR = 1 << 3
DIR              = DESTDIR|BUILDDIR|CAPSULESCRIPTDIR


class BasePolicy(action.RecipeAction):
    """
    Abstract Superclass for all policy actions. Common bits between Policy
    and GroupPolicy should be defined at this level
    """
    invariantsubtrees = []
    invariantexceptions = []
    invariantinclusions = []
    allowUnusedFilters = False

    def __init__(self, *args, **kwargs):
        self.unusedFilters = {'inclusions' : set(), 'exceptions' : set()}
        action.RecipeAction.__init__(self, *args, **kwargs)

    def postInit(self):
        """
        Hook for initialization that cannot happen until after all
        policies have been loaded into the recipe and thus cannot
        happen in __init__; mainly for policies that need to pass
        information to other policies at initialization time.
        """
        pass

    def updateArgs(self, *args, **keywords):
        """
        The default way to update a class is to override any provided
        keywords.  Subclasses which have the ability to provide more
        intelligent handling can override this method.  This method
        is invoked automatically by recipe.py when a recipe references
        a policy object.  It acts rather like __init__ except that it
        can meaningfully be called more than once for an object.

        Some keyword arguments (at least C{exceptions} and C{subtrees})
        should be appended rather than replaced.
        """
        allowUnusedFilters = keywords.pop('allowUnusedFilters', False) or \
                self.allowUnusedFilters

        exceptions = keywords.pop('exceptions', None)
        if exceptions:
            if not self.exceptions:
                self.exceptions = []
            if type(exceptions) in (list, set): # Not tuple: CNY-3437
                self.exceptions.extend(exceptions)
                if not allowUnusedFilters:
                    for item in exceptions:
                        if not callable(item):
                            self.unusedFilters['exceptions'].add(item)
            else:
                self.exceptions.append(exceptions) # PathSet here
                if not allowUnusedFilters and not callable(exceptions):
                    self.unusedFilters['exceptions'].add(exceptions)
        subtrees = keywords.pop('subtrees', None)
        if subtrees:
            if not self.subtrees:
                self.subtrees = []
            if type(subtrees) in (list, tuple):
                self.subtrees.extend(subtrees)
            else:
                self.subtrees.append(subtrees)

        inclusions = keywords.pop('inclusions', [])
        if (args or inclusions) and not self.inclusions:
            self.inclusions = []

        if inclusions:
            if isinstance(inclusions, (list, set)): # Not tuple: CNY-3437
                self.inclusions.extend(inclusions)
                if not allowUnusedFilters:
                    for item in inclusions:
                        if not callable(item):
                            self.unusedFilters['inclusions'].add(item)
            else:
                self.inclusions.append(inclusions) # PathSet here
                if not allowUnusedFilters and not callable(inclusions):
                    self.unusedFilters['inclusions'].add(inclusions)

        if args:
            self.inclusions.extend(args)
            if not allowUnusedFilters:
                for item in args:
                    if not callable(item):
                        self.unusedFilters['inclusions'].add(item)

        self.addArgs(**keywords)

    def _pathAllowed(self, path):
        if self.policyInclusion(path) and not self.policyException(path):
            return True
        return False

    def postPolicy(self):
        if self.unusedFilters['exceptions']:
            for filter in self.unusedFilters['exceptions']:
                self.warn('Exception %s for %s was not used' % \
                        (filter, self.__class__.__name__))
        if self.unusedFilters['inclusions']:
            for filter in self.unusedFilters['inclusions']:
                self.warn('Inclusion %s for %s was not used' % \
                        (filter, self.__class__.__name__))

    # warning and error reporting

    def _addClassName(self, args):
        args = list(args)
        args[0] = ': '.join((self.__class__.__name__, args[0]))
        return args

    def dbg(self, *args, **kwargs):
        args = self._addClassName(args)
        log.debug(*args, **kwargs)

    def info(self, *args, **kwargs):
        args = self._addClassName(args)
        log.info(*args, **kwargs)

    def warn(self, *args, **kwargs):
        args = self._addClassName(args)
        log.warning(*args, **kwargs)

    def error(self, *args, **kwargs):
        args = self._addClassName(args)
        log.error(*args, **kwargs)
        self.recipe.reportErrors(*args, **kwargs)

class Policy(BasePolicy):
    """
    Abstract superclass for all policy actions.  Policy actions
    that operate on the entire C{%(destdir)s} implement the C{do} method;
    Policy actions that operate on a per-file basis implement the
    C{doFile} method.  The C{doFile} function is never called for files
    that match an exception regexp; C{do} functions must implement
    their own exception regexp handling.

    The class variables below apply automatically to the C{doFile}
    method; classes implementing the C{do} method instead may use them as
    well, but should use the same rules if they do use them.  All of
    them have C{self.macros} applied before use.

    @cvar invariantsubtrees: if C{invariantsubtrees} is not empty,
    then it is a list of subtrees (relative to C{%(destdir)s}) to
    walk INSTEAD of walking the entire C{%(destdir)s} tree.  Any
    C{subtrees} are appended to C{invariantsubtrees}.

    @cvar invariantinclusions: if C{invariantinclusions} is not empty,
    then only files matching a filter in it are considered to be passed
    to to the C{doFile} method.  Any exceptions, including invariants,
    are applied after C{invariantinclusions} are applied; this means
    that all exceptions OVERRULE every type of inclusion.  If
    C{invariantinclusions} is None, then the policy applies to no
    files by default, but will apply to specified files.

    @cvar invariantexceptions: subclasses may set to a list of
    exception filters that are always applied regardless of what other
    exceptions may be provided by the recipe; these exceptions being
    applied is an invariant condition of the C{doFile} method.

    @cvar recursive: if True, walk entire subtrees; if False,
    work only on contents of listed directories (C{invariantsubtrees}
    and C{subtrees}).
    @type recursive: boolean

    @cvar filetree: where to look for files to which to apply the
    policy: C{policy.DESTDIR}, the default, walks the files in the
    C{destdir}, C{policy.BUILDDIR} walks the files in the build
    directory, and C{policy.PACKAGE} iterates over the packaged
    files rather than walking the destdir, which besides being
    possibly faster also applies to files that are not on the
    filesystem (like device nodes).  C{policy.CAPSULESCRIPTDIR}
    iterates over capsule script files, which are not packaged
    as files but are made available for inspection in policies.

    @cvar rootdir: The root of the tree to walk for files, normally
    implied by the setting of filetree.

    @cvar processUnmodified: allows special handling for derived
    packages in order to make only appropriate changes in the
    derived package.  C{None} (default) disables the policy for
    derived packages.  C{True} causes C{doFile} to be called for
    all files found, regardless of whether they are in the
    parent version, and C{False} causes C{doFile} to be called
    only for files that are new or have changed.
    Note that if C{filetree} is C{policy.PACKAGE}, unchanged
    file contents will lead to unchanged timestamps.
    """
    bucket = None
    recursive = True
    filetree = DESTDIR
    rootdir = None
    processUnmodified = None

    keywords = {
        'use': None,
        'exceptions': None,
        'inclusions': None,
        'subtrees': None,
    }

    def __init__(self, recipe, **keywords):
        """
        @keyword exceptions: Optional argument; regexp(s) specifying
        files to ignore while taking the policy action.  It will be
        interpolated against recipe macros before being used.
        @type exceptions: None, filter string/tuple, or
        tuple/list of filter strings/tuples
        @keyword use: Optional argument; Use flag(s) telling whether
        to actually perform the action.
        @type use: None, Use flag, or tuple/list of Use flags
        @keyword subtree: Subtree to which to limit the policy, or it
        it already is limited (invariantsubtrees), then additional
        subtrees to consider.
        @type subtree: string or sequence of strings
        @keyword inclusions: C{FileFilter}s to which to limit the policy,
        or if it already is limited (invariantinclusion) then additional
        C{FileFilter}s to include within the general limitation.
        @type inclusions: C{FileFilter} strings, C{FileFilter} tuples,
        or list (not tuple) of C{FileFilter} strings or C{FileFilter} tuples.
        """
        # enforce abstract base class status
        assert(self.__class__ is not Policy)

        BasePolicy.__init__(self, None, [], **keywords)
        self.recipe = recipe
        if self.rootdir is None and self.filetree:
            self.rootdir = {
                DESTDIR: '%(destdir)s',
                BUILDDIR: '%(builddir)s',
                CAPSULESCRIPTDIR: '%(destdir)s/../_CAPSULE_SCRIPTS_',
                PACKAGE: '',
            }[self.filetree]

    def filterExpression(self, expression, name=None):
        """
        backwards compatibility
        """
        args, kwargs = self.filterExpArgs(expression, name)
        return (args[0],
                args[1],
                kwargs['setmode'],
                kwargs['unsetmode'],
                kwargs['name'],
                kwargs['rootdir'])

    def filterExpArgs(self, expression, name=None):
        """
        @param expression: regular expression or tuple of
        (regex, [setmode, [unsetmode]])
        Creates arguments to filter.Filter.__init__
        """
        kwargs = {
            'name': name,
            'setmode': None,
            'unsetmode': None,
            'rootdir': self.rootdir,
        }
        macros = self.macros
        if isinstance(expression, (str, types.FunctionType)) or callable(
                expression):
            return (expression, macros), kwargs

        if not isinstance(expression, list):
            expression = list(expression)

        # this normally happens when code at a higher level
        # has a filterExp tuple in a list of items and does
        # not need to know how to handle it separately.
        if len(expression) == 1 and type(expression[0]) in (list, tuple):
            expression = list(expression[0])

        regex = expression.pop(0)
        if expression:
            kwargs['setmode'] = expression.pop(0)
        if expression:
            kwargs['unsetmode'] = expression.pop(0)
        return (regex, macros), kwargs

    def compileFilters(self, expressionList, filterList, unusedList=None):
        seen = []
        newUnused = []
        for expression in expressionList:
            if expression in seen:
                # only put each expression on the list once
                continue
            if isinstance(expression, action.Glob):
                expression = expression()
            elif isinstance(expression, action.Regexp):
                expression = expression.pattern
            seen.append(expression)
            args, kwargs = self.filterExpArgs(expression)
            f = filter.Filter(*args, **kwargs)
            if unusedList and expression in unusedList:
                unusedList.discard(expression)
                newUnused.append(f.regexp)
            filterList.append(f)
        if unusedList is not None:
            unusedList.update(newUnused)

    def doProcess(self, recipe):
        """
        Invocation instance
        @param recipe: holds the recipe object, which is used for
        the macro set and package objects.
        @return: None
        @rtype: None
        """
        self.recipe = recipe
        self.macros = recipe.macros

        if not self._isSupportedTarget():
            return

        if self.rootdir:
            self.rootdir = util.normpath(self.rootdir % self.macros)

        if (hasattr(recipe, '_isDerived')
            and recipe._isDerived == True
            and self.processUnmodified is None):
            # This policy does not handle derived packages
            return

        if hasattr(self.__class__, 'preProcess'):
            self.preProcess()

        # is runtime check implemented?
        if hasattr(self.__class__, 'test'):
            if not self.test():
                return

        # change self.use to be a simple flag
        self.use = action.checkUse(self.use)

        # compile the exceptions
        self.exceptionFilters = []
        self.compileFilters(self.invariantexceptions, self.exceptionFilters)
        if self.exceptions:
            if not isinstance(self.exceptions, (tuple, list, set)):
                # turn a plain string into a sequence
                self.exceptions = (self.exceptions,)
            self.compileFilters(self.exceptions, self.exceptionFilters, self.unusedFilters['exceptions'])

        # compile the inclusions
        self.inclusionFilters = []
        if self.invariantinclusions is None:
            self.compileFilters([], self.inclusionFilters)
        else:
            self.compileFilters(self.invariantinclusions, self.inclusionFilters)
        if not self.inclusions:
            # an empty list, as opposed to None, means nothing is included
            if isinstance(self.inclusions, (tuple, list, set)):
                return
        else:
            if not isinstance(self.inclusions, (tuple, list, set)):
                # turn a plain string into a sequence
                self.inclusions = (self.inclusions,)
            self.compileFilters(self.inclusions, self.inclusionFilters, self.unusedFilters['inclusions'])

        # dispatch if/as appropriate
        if self.use:
            self.do()

        if hasattr(self.__class__, 'postProcess'):
            self.postProcess()

    def do(self):
        # calls doFile on all appropriate files -- can be overridden by
        # subclasses
        if not self.filetree:
            return

        if self.filetree & PACKAGE:
            pkg = self.recipe.autopkg
            for thispath in sorted(pkg.pathMap):
                if self._pathAllowed(thispath):
                    self.doFile(thispath)
            return

        assert(self.filetree & DIR)
        if self.subtrees:
            self.invariantsubtrees.extend(self.subtrees)
        if not self.invariantsubtrees:
            self.invariantsubtrees.append('/')
        for self.currentsubtree in self.invariantsubtrees:
            fullpath = (self.rootdir+self.currentsubtree) %self.macros
            dirs = util.braceGlob(fullpath)
            for d in dirs:
                if self.recursive:
                    os.path.walk(d, self.walkDir, None)
                else:
                    # only one level
                    if os.path.isdir(d):
                        self.walkDir(None, d, os.listdir(d))

    def walkDir(self, ignore, dirname, names):
        # chop off bit not useful for comparison
        rootdirlen = len(self.rootdir)
        path=dirname[rootdirlen:]
        for name in names:
           thispath = util.normpath(path + os.sep + name)
           if self._pathAllowed(thispath):
               self.doFile(thispath)

    FILE_MISSING = -1
    FILE_UNCHANGED = 0
    FILE_CHANGED = 1
    FILE_NEW = 2
    def fileChanged(self, path):
        """
        check to see if the file has changed
        @param path: the path to check
        @return: FILE_MISSING, FILE_CHANGED, FILE_UNCHANGED, FILE_NEW
        @rtype: int
        """
        newPath = util.joinPaths(self.macros.destdir, path)
        if not util.exists(newPath):
            return self.FILE_MISSING

        from conary.build.recipe import RECIPE_TYPE_CAPSULE
        if self.recipe._recipeType == RECIPE_TYPE_CAPSULE:
            if not os.path.isfile(newPath):
                # for capsules we get everything but contents from
                # the capsule header
                return self.FILE_UNCHANGED

            # For derived capsule recipes we use the exploder to provide the old
            # sha1. For regular capsule recipes we use the capsuleFileSha1s map
            # to provide the old sha1.
            oldSha1=None
            if os.path.islink(newPath):
                oldSha1 = os.readlink(newPath)
            elif hasattr(self.recipe,'exploder'):
                oldf = self.recipe.exploder.fileObjMap.get(path,None)
                if oldf and oldf.hasContents:
                    oldSha1 = oldf.contents.sha1()
            else:
                capPaths = self.recipe._getCapsulePathsForFile(path)
                if not capPaths:
                    return self.FILE_NEW
                oldSha1 = self.recipe.capsuleFileSha1s[capPaths[0]][path]

            if oldSha1:
                if os.path.islink(newPath):
                    newSha1 = os.readlink(newPath)
                else:
                    newSha1 = sha1helper.sha1FileBin(newPath)
                if oldSha1 == newSha1:
                    return self.FILE_UNCHANGED
                return self.FILE_CHANGED
            return self.FILE_NEW

        oldMtime = self.recipe._derivedFiles.get(path, None)
        if os.path.islink(newPath):
            # symlinks are special, we compare the target of the link
            # instead of the mtime
            newMtime = os.readlink(newPath)
        else:
            newMtime = os.lstat(newPath).st_mtime
        if oldMtime:
            if oldMtime == newMtime:
                return self.FILE_UNCHANGED
            return self.FILE_CHANGED
        return self.FILE_NEW
    # for backwards compatiblity
    mtimeChanged=fileChanged

    def policyInclusion(self, filespec):
        if (hasattr(self.recipe, '_isDerived')
            and self.recipe._isDerived == True
            and self.processUnmodified is False
            and filespec in self.recipe._derivedFiles
            and not self.fileChanged(filespec)):
            # policy has elected not to handle unchanged files
            return False
        if not self.inclusions and self.invariantinclusions is None:
            # policy applies to nothing by default, and no files
            # have been specified
            return False
        if not self.inclusionFilters:
            # empty list is '.*'
            return True
        res = False
        for f in self.inclusionFilters:
            # We can't short circuit. We must discard all valid inclusions
            # that are specified by regular expressions.
            # It's possible that an invariant inclusion will match before
            # an explicit inclusion, and we need to distinguish between
            # "filter was unused because no paths matched" and
            # "filter was unused because it's redundant", because erroring
            # on the second case is confusing rather than helpful.
            if f.match(filespec):
                if hasattr(f, 'regexp'):
                    self.unusedFilters['inclusions'].discard(f.regexp)
                res = True
        return res

    def policyException(self, filespec):
        res = False
        for f in self.exceptionFilters:
            # We can't short circuit. We must discard all valid exceptions.
            if f.match(filespec):
                if hasattr(f, 'regexp'):
                    self.unusedFilters['exceptions'].discard(f.regexp)
                res = True
        return res

class UserGroupBasePolicy(Policy):
    def updateArgs(self, *args, **kwargs):
        self.error("Do not directly invoke %s" % self.__class__.__name__)

class GroupPolicy(BasePolicy):
    keywords = {
        'exceptions': None,
        'inclusions': None,
    }
    supported_targets = (TARGET_LINUX, TARGET_WINDOWS)

    def __init__(self, recipe, **keywords):
        """
        @keyword exceptions: Optional argument; regexp(s) specifying
        troves to ignore while taking the policy action.  It will be
        interpolated against recipe macros before being used.
        @type exceptions: None, filter string/tuple/C{TroveFilter}s,
        tuple/list of filter strings/tuples/C{TroveFilter}s.
        @keyword inclusions: C{TroveFilter}s to which to limit the policy,
        or if it already is limited (invariantinclusion) then additional
        C{TroveFilter}s to include within the general limitation.
        @type inclusions: Filter strings/C{TroveFilter}s or list (not tuple)
        of filter strings or C{TroveFilter}s.
        """
        # enforce abstract base class status
        assert(self.__class__ is not GroupPolicy)

        BasePolicy.__init__(self, None, [], **keywords)
        self.recipe = recipe

    def policyInclusion(self, filespec):
        if not self.inclusionFilters:
            # empty list is '.*'
            return True
        res = False
        for f in self.inclusionFilters:
            if f.match(filespec):
                self.unusedFilters['inclusions'].discard(f)
                res = True
        return res

    def policyException(self, filespec):
        res = False
        for f in self.exceptionFilters:
            # we can't short circuit. we must discard all valid exceptions.
            if f.match(filespec):
                self.unusedFilters['exceptions'].discard(f)
                res = True
        return res

    def formatTrovePath(self, path):
        groupName = [x[0] for x in reversed(path[:-1]) \
                if x[0] in self.recipe.groups][0]
        grp = self.recipe.groups[groupName]
        res = ""
        indent = 0
        res += "%s=%s[%s]: (%s)\n" % ( path[-1][0], path[-1][1], path[-1][2],
                grp.getReasonString(*path[-1]))
        for nvf in path:
            res += " " * indent + "%s=%s[%s]\n" % nvf
            indent += 2
        return res

    def compileFilters(self, expressionList, filterList, unusedList=None):
        seen = []
        if unusedList is not None:
            newUnused = []
            # fix up the unused list, updateArgs is unaware of specific
            # filter types
            for expression in list(unusedList)[:]:
                if isinstance(expression, str):
                    unusedList.discard(expression)
                    expression = trovefilter.TroveFilter(self.recipe,
                            name = expression)
                newUnused.append(expression)
            unusedList.update(newUnused)

        for expression in expressionList:
            if isinstance(expression, str):
                expression = trovefilter.TroveFilter(self.recipe,
                        name = expression)
            if expression in seen:
                # only put each expression on the list once
                continue
            expression.compile()
            seen.append(expression)
            filterList.append(expression)

    def doProcess(self, recipe):
        """
        Invocation instance
        @param recipe: holds the recipe object, which is used for
        the macro set and group objects.
        @return: None
        @rtype: None
        """
        self.recipe = recipe
        self.macros = recipe.macros

        if hasattr(self.__class__, 'preProcess'):
            self.preProcess()

        # is runtime check implemented?
        if hasattr(self.__class__, 'test'):
            if not self.test():
                return

        # change self.use to be a simple flag
        self.use = action.checkUse(self.use)

        # compile the exceptions
        self.exceptionFilters = []
        self.compileFilters(self.invariantexceptions, self.exceptionFilters)
        if self.exceptions:
            if not isinstance(self.exceptions, (tuple, list)):
                # turn a plain string into a sequence
                self.exceptions = (self.exceptions,)
            self.compileFilters(self.exceptions, self.exceptionFilters,
                    self.unusedFilters['exceptions'])

        # compile the inclusions
        self.inclusionFilters = []
        self.compileFilters(self.invariantinclusions, self.inclusionFilters)
        if not self.inclusions:
            # an empty list, as opposed to None, means nothing is included
            if isinstance(self.inclusions, (tuple, list)):
                return
        else:
            if not isinstance(self.inclusions, (tuple, list)):
                # turn a plain string into a sequence
                self.inclusions = (self.inclusions,)
            self.compileFilters(self.inclusions, self.inclusionFilters,
                    self.unusedFilters['inclusions'])

        # dispatch if/as appropriate
        if self.use:
            self.do()

        if hasattr(self.__class__, 'postProcess'):
            self.postProcess()

    def walkTrove(self, path, cache, trv):
        path = path[:]
        nvf = trv.getNameVersionFlavor()
        path.append(nvf)
        if self._pathAllowed(path):
            if nvf not in self.recipe.troveMap:
                yield path
            # we walk each group we built separately, so don't traverse them
            for nvf in trv.iterTroveList(strongRefs = True):
                if nvf not in self.recipe.troveMap:
                    if cache.troveIsCached(nvf):
                        newTrv = cache.getTrove(withFiles = False, *nvf)
                        for childPath in self.walkTrove(path, cache, newTrv):
                            yield childPath

    def do(self):
        grpGraph = graph.DirectedGraph()
        # get a top to bottom directed graph order
        for grp in list(self.recipe.troveMap.values()):
            nvf = grp.getNameVersionFlavor()
            grpGraph.addNode(nvf)
            for child in grp.iterTroveList(strongRefs = True):
                if child in self.recipe.troveMap:
                    grpGraph.addEdge(nvf, child)
        order = grpGraph.getTotalOrdering()
        # build a list of paths, but go in the above order to
        # ensure sane results
        if self.checkImageGroups:
            policySets = dict((x[0], [[x]]) for x in order \
                    if self.recipe.groups[x[0]].imageGroup)
        else:
            policySets = {order[0][0]: [[order[0]]]}
        for nvf in order:
            grp = self.recipe.troveMap[nvf]
            for paths in list(policySets.values()):
                for path in [x for x in paths if x[-1] == nvf]:
                    for child in grp.iterTroveList(strongRefs = True):
                        newPath = path + [child]
                        if child in self.recipe.troveMap:
                            if self._pathAllowed(newPath):
                                paths.append(newPath)
        # now we can use the final group in each chain to determine the
        # inclusion info for each trove.
        for groupName, paths in policySets.items():
            troveSet = []
            for path in paths:
                grp = self.recipe.troveMap[path[-1]]
                cache = self.recipe.groups[grp.getName()].cache
                infoDict = {}
                for nvf, byDefault, isStrong in grp.iterTroveListInfo():
                    infoDict[nvf] = (byDefault, isStrong)
                for trovePath in self.walkTrove(path[:-1], cache, grp):
                    byDefault, isStrong = infoDict[trovePath[-1]]
                    troveSet.append((trovePath, byDefault, isStrong))
            if troveSet:
                self.doTroveSet(troveSet)

# External policy classes (do not create classes for internal policy buckets)
class DestdirPolicy(Policy):
    bucket = DESTDIR_MODIFICATION

class PackagePolicy(Policy):
    bucket = PACKAGE_MODIFICATION

class EnforcementPolicy(Policy):
    bucket = ENFORCEMENT

class GroupEnforcementPolicy(GroupPolicy):
    bucket = GROUP_ENFORCEMENT
    checkImageGroups = False

class ImageGroupEnforcementPolicy(GroupPolicy):
    bucket = GROUP_ENFORCEMENT
    checkImageGroups = True

class _K:
    pass
classType = type(_K)

# loading, sorting, and initializing policy modules
def loadPolicy(recipeObj, policySet = None, internalPolicyModules = (),
        basePolicy = Policy):
    # path -> module
    policyPathMap = {}
    # bucket -> ordered list of policy objects
    policies = {
        TESTSUITE: [],
        DESTDIR_PREPARATION: [],
        DESTDIR_MODIFICATION: [],
        PACKAGE_CREATION: [],
        PACKAGE_MODIFICATION: [],
        ENFORCEMENT: [],
        ERROR_REPORTING: [],
        GROUP_ENFORCEMENT: [],
    }
    # bucket -> dict of policy classes
    policyMap = dict((b, {}) for b in list(policies.keys()))
    # name -> policy classes
    policyNameMap = {}

    # Load conary internal policy. Note that these are now loaded first, 
    # so that pluggable policy may override internal policy.  Also note
    # that the order of the internalPolicyModules list matters, as the
    # *last* module of a given name which is loaded wins.  This is
    # also true of pluggable policy, so the policyDirs config option must
    # be carefully constructed in reverse order of preference.

    import conary.build.destdirpolicy
    import conary.build.derivedpolicy
    import conary.build.derivedcapsulepolicy
    import conary.build.infopolicy
    import conary.build.packagepolicy
    import conary.build.capsulepolicy
    import conary.build.grouppolicy
    for pt in internalPolicyModules:
        m = sys.modules['conary.build.'+pt]
        for symbolName in list(m.__dict__.keys()):
            policyCls = m.__dict__[symbolName]
            if type(policyCls) is not classType:
                continue
            if symbolName[0] != '_' and issubclass(policyCls, basePolicy):
                policyNameMap[symbolName] = policyCls

    # Load pluggable policy
    for policyDir in recipeObj.cfg.policyDirs:
        if not os.path.isdir(policyDir):
            continue
        for filename in sorted(os.listdir(policyDir)):
            fullpath = os.sep.join((policyDir, filename))
            if not filename.endswith('.py') or not util.isregular(fullpath):
                continue
            # do not load shared libraries as policy!
            desc = [x for x in imp.get_suffixes() if x[0] == '.py'][0]
            f = file(fullpath)
            modname = filename[:-3]
            m = imp.load_module(modname, f, fullpath, desc)
            f.close()
            policyPathMap[fullpath] = m

            for symbolName in m.__dict__:
                policyCls = m.__dict__[symbolName]
                if type(policyCls) is not classType:
                    continue
                if policySet is not None and symbolName not in policySet:
                    continue
                if symbolName[0].isupper() and issubclass(policyCls,
                        basePolicy):
                    policyNameMap[symbolName] = policyCls

    # Enforce dependencies
    missingDeps = []
    for policyCls in list(policyNameMap.values()):
        if hasattr(policyCls, 'requires'):
            for reqName, reqType in policyCls.requires:
                if reqType & REQUIRED and reqName not in policyNameMap:
                    missingDeps.append((policyCls.__name__, reqName))
    if missingDeps:
        raise PolicyError('\n'.join(
            ('policy %s missing required policy %s' %(x,y)
             for x, y in missingDeps)))

    # Sort and initialize
    for policyName, policyCls in policyNameMap.items():
        policyMap[policyCls.bucket][policyName]=policyNameMap[policyName]
    for bucket in list(policyMap.keys()):
        dg = graph.DirectedGraph()
        for policyCls in list(policyMap[bucket].values()):
            dg.addNode(policyCls)
            if hasattr(policyCls, 'requires'):
                for reqName, reqType in policyCls.requires:
                    if reqType & ORDERED and reqName in policyMap[bucket]:
                        if reqType & PRIOR:
                            dg.addEdge(policyNameMap[reqName], policyCls)
                        else:
                            dg.addEdge(policyCls, policyNameMap[reqName])

        # test for dependency loops
        depLoops = [x for x in dg.getStronglyConnectedComponents()
                    if len(x) > 1]
        if depLoops:
            # convert to names
            depLoops = [sorted(x.__name__ for x in y) for y in depLoops]
            raise PolicyError('\n'.join(
                'found dependency loop: %s' %', '.join(y)
                 for y in depLoops))

        # store an ordered list of initialized policy objects
        policies[bucket] = [x(recipeObj) for x in dg.getTotalOrdering(
            nodeSort=lambda a, b: cmp(a[1].__name__, b[1].__name__))]

    return policyPathMap, policies



class PolicyError(errors.CookError):
    pass
