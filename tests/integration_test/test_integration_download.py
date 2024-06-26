"""integration Tests for satip.download.py."""
import os
import pytest

import pandas as pd

from satip.download import (
    _determine_datetimes_to_download_files,
    _get_missing_datetimes_from_list_of_files,
    download_eumetsat_data,
)
from satip.utils import format_dt_str


class TestDownload():
    """Test case for downloader tests."""

    def test_download_eumetsat_data(self):  # noqa
        # Call the downloader on a very short chunk of data:
        assert download_eumetsat_data(
            download_directory=str(os.getcwd() + "/storage/"),
            start_date=format_dt_str("2020-06-01 11:59:00"),
            end_date=format_dt_str("2020-06-01 12:02:00"),
            user_key=os.environ.get("EUMETSAT_USER_KEY"),
            user_secret=os.environ.get("EUMETSAT_USER_SECRET"),
            auth_filename=None,
            number_of_processes=2,
            product=["cloud", "rss"],
            enforce_full_days=False,
        ) is None
