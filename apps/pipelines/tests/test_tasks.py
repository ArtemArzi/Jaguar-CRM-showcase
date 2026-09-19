from unittest.mock import patch

import pytest

from apps.pipelines.tasks import advance_all_pipelines


@pytest.mark.django_db
class TestAdvanceAllPipelines:
    @patch("apps.pipelines.services.advance_due_pipelines")
    def test_iterates_clubs(self, mock_advance, club, other_club):
        mock_advance.return_value = {"advanced": 0, "completed": 0}
        result = advance_all_pipelines()
        assert result["clubs"] == 2
        assert mock_advance.call_count == 2
