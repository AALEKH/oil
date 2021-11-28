#!/usr/bin/env python2
# Copyright 2016 Andy Chu. All rights reserved.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
"""
builtin_misc.py - Misc builtins.
"""
from __future__ import print_function

from errno import EINTR

from _devbuild.gen import arg_types
from _devbuild.gen.runtime_asdl import (
    span_e, cmd_value__Argv, lvalue, value, scope_e)
from _devbuild.gen.syntax_asdl import source
from asdl import runtime
from core import alloc
from core import error
from core.pyerror import e_usage, e_die, log
from core import pyos
from core import pyutil
from core import state
from core import ui
from core import vm
from frontend import flag_spec
from frontend import reader
from frontend import typed_args
from mycpp import mylib
from osh import word_compile
from pylib import os_path
from qsn_ import qsn_native

import libc
import posix_ as posix

from typing import Tuple, List, Optional, TYPE_CHECKING
if TYPE_CHECKING:
  from _devbuild.gen.runtime_asdl import span_t
  from core.pyutil import _ResourceLoader
  from core.state import Mem, DirStack
  from core.ui import ErrorFormatter
  from frontend.parse_lib import ParseContext
  from osh.cmd_eval import CommandEvaluator
  from osh.split import SplitContext

_ = log

#
# Implementation of builtins.
#


class Times(vm._Builtin):

  def __init__(self):
    # type: () -> None
    """Empty constructor for mycpp."""
    vm._Builtin.__init__(self)

  def Run(self, cmd_val):
    # type: (cmd_value__Argv) -> int
    pyos.PrintTimes()
    return 0


# The Read builtin splits using IFS.
#
# Summary:
# - Split with IFS, except \ can escape them!  This is different than the
#   algorithm for splitting words (at least the way I've represented it.)

# Bash manual:
# - If there are more words than names, the remaining words and their
#   intervening delimiters are assigned to the last name.
# - If there are fewer words read from the input stream than names, the
#   remaining names are assigned empty values.
# - The characters in the value of the IFS variable are used to split the line
#   into words using the same rules the shell uses for expansion (described
# above in Word Splitting).
# - The backslash character '\' may be used to remove any special meaning for
#   the next character read and for line continuation.

def _AppendParts(s, spans, max_results, join_next, parts):
  # type: (str, List[Tuple[span_t, int]], int, bool, List[mylib.BufWriter]) -> Tuple[bool, bool]
  """ Append to 'parts', for the 'read' builtin.
  
  Similar to _SpansToParts in osh/split.py

  Args:
    s: The original string
    spans: List of (span, end_index)
    max_results: the maximum number of parts we want
    join_next: Whether to join the next span to the previous part.  This
    happens in two cases:
      - when we have '\ '
      - and when we have more spans # than max_results.
  """
  start_index = 0
  # If the last span was black, and we get a backslash, set join_next to merge
  # two black spans.
  last_span_was_black = False

  for span_type, end_index in spans:
    if span_type == span_e.Black:
      if join_next and parts:
        parts[-1].write(s[start_index:end_index])
        join_next = False
      else:
        buf = mylib.BufWriter()
        buf.write(s[start_index:end_index])
        parts.append(buf)
      last_span_was_black = True

    elif span_type == span_e.Delim:
      if join_next:
        parts[-1].write(s[start_index:end_index])
        join_next = False
      last_span_was_black = False

    elif span_type == span_e.Backslash:
      if last_span_was_black:
        join_next = True
      last_span_was_black = False

    if max_results and len(parts) >= max_results:
      join_next = True

    start_index = end_index

  done = True
  if len(spans):
    #log('%s %s', s, spans)
    #log('%s', spans[-1])
    last_span_type, _ = spans[-1]
    if last_span_type == span_e.Backslash:
      done = False

  #log('PARTS %s', parts)
  return done, join_next

#
# Three read() wrappers for 'read' builtin that RunPendingTraps: _ReadN,
# _ReadUntilDelim, and _ReadLineSlowly
#

def _ReadN(stdin_fd, num_bytes, cmd_ev):
  # type: (int, int, CommandEvaluator) -> str
  chunks = []  # type: List[str]
  bytes_left = num_bytes
  while bytes_left > 0:
    n, err_num = pyos.Read(stdin_fd, bytes_left, chunks)  # read up to n bytes

    if n < 0:
      if err_num == EINTR:
        cmd_ev.RunPendingTraps()
        # retry after running traps
      else:
        # Like the top level IOError handler
        e_die('osh I/O error: %s', posix.strerror(err_num), status=2)

    elif n == 0:  # EOF
      break

    else:
      bytes_left -= n

  return ''.join(chunks)


def _ReadUntilDelim(delim_byte, cmd_ev):
  # type: (int, CommandEvaluator) -> Tuple[str, bool]
  """Read a portion of stdin.
  
  Read until that delimiter, but don't include it.
  """
  eof = False
  ch_array = []  # type: List[int]
  while True:
    ch, err_num = pyos.ReadByte(0)
    if ch < 0:
      if err_num == EINTR:
        cmd_ev.RunPendingTraps()
        # retry after running traps
      else:
        # Like the top level IOError handler
        e_die('osh I/O error: %s', posix.strerror(err_num), status=2)

    elif ch == pyos.EOF_SENTINEL:
      eof = True
      break

    elif ch == delim_byte:
      break

    else:
      ch_array.append(ch)

  return pyutil.ChArrayToString(ch_array), eof


# sys.stdin.readline() in Python has its own buffering which is incompatible
# with shell semantics.  dash, mksh, and zsh all read a single byte at a
# time with read(0, 1).

# TODO:
# - _ReadLineSlowly should have keep_newline (mapfile -t)
#   - this halves memory usage!

def _ReadLineSlowly(cmd_ev):
  # type: (CommandEvaluator) -> str
  """Read a line from stdin."""
  ch_array = []  # type: List[int]
  while True:
    ch, err_num = pyos.ReadByte(0)

    if ch < 0:
      if err_num == EINTR:
        cmd_ev.RunPendingTraps()
        # retry after running traps
      else:
        # Like the top level IOError handler
        e_die('osh I/O error: %s', posix.strerror(err_num), status=2)

    elif ch == pyos.EOF_SENTINEL:
      break

    else:
      ch_array.append(ch)

    # TODO: Add option to omit newline
    if ch == pyos.NEWLINE_CH:
      break

  return pyutil.ChArrayToString(ch_array)


def _ReadAll():
  # type: () -> str
  """Read all of stdin.

  Similar to command sub in core/executor.py.
  """
  chunks = []  # type: List[str]
  while True:
    n, err_num = pyos.Read(0, 4096, chunks)

    if n < 0:
      if err_num == EINTR:
        # Retry only.  Like read --line (and command sub), read --all doesn't
        # run traps.  It would be a bit weird to run every 4096 bytes.
        pass
      else:
        # Like the top level IOError handler
        e_die('osh I/O error: %s', posix.strerror(err_num), status=2)

    elif n == 0:  # EOF
      break

  return ''.join(chunks)


class Read(vm._Builtin):

  def __init__(self, splitter, mem, parse_ctx, cmd_ev):
    # type: (SplitContext, Mem, ParseContext, CommandEvaluator) -> None
    self.splitter = splitter
    self.mem = mem
    self.parse_ctx = parse_ctx
    self.cmd_ev = cmd_ev
    self.stdin = mylib.Stdin()

  def _Line(self, arg, var_name):
    # type: (arg_types.read, str) -> int
    """For read --line."""

    # Use an optimized C implementation rather than _ReadLineSlowly, which
    # calls ReadByte() over and over.
    line = pyos.ReadLine()
    if len(line) == 0:  # EOF
      return 1

    if not arg.with_eol:
      if line.endswith('\r\n'):
        line = line[:-2]
      elif line.endswith('\n'):
        line = line[:-1]

    # Lines that don't start with a single quote aren't QSN.  They may contain
    # a single quote internally, like:
    #
    # Fool's Gold
    if arg.q and line.startswith("'"):
      arena = self.parse_ctx.arena
      line_reader = reader.StringLineReader(line, arena)
      lexer = self.parse_ctx.MakeLexer(line_reader)

      # The parser only yields valid tokens:
      #     Char_Literals, Char_OneChar, Char_Hex, Char_UBraced
      # So we can use word_compile.EvalCStringToken, which is also used for
      # $''.
      # Important: we don't generate Id.Unknown_Backslash because that is valid
      # in echo -e.  We just make it Id.Unknown_Tok?
      try:
        # TODO: read should know about stdin, and redirects, and pipelines?
        with alloc.ctx_Location(arena, source.Stdin('')):
          tokens = qsn_native.Parse(lexer)
      except error.Parse as e:
        ui.PrettyPrintError(e, arena)
        return 1
      tmp = [word_compile.EvalCStringToken(t) for t in tokens]
      line = ''.join(tmp)

    lhs = lvalue.Named(var_name)
    self.mem.SetValue(lhs, value.Str(line), scope_e.LocalOnly)
    return 0

  def _All(self, var_name):
    # type: (str) -> int
    contents = _ReadAll()

    # No error conditions?

    lhs = lvalue.Named(var_name)
    self.mem.SetValue(lhs, value.Str(contents), scope_e.LocalOnly)
    return 0

  def Run(self, cmd_val):
    # type: (cmd_value__Argv) -> int
    attrs, arg_r = flag_spec.ParseCmdVal('read', cmd_val)
    arg = arg_types.read(attrs.attrs)
    names = arg_r.Rest()

    # Don't respect any of the other options here?  This is buffered I/O.
    if arg.line:  # read --line
      var_name, var_spid = arg_r.Peek2()
      if var_name is None:
        var_name = '_line'
      else:
        if var_name.startswith(':'):  # optional : sigil
          var_name = var_name[1:]
        arg_r.Next()

      next_arg, next_spid = arg_r.Peek2()
      if next_arg is not None:
        raise error.Usage('got extra argument', span_id=next_spid)

      return self._Line(arg, var_name)

    if arg.q:
      e_usage('--qsn can only be used with --line')

    if arg.all:  # read --all
      var_name, var_spid = arg_r.Peek2()
      if var_name is None:
        var_name = '_all'
      else:
        if var_name.startswith(':'):  # optional : sigil
          var_name = var_name[1:]
        arg_r.Next()

      next_arg, next_spid = arg_r.Peek2()
      if next_arg is not None:
        raise error.Usage('got extra argument', span_id=next_spid)

      return self._All(var_name)

    if arg.q:
      e_usage('--qsn not implemented yet')

    fd = self.stdin.fileno()

    if arg.t >= 0.0:
      if arg.t != 0.0:
        e_die("read -t isn't implemented (except t=0)")
      else:
        return 0 if pyos.InputAvailable(fd) else 1

    bits = 0
    if self.stdin.isatty():
      # -d and -n should be unbuffered
      if arg.d is not None or arg.n >= 0:
        bits |= pyos.TERM_ICANON
      if arg.s:  # silent
        bits |= pyos.TERM_ECHO

      if arg.p is not None:  # only if tty
        mylib.Stderr().write(arg.p)

    if bits == 0:
      status = self._Read(arg, names)
    else:
      term = pyos.TermState(fd, ~bits)
      try:
        status = self._Read(arg, names)
      finally:
        term.Restore()
    return status

  def _Read(self, arg, names):
    # type: (arg_types.read, List[str]) -> int

    if arg.n >= 0 :  # read a certain number of bytes (-1 means unset)
      if len(names):
        name = names[0]
      else:
        name = 'REPLY'  # default variable name

      stdin_fd = self.stdin.fileno()
      s = _ReadN(stdin_fd, arg.n, self.cmd_ev)

      state.BuiltinSetString(self.mem, name, s)

      # Did we read all the bytes we wanted?
      return 0 if len(s) == arg.n else 1

    if len(names) == 0:
      names.append('REPLY')

    # leftover words assigned to the last name
    if arg.a is not None:
      max_results = 0  # no max
    else:
      max_results = len(names)

    if arg.Z:  # -0 is synonym for -r -d ''
      raw = True
      delim_byte = 0
    else:
      raw = arg.r
      if arg.d is not None:
        if len(arg.d):
          delim_byte = ord(arg.d[0])
        else:
          delim_byte = 0  # -d '' delimits by NUL
      else:
        delim_byte = pyos.NEWLINE_CH  # read a line

    # We have to read more than one line if there is a line continuation (and
    # it's not -r).
    parts = []  # type: List[mylib.BufWriter]
    join_next = False
    status = 0
    while True:
      line, eof = _ReadUntilDelim(delim_byte, self.cmd_ev)

      if eof:
        # status 1 to terminate loop.  (This is true even though we set
        # variables).
        status = 1

      #log('LINE %r', line)
      if len(line) == 0:
        break

      spans = self.splitter.SplitForRead(line, not raw)
      done, join_next = _AppendParts(line, spans, max_results, join_next, parts)

      #log('PARTS %s continued %s', parts, continued)
      if done:
        break

    entries = [buf.getvalue() for buf in parts]
    num_parts = len(entries)
    if arg.a is not None:
      state.BuiltinSetArray(self.mem, arg.a, entries)
    else:
      for i in xrange(max_results):
        if i < num_parts:
          s = entries[i]
        else:
          s = ''  # if there are too many variables
        var_name = names[i]
        if var_name.startswith(':'):
          var_name = var_name[1:]
        #log('read: %s = %s', var_name, s)
        state.BuiltinSetString(self.mem, var_name, s)

    return status


class MapFile(vm._Builtin):
  """ mapfile / readarray """

  def __init__(self, mem, errfmt, cmd_ev):
    # type: (Mem, ErrorFormatter, CommandEvaluator) -> None
    self.mem = mem
    self.errfmt = errfmt
    self.cmd_ev = cmd_ev

  def Run(self, cmd_val):
    # type: (cmd_value__Argv) -> int
    attrs, arg_r = flag_spec.ParseCmdVal('mapfile', cmd_val)
    arg = arg_types.mapfile(attrs.attrs)

    var_name, _ = arg_r.Peek2()
    if var_name is None:
      var_name = 'MAPFILE'
    else:
     if var_name.startswith(':'):
       var_name = var_name[1:]

    lines = []  # type: List[str]
    while True:
      line = _ReadLineSlowly(self.cmd_ev)
      if len(line) == 0:
        break
      # note: at least on Linux, bash doesn't strip \r\n
      if arg.t and line.endswith('\n'):
        line = line[:-1]
      lines.append(line)

    state.BuiltinSetArray(self.mem, var_name, lines)
    return 0


class Cd(vm._Builtin):
  def __init__(self, mem, dir_stack, cmd_ev, errfmt):
    # type: (Mem, DirStack, CommandEvaluator, ErrorFormatter) -> None
    self.mem = mem
    self.dir_stack = dir_stack
    self.cmd_ev = cmd_ev  # To run blocks
    self.errfmt = errfmt

  def Run(self, cmd_val):
    # type: (cmd_value__Argv) -> int
    attrs, arg_r = flag_spec.ParseCmdVal('cd', cmd_val)
    arg = arg_types.cd(attrs.attrs)

    dest_dir, arg_spid = arg_r.Peek2()
    if dest_dir is None:
      val = self.mem.GetValue('HOME')
      try:
        dest_dir = state.GetString(self.mem, 'HOME')
      except error.Runtime as e:
        self.errfmt.Print_(e.UserErrorString())
        return 1

    if dest_dir == '-':
      try:
        dest_dir = state.GetString(self.mem, 'OLDPWD')
        print(dest_dir)  # Shells print the directory
      except error.Runtime as e:
        self.errfmt.Print_(e.UserErrorString())
        return 1

    try:
      pwd = state.GetString(self.mem, 'PWD')
    except error.Runtime as e:
      self.errfmt.Print_(e.UserErrorString())
      return 1

    # Calculate new directory, chdir() to it, then set PWD to it.  NOTE: We can't
    # call posix.getcwd() because it can raise OSError if the directory was
    # removed (ENOENT.)
    abspath = os_path.join(pwd, dest_dir)  # make it absolute, for cd ..
    if arg.P:
      # -P means resolve symbolic links, then process '..'
      real_dest_dir = libc.realpath(abspath)
    else:
      # -L means process '..' first.  This just does string manipulation.  (But
      # realpath afterward isn't correct?)
      real_dest_dir = os_path.normpath(abspath)

    err_num = pyos.Chdir(real_dest_dir)
    if err_num != 0:
      self.errfmt.Print_("cd %r: %s" % (real_dest_dir, posix.strerror(err_num)),
                         span_id=arg_spid)
      return 1

    state.ExportGlobalString(self.mem, 'PWD', real_dest_dir)

    # WEIRD: We need a copy that is NOT PWD, because the user could mutate PWD.
    # Other shells use global variables.
    self.mem.SetPwd(real_dest_dir)

    block = typed_args.GetOneBlock(cmd_val.typed_args)
    if block:
      self.dir_stack.Push(real_dest_dir)
      try:
        unused = self.cmd_ev.EvalBlock(block)
      finally:  # TODO: Change this to a context manager.
        # note: it might be more consistent to use an exception here.
        if not _PopDirStack(self.mem, self.dir_stack, self.errfmt):
          return 1

    else:  # No block
      state.ExportGlobalString(self.mem, 'OLDPWD', pwd)
      self.dir_stack.Reset()  # for pushd/popd/dirs

    return 0


WITH_LINE_NUMBERS = 1
WITHOUT_LINE_NUMBERS = 2
SINGLE_LINE = 3

def _PrintDirStack(dir_stack, style, home_dir):
  # type: (DirStack, int, Optional[str]) -> None
  """Helper for 'dirs'."""

  if style == WITH_LINE_NUMBERS:
    for i, entry in enumerate(dir_stack.Iter()):
      print('%2d  %s' % (i, ui.PrettyDir(entry, home_dir)))

  elif style == WITHOUT_LINE_NUMBERS:
    for entry in dir_stack.Iter():
      print(ui.PrettyDir(entry, home_dir))

  elif style == SINGLE_LINE:
    parts = [ui.PrettyDir(entry, home_dir) for entry in dir_stack.Iter()]
    s = ' '.join(parts)
    print(s)


class Pushd(vm._Builtin):
  def __init__(self, mem, dir_stack, errfmt):
    # type: (Mem, DirStack, ErrorFormatter) -> None
    self.mem = mem
    self.dir_stack = dir_stack
    self.errfmt = errfmt

  def Run(self, cmd_val):
    # type: (cmd_value__Argv) -> int
    num_args = len(cmd_val.argv) - 1
    if num_args == 0:
      # TODO: It's suppose to try another dir before doing this?
      self.errfmt.Print_('pushd: no other directory')
      return 1
    elif num_args > 1:
      e_usage('got too many arguments')

    # TODO: 'cd' uses normpath?  Is that inconsistent?
    dest_dir = os_path.abspath(cmd_val.argv[1])
    err_num = pyos.Chdir(dest_dir)
    if err_num != 0:
      self.errfmt.Print_("pushd: %r: %s" % (dest_dir, posix.strerror(err_num)),
                         span_id=cmd_val.arg_spids[1])
      return 1

    self.dir_stack.Push(dest_dir)
    _PrintDirStack(self.dir_stack, SINGLE_LINE, state.MaybeString(self.mem, 'HOME'))
    state.ExportGlobalString(self.mem, 'PWD', dest_dir)
    self.mem.SetPwd(dest_dir)
    return 0


def _PopDirStack(mem, dir_stack, errfmt):
  # type: (Mem, DirStack, ErrorFormatter) -> bool
  """Helper for popd and cd { ... }."""
  dest_dir = dir_stack.Pop()
  if dest_dir is None:
    errfmt.Print_('popd: directory stack is empty')
    return False

  err_num = pyos.Chdir(dest_dir)
  if err_num != 0:
    # Happens if a directory is deleted in pushing and popping
    errfmt.Print_("popd: %r: %s" % (dest_dir, posix.strerror(err_num)))
    return False

  state.SetGlobalString(mem, 'PWD', dest_dir)
  mem.SetPwd(dest_dir)
  return True


class Popd(vm._Builtin):
  def __init__(self, mem, dir_stack, errfmt):
    # type: (Mem, DirStack, ErrorFormatter) -> None
    self.mem = mem
    self.dir_stack = dir_stack
    self.errfmt = errfmt

  def Run(self, cmd_val):
    # type: (cmd_value__Argv) -> int
    if len(cmd_val.arg_spids) > 1:
      e_usage('got extra argument', span_id=cmd_val.arg_spids[1])

    if not _PopDirStack(self.mem, self.dir_stack, self.errfmt):
      return 1  # error

    _PrintDirStack(self.dir_stack, SINGLE_LINE, state.MaybeString(self.mem, ('HOME')))
    return 0


class Dirs(vm._Builtin):
  def __init__(self, mem, dir_stack, errfmt):
    # type: (Mem, DirStack, ErrorFormatter) -> None
    self.mem = mem
    self.dir_stack = dir_stack
    self.errfmt = errfmt

  def Run(self, cmd_val):
    # type: (cmd_value__Argv) -> int
    attrs, arg_r = flag_spec.ParseCmdVal('dirs', cmd_val)
    arg = arg_types.dirs(attrs.attrs)

    home_dir = state.MaybeString(self.mem, 'HOME')
    style = SINGLE_LINE

    # Following bash order of flag priority
    if arg.l:
      home_dir = None  # disable pretty ~
    if arg.c:
      self.dir_stack.Reset()
      return 0
    elif arg.v:
      style = WITH_LINE_NUMBERS
    elif arg.p:
      style = WITHOUT_LINE_NUMBERS

    _PrintDirStack(self.dir_stack, style, home_dir)
    return 0


class Pwd(vm._Builtin):
  """
  NOTE: pwd doesn't just call getcwd(), which returns a "physical" dir (not a
  symlink).
  """
  def __init__(self, mem, errfmt):
    # type: (Mem, ErrorFormatter) -> None
    self.mem = mem
    self.errfmt = errfmt

  def Run(self, cmd_val):
    # type: (cmd_value__Argv) -> int
    attrs, arg_r = flag_spec.ParseCmdVal('pwd', cmd_val)
    arg = arg_types.pwd(attrs.attrs)

    # NOTE: 'pwd' will succeed even if the directory has disappeared.  Other
    # shells behave that way too.
    pwd = self.mem.pwd

    # '-L' is the default behavior; no need to check it
    # TODO: ensure that if multiple flags are provided, the *last* one overrides
    # the others
    if arg.P:
      pwd = libc.realpath(pwd)
    print(pwd)
    return 0


# TODO: Need $VERSION inside all pages?

# Needs a different _ResourceLoader to translate
class Help(vm._Builtin):

  def __init__(self, loader, errfmt):
    # type: (_ResourceLoader, ErrorFormatter) -> None
    self.loader = loader
    self.errfmt = errfmt

  def _Groups(self):
    # type: () -> List[str]
    # TODO: cache this?
    contents = self.loader.Get('_devbuild/help/groups.txt')
    groups = contents.splitlines(False)  # no newlines
    return groups

  def Run(self, cmd_val):
    # type: (cmd_value__Argv) -> int

    attrs, arg_r = flag_spec.ParseCmdVal('help', cmd_val)
    #arg = arg_types.help(attrs.attrs)

    topic, blame_spid = arg_r.Peek2()
    if topic is None:
      topic = 'help'
      blame_spid = runtime.NO_SPID
    else:
      arg_r.Next()

    try:
      contents = self.loader.Get('_devbuild/help/%s' % topic)
    except IOError:
      # Notes:
      # 1. bash suggests:
      # man -k zzz
      # info zzz
      # help help
      # We should do something smarter.

      # 2. This also happens on 'build/dev.sh minimal', which isn't quite
      # accurate.  We don't have an exact list of help topics!

      # 3. This is mostly an interactive command.  Is it obnoxious to
      # quote the line of code?
      self.errfmt.Print_('no help topics match %r' % topic,
                         span_id=blame_spid)
      return 1

    print(contents)
    return 0


class Cat(vm._Builtin):
  """Internal implementation detail for $(< file).
  
  Maybe expose this as 'builtin cat' ?
  """
  def __init__(self):
    # type: () -> None
    """Empty constructor for mycpp."""
    vm._Builtin.__init__(self)

  def Run(self, cmd_val):
    # type: (cmd_value__Argv) -> int
    chunks = []  # type: List[str]
    while True:
      n, err_num = pyos.Read(0, 4096, chunks)

      if n < 0:
        if err_num == EINTR:
          pass  # retry
        else:
          # Like the top level IOError handler
          e_die('osh I/O error: %s', posix.strerror(err_num), status=2)

      elif n == 0:  # EOF
        break

      else:
        # Stream it to stdout
        assert len(chunks) == 1
        mylib.Stdout().write(chunks[0])
        chunks.pop()

    return 0
