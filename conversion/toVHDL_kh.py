#!/usr/bin/env python
# -*- coding: utf-8 -*-

#
# to_vhdl_kh: vhdl convertor that Keeps Hierarchy
#
# Author:  Oscar Diaz <oscar.dc0@gmail.com>
#
# This code is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 3 of the License, or (at your option) any later version.
#
# This code is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this package; if not, see 
# <http://www.gnu.org/licenses/>.
#

import sys
import re
import os
import inspect
import warnings
from myhdl import ToVHDLError, ToVHDLWarning, intbv

import myhdl
from myhdl import *
from myhdl.conversion._misc import _genUniqueSuffix
from myhdl.conversion._analyze import (_enumTypeSet, _constDict, _AnalyzeTopFuncVisitor)
from myhdl.conversion._toVHDL import (toVHDL, _ToVHDLConvertor, _enumPortTypeSet, 
                                      _writeFileHeader, _shortversion)
from myhdl._extractHierarchy import (_memInfoMap, _UserCode)
from myhdl._Signal import _Signal

from myhdl.conversion._toVHDL import _convertGens as _original_convertGens
from myhdl.conversion._toVHDL import _writeCustomPackage as _original_writeCustomPackage
from myhdl.conversion._toVHDL import _writeModuleHeader as _original_writeModuleHeader

# main object
class _ToVHDL_kh_Convertor(_ToVHDLConvertor):
    __slots__ = ("maxdepth", 
                 "no_component_files"
                 )

    def __init__(self):
        _ToVHDLConvertor.__init__(self)
        
        # additional arguments
        self.no_component_files = False
        # Whether to save code from components to files
        # True to try to store code in open_interceptor object
        self.maxdepth = None
        # set recursion depth. use None to unlimited recursion, 
        # 0 will fall back to standard convertor
        # 1 only convert one layer
        
    def _convert_filter(self, h, intf, siglist, memlist, genlist):
        # Keep-hierarchy code as filter
        self._kh_filter(h, intf, siglist, memlist, genlist)
    
    def _kh_filter(self, h, intf, siglist, memlist, genlist):

        if self.maxdepth == 0:
            # disabled kh
            setattr(intf, "kh_comp_inst", [])
            setattr(intf, "kh_comp_decls", {})
            genlist.insert(0, intf)
            return
        
        # get list of components for instantiation
        comp_inst = {}
        direct_impl = {}
        missing_idx = range(1, len(h.hierarchy))
        for inst_name, inst in h.hierarchy[0].subs:
            found = False
            for idx, ih in enumerate(h.hierarchy):
                # only read level=2 entries
                if ih.level != 2:
                    continue
                if ih.name.startswith(inst_name):
                    comp_inst[ih.name] = ih
                    missing_idx.remove(idx)
                    found = True
            if not found:
                # instance don't have entry in hierarchy. Store as direct implementation
                direct_impl[inst_name] = inst
        for missing in missing_idx:
            # only read level=2 entries
            if h.hierarchy[missing].level <= 2:
                warnings.warn("Missing instance name for '%s' (level %d)" % 
                                (h.hierarchy[missing].name, 
                                 h.hierarchy[missing].level), 
                                 category=ToVHDLWarning)
                
        # avoid reserved words in port names
        for sname, sig in intf.argdict.items():
            if sname in _VHDL_Reserved_words:
                # change only key in dict and list member in argnames
                warnings.warn("Invalid VHDL name for signal %r. Changgenling to %r" % 
                              (sname, "myhdl_" + sname),category=ToVHDLWarning)
                intf.argdict["myhdl_" + sname] = intf.argdict.pop(sname)
                intf.argnames[intf.argnames.index(sname)] = "myhdl_" + sname
                
        # keep "genlist" with only direct generators
        del_genlist = []
        for tree in genlist:
            if isinstance(tree, _UserCode):
                # user-defined code: can only be used from a structural 
                # definition. 
                
                # special case: top-function using user-defined code
                # pass as direct implementation
                if h.hierarchy[0].func.func_name != tree.funcname:
                    del_genlist.append(tree)
            else:
                for f in tree.body:
                    if f.name not in direct_impl.keys():
                        del_genlist.append(tree)
                    
        del_genlist.reverse()
        for d in del_genlist:
            genlist.remove(d)
            
        # infer internal signals: all signals but arguments
        internals = []
        for sname, sig in h.hierarchy[0].sigdict.iteritems():
            if sname not in intf.argnames:
                internals.append(sig._name)
                # unused internal signals: change to used read
                if not sig._used:
                    sig._used = True
                    sig._read = True
        # TODO: infer correctly the list of internal signals: discard unused
        # signals and top-level signals. Currently recovering unused signals in
        # case they're needed in instance's port map (hint: check attributes of 
        # signals passed to recursive toVHDL)
        
        # all signals in memdict as internals
        for mi in h.hierarchy[0].memdict.itervalues():
            for sig in mi.mem:
                if sig._name not in intf.argnames:
                    # note: signal name could change here for improve context 
                    # in output code. (check if it's worth the change)
                    internals.append(sig._name)
                    
        # unnecesary signals from flat model
        discard_siglist = []
        discard_memlist = []
        for ih in h.hierarchy[1:]:
            for sig in ih.sigdict.itervalues():
                if (sig._name not in discard_siglist) and (sig._name not in internals) and (sig._name not in intf.argnames):
                    discard_siglist.append(sig._name)
            for mi in ih.memdict.itervalues():
                for sig in mi.mem:
                    if (sig._name not in discard_siglist) and (sig._name not in internals) and (sig._name not in intf.argnames):
                        discard_siglist.append(sig._name)
                        if mi.name not in discard_memlist:
                            discard_memlist.append(mi.name)
                            
        # TODO: memlist discard necessary? Check it.
        for dname in discard_siglist:
            for i in range(len(siglist)):
                if siglist[i]._name == dname:
                    siglist[i]._clear()
                    del siglist[i]
                    break
        
        comp_dict = {}
        for name, inst in comp_inst.items():
            # NOTE: for each instance check argument values when called
            # and generate a different component if the values are 
            # different between instances. That means each component will 
            # have "fixed" generics. This could change in the future, provided
            # the conversor support generics.
            
            # comp_dict has [func_name]: ([<inst_name>,...], <paramdict>), ...
            # paramdict holds data about how the instance was called
            paramdict = inst.argdict.copy()
            strargs = inspect.getargspec(inst.func).args
            # sigdict: get _val from signals
            for k, v in inst.sigdict.items():
                if k in strargs:
                    paramdict[k] = v._val
            
            if inst.func.func_name not in comp_dict:
                comp_dict[inst.func.func_name] = [([name], paramdict)]
            else:
                hit = False
                for cnames, carg in comp_dict[inst.func.func_name]:
                    # special comparison: see function def
                    if _param_compare(carg, paramdict):
                        cnames.append(name)
                        hit = True
                        break
                if not hit:
                    comp_dict[inst.func.func_name].append(([name], paramdict))
                    
        files = {}
        comp_decls = {}
        # NOTE: this is a ugly way to put new use statements. Look for a better way
        use_clauses = ["use %s.pck_myhdl_%s.all;" % (self.library, _shortversion)]
        for func_name, instdata in comp_dict.iteritems():
            for cidx, cdata in enumerate(instdata):
                inst_name = cdata[0][0]
                inst = comp_inst[inst_name]
                argdict = cdata[1]
                
                # entity names (and VHDL identifiers) cannot start with "_" 
                # (TODO: probably other characters have the same problem)
                comp_name = func_name
                if func_name[0] == "_":
                    comp_name = "myhdl" + comp_name
                if len(instdata) > 1:
                    # multiple component for a single function
                    comp_name = "%s_%d" % (comp_name, cidx)
                    
                comp_decls[comp_name] = cdata[0]
                use_clauses.append("use %s.%s;" % (self.library, comp_name))
                
                if not self.no_component_files:
                    # copy list of non-Signal arguments 
                    func_args = {}
                    strargs = inspect.getargspec(inst.func).args
                    for k, v in inst.sigdict.items():
                        if k in strargs:
                            func_args[k] = _Signal(v._val)
                    for k, v in argdict.items():
                        if (k not in func_args) and (k in strargs):
                            func_args[k] = v
                    
                    # check for 'self' in argdict
                    if "self" in func_args:
                        warnings.warn("Detected 'self' argument: %s. Removed before recursive calling." 
                                      % repr(func_args["self"]), category=ToVHDLWarning)
                        del func_args["self"]
                    
                    if self.maxdepth == 1 :
                        # standard convertor
                        convertor = toVHDL
                    else:
                        # recursive convertor: create a new one
                        convertor = _ToVHDL_kh_Convertor()
                        convertor.maxdepth = self.maxdepth
                        convertor.no_component_files = self.no_component_files
                        if self.maxdepth is not None:
                            convertor.maxdepth -= 1
                            
                    # copy some attributes
                    for attr in _ToVHDLConvertor.__slots__:
                        if attr not in ("name", "component_declarations", "no_myhdl_package"):
                            setattr(convertor, attr, getattr(self, attr))
                    convertor.no_myhdl_package = True
                    convertor.name = comp_name
                    
                    # NOTE: toVHDL is non-reentrant function
                    # save some states prior to call
                    state_memInfoMap = _memInfoMap.copy()
                    state_genUniqueSuffix = _genUniqueSuffix.i
                    state_enumTypeSet = _enumTypeSet.copy()
                    state_constDict = _constDict.copy()
                    
                    # support for enum types in entity ports
                    state_enumPortTypeSet = _enumPortTypeSet.copy()
                    _enumPortTypeSet.clear()
                
                    # recursive call
                    convertor(inst.func, **func_args)
                
                    _genUniqueSuffix.i = state_genUniqueSuffix
                    _enumTypeSet.update(state_enumTypeSet)
                    _memInfoMap.clear()
                    _memInfoMap.update(state_memInfoMap)
                    _constDict.clear()
                    _constDict.update(state_constDict)
                
                    if len(_enumPortTypeSet) > 0:
                        # replace local or port TypeSet with a use statement
                        use_clauses.append("use %s.pck_%s.all;" % (self.library, comp_name))
                        for e in _enumPortTypeSet:
                            if e in _enumTypeSet:
                                _enumTypeSet.remove(e)
                    _enumPortTypeSet.clear()
                    _enumPortTypeSet.update(state_enumPortTypeSet)
                
        # KH transformations done. Save additional data in intf object
        # this will be used on _writeSigDecls and _convertGens
        setattr(intf, "kh_comp_inst", comp_inst)
        setattr(intf, "kh_comp_decls", comp_decls)
        setattr(intf, "kh_use_clauses", use_clauses)
        # _convertGens don't get intf as argument. Use genlist to pass
        # intf to _convertGens
        genlist.insert(0, intf)
        
    def _cleanup(self, siglist):
        _ToVHDLConvertor._cleanup(self, siglist)
        self.no_component_files = False
        self.maxdepth = None

toVHDL_kh = _ToVHDL_kh_Convertor()

# override converter functions

def _convertGens(genlist, siglist, memlist, vfile):
    if isinstance(genlist[0],  _AnalyzeTopFuncVisitor):
        intf = genlist.pop(0)
    else:
        intf = None
    
    _original_convertGens(genlist, siglist, memlist, vfile)
    
    if intf is not None:
        # instantiations
        for comp_name, comp_data in intf.kh_comp_decls.items():
            for inst_name in comp_data:
                inst = intf.kh_comp_inst[inst_name]
                print >> vfile, "\n%s : entity %s \nport map (" % (inst_name, comp_name)
                pmap = []
                strargs = inspect.getargspec(inst.func).args
                for sname, sig in inst.sigdict.items():
                    if sname in strargs:
                        # fix invalid name if required
                        if sname in _VHDL_Reserved_words:
                            sname = "myhdl_" + sname
                        pmap.append("    %s => %s" % (sname, sig._name))
                print >> vfile, ",\n".join(pmap) + ");"
            
        print >> vfile, "\n"
        
def _writeCustomPackage(f, intf):
    # Enumeration support: in KH mode, type related to an entity
    # should go to a separate file.
    if hasattr(intf, "kh_comp_decls"):
        vpath = "pck_" + intf.name + ".vhd"
        vfile = open(vpath, 'w')
        _writeFileHeader(vfile, vpath)
        _original_writeCustomPackage(vfile, intf)
        vfile.close()
    else:
        _original_writeCustomPackage(f, intf)
        
def _writeModuleHeader(f, intf, needPck, lib, arch, useClauses, doc, numeric):
    # Additional packages: support for additional useClauses,
    #   components in local library, external package file
    if hasattr(intf, "kh_use_clauses"):
        if useClauses is None:
            mod_useClauses = "\n".join(intf.kh_use_clauses)
        else:
            mod_useClauses = useClauses + "\n" + "\n".join(intf.kh_use_clauses)
        _original_writeModuleHeader(f, intf, needPck, lib, arch, mod_useClauses, doc, numeric)
    else:
        _original_writeModuleHeader(f, intf, needPck, lib, arch, useClauses, doc, numeric)

def _monkey_convertor():
    """
    Monkey patch
    
    This replaces original _writeSigDecls, _convertGens and _writeCustomPackage
    to support kh conversion.
    """
    for k, v in sys.modules.items():
        if k == "toVHDL_kh":
            continue
        if "_convertGens" in dir(v):
            if v._convertGens == _original_convertGens:
                #print "Monkey change k %s v %s from %s to %s" % (k, v, v._convertGens, _original_convertGens)
                setattr(v, "_convertGens", _convertGens)
        if "_writeCustomPackage" in dir(v):
            if v._writeCustomPackage == _original_writeCustomPackage:
                #print "Monkey change k %s v %s from %s to %s" % (k, v, v._writeCustomPackage, _original_writeCustomPackage)
                setattr(v, "_writeCustomPackage", _writeCustomPackage)
        if "_writeModuleHeader" in dir(v):
            if v._writeModuleHeader == _original_writeModuleHeader:
                #print "Monkey change k %s v %s from %s to %s" % (k, v, v._writeModuleHeader, _original_writeModuleHeader)
                setattr(v, "_writeModuleHeader", _writeModuleHeader)
    #print "MONKEY: patch done! test it"

_monkey_convertor()

# special comparison
def _param_compare(x, y):
    # assume x and y dicts
    # same keys
    if x.keys() != y.keys():
        return False
    # compare each value
    for k in x.keys():
        if x[k] != y[k]:
            return False
        # additional comparison: 
        # if values are intbv, compare its bitlenghts
        if isinstance(x[k], intbv) and isinstance(y[k], intbv):
            if len(x[k]) != len(y[k]):
                return False
        elif isinstance(x[k], intbv) or isinstance(y[k], intbv):
            # one of them is intbv but the other not.
            return False
    # otherwise, equals
    return True
    
# Reserved words in VHDL (IEEE 1076-2008)
_VHDL_Reserved_words = ("abs", "access", "after", "alias", "all", "and", 
"architecture", "array", "assert", "assume", "assume_guarantee", "attribute", 
"begin", "block", "body", "buffer", "bus", "case", "component", "configuration", 
"constant", "context", "cover", "default", "disconnect", "downto", "else", 
"elsif", "end", "entity", "exit", "fairness", "file", "for", "force", "function", 
"generate", "generic", "group", "guarded", "if", "impure", "in", "inertial", 
"inout", "is", "label", "library", "linkage", "literal", "loop", "map", "mod", 
"nand", "new", "next", "nor", "not", "null", "of", "on", "open", "or", "others", 
"out", "package", "parameter", "port", "postponed", "procedure", "process", 
"property", "protected", "pure", "range", "record", "register", "reject", 
"release", "rem", "report", "restrict", "restrict_guarantee", "return", "rol", 
"ror", "select", "sequence", "severity", "signal", "shared", "sla", "sll", 
"sra", "srl", "strong", "subtype", "then", "to", "transport", "type", 
"unaffected", "units", "until", "use", "variable", "vmode", "vprop", "vunit", 
"wait", "when", "while", "with", "xnor", "xor")
