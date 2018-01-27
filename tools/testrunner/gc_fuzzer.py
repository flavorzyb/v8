#!/usr/bin/env python
#
# Copyright 2017 the V8 project authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.


from os.path import join
import itertools
import json
import math
import multiprocessing
import os
import random
import shlex
import sys
import time

# Adds testrunner to the path hence it has to be imported at the beggining.
import base_runner

from testrunner.local import execution
from testrunner.local import progress
from testrunner.local import testsuite
from testrunner.local import utils
from testrunner.local import verbose
from testrunner.objects import context


DEFAULT_SUITES = ["mjsunit", "webkit", "benchmarks"]
TIMEOUT_DEFAULT = 60

# Double the timeout for these:
SLOW_ARCHS = ["arm",
              "mipsel"]


class GCFuzzer(base_runner.BaseTestRunner):
  def __init__(self, *args, **kwargs):
    super(GCFuzzer, self).__init__(*args, **kwargs)

    self.fuzzer_rng = None

  def _add_parser_options(self, parser):
    parser.add_option("--command-prefix",
                      help="Prepended to each shell command used to run a test",
                      default="")
    parser.add_option("--coverage", help=("Exponential test coverage "
                      "(range 0.0, 1.0) - 0.0: one test, 1.0 all tests (slow)"),
                      default=0.4, type="float")
    parser.add_option("--coverage-lift", help=("Lifts test coverage for tests "
                      "with a low memory size reached (range 0, inf)"),
                      default=20, type="int")
    parser.add_option("--dump-results-file", help="Dump maximum limit reached")
    parser.add_option("--extra-flags",
                      help="Additional flags to pass to each test command",
                      default="")
    parser.add_option("--isolates", help="Whether to test isolates",
                      default=False, action="store_true")
    parser.add_option("-j", help="The number of parallel tasks to run",
                      default=0, type="int")
    parser.add_option("-p", "--progress",
                      help=("The style of progress indicator"
                            " (verbose, dots, color, mono)"),
                      choices=progress.PROGRESS_INDICATORS.keys(),
                      default="mono")
    parser.add_option("-t", "--timeout", help="Timeout in seconds",
                      default= -1, type="int")
    parser.add_option("--random-seed", default=0,
                      help="Default seed for initializing random generator")
    parser.add_option("--fuzzer-random-seed", default=0,
                      help="Default seed for initializing fuzzer random "
                      "generator")
    parser.add_option("--stress-compaction", default=False, action="store_true",
                      help="Enable stress_compaction_random flag")
    parser.add_option("--stress-gc", default=False, action="store_true",
                      help="Enable stress-gc-interval flag")
    parser.add_option("--stress-marking", default=False, action="store_true",
                      help="Enable stress-marking flag")
    parser.add_option("--stress-scavenge", default=False, action="store_true",
                      help="Enable stress-scavenge flag")

    parser.add_option("--distribution-factor1", help="DEPRECATED")
    parser.add_option("--distribution-factor2", help="DEPRECATED")
    parser.add_option("--distribution-mode", help="DEPRECATED")
    parser.add_option("--seed", help="DEPRECATED")
    return parser


  def _process_options(self, options):
    # Special processing of other options, sorted alphabetically.
    options.command_prefix = shlex.split(options.command_prefix)
    options.extra_flags = shlex.split(options.extra_flags)
    if options.j == 0:
      options.j = multiprocessing.cpu_count()
    while options.random_seed == 0:
      options.random_seed = random.SystemRandom().randint(-2147483648,
                                                          2147483647)
    while options.fuzzer_random_seed == 0:
      options.fuzzer_random_seed = random.SystemRandom().randint(-2147483648,
                                                                 2147483647)
    self.fuzzer_rng = random.Random(options.fuzzer_random_seed)
    return True

  def _calculate_n_tests(self, m, options):
    """Calculates the number of tests from m points with exponential coverage.
    The coverage is expected to be between 0.0 and 1.0.
    The 'coverage lift' lifts the coverage for tests with smaller m values.
    """
    c = float(options.coverage)
    l = float(options.coverage_lift)
    return int(math.pow(m, (m * c + l) / (m + l)))

  def _get_default_suite_names(self):
    return DEFAULT_SUITES

  def _do_execute(self, suites, args, options):
    print(">>> Running tests for %s.%s" % (self.build_config.arch,
                                           self.mode_name))

    # Populate context object.
    timeout = options.timeout
    if timeout == -1:
      # Simulators are slow, therefore allow a longer default timeout.
      if self.build_config.arch in SLOW_ARCHS:
        timeout = 2 * TIMEOUT_DEFAULT;
      else:
        timeout = TIMEOUT_DEFAULT;

    timeout *= self.mode_options.timeout_scalefactor
    ctx = context.Context(self.build_config.arch,
                          self.mode_options.execution_mode,
                          self.outdir,
                          self.mode_options.flags, options.verbose,
                          timeout, options.isolates,
                          options.command_prefix,
                          options.extra_flags,
                          False,  # Keep i18n on by default.
                          options.random_seed,
                          True,  # No sorting of test cases.
                          0,  # Don't rerun failing tests.
                          0,  # No use of a rerun-failing-tests maximum.
                          False,  # No no_harness mode.
                          False,  # Don't use perf data.
                          False)  # Coverage not supported.

    num_tests = self._load_tests(args, options, suites, ctx)
    if num_tests == 0:
      print "No tests to run."
      return 0

    test_backup = dict(map(lambda s: (s, s.tests), suites))

    print('>>> Collection phase')
    for s in suites:
      analysis_flags = ['--fuzzer-gc-analysis']
      s.tests = map(lambda t: t.create_variant(t.variant, analysis_flags,
                                               'analysis'),
                    s.tests)
      for t in s.tests:
        t.cmd = t.get_command(ctx)

    progress_indicator = progress.PROGRESS_INDICATORS[options.progress]()
    runner = execution.Runner(suites, progress_indicator, ctx)
    exit_code = runner.Run(options.j)

    print('>>> Analysis phase')
    test_results = dict()
    for s in suites:
      for t in s.tests:
        # Skip failed tests.
        if t.output_proc.has_unexpected_output(runner.outputs[t]):
          print '%s failed, skipping' % t.path
          continue
        max_limits = self._get_max_limits_reached(runner.outputs[t])
        if max_limits:
          test_results[t.path] = max_limits

    runner = None

    if options.dump_results_file:
      with file("%s.%d.txt" % (options.dump_results_file, time.time()),
                "w") as f:
        f.write(json.dumps(test_results))

    num_tests = 0
    for s in suites:
      s.tests = []
      for t in test_backup[s]:
        results = test_results.get(t.path)
        if not results:
          continue
        max_marking, max_new_space, max_allocations = results

        # Only when combining the flags, make sure one has a minimum of 1 if
        # also the other is >=1. Otherwise we might skip it below.
        if options.stress_marking and options.stress_scavenge:
          if max_new_space:
            max_marking = max(max_marking, 1)
          if max_marking:
            max_new_space = max(max_new_space, 1)

        # Make as many subtests as determined by the most dominant factor
        # (one of marking or new space).
        subtests_count = 1
        base_flags = []
        if options.stress_marking:
          if not max_marking:
            # Skip 0 as it switches off the flag.
            continue
          subtests_count = max(
              subtests_count, self._calculate_n_tests(max_marking, options))
          base_flags += ['--stress_marking', str(max_marking)]
        if options.stress_scavenge:
          if not max_new_space:
            # Skip 0 as it switches off the flag.
            continue
          # Divide by 5, since new space is more dominating than marking.
          subtests_count = max(
              subtests_count, self._calculate_n_tests(
                  max(1, max_new_space / 5), options))
          base_flags += ['--stress_scavenge', str(max_new_space)]
        if options.stress_gc:
          # Only makes sense in combination with other flags, since we always
          # reach our upper limit of 5000.
          base_flags += ['--random-gc-interval', str(max_allocations)]
        if options.stress_compaction:
          base_flags.append('--stress_compaction_random')

        for i in xrange(0, subtests_count):
          fuzzing_flags = [
            '--fuzzer_random_seed', str(self._next_fuzzer_seed()),
          ] + base_flags
          s.tests.append(t.create_variant(t.variant, fuzzing_flags, i))
      for t in s.tests:
        t.cmd = t.get_command(ctx)
      num_tests += len(s.tests)

    if num_tests == 0:
      print "No tests to run."
      return exit_code

    print(">>> Fuzzing phase (%d test cases)" % num_tests)
    progress_indicator = progress.PROGRESS_INDICATORS[options.progress]()
    runner = execution.Runner(suites, progress_indicator, ctx)

    return runner.Run(options.j) or exit_code

  def _load_tests(self, args, options, suites, ctx):
    # Find available test suites and read test cases from them.
    variables = {
      "arch": self.build_config.arch,
      "asan": self.build_config.asan,
      "byteorder": sys.byteorder,
      "dcheck_always_on": self.build_config.dcheck_always_on,
      "deopt_fuzzer": False,
      "gc_fuzzer": True,
      "gc_stress": True,
      "gcov_coverage": self.build_config.gcov_coverage,
      "isolates": options.isolates,
      "mode": self.mode_options.status_mode,
      "msan": self.build_config.msan,
      "no_harness": False,
      "no_i18n": self.build_config.no_i18n,
      "no_snap": self.build_config.no_snap,
      "novfp3": False,
      "predictable": self.build_config.predictable,
      "simulator": utils.UseSimulator(self.build_config.arch),
      "simulator_run": False,
      "system": utils.GuessOS(),
      "tsan": self.build_config.tsan,
      "ubsan_vptr": self.build_config.ubsan_vptr,
    }

    num_tests = 0
    test_id = 0
    for s in suites:
      s.ReadStatusFile(variables)
      s.ReadTestCases(ctx)
      if len(args) > 0:
        s.FilterTestCasesByArgs(args)
      s.FilterTestCasesByStatus(False)

      num_tests += len(s.tests)
      for t in s.tests:
        t.id = test_id
        test_id += 1

    return num_tests

  # Parses test stdout and returns what was the highest reached percent of the
  # incremental marking limit (0-100), new space size (0-100) and allocations
  # (6-5000).
  @staticmethod
  def _get_max_limits_reached(output):
    """Returns: list [max marking, max new space, allocations]"""
    if not output.stdout:
      return None

    results = [0, 0, 0]
    for l in reversed(output.stdout.splitlines()):
      if l.startswith('### Maximum marking limit reached ='):
        results[0] = float(l.split()[6])
      elif l.startswith('### Maximum new space size reached ='):
        results[1] = float(l.split()[7])
      elif l.startswith('### Allocations ='):
        # Also remove the comma in the end after split.
        results[2] = int(l.split()[3][:-1])
      if all(results):
        break

    if any(results):
      return (
          max(0, int(results[0])),
          max(0, int(results[1])),
          min(5000, max(6, results[2])),
      )
    return None

  def _next_fuzzer_seed(self):
    fuzzer_seed = None
    while not fuzzer_seed:
      fuzzer_seed = self.fuzzer_rng.randint(-2147483648, 2147483647)
    return fuzzer_seed


if __name__ == '__main__':
  sys.exit(GCFuzzer().execute())