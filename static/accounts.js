// Account settings: a dropdown per account, laid out like the advanced
// search on the library page. Each change is saved straight away without
// reloading, so the panel stays open while several settings are changed.
(function () {
    const SAVED_MESSAGE_MS = 1800;

    document.querySelectorAll("[data-account-settings]").forEach((form) => {
        const toggle = form.querySelector(".account-settings-toggle");
        const panel = form.querySelector(".account-settings-panel");
        const status = form.querySelector(".account-settings-status");
        const tabs = [...form.querySelectorAll('[role="tab"]')];
        let statusTimer = null;
        let pending = Promise.resolve();

        function setOpen(open) {
            panel.hidden = !open;
            toggle.setAttribute("aria-expanded", String(open));
        }

        function selectTab(tab, focus) {
            tabs.forEach((item) => {
                const selected = item === tab;
                item.setAttribute("aria-selected", String(selected));
                item.tabIndex = selected ? 0 : -1;
                document.getElementById(
                    item.getAttribute("aria-controls")
                ).hidden = !selected;
            });
            if (focus) tab.focus();
        }

        function showStatus(text, isError) {
            window.clearTimeout(statusTimer);
            status.textContent = text;
            status.classList.toggle("is-error", Boolean(isError));
            if (!isError) {
                statusTimer = window.setTimeout(() => {
                    status.textContent = "";
                }, SAVED_MESSAGE_MS);
            }
        }

        function save() {
            // The form is read when the save starts, and saves run one at a
            // time, so the last change made is the one that ends up stored.
            const body = new FormData(form);
            pending = pending.then(() => fetch(form.action, {
                method: "POST",
                body,
                headers: { "X-Requested-With": "XMLHttpRequest" }
            }).then((response) => {
                if (!response.ok) throw new Error(response.statusText);
                showStatus("Saved");
            }).catch(() => {
                showStatus("Could not save. Try again.", true);
            }));
        }

        toggle.addEventListener("click", () => setOpen(panel.hidden));
        tabs.forEach((tab, index) => {
            tab.addEventListener("click", () => selectTab(tab, false));
            tab.addEventListener("keydown", (event) => {
                const step = { ArrowRight: 1, ArrowLeft: -1 }[event.key];
                if (!step) return;
                event.preventDefault();
                selectTab(tabs[(index + step + tabs.length) % tabs.length], true);
            });
        });
        panel.addEventListener("keydown", (event) => {
            if (event.key === "Escape") {
                event.stopPropagation();
                setOpen(false);
                toggle.focus();
            }
        });
        form.querySelectorAll("[data-autosubmit]").forEach((input) => {
            input.addEventListener("change", save);
        });
    });
})();

// Storage: how much disk ReelSearch uses, and leftovers that can be removed
// once the person has confirmed what will be deleted.
(function () {
    const panel = document.querySelector("[data-storage]");
    if (!panel) return;
    const usage = panel.querySelector("[data-storage-usage]");
    const form = panel.querySelector("[data-storage-form]");
    const list = panel.querySelector("[data-storage-list]");
    const button = panel.querySelector("[data-storage-button]");
    const status = panel.querySelector("[data-storage-status]");
    const LABELS = {
        profiles: ["Browser profiles", "Logins of removed accounts and unfinished logins."],
        info_files: ["Video info files", "Written next to downloads by older versions."],
        media: ["Leftover downloads", "Videos a stopped sync did not get to delete."],
        sync_state: ["Old sync progress", "Kept for accounts that no longer exist."],
        url_files: ["Old video lists", "Saved-video lists of accounts that no longer exist."],
    };
    let busy = false;

    function size(bytes) {
        const units = ["B", "KB", "MB", "GB"];
        let value = bytes;
        let unit = 0;
        while (value >= 1024 && unit < units.length - 1) {
            value /= 1024;
            unit += 1;
        }
        return `${value.toFixed(unit && value < 10 ? 1 : 0)} ${units[unit]}`;
    }

    function showStatus(text, isError) {
        status.textContent = text;
        status.classList.toggle("is-error", Boolean(isError));
    }

    function render(report) {
        busy = report.busy;
        const used = report.usage;
        usage.textContent = (
            `Library ${size(used.library)} · Downloads ${size(used.videos)} · `
            + `Browser profiles ${size(used.profiles)}. `
            + (report.reclaimable
                ? `${size(report.reclaimable)} can be freed.`
                : "Nothing to clean up.")
        );
        list.replaceChildren();
        Object.entries(report.categories).forEach(([name, category]) => {
            if (!category.count) return;
            const [label, note] = LABELS[name] || [name, ""];
            const row = document.createElement("label");
            const box = document.createElement("input");
            box.type = "checkbox";
            box.name = "category";
            box.value = name;
            box.checked = true;
            box.dataset.label = `${label} (${category.count})`;
            const hint = document.createElement("span");
            hint.className = "account-setting-note";
            hint.textContent = note;
            row.append(
                box, ` ${label}: ${category.count}, ${size(category.bytes)}`, hint
            );
            list.append(row);
        });
        form.hidden = !list.children.length;
        button.disabled = false;
        if (busy) showStatus("Clean up is available once syncs and logins finish.");
    }

    function load() {
        return fetch(panel.dataset.reportUrl, {
            headers: { "X-Requested-With": "XMLHttpRequest" }
        }).then((response) => {
            if (!response.ok) throw new Error(response.statusText);
            return response.json();
        }).then(render).catch(() => {
            usage.textContent = "Could not check disk use.";
        });
    }

    form.addEventListener("submit", (event) => {
        event.preventDefault();
        const chosen = [...form.querySelectorAll('input[name="category"]:checked')];
        if (!chosen.length) {
            showStatus("Choose what to clean up.", true);
            return;
        }
        const summary = chosen.map((box) => `- ${box.dataset.label}`).join("\n");
        const warning = chosen.some((box) => box.value === "profiles")
            ? "\n\nRemoved browser profiles cannot be used to log in again."
            : "";
        if (!window.confirm(
            `Permanently delete these from this computer?\n\n${summary}${warning}`
        )) return;
        button.disabled = true;
        showStatus("Cleaning up…");
        fetch(form.action, {
            method: "POST",
            body: new FormData(form),
            headers: { "X-Requested-With": "XMLHttpRequest" }
        }).then((response) => response.json().then((body) => {
            if (!response.ok) throw new Error(body.error || response.statusText);
            render(body.storage);
            showStatus(body.failed.length
                ? `Done, but ${body.failed.length} could not be removed (still in use?). Try again later.`
                : "Cleaned up.", Boolean(body.failed.length));
        })).catch((error) => {
            button.disabled = false;
            showStatus(error.message || "Could not clean up. Try again.", true);
        });
    });

    load();
})();
