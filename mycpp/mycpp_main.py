#!/usr/bin/env python3
"""
mycpp_main.py - Translate a subset of Python to C++, using MyPy's typed AST.
"""
from __future__ import print_function

import optparse
import os
import sys

from typing import List, Optional, Tuple

from mypy import build
from mypy.build import build as mypy_build
from mypy.build import BuildSource
from mypy.main import process_options
from mypy.options import Options

import const_pass
import cppgen_pass
import debug_pass
import pass_state

from util import log


def Options():
  """Returns an option parser instance."""

  p = optparse.OptionParser()
  p.add_option(
      '-v', '--verbose', dest='verbose', action='store_true', default=False,
      help='Show details about translation')

  p.add_option(
      '--shared-ptr', dest='shared_ptr', action='store_true', default=False,
      help='Emit code using std::shared_ptr instead of raw pointers')

  p.add_option(
      '--to-header', dest='to_header', action='append', default=[],
      help='Export this module to a header, e.g. frontend.args')

  p.add_option(
      '--header-out', dest='header_out', default=None,
      help='Write this header')

  return p


# Copied from mypyc/build.py
def get_mypy_config(paths: List[str],
                    mypy_options: Optional[List[str]]) -> Tuple[List[BuildSource], Options]:
    """Construct mypy BuildSources and Options from file and options lists"""
    # It is kind of silly to do this but oh well
    mypy_options = mypy_options or []
    mypy_options.append('--')
    mypy_options.extend(paths)

    sources, options = process_options(mypy_options)

    # OSH PATCH
    #if options.python_version[0] == 2:
    if 0:
        fail('Python 2 not supported')

    # OSH Patch!
    #if not options.strict_optional:
    if 0:
        fail('Disabling strict optional checking not supported')

    options.show_traceback = True
    # Needed to get types for all AST nodes
    options.export_types = True
    # TODO: Support incremental checking
    options.incremental = False
    # 10/2019: FIX for MyPy 0.730.  Not sure why I need this but I do.
    options.preserve_asts = True

    for source in sources:
        options.per_module_options.setdefault(source.module, {})['mypyc'] = True

    return sources, options


def ModulesToCompile(result, mod_names):
  # HACK TO PUT asdl/runtime FIRST.  It has runtime::SPID.
  #
  # Another fix is to hoist those to the declaration phase?  Not sure if that
  # makes sense.

  # FIRST files.  Somehow the MyPy builder reorders the modules.
  for name, module in result.files.items():
    if name in ('asdl.runtime', 'core.vm'):
      yield name, module

  for name, module in result.files.items():
    # Only translate files that were mentioned on the command line
    suffix = name.split('.')[-1]
    if suffix not in mod_names:
      continue

    # FIRST files.  Don't do it a second time!
    if name in ('asdl.runtime', 'core.vm'):
      continue

    # should be LAST because it uses 2 base classes
    if name in ('osh.builtin_bracket',):
      continue

    yield name, module

  # LAST files
  for name, module in result.files.items():
    if name in ('osh.builtin_bracket',):
      yield name, module


def main(argv):
  # TODO: Put these in the shell script
  mypy_options = [
     '--py2', '--strict', '--no-implicit-optional', '--no-strict-optional',
     # for consistency?
     '--follow-imports=silent',
     #'--verbose',
  ]

  o = Options()
  opts, argv = o.parse_args(argv)

  if opts.shared_ptr:
    log('*** Compiling with std::shared_ptr<> ***')
    cppgen_pass.SHARED_PTR = True  # global var hack for now
     
  paths = argv[1:]  # e.g. asdl/typed_arith_parse.py

  if 0:
    print(opts)
    print(paths)
    return

  # e.g. asdl/typed_arith_parse.py -> 'typed_arith_parse'
  mod_names = [os.path.basename(p) for p in paths]
  mod_names = [os.path.splitext(name)[0] for name in mod_names]

  # Ditto
  to_header = opts.to_header
  #if to_header:
  if 0:
    to_header = [os.path.basename(p) for p in to_header]
    to_header = [os.path.splitext(name)[0] for name in to_header]

  #log('to_header %s', to_header)

  sources, options = get_mypy_config(paths, mypy_options)
  if 0:
    for source in sources:
      log('source %s', source)
  #log('options %s', options)

  #result = emitmodule.parse_and_typecheck(sources, options)
  import time
  start_time = time.time()
  result = mypy_build(sources=sources, options=options)
  #log('elapsed 1: %f', time.time() - start_time)

  if result.errors:
    log('')
    log('-'* 80)
    for e in result.errors:
      log(e)
    log('-'* 80)
    log('')

  # Important functions in mypyc/build.py:
  #
  # generate_c (251 lines)
  #   parse_and_typecheck
  #   compile_modules_to_c

  # mypyc/emitmodule.py (487 lines)
  # def compile_modules_to_c(result: BuildResult, module_names: List[str],
  # class ModuleGenerator:
  #   # This generates a whole bunch of textual code!

  # literals, modules, errors = genops.build_ir(file_nodes, result.graph,
  # result.types)

  # TODO: Debug what comes out of here.
  #build.dump_graph(result.graph)
  #return

  # no-op
  for name in result.graph:
    state = result.graph[name]

  # GLOBAL Constant pass over all modules.  We want to collect duplicate
  # strings together.  And have globally unique IDs str0, str1, ... strN.
  const_lookup = {}
  const_code = []
  pass1 = const_pass.Collect(result.types, const_lookup, const_code)

  to_compile = list(ModulesToCompile(result, mod_names))

  # HACK: Why do I get oil.asdl.tdop in addition to asdl.tdop?
  names = set(name for name, _ in to_compile)

  filtered = []
  seen = set()
  for name, module in to_compile:
    if name.startswith('oil.'):
      name = name[4:]

    if name not in seen:  # remove dupe
      filtered.append((name, module))
      seen.add(name)

  to_compile = filtered

  import pickle
  if 1:
    for name, module in to_compile:
      log('to_compile %s', name)

      # can't pickle but now I see deserialize() nodes and stuff
      #s = pickle.dumps(module)
      #log('%d pickle', len(s))

  # Print the tree for debugging
  if 0:
    for name, module in to_compile:
      builder = debug_pass.Print(result.types)
      builder.visit_mypy_file(module)
    return

  f = sys.stdout

  gc = bool(os.getenv('GC'))
  header_name = 'gc_heap' if gc else 'mylib'

  # TODO: Add --cc-out?  But there is a preamble and postamble.
  f.write("""\
// BEGIN mycpp output

#include "%s.h"

using gc_heap::Alloc;
using gc_heap::kZeroMask;
using gc_heap::Local;
""" % header_name)

  if gc:
    f.write("""\
#include "my_runtime.h"

using gc_heap::NewStr;
""")

  if to_header:
    f.write('#include "%s"\n' % os.path.basename(opts.header_out))
    f.write('\n')

  # Collect constants and then emit code.
  for name, module in to_compile:
    pass1.visit_mypy_file(module)

  # Instead of top-level code, should we generate a function and call it from
  # main?
  for line in const_code:
    f.write('%s\n' % line)
  f.write('\n')

  # Note: doesn't take into account module names!
  virtual = pass_state.Virtual()

  if opts.header_out:
    header_f = open(opts.header_out, 'w')  # Not closed
    guard = 'RUNTIME_H'
    header_f.write("""\
// %s: translated from Python by mycpp

#ifndef %s
#define %s

#include "%s.h"
""" % (os.path.basename(opts.header_out), guard, guard, header_name))

  log('\tFORWARD DECL')

  # Forward declarations first.
  # class Foo; class Bar;
  for name, module in to_compile:
    log('forward decl name %s', name)
    if name in to_header:
      out_f = header_f
    else:
      out_f = f
    p2 = cppgen_pass.Generate(result.types, const_lookup, out_f,
                              virtual=virtual, forward_decl=True)

    p2.visit_mypy_file(module)

  # After seeing class and method names in the first pass, figure out which
  # ones are virtual.  We use this info in the second pass.
  virtual.Calculate()
  #log('V %s', virtual.virtuals)

  local_vars = {}  # FuncDef node -> (name, c_type) list

  # Node -> fmt_name, plus a hack for the counter
  # TODO: This could be a class with 2 members
  fmt_ids = {'_counter': 0}

  log('\tDECL')

  # First generate ALL C++ declarations / "headers".
  # class Foo { void method(); }; class Bar { void method(); };
  for name, module in to_compile:
    log('decl name %s', name)
    if name in to_header:
      out_f = header_f
    else:
      out_f = f
    p3 = cppgen_pass.Generate(result.types, const_lookup, out_f,
                              local_vars=local_vars, fmt_ids=fmt_ids,
                              virtual=virtual, decl=True)

    p3.visit_mypy_file(module)

  if opts.header_out:
    header_f.write("""\
#endif  // %s
""" % guard)

  log('\tDEFINITION')

  # Now the definitions / implementations.
  # void Foo:method() { ... }
  # void Bar:method() { ... }
  for name, module in to_compile:
    p4 = cppgen_pass.Generate(result.types, const_lookup, f,
                              local_vars=local_vars, fmt_ids=fmt_ids)
    p4.visit_mypy_file(module)


if __name__ == '__main__':
  try:
    main(sys.argv)
  except RuntimeError as e:
    print('FATAL: %s' % e, file=sys.stderr)
    sys.exit(1)
