import datetime as dt
from unittest.mock import patch
import pytest
from backend.mail_ingest import MailClientCreateRequest

@pytest.mark.parametrize('now,limit',[(dt.datetime(2026,9,24,tzinfo=dt.timezone.utc),dt.datetime(2036,9,24,tzinfo=dt.timezone.utc)),(dt.datetime(2028,2,29,tzinfo=dt.timezone.utc),dt.datetime(2038,2,28,tzinfo=dt.timezone.utc))])
def test_ten_calendar_year_boundary(now,limit):
    with patch('backend.mail_ingest._utc_now',return_value=now):
        assert MailClientCreateRequest.validate_expires_at(limit)==limit
        with pytest.raises(ValueError,match='10年'):
            MailClientCreateRequest.validate_expires_at(limit+dt.timedelta(seconds=1))
        with pytest.raises(ValueError,match='未来'):
            MailClientCreateRequest.validate_expires_at(now)
        with pytest.raises(ValueError,match='时区'):
            MailClientCreateRequest.validate_expires_at(limit.replace(tzinfo=None))
