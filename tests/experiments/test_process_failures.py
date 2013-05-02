#!/usr/bin/env python
# Copyright (c) 2012 Cloudera, Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Impala process failure test suite

import logging
import sys
import pytest
import time
import os
from random import choice
from subprocess import call
from tests.common.impala_test_suite import ImpalaTestSuite
from tests.common.impala_cluster import ImpalaCluster
from time import sleep

IMPALA_HOME = os.environ['IMPALA_HOME']
CLUSTER_SIZE = 3
# The exact query doesn't matter much for these tests, just want a query that touches
# data on all nodes.
QUERY = "select count(l_comment) from lineitem"

# Validates killing and restarting impalad processes between query executions
class TestProcessFailures(ImpalaTestSuite):
  @classmethod
  def get_workload(cls):
    return 'tpch'

  @classmethod
  def add_test_dimensions(cls):
    super(TestProcessFailures, cls).add_test_dimensions()
    cls.TestMatrix.add_constraint(lambda v:\
        v.get_value('table_format').file_format == 'text' and
        v.get_value('table_format').compression_codec == 'none')
    cls.TestMatrix.add_constraint(lambda v:\
        v.get_value('exec_option')['batch_size'] == 0 and\
        v.get_value('exec_option')['disable_codegen'] == False and\
        v.get_value('exec_option')['num_nodes'] == 0)

  def setup_class(cls):
    # No-op, but needed to override base class setup which is not wanted in this
    # case.
    pass

  def teardown_class(cls):
    pass

  def setup_method(self, method):
    # Start a clean new cluster before each test
    self.__start_impala_cluster()
    sleep(3)
    self.cluster = ImpalaCluster()
    statestored = self.cluster.statestored
    statestored.service.wait_for_live_backends(CLUSTER_SIZE, timeout=15)
    for impalad in self.cluster.impalads:
      impalad.service.wait_for_num_known_live_backends(CLUSTER_SIZE, timeout=30)

  @classmethod
  def __stop_impala_cluster(cls):
    # TODO: Figure out a better way to handle case where processes are just starting
    # / cleaning up so that sleeps are not needed.
    sleep(2)
    call([os.path.join(IMPALA_HOME, 'bin/start-impala-cluster.py'), '--kill_only'])
    sleep(2)

  @classmethod
  def __start_impala_cluster(cls):
    call([os.path.join(IMPALA_HOME, 'bin/start-impala-cluster.py'),
        '--wait', '--impalad_args="-logtostderr=1"', '-s %d' % CLUSTER_SIZE])

  @pytest.mark.execute_serially
  def test_restart_coordinator(self, vector):
    """Restarts the coordinator between queries"""
    impalad = self.cluster.get_any_impalad()
    client = impalad.service.create_beeswax_client()

    self.execute_query_using_client(client, QUERY, vector)

    statestored = self.cluster.statestored
    impalad.restart()
    statestored.service.wait_for_live_backends(CLUSTER_SIZE, timeout=15)

    # Reconnect
    client = impalad.service.create_beeswax_client()
    self.execute_query_using_client(client, QUERY, vector)

  @pytest.mark.execute_serially
  def test_restart_statestore(self, vector):
    """Tests the cluster still functions when the statestore dies"""
    impalad = self.cluster.get_any_impalad()
    client = impalad.service.create_beeswax_client()
    statestored = self.cluster.statestored
    statestored.kill()
    impalad.service.wait_for_metric_value(
        'statestore-subscriber.connected', 0, timeout=30)
    self.execute_query_using_client(client, QUERY, vector)
    # Reconnect
    statestored.start()

    impalad.service.wait_for_metric_value(
        'statestore-subscriber.connected', 1, timeout=30)
    statestored.service.wait_for_live_backends(CLUSTER_SIZE, timeout=15)
    self.execute_query_using_client(client, QUERY, vector)

  @pytest.mark.execute_serially
  def test_kill_restart_worker(self, vector):
    """Verifies a worker is able to be killed"""
    impalad = self.cluster.get_any_impalad()
    client = impalad.service.create_beeswax_client()
    self.execute_query_using_client(client, QUERY, vector)

    # select a different impalad and restart it
    worker_impalad = self.cluster.get_different_impalad(impalad)
    print "Coordinator impalad: %s Worker impalad: %s" % (impalad, worker_impalad)

    statestored = self.cluster.statestored
    worker_impalad.kill()

    # First wait until the the statestore realizes the impalad has gone down.
    statestored.service.wait_for_live_backends(CLUSTER_SIZE - 1, timeout=30)
    # Wait until the impalad registers another instance went down.
    impalad.service.wait_for_num_known_live_backends(CLUSTER_SIZE - 1, timeout=30)

    # Should work fine even if a worker is down.
    self.execute_query_using_client(client, QUERY, vector)

    # Bring the worker back online and validate queries still work.
    worker_impalad.start()
    statestored.service.wait_for_live_backends(CLUSTER_SIZE, timeout=30)
    self.execute_query_using_client(client, QUERY, vector)

  @pytest.mark.execute_serially
  def test_restart_cluster(self, vector):
    """Restarts all the impalads and runs a query"""
    impalad = self.cluster.get_any_impalad()
    client = impalad.service.create_beeswax_client()
    self.execute_query_using_client(client, QUERY, vector)
    for impalad_proc in self.cluster.impalads:
      impalad_proc.restart()

    statestored = self.cluster.statestored
    statestored.service.wait_for_live_backends(CLUSTER_SIZE, timeout=30)

    # Should be able to the query against any node.
    for impalad in self.cluster.impalads:
      client = impalad.service.create_beeswax_client()
      self.execute_query_using_client(client, QUERY, vector)