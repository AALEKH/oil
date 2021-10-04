# Copyright 2016 Andy Chu. All rights reserved.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
"""
ui.py - User interface constructs.
"""
from __future__ import print_function

from _devbuild.gen.id_kind_asdl import Id, Id_t, Id_str
from _devbuild.gen.syntax_asdl import (
    Token, command_t, command_str,
    source_e, source__Stdin, source__MainFile, source__SourcedFile,
    source__Alias, source__LValue, source__Variable, source__ArgvWord
)
from _devbuild.gen.runtime_asdl import value_str, value_t
from asdl import runtime
from asdl import format as fmt
from core.pyutil import stderr_line
from osh import word_
from mycpp import mylib
from mycpp.mylib import tagswitch, NewStr

from typing import List, Optional, cast, Any, TYPE_CHECKING
if TYPE_CHECKING:
  from _devbuild.gen import arg_types
  from core.alloc import Arena
  from core.error import _ErrorWithLocation
  from mycpp.mylib import Writer
  #from frontend.args import UsageError


def ValType(val):
  # type: (value_t) -> str
  """For displaying type errors in the UI."""

  # Displays 'value.MaybeStrArray' for now, maybe change it.
  return NewStr(value_str(val.tag_()))


def CommandType(cmd):
  # type: (command_t) -> str
  """For displaying commands in the UI."""

  # Displays 'value.MaybeStrArray' for now, maybe change it.
  return NewStr(command_str(cmd.tag_()))


def PrettyId(id_):
  # type: (Id_t) -> str
  """For displaying type errors in the UI."""

  # Displays 'Id.BoolUnary_v' for now
  return NewStr(Id_str(id_))


def PrettyToken(tok, arena):
  # type: (Token, Arena) -> str
  """Returns a readable token value for the user.  For syntax errors."""
  if tok.id == Id.Eof_Real:
    return 'EOF'

  span = arena.GetLineSpan(tok.span_id)
  line = arena.GetLine(span.line_id)
  val = line[span.col: span.col + span.length]
  # TODO: Print length 0 as 'EOF'?
  return repr(val)


def PrettyDir(dir_name, home_dir):
  # type: (str, Optional[str]) -> str
  """Maybe replace the home dir with ~.

  Used by the 'dirs' builtin and the prompt evaluator.
  """
  if home_dir is not None:
    if dir_name == home_dir or dir_name.startswith(home_dir + '/'):
      return '~' + dir_name[len(home_dir):]

  return dir_name


def _PrintCodeExcerpt(line, col, length, f):
  # type: (str, int, int, Writer) -> None
  f.write('  '); f.write(line.rstrip())
  f.write('\n  ')
  # preserve tabs
  for c in line[:col]:
    f.write('\t' if c == '\t' else ' ')
  f.write('^')
  f.write('~' * (length-1))
  f.write('\n')


def _PrintWithSpanId(prefix, msg, span_id, arena, f):
  # type: (str, str, int, Arena, Writer) -> None
  line_span = arena.GetLineSpan(span_id)
  orig_col = line_span.col
  line_id = line_span.line_id

  src = arena.GetLineSource(line_id)
  line = arena.GetLine(line_id)
  line_num = arena.GetLineNumber(line_id)  # overwritten by source__LValue case

  # LValue is the only case where we don't print this
  if src.tag_() != source_e.LValue:
    _PrintCodeExcerpt(line, line_span.col, line_span.length, f)

  UP_src = src
  with tagswitch(src) as case:
    # TODO: Use color instead of [ ]
    if case(source_e.Interactive):
      source_str = '[ interactive ]'  # This might need some changes
    elif case(source_e.Headless):
      source_str = '[ headless ]'
    elif case(source_e.CFlag):
      source_str = '[ -c flag ]'
    elif case(source_e.Stdin):
      src = cast(source__Stdin, UP_src)
      source_str = '[ stdin%s ]' % src.comment

    elif case(source_e.MainFile):
      src = cast(source__MainFile, UP_src)
      source_str = src.path
    elif case(source_e.SourcedFile):
      src = cast(source__SourcedFile, UP_src)
      # TODO: could chain of 'source' with the spid
      source_str = src.path  # no [ ]

    elif case(source_e.ArgvWord):
      src = cast(source__ArgvWord, UP_src)
      if src.span_id == runtime.NO_SPID:
        source_str = '[ word at ? ]'
      else:
        span = arena.GetLineSpan(src.span_id)
        line_num = arena.GetLineNumber(span.line_id)
        outer_source = arena.GetLineSourceString(span.line_id)
        source_str = '[ word at line %d of %s ]' % (line_num, outer_source)
      # NOTE: not using _PrintCodeExcerpt

    elif case(source_e.Variable):
      src = cast(source__Variable, UP_src)
      var_name = src.var_name if src.var_name is not None else '?'
      source_str = '[ var %s ]' %  var_name
      # TODO: could point to outer_source if we knew where the variable was
      # assigned

    elif case(source_e.Alias):
      src = cast(source__Alias, UP_src)
      source_str = '[ expansion of alias %r ]' % src.argv0
    elif case(source_e.Backticks):
      #src = cast(source__Backticks, UP_src)
      source_str = '[ backticks at ... ]'
    elif case(source_e.LValue):
      src = cast(source__LValue, UP_src)
      span2 = arena.GetLineSpan(src.left_spid)
      line2 = arena.GetLine(span2.line_id)
      outer_source = arena.GetLineSourceString(span2.line_id)
      source_str = '[ array LValue in %s ]' % outer_source
      # NOTE: The inner line number is always 1 because of reparsing.  We
      # overwrite it with the original span.
      line_num = arena.GetLineNumber(span2.line_id)

      # We want the excerpt to look like this:
      #   a[x+]=1
      #       ^
      # Rather than quoting the internal buffer:
      #   x+
      #     ^
      lbracket_col = span2.col + span2.length
      _PrintCodeExcerpt(line2, orig_col + lbracket_col, 1, f)

    else:
      raise AssertionError()

  # TODO: If the line is blank, it would be nice to print the last non-blank
  # line too?
  f.write('%s:%d: %s%s\n' % (source_str, line_num, prefix, msg))


def _PrintWithOptionalSpanId(prefix, msg, span_id, arena):
  # type: (str, str, int, Arena) -> None
  f = mylib.Stderr()
  if span_id == runtime.NO_SPID:  # When does this happen?
    f.write('[??? no location ???] %s%s\n' % (prefix, msg))
  else:
    _PrintWithSpanId(prefix, msg, span_id, arena, f)


def _pp(err, arena, prefix):
  # type: (_ErrorWithLocation, Arena, str) -> None
  """
  Called by free function PrettyPrintError and method PrettyPrintError.  This
  is a HACK for mycpp translation.  C++ can't find a free function
  PrettyPrintError() when called within a METHOD of the same name.
  """
  msg = err.UserErrorString()
  span_id = word_.SpanIdFromError(err)

  # TODO: Should there be a special span_id of 0 for EOF?  runtime.NO_SPID
  # means there is no location info, but 0 could mean that the location is EOF.
  # So then you query the arena for the last line in that case?
  # Eof_Real is the ONLY token with 0 span, because it's invisible!
  # Well Eol_Tok is a sentinel with a span_id of runtime.NO_SPID.  I think
  # that is OK.
  # Problem: the column for Eof could be useful.

  _PrintWithOptionalSpanId(prefix, msg, span_id, arena)


def PrettyPrintError(err, arena, prefix=''):
  # type: (_ErrorWithLocation, Arena, str) -> None
  """
  Args:
    prefix: in osh/cmd_eval.py we want to print 'fatal'
  """
  _pp(err, arena, prefix)


# TODO:
# - ColorErrorFormatter
# - BareErrorFormatter?  Could just display the foo.sh:37:8: and not quotation.
#
# Are these controlled by a flag?  It's sort of like --comp-ui.  Maybe
# --error-ui.

class ErrorFormatter(object):

  def __init__(self, arena):
    # type: (Arena) -> None
    self.arena = arena
    self.last_spid = runtime.NO_SPID  # last resort for location info
    self.spid_stack = []  # type: List[int]

  # A stack used for the current builtin.  A fallback for UsageError.
  # TODO: Should we have PushBuiltinName?  Then we can have a consistent style
  # like foo.sh:1: (compopt) Not currently executing.

  def PushLocation(self, spid):
    # type: (int) -> None
    #log('%sPushLocation(%d)', '  ' * len(self.spid_stack), spid)
    self.spid_stack.append(spid)

  def PopLocation(self):
    # type: () -> None
    self.spid_stack.pop()
    #log('%sPopLocation -> %d', '  ' * len(self.spid_stack), self.last_spid)

  def CurrentLocation(self):
    # type: () -> int
    if len(self.spid_stack):
      return self.spid_stack[-1]
    else:
      return runtime.NO_SPID

  if mylib.PYTHON:
    def Print(self, msg, *args, **kwargs):
      # type: (str, *Any, **Any) -> None
      """Print a message with a code quotation based on the given span_id."""
      span_id = kwargs.pop('span_id', self.CurrentLocation())
      prefix = kwargs.pop('prefix', '')
      if args:
        msg = msg % args
      _PrintWithOptionalSpanId(prefix, msg, span_id, self.arena)

  def PrefixPrint(self, msg, prefix, span_id=runtime.NO_SPID):
    # type: (str, str, int) -> None
    """Print a hard-coded message with a prefix."""
    _PrintWithOptionalSpanId(prefix, msg, span_id, self.arena)

  def Print_(self, msg, span_id=runtime.NO_SPID):
    # type: (str, int) -> None
    """Print a hard-coded message.

    TODO: Rename this to Print(), and other variants to Printf.
    """
    if span_id == runtime.NO_SPID:
      span_id = self.CurrentLocation()
    _PrintWithOptionalSpanId('', msg, span_id, self.arena)

  def StderrLine(self, msg):
    # type: (str) -> None
    stderr_line(msg)

  def PrettyPrintError(self, err, prefix=''):
    # type: (_ErrorWithLocation, str) -> None
    """Print an exception that was caught."""
    _pp(err, self.arena, prefix)


def PrintAst(node, flag):
  # type: (command_t, arg_types.main) -> None

  if flag.ast_format == 'none':
    stderr_line('AST not printed.')
    if 0:
      from _devbuild.gen.id_kind_asdl import Id_str
      from frontend.lexer import ID_HIST
      for id_, count in ID_HIST.most_common(10):
        print('%8d %s' % (count, Id_str(id_)))
      print()
      total = sum(ID_HIST.values())
      print('%8d total tokens returned' % total)

  else:  # text output
    f = mylib.Stdout()

    afmt = flag.ast_format  # note: mycpp rewrite to avoid 'in'
    if afmt in ('text', 'abbrev-text'):
      ast_f = fmt.DetectConsoleOutput(f)
    elif afmt in ('html', 'abbrev-html'):
      ast_f = fmt.HtmlOutput(f)
    else:
      raise AssertionError()

    if 'abbrev-' in afmt:
      tree = node.AbbreviatedTree()
    else:
      tree = node.PrettyTree()

    ast_f.FileHeader()
    fmt.PrintTree(tree, ast_f)
    ast_f.FileFooter()
    ast_f.write('\n')
