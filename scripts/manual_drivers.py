"""Check that Instagram and TikTok get separate browsers and profiles.

Run from the repo root: python -m scripts.manual_drivers
"""
from src.instagram import Instagram
from src.tiktok import TikTok


def main():
    print("Creating Instagram...")
    instagram = Instagram("test_user")

    print("Creating TikTok...")
    tiktok = TikTok("UngAngell420")

    print("\n--- OBJECTS ---")
    print(f"Instagram object: {id(instagram)}")
    print(f"TikTok object:    {id(tiktok)}")

    print("\n--- PLATFORMS ---")
    print(f"Instagram platform: {instagram.platform}")
    print(f"TikTok platform:    {tiktok.platform}")

    print("\n--- PROFILES ---")
    print(f"Instagram profile: {instagram.profile_path}")
    print(f"TikTok profile:    {tiktok.profile_path}")

    print("\n--- CREATING DRIVERS ---")

    tiktok._create_driver()
    print("TikTok driver created.")


    instagram._create_driver()
    print("Instagram driver created.")

    print("\n--- DRIVERS ---")
    print(f"Instagram driver: {id(instagram.driver)}")
    print(f"TikTok driver:    {id(tiktok.driver)}")

    print("\n--- CHECKS ---")

    print(
        "Different objects:",
        instagram is not tiktok
    )

    print(
        "Different drivers:",
        instagram.driver is not tiktok.driver
    )

    print(
        "Different profiles:",
        instagram.profile_path != tiktok.profile_path
    )

    print("\n--- RESULTS ---")

    if instagram.profile_path == tiktok.profile_path:
        print("❌ ERROR: Both platforms use the SAME Chrome profile!")

    elif instagram.driver is tiktok.driver:
        print("❌ ERROR: Both platforms use the SAME Selenium driver!")

    else:
        print("✅ Instagram and TikTok have separate drivers and profiles.")

    input("\nPress Enter to close Chrome...")

    if instagram.driver:
        instagram.driver.quit()

    if tiktok.driver:
        tiktok.driver.quit()


if __name__ == "__main__":
    main()