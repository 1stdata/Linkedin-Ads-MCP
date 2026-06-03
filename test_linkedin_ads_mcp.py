"""
Integration tests for the LinkedIn Ads MCP server.

Uses unittest.mock to patch HTTP requests so tests run without real credentials.
Run with:  python -m pytest test_linkedin_ads_mcp.py -v
       or: python test_linkedin_ads_mcp.py

Note: When calling @mcp.tool() async functions directly (outside FastMCP),
Field(default=...) defaults are FieldInfo objects, not the actual value.
All parameters must be passed explicitly in tests.
"""

import asyncio
import json
import os
import unittest
from datetime import datetime as real_datetime
from unittest.mock import patch, MagicMock

# Ensure env vars are set for import (tests mock actual API calls)
os.environ.setdefault("LINKEDIN_ACCESS_TOKEN", "test-token-123")
os.environ.setdefault("LINKEDIN_BUSINESS_ACCOUNT_ID", "511389977")

import linkedin_ads_server as server


class FakeResponse:
    """Minimal requests.Response stand-in."""

    def __init__(self, json_data=None, status_code=200, headers=None, text=""):
        self._json = json_data or {}
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text or json.dumps(self._json)

    def json(self):
        return self._json


def run_async(coro):
    """Helper to run async tool functions in sync test context."""
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------

class TestHelpers(unittest.TestCase):

    def test_format_account_urn_plain(self):
        self.assertEqual(server.format_account_urn("511389977"),
                         "urn:li:sponsoredAccount:511389977")

    def test_format_account_urn_already_urn(self):
        urn = "urn:li:sponsoredAccount:511389977"
        self.assertEqual(server.format_account_urn(urn), urn)

    def test_format_campaign_urn(self):
        self.assertEqual(server.format_campaign_urn("12345"),
                         "urn:li:sponsoredCampaign:12345")

    def test_format_creative_urn(self):
        self.assertEqual(server.format_creative_urn("999"),
                         "urn:li:sponsoredCreative:999")

    def test_extract_id_from_urn(self):
        self.assertEqual(server.extract_id_from_urn("urn:li:sponsoredCampaign:12345"), "12345")
        self.assertEqual(server.extract_id_from_urn(""), "")

    def test_iso_to_epoch_ms(self):
        ms = server.iso_to_epoch_ms("2025-01-01")
        self.assertIsInstance(ms, int)
        self.assertGreater(ms, 0)

    def test_epoch_ms_to_iso(self):
        # Use a known value and check it round-trips reasonably
        ms = server.iso_to_epoch_ms("2025-06-15")
        result = server.epoch_ms_to_iso(ms)
        self.assertIn("2025", result)
        self.assertIn("06", result)

    def test_epoch_ms_to_iso_zero(self):
        self.assertEqual(server.epoch_ms_to_iso(0), "N/A")

    def test_parse_date_params(self):
        params = server.parse_date_params("2025-05-01", "2025-05-31")
        self.assertEqual(params["dateRange.start.year"], 2025)
        self.assertEqual(params["dateRange.start.month"], 5)
        self.assertEqual(params["dateRange.start.day"], 1)
        self.assertEqual(params["dateRange.end.day"], 31)


class TestFormatOutput(unittest.TestCase):

    def test_format_table(self):
        data = [{"name": "Campaign A", "clicks": 100}, {"name": "Campaign B", "clicks": 200}]
        out = server.format_output(data, format_type="table")
        self.assertIn("Campaign A", out)
        self.assertIn("Campaign B", out)
        self.assertIn("clicks", out)

    def test_format_json(self):
        data = [{"a": 1}]
        out = server.format_output(data, format_type="json")
        parsed = json.loads(out)
        self.assertEqual(parsed[0]["a"], 1)

    def test_format_csv(self):
        data = [{"x": "hello", "y": "world"}]
        out = server.format_output(data, format_type="csv")
        lines = out.strip().split("\n")
        self.assertEqual(lines[0], "x,y")
        self.assertIn("hello", lines[1])

    def test_format_empty(self):
        self.assertEqual(server.format_output([]), "No data to display.")
        self.assertEqual(server.format_output(None), "No data to display.")


class TestGetCredentials(unittest.TestCase):

    @patch.dict(os.environ, {"LINKEDIN_ACCESS_TOKEN": "my-token"})
    def test_returns_env_token(self):
        server.LINKEDIN_ACCESS_TOKEN = "my-token"
        server.LINKEDIN_TOKEN_PATH = ""
        token = server.get_credentials()
        self.assertEqual(token, "my-token")

    @patch.dict(os.environ, {"LINKEDIN_ACCESS_TOKEN": "", "LINKEDIN_REFRESH_TOKEN": ""})
    def test_raises_when_no_token(self):
        server.LINKEDIN_ACCESS_TOKEN = ""
        server.LINKEDIN_REFRESH_TOKEN = ""
        server.LINKEDIN_TOKEN_PATH = ""
        with self.assertRaises(ValueError):
            server.get_credentials()


# ---------------------------------------------------------------------------
# Tool tests (API calls mocked)
# ---------------------------------------------------------------------------

class TestListAccounts(unittest.TestCase):

    @patch("linkedin_ads_server.linkedin_paginated_request")
    def test_list_accounts_success(self, mock_req):
        mock_req.return_value = [
            {"id": "urn:li:sponsoredAccount:511389977", "name": "Test Account",
             "status": "ACTIVE", "type": "BUSINESS", "currency": "USD"},
        ]
        result = run_async(server.list_accounts(format="table"))
        self.assertIn("511389977", result)
        self.assertIn("Test Account", result)

    @patch("linkedin_ads_server.linkedin_paginated_request")
    def test_list_accounts_empty(self, mock_req):
        mock_req.return_value = []
        result = run_async(server.list_accounts(format="table"))
        self.assertIn("No accessible", result)


class TestGetAccountDetails(unittest.TestCase):

    @patch("linkedin_ads_server.linkedin_api_request")
    def test_get_account_details(self, mock_req):
        mock_req.return_value = {
            "name": "Test Account",
            "status": "ACTIVE",
            "type": "BUSINESS",
            "currency": "USD",
        }
        result = run_async(server.get_account_details(account_id="511389977"))
        self.assertIn("Test Account", result)
        self.assertIn("ACTIVE", result)


class TestListCampaignGroups(unittest.TestCase):

    @patch("linkedin_ads_server.linkedin_paginated_request")
    def test_list_campaign_groups(self, mock_req):
        mock_req.return_value = [
            {"id": "urn:li:sponsoredCampaignGroup:100", "name": "Group 1",
             "status": "ACTIVE", "totalBudget": {"amount": "1000", "currencyCode": "USD"},
             "runSchedule": {"start": 1735689600000}},
        ]
        result = run_async(server.list_campaign_groups(
            account_id="511389977", status_filter="", format="table"))
        self.assertIn("Group 1", result)


class TestCreateCampaignGroup(unittest.TestCase):

    @patch("linkedin_ads_server.linkedin_api_request")
    def test_create_campaign_group(self, mock_req):
        mock_req.return_value = {"_created_id": "200", "status_code": 201}
        result = run_async(server.create_campaign_group(
            account_id="511389977", name="New Group", status="DRAFT",
            total_budget_amount="", total_budget_currency="USD",
            start_date="", end_date=""))
        self.assertIn("created successfully", result)
        self.assertIn("200", result)


class TestUpdateCampaignGroup(unittest.TestCase):

    @patch("linkedin_ads_server.linkedin_api_request")
    def test_update_campaign_group(self, mock_req):
        mock_req.return_value = {"status_code": 204, "success": True}
        result = run_async(server.update_campaign_group(
            account_id="511389977", group_id="100", name="Renamed",
            status="", total_budget_amount="", total_budget_currency="USD",
            start_date="", end_date=""))
        self.assertIn("updated successfully", result)

    def test_update_no_fields(self):
        result = run_async(server.update_campaign_group(
            account_id="511389977", group_id="100", name="",
            status="", total_budget_amount="", total_budget_currency="USD",
            start_date="", end_date=""))
        self.assertIn("No fields to update", result)


class TestListCampaigns(unittest.TestCase):

    @patch("linkedin_ads_server.linkedin_paginated_request")
    def test_list_campaigns(self, mock_req):
        mock_req.return_value = [
            {"id": "urn:li:sponsoredCampaign:50", "name": "My Campaign",
             "status": "ACTIVE", "type": "SPONSORED_UPDATES",
             "objectiveType": "WEBSITE_VISITS",
             "dailyBudget": {"amount": "50", "currencyCode": "USD"},
             "runSchedule": {}},
        ]
        result = run_async(server.list_campaigns(
            account_id="511389977", status_filter="", campaign_group_id="", format="table"))
        self.assertIn("My Campaign", result)


class TestGetCampaignDetails(unittest.TestCase):

    @patch("linkedin_ads_server.linkedin_api_request")
    def test_get_campaign_details(self, mock_req):
        mock_req.return_value = {
            "name": "My Campaign",
            "status": "ACTIVE",
            "type": "SPONSORED_UPDATES",
            "objectiveType": "WEBSITE_VISITS",
        }
        result = run_async(server.get_campaign_details(
            account_id="511389977", campaign_id="50"))
        self.assertIn("My Campaign", result)
        self.assertIn("WEBSITE_VISITS", result)


class TestCreateCampaign(unittest.TestCase):

    @patch("linkedin_ads_server.linkedin_api_request")
    def test_create_campaign(self, mock_req):
        mock_req.return_value = {"_created_id": "300", "status_code": 201}
        result = run_async(server.create_campaign(
            account_id="511389977",
            name="New Campaign",
            campaign_group_id="100",
            objective_type="WEBSITE_VISITS",
            campaign_type="SPONSORED_UPDATES",
            daily_budget_amount="50.00",
            daily_budget_currency="USD",
            cost_type="CPM",
            bid_strategy="",
            bid_amount="",
            status="DRAFT",
            start_date="",
            end_date="",
            pacing_strategy="",
            locale_country="US",
            locale_language="en",
        ))
        self.assertIn("created successfully", result)
        self.assertIn("300", result)


class TestUpdateCampaign(unittest.TestCase):

    @patch("linkedin_ads_server.linkedin_api_request")
    def test_update_campaign(self, mock_req):
        mock_req.return_value = {"status_code": 204, "success": True}
        result = run_async(server.update_campaign(
            account_id="511389977", campaign_id="50", name="Updated Name",
            status="", daily_budget_amount="", daily_budget_currency="USD",
            bid_strategy="", bid_amount="", start_date="", end_date="", pacing_strategy=""))
        self.assertIn("updated successfully", result)


class TestPauseResumeCampaign(unittest.TestCase):

    @patch("linkedin_ads_server.linkedin_api_request")
    def test_pause(self, mock_req):
        mock_req.return_value = {"status_code": 204, "success": True}
        result = run_async(server.pause_resume_campaign(
            account_id="511389977", campaign_id="50", action="pause"))
        self.assertIn("updated successfully", result)

    @patch("linkedin_ads_server.linkedin_api_request")
    def test_resume(self, mock_req):
        mock_req.return_value = {"status_code": 204, "success": True}
        result = run_async(server.pause_resume_campaign(
            account_id="511389977", campaign_id="50", action="resume"))
        self.assertIn("updated successfully", result)


class TestListCreatives(unittest.TestCase):

    @patch("linkedin_ads_server.linkedin_paginated_request")
    def test_list_creatives(self, mock_req):
        mock_req.return_value = [
            {"id": "urn:li:sponsoredCreative:700", "status": "ACTIVE",
             "campaign": "urn:li:sponsoredCampaign:50", "type": "SPONSORED_STATUS_UPDATE",
             "intendedStatus": "ACTIVE", "isServing": True},
        ]
        result = run_async(server.list_creatives(
            account_id="511389977", campaign_id="", status_filter="", format="table"))
        self.assertIn("700", result)


class TestGetCreativeDetails(unittest.TestCase):

    @patch("linkedin_ads_server.linkedin_api_request")
    def test_get_creative_details(self, mock_req):
        mock_req.return_value = {
            "id": "700",
            "status": "ACTIVE",
            "content": {"reference": "urn:li:share:12345"},
        }
        result = run_async(server.get_creative_details(
            account_id="511389977", creative_id="700"))
        self.assertIn("700", result)


class TestCreateCreative(unittest.TestCase):

    @patch("linkedin_ads_server.linkedin_api_request")
    def test_create_creative(self, mock_req):
        mock_req.return_value = {"_created_id": "800", "status_code": 201}
        result = run_async(server.create_creative(
            account_id="511389977",
            campaign_id="50",
            content_reference="urn:li:share:12345",
            status="ACTIVE",
            call_to_action="",
            intended_status="ACTIVE",
        ))
        self.assertIn("created successfully", result)
        self.assertIn("800", result)


class TestCampaignAnalytics(unittest.TestCase):

    @patch("linkedin_ads_server.linkedin_paginated_request")
    def test_get_campaign_analytics(self, mock_req):
        mock_req.return_value = [
            {"pivotValue": "urn:li:sponsoredCampaign:50",
             "dateRange": {"start": {"year": 2025, "month": 5, "day": 1},
                           "end": {"year": 2025, "month": 5, "day": 31}},
             "impressions": 1000, "clicks": 50, "costInLocalCurrency": "25.00",
             "landingPageClicks": 40, "likes": 10, "shares": 5,
             "comments": 2, "conversions": 3, "approximateUniqueImpressions": 800},
        ]
        result = run_async(server.get_campaign_analytics(
            account_id="511389977", start_date="2025-05-01", end_date="2025-05-31",
            campaign_ids="", time_granularity="DAILY", format="table"))
        self.assertIn("1000", result)
        self.assertIn("50", result)
        self.assertIn("5.00%", result)  # CTR


class TestAccountAnalytics(unittest.TestCase):

    @patch("linkedin_ads_server.linkedin_paginated_request")
    def test_get_account_analytics(self, mock_req):
        mock_req.return_value = [
            {"pivotValue": "urn:li:sponsoredAccount:511389977",
             "dateRange": {"start": {"year": 2025, "month": 5, "day": 1},
                           "end": {"year": 2025, "month": 5, "day": 31}},
             "impressions": 5000, "clicks": 200, "costInLocalCurrency": "100.00",
             "landingPageClicks": 180, "likes": 50, "shares": 20,
             "comments": 10, "conversions": 15, "approximateUniqueImpressions": 4000},
        ]
        result = run_async(server.get_account_analytics(
            account_id="511389977", start_date="2025-05-01", end_date="2025-05-31",
            time_granularity="DAILY", format="table"))
        self.assertIn("5000", result)
        self.assertIn("4.00%", result)  # CTR


class TestCreativeAnalytics(unittest.TestCase):

    @patch("linkedin_ads_server.linkedin_paginated_request")
    def test_get_creative_analytics(self, mock_req):
        mock_req.return_value = [
            {"pivotValue": "urn:li:sponsoredCreative:700",
             "dateRange": {"start": {"year": 2025, "month": 5, "day": 1},
                           "end": {"year": 2025, "month": 5, "day": 31}},
             "impressions": 500, "clicks": 25, "costInLocalCurrency": "12.50",
             "landingPageClicks": 20, "likes": 5, "shares": 2,
             "comments": 1, "conversions": 2, "approximateUniqueImpressions": 400},
        ]
        result = run_async(server.get_creative_analytics(
            account_id="511389977", start_date="2025-05-01", end_date="2025-05-31",
            creative_ids="", time_granularity="DAILY", format="table"))
        self.assertIn("500", result)


class TestCampaignGroupAnalytics(unittest.TestCase):

    @patch("linkedin_ads_server.linkedin_paginated_request")
    def test_get_campaign_group_analytics(self, mock_req):
        mock_req.return_value = [
            {"pivotValue": "urn:li:sponsoredCampaignGroup:100",
             "dateRange": {"start": {"year": 2025, "month": 5, "day": 1},
                           "end": {"year": 2025, "month": 5, "day": 31}},
             "impressions": 3000, "clicks": 150, "costInLocalCurrency": "75.00",
             "landingPageClicks": 130, "likes": 30, "shares": 12,
             "comments": 5, "conversions": 8, "approximateUniqueImpressions": 2500},
        ]
        result = run_async(server.get_campaign_group_analytics(
            account_id="511389977", start_date="2025-05-01", end_date="2025-05-31",
            campaign_group_ids="", time_granularity="DAILY", format="table"))
        self.assertIn("3000", result)


class TestDemographicAnalytics(unittest.TestCase):

    @patch("linkedin_ads_server.linkedin_paginated_request")
    def test_get_demographic_analytics(self, mock_req):
        mock_req.return_value = [
            {"pivotValue": "urn:li:title:100",
             "dateRange": {"start": {"year": 2025, "month": 5, "day": 1},
                           "end": {"year": 2025, "month": 5, "day": 31}},
             "impressions": 200, "clicks": 10, "costInLocalCurrency": "5.00",
             "landingPageClicks": 8, "likes": 2, "shares": 1,
             "comments": 0, "conversions": 1, "approximateUniqueImpressions": 180},
        ]
        result = run_async(server.get_demographic_analytics(
            account_id="511389977", start_date="2025-05-01", end_date="2025-05-31",
            demographic_type="MEMBER_JOB_TITLE", campaign_ids="",
            time_granularity="ALL", format="table"))
        self.assertIn("200", result)


class TestMultiPivotAnalytics(unittest.TestCase):

    @patch("linkedin_ads_server.linkedin_paginated_request")
    def test_multi_pivot(self, mock_req):
        mock_req.return_value = [
            {"pivotValues": ["urn:li:sponsoredCampaign:50", "urn:li:geo:103644278"],
             "dateRange": {"start": {"year": 2025, "month": 5, "day": 1},
                           "end": {"year": 2025, "month": 5, "day": 31}},
             "impressions": 800, "clicks": 40, "costInLocalCurrency": "20.00",
             "conversions": 2},
        ]
        result = run_async(server.get_multi_pivot_analytics(
            account_id="511389977", start_date="2025-05-01", end_date="2025-05-31",
            pivots="CAMPAIGN,MEMBER_COUNTRY_V2", campaign_ids="",
            time_granularity="ALL", format="table"))
        self.assertIn("800", result)

    def test_too_many_pivots(self):
        result = run_async(server.get_multi_pivot_analytics(
            account_id="511389977", start_date="2025-05-01", end_date="2025-05-31",
            pivots="A,B,C,D", campaign_ids="", time_granularity="ALL", format="table"))
        self.assertIn("Maximum 3 pivots", result)


class TestTargetingFacets(unittest.TestCase):

    @patch("linkedin_ads_server.linkedin_api_request")
    def test_get_targeting_facets(self, mock_req):
        mock_req.return_value = {
            "elements": [
                {"name": "Job Titles", "urn": "urn:li:adTargetingFacet:titles",
                 "availableEntityFinders": ["TYPEAHEAD"]},
            ]
        }
        result = run_async(server.get_targeting_facets(format="table"))
        self.assertIn("Job Titles", result)


class TestTargetingEntities(unittest.TestCase):

    @patch("linkedin_ads_server.linkedin_api_request")
    def test_get_targeting_entities(self, mock_req):
        mock_req.return_value = {
            "elements": [
                {"name": "Software Engineer", "urn": "urn:li:title:100",
                 "facetUrn": "urn:li:adTargetingFacet:titles"},
            ]
        }
        result = run_async(server.get_targeting_entities(
            facet_urn="urn:li:adTargetingFacet:titles", query="software engineer",
            limit=25, format="table"))
        self.assertIn("Software Engineer", result)


class TestEstimateAudienceSize(unittest.TestCase):

    @patch("linkedin_ads_server.linkedin_api_request")
    def test_estimate_audience(self, mock_req):
        mock_req.return_value = {
            "total": 150000,
            "activeCount": 120000,
        }
        criteria = json.dumps({"include": {"and": [{"or": {"urn:li:adTargetingFacet:locations": ["urn:li:geo:103644278"]}}]}})
        result = run_async(server.estimate_audience_size(
            account_id="511389977", targeting_criteria_json=criteria))
        self.assertIn("150000", result)

    def test_invalid_json(self):
        result = run_async(server.estimate_audience_size(
            account_id="511389977", targeting_criteria_json="not json"))
        self.assertIn("Invalid JSON", result)


class TestScheduleCampaign(unittest.TestCase):

    @patch("linkedin_ads_server.linkedin_api_request")
    def test_schedule_campaign(self, mock_req):
        mock_req.return_value = {"status_code": 204, "success": True}
        result = run_async(server.schedule_campaign(
            account_id="511389977", campaign_id="50",
            start_date="2025-07-01", end_date="2025-09-30",
            daily_budget_amount="100.00", daily_budget_currency="USD",
            pacing_strategy="", activate=True))
        self.assertIn("scheduled successfully", result)
        self.assertIn("ACTIVE", result)


class TestScheduleCampaignGroup(unittest.TestCase):

    @patch("linkedin_ads_server.linkedin_api_request")
    def test_schedule_campaign_group(self, mock_req):
        mock_req.return_value = {"status_code": 204, "success": True}
        result = run_async(server.schedule_campaign_group(
            account_id="511389977", group_id="100",
            start_date="2025-07-01", end_date="2025-09-30",
            total_budget_amount="5000.00", total_budget_currency="USD",
            activate=True))
        self.assertIn("scheduled successfully", result)
        self.assertIn("ACTIVE", result)


# ---------------------------------------------------------------------------
# New tools: Company, Comparison, Reach, Audiences, Conversions, Lead Gen, Resolvers
# ---------------------------------------------------------------------------

class TestGetCompanyPerformance(unittest.TestCase):

    @patch("linkedin_ads_server.linkedin_paginated_request")
    def test_get_company_performance(self, mock_req):
        mock_req.return_value = [
            {"pivotValue": "urn:li:organization:12345",
             "dateRange": {"start": {"year": 2025, "month": 5, "day": 1},
                           "end": {"year": 2025, "month": 5, "day": 31}},
             "impressions": 600, "clicks": 30, "costInLocalCurrency": "15.00",
             "landingPageClicks": 25, "likes": 5, "shares": 2,
             "comments": 1, "conversions": 2, "approximateUniqueImpressions": 500},
        ]
        result = run_async(server.get_company_performance(
            account_id="511389977", start_date="2025-05-01", end_date="2025-05-31",
            campaign_ids="", limit=200, format="table"))
        self.assertIn("600", result)
        self.assertIn("Company Performance", result)


class TestComparePerformance(unittest.TestCase):

    @patch("linkedin_ads_server.linkedin_paginated_request")
    def test_compare_performance(self, mock_req):
        period1 = [{"impressions": 1000, "clicks": 50, "costInLocalCurrency": "25.00",
                     "landingPageClicks": 40, "likes": 10, "shares": 5,
                     "comments": 2, "conversions": 3, "approximateUniqueImpressions": 800}]
        period2 = [{"impressions": 1500, "clicks": 80, "costInLocalCurrency": "40.00",
                     "landingPageClicks": 60, "likes": 15, "shares": 8,
                     "comments": 4, "conversions": 5, "approximateUniqueImpressions": 1200}]
        mock_req.side_effect = [period1, period2]

        result = run_async(server.compare_performance(
            account_id="511389977",
            date_range_1_start="2025-04-01", date_range_1_end="2025-04-30",
            date_range_2_start="2025-05-01", date_range_2_end="2025-05-31",
            format="table"))
        self.assertIn("Performance Comparison", result)
        self.assertIn("impressions", result)
        self.assertIn("change", result)


class TestGetAudienceReach(unittest.TestCase):

    @patch("linkedin_ads_server.linkedin_paginated_request")
    def test_get_audience_reach(self, mock_req):
        mock_req.return_value = [
            {"impressions": 5000, "clicks": 200, "costInLocalCurrency": "100.00",
             "approximateUniqueImpressions": 3000},
        ]
        result = run_async(server.get_audience_reach(
            account_id="511389977", start_date="2025-05-01", end_date="2025-05-31",
            format="table"))
        self.assertIn("5,000", result)
        self.assertIn("3,000", result)
        self.assertIn("1.67", result)  # frequency


class TestListSavedAudiences(unittest.TestCase):

    @patch("linkedin_ads_server.linkedin_paginated_request")
    def test_list_saved_audiences(self, mock_req):
        mock_req.return_value = [
            {"id": "urn:li:adSegment:100", "name": "Tech Decision Makers",
             "status": "ACTIVE", "type": "COMPANY_LIST", "matchedCount": 5000},
        ]
        result = run_async(server.list_saved_audiences(
            account_id="511389977", format="table"))
        self.assertIn("Tech Decision Makers", result)

    @patch("linkedin_ads_server.linkedin_paginated_request")
    def test_list_saved_audiences_empty(self, mock_req):
        mock_req.return_value = []
        result = run_async(server.list_saved_audiences(
            account_id="511389977", format="table"))
        self.assertIn("No saved audiences", result)


class TestGetConversionPerformance(unittest.TestCase):

    @patch("linkedin_ads_server.linkedin_paginated_request")
    def test_get_conversion_performance(self, mock_req):
        mock_req.return_value = [
            {"pivotValue": "urn:li:sponsoredCampaign:50",
             "dateRange": {"start": {"year": 2025, "month": 5, "day": 1},
                           "end": {"year": 2025, "month": 5, "day": 31}},
             "impressions": 2000, "clicks": 100, "costInLocalCurrency": "50.00",
             "conversions": 10, "externalWebsiteConversions": 8,
             "externalWebsitePostClickConversions": 6,
             "externalWebsitePostViewConversions": 2},
        ]
        result = run_async(server.get_conversion_performance(
            account_id="511389977", start_date="2025-05-01", end_date="2025-05-31",
            format="table"))
        self.assertIn("Conversion Performance", result)
        self.assertIn("10", result)


class TestGetLeadGenPerformance(unittest.TestCase):

    @patch("linkedin_ads_server.linkedin_paginated_request")
    def test_get_lead_gen_performance(self, mock_req):
        mock_req.return_value = [
            {"pivotValue": "urn:li:sponsoredCampaign:50",
             "impressions": 3000, "clicks": 150, "costInLocalCurrency": "75.00",
             "oneClickLeadFormOpens": 50, "oneClickLeads": 20},
        ]
        result = run_async(server.get_lead_gen_performance(
            account_id="511389977", start_date="2025-05-01", end_date="2025-05-31",
            format="table"))
        self.assertIn("Lead Gen Performance", result)


class TestListConversions(unittest.TestCase):

    @patch("linkedin_ads_server.linkedin_paginated_request")
    def test_list_conversions(self, mock_req):
        mock_req.return_value = [
            {"id": "urn:li:conversion:500", "name": "Website Purchase",
             "type": "URL_BASED", "enabled": True,
             "postClickAttributionWindowSize": 30,
             "viewThroughAttributionWindowSize": 7,
             "attributionType": "LAST_TOUCH_BY_CAMPAIGN"},
        ]
        result = run_async(server.list_conversions(
            account_id="511389977", format="table"))
        self.assertIn("Website Purchase", result)

    @patch("linkedin_ads_server.linkedin_paginated_request")
    def test_list_conversions_empty(self, mock_req):
        mock_req.return_value = []
        result = run_async(server.list_conversions(
            account_id="511389977", format="table"))
        self.assertIn("No conversion tracking rules", result)


class TestListLeadForms(unittest.TestCase):

    @patch("linkedin_ads_server.linkedin_paginated_request")
    def test_list_lead_forms(self, mock_req):
        mock_req.return_value = [
            {"id": "urn:li:leadForm:600", "name": "Contact Us",
             "status": "ACTIVE", "headline": "Get in touch",
             "description": "Fill the form", "createdAt": 1735689600000},
        ]
        result = run_async(server.list_lead_forms(
            account_id="511389977", format="table"))
        self.assertIn("Contact Us", result)

    @patch("linkedin_ads_server.linkedin_paginated_request")
    def test_list_lead_forms_empty(self, mock_req):
        mock_req.return_value = []
        result = run_async(server.list_lead_forms(
            account_id="511389977", format="table"))
        self.assertIn("No Lead Gen Forms", result)


class TestResolveCampaigns(unittest.TestCase):

    @patch("linkedin_ads_server.linkedin_api_request")
    def test_resolve_campaigns(self, mock_req):
        mock_req.return_value = {
            "name": "Summer Campaign",
            "status": "ACTIVE",
            "type": "SPONSORED_UPDATES",
            "objectiveType": "WEBSITE_VISITS",
        }
        result = run_async(server.resolve_campaigns(
            campaign_ids="50,60", account_id="511389977", format="table"))
        self.assertIn("Summer Campaign", result)
        self.assertIn("Resolved Campaigns", result)

    def test_resolve_campaigns_no_ids(self):
        result = run_async(server.resolve_campaigns(
            campaign_ids="", account_id="", format="table"))
        self.assertIn("No campaign IDs", result)


class TestResolveCreatives(unittest.TestCase):

    @patch("linkedin_ads_server.linkedin_api_request")
    def test_resolve_creatives(self, mock_req):
        mock_req.return_value = {
            "status": "ACTIVE",
            "intendedStatus": "ACTIVE",
            "campaign": "urn:li:sponsoredCampaign:50",
            "type": "SPONSORED_STATUS_UPDATE",
        }
        result = run_async(server.resolve_creatives(
            creative_ids="700", account_id="511389977", format="table"))
        self.assertIn("Resolved Creatives", result)
        self.assertIn("ACTIVE", result)


class TestResolveCampaignGroups(unittest.TestCase):

    @patch("linkedin_ads_server.linkedin_api_request")
    def test_resolve_campaign_groups(self, mock_req):
        mock_req.return_value = {"name": "Q3 Group", "status": "ACTIVE"}
        result = run_async(server.resolve_campaign_groups(
            group_ids="100", account_id="511389977", format="table"))
        self.assertIn("Q3 Group", result)
        self.assertIn("Resolved Campaign Groups", result)


class TestResolveAudiences(unittest.TestCase):

    @patch("linkedin_ads_server.linkedin_api_request")
    def test_resolve_audiences(self, mock_req):
        mock_req.return_value = {
            "name": "Enterprise IT", "status": "ACTIVE",
            "type": "COMPANY_LIST", "matchedCount": 3000}
        result = run_async(server.resolve_audiences(
            audience_ids="200", account_id="511389977", format="table"))
        self.assertIn("Enterprise IT", result)
        self.assertIn("Resolved Audiences", result)


class TestResolveAccounts(unittest.TestCase):

    @patch("linkedin_ads_server.linkedin_api_request")
    def test_resolve_accounts(self, mock_req):
        mock_req.return_value = {
            "name": "InnoVint", "status": "ACTIVE",
            "type": "BUSINESS", "currency": "USD"}
        result = run_async(server.resolve_accounts(
            account_ids="511389977", format="table"))
        self.assertIn("InnoVint", result)
        self.assertIn("Resolved Accounts", result)


class TestResources(unittest.TestCase):

    def test_reference_resource(self):
        content = server.linkedin_ads_reference()
        self.assertIn("Entity Hierarchy", content)
        self.assertIn("URN Formats", content)
        self.assertIn("Campaign Objectives", content)

    def test_targeting_guide_resource(self):
        content = server.linkedin_ads_targeting_guide()
        self.assertIn("Targeting Guide", content)
        self.assertIn("JSON Structure", content)


class TestPrompts(unittest.TestCase):

    def test_workflow_prompt(self):
        content = server.linkedin_ads_workflow()
        self.assertIn("Discover accounts", content)
        self.assertIn("list_accounts", content)

    def test_analytics_help_prompt(self):
        content = server.linkedin_ads_analytics_help()
        self.assertIn("impressions", content)
        self.assertIn("DAILY", content)

    def test_campaign_creation_help_prompt(self):
        content = server.linkedin_ads_campaign_creation_help()
        self.assertIn("Create a Campaign Group", content)
        self.assertIn("WEBSITE_VISITS", content)


# ---------------------------------------------------------------------------
# New tools: Bid Strategy, Weekday Scheduling
# ---------------------------------------------------------------------------

class TestSetBidStrategy(unittest.TestCase):

    @patch("linkedin_ads_server.linkedin_api_request")
    def test_set_bid_strategy_success(self, mock_req):
        mock_req.return_value = {"status_code": 204, "success": True}
        result = run_async(server.set_bid_strategy(
            account_id="511389977", campaign_id="50",
            bid_strategy="TARGET_COST", bid_amount="5.00", bid_currency="USD"))
        self.assertIn("bid strategy updated successfully", result)
        self.assertIn("TARGET_COST", result)
        self.assertIn("5.00", result)

    @patch("linkedin_ads_server.linkedin_api_request")
    def test_set_bid_strategy_no_amount(self, mock_req):
        mock_req.return_value = {"status_code": 204, "success": True}
        result = run_async(server.set_bid_strategy(
            account_id="511389977", campaign_id="50",
            bid_strategy="MAXIMUM_DELIVERY", bid_amount="", bid_currency="USD"))
        self.assertIn("MAXIMUM_DELIVERY", result)
        self.assertNotIn("Bid Amount", result)

    def test_set_bid_strategy_invalid(self):
        result = run_async(server.set_bid_strategy(
            account_id="511389977", campaign_id="50",
            bid_strategy="INVALID", bid_amount="", bid_currency="USD"))
        self.assertIn("Invalid bid strategy", result)

    @patch("linkedin_ads_server.linkedin_api_request")
    def test_set_bid_strategy_api_error(self, mock_req):
        mock_req.return_value = {"error": "Unauthorized", "status_code": 401}
        result = run_async(server.set_bid_strategy(
            account_id="511389977", campaign_id="50",
            bid_strategy="MANUAL_CPC", bid_amount="3.00", bid_currency="USD"))
        self.assertIn("Error setting bid strategy", result)


class TestCreateCampaignWithBidStrategy(unittest.TestCase):

    @patch("linkedin_ads_server.linkedin_api_request")
    def test_create_campaign_with_bid_strategy(self, mock_req):
        mock_req.return_value = {"_created_id": "400", "status_code": 201}
        result = run_async(server.create_campaign(
            account_id="511389977",
            name="Strategy Campaign",
            campaign_group_id="100",
            objective_type="WEBSITE_VISITS",
            campaign_type="SPONSORED_UPDATES",
            daily_budget_amount="50.00",
            daily_budget_currency="USD",
            cost_type="CPM",
            bid_strategy="MAXIMUM_DELIVERY",
            bid_amount="",
            status="DRAFT",
            start_date="",
            end_date="",
            pacing_strategy="",
            locale_country="US",
            locale_language="en",
        ))
        self.assertIn("created successfully", result)
        # Verify bidStrategy was in the API call body
        call_args = mock_req.call_args
        body = call_args[1].get("json_body") or call_args[0][2] if len(call_args[0]) > 2 else call_args[1].get("json_body")
        self.assertEqual(body["bidStrategy"], "MAXIMUM_DELIVERY")


class TestUpdateCampaignWithBidStrategy(unittest.TestCase):

    @patch("linkedin_ads_server.linkedin_api_request")
    def test_update_campaign_with_bid_strategy(self, mock_req):
        mock_req.return_value = {"status_code": 204, "success": True}
        result = run_async(server.update_campaign(
            account_id="511389977", campaign_id="50", name="",
            status="", daily_budget_amount="", daily_budget_currency="USD",
            bid_strategy="TARGET_COST", bid_amount="7.00",
            start_date="", end_date="", pacing_strategy=""))
        self.assertIn("updated successfully", result)
        self.assertIn("bidStrategy", result)


class TestAddWeekdaySchedule(unittest.TestCase):

    def setUp(self):
        """Ensure clean state by removing schedules.json if present."""
        self._original_file = server.SCHEDULES_FILE
        self._test_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_schedules.json")
        server.SCHEDULES_FILE = self._test_file
        if os.path.exists(self._test_file):
            os.remove(self._test_file)

    def tearDown(self):
        server.SCHEDULES_FILE = self._original_file
        if os.path.exists(self._test_file):
            os.remove(self._test_file)

    def test_add_weekday_schedule(self):
        result = run_async(server.add_weekday_schedule(
            account_id="511389977", campaign_id="12345",
            campaign_name="Summer Campaign", timezone="America/New_York",
            resume_time="06:00", pause_time="18:00"))
        self.assertIn("added to weekday-only schedule", result)
        self.assertIn("Monday at 06:00", result)
        self.assertIn("Friday at 18:00", result)

        # Verify file was created
        with open(self._test_file) as f:
            data = json.load(f)
        self.assertEqual(len(data["weekday_only"]), 1)
        self.assertEqual(data["weekday_only"][0]["campaign_id"], "12345")

    def test_add_duplicate(self):
        run_async(server.add_weekday_schedule(
            account_id="511389977", campaign_id="12345",
            campaign_name="Summer Campaign", timezone="America/New_York",
            resume_time="06:00", pause_time="18:00"))
        result = run_async(server.add_weekday_schedule(
            account_id="511389977", campaign_id="12345",
            campaign_name="Summer Campaign", timezone="America/New_York",
            resume_time="06:00", pause_time="18:00"))
        self.assertIn("already scheduled", result)


class TestRemoveWeekdaySchedule(unittest.TestCase):

    def setUp(self):
        self._original_file = server.SCHEDULES_FILE
        self._test_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_schedules.json")
        server.SCHEDULES_FILE = self._test_file
        # Pre-populate
        data = {"weekday_only": [{
            "account_id": "511389977",
            "campaign_id": "12345",
            "campaign_name": "Test",
            "timezone": "UTC",
            "resume_time": "06:00",
            "pause_time": "18:00",
            "added_at": "2025-06-01T00:00:00",
        }]}
        with open(self._test_file, "w") as f:
            json.dump(data, f)

    def tearDown(self):
        server.SCHEDULES_FILE = self._original_file
        if os.path.exists(self._test_file):
            os.remove(self._test_file)

    def test_remove_existing(self):
        result = run_async(server.remove_weekday_schedule(
            account_id="511389977", campaign_id="12345"))
        self.assertIn("removed from weekday-only scheduling", result)

        with open(self._test_file) as f:
            data = json.load(f)
        self.assertEqual(len(data["weekday_only"]), 0)

    def test_remove_nonexistent(self):
        result = run_async(server.remove_weekday_schedule(
            account_id="511389977", campaign_id="99999"))
        self.assertIn("not found", result)


class TestListWeekdaySchedules(unittest.TestCase):

    def setUp(self):
        self._original_file = server.SCHEDULES_FILE
        self._test_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_schedules.json")
        server.SCHEDULES_FILE = self._test_file

    def tearDown(self):
        server.SCHEDULES_FILE = self._original_file
        if os.path.exists(self._test_file):
            os.remove(self._test_file)

    def test_list_empty(self):
        if os.path.exists(self._test_file):
            os.remove(self._test_file)
        result = run_async(server.list_weekday_schedules(format="table"))
        self.assertIn("No weekday-only schedules", result)

    def test_list_with_rules(self):
        data = {"weekday_only": [
            {"account_id": "511389977", "campaign_id": "12345",
             "campaign_name": "Summer Campaign", "timezone": "America/New_York",
             "resume_time": "06:00", "pause_time": "18:00", "added_at": "2025-06-01T00:00:00"},
            {"account_id": "511389977", "campaign_id": "67890",
             "campaign_name": "Winter Campaign", "timezone": "UTC",
             "resume_time": "07:00", "pause_time": "17:00", "added_at": "2025-06-02T00:00:00"},
        ]}
        with open(self._test_file, "w") as f:
            json.dump(data, f)

        result = run_async(server.list_weekday_schedules(format="table"))
        self.assertIn("12345", result)
        self.assertIn("67890", result)
        self.assertIn("Summer Campaign", result)
        self.assertIn("Winter Campaign", result)


class TestRunWeekdayScheduler(unittest.TestCase):

    def setUp(self):
        self._original_file = server.SCHEDULES_FILE
        self._test_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_schedules.json")
        server.SCHEDULES_FILE = self._test_file

    def tearDown(self):
        server.SCHEDULES_FILE = self._original_file
        if os.path.exists(self._test_file):
            os.remove(self._test_file)

    def test_no_rules(self):
        if os.path.exists(self._test_file):
            os.remove(self._test_file)
        result = run_async(server.run_weekday_scheduler())
        self.assertIn("No weekday-only schedules", result)

    @patch("linkedin_ads_server.linkedin_api_request")
    @patch("linkedin_ads_server.datetime")
    def test_weekday_resumes(self, mock_dt, mock_req):
        """On a Tuesday, campaigns should be set to ACTIVE."""
        from zoneinfo import ZoneInfo
        mock_dt.now.return_value = real_datetime(2025, 6, 3, 10, 0, tzinfo=ZoneInfo("UTC"))  # Tuesday
        mock_dt.side_effect = lambda *a, **kw: real_datetime(*a, **kw)
        mock_req.return_value = {"status_code": 204, "success": True}

        data = {"weekday_only": [{
            "account_id": "511389977", "campaign_id": "12345",
            "campaign_name": "Test", "timezone": "UTC",
            "resume_time": "06:00", "pause_time": "18:00",
            "added_at": "2025-06-01T00:00:00",
        }]}
        with open(self._test_file, "w") as f:
            json.dump(data, f)

        result = run_async(server.run_weekday_scheduler())
        self.assertIn("ACTIVE", result)
        self.assertIn("Processed 1 rule(s)", result)

    @patch("linkedin_ads_server.linkedin_api_request")
    @patch("linkedin_ads_server.datetime")
    def test_weekend_pauses(self, mock_dt, mock_req):
        """On a Saturday, campaigns should be set to PAUSED."""
        from zoneinfo import ZoneInfo
        mock_dt.now.return_value = real_datetime(2025, 6, 7, 10, 0, tzinfo=ZoneInfo("UTC"))  # Saturday
        mock_dt.side_effect = lambda *a, **kw: real_datetime(*a, **kw)
        mock_req.return_value = {"status_code": 204, "success": True}

        data = {"weekday_only": [{
            "account_id": "511389977", "campaign_id": "12345",
            "campaign_name": "Test", "timezone": "UTC",
            "resume_time": "06:00", "pause_time": "18:00",
            "added_at": "2025-06-01T00:00:00",
        }]}
        with open(self._test_file, "w") as f:
            json.dump(data, f)

        result = run_async(server.run_weekday_scheduler())
        self.assertIn("PAUSED", result)
        self.assertIn("Processed 1 rule(s)", result)

    @patch("linkedin_ads_server.linkedin_api_request")
    @patch("linkedin_ads_server.datetime")
    def test_friday_evening_pauses(self, mock_dt, mock_req):
        """On Friday at 19:00, campaigns should be set to PAUSED."""
        from zoneinfo import ZoneInfo
        mock_dt.now.return_value = real_datetime(2025, 6, 6, 19, 0, tzinfo=ZoneInfo("UTC"))  # Friday 19:00
        mock_dt.side_effect = lambda *a, **kw: real_datetime(*a, **kw)
        mock_req.return_value = {"status_code": 204, "success": True}

        data = {"weekday_only": [{
            "account_id": "511389977", "campaign_id": "12345",
            "campaign_name": "Test", "timezone": "UTC",
            "resume_time": "06:00", "pause_time": "18:00",
            "added_at": "2025-06-01T00:00:00",
        }]}
        with open(self._test_file, "w") as f:
            json.dump(data, f)

        result = run_async(server.run_weekday_scheduler())
        self.assertIn("PAUSED", result)


if __name__ == "__main__":
    unittest.main()
