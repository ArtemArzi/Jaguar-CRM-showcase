"""Tests for get_club_sessions selector — Тренировки sub-tab."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from apps.attendance.tests.factories import (
    CheckinFactory,
    ScheduleFactory,
    TrainingGroupFactory,
)
from apps.billing.tests.factories import TrainingTypeFactory
from apps.clubs.tests.factories import LocationFactory
from apps.students.tests.factories import StudentFactory
from apps.trainers.selectors import get_club_sessions
from apps.trainers.tests.factories import TrainerFactory


@pytest.mark.django_db
class TestGetClubSessions:
    def test_groups_by_schedule_and_date(self, club):
        """3 checkins on one schedule × date → 1 session with attendees=3."""
        loc = LocationFactory(club=club)
        tt = TrainingTypeFactory(club=club)
        trainer = TrainerFactory(club=club)
        schedule = ScheduleFactory(
            club=club, trainer=trainer, location=loc, training_type=tt,
            group_name="BJJ утро",
        )
        d = date.today()
        for _ in range(3):
            student = StudentFactory(club=club)
            CheckinFactory(
                club=club, student=student, schedule=schedule,
                trainer=trainer, location=loc, training_type=tt, date=d,
            )

        result = get_club_sessions(
            club=club, date_from=d - timedelta(days=1), date_to=d + timedelta(days=1),
        )
        assert len(result) == 1
        row = result[0]
        assert row["attendees"] == 3
        assert row["group_name"] == "BJJ утро"
        assert row["trainer_id"] == trainer.id

    def test_splits_by_schedule_in_same_day(self, club):
        """Two different schedules on the same day → 2 separate sessions."""
        loc = LocationFactory(club=club)
        tt = TrainingTypeFactory(club=club)
        trainer = TrainerFactory(club=club)
        sched_morning = ScheduleFactory(
            club=club, trainer=trainer, location=loc, training_type=tt,
            group_name="BJJ утро",
        )
        sched_evening = ScheduleFactory(
            club=club, trainer=trainer, location=loc, training_type=tt,
            group_name="BJJ вечер",
        )
        d = date.today()
        for _ in range(2):
            CheckinFactory(
                club=club, student=StudentFactory(club=club),
                schedule=sched_morning, trainer=trainer,
                location=loc, training_type=tt, date=d,
            )
        for _ in range(4):
            CheckinFactory(
                club=club, student=StudentFactory(club=club),
                schedule=sched_evening, trainer=trainer,
                location=loc, training_type=tt, date=d,
            )

        rows = get_club_sessions(
            club=club, date_from=d, date_to=d,
        )
        assert len(rows) == 2
        by_name = {r["group_name"]: r["attendees"] for r in rows}
        assert by_name["BJJ утро"] == 2
        assert by_name["BJJ вечер"] == 4

    def test_filter_by_trainer(self, club):
        loc = LocationFactory(club=club)
        tt = TrainingTypeFactory(club=club)
        t1 = TrainerFactory(club=club, first_name="Влад")
        t2 = TrainerFactory(club=club, first_name="Петя")
        s1 = ScheduleFactory(club=club, trainer=t1, location=loc, training_type=tt)
        s2 = ScheduleFactory(club=club, trainer=t2, location=loc, training_type=tt)
        d = date.today()
        CheckinFactory(
            club=club,
            student=StudentFactory(club=club),
            schedule=s1,
            trainer=t1,
            location=loc,
            training_type=tt,
            date=d,
        )
        CheckinFactory(
            club=club,
            student=StudentFactory(club=club),
            schedule=s2,
            trainer=t2,
            location=loc,
            training_type=tt,
            date=d,
        )

        rows = get_club_sessions(
            club=club, date_from=d, date_to=d, trainer_id=t1.id,
        )
        assert len(rows) == 1
        assert rows[0]["trainer_id"] == t1.id

    def test_filter_by_schedule(self, club):
        loc = LocationFactory(club=club)
        tt = TrainingTypeFactory(club=club)
        trainer = TrainerFactory(club=club)
        s1 = ScheduleFactory(club=club, trainer=trainer, location=loc, training_type=tt, group_name="A")
        s2 = ScheduleFactory(club=club, trainer=trainer, location=loc, training_type=tt, group_name="B")
        d = date.today()
        CheckinFactory(
            club=club,
            student=StudentFactory(club=club),
            schedule=s1,
            trainer=trainer,
            location=loc,
            training_type=tt,
            date=d,
        )
        CheckinFactory(
            club=club,
            student=StudentFactory(club=club),
            schedule=s2,
            trainer=trainer,
            location=loc,
            training_type=tt,
            date=d,
        )

        rows = get_club_sessions(
            club=club, date_from=d, date_to=d, schedule_id=s2.id,
        )
        assert len(rows) == 1
        assert rows[0]["schedule_id"] == s2.id
        assert rows[0]["group_name"] == "B"

    def test_mapped_schedule_uses_canonical_group_name(self, club):
        trainer = TrainerFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind="group")
        group = TrainingGroupFactory(
            club=club,
            name="Canonical BJJ",
            training_type=training_type,
            responsible_trainer=trainer,
        )
        schedule = ScheduleFactory(
            club=club,
            training_group=group,
            group_name="Stale weekday label",
            trainer=trainer,
            location=group.location,
            training_type=training_type,
        )
        type(schedule).objects.for_club(club).filter(id=schedule.id).update(
            group_name="Stale weekday label"
        )
        target_date = date.today()
        CheckinFactory(
            club=club,
            student=StudentFactory(club=club),
            schedule=schedule,
            trainer=trainer,
            location=group.location,
            training_type=training_type,
            date=target_date,
        )

        rows = get_club_sessions(
            club=club,
            date_from=target_date,
            date_to=target_date,
        )

        assert rows[0]["group_name"] == group.name

    def test_tenant_isolation(self, club, other_club):
        loc_a = LocationFactory(club=club)
        loc_b = LocationFactory(club=other_club)
        tt_a = TrainingTypeFactory(club=club)
        tt_b = TrainingTypeFactory(club=other_club)
        t_a = TrainerFactory(club=club)
        t_b = TrainerFactory(club=other_club)
        s_a = ScheduleFactory(club=club, trainer=t_a, location=loc_a, training_type=tt_a)
        s_b = ScheduleFactory(club=other_club, trainer=t_b, location=loc_b, training_type=tt_b)
        d = date.today()
        CheckinFactory(
            club=club,
            student=StudentFactory(club=club),
            schedule=s_a,
            trainer=t_a,
            location=loc_a,
            training_type=tt_a,
            date=d,
        )
        CheckinFactory(
            club=other_club,
            student=StudentFactory(club=other_club),
            schedule=s_b,
            trainer=t_b,
            location=loc_b,
            training_type=tt_b,
            date=d,
        )

        rows = get_club_sessions(club=club, date_from=d, date_to=d)
        assert len(rows) == 1
        assert rows[0]["trainer_id"] == t_a.id

    def test_excludes_soft_deleted(self, club):
        loc = LocationFactory(club=club)
        tt = TrainingTypeFactory(club=club)
        trainer = TrainerFactory(club=club)
        schedule = ScheduleFactory(club=club, trainer=trainer, location=loc, training_type=tt)
        d = date.today()
        CheckinFactory(
            club=club,
            student=StudentFactory(club=club),
            schedule=schedule,
            trainer=trainer,
            location=loc,
            training_type=tt,
            date=d,
        )
        deleted_checkin = CheckinFactory(
            club=club,
            student=StudentFactory(club=club),
            schedule=schedule,
            trainer=trainer,
            location=loc,
            training_type=tt,
            date=d,
        )
        deleted_checkin.soft_delete()

        rows = get_club_sessions(club=club, date_from=d, date_to=d)
        assert len(rows) == 1
        assert rows[0]["attendees"] == 1

    def test_empty_period_returns_empty_list(self, club):
        rows = get_club_sessions(
            club=club,
            date_from=date(2020, 1, 1),
            date_to=date(2020, 12, 31),
        )
        assert rows == []
