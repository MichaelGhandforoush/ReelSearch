Requires Python 3.10.

Navigate into the project (the folder containing app.py):
cd ReelSearch

Create and activate a virtual environment:
python -m venv .venv
.venv\Scripts\activate          (Windows)
source .venv/bin/activate       (macOS / Linux)

Install dependencies:
pip install -r requirements.txt

requirements.txt lists the tested versions. torch, torchvision and
bitsandbytes accept any release in the tested series; for a CUDA build of
torch, install it from https://pytorch.org before running the line above.

Install Tesseract OCR (optional, reads on-screen text in videos):
This is a separate program, not a pip package.
Windows: https://github.com/UB-Mannheim/tesseract/wiki
macOS:   brew install tesseract
Linux:   sudo apt install tesseract-ocr
If tesseract is not on PATH, set TESSERACT_CMD to the full path of the
executable, e.g. C:\Program Files\Tesseract-OCR\tesseract.exe
Without it, videos are indexed without on-screen text.

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


Tests:
pip install -r requirements-dev.txt
pytest

Scripts in scripts/ open real browsers and are not tests. Run them from
the project folder, e.g. python -m scripts.manual_drivers
