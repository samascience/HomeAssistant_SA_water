"""DataUpdateCoordinator for Sensus Analytics Integration."""

import logging
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ACCOUNT_NUMBER,
    CONF_BASE_URL,
    CONF_PASSWORD,
    CONF_SSO_AUTH,
    CONF_USERNAME,
    CONF_WATER_METER_NUMBER,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class SensusAnalyticsDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the API."""

    def __init__(self, hass: HomeAssistant, config_entry):
        """Initialize."""
        self.hass = hass
        self.base_url = config_entry.data[CONF_BASE_URL]
        self.username = config_entry.data[CONF_USERNAME]
        self.password = config_entry.data[CONF_PASSWORD]
        self.sso_auth = config_entry.data.get(CONF_SSO_AUTH, "").strip()
        self.account_number = config_entry.data[CONF_ACCOUNT_NUMBER]
        self.water_meter_number = config_entry.data[CONF_WATER_METER_NUMBER]
        self.config_entry = config_entry

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=5),
        )

    async def _async_update_data(self):
        """Fetch data from API."""
        return await self.hass.async_add_executor_job(self._fetch_data)

    def _fetch_data(self):
        """Fetch data from the Sensus Analytics API."""
        try:
            session = self._create_authenticated_session()
            data = self._fetch_daily_water_data(session)
            local_tz = dt_util.get_time_zone(self.hass.config.time_zone)
            target_date = datetime.now(local_tz) - timedelta(days=1)
            hourly_data = self._retrieve_hourly_data(session, target_date)
            if hourly_data:
                data["hourly_usage_data"] = hourly_data
            return data
        except UpdateFailed:
            raise
        except Exception as error:
            _LOGGER.error("Unexpected error: %s", error)
            raise UpdateFailed(f"Unexpected error: {error}") from error

    def _create_authenticated_session(self):
        """Create an authenticated password or optional SSO session."""
        session = requests.Session()
        session.headers.update({"Accept": "application/json, text/plain, */*"})

        if self.sso_auth:
            token = self._extract_sso_token(self.sso_auth)
            response = session.get(
                urljoin(self.base_url, "main.html"),
                params={"sso_auth": token},
                allow_redirects=True,
                timeout=15,
            )
            if "sensus-analytics.com" not in response.url or not session.cookies:
                raise UpdateFailed(
                    "Sensus SSO exchange failed. The token may be expired or the "
                    "utility has retired the Sensus portal."
                )
            return session

        response = session.post(
            urljoin(self.base_url, "j_spring_security_check"),
            data={"j_username": self.username, "j_password": self.password},
            allow_redirects=False,
            timeout=10,
        )
        if response.status_code != 302 or "#/failed" in response.headers.get("location", ""):
            raise UpdateFailed("Authentication failed")
        return session

    @staticmethod
    def _extract_sso_token(sso_auth: str) -> str:
        """Extract a raw JWT from a token, query parameter, or complete URL."""
        sso_auth = sso_auth.strip()
        if sso_auth.startswith(("http://", "https://")):
            token = parse_qs(urlparse(sso_auth).query).get("sso_auth", [""])[0]
        elif sso_auth.startswith("sso_auth="):
            token = sso_auth.removeprefix("sso_auth=")
        else:
            token = sso_auth
        token = token.split("#", 1)[0]
        if not token:
            raise UpdateFailed("The supplied Sensus SSO value has no sso_auth token")
        return token

    @staticmethod
    def _parse_json_response(response: requests.Response, endpoint: str):
        """Return JSON or report a retired portal redirect clearly."""
        content_type = response.headers.get("content-type", "").lower()
        if "json" not in content_type:
            _LOGGER.warning(
                "Sensus %s endpoint returned %s from %s instead of JSON",
                endpoint,
                content_type or "an unknown content type",
                response.url,
            )
            raise UpdateFailed(
                "Sensus returned HTML instead of usage data. The SSO token may be "
                "expired, or the utility has retired the legacy Sensus API."
            )
        try:
            return response.json()
        except ValueError as error:
            raise UpdateFailed(
                "Sensus returned invalid usage data. The SSO token may be expired "
                "or the utility has retired the legacy Sensus API."
            ) from error

    def _fetch_daily_water_data(self, session):
        """Fetch daily water meter data."""
        response = session.post(
            urljoin(self.base_url, "water/widget/byPage"),
            json={
                "group": "meters",
                "accountNumber": self.account_number,
                "deviceId": self.water_meter_number,
            },
            timeout=10,
        )
        response.raise_for_status()
        data = self._parse_json_response(response, "daily usage")
        return data.get("widgetList")[0].get("data").get("devices")[0]

    def _retrieve_hourly_data(self, session: requests.Session, target_date: datetime):
        """Retrieve hourly usage data for a specific date based on local time."""
        start_ts, end_ts = self._get_start_end_timestamps(target_date)
        usage_url, params = self._construct_hourly_data_request(start_ts, end_ts)
        try:
            response = session.get(usage_url, params=params, timeout=10)
            response.raise_for_status()
            return self._process_hourly_data_response(
                self._parse_json_response(response, "hourly usage")
            )
        except (requests.exceptions.RequestException, KeyError, TypeError, ValueError) as error:
            _LOGGER.error("Hourly data retrieval failed: %s", error)
            return None

    def _get_start_end_timestamps(self, target_date):
        """Get start and end of day timestamps in milliseconds."""
        local_tz = dt_util.get_time_zone(self.hass.config.time_zone)
        start_dt = datetime.combine(target_date, datetime.min.time(), tzinfo=local_tz)
        end_dt = datetime.combine(target_date, datetime.max.time(), tzinfo=local_tz)
        return int(start_dt.timestamp() * 1000), int(end_dt.timestamp() * 1000)

    def _construct_hourly_data_request(self, start_ts, end_ts):
        """Construct the hourly usage request."""
        return (
            urljoin(
                self.base_url,
                f"water/usage/{self.account_number}/{self.water_meter_number}",
            ),
            {"start": start_ts, "end": end_ts, "zoom": "day", "page": "null", "weather": "1"},
        )

    def _process_hourly_data_response(self, hourly_data):
        """Process and structure the hourly data response."""
        if not isinstance(hourly_data, dict) or not hourly_data.get("operationSuccess", False):
            return None
        usage_list = hourly_data.get("data", {}).get("usage", [])
        if len(usage_list) < 2:
            return None
        usage_unit, rain_unit, temp_unit = usage_list[0][:3]
        return [
            {
                "timestamp": entry[0],
                "usage": entry[1],
                "rain": entry[2],
                "temp": entry[3],
                "usage_unit": usage_unit,
                "rain_unit": rain_unit,
                "temp_unit": temp_unit,
            }
            for entry in usage_list[1:]
        ]
