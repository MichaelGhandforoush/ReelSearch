from src.base_platform import Platform


class TikTok(Platform):
    abbreviation = "TT"
    cookie_domain = "tiktok.com"

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

    def get_account_url(self):
        return f"https://www.tiktok.com/@{self.username}"

    def is_video_url(self, url):
        return "/video/" in url
        