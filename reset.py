import shutil
import os


PLATFORMS = [
    "instagram",
    "tiktok",
]


def reset_database():
    path = "./chroma_db"

    if os.path.exists(path):
        shutil.rmtree(path)
        print("Database reset")
    else:
        print("Database does not exist")


def delete_urls(platform):
    file_path = f"{platform}_urls.json"

    if os.path.exists(file_path):
        os.remove(file_path)
        print(f"Deleted {file_path}")
    else:
        print(f"{file_path} does not exist")


def delete_username(platform):
    file_path = f"{platform}_username.txt"

    if os.path.exists(file_path):
        os.remove(file_path)
        print(f"Deleted {file_path}")
    else:
        print(f"{file_path} does not exist")


def delete_videos():
    folder_path = "videos"

    if os.path.exists(folder_path):
        shutil.rmtree(folder_path)
        print("Deleted videos folder")
    else:
        print("Videos folder does not exist")


def reset_platform(platform):
    platform = platform.lower()

    delete_urls(platform)
    delete_username(platform)


def reset_all():
    reset_database()
    delete_videos()

    for platform in PLATFORMS:
        reset_platform(platform)


reset_all()
