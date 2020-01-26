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
This module defines trove filters.
"""

import itertools
import re

from conary.deps import arch, deps
from conary import versions

VT_NONE         = 0
VT_LABEL        = 1
VT_BRANCH       = 2
VT_VERSION      = 3
VT_REVISION     = 4
VT_SRC_REVISION = 5
VT_BIN_REVISION = 6


class AbstractFilter(object):
    def match(self, *args, **kwargs):
        raise NotImplementedError
    def __invert__(self):
        return NotFilter(self)
    def __or__(self, filter):
        return OrFilter(self, filter)
    def __and__(self, filter):
        return AndFilter(self, filter)
    def __hash__(self):
        if 'filters' in self.__dict__:
            res = None
            for f in self.filters:
                res = hash((res, hash(f)))
            return res
        else:
            return object.__hash__(self)
    __mul__ = __and__
    __add__ = __or__
    __neg__ = __invert__
    def __sub__(self, filter):
        return OrFilter(self, NotFilter(filter))
    def compile(self):
        pass

class AndFilter(AbstractFilter):
    def __init__(self, *filters):
        self.filters = filters
    def match(self, *args, **kwargs):
        for filter in self.filters:
            if not filter.match(*args, **kwargs):
                return False
        return True

class OrFilter(AbstractFilter):
    def __init__(self, *filters):
        self.filters = filters
    def match(self, *args, **kwargs):
        for filter in self.filters:
            if filter.match(*args, **kwargs):
                return True
        return False

class NotFilter(AbstractFilter):
    def __init__(self, filter):
        self.filter = filter
    def match(self, *args, **kwargs):
        return not self.filter.match(*args, **kwargs)

class TroveFilter(AbstractFilter):
    def __init__(self, recipe, name = None, version = None, flavor = None):
        self.compiled = False
        self.macros = recipe.macros
        self.name = self.label = self.branch = self.version = self.flavor = \
                self.revision = None
        if name is not None:
            self._validateRegexp(name, 'name')
            self.name = name
        if version is not None:
            self.version = version
        if flavor is not None:
            self.flavor = deps.parseFlavor(flavor)

    def __eq__(self, filter):
        return self.name == filter.name and \
                self.version == filter.version and \
                self.flavor == filter.flavor

    def __hash__(self):
        return hash((self.name, self.version, self.flavor))

    def __str__(self):
        name = self.name or ''
        ver = self.version or ''
        if ver:
            ver = '=' + ver
        flv = self.flavor and '[%s]' % self.flavor or ''
        return "<TroveFilter: '%s%s%s'>" % (name, ver, flv)

    def _validateRegexp(self, pattern, param):
        try:
            re.compile(pattern)
        except:
            raise RuntimeError("Bad Regexp: '%s' for %s" % (pattern, param))

    def _compilePattern(self, pattern):
        if pattern is not None:
            if not pattern or pattern[0] != '^':
                pattern = '^' + pattern
            if pattern[-1] != '$':
                pattern += '$'
            return re.compile(pattern % self.macros)

    def _getVersionType(self, version):
        if not version:
            return VT_NONE
        if '/' not in version:
            if '@' not in version:
                return version.count('-') + VT_REVISION
            else:
                return VT_LABEL
        else:
            ver = versions.VersionFromString(version)
            if isinstance(ver, versions.Branch):
                return VT_BRANCH
            elif isinstance(ver, versions.Version):
                return VT_VERSION

    def _compareVersions(self, versionType, a, b):
        if not versionType:
            return True
        if isinstance(b, str):
            return a == b
        if versionType == VT_LABEL:
            return a == str(b.branch().label())
        if versionType == VT_BRANCH:
            return a == str(b.branch())
        if versionType == VT_VERSION:
            return a == str(b)
        if versionType == VT_REVISION:
            return a == str(b.trailingRevision().getVersion())
        if versionType == VT_SRC_REVISION:
            rev = b.trailingRevision()
            return a == ('%s-%s' % (rev.getVersion(), rev.getSourceCount()))
        if versionType == VT_BIN_REVISION:
            return a == str(b.trailingRevision())
        return False

    def _compareFlavors(self, a, b):
        if a is None:
            return True
        if a == b:
            return True
        # this doesn't need to account for all flavors that there are,
        # just the ones that can reasonably co-exist
        a_arches = []
        b_arches = []
        for prefArch in set(itertools.chain(*list(arch.FlavorPreferences.flavorPreferences.values()))):
            prefArch = deps.parseFlavor(prefArch)
            a_arches.append(a.satisfies(prefArch))
            b_arches.append(b.satisfies(prefArch))
        if True in a_arches and a_arches != b_arches:
            # filter specified any kind of arch at all and all
            # arch definitions match
            return False
        if not str(a.difference(b)):
            # check if characteristics of b completely subsume the filter
            return True
        return False

    def compile(self):
        self.nameRe = self._compilePattern(self.name)
        if self.version is not None:
            version = self.version % self.macros
            self.versionType = self._getVersionType(version)
            self.version = self.version and self.version % self.macros
        else:
            self.versionType = VT_NONE
        self.compiled =  True

    def match(self, nvfTuples):
        if not self.compiled:
            self.compile()
        for name, version, flavor in nvfTuples:
            match = True
            if self.name is not None:
                match = match and re.match(self.nameRe, name)
            if self.version is not None:
                match = match and self._compareVersions(self.versionType,
                                                        self.version, version)
            if str(self.flavor):
                if isinstance(flavor, str):
                    flavor = deps.parseFlavor(self.flavor)
                match = match and self._compareFlavors(self.flavor, flavor)
            if match:
                return True
        return False
