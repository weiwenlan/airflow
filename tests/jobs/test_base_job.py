#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
from __future__ import annotations

import datetime
import sys
from unittest.mock import ANY, Mock, patch

from pytest import raises
from sqlalchemy.exc import OperationalError

from airflow.executors.sequential_executor import SequentialExecutor
from airflow.jobs.job import Job, most_recent_job
from airflow.listeners.listener import get_listener_manager
from airflow.utils import timezone
from airflow.utils.session import create_session
from airflow.utils.state import State
from tests.listeners import lifecycle_listener
from tests.test_utils.config import conf_vars
from tests.utils.test_helpers import MockJobRunner


class TestJob:
    def test_state_success(self):
        job = Job(job_runner=MockJobRunner())
        job.run()

        assert job.state == State.SUCCESS
        assert job.end_date is not None

    def test_state_sysexit(self):
        import sys

        job = Job(job_runner=MockJobRunner(lambda: sys.exit(0)))
        job.run()

        assert job.state == State.SUCCESS
        assert job.end_date is not None

    def test_base_job_respects_plugin_hooks(self):

        import sys

        job = Job(job_runner=MockJobRunner(lambda: sys.exit(0)))
        job.run()

        assert job.state == State.SUCCESS
        assert job.end_date is not None

    def test_base_job_respects_plugin_lifecycle(self, dag_maker):
        """
        Test if DagRun is successful, and if Success callbacks is defined, it is sent to DagFileProcessor.
        """
        get_listener_manager().add_listener(lifecycle_listener)

        job = Job(job_runner=MockJobRunner(lambda: sys.exit(0)))
        job.run()

        assert lifecycle_listener.started_component is job
        assert lifecycle_listener.stopped_component is job

    def test_state_failed(self):
        def abort():
            raise RuntimeError("fail")

        job = Job(job_runner=MockJobRunner(abort))
        with raises(RuntimeError):
            job.run()

        assert job.state == State.FAILED
        assert job.end_date is not None

    def test_most_recent_job(self):
        with create_session() as session:
            old_job = Job(job_runner=MockJobRunner(), heartrate=10)
            old_job.latest_heartbeat = old_job.latest_heartbeat - datetime.timedelta(seconds=20)
            job = Job(job_runner=MockJobRunner(), heartrate=10)
            session.add(job)
            session.add(old_job)
            session.flush()

            assert most_recent_job(MockJobRunner.job_type, session=session) == job
            assert old_job.most_recent_job(session=session) == job

            session.rollback()

    def test_most_recent_job_running_precedence(self):
        with create_session() as session:
            old_running_state_job = Job(job_runner=MockJobRunner(), heartrate=10)
            old_running_state_job.latest_heartbeat = timezone.utcnow()
            old_running_state_job.state = State.RUNNING
            new_failed_state_job = Job(job_runner=MockJobRunner(), heartrate=10)
            new_failed_state_job.latest_heartbeat = timezone.utcnow()
            new_failed_state_job.state = State.FAILED
            new_null_state_job = Job(job_runner=MockJobRunner(), heartrate=10)
            new_null_state_job.latest_heartbeat = timezone.utcnow()
            new_null_state_job.state = None
            session.add(old_running_state_job)
            session.add(new_failed_state_job)
            session.add(new_null_state_job)
            session.flush()

            assert most_recent_job(MockJobRunner.job_type, session=session) == old_running_state_job

            session.rollback()

    def test_is_alive(self):
        job = Job(job_runner=MockJobRunner(), heartrate=10, state=State.RUNNING)
        assert job.is_alive() is True

        job.latest_heartbeat = timezone.utcnow() - datetime.timedelta(seconds=20)
        assert job.is_alive() is True

        job.latest_heartbeat = timezone.utcnow() - datetime.timedelta(seconds=21)
        assert job.is_alive() is False

        # test because .seconds was used before instead of total_seconds
        # internal repr of datetime is (days, seconds)
        job.latest_heartbeat = timezone.utcnow() - datetime.timedelta(days=1)
        assert job.is_alive() is False

        job.state = State.SUCCESS
        job.latest_heartbeat = timezone.utcnow() - datetime.timedelta(seconds=10)
        assert job.is_alive() is False, "Completed jobs even with recent heartbeat should not be alive"

    @patch("airflow.jobs.job.create_session")
    def test_heartbeat_failed(self, mock_create_session):
        when = timezone.utcnow() - datetime.timedelta(seconds=60)
        with create_session() as session:
            mock_session = Mock(spec_set=session, name="MockSession")
            mock_create_session.return_value.__enter__.return_value = mock_session

            job = Job(job_runner=MockJobRunner(), heartrate=10, state=State.RUNNING)
            job.latest_heartbeat = when

            mock_session.commit.side_effect = OperationalError("Force fail", {}, None)

            job.heartbeat()

            assert job.latest_heartbeat == when, "attribute not updated when heartbeat fails"

    @conf_vars(
        {
            ("scheduler", "max_tis_per_query"): "100",
            ("core", "executor"): "SequentialExecutor",
        }
    )
    @patch("airflow.jobs.job.ExecutorLoader.get_default_executor")
    @patch("airflow.jobs.job.get_hostname")
    @patch("airflow.jobs.job.getuser")
    def test_essential_attr(self, mock_getuser, mock_hostname, mock_default_executor):
        mock_sequential_executor = SequentialExecutor()
        mock_hostname.return_value = "test_hostname"
        mock_getuser.return_value = "testuser"
        mock_default_executor.return_value = mock_sequential_executor

        test_job = Job(job_runner=MockJobRunner(), heartrate=10, dag_id="example_dag", state=State.RUNNING)
        assert test_job.executor_class == "SequentialExecutor"
        assert test_job.heartrate == 10
        assert test_job.dag_id == "example_dag"
        assert test_job.hostname == "test_hostname"
        assert test_job.max_tis_per_query == 100
        assert test_job.unixname == "testuser"
        assert test_job.state == "running"
        assert test_job.executor == mock_sequential_executor

    def test_heartbeat(self, frozen_sleep, monkeypatch):
        monkeypatch.setattr("airflow.jobs.job.sleep", frozen_sleep)
        with create_session() as session:
            job = Job(job_runner=MockJobRunner(), heartrate=10)
            job.latest_heartbeat = timezone.utcnow()
            session.add(job)
            session.commit()

            hb_callback = Mock()
            job.job_runner.heartbeat_callback = hb_callback

            job.heartbeat()

            hb_callback.assert_called_once_with(session=ANY)

            hb_callback.reset_mock()
            job.heartbeat(only_if_necessary=True)
            assert hb_callback.called is False
