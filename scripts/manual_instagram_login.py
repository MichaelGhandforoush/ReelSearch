import os

from src.instagram import Instagram

username = os.environ["INSTAGRAM_USERNAME"]
instagram = Instagram(username)
instagram.log_in()
input("Press Enter to close the browser...")
