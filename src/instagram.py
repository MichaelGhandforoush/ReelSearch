""" from selenium import webdriver
from selenium.webdriver.chrome.options import Options
import time
import json
import os
from selenium.webdriver.common.by import By

class Instagram:
    def __init__(self, username):
        self.options = Options()
        self.options.add_argument(
        r"--user-data-dir=C:\\selenium\\instagram_profle")
        #self.browser = None
        self.driver = None
        self.username = username

    def log_in(self):
        #self.browser = webdriver.Chrome()
        self.driver = webdriver.Chrome(options=self.options)
        self.driver.get(f"https://www.instagram.com/{self.username}/saved/all-posts/")
    
    def load_urls(self, filename):
        if os.path.exists(filename):
            with open(filename, "r") as f:
                return set(json.load(f))
        return set()


    def save_urls(self, urls, filename):
        with open(filename, "w") as f:
            json.dump(list(urls), f, indent=2)


    def scrape_instagram_videos(self, filename="instagram_urls.json"):
        max_videos = 100
        # Load previous progress
        urls = self.load_urls(filename)

        print(f"Starting with {len(urls)} existing URLs")

        no_new_count = 0

        #while True:
        for i in range(max_videos):
            old_count = len(urls)

            # Find currently loaded links
            links = self.driver.find_elements(
                By.TAG_NAME,
                "a"
            )

            for link in links:
                try:
                    href = link.get_attribute("href")

                    if href and (
                        "/reel/" in href or
                        "/p/" in href
                    ):
                        urls.add(href)

                except:
                    pass


            # Save immediately after collecting
            if len(urls) > old_count:
                self.save_urls(urls, filename)
                if(len(urls)%10==0):
                    print(f"Saved {len(urls)} URLs")


            # Scroll down
            self.driver.execute_script(
                "window.scrollTo(0, document.body.scrollHeight);"
            )

            time.sleep(1)


            # Stop if nothing new appears
            if len(urls) == old_count:
                no_new_count += 1
            else:
                no_new_count = 0


            # Require multiple failed scrolls before stopping
            if no_new_count >= 5:
                print("No more videos found")
                break


        return urls
 """
from .base_platform import Platform


class Instagram(Platform):
    abbreviation = "IG"

    def __init__(
        self, username, account_id=None, profile_path=None, urls_path=None
    ):
        super().__init__(
            username,
            "instagram",
            profile_path or r"C:\selenium\instagram_profile",
            account_id=account_id,
            urls_path=urls_path,
        )

    def get_saved_url(self):
        return (
            f"https://www.instagram.com/{self.username}/saved/all-posts/"
        )

    def get_account_url(self):
        return f"https://www.instagram.com/{self.username}/"

    def is_video_url(self, url):
        return "/reel/" in url or "/p/" in url