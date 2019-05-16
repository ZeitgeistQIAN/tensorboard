# Copyright 2019 The TensorFlow Authors. All Rights Reserved.
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
# ==============================================================================
"""Self-diagnosis script. Run this if your environment is broken."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

# This script may only depend on the Python standard library. It is not
# built with Bazel and should not assume any third-party dependencies.
import collections
import errno
import functools
import logging
import os
import pipes
import shlex
import socket
import subprocess
import sys
import tempfile
import textwrap
import traceback


# A *check* is a function (of no arguments) that performs a diagnostic,
# writes log messages, and optionally yields suggestions.
CHECKS = []


# A suggestion to the end user.
#   headline (str): A short description, like "Turn it off and on
#     again". Should be imperative with no trailing punctuation. May
#     contain Markdown.
#   description (str): A full enumeration of the steps that the user
#     should take to accept the suggestion. Within this string, prose
#     should be formatted with `reflow`. May contain Markdown.
Suggestion = collections.namedtuple("Suggestion", ("headline", "description"))


# Register a function as a check.
def check(fn):
  """Register a function as a check.

  Checks are run in the order in which they are registered.

  Args:
    fn: A function that takes no arguments and either returns `None` or
      returns a generator of `Suggestion`s. (The ability to return
      `None` is to work around the awkwardness of defining empty
      generator functions in Python.)

  Returns:
    A wrapped version of `fn` that returns a generator of `Suggestion`s.
  """
  @functools.wraps(fn)
  def wrapper():
    result = fn()
    return iter(()) if result is None else result
  CHECKS.append(wrapper)
  return wrapper


def reflow(paragraph):
  return textwrap.fill(textwrap.dedent(paragraph).strip())


def pip(args):
  """Invoke command-line Pip with the specified args.

  Returns:
    A bytestring containing the output of Pip.
  """
  # Suppress the Python 2.7 deprecation warning.
  PYTHONWARNINGS_KEY = "PYTHONWARNINGS"
  old_pythonwarnings = os.environ.get(PYTHONWARNINGS_KEY)
  new_pythonwarnings = "%s%s" % (
      "ignore:DEPRECATION",
      ",%s" % old_pythonwarnings if old_pythonwarnings else "",
  )
  command = [sys.executable, "-m", "pip"]
  command.extend(args)
  try:
    os.environ[PYTHONWARNINGS_KEY] = new_pythonwarnings
    return subprocess.check_output(command)
  finally:
    if old_pythonwarnings is None:
      del os.environ[PYTHONWARNINGS_KEY]
    else:
      os.environ[PYTHONWARNINGS_KEY] = old_pythonwarnings


def which(name):
  """Return the path to a binary, or `None` if it's not on the path.

  Returns:
    A bytestring.
  """
  binary = "where" if os.name == "nt" else "which"
  try:
    return subprocess.check_output([binary, name])
  except subprocess.CalledProcessError:
    return None


@check
def general():
  logging.info("sys.version_info: %s", sys.version_info)
  logging.info("os.name: %s", os.name)
  na = type("N/A", (object,), {"__repr__": lambda self: "N/A"})
  logging.info("os.uname(): %r", getattr(os, "uname", na)(),)
  logging.info(
      "sys.getwindowsversion(): %r",
      getattr(sys, "getwindowsversion", na)(),
  )


@check
def package_management():
  conda_meta = os.path.join(sys.prefix, "conda-meta")
  logging.info("has conda-meta: %s", os.path.exists(conda_meta))
  logging.info("$VIRTUAL_ENV: %r", os.environ.get("VIRTUAL_ENV"))


@check
def installed_packages():
  freeze = pip(["freeze", "--all"]).decode("utf-8").splitlines()
  packages = {line.split(u"==")[0]: line for line in freeze}
  packages_set = frozenset(packages)

  # For each of the following families, expect exactly one package to be
  # installed.
  expect_unique = [
      frozenset([
          u"tensorboard",
          u"tb-nightly",
      ]),
      frozenset([
          u"tensorflow",
          u"tensorflow-gpu",
          u"tf-nightly",
          u"tf-nightly-2.0-preview",
          u"tf-nightly-gpu",
          u"tf-nightly-gpu-2.0-preview",
      ]),
      frozenset([
          u"tensorflow-estimator",
          u"tensorflow-estimator-2.0-preview",
          u"tf-estimator-nightly",
      ]),
  ]

  found_conflict = False
  for family in expect_unique:
    actual = family & packages_set
    for package in actual:
      logging.info(u"installed: %s" % (packages[package],))
    if len(actual) == 0:
      logging.warn(u"no installation among: %s" % (sorted(family),))
    elif len(actual) > 1:
      logging.warn(u"conflicting installations: %s" % (sorted(actual),))
      found_conflict = True

  if found_conflict:
    preamble = reflow(
        """
        Conflicting package installations found. Depending on the order
        of installations and uninstallations, behavior may be undefined.
        Please uninstall ALL versions of TensorFlow and TensorBoard,
        then reinstall ONLY the desired version of TensorFlow, which
        will transitively pull in the proper version of TensorBoard. (If
        you use TensorBoard without TensorFlow, just reinstall the
        appropriate version of TensorBoard directly.)
        """
    )
    packages_to_uninstall = sorted(
        frozenset().union(*expect_unique) & packages_set
    )
    commands = [
        "pip uninstall %s" % " ".join(packages_to_uninstall),
        "pip install tensorflow  # or `tensorflow-gpu`, or `tf-nightly`, ...",
    ]
    message = "%s\n\nNamely:\n\n%s" % (
        preamble,
        "\n".join("\t%s" % c for c in commands),
    )
    yield Suggestion("Fix conflicting installations", message)


@check
def tensorboard_python_version():
  from tensorboard import version
  logging.info("tensorboard.version.VERSION: %r", version.VERSION)


@check
def tensorflow_python_version():
  import tensorflow as tf
  logging.info("tensorflow.__version__: %r", tf.__version__)
  logging.info("tensorflow.__git_version__: %r", tf.__git_version__)


@check
def tensorboard_binary_path():
  logging.info("which tensorboard: %r", which("tensorboard"))


@check
def readable_fqdn():
  # May raise `UnicodeDecodeError` for non-ASCII hostnames:
  # https://github.com/tensorflow/tensorboard/issues/682
  try:
    logging.info("socket.getfqdn(): %r", socket.getfqdn())
  except UnicodeDecodeError as e:
    try:
      binary_hostname = subprocess.check_output(["hostname"]).strip()
    except subprocess.CalledProcessError:
      binary_hostname = b"<unavailable>"
    is_non_ascii = not all(
        0x20 <= (ord(c) if not isinstance(c, int) else c) <= 0x7E  # Python 2
        for c in binary_hostname
    )
    if is_non_ascii:
      message = reflow(
          """
          Your computer's hostname, %r, contains bytes outside of the
          printable ASCII range. Some versions of Python have trouble
          working with such names (https://bugs.python.org/issue26227).
          Consider changing to a hostname that only contains printable
          ASCII bytes.
          """ % (binary_hostname,)
      )
      yield Suggestion("Use an ASCII hostname", message)
    else:
      message = reflow(
          """
          Python can't read your computer's hostname, %r. This can occur
          if the hostname contains non-ASCII bytes
          (https://bugs.python.org/issue26227). Consider changing your
          hostname, rebooting your machine, and rerunning this diagnosis
          script to see if the problem is resolved.
          """ % (binary_hostname,)
      )
      yield Suggestion("Use a simpler hostname", message)
    raise e


@check
def stat_tensorboardinfo():
  # We don't use `manager._get_info_dir`, because (a) that requires
  # TensorBoard, and (b) that creates the directory if it doesn't exist.
  path = os.path.join(tempfile.gettempdir(), ".tensorboard-info")
  logging.info("directory: %s", path)
  try:
    stat_result = os.stat(path)
  except OSError as e:
    if e.errno == errno.ENOENT:
      # No problem; this is just fine.
      logging.info(".tensorboard-info directory does not exist")
      return
    else:
      raise
  logging.info("os.stat(...): %r", stat_result)
  logging.info("mode: 0o%o", stat_result.st_mode)
  if stat_result.st_mode & 0o777 != 0o777:
    preamble = reflow(
        """
        The ".tensorboard-info" directory was created by an old version
        of TensorBoard, and its permissions are not set correctly; see
        issue #2010. Change that directory to be world-accessible (may
        require superuser privilege):
        """
    )
    # This error should only appear on Unices, so it's okay to use
    # Unix-specific utilities and shell syntax.
    quote = getattr(shlex, "quote", None) or pipes.quote  # Python <3.3
    command = "chmod 777 %s" % quote(path)
    message = "%s\n\n\t%s" % (preamble, command)
    yield Suggestion("Fix permissions on \"%s\"" % path, message)


# Prefer to include this check last, as its output is long.
@check
def full_pip_freeze():
  logging.info("pip freeze --all:\n%s", pip(["freeze", "--all"]).decode("utf-8"))


def set_up_logging():
  # Manually install handlers to prevent TensorFlow from stomping the
  # default configuration if it's imported:
  # https://github.com/tensorflow/tensorflow/issues/28147
  logger = logging.getLogger()
  logger.setLevel(logging.INFO)
  handler = logging.StreamHandler(sys.stdout)
  handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
  logger.addHandler(handler)


def main():
  set_up_logging()

  print("### Diagnostics")
  print()

  markdown_ticks = "``````"  # seems likely to be sufficient
  print(markdown_ticks)
  suggestions = []
  for check in CHECKS:
    print("--- check: %s" % check.__name__)
    try:
      suggestions.extend(check())
    except Exception:
      traceback.print_exc(file=sys.stdout)
      pass
  print(markdown_ticks)
  print()
  print("End of diagnostics.")

  for suggestion in suggestions:
    print()
    print("### Suggestion: %s" % suggestion.headline)
    print()
    print(suggestion.description)

  print()
  print("### Next steps")
  print()
  if suggestions:
    print(reflow(
        """
        Please try each suggestion enumerated above to determine whether
        it solves your problem. If none of these suggestions works,
        please copy ALL of the above output, including the lines
        containing only backticks, into your GitHub issue or comment. Be
        sure to redact any sensitive information.
        """
    ))
  else:
    print(reflow(
        """
        No action items identified. Please copy ALL of the above output,
        including the lines containing only backticks, into your GitHub
        issue or comment. Be sure to redact any sensitive information.
        """
    ))


if __name__ == "__main__":
  main()
