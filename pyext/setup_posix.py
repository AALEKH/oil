#!/usr/bin/env python2
from distutils.core import setup, Extension

# It's named posix_ rather than posix to differentiate it from the stdlib
# module it's based on.

module = Extension('posix_',
                    sources = ['pyext/posixmodule.c'],
                    # we deleted some entries from the method table; I don't
                    # want to see warnings about it
                    extra_compile_args = ['-Wno-unused-function'],
                    )

setup(name = 'posix_',
      version = '1.0',
      description = 'Our fork of the stdlib module',
      # For posix_methods.def
      include_dirs = ['build/oil-defs'],
      ext_modules = [module])
