from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions
from selenium.webdriver.support.ui import WebDriverWait

from src.base_platform import Platform


class TikTok(Platform):
    abbreviation = "TT"
    cookie_domain = "tiktok.com"

    # TikTok's page layout, kept here so a change on their side is a
    # one-line fix.
    favorites_tab_selector = '[data-e2e="favorites-tab"]'
    video_link_selector = 'a[href*="/video/"]'
    favorites_wait_seconds = 15

    def __init__(
        self, username, account_id=None, profile_path=None, urls_path=None
    ):
        super().__init__(
            username,
            "tiktok",
            profile_path or r"C:\selenium\tiktok_profile",
            account_id=account_id,
            urls_path=urls_path,
        )

    def get_saved_url(self):
        # TikTok keeps favorites behind the profile UI rather than a stable URL.
        return f"https://www.tiktok.com/@{self.username}"

    def prepare_saved_page(self):
        """Switch the profile page to the Favorites tab."""
        wait = WebDriverWait(self.driver, self.favorites_wait_seconds)
        try:
            tab = wait.until(expected_conditions.element_to_be_clickable(
                (By.CSS_SELECTOR, self.favorites_tab_selector)
            ))
        except TimeoutException:
            raise RuntimeError(
                "Could not open the TikTok Favorites tab; the page layout "
                "may have changed."
            ) from None
        # The profile opens on the account's own videos; waiting for them to
        # be replaced keeps them out of the collected favorites.
        own_videos = self.driver.find_elements(
            By.CSS_SELECTOR, self.video_link_selector
        )
        tab.click()
        try:
            if own_videos:
                wait.until(expected_conditions.staleness_of(own_videos[0]))
            wait.until(expected_conditions.presence_of_element_located(
                (By.CSS_SELECTOR, self.video_link_selector)
            ))
        except TimeoutException:
            # Empty or private favorites: nothing to collect, not an error.
            print("No TikTok favorites showed up; the tab may be empty.")

    def get_account_url(self):
        return f"https://www.tiktok.com/@{self.username}"

    def is_video_url(self, url):
        return "/video/" in url
