from apps.retention.schemas import CloseTaskIn


class TestCloseTaskInResolutions:
    def test_accepts_new_resolution_choices(self):
        for res in ["called_will_come", "no_answer", "quit"]:
            task_in = CloseTaskIn(resolution=res, notes="")
            assert task_in.resolution == res
