STUDENT_HOME_URL = "/student"
STUDENT_SCHEDULE_URL = "/student/schedule"
TRAINER_TASKS_URL = "/trainer/tasks"


def parent_child_url(student_id: int) -> str:
    return f"/parent/child/{student_id}"


def trainer_task_url(task_id: int) -> str:
    return f"/trainer/tasks/{task_id}"


def trainer_tasks_url() -> str:
    return TRAINER_TASKS_URL
