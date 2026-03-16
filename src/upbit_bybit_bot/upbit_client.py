from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator
from urllib.parse import parse_qs, urljoin, urlparse
from zoneinfo import ZoneInfo

from playwright.sync_api import BrowserContext, TimeoutError, sync_playwright

from upbit_bybit_bot.models import ListingSignal, NoticeSummary

LOGGER = logging.getLogger(__name__)
SEOUL = ZoneInfo("Asia/Seoul")
NOTICE_URL = "https://upbit.com/service_center/notice"
LISTING_KEYWORDS = ("신규 거래지원 안내", "거래지원 안내")
MARKET_SYMBOLS = {"KRW", "BTC", "USDT"}


class UpbitNoticeClient:
    def __init__(self, headless: bool = True) -> None:
        self.headless = headless

    def list_recent_notices(self, limit: int = 30) -> list[NoticeSummary]:
        with self._browser_context() as context:
            page = context.new_page()
            page.goto(NOTICE_URL, wait_until="domcontentloaded")
            page.wait_for_load_state("networkidle")
            page.wait_for_selector("a[href*='/service_center/notice?id=']", timeout=15000)

            anchors = page.locator("a[href*='/service_center/notice?id=']")
            notices: list[NoticeSummary] = []
            seen_ids: set[str] = set()

            for index in range(min(limit, anchors.count())):
                anchor = anchors.nth(index)
                href = anchor.get_attribute("href")
                if not href:
                    continue

                notice_id = self._extract_notice_id(href)
                if not notice_id or notice_id in seen_ids:
                    continue

                title = self._clean_text(anchor.inner_text())
                row = anchor.locator("xpath=ancestor::tr[1]")
                row_text = self._clean_text(row.inner_text()) if row.count() else title
                notices.append(
                    NoticeSummary(
                        notice_id=notice_id,
                        title=title,
                        url=urljoin(NOTICE_URL, href),
                        published_at=self._extract_date(row_text),
                    )
                )
                seen_ids.add(notice_id)

            return notices

    def extract_listing_signals(self, notice: NoticeSummary) -> list[ListingSignal]:
        if not any(keyword in notice.title for keyword in LISTING_KEYWORDS):
            return []

        with self._browser_context() as context:
            page = context.new_page()
            page.goto(notice.url, wait_until="domcontentloaded")
            page.wait_for_load_state("networkidle")
            body_text = self._clean_text(page.locator("body").inner_text())

        listing_at = self._extract_listing_at(body_text)
        symbols = self._extract_asset_symbols(f"{notice.title} {body_text}")
        detected_at = datetime.now(tz=SEOUL)

        signals = [
            ListingSignal(
                notice_id=notice.notice_id,
                title=notice.title,
                url=notice.url,
                asset_symbol=symbol,
                listing_at=listing_at,
                detected_at=detected_at,
            )
            for symbol in symbols
        ]

        if not signals:
            LOGGER.warning("Could not extract asset symbols from Upbit notice %s", notice.url)

        return signals

    @contextmanager
    def _browser_context(self) -> Iterator[BrowserContext]:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=self.headless)
            context = browser.new_context(locale="ko-KR", timezone_id="Asia/Seoul")
            try:
                yield context
            except TimeoutError as error:
                raise RuntimeError("Timed out while loading Upbit notice pages") from error
            finally:
                browser.close()

    @staticmethod
    def _extract_notice_id(href: str) -> str | None:
        parsed = urlparse(href)
        values = parse_qs(parsed.query).get("id")
        if not values:
            return None
        return values[0]

    @staticmethod
    def _extract_date(text: str) -> datetime | None:
        match = re.search(r"(20\d{2})[.-](\d{2})[.-](\d{2})", text)
        if not match:
            return None
        year, month, day = (int(value) for value in match.groups())
        return datetime(year, month, day, tzinfo=SEOUL)

    @staticmethod
    def _extract_asset_symbols(text: str) -> list[str]:
        matches = re.findall(r"\(([A-Z0-9]{2,15})\)", text)
        symbols: list[str] = []
        seen: set[str] = set()
        for symbol in matches:
            if symbol in MARKET_SYMBOLS or symbol in seen:
                continue
            symbols.append(symbol)
            seen.add(symbol)
        return symbols

    @staticmethod
    def _extract_listing_at(text: str) -> datetime | None:
        candidate_lines = [
            line
            for line in re.split(r"[\r\n]+", text)
            if any(keyword in line for keyword in ("거래지원", "거래 지원", "개시", "시작", "예정"))
        ]
        for line in candidate_lines or [text]:
            match = re.search(
                r"(20\d{2})[./-]\s*(\d{1,2})[./-]\s*(\d{1,2})(?:\([^)]*\))?\s*(\d{1,2}):(\d{2})",
                line,
            )
            if not match:
                continue
            year, month, day, hour, minute = (int(value) for value in match.groups())
            return datetime(year, month, day, hour, minute, tzinfo=SEOUL)
        return None

    @staticmethod
    def _clean_text(value: str) -> str:
        return re.sub(r"\s+", " ", value).strip()
