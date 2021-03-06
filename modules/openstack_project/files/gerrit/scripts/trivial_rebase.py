#!/usr/bin/env python2.6

# Copyright (c) 2010, Code Aurora Forum. All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are
# met:
#    # Redistributions of source code must retain the above copyright
#       notice, this list of conditions and the following disclaimer.
#    # Redistributions in binary form must reproduce the above
#       copyright notice, this list of conditions and the following
#       disclaimer in the documentation and/or other materials provided
#       with the distribution.
#    # Neither the name of Code Aurora Forum, Inc. nor the names of its
#       contributors may be used to endorse or promote products derived
#       from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED "AS IS" AND ANY EXPRESS OR IMPLIED
# WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NON-INFRINGEMENT
# ARE DISCLAIMED.  IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS
# BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR
# BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY,
# WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE
# OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN
# IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

# This script is designed to detect when a patchset uploaded to Gerrit is
# 'identical' (determined via git-patch-id) and reapply reviews onto the new
# patchset from the previous patchset.

# Get usage and help info by running: ./trivial_rebase.py --help
# Documentation is available here: https://www.codeaurora.org/xwiki/bin/QAEP/Gerrit

import json
import subprocess
from sys import exit

from optparse import OptionParser as _realOptionParser, AmbiguousOptionError, \
  BadOptionError
class OptionParser(_realOptionParser):
  """Make OptionParser silently swallow unrecognized options."""
  def _process_args(self, largs, rargs, values):
    while rargs:
      try:
        _realOptionParser._process_args(self, largs, rargs, values)
      except (AmbiguousOptionError, BadOptionError), e:
        largs.append(e.opt_str)

class CheckCallError(OSError):
  """CheckCall() returned non-0."""
  def __init__(self, command, cwd, retcode, stdout, stderr=None):
    OSError.__init__(self, command, cwd, retcode, stdout, stderr)
    self.command = command
    self.cwd = cwd
    self.retcode = retcode
    self.stdout = stdout
    self.stderr = stderr

def CheckCall(command, cwd=None):
  """Like subprocess.check_call() but returns stdout.

  Works on python 2.4
  """
  try:
    process = subprocess.Popen(command, cwd=cwd, stdout=subprocess.PIPE)
    std_out, std_err = process.communicate()
  except OSError, e:
    raise CheckCallError(command, cwd, e.errno, None)
  if process.returncode:
    raise CheckCallError(command, cwd, process.returncode, std_out, std_err)
  return std_out, std_err

def Gssh(options, api_command):
  """Makes a Gerrit API call via SSH and returns the stdout results."""
  ssh_cmd = ['ssh',
             '-l', 'Gerrit Code Review',
             '-p', options.port,
             '-i', options.private_key_path,
             options.server,
             api_command]
  try:
    return CheckCall(ssh_cmd)[0]
  except CheckCallError, e:
    import sys
    err_template = "call: %s\nreturn code: %s\nstdout: %s\nstderr: %s\n"
    sys.stderr.write(err_template%(ssh_cmd, e.retcode, e.stdout, e.stderr))
    raise

def GsqlQuery(sql_query, options):
  """Runs a gerrit gsql query and returns the result"""
  gsql_cmd = "gerrit gsql --format JSON -c %s"%sql_query
  gsql_out = Gssh(options, gsql_cmd)
  new_out = gsql_out.replace('}}\n', '}}\nsplit here\n')
  return new_out.split('split here\n')

def FindPrevRev(options):
  """Finds the revision of the previous patch set on the change"""
  sql_query = ("\"SELECT revision FROM patch_sets,changes WHERE "
               "patch_sets.change_id = changes.change_id AND "
               "patch_sets.patch_set_id = %s AND "
               "changes.change_key = \'%s\'\"" % ((options.patchset - 1),
                                                  options.changeId))
  revisions = GsqlQuery(sql_query, options)

  json_dict = json.loads(revisions[0], strict=False)
  return json_dict["columns"]["revision"]

def GetApprovals(options):
  """Get all the approvals on a specific patch set

  Returns a list of approval dicts"""
  sql_query = ("\"SELECT value,account_id,category_id FROM patch_set_approvals "
               "WHERE patch_set_id = %s AND change_id = (SELECT change_id FROM "
               "changes WHERE change_key = \'%s\') AND value <> 0\""
               % ((options.patchset - 1), options.changeId))
  gsql_out = GsqlQuery(sql_query, options)
  approvals = []
  for json_str in gsql_out:
    dict = json.loads(json_str, strict=False)
    if dict["type"] == "row":
      approvals.append(dict["columns"])
  return approvals

def GetPatchId(revision, consider_whitespace=False):
  git_show_cmd = ['git', 'show', revision]
  patch_id_cmd = ['git', 'patch-id']
  patch_id_process = subprocess.Popen(patch_id_cmd, stdout=subprocess.PIPE,
                                      stdin=subprocess.PIPE)
  git_show_process = subprocess.Popen(git_show_cmd, stdout=subprocess.PIPE)
  if consider_whitespace:
    # This matches on change lines in the patch (those starting with "+"
    # or "-" but not followed by another of the same), then replaces any
    # space or tab characters with "%" before calculating a patch-id.
    replace_ws_cmd = ['sed', r'/^\(+[^+]\|-[^-]\)/y/ \t/%%/']
    replace_ws_process = subprocess.Popen(replace_ws_cmd,
                                          stdout=subprocess.PIPE,
                                          stdin=subprocess.PIPE)
    return patch_id_process.communicate(
        replace_ws_process.communicate(git_show_process.communicate()[0])[0]
        )[0]
  else:
    return patch_id_process.communicate(git_show_process.communicate()[0])[0]

def SuExec(options, as_user, cmd):
  suexec_cmd = "suexec --as %s -- %s"%(as_user, cmd)
  Gssh(options, suexec_cmd)

def DiffCommitMessages(commit1, commit2):
  log_cmd1 = ['git', 'log', '--pretty=format:"%an %ae%n%s%n%b"',
              commit1 + '^!']
  commit1_log = CheckCall(log_cmd1)
  log_cmd2 = ['git', 'log', '--pretty=format:"%an %ae%n%s%n%b"',
              commit2 + '^!']
  commit2_log = CheckCall(log_cmd2)
  if commit1_log != commit2_log:
    return True
  return False

def Main():
  usage = "usage: %prog <required options> [optional options]"
  parser = OptionParser(usage=usage)
  parser.add_option("--change", dest="changeId", help="Change identifier")
  parser.add_option("--project", help="Project path in Gerrit")
  parser.add_option("--commit", help="Git commit-ish for this patchset")
  parser.add_option("--patchset", type="int", help="The patchset number")
  parser.add_option("--role-user", dest="role_user",
                    help="E-mail/ID of user commenting on commit messages")
  parser.add_option("--private-key-path", dest="private_key_path",
                    help="Full path to Gerrit SSH daemon's private host key")
  parser.add_option("--server-port", dest="port", default='29418',
                    help="Port to connect to Gerrit's SSH daemon "
                         "[default: %default]")
  parser.add_option("--server", dest="server", default="localhost",
                    help="Server name/address for Gerrit's SSH daemon "
                         "[default: %default]")
  parser.add_option("--whitespace", action="store_true",
                    help="Treat whitespace as significant")

  (options, args) = parser.parse_args()

  if not options.changeId:
    parser.print_help()
    exit(0)

  if options.patchset == 1:
    # Nothing to detect on first patchset
    exit(0)
  prev_revision = None
  prev_revision = FindPrevRev(options)
  if not prev_revision:
    # Couldn't find a previous revision
    exit(0)
  prev_patch_id = GetPatchId(prev_revision)
  cur_patch_id = GetPatchId(options.commit)
  if cur_patch_id.split()[0] != prev_patch_id.split()[0]:
    # patch-ids don't match
    exit(0)
  # Patch ids match. This is a trivial rebase.
  # In addition to patch-id we should check if whitespace content changed. Some
  # languages are more sensitive to whitespace than others, and some changes
  # may either introduce or be intended to fix style problems specifically
  # involving whitespace as well.
  if options.whitespace:
    prev_patch_ws = GetPatchId(prev_revision, consider_whitespace=True)
    cur_patch_ws = GetPatchId(options.commit, consider_whitespace=True)
    if cur_patch_ws.split()[0] != prev_patch_ws.split()[0]:
      # Insert a comment into the change letting the approvers know only the
      # whitespace changed
      comment_msg = "\"New patchset patch-id matches previous patchset, " \
                     "but whitespace content has changed.\""
      comment_cmd = ['gerrit', 'approve', '--project', options.project,
                     '--message', comment_msg, options.commit]
      SuExec(options, options.role_user, ' '.join(comment_cmd))
      exit(0)

  # We should also check if the commit message changed. Most approvers would
  # want to re-review changes when the commit message changes.
  changed = DiffCommitMessages(prev_revision, options.commit)
  if changed:
    # Insert a comment into the change letting the approvers know only the
    # commit message changed
    comment_msg = "\"New patchset patch-id matches previous patchset, " \
                   "but commit message has changed.\""
    comment_cmd = ['gerrit', 'approve', '--project', options.project,
                   '--message', comment_msg, options.commit]
    SuExec(options, options.role_user, ' '.join(comment_cmd))
    exit(0)

  # Need to get all approvals on prior patch set, then suexec them onto
  # this patchset.
  approvals = GetApprovals(options)
  gerrit_approve_msg = ("\'Automatically re-added by Gerrit trivial rebase "
                        "detection script.\'")
  for approval in approvals:
    # Note: Sites with different 'copy_min_score' values in the
    # approval_categories DB table might want different behavior here.
    # Additional categories should also be added if desired.
    if approval["category_id"] == "CRVW":
      approve_category = '--code-review'
    elif approval["category_id"] == "VRIF":
      # Don't re-add verifies
      #approve_category = '--verified'
      continue
    elif approval["category_id"] == "SUBM":
      # We don't care about previous submit attempts
      continue
    elif approval["category_id"] == "APRV":
      # Similarly squash old approvals
      continue
    else:
      print "Unsupported category: %s" % approval
      exit(0)

    score = approval["value"]
    gerrit_approve_cmd = ['gerrit', 'approve', '--project', options.project,
                          '--message', gerrit_approve_msg, approve_category,
                          score, options.commit]
    SuExec(options, approval["account_id"], ' '.join(gerrit_approve_cmd))
  exit(0)

if __name__ == "__main__":
  Main()
