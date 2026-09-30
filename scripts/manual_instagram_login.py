"""Open an Instagram login window by hand. Needs INSTAGRAM_USERNAME.

Run from the repo root: python -m scripts.manual_instagram_login
"""
import os

from src.instagram import Instagram

username = os.environ["INSTAGRAM_USERNAME"]
instagram = Instagram(username)
instagram.log_in()
input("Press Enter to close the browser...")
