# Copyright (c) 2005 rPath, Inc.
#
# This program is distributed under the terms of the Common Public License,
# version 1.0. A copy of this license should have been distributed with this
# source file in a file called LICENSE. If it is not present, the license
# is always available at http://www.opensource.org/licenses/cpl.php.
#
# This program is distributed in the hope that it will be useful, but
# without any warranty; without even the implied warranty of merchantability
# or fitness for a particular purpose. See the Common Public License for
# full details.
#

from conary import trove, versions
from conary.deps import deps
from conary.lib import util
from conary.build import errors as builderrors
from conary.build import macros
from conary.build import use
from conary.build.recipe import Recipe, RECIPE_TYPE_REDIRECT

import itertools

class RedirectRule(object):
    __slots__ = [ 'destName', 'branchStr', 'sourceFlavor', 'targetFlavor',
                  'skipTargetMatching' ]

    def findAvailableTargetFlavors(self, repos):
        if self.branchStr is None:
            # redirect to nothing
            return set()

        if self.branchStr[0] == '/':
            branch = versions.VersionFromString(self.branchStr)
            if not isinstance(branch, versions.Branch):
                raise builderrors.RecipeFileError, \
                    "Redirects must specify branches or labels, " \
                    "not versions"

            matches = repos.getTroveLeavesByBranch(
                            { self.destName : { branch : None } })
        else:
            label = versions.Label(self.branchStr)
            matches = repos.getTroveLeavesByLabel(
                            { self.destName : { label : None } })
            # check for label multiplicity
            if matches:
                branches = set(x.branch() for x in matches[self.destName])
                if len(branches) > 1:
                    raise builderrors.RecipeFileError, \
                        "Label %s matched multiple branches." % str(label)

        targetFlavors = set()
        # Get the flavors and branch available on the target
        for version, flavorList in matches.get(self.destName, {}).iteritems():
            targetFlavors.update((version, x) for x in flavorList)

        return targetFlavors


    def copy(self):
        return RedirectRule(self.destName, self.branchStr, self.sourceFlavor,
                            self.targetFlavor, self.skipTargetMatching)

    def __init__(self, destName = None, branchStr = None, sourceFlavor = None,
                 targetFlavor = None, skipTargetMatching = None):
        self.destName = destName
        self.branchStr = branchStr
        self.sourceFlavor = sourceFlavor
        self.targetFlavor = targetFlavor
        self.skipTargetMatching = skipTargetMatching

class RedirectRecipe(Recipe):
    Flags = use.LocalFlags
    _recipeType = RECIPE_TYPE_REDIRECT
    internalAbstractBaseClass = 1

    def addRedirect(self, toTrove, branchStr = None, sourceFlavor = None,
                    targetFlavor = None, fromTrove = None, 
                    skipTargetMatching = False):
        if ((sourceFlavor is not None) and (targetFlavor is None)) or \
           ((targetFlavor is not None) and (sourceFlavor is None)):
            raise builderrors.RecipeFileError, \
                "sourceFlavor and targetFlavor must be specified jointly"

        if sourceFlavor is not None:
            f = deps.parseFlavor(sourceFlavor)
            if f is None:
                raise ValueError, 'invalid flavor %s' % sourceFlavor
            sourceFlavor = f

        if targetFlavor is not None:
            f = deps.parseFlavor(targetFlavor)
            if f is None:
                raise ValueError, 'invalid flavor %s' % targetFlavor
            targetFlavor = f

        if fromTrove is None:
            fromTrove = self.name
        elif fromTrove.find(":") != -1:
            raise ValueError, 'components cannot be individually redirected'

        rule = RedirectRule(destName = toTrove, branchStr = branchStr,
                            sourceFlavor = sourceFlavor,
                            targetFlavor = targetFlavor,
                            skipTargetMatching = skipTargetMatching)

        l = self.rules.setdefault(fromTrove, list())
        if sourceFlavor is None:
            l.append(rule)
        else:
            # the default (with no sourceFlavor) has to be at the end to
            # make sure it matches last
            l.insert(0, rule)

    def addRemoveRedirect(self, fromTrove = None):
        # We don't allow flavor-specificty for remove rules. You could write
        # redirect rules for everything which ought to be redirected and have
        # a catch-all remove redirect for everything else.
        if fromTrove is None:
            fromTrove = self.name
        elif fromTrove.find(":") != -1:
            raise ValueError, 'components cannot be individually redirected'

        # the None for branchStr (the second item in this tuple) indicates
        # this is a redirect to nothing
        rule = RedirectRule(destName = self.name)
        l = self.rules.setdefault(self.name, list())
        l.insert(0, rule)

    def _findSourceTroves(self):
        sourceSearch = {}
        for fromTrove in self.rules.iterkeys():
            sourceSearch.setdefault(fromTrove, { self.branch : None })

        # this treats previously-built redirects as flavors we need to
        # redirect from, which seems a bit weird
        sourceTroveMatches = self.repos.getTroveLeavesByBranch(sourceSearch)

        if len(sourceTroveMatches) != len(sourceSearch):
            missing = set(sourceSearch) - set(sourceTroveMatches)
            raise builderrors.RecipeFileError, \
                    "No troves found with name(s) %s" % " ".join(missing)

        return sourceTroveMatches

    def _getSourceTroves(self, searchResult):
        l = []
        for name, d in searchResult.iteritems():
            for version, flavorList in d.iteritems():
                l += [ (name, (None, None), (version, x), True) 
                                for x in flavorList ]

        trvCsDict = {}
        # We don't need to recurse here since we only support package
        # redirects
        cs = self.repos.createChangeSet(l, recurse = False,
                                        withFiles = False)
        for trvCs in cs.iterNewTroveList():
            info = (trvCs.getName(), trvCs.getNewVersion(),
                    trvCs.getNewFlavor())
            trvCsDict[info] = trvCs

        return trvCsDict

    @staticmethod
    def _getTargetRules(rules, name):
        # return the rules for troves with this name; if it's a component of
        # a package we alrady built reuse the rule which we used for that 
        # package
        targetRules = rules.get(name, None)
        if targetRules is None and ':' in name:
            pkgName, compName = name.split(':')
            targetRules = rules.get(pkgName, None)
            if targetRules is not None:
                targetRules = [ x.copy() for x in targetRules ]
                for rule in targetRules:
                    rule.destName = rule.destName + ':' + compName

        if targetRules is None:
            raise builderrors.RecipeFileError, \
                "Cannot find redirection for trove %s" % name

        return targetRules

    def findTroves(self):
        sourceTroveMatches = self._findSourceTroves()
        trvCsDict = self._getSourceTroves(sourceTroveMatches)

        redirMap = {}
        redirRuleMap = {}

        for sourceName in sourceTroveMatches.keys():
            # all of the versions/flavors which currently exist for this
            # trove
            sourceTroveVersions = sourceTroveMatches.pop(sourceName)

            # set of rules for where this trove should redirect to
            targetRules = self._getTargetRules(self.rules, sourceName)

            # XXX the repository operations should be pulled out of all of
            # these loops
            additionalNames = set()
            for rule in targetRules:
                # get all of the flavors this rule specifies redirecting to
                targetFlavors = rule.findAvailableTargetFlavors(self.repos)

                if (rule.branchStr and not targetFlavors
                                   and rule.destName in self.rules):
                    # We're redirecting to something which doesn't
                    # exist. This is an error if it's the top of a
                    # redirect (a package), but generates an erase
                    # redirect if it's for a component.
                    raise builderrors.RecipeFileError, \
                        "Trove %s does not exist" % (rule.destName)

                # This lets us catch where we haven't found any matches for
                # this rule. If we have found any matches for this rule, no
                # error results, even if some of the troves on that label
                # cannot be redirected due to flavor conflicts
                foundMatch = False

                # Try to create redirects for each version/flavor combination
                for version, flavorList in sourceTroveVersions.items():
                    for sourceFlavor in flavorList:
                        if rule.sourceFlavor is not None and \
                           sourceFlavor != rule.sourceFlavor:
                            continue

                        match = None
                        for targetVersion, targetFlavor in targetFlavors:
                            if (not rule.skipTargetMatching and
                                rule.targetFlavor is not None and
                                targetFlavor != rule.targetFlavor):
                                continue

                            if ((rule.sourceFlavor is not None)
                                or rule.skipTargetMatching
                                or sourceFlavor.score(targetFlavor) is not False):
                                match = (targetVersion, targetFlavor)
                                break

                        if match is not None:
                            # found a compatible trove to redirect to
                            if (sourceName, sourceFlavor) in redirMap:
                                # a default-flavor rule doesn't cause a
                                # conflict with a flavor-specifying rule
                                # because the later is more specific (and
                                # we know we've already processed the
                                # flavor-specifying rule because self.rules
                                # is sorted with flavor-specifying rules
                                # at the front)
                                previousRule = redirRuleMap[(sourceName,
                                                             sourceFlavor)]
                                if (previousRule.sourceFlavor 
                                                    is not None and
                                    rule.sourceFlavor is None):
                                    # the default rule should be skipped
                                    # rather than causing a conflict
                                    continue

                                raise builderrors.RecipeFileError, \
                                    "Multiple redirect targets specified " \
                                    "from trove %s[%s]" % (sourceName, sourceFlavor)

                            redirInfo = (rule.destName, match[0].branch(),
                                         rule.targetFlavor)
                        elif not targetFlavors:
                            # redirect to nothing
                            redirInfo = (None, None, None)
                        elif rule.targetFlavor is not None:
                            raise builderrors.RecipeFileError, \
                                "Trove %s does not exist for flavor [%s]" \
                                % (sourceName, targetFlavor)
                        else:
                            continue

                        # we created a redirect!
                        foundMatch = True

                        redirMap[(sourceName, sourceFlavor)] = redirInfo + ([], )
                        redirRuleMap[(sourceName, sourceFlavor)] = rule

                        # Groups don't include any additional redirections, and
                        # neither do items which aren't collections
                        if (sourceName.startswith('group-') or
                            not trove.troveIsCollection(sourceName)):
                            continue

                        # add any troves the redirected trove referenced
                        # to the todo list
                        trvCs = trvCsDict[(sourceName, version, sourceFlavor)]

                        # we can't integrity check here because we got
                        # the trove w/o files
                        trv = trove.Trove(trvCs, skipIntegrityChecks = True)
                        if redirInfo[0] is not None:
                            targetTrove = self.repos.getTrove(rule.destName,
                                                              match[0],
                                                              match[1],
                                                              withFiles = False)
                            targetComponents = set([ x[0].split(':')[1]
                                for x in
                                targetTrove.iterTroveList(strongRefs = True) ])
                        else:
                            targetComponents = set()

                        # assemble a set of all of the components included
                        # in this trove
                        currentComponents = set([ x[0].split(':')[1] for x in
                                        trv.iterTroveList(strongRefs = True) ])

                        # components shared between the current trove and
                        # the target should be redirected to the target
                        # components
                        for compName in currentComponents & targetComponents:
                            newName = redirInfo[0] + ':' + compName
                            redirMap[(sourceName + ':' + compName, sourceFlavor)] = \
                                    ( newName, redirInfo[1], redirInfo[2], [] )

                        # now get all of the components which have been
                        # included in this trove anywhere on the branch; those
                        # components need to generate erase redirects
                        allVersions = self.repos.getTroveVersionsByBranch(
                            { trv.getName() :
                                { trv.getVersion().branch() : None } } )
                        l = []
                        for subVersion, subFlavorList in \
                                allVersions[trv.getName()].iteritems():
                            l += [ ( trv.getName(), subVersion, flavor)
                                     for flavor in subFlavorList ]

                        allTroves = self.repos.getTroves(l, withFiles = False)
                        allComponents = set()
                        for otherTrv in allTroves:
                            allComponents.update(
                               [ x[0].split(':')[1] for x in
                                 otherTrv.iterTroveList(strongRefs = True) ] )

                        # components which existed at any point for this
                        # trove but don't have a component in the redirect
                        # target need to be erased
                        for subName in allComponents - targetComponents:
                            redirMap[(sourceName + ':' + subName, sourceFlavor)] = \
                                    ( None, None, None, [] )

                        # the package redirect includes references to the
                        # component redirects to let the update code know
                        # how to redirect the components; this tracks the
                        # components of this redirect
                        redirMap[(sourceName, sourceFlavor)][-1].extend(
                            [ sourceName + ':' + x for x in allComponents ])

                if not foundMatch:
                    raise builderrors.CookError(
                    "Could not find target with satisfying flavor"
                    " for redirect %s - either create a redirect"
                    " with targetFlavor and sourceFlavor set, or"
                    " create a redirect with skipTargetMatching = True" % sourceName)

        self.redirections = redirMap

    def getRedirections(self):
        return self.redirections

    def __init__(self, repos, cfg, branch, flavor, extraMacros={}):
        self.repos = repos
        self.cfg = cfg
        self.redirections = {}
        self.branch = branch
        self.flavor = flavor
        self.macros = macros.Macros()
        self.macros.update(extraMacros)
        self.rules = {}


