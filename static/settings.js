// Preferences kept in this browser. A settings page can read and change
// them with ReelSearchSettings.get / set; code that cares about a setting
// listens for the "reelsearch:settingschange" event on window.
window.ReelSearchSettings = (function () {
    const STORAGE_KEY = "reelsearch.settings";
    const DEFAULTS = {
        // Full-screen viewer: play the video on screen, pause it when
        // scrolling away and start the next one when it is reached.
        viewerAutoplay: true
    };

    function stored() {
        try {
            const value = JSON.parse(window.localStorage.getItem(STORAGE_KEY));
            return value && typeof value === "object" ? value : {};
        } catch (error) {
            return {};
        }
    }

    function get(name) {
        if (!(name in DEFAULTS)) throw new Error(`Unknown setting: ${name}`);
        const value = stored()[name];
        return typeof value === typeof DEFAULTS[name] ? value : DEFAULTS[name];
    }

    function set(name, value) {
        if (!(name in DEFAULTS)) throw new Error(`Unknown setting: ${name}`);
        if (typeof value !== typeof DEFAULTS[name]) {
            throw new TypeError(`${name} must be a ${typeof DEFAULTS[name]}`);
        }
        const values = stored();
        values[name] = value;
        try {
            window.localStorage.setItem(STORAGE_KEY, JSON.stringify(values));
        } catch (error) {
            // Storage unavailable: the change applies until the page closes.
        }
        window.dispatchEvent(new CustomEvent("reelsearch:settingschange", {
            detail: { name, value }
        }));
    }

    function all() {
        return Object.fromEntries(Object.keys(DEFAULTS).map((name) => [name, get(name)]));
    }

    return { get, set, all, defaults: { ...DEFAULTS } };
})();
