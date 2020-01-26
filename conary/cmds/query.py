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
Provides the output for the "conary query" command
"""

import itertools
import os

from conary import display
from conary.conaryclient import cmdline
from conary.deps import deps
from conary.lib import util, api

def displayTroves(db, cfg, troveSpecs = [], pathList = [],
                  whatProvidesList = [],
                  # trove options
                  info = False, digSigs = False, showBuildReqs = False,
                  showDeps = False,
                  # file options
                  ls = False, lsl = False, ids = False, sha1s = False,
                  tags = False, fileDeps = False, fileVersions = False,
                  fileFlavors = False, capsules = False,
                  # collection options
                  showTroves = False, recurse = None, showAllTroves = False,
                  weakRefs = False, showTroveFlags = False,
                  pristine = True, alwaysDisplayHeaders = False,
                  exactFlavors = False):
    """Displays troves after finding them on the local system

       @param db: Database instance to search for troves in
       @type db: local.database.Database
       @param cfg: conary config
       @type cfg: conarycfg.ConaryConfiguration
       @param troveSpecs: troves to search for
       @type troveSpecs: list of troveSpecs (n[=v][[f]])
       @param pathList: paths to match up to troves
       @type pathList: list of strings
       @param whatProvidesList: list of dependencies to find provides for
       @type whatProvidesList: list of strings
       @param info: If true, display general information about the trove
       @type info: bool
       @param digSigs: If true, display digital signatures for a trove.
       @type digSigs: bool
       @param showBuildReqs: If true, display the versions and flavors of the
       build requirements that were used to build the given troves
       @type showBuildReqs: bool
       @param showDeps: If true, display provides and requires information
       for the trove.
       @type showDeps: bool
       @param ls: If true, list files in the trove
       @type ls: bool
       @param lsl: If true, list files in the trove + ls -l information
       @type lsl: bool
       @param ids: If true, list pathIds for files in the troves
       @type ids: bool
       @param sha1s: If true, list sha1s for files in the troves
       @type sha1s: bool
       @param tags: If true, list tags for files in the troves
       @type tags: bool
       @param fileDeps: If true, print file-level dependencies
       @type fileDeps: bool
       @param fileVersions: If true, print fileversions
       @type fileVersions: bool
       @param showTroves: If true, display byDefault True child troves of this
       trove
       @type showTroves: bool
       @param recurse: display child troves of this trove, recursively
       @type recurse: bool
       @param showAllTroves: If true, display all byDefault False child troves
       of this trove
       @type showAllTroves: bool
       @param weakRefs: display both weak and strong references of this trove.
       @type weakRefs: bool
       @param showTroveFlags: display [<flags>] list with information about
       the given troves.
       @type showTroveFlags: bool
       @param pristine: If true, display the pristine version of this trove
       @type pristine: bool
       @param alwaysDisplayHeaders: If true, display headers even when listing
       files.
       @type alwaysDisplayHeaders: bool
       @rtype: None
    """

    whatProvidesList = [ deps.parseDep(x) for x in whatProvidesList ]

    troveTups, primary = getTrovesToDisplay(db, troveSpecs, pathList,
                                            whatProvidesList,
                                            exactFlavors=exactFlavors)

    dcfg = LocalDisplayConfig(db, affinityDb=db)
    # it might seem weird to use the same source we're querying as
    # a source for affinity info, but it makes sure that all troves with a
    # particular name are looked at for flavor info

    dcfg.setTroveDisplay(deps=showDeps, info=info,
                         showBuildReqs=showBuildReqs,
                         digSigs=digSigs, fullVersions=cfg.fullVersions,
                         showLabels=cfg.showLabels, fullFlavors=cfg.fullFlavors,
                         showComponents = cfg.showComponents,
                         baseFlavors = cfg.flavor)

    dcfg.setFileDisplay(ls=ls, lsl=lsl, ids=ids, sha1s=sha1s, tags=tags,
                        fileDeps=fileDeps, fileVersions=fileVersions,
                        fileFlavors=fileFlavors, capsules=capsules)


    recurseOne = showTroves or showAllTroves or weakRefs or showTroveFlags

    if recurse is None and not recurseOne and primary:
        # if we didn't explicitly set recurse and we're not recursing one
        # level explicitly and we specified troves (so everything won't
        # show up at the top level anyway), guess at whether to recurse
        recurse = True in (ls, lsl, ids, sha1s, tags, showDeps, fileDeps,
                           fileVersions, fileFlavors)

    displayHeaders = alwaysDisplayHeaders or showTroveFlags

    dcfg.setChildDisplay(recurseAll = recurse, recurseOne = recurseOne,
                         showNotByDefault = True,
                         showNotExists = showAllTroves,
                         showWeakRefs = weakRefs,
                         showTroveFlags = showTroveFlags,
                         displayHeaders = displayHeaders,
                         checkExists = True)
    dcfg.setShowPristine(pristine)

    if primary:
        dcfg.setPrimaryTroves(set(troveTups))

    formatter = LocalTroveFormatter(dcfg)

    display.displayTroves(dcfg, formatter, troveTups)


@api.publicApi
def getTrovesToDisplay(db, troveSpecs, pathList=[], whatProvidesList=[],
                       exactFlavors=False):
    """ Finds the given trove and path specifiers, and returns matching
        (n,v,f) tuples.
        @param db: database to search
        @type db: local.database.Database
        @param troveSpecs: troves to search for
        @type troveSpecs: list of troveSpecs (n[=v][[f]])
        @param pathList: paths which should be linked to some trove in this
                         database.
        @type pathList: list of strings
        @param whatProvidesList: deps to search for providers of
        @type whatProvidesList: list of strings

        @raises TroveSpecError: Raised if one of the troveSpecs is of an
                                invalid format

        @note: This function calls database routines which could raise any
               errors defined in L{dbstore.sqlerrors}

        @rtype: troveTupleList (list of (name, version, flavor) tuples),
                and a boolean that stats whether the troves returned should
                be considered primary (and therefore not compressed ever).
    """

    primary = True

    if troveSpecs:
        troveSpecs = [ cmdline.parseTroveSpec(x, allowEmptyName=False) \
                                                        for x in troveSpecs ]
    else:
        troveSpecs = []

    normPathList = [ util.realpath(os.path.abspath(util.normpath(x)))
                     for x in pathList ]

    troveTups = []
    for path, origPath in zip(normPathList, pathList):
        if origPath.endswith('/'):
            allPaths = [ path + '/' + x for x in os.listdir(db.root + path) ]
        else:
            allPaths = [ path ]

        for thisPath in allPaths:
            for trove in db.iterTrovesByPath(thisPath):
                troveTups.append((trove.getName(), trove.getVersion(),
                                  trove.getFlavor()))

    if whatProvidesList:
        results = db.getTrovesWithProvides(whatProvidesList)
        troveTups.extend(itertools.chain(*iter(results.values())))

    if not (troveSpecs or pathList or whatProvidesList):
        troveTups = sorted(db.iterAllTroves())
        primary = False
    else:
        results = db.findTroves(None, troveSpecs, exactFlavors=exactFlavors)

        for troveSpec in troveSpecs:
            troveTups.extend(results.get(troveSpec, []))

    return troveTups, primary


class LocalDisplayConfig(display.DisplayConfig):
    def __init__(self, *args, **kw):
        display.DisplayConfig.__init__(self, *args, **kw)
        self.showPristine = False

    def setShowPristine(self, b = True):
        self.showPristine = b

    def getPristine(self):
        return self.showPristine

class LocalTroveFormatter(display.TroveFormatter):
    pass
