// Loaded in <head> so the saved theme applies before the page paints.
(function () {
    const root = document.documentElement;
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    // Clicks closer together than this count as one quick succession.
    const QUICK_CLICK_MS = 500;
    const PRIDE_CLICKS = 3;

    function savedTheme() {
        try {
            return localStorage.getItem("theme");
        } catch (error) {
            return null;
        }
    }

    function saveTheme(theme) {
        try {
            if (theme) localStorage.setItem("theme", theme);
            else localStorage.removeItem("theme");
        } catch (error) {
            // Theme still applies for this page view.
        }
    }

    function setThemeAttribute(theme) {
        if (theme) root.dataset.theme = theme;
        else delete root.dataset.theme;
    }

    function currentTheme() {
        return root.dataset.theme || (media.matches ? "dark" : "light");
    }

    function syncToggles() {
        const next = currentTheme() === "dark" ? "light" : "dark";
        document.querySelectorAll("[data-theme-toggle]").forEach((button) => {
            button.setAttribute("aria-label", `Switch to ${next} mode`);
            button.title = `Switch to ${next} mode`;
        });
    }

    const saved = savedTheme();
    if (saved === "light" || saved === "dark") setThemeAttribute(saved);

    let quickClicks = 0;
    let lastClick = 0;
    // The theme as it was before the current run of quick clicks began.
    let beforeClicks = null;

    document.addEventListener("click", (event) => {
        if (!event.target.closest("[data-theme-toggle]")) return;

        // Any click while in pride mode just leaves it; the theme underneath
        // is exactly what it was before pride mode, so nothing else changes.
        if (root.dataset.pride) {
            delete root.dataset.pride;
            quickClicks = 0;
            return;
        }

        const now = Date.now();
        if (now - lastClick > QUICK_CLICK_MS || quickClicks === 0) {
            quickClicks = 0;
            beforeClicks = {
                attribute: root.dataset.theme || null,
                saved: savedTheme(),
            };
        }
        lastClick = now;
        quickClicks += 1;

        if (quickClicks >= PRIDE_CLICKS) {
            // Undo the toggles from this run so leaving pride mode is
            // seamless, then show the flag.
            setThemeAttribute(beforeClicks.attribute);
            saveTheme(beforeClicks.saved);
            root.dataset.pride = "true";
            quickClicks = 0;
        } else {
            const next = currentTheme() === "dark" ? "light" : "dark";
            setThemeAttribute(next);
            saveTheme(next);
        }
        syncToggles();
    });
    media.addEventListener("change", syncToggles);
    document.addEventListener("DOMContentLoaded", syncToggles);
})();
