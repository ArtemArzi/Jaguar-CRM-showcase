from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.leads.models import LeadLifecycleEvent
from apps.notifications.routes import trainer_task_url, trainer_tasks_url
from apps.pipelines.models import Pipeline, PipelineExecution, PipelineStep
from apps.pipelines.services import (
    advance_due_pipelines,
    cancel_pipeline,
    seed_default_pipelines,
    trigger_pipeline,
)
from apps.pipelines.tests.factories import (
    PipelineExecutionFactory,
)
from apps.retention.models import RetentionTask
from apps.retention.selectors import get_tasks_for_trainer
from apps.students.models import Student
from apps.students.tests.factories import StudentFactory
from apps.trainers.tests.factories import TrainerFactory


@pytest.mark.django_db
class TestSeedDefaultPipelines:
    def test_creates_follow_up(self, club):
        seed_default_pipelines(club_id=club.id)
        pipeline = Pipeline.objects.for_club(club).get(
            pipeline_type=Pipeline.PipelineType.FOLLOW_UP
        )
        steps = list(pipeline.steps.order_by("order"))
        assert len(steps) == 3
        assert steps[0].delay_hours == 2
        assert steps[1].delay_hours == 72
        assert steps[2].delay_hours == 168
        assert steps[2].is_terminal is True

    def test_creates_win_back(self, club):
        seed_default_pipelines(club_id=club.id)
        pipeline = Pipeline.objects.for_club(club).get(
            pipeline_type=Pipeline.PipelineType.WIN_BACK
        )
        steps = list(pipeline.steps.order_by("order"))
        assert len(steps) == 2
        assert steps[0].delay_hours == 336
        assert steps[1].delay_hours == 1440
        assert steps[1].is_terminal is True

    def test_idempotent(self, club):
        seed_default_pipelines(club_id=club.id)
        seed_default_pipelines(club_id=club.id)
        assert Pipeline.objects.for_club(club).count() == 2


@pytest.mark.django_db
class TestTriggerPipeline:
    def test_creates_execution(self, club):
        seed_default_pipelines(club_id=club.id)
        student = StudentFactory(club=club)
        execution = trigger_pipeline(
            club_id=club.id, student_id=student.id, pipeline_type="follow_up"
        )
        assert execution is not None
        assert execution.next_step_at is not None
        assert execution.completed_at is None

    def test_skip_duplicate(self, club):
        seed_default_pipelines(club_id=club.id)
        student = StudentFactory(club=club)
        exec1 = trigger_pipeline(
            club_id=club.id, student_id=student.id, pipeline_type="follow_up"
        )
        exec2 = trigger_pipeline(
            club_id=club.id, student_id=student.id, pipeline_type="follow_up"
        )
        assert exec1 is not None
        assert exec2 is None
        assert PipelineExecution.objects.for_club(club).count() == 1

    def test_no_pipeline_returns_none(self, club):
        student = StudentFactory(club=club)
        result = trigger_pipeline(
            club_id=club.id, student_id=student.id, pipeline_type="follow_up"
        )
        assert result is None


@pytest.mark.django_db
class TestAdvanceDuePipelines:
    @patch("apps.notifications.services.send_push_to_user")
    def test_executes_step(self, mock_push, club):
        seed_default_pipelines(club_id=club.id)
        trainer = TrainerFactory(club=club)
        student = StudentFactory(club=club, assigned_trainer=trainer)
        execution = trigger_pipeline(
            club_id=club.id, student_id=student.id, pipeline_type="follow_up"
        )
        # Make it due
        PipelineExecution.objects.filter(id=execution.id).update(
            next_step_at=timezone.now() - timedelta(minutes=1)
        )
        result = advance_due_pipelines(club_id=club.id)
        assert result["advanced"] == 1
        execution.refresh_from_db()
        assert execution.current_step is not None
        assert execution.current_step.order == 1

    @patch("apps.notifications.services.send_push_to_user")
    def test_create_task_creates_post_trial_task_due_today(self, mock_push, club, trainer_user):
        seed_default_pipelines(club_id=club.id)
        trainer = TrainerFactory(club=club, user=trainer_user)
        student = StudentFactory(
            club=club,
            status=Student.Status.TRIAL,
            assigned_trainer=trainer,
        )
        execution = trigger_pipeline(
            club_id=club.id, student_id=student.id, pipeline_type="follow_up"
        )
        PipelineExecution.objects.filter(id=execution.id).update(
            next_step_at=timezone.now() - timedelta(minutes=1)
        )

        advance_due_pipelines(club_id=club.id)

        task = RetentionTask.objects.for_club(club).get(student=student)
        assert task.trainer == trainer
        assert task.task_type == RetentionTask.TaskType.POST_TRIAL
        assert task.due_date == timezone.localdate()
        assert "Позвонить, спросить впечатления" in task.notes

        mock_push.assert_called_once()
        assert mock_push.call_args.kwargs["url"] == trainer_task_url(task.id)
        assert student.first_name not in mock_push.call_args.kwargs["body"]
        assert student.last_name not in mock_push.call_args.kwargs["body"]

    @patch("apps.notifications.services.send_push_to_user")
    def test_create_task_retry_reuses_open_retention_task(self, mock_push, club, trainer_user):
        seed_default_pipelines(club_id=club.id)
        trainer = TrainerFactory(club=club, user=trainer_user)
        student = StudentFactory(
            club=club,
            status=Student.Status.TRIAL,
            assigned_trainer=trainer,
        )
        execution = trigger_pipeline(
            club_id=club.id, student_id=student.id, pipeline_type="follow_up"
        )

        PipelineExecution.objects.filter(id=execution.id).update(
            next_step_at=timezone.now() - timedelta(minutes=1)
        )
        advance_due_pipelines(club_id=club.id)
        task = RetentionTask.objects.for_club(club).get(student=student)

        PipelineExecution.objects.filter(id=execution.id).update(
            current_step=None,
            next_step_at=timezone.now() - timedelta(minutes=1),
            completed_at=None,
        )
        advance_due_pipelines(club_id=club.id)

        assert RetentionTask.objects.for_club(club).filter(student=student).count() == 1
        assert RetentionTask.objects.for_club(club).get(student=student).id == task.id
        assert mock_push.call_count == 2
        assert mock_push.call_args.kwargs["url"] == trainer_task_url(task.id)

    @patch("apps.notifications.services.send_push_to_user")
    def test_create_task_without_trainer_user_skips_task_and_push(self, mock_push, club):
        seed_default_pipelines(club_id=club.id)
        trainer = TrainerFactory(club=club, user=None)
        student = StudentFactory(
            club=club,
            status=Student.Status.TRIAL,
            assigned_trainer=trainer,
        )
        execution = trigger_pipeline(
            club_id=club.id, student_id=student.id, pipeline_type="follow_up"
        )
        PipelineExecution.objects.filter(id=execution.id).update(
            next_step_at=timezone.now() - timedelta(minutes=1)
        )

        advance_due_pipelines(club_id=club.id)

        assert RetentionTask.objects.for_club(club).filter(student=student).count() == 0
        mock_push.assert_not_called()

    @patch("apps.notifications.services.send_push_to_user")
    def test_create_task_without_assigned_trainer_skips_task_and_push(self, mock_push, club):
        seed_default_pipelines(club_id=club.id)
        student = StudentFactory(
            club=club,
            status=Student.Status.TRIAL,
            assigned_trainer=None,
        )
        execution = trigger_pipeline(
            club_id=club.id, student_id=student.id, pipeline_type="follow_up"
        )
        PipelineExecution.objects.filter(id=execution.id).update(
            next_step_at=timezone.now() - timedelta(minutes=1)
        )

        advance_due_pipelines(club_id=club.id)

        assert RetentionTask.objects.for_club(club).filter(student=student).count() == 0
        mock_push.assert_not_called()

    @patch("apps.notifications.services.send_push_to_user")
    def test_create_task_notes_redact_message_contact_details(self, mock_push, club, trainer_user):
        seed_default_pipelines(club_id=club.id)
        pipeline = Pipeline.objects.for_club(club).get(
            pipeline_type=Pipeline.PipelineType.FOLLOW_UP
        )
        pipeline.steps.filter(order=1).update(
            action_config={"message": "Follow up at student@example.invalid or +15555550123"}
        )
        trainer = TrainerFactory(club=club, user=trainer_user)
        student = StudentFactory(
            club=club,
            status=Student.Status.TRIAL,
            assigned_trainer=trainer,
        )
        execution = trigger_pipeline(
            club_id=club.id, student_id=student.id, pipeline_type="follow_up"
        )
        PipelineExecution.objects.filter(id=execution.id).update(
            next_step_at=timezone.now() - timedelta(minutes=1)
        )

        advance_due_pipelines(club_id=club.id)

        task = RetentionTask.objects.for_club(club).get(student=student)
        assert "[redacted-email]" in task.notes
        assert "[redacted-phone]" in task.notes
        assert "student@example.invalid" not in task.notes
        assert "+15555550123" not in task.notes

    @patch("apps.notifications.services.send_push_to_user")
    def test_created_task_is_visible_in_retention_trainer_selector(self, mock_push, club, trainer_user):
        seed_default_pipelines(club_id=club.id)
        trainer = TrainerFactory(club=club, user=trainer_user)
        student = StudentFactory(
            club=club,
            status=Student.Status.TRIAL,
            assigned_trainer=trainer,
        )
        execution = trigger_pipeline(
            club_id=club.id, student_id=student.id, pipeline_type="follow_up"
        )
        PipelineExecution.objects.filter(id=execution.id).update(
            next_step_at=timezone.now() - timedelta(minutes=1)
        )

        advance_due_pipelines(club_id=club.id)

        tasks = list(get_tasks_for_trainer(club=club, trainer_id=trainer.id))
        assert len(tasks) == 1
        assert tasks[0].student_id == student.id
        assert tasks[0].task_type == RetentionTask.TaskType.POST_TRIAL

    @patch("apps.notifications.services.send_push_to_user")
    def test_create_task_tenant_isolation(self, mock_push, club, other_club, trainer_user):
        seed_default_pipelines(club_id=club.id)
        seed_default_pipelines(club_id=other_club.id)
        trainer = TrainerFactory(club=club, user=trainer_user)
        other_trainer = TrainerFactory(club=other_club)
        student = StudentFactory(
            club=club,
            status=Student.Status.TRIAL,
            assigned_trainer=trainer,
        )
        other_student = StudentFactory(
            club=other_club,
            status=Student.Status.TRIAL,
            assigned_trainer=other_trainer,
        )
        execution = trigger_pipeline(
            club_id=club.id, student_id=student.id, pipeline_type="follow_up"
        )
        other_execution = trigger_pipeline(
            club_id=other_club.id, student_id=other_student.id, pipeline_type="follow_up"
        )
        PipelineExecution.objects.filter(id=execution.id).update(
            next_step_at=timezone.now() - timedelta(minutes=1)
        )
        PipelineExecution.objects.filter(id=other_execution.id).update(
            next_step_at=timezone.now() - timedelta(minutes=1)
        )

        advance_due_pipelines(club_id=club.id)

        assert RetentionTask.objects.for_club(club).filter(student=student).count() == 1
        assert RetentionTask.objects.for_club(other_club).filter(student=other_student).count() == 0

    @patch("apps.notifications.services.send_push_to_user")
    def test_send_push_action_links_to_trainer_tasks_list(self, mock_push, club, trainer_user):
        pipeline = Pipeline.objects.create(
            club=club,
            name="Trainer push",
            pipeline_type=Pipeline.PipelineType.FOLLOW_UP,
        )
        PipelineStep.objects.create(
            club=club,
            pipeline=pipeline,
            order=1,
            delay_hours=0,
            action_type=PipelineStep.ActionType.SEND_PUSH,
            action_config={"message": "Call after trial"},
        )
        trainer = TrainerFactory(club=club, user=trainer_user)
        student = StudentFactory(club=club, assigned_trainer=trainer)
        execution = trigger_pipeline(
            club_id=club.id, student_id=student.id, pipeline_type="follow_up"
        )
        PipelineExecution.objects.filter(id=execution.id).update(
            next_step_at=timezone.now() - timedelta(minutes=1)
        )

        advance_due_pipelines(club_id=club.id)

        mock_push.assert_called_once()
        assert mock_push.call_args.kwargs["url"] == trainer_tasks_url()

    @patch("apps.notifications.services.send_push_to_user")
    def test_terminal_step_completes(self, mock_push, club):
        seed_default_pipelines(club_id=club.id)
        trainer = TrainerFactory(club=club)
        student = StudentFactory(club=club, assigned_trainer=trainer)
        pipeline = Pipeline.objects.for_club(club).get(
            pipeline_type=Pipeline.PipelineType.FOLLOW_UP
        )
        step2 = PipelineStep.objects.get(pipeline=pipeline, order=2)
        execution = PipelineExecutionFactory(
            club=club,
            pipeline=pipeline,
            student=student,
            current_step=step2,
            next_step_at=timezone.now() - timedelta(minutes=1),
        )
        result = advance_due_pipelines(club_id=club.id)
        assert result["advanced"] == 1
        assert result["completed"] == 1
        execution.refresh_from_db()
        assert execution.completed_at is not None
        # Student should be marked LOST
        student.refresh_from_db()
        assert student.status == Student.Status.LOST
        assert student.lead_status is None
        event = LeadLifecycleEvent.objects.for_club(club).get(student=student)
        assert event.event_type == LeadLifecycleEvent.EventType.LEAD_LOST
        assert event.reason == Student.LossReason.CHANGED_MIND

    @patch("apps.notifications.services.send_push_to_user")
    def test_terminal_lost_for_trial_lead_uses_lead_lifecycle(self, mock_push, club):
        seed_default_pipelines(club_id=club.id)
        trainer = TrainerFactory(club=club)
        student = StudentFactory(
            club=club,
            status=Student.Status.TRIAL,
            lead_status=Student.LeadStatus.TRIAL_DONE,
            assigned_trainer=trainer,
        )
        pipeline = Pipeline.objects.for_club(club).get(
            pipeline_type=Pipeline.PipelineType.FOLLOW_UP
        )
        step2 = PipelineStep.objects.get(pipeline=pipeline, order=2)
        execution = PipelineExecutionFactory(
            club=club,
            pipeline=pipeline,
            student=student,
            current_step=step2,
            next_step_at=timezone.now() - timedelta(minutes=1),
        )

        result = advance_due_pipelines(club_id=club.id)

        assert result["completed"] == 1
        execution.refresh_from_db()
        student.refresh_from_db()
        assert execution.completed_at is not None
        assert student.status == Student.Status.LOST
        assert student.lead_status is None
        event = LeadLifecycleEvent.objects.for_club(club).get(student=student)
        assert event.event_type == LeadLifecycleEvent.EventType.LEAD_LOST
        assert event.old_lead_status == Student.LeadStatus.TRIAL_DONE

    @patch("apps.notifications.services.send_push_to_user")
    def test_terminal_lost_for_cleared_trial_lead_is_idempotent(self, mock_push, club):
        seed_default_pipelines(club_id=club.id)
        trainer = TrainerFactory(club=club)
        student = StudentFactory(
            club=club,
            status=Student.Status.TRIAL,
            lead_status=None,
            assigned_trainer=trainer,
        )
        pipeline = Pipeline.objects.for_club(club).get(
            pipeline_type=Pipeline.PipelineType.FOLLOW_UP
        )
        step2 = PipelineStep.objects.get(pipeline=pipeline, order=2)
        execution = PipelineExecutionFactory(
            club=club,
            pipeline=pipeline,
            student=student,
            current_step=step2,
            next_step_at=timezone.now() - timedelta(minutes=1),
        )

        first = advance_due_pipelines(club_id=club.id)
        second = advance_due_pipelines(club_id=club.id)

        assert first["completed"] == 1
        assert second["completed"] == 0
        execution.refresh_from_db()
        student.refresh_from_db()
        assert execution.completed_at is not None
        assert student.status == Student.Status.LOST
        assert student.lead_status is None
        assert not LeadLifecycleEvent.objects.for_club(club).filter(student=student).exists()

    @patch("apps.notifications.services.send_push_to_user")
    def test_terminal_lost_for_active_non_lead_does_not_create_lead_lifecycle(self, mock_push, club):
        seed_default_pipelines(club_id=club.id)
        trainer = TrainerFactory(club=club)
        student = StudentFactory(
            club=club,
            status=Student.Status.ACTIVE,
            lead_status=None,
            assigned_trainer=trainer,
        )
        pipeline = Pipeline.objects.for_club(club).get(
            pipeline_type=Pipeline.PipelineType.FOLLOW_UP
        )
        step2 = PipelineStep.objects.get(pipeline=pipeline, order=2)
        execution = PipelineExecutionFactory(
            club=club,
            pipeline=pipeline,
            student=student,
            current_step=step2,
            next_step_at=timezone.now() - timedelta(minutes=1),
        )

        result = advance_due_pipelines(club_id=club.id)

        assert result["completed"] == 1
        execution.refresh_from_db()
        student.refresh_from_db()
        assert execution.completed_at is not None
        assert student.status == Student.Status.LOST
        assert student.loss_reason == Student.LossReason.CHANGED_MIND
        assert not LeadLifecycleEvent.objects.for_club(club).filter(student=student).exists()

    def test_skips_cancelled(self, club):
        seed_default_pipelines(club_id=club.id)
        student = StudentFactory(club=club)
        execution = trigger_pipeline(
            club_id=club.id, student_id=student.id, pipeline_type="follow_up"
        )
        PipelineExecution.objects.filter(id=execution.id).update(
            next_step_at=timezone.now() - timedelta(minutes=1),
            cancelled_at=timezone.now(),
        )
        result = advance_due_pipelines(club_id=club.id)
        assert result["advanced"] == 0

    def test_tenant_isolation(self, club, other_club):
        seed_default_pipelines(club_id=club.id)
        seed_default_pipelines(club_id=other_club.id)
        student_a = StudentFactory(club=club)
        student_b = StudentFactory(club=other_club)
        trigger_pipeline(
            club_id=club.id, student_id=student_a.id, pipeline_type="follow_up"
        )
        trigger_pipeline(
            club_id=other_club.id, student_id=student_b.id, pipeline_type="follow_up"
        )
        # Only club A's executions
        execs_a = PipelineExecution.objects.for_club(club)
        execs_b = PipelineExecution.objects.for_club(other_club)
        assert execs_a.count() == 1
        assert execs_b.count() == 1
        assert execs_a.first().student_id == student_a.id

    @patch("apps.notifications.services.send_push_to_user")
    def test_follow_up_full_cycle(self, mock_push, club):
        """Full cycle: trigger -> advance 3 steps -> completed, student LOST."""
        seed_default_pipelines(club_id=club.id)
        trainer = TrainerFactory(club=club)
        student = StudentFactory(club=club, status=Student.Status.LEAD, assigned_trainer=trainer)
        execution = trigger_pipeline(
            club_id=club.id, student_id=student.id, pipeline_type="follow_up"
        )

        for step_num in range(1, 4):
            PipelineExecution.objects.filter(id=execution.id).update(
                next_step_at=timezone.now() - timedelta(minutes=1)
            )
            advance_due_pipelines(club_id=club.id)
            execution.refresh_from_db()
            assert execution.current_step.order == step_num

        execution.refresh_from_db()
        assert execution.completed_at is not None
        student.refresh_from_db()
        assert student.status == Student.Status.LOST

    @patch("apps.notifications.services.send_push_to_user")
    def test_win_back_full_cycle(self, mock_push, club):
        """Full cycle: trigger -> advance 2 steps -> completed."""
        seed_default_pipelines(club_id=club.id)
        trainer = TrainerFactory(club=club)
        student = StudentFactory(
            club=club, status=Student.Status.CHURNED, assigned_trainer=trainer
        )
        execution = trigger_pipeline(
            club_id=club.id, student_id=student.id, pipeline_type="win_back"
        )

        for step_num in range(1, 3):
            PipelineExecution.objects.filter(id=execution.id).update(
                next_step_at=timezone.now() - timedelta(minutes=1)
            )
            advance_due_pipelines(club_id=club.id)
            execution.refresh_from_db()
            assert execution.current_step.order == step_num

        execution.refresh_from_db()
        assert execution.completed_at is not None


@pytest.mark.django_db
class TestCancelPipeline:
    def test_cancel(self, club):
        seed_default_pipelines(club_id=club.id)
        student = StudentFactory(club=club)
        trigger_pipeline(
            club_id=club.id, student_id=student.id, pipeline_type="follow_up"
        )
        count = cancel_pipeline(club_id=club.id, student_id=student.id)
        assert count == 1
        execution = PipelineExecution.objects.for_club(club).first()
        assert execution.cancelled_at is not None

    def test_cancel_specific_type(self, club):
        seed_default_pipelines(club_id=club.id)
        student = StudentFactory(club=club)
        trigger_pipeline(
            club_id=club.id, student_id=student.id, pipeline_type="follow_up"
        )
        trigger_pipeline(
            club_id=club.id, student_id=student.id, pipeline_type="win_back"
        )
        count = cancel_pipeline(
            club_id=club.id, student_id=student.id, pipeline_type="follow_up"
        )
        assert count == 1
        # win_back still active
        active = PipelineExecution.objects.for_club(club).filter(
            cancelled_at__isnull=True
        )
        assert active.count() == 1
        assert active.first().pipeline.pipeline_type == Pipeline.PipelineType.WIN_BACK
