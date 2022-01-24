#!/usr/bin/env python2
# Copyright 2016 Andy Chu. All rights reserved.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
from __future__ import print_function
"""
line_input_test.py: Tests for line_input
"""

import unittest

#from core.pyerror import log

import line_input


class LineInputTest(unittest.TestCase):

  def testMatchOshToken(self):
    print(dir(line_input))

  def testGetScreenSize(self):
    screen_size = line_input.get_screen_size()
    print('screen size %s' % (screen_size,))


if __name__ == '__main__':
  unittest.main()
