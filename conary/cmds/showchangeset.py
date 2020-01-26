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
Provides the output for the "conary showcs" command
"""

import itertools, sys

#conary
from conary import conaryclient
from conary.conaryclient import cmdline
from conary import display
from conary.cmds import query
from conary.lib import log
from conary.repository import trovesource

def usage():
    print("conary showcs   <changeset> [trove[=version]]")
    print("  Accepts all common display options.  Also,")
    print("                --show-changes        For modifications, show the old ")
    print("                                      file info below new")
    print("                --all                 Combine tags to display most information about the changeset")
    print("                --recurse-repository  Search repositories for information about referenced but not")
    print("                                      included troves")
    print("")

def displayChangeSet(db, cs, troveSpecs, cfg,
                     asDiff = False, diffBinaries = False,
                     # selection options
                     exactFlavors = False,
                     # trove options
                     info = False, digSigs = False, deps = False,
                     showBuildReqs = False, all = False,
                     # file options
                     ls = False, lsl = False, ids = False, sha1s = False,
                     tags = False, fileDeps = False, fileVersions = False,
                     fileFlavors = False, capsules = False,
                     # collection options
                     showTroves = False, recurse = None, showAllTroves = False,
                     weakRefs = False, showTroveFlags = False,
                     alwaysDisplayHeaders = False,  recurseRepos=False,
                     # job options
                     showChanges = False, asJob = False):

    asDiff = asDiff or diffBinaries;

    if all:
        deps = recurse = showTroveFlags = showAllTroves = True
        if ls:
            fileDeps = lsl = tags = True

    if showChanges:
        lsl = True

    if recurseRepos:
        recurse = True

    client = conaryclient.ConaryClient(cfg)
    repos = client.getRepos()

    if asDiff:
        troveSource = trovesource.SourceStack(client.getDatabase(), repos)
        for x in cs.gitDiff(troveSource, diffBinaries = diffBinaries):
            sys.stdout.write(x)
    elif not asJob and not showChanges and cs.isAbsolute():
        changeSetSource = trovesource.ChangesetFilesTroveSource(None)
        changeSetSource.addChangeSet(cs)


        if not troveSpecs:
            troveTups = cs.getPrimaryTroveList()
            primary = True
            if not troveTups:
                log.warning('No primary troves in changeset, listing all troves')
                troveTups = [(x.getName(), x.getNewVersion(), x.getNewFlavor())\
                                            for x in cs.iterNewTroveList()]
        else:
            troveTups, primary  = query.getTrovesToDisplay(changeSetSource,
                                                           troveSpecs,
                                                     exactFlavors=exactFlavors)
        if recurseRepos:
            querySource = trovesource.stack(changeSetSource, client.getRepos())
        else:
            querySource = changeSetSource

        dcfg = display.DisplayConfig(querySource, client.db)
        dcfg.setTroveDisplay(deps=deps, info=info, showBuildReqs=showBuildReqs,
                             digSigs=digSigs, fullFlavors=cfg.fullFlavors,
                             showLabels=cfg.showLabels, baseFlavors=cfg.flavor,
                             fullVersions=cfg.fullVersions,
                             )
        dcfg.setFileDisplay(ls=ls, lsl=lsl, ids=ids, sha1s=sha1s, tags=tags,
                            fileDeps=fileDeps, fileVersions=fileVersions,
                            fileFlavors=fileFlavors, capsules=capsules)

        recurseOne = showTroves or showAllTroves or weakRefs
        if recurse is None and not recurseOne:
            # if we didn't explicitly set recurse and we're not recursing one
            # level explicitly
            recurse = True in (ls, lsl, ids, sha1s, tags, deps, fileDeps,
                               fileVersions, fileFlavors)

        dcfg.setChildDisplay(recurseAll = recurse, recurseOne = recurseOne,
                         showNotByDefault = showAllTroves,
                         showWeakRefs = weakRefs,
                         checkExists = True, showNotExists = True,
                         showTroveFlags = showTroveFlags,
                         displayHeaders = alwaysDisplayHeaders or showTroveFlags)

        if primary:
            dcfg.setPrimaryTroves(set(troveTups))
        formatter = display.TroveFormatter(dcfg)
        display.displayTroves(dcfg, formatter, troveTups)
    else:
        changeSetSource = trovesource.ChangeSetJobSource(repos,
                                             trovesource.stack(db, repos))
        changeSetSource.addChangeSet(cs)

        jobs = getJobsToDisplay(changeSetSource, troveSpecs)

        dcfg = display.JobDisplayConfig(changeSetSource, client.db)

        dcfg.setJobDisplay(showChanges=showChanges,
                           compressJobs=not cfg.showComponents)

        dcfg.setTroveDisplay(deps=deps, info=info, fullFlavors=cfg.fullFlavors,
                             showLabels=cfg.showLabels, baseFlavors=cfg.flavor,
                             fullVersions=cfg.fullVersions)


        dcfg.setFileDisplay(ls=ls, lsl=lsl, ids=ids, sha1s=sha1s, tags=tags,
                            fileDeps=fileDeps, fileVersions=fileVersions,
                            fileFlavors=fileFlavors)

        recurseOne = showTroves or showAllTroves or weakRefs
        if recurse is None and not recurseOne:
            # if we didn't explicitly set recurse and we're not recursing one
            # level explicitly and we specified troves (so everything won't
            # show up at the top level anyway), guess at whether to recurse
            recurse = True in (ls, lsl, ids, sha1s, tags, deps, fileDeps,
                               fileVersions, fileFlavors)

        dcfg.setChildDisplay(recurseAll = recurse, recurseOne = recurseOne,
                         showNotByDefault = showAllTroves,
                         showWeakRefs = weakRefs,
                         showTroveFlags = showTroveFlags)

        formatter = display.JobFormatter(dcfg)
        display.displayJobs(dcfg, formatter, jobs)


def getJobsToDisplay(jobSource, jobSpecs):
    if jobSpecs:
        jobSpecs = cmdline.parseChangeList(jobSpecs, allowChangeSets=False)
    else:
        jobSpecs = []

    if jobSpecs:
        results = jobSource.findJobs(jobSpecs)
        jobs = list(itertools.chain(*iter(results.values())))
    else:
        jobs = list(jobSource.iterAllJobs())

    return jobs
