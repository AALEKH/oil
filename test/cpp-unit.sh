#!/bin/bash
#
# Usage:
#   ./cpp-unit.sh <function name>

set -o nounset
set -o pipefail
set -o errexit

source build/mycpp.sh  # for compile function

# Copied from devtools/release.sh tarball-build-deps
# for the dev-minimal toil task to run C++ unit tests
deps() {
  local d1='_deps/re2c-1.0.3'
  if test -d $d1; then
    echo "$d1 exists: skipping re2c"
  else
    build/codegen.sh download-re2c
    build/codegen.sh install-re2c
  fi
}

cpp-unit-tests() {
  ### Run unit tests in the cpp/ dir

  local name='unit_tests'
  local bin=_bin/$name.asan  # important: ASAN flags

  mkdir -p _bin
  compile $bin -D CPP_UNIT_TEST \
    cpp/unit_tests.cc \
    _build/cpp/arg_types.cc \
    cpp/frontend_flag_spec.cc \
    cpp/frontend_match.cc \
    cpp/libc.cc \
    cpp/osh_bool_stat.cc \
    mycpp/mylib.cc \

    # I wanted to put frontend/args.py in here.  But it depends on a lot of
    # stuff like runtime_asdl.
    #asdl/runtime.cc  # for args::ParseMore() 

  $bin "$@"
}

mycpp-unit-tests() {
  ### Run unit tests in the mycpp/ dir

  pushd mycpp
  ./run.sh mylib-test
  ./run.sh target-lang
  popd
}

all() {
  build/codegen.sh ast-id-lex  # id.h, osh-types.h, osh-lex.h
  build/codegen.sh flag-gen-cpp  # _build/cpp/arg_types.h
  build/dev.sh oil-asdl-to-cpp  # unit tests depend on id_kind_asdl.h, etc.

  cpp-unit-tests
  mycpp-unit-tests

  asdl/run.sh gen-cpp-test
}


"$@"
