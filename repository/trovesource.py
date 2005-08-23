import changeset
import findtrove
import itertools
import trove

class AbstractTroveSource:
    """ Provides the interface necessary for performing
        findTrove operations on arbitrary sets of troves.
        As long as the subclass provides the following methods,
        findTrove will be able to search it.  You can set the 
        type of searching findTrove will default to here as 
        well.
    """

    def __init__(self):
        self._allowNoLabel = True
        self._bestFlavor = False
        self._getLeavesOnly = False

    def getTroveLeavesByLabel(self, query, bestFlavor=True):
        raise NotImplementedError

    def getTroveVersionsByLabel(self, query, bestFlavor=True):
        raise NotImplementedError

    def getTroveLeavesByBranch(self, query, bestFlavor=True):
        raise NotImplementedError

    def getTroveVersionsByBranch(self, query, bestFlavor=True):
        raise NotImplementedError

    def getTroveVersionFlavors(self, query, bestFlavor=True):
        raise NotImplementedError

    def getTroves(self, troveList, withFiles = True):
        raise NotImplementedError

    def getTrove(self, name, version, flavor, withFiles = True):
        return self.getTroves((name, version, flavor), withFiles)[0]

    def getTroveVersionList(self, name, withFlavors=False):
        raise NotImplementedError

    def findTroves(self, labelPath, troves, defaultFlavor=None, 
                   acrossLabels=True, acrossFlavors=True, 
                   affinityDatabase=None, allowMissing=False):
        troveFinder = findtrove.TroveFinder(self, labelPath, 
                                            defaultFlavor, acrossLabels,
                                            acrossFlavors, affinityDatabase,
                                            allowNoLabel=self._allowNoLabel,
                                            bestFlavor=self._bestFlavor,
                                            getLeaves=self._getLeavesOnly)
        return troveFinder.findTroves(troves, allowMissing)

    def findTrove(self, labelPath, (name, versionStr, flavor), 
                  defaultFlavor=None, acrossSources = True, 
                  acrossFlavors = True, affinityDatabase = None):
        res = self.findTroves(labelPath, ((name, versionStr, flavor),),
                              defaultFlavor, acrossSources, acrossFlavors,
                              affinityDatabase)
        return res[(name, versionStr, flavor)]

# constants mostly stolen from netrepos/netserver
_GET_TROVE_ALL_VERSIONS = 1
_GET_TROVE_VERY_LATEST  = 2         # latest of any flavor

_GET_TROVE_NO_FLAVOR          = 1     # no flavor info is returned
_GET_TROVE_ALL_FLAVORS        = 2     # all flavors (no scoring)
_GET_TROVE_BEST_FLAVOR        = 3     # the best flavor for flavorFilter
_GET_TROVE_ALLOWED_FLAVOR     = 4     # all flavors which are legal

_CHECK_TROVE_REG_FLAVOR    = 1          # use exact flavor and ensure trove 
                                        # flavor is satisfied by query flavor
_CHECK_TROVE_STRONG_FLAVOR = 2          # use strong flavors and reverse sense

_GTL_VERSION_TYPE_NONE    = 0
_GTL_VERSION_TYPE_LABEL   = 1
_GTL_VERSION_TYPE_VERSION = 2
_GTL_VERSION_TYPE_BRANCH  = 3

class SimpleTroveSource(AbstractTroveSource):
    """ A simple implementation of most of the methods needed 
        for findTrove - all of the methods are implemplemented
        in terms of trovesByName, which is left for subclasses to 
        implement.
    """

    def trovesByName(self, name):
        raise NotImplementedError

    def getTroves(self, troveList, withFiles = True):
        raise NotImplementedError
    
    def __init__(self):
        self.searchAsDatabase()

    def searchAsRepository(self):
        self._allowNoLabel = False
        self._bestFlavor = True
        self._getLeavesOnly = True
        self._flavorCheck = _CHECK_TROVE_REG_FLAVOR

    def searchAsDatabase(self):
        self._allowNoLabel = True
        self._bestFlavor = False
        self._getLeavesOnly = False
        self._flavorCheck = _CHECK_TROVE_STRONG_FLAVOR

    def getTroveVersionList(self, name, withFlavors=False):
        if withFlavors:
            return [ x[1:] for x in self.trovesByName(name) ]
        else:
            return [ x[1] for x in self.trovesByName(name) ]

    def _toQueryDict(self, troveList):
        d = {}
        for (n,v,f) in troveList:
            d.setdefault(n, {}).setdefault(v, []).append(f)
        return d

    def _getTrovesByType(self, troveSpecs, 
                         versionType=_GTL_VERSION_TYPE_NONE,
                         latestFilter=_GET_TROVE_ALL_VERSIONS, 
                         bestFlavor=False):
        """ Implements the various getTrove methods by grabbing
            information from trovesByName.  Note it takes an 
            extra parameter over the netrepos/netserver version - 
            flavorCheck - which, if set to _GET_TROVE_STRONG_FLAVOR,
            does a strong comparison against the listed troves - 
            the specified flavor _must_ exist in potentially matching 
            troves, and sense reversal is not allowed - e.g. 
            ~!foo is not an acceptable match for a flavor request of 
            ~foo.  The mode is useful when the troveSpec is specified
            by the user and is matching against a limited set of troves.
        """
        # some cases explained: 
        # if latestFilter ==  _GET_TROVE_ALL_VERSIONS and 
        # flavorFilter == _GET_TROVE_BEST_FLAVOR,
        # for each version, return the best flavor for that version.

        # if latestFilter == _GET_TROVE_VERY_LATEST and flavorFilter
        # == _GET_TROVE_BEST_FLAVOR
        # get the latest version that has an allowed flavor, then get
        # only the best one of the flavors at that latest version

        if bestFlavor:
            flavorFilter = _GET_TROVE_BEST_FLAVOR
        else:
            flavorFilter = _GET_TROVE_ALL_FLAVORS
            # if any flavor query specified is not None, assume
            # that we want all _allowed_ flavors, not all 
            # flavors
            for name, versionQuery in troveSpecs.iteritems():
                for version, flavorQuery in versionQuery.iteritems():
                    if flavorQuery is not None:
                        flavorFilter = _GET_TROVE_ALLOWED_FLAVOR
                        break
                if flavorFilter == _GET_TROVE_ALLOWED_FLAVOR:
                    break


        flavorCheck = self._flavorCheck

        allTroves = {}
        for name, versionQuery in troveSpecs.iteritems():
            troves = self._toQueryDict(self.trovesByName(name))
            if not troves:
                continue
            if not versionQuery:
                allTroves[name] = troves[name]
                continue
            versionResults = {}
            for version in troves[name].iterkeys():
                if versionType == _GTL_VERSION_TYPE_LABEL:
                    theLabel = version.branch().label()
                    if theLabel not in versionQuery:
                        continue
                    versionResults.setdefault(theLabel, []).append(version)
                elif versionType == _GTL_VERSION_TYPE_BRANCH:
                    theBranch = version.branch()
                    if theBranch not in versionQuery:
                        continue
                    versionResults.setdefault(theBranch, []).append(version)
                elif versionType == _GTL_VERSION_TYPE_VERSION:
                    if version not in versionQuery:
                        continue
                    versionResults.setdefault(version, []).append(version)
                else:
                    assert(False)
            for queryKey, versionList in versionResults.iteritems():
                if latestFilter == _GET_TROVE_VERY_LATEST:
                    versionList.sort()
                    versionList.reverse()
                flavorQuery = versionQuery[queryKey]
                if (flavorFilter == _GET_TROVE_ALL_FLAVORS or
                                                 flavorQuery is None):

                    if latestFilter == _GET_TROVE_VERY_LATEST:
                        versionList = versionList[:1]
                    for version in versionList:
                        vDict = allTroves.setdefault(name, {})
                        fSet = vDict.setdefault(version, set())
                        fSet.update(troves[name][version])
                else:
                    for qFlavor in flavorQuery:
                        for version in versionList:
                            flavorList = troves[name][version]
                            troveFlavors = set() 
                            if flavorCheck == _CHECK_TROVE_STRONG_FLAVOR:
                                strongFlavors = [x.toStrongFlavor() for x in flavorList]
                                flavorList = zip(strongFlavors, flavorList)
                                scores = ((x[0].score(qFlavor), x[1]) \
                                                            for x in flavorList)
                            else:
                                scores = ((qFlavor.score(x), x) for x in flavorList)
                            scores = [ x for x in scores if x[0] is not False]
                            if scores:
                                if flavorFilter == _GET_TROVE_BEST_FLAVOR:
                                    troveFlavors.add(max(scores)[1])
                                elif flavorFilter == _GET_TROVE_ALLOWED_FLAVOR:
                                    troveFlavors.update([x[1] for x in scores])
                                else:
                                    assert(false)

                        
                            if troveFlavors: 
                                vDict = allTroves.setdefault(name, {})
                                fSet = vDict.setdefault(version, set())
                                fSet.update(troveFlavors)
                                if latestFilter == _GET_TROVE_VERY_LATEST:
                                    break
        return allTroves

    def getTroveLeavesByLabel(self, troveSpecs, bestFlavor=True):
        return self._getTrovesByType(troveSpecs, _GTL_VERSION_TYPE_LABEL, 
                                 _GET_TROVE_VERY_LATEST, bestFlavor)

    def getTroveVersionsByLabel(self, troveSpecs, bestFlavor=True):
        return self._getTrovesByType(troveSpecs, _GTL_VERSION_TYPE_LABEL, 
                                     _GET_TROVE_ALL_VERSIONS, bestFlavor)

    def getTroveLeavesByBranch(self, troveSpecs, bestFlavor=True):
        """ Takes {n : { Version : [f,...]} dict """
        return self._getTrovesByType(troveSpecs, _GTL_VERSION_TYPE_BRANCH,
                                     _GET_TROVE_VERY_LATEST, bestFlavor)

    def getTroveVersionsByBranch(self, troveSpecs, bestFlavor=True):
        return self._getTrovesByType(troveSpecs, _GTL_VERSION_TYPE_BRANCH, 
                                     _GET_TROVE_ALL_VERSIONS, bestFlavor)

    def getTroveVersionFlavors(self, troveSpecs, bestFlavor=True):
        """ Takes {n : { Version : [f,...]} dict """
        return self._getTrovesByType(troveSpecs, 
                                     _GTL_VERSION_TYPE_VERSION, 
                                     _GET_TROVE_ALL_VERSIONS, 
                                     bestFlavor)

class TroveListTroveSource(SimpleTroveSource):
    def __init__(self, source, troveTups, withDeps=False):
        SimpleTroveSource.__init__(self)
        troveTups = [ x for x in troveTups ]
        self.deps = {}
        self._trovesByName = {}
        self.source = source
        self.sourceTups = troveTups[:]

        for (n,v,f) in troveTups:
            self._trovesByName.setdefault(n, []).append((n,v,f))

        foundTups = set()
        
        # recurse into the given trove tups to include all child troves
        while troveTups:
            self._trovesByName.setdefault(n, []).append((n,v,f))
            newTroves = source.getTroves(troveTups, withFiles=False)
            foundTups.update(newTroves)
            troveTups = []
            for newTrove in newTroves:
                for tup in newTrove.iterTroveList():
                    self._trovesByName.setdefault(tup[0], []).append(tup)
                    if tup not in foundTups:
                        troveTups.append(tup)

    def getSourceTroves(self):
        return self.getTroves(self.sourceTups)

    def getTroves(self, troveTups, withFiles=False):
        return self.source.getTroves(troveTups, withFiles)

    def trovesByName(self, name):
        return self._trovesByName.get(name, [])


class GroupRecipeSource(SimpleTroveSource):
    """ A TroveSource that contains all the troves in a cooking 
        (but not yet committed) recipe.  Useful for modifying a recipe
        in progress using findTrove.
    """

    def __init__(self, source, groupRecipe):
        self.searchAsDatabase()
        self.deps = {}
        self._trovesByName = {}
        self.source = source
        self.sourceTups = groupRecipe.troves

        for (n,v,f) in self.sourceTups:
            self._trovesByName.setdefault(n, []).append((n,v,f))

    def getTroves(self, troveTups, withFiles=False):
        return self.source.getTroves(troveTups, withFiles)

    def trovesByName(self, name):
        return self._trovesByName.get(name, []) 

class ReferencedTrovesSource(SimpleTroveSource):
    """ A TroveSource that only (n,v,f) pairs for troves that are
        referenced by other, installed troves.
    """
    def __init__(self, source):
        self.searchAsDatabase()
        self.source = source

    def getTroves(self, troveTups, *args, **kw):
        return self.source.getTroves(troveTups, *args, **kw)

    def trovesByName(self, name):
        return self.source.findTroveReferences([name])[0]

class ChangesetFilesTroveSource(SimpleTroveSource):

    # Provide a trove source based on both absolute and relative change
    # set files. Changesets withFiles=False can be generated from this
    # source. Conflicting troves added to this cause an exception, and
    # if the old version for a relative trovechangeset is not available,
    # an exception is thrown.

    # it's likely this should all be indexed by troveName instead of
    # full tuples

    def __init__(self, db):
        SimpleTroveSource.__init__(self)
        self.db = db
        self.troveCsMap = {}

    def addChangeSet(self, cs):
        relative = []
        for trvCs in cs.iterNewTroveList():
            info = (trvCs.getName(), trvCs.getNewVersion(), 
                    trvCs.getNewFlavor())
            if trvCs.getOldVersion() is None:
                if info in self.troveCsMap:
                    raise DuplicateTrove
                self.troveCsMap[info] = cs
                continue

            relative.append((trvCs, info))

        present = self.db.hasTroves([ (x[0].getName(), x[0].getOldVersion(),
                                       x[0].getOldFlavor()) for x in relative ])
        for (trvCs, info), isPresent in itertools.izip(relative, present):
            if not isPresent:
                raise MissingTrove
            
            if info in self.troveCsMap:
                raise DuplicateTrove
            self.troveCsMap[info] = cs

    def trovesByName(self, name):
        l = []
        for info in self.troveCsMap:
            if info[0] == name:
                l.append(info)

        return l

    def getTroves(self, troveList, withFiles = True):
        assert(not withFiles)
        retList = []

        for info in troveList:
            trvCs = self.troveCsMap[info].getNewTroveVersion(*info)
            if trvCs.getOldVersion() is None:
		newTrove = trove.Trove(trvCs.getName(), trvCs.getNewVersion(),
                                       trvCs.getNewFlavor(), 
                                       trvCs.getChangeLog())
            else:
                newTrove = self.db.getTrove(trvCs.getName(), 
                                            trvCs.getOldVersion(),
                                            trvCs.getOldFlavor())

            newTrove.applyChangeSet(trvCs)
            retList.append(newTrove)

        return retList

    def createChangeSet(self, jobList, withFiles = True, recurse = False):
        # Returns the changeset plus a remainder list of the bits it
        # couldn't do
        def _findTroveObj(availSet, name, version, flavor):
            info = (name, version, flavor)
            (inDb, fromCs) = availSet[info]

            if fromCs:
                [ trv ] = self.getTroves([info], withFiles = False)
            elif inDb:
                # XXX this should be parallelized...
                trv = self.db.getTrove(*info)
            else:
                trv = None

            return trv

        assert(not withFiles)
        assert(not recurse)
        troves = []
        for job in jobList:
            if job[1][0] is not None:
                troves.append((job[0], job[1][0], job[1][1]))
            if job[2][0] is not None:
                troves.append((job[0], job[2][0], job[2][1]))

        inDatabase = self.db.hasTroves(troves)
        asChangeset = [ info in self.troveCsMap for info in troves ]
        trovesAvailable = dict((x[0], (x[1], x[2])) for x in 
                            itertools.izip(troves, inDatabase, asChangeset))

        cs = changeset.ChangeSet()
        remainder = []

        for job in jobList:
            if job[2][0] is None:
                cs.oldTrove(job[0], job[1][0], job[1][1])
                continue

            newTrv = _findTroveObj(trovesAvailable, job[0], job[2][0], 
                                   job[2][1])
            if newTrv is None:
                remainder.append(job)
                continue

            if job[1][0] is None:
                oldTrv = None
            else:
                oldTrv = _findTroveObj(trovesAvailable, job[0], job[1][0], 
                                       job[1][1])
                if oldTrv is None:
                    remainder.append(job)

            cs.newTrove(newTrv.diff(oldTrv)[0])

        return (cs, remainder)
