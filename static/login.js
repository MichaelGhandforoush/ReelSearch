// Waiting page for a login that is open in another browser window. The
// account is only saved by the server once the login is really finished; this
// page shows how it is going and lets the person confirm or cancel.
(function () {
    const root = document.querySelector("#login-waiting");
    if (!root) return;

    const statusUrl = root.dataset.statusUrl;
    const confirmUrl = root.dataset.confirmUrl;
    const cancelUrl = root.dataset.cancelUrl;
    const doneUrl = root.dataset.doneUrl;
    const leaveUrl = root.dataset.leaveUrl;

    const statusText = document.querySelector("#login-status");
    const notice = document.querySelector("#login-notice");
    const errorBox = document.querySelector("#login-error");
    const actions = document.querySelector("#login-actions");
    const retry = document.querySelector("#login-retry");
    const continueButton = document.querySelector("#login-continue");
    const cancelButton = document.querySelector("#login-cancel");
    const POLL_MS = 1500;

    let finished = false;
    let checking = false;
    let timer = null;

    const MESSAGES = {
        opening: "Opening the browser…",
        waiting: "Waiting for you to log in…"
    };

    function fail(message) {
        finished = true;
        clearTimeout(timer);
        statusText.hidden = true;
        notice.hidden = true;
        actions.hidden = true;
        errorBox.textContent = message;
        errorBox.hidden = false;
        retry.hidden = false;
    }

    function show(data) {
        if (finished) return;
        if (data.status === "connected") {
            finished = true;
            statusText.textContent = "Connected. Taking you to your library…";
            statusText.classList.add("is-done");
            actions.hidden = true;
            window.location.assign(data.done_url || doneUrl);
            return;
        }
        if (data.status === "cancelled") {
            finished = true;
            window.location.assign(leaveUrl);
            return;
        }
        if (data.status === "failed" || data.status === "unknown") {
            fail(data.error || "The login could not be completed.");
            return;
        }
        if (!checking) statusText.textContent = MESSAGES[data.status] || "";
        notice.textContent = data.notice || "";
        notice.hidden = !data.notice;
        if (data.notice && checking) {
            checking = false;
            continueButton.disabled = false;
            continueButton.textContent = "Continue";
            statusText.textContent = MESSAGES.waiting;
        }
    }

    async function poll() {
        clearTimeout(timer);
        if (finished) return;
        try {
            const response = await fetch(statusUrl, { cache: "no-store" });
            show(await response.json());
        } catch (error) {
            // The server is briefly unreachable; the next poll tries again.
        }
        if (!finished) timer = window.setTimeout(poll, checking ? 600 : POLL_MS);
    }

    continueButton.addEventListener("click", async () => {
        if (finished || checking) return;
        checking = true;
        continueButton.disabled = true;
        continueButton.textContent = "Checking…";
        statusText.textContent = "Checking your login…";
        notice.hidden = true;
        try {
            await fetch(confirmUrl, { method: "POST" });
        } catch (error) {
            checking = false;
            continueButton.disabled = false;
            continueButton.textContent = "Continue";
            notice.textContent = "Could not reach ReelSearch. Try again.";
            notice.hidden = false;
            return;
        }
        poll();
    });

    // Cancelling closes the browser and discards the login. The request is
    // sent even as the page moves on.
    async function leave(url) {
        if (!finished) {
            finished = true;
            clearTimeout(timer);
            try {
                await Promise.race([
                    fetch(cancelUrl, { method: "POST", keepalive: true }),
                    new Promise((resolve) => window.setTimeout(resolve, 800))
                ]);
            } catch (error) {
                // Nothing more to do; the server drops an unattended login.
            }
        }
        window.location.assign(url);
    }

    cancelButton.addEventListener("click", () => leave(leaveUrl));
    document.querySelectorAll(".back-link").forEach((link) => {
        link.addEventListener("click", (event) => {
            if (finished) return;
            event.preventDefault();
            leave(link.href);
        });
    });

    poll();
})();
