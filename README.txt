Navigate into the project:
cd reelsearch

Create virtual enviroment:
python -m venv venv

Install dependencies:
pip install -r requirements.txt

Start the server:
python app.py

Open:
http://127.0.0.1:5000


Instagram login:
On first use write Instagram username and login through the opened browser
Start a sync.
Future sessions reuse the stored browser profile and reels.

Account management:
Use All accounts on the library page to add multiple Instagram or TikTok
accounts. Each account gets its own browser profile, saved URL cache, sync
state, and indexed video ownership. Login remains manual in the Selenium
browser; ReelSearch does not store passwords.
