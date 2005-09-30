# -*- mode: python -*-
#
# Copyright (c) 2005 rPath, Inc.
#
# This program is distributed under the terms of the Common Public License,
# version 1.0. A copy of this license should have been distributed with this
# source file in a file called LICENSE. If it is not present, the license
# is always available at http://www.opensource.org/licenses/cpl.php.
#
# This program is distributed in the hope that it will be useful, but
# without any waranty; without even the implied warranty of merchantability
# or fitness for a particular purpose. See the Common Public License for
# full details.
#
"""
Implements branch and shadow command line functionality.
"""
import itertools

import conaryclient
import versions
from lib import log
import updatecmd

def _getBranchType(binaryOnly, sourceOnly):
    if binaryOnly and sourceOnly:
        raise OptionsError, ('Can only specify one of --binary-only and'
                             ' --source-only')
    if binaryOnly:
        return conaryclient.BRANCH_BINARY_ONLY
    elif sourceOnly:
        return conaryclient.BRANCH_SOURCE_ONLY
    else:
        return conaryclient.BRANCH_ALL

def branch(repos, cfg, newLabel, troveSpecs, makeShadow = False,
           sourceOnly = False, binaryOnly = False):
    branchType = _getBranchType(binaryOnly, sourceOnly)

    client = conaryclient.ConaryClient(cfg)

    troveSpecs = [ updatecmd.parseTroveSpec(x) for x in troveSpecs ]

    result = repos.findTroves(cfg.buildLabel, troveSpecs, cfg.buildFlavor)
    troveList = [ x for x in itertools.chain(*result.itervalues())]

    if makeShadow:
        dups = client.createShadow(newLabel, troveList, branchType=branchType)
    else:
        dups = client.createBranch(newLabel, troveList, branchType=branchType)

    for (name, branch) in dups:
        log.warning("%s already has branch %s", name, branch.asString())
