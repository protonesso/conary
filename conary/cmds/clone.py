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

from conary import callbacks
from conary import errors
from conary import versions
from conary.conaryclient import ConaryClient, cmdline
from conary.conaryclient import callbacks as client_callbacks


def displayCloneJob(cs):
    indent = '   '
    def _sortTroveNameKey(x):
        name = x.getName()
        return (not name.endswith(':source'), x.getNewFlavor(), name)
    csTroves = sorted(cs.iterNewTroveList(), key=_sortTroveNameKey)

    for csTrove in csTroves:
        newInfo = str(csTrove.getNewVersion())
        flavor = csTrove.getNewFlavor()
        if not flavor.isEmpty():
            newInfo += '[%s]' % flavor

        print("%sClone  %-20s (%s)" % (indent, csTrove.getName(), newInfo))

def CloneTrove(cfg, targetBranch, troveSpecList, updateBuildInfo = True,
               info = False, cloneSources = False, message = None,
               test = False, fullRecurse = False, ignoreConflicts = False,
               exactFlavors = False):
    client = ConaryClient(cfg)
    repos = client.getRepos()

    targetBranch = versions.VersionFromString(targetBranch)
    if not isinstance(targetBranch, versions.Branch):
        raise errors.ParseError('Cannot specify full version "%s" to clone to - must specify target branch' % targetBranch)

    troveSpecs = [ cmdline.parseTroveSpec(x) for x in troveSpecList]

    componentSpecs = [ x[0] for x in troveSpecs
                       if ':' in x[0] and x[0].split(':')[1] != 'source']
    if componentSpecs:
        raise errors.ParseError('Cannot clone components: %s' % ', '.join(componentSpecs))


    trovesToClone = repos.findTroves(cfg.installLabelPath,
                                    troveSpecs, cfg.flavor,
                                    exactFlavors = exactFlavors)
    trovesToClone = list(set(itertools.chain(*iter(trovesToClone.values()))))

    if not client.cfg.quiet:
        callback = client_callbacks.CloneCallback(client.cfg, message)
    else:
        callback = callbacks.CloneCallback()

    okay, cs = client.createCloneChangeSet(targetBranch, trovesToClone,
                                           updateBuildInfo=updateBuildInfo,
                                           infoOnly=info, callback=callback,
                                           fullRecurse=fullRecurse,
                                           cloneSources=cloneSources)
    if not okay:
        return
    return _finishClone(client, cfg, cs, callback, info=info,
                        test=test, ignoreConflicts=ignoreConflicts)

def _convertLabelOrBranch(lblStr, template):
    try:
        if not lblStr:
            return None
        if lblStr[0] == '/':
            v = versions.VersionFromString(lblStr)
            if isinstance(v, versions.Branch):
                return v
            # Some day we could lift this restriction if its useful.
            raise errors.ParseError('Cannot specify version to promote'
                                    ' - must specify branch or label')
        if not template:
            return versions.Label(lblStr)


        hostName = template.getHost()
        nameSpace = template.getNamespace()
        tag = template.branch

        if lblStr[0] == ':':
            lblStr = '%s@%s%s' % (hostName, nameSpace, lblStr)
        elif lblStr[0] == '@':
            lblStr = '%s%s' % (hostName, lblStr)
        elif lblStr[-1] == '@':
            lblStr = '%s%s:%s' % (lblStr, nameSpace, tag)
        return versions.Label(lblStr)
    except Exception as msg:
        raise errors.ParseError('Error parsing %r: %s' % (lblStr, msg))

def promoteTroves(cfg, troveSpecs, targetList, skipBuildInfo=False,
                  info=False, message=None, test=False,
                  ignoreConflicts=False, cloneOnlyByDefaultTroves=False,
                  cloneSources = False, allFlavors = False, client=None,
                  targetFile = None, exactFlavors = None,
                  excludeGroups = False):
    targetMap = {}
    searchPath = []
    for fromLoc, toLoc in targetList:
        context = cfg.buildLabel
        fromLoc = _convertLabelOrBranch(fromLoc, context)
        if fromLoc is not None:
            if isinstance(fromLoc, versions.Branch):
                context = fromLoc.label()
            else:
                context = fromLoc
            searchPath.append(context)
        toLoc = _convertLabelOrBranch(toLoc, context)
        targetMap[fromLoc] = toLoc

    troveSpecs = [ cmdline.parseTroveSpec(x, False) for x in troveSpecs ]
    if exactFlavors:
        allFlavors = False
    elif allFlavors:
        cfg.flavor = []
        troveSpecFlavors =  {}
        for troveSpec in troveSpecs:
            troveSpecFlavors.setdefault(
                        (troveSpec[0], troveSpec[1], None),
                            []).append(troveSpec[2])
        troveSpecs = list(troveSpecFlavors)


    client = ConaryClient(cfg)
    if not searchPath:
        searchPath = cfg.buildLabel
    searchSource = client.getSearchSource(installLabelPath=searchPath)
    results = searchSource.findTroves(troveSpecs,
                                      bestFlavor=not allFlavors,
                                      exactFlavors=exactFlavors)
    if allFlavors:
        trovesToClone = []
        for troveSpec, troveTups in list(results.items()):
            specFlavors = troveSpecFlavors[troveSpec]
            for specFlavor in specFlavors:
                if specFlavor is None:
                    matchingTups = troveTups
                else:
                    matchingTups = [ x for x in troveTups
                                     if x[2].stronglySatisfies(specFlavor)]
                # we only clone the latest version for all troves.
                # bestFlavor=False resturns the leaves for all flavors, so
                # we may need to cut some out.
                latest = max([x[1] for x in matchingTups])
                matchingTups = [ x for x in matchingTups if x[1] == latest ]
                trovesToClone.extend(matchingTups)
    else:
        trovesToClone = itertools.chain(*iter(results.values()))
    trovesToClone = list(set(trovesToClone))

    if not client.cfg.quiet:
        callback = client_callbacks.CloneCallback(client.cfg, message)
    else:
        callback = callbacks.CloneCallback()

    okay, cs = client.createSiblingCloneChangeSet(
                           targetMap, trovesToClone,
                           updateBuildInfo=not skipBuildInfo,
                           infoOnly=info, callback=callback,
                           cloneOnlyByDefaultTroves=cloneOnlyByDefaultTroves,
                           cloneSources=cloneSources,
                           excludeGroups=excludeGroups)
    if not okay:
        return False
    return _finishClone(client, cfg, cs, callback, info=info,
                        test=test, ignoreConflicts=ignoreConflicts,
                        targetFile=targetFile)

def _finishClone(client, cfg, cs, callback, info=False, test=False,
                 ignoreConflicts=False, targetFile=None):
    repos = client.repos
    if cfg.interactive or info:
        print('The following clones will be created:')
        displayCloneJob(cs)

    if info:
        return

    if cfg.interactive:
        print()
        okay = cmdline.askYn('continue with clone? [y/N]', default=False)
        if not okay:
            return

    if targetFile:
        cs.writeToFile(targetFile)
    elif not test:
        repos.commitChangeSet(cs, callback=callback)
    return cs
