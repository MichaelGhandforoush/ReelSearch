(function () {
    const searchForm = document.querySelector("#search-form");
    const searchInput = document.querySelector("#search-input");
    const searchResults = document.querySelector("#search-results");
    const loadMoreButton = document.querySelector("#load-more");
    const loadingIndicator = document.querySelector("#loading-indicator");
    const allVideos = document.querySelector("#all-videos");
    const progress = document.querySelector("#video-progress");
    const libraryTotal = document.querySelector("#library-total");
    const syncControls = [...document.querySelectorAll(".sync-control")];
    const syncTimers = new Map();

    function videosPerRow() {
        if (!allVideos) return 1;
        const styles = window.getComputedStyle(allVideos);
        return Math.max(
            1,
            styles.gridTemplateColumns.split(" ").filter(Boolean).length
        );
    }

    const viewer = document.querySelector("#video-viewer");
    const viewerScroll = document.querySelector("#video-viewer-scroll");
    const viewerClose = document.querySelector("#video-viewer-close");
    const viewerState = {
        source: null,
        index: -1,
        loading: false,
        observer: null,
        observed: new Set(),
        rendered: new Set()
    };

    if (searchInput) {
        searchInput.addEventListener("focus", () => searchInput.select());
    }

    function processInstagramEmbeds(root) {
        if (window.instgrm && window.instgrm.Embeds) {
            window.instgrm.Embeds.process(root || document);
        }
    }

    function sourceContainer(source) {
        return source === "all" ? allVideos : searchResults;
    }

    function sourceCards(source) {
        const container = sourceContainer(source);
        return container
            ? [...container.querySelectorAll(
                `.video-card[data-video-source="${source}"]`
            )]
            : [];
    }

    function createViewerCard(sourceCard, index, autoplay) {
        const card = sourceCard.cloneNode(true);
        const trigger = card.querySelector(".video-open-trigger");
        if (trigger) trigger.remove();

        const iframe = card.querySelector("iframe");
        if (iframe) {
            iframe.setAttribute("loading", "eager");
            if (autoplay) {
                iframe.src = iframe.src.replace("autoplay=0", "autoplay=1");
            }
        }

        card.dataset.viewerIndex = String(index);
        card.classList.add("video-viewer-card");
        return card;
    }

    function appendViewerCard(index) {
        if (!viewerState.source || viewerState.rendered.has(index)) return;
        const card = sourceCards(viewerState.source)[index];
        if (!card) return;
        const viewerCard = createViewerCard(
            card,
            index,
            index === viewerState.index
        );
        viewerScroll.appendChild(viewerCard);
        viewerState.rendered.add(index);
        processInstagramEmbeds(viewerCard);
    }

    function activateViewerCard(index) {
        const card = viewerScroll.querySelector(
            `[data-viewer-index="${index}"]`
        );
        const iframe = card && card.querySelector("iframe");
        if (iframe && iframe.src.includes("autoplay=0")) {
            iframe.src = iframe.src.replace("autoplay=0", "autoplay=1");
        }
    }

    function observeViewerSlides() {
        if (viewerState.observer) return;
        viewerState.observer = new IntersectionObserver((entries) => {
            const visible = entries
                .filter((entry) => entry.isIntersecting)
                .sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
            if (!visible) return;

            const index = Number(visible.target.dataset.viewerIndex);
            if (Number.isInteger(index) && index !== viewerState.index) {
                viewerState.index = index;
                activateViewerCard(index);
                appendViewerSlide(index + 1);
                appendViewerSlide(index + 2);
                preloadViewerAhead();
            }
        }, {
            root: viewerScroll,
            threshold: [0.7, 0.9]
        });
    }

    function appendViewerSlide(index) {
        appendViewerCard(index);
        const card = viewerScroll.querySelector(
            `[data-viewer-index="${index}"]`
        );
        if (card && !viewerState.observed.has(index)) {
            observeViewerSlides();
            viewerState.observer.observe(card);
            viewerState.observed.add(index);
        }
    }

    function ensureViewerWindow() {
        if (!viewerState.source) return;
        appendViewerSlide(viewerState.index);
        appendViewerSlide(viewerState.index + 1);
        appendViewerSlide(viewerState.index + 2);
    }

    async function preloadViewerAhead() {
        ensureViewerWindow();
        const available = sourceCards(viewerState.source).length;
        if (viewerState.index < available - 2) return;
        await loadViewerMore();
    }

    async function loadViewerMore() {
        if (viewerState.loading || !viewerState.source) return;
        viewerState.loading = true;
        try {
            if (
                viewerState.source === "all" &&
                loadMoreButton &&
                !loadMoreButton.hidden
            ) {
                await loadMoreVideos();
            } else if (viewerState.source === "search") {
                const moreButton = searchResults.querySelector(
                    ".load-more-search"
                );
                if (moreButton) await loadMoreSearchResults(moreButton);
            }
            ensureViewerWindow();
        } finally {
            viewerState.loading = false;
        }
    }

    function openViewer(card) {
        if (!viewer || !viewerScroll) return;
        viewerState.source = card.dataset.videoSource;
        viewerState.index = sourceCards(viewerState.source).indexOf(card);
        viewerState.rendered.clear();
        viewerState.observed.clear();
        if (viewerState.observer) viewerState.observer.disconnect();
        viewerState.observer = null;
        viewerScroll.innerHTML = "";
        viewer.hidden = false;
        viewer.setAttribute("aria-hidden", "false");
        document.body.classList.add("viewer-open");
        ensureViewerWindow();
        const active = viewerScroll.querySelector(
            `[data-viewer-index="${viewerState.index}"]`
        );
        if (active) active.scrollIntoView({ block: "start", behavior: "auto" });
        preloadViewerAhead();
    }

    function closeViewer() {
        if (!viewer) return;
        viewer.hidden = true;
        viewer.setAttribute("aria-hidden", "true");
        document.body.classList.remove("viewer-open");
        if (viewerState.observer) viewerState.observer.disconnect();
        viewerState.observer = null;
        viewerState.observed.clear();
        viewerScroll.innerHTML = "";
        viewerState.rendered.clear();
        viewerState.source = null;
        viewerState.index = -1;
        viewerState.loading = false;
    }

    document.addEventListener("click", (event) => {
        const trigger = event.target.closest(".video-open-trigger");
        if (trigger) {
            openViewer(trigger.closest(".video-card"));
            return;
        }
        const card = event.target.closest(".video-card");
        if (
            card &&
            !viewer?.contains(card) &&
            !event.target.closest("iframe") &&
            !event.target.closest("button")
        ) {
            openViewer(card);
        }
    });

    if (viewerClose) viewerClose.addEventListener("click", closeViewer);
    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape" && viewer && !viewer.hidden) {
            closeViewer();
        }
    });

    if (searchForm) {
        let searchTimer = null;
        let searchRequest = null;

        async function submitSearch() {
            const query = searchInput.value.trim();
            if (!query) {
                searchResults.innerHTML = "";
                return;
            }
            if (searchRequest) searchRequest.abort();
            searchRequest = new AbortController();
            try {
                const response = await fetch(searchForm.action, {
                    method: "POST",
                    body: new FormData(searchForm),
                    headers: { "X-Requested-With": "XMLHttpRequest" },
                    signal: searchRequest.signal
                });
                if (!response.ok) throw new Error("Search request failed.");
                closeViewer();
                searchResults.innerHTML = await response.text();
                processInstagramEmbeds(searchResults);
                history.replaceState(
                    null,
                    "",
                    `${window.location.pathname}?q=${encodeURIComponent(
                        searchInput.value
                    )}`
                );
            } catch (error) {
                if (error.name !== "AbortError") {
                    searchResults.innerHTML =
                        '<div class="inline-error">Search failed. Please try again.</div>';
                }
            }
        }

        searchForm.addEventListener("submit", (event) => {
            event.preventDefault();
            clearTimeout(searchTimer);
            submitSearch();
        });

        searchInput.addEventListener("input", () => {
            clearTimeout(searchTimer);
            searchTimer = window.setTimeout(submitSearch, 350);
        });
    }

    async function loadMoreSearchResults(button) {
        const resultsGrid = searchResults.querySelector(".video-grid");
        const loading = button.parentElement.querySelector(".loading-indicator");
        if (!resultsGrid || button.disabled) return;

        button.disabled = true;
        loading.hidden = false;
        const formData = new FormData();
        formData.append("query", button.dataset.query);
        formData.append("offset", button.dataset.offset);

        try {
            const response = await fetch("/search", {
                method: "POST",
                body: formData,
                headers: { "X-Requested-With": "XMLHttpRequest" }
            });
            if (!response.ok) throw new Error("Could not load more results.");
            resultsGrid.insertAdjacentHTML("beforeend", await response.text());

            const loadedCount = resultsGrid.querySelectorAll(".video-card").length;
            button.dataset.offset = response.headers.get(
                "X-Search-Next-Offset"
            ) || loadedCount;
            if (response.headers.get("X-Search-Has-More") !== "true") {
                button.parentElement.remove();
            } else {
                button.disabled = false;
            }
            const count = searchResults.querySelector(".section-note");
            if (count) count.textContent = `${loadedCount} shown`;
            processInstagramEmbeds(resultsGrid);
        } catch (error) {
            button.disabled = false;
            loading.textContent = "Could not load more results. Try again.";
        } finally {
            loading.hidden = true;
        }
    }

    if (searchResults) {
        searchResults.addEventListener("click", (event) => {
            const button = event.target.closest(".load-more-search");
            if (button) loadMoreSearchResults(button);
        });
    }

    function setLibraryTotal(total) {
        if (!Number.isFinite(total)) return;
        if (libraryTotal) libraryTotal.textContent = total;
        if (!loadMoreButton) return;
        loadMoreButton.dataset.total = total;
        const offset = Number(loadMoreButton.dataset.offset);
        if (progress) progress.textContent = `${Math.min(offset, total)} of ${total}`;
        if (offset < total && !loadMoreButton.disabled) {
            loadMoreButton.hidden = false;
        }
    }

    async function refreshLibraryTotal() {
        try {
            const response = await fetch("/videos/count", {
                headers: { "X-Requested-With": "XMLHttpRequest" }
            });
            if (!response.ok) return;
            setLibraryTotal((await response.json()).total_videos);
        } catch (error) {
            // Keep the last known total; the next refresh will retry.
        }
    }

    async function loadMoreVideos() {
        if (!loadMoreButton || loadMoreButton.disabled) return;
        const offset = Number(loadMoreButton.dataset.offset);
        const total = Number(loadMoreButton.dataset.total);
        if (offset >= total) return;

        loadMoreButton.disabled = true;
        loadMoreButton.hidden = true;
        loadingIndicator.hidden = false;
        try {
            const rowSize = videosPerRow();
            const response = await fetch(
                `/videos?offset=${offset}&limit=${rowSize}`,
                {
                headers: { "X-Requested-With": "XMLHttpRequest" }
                }
            );
            if (!response.ok) throw new Error("Could not load videos.");
            allVideos.insertAdjacentHTML("beforeend", await response.text());
            const loadedCount = allVideos.querySelectorAll(".video-card").length;
            // The total may have changed while this request was in flight.
            const latestTotal = Number(loadMoreButton.dataset.total);
            const nextOffset = Math.min(loadedCount, latestTotal);
            loadMoreButton.dataset.offset = nextOffset;
            progress.textContent = `${nextOffset} of ${latestTotal}`;
            // Stay enabled even when everything is shown, so videos added by
            // a later sync can still be loaded.
            loadMoreButton.disabled = false;
            loadMoreButton.hidden = nextOffset >= latestTotal;
            processInstagramEmbeds(allVideos);
        } catch (error) {
            loadMoreButton.hidden = false;
            loadMoreButton.disabled = false;
            loadingIndicator.textContent = "Could not load more. Try again.";
        } finally {
            loadingIndicator.hidden = true;
        }
    }

    function updateSyncControl(control, data) {
        const platform = control.dataset.platform;
        const label = control.querySelector(".sync-label");
        const stateElement = document.querySelector(
            `[data-sync-state="${platform}"]`
        );
        const progressText = `${data.loaded} loaded · ${data.remaining} left`;
        const isActive = [
            "collecting_urls",
            "loading_videos",
            "running",
            "pause_requested"
        ].includes(data.status);
        label.textContent = isActive
            ? (data.status === "pause_requested" ? "Pausing…" : "Pause")
            : "Sync";
        control.disabled = data.status === "pause_requested";
        if (stateElement) {
            if (data.status === "collecting_urls") {
                stateElement.textContent =
                    `Grabbing URLs · ${progressText}`;
            } else if (
                data.status === "loading_videos" ||
                data.status === "running"
            ) {
                stateElement.textContent =
                    `Loading videos · ${progressText}`;
            } else if (data.status === "pause_requested") {
                stateElement.textContent = `Pausing · ${progressText}`;
            } else if (data.status === "paused") {
                stateElement.textContent = `Paused · ${progressText}`;
            } else if (data.status === "completed") {
                stateElement.textContent =
                    data.remaining === 0
                        ? "Up to date"
                        : `${data.remaining} left to load`;
            } else if (data.status === "error") {
                stateElement.textContent =
                    data.last_error || `${data.remaining} left to load`;
            } else {
                stateElement.textContent =
                    data.source_total > 0 ? `${progressText}` : "";
            }
        }
        setLibraryTotal(data.total_videos);
        if (
            [
                "collecting_urls",
                "loading_videos",
                "running",
                "pause_requested"
            ].includes(data.status)
        ) {
            if (!syncTimers.has(platform)) {
                syncTimers.set(platform, window.setTimeout(
                    () => pollSync(control),
                    1200
                ));
            }
        } else {
            syncTimers.delete(platform);
        }
    }

    async function pollSync(control) {
        // Cancel any pending poll so repeated calls never start a second loop.
        window.clearTimeout(syncTimers.get(control.dataset.platform));
        syncTimers.delete(control.dataset.platform);
        try {
            const response = await fetch(control.dataset.statusUrl, {
                headers: { "X-Requested-With": "XMLHttpRequest" }
            });
            if (!response.ok) throw new Error("Could not read sync status.");
            const data = await response.json();
            updateSyncControl(control, data);

            if (
                ["collecting_urls", "loading_videos", "running"].includes(
                    data.status
                ) &&
                data.total_videos > allVideos.querySelectorAll(".video-card").length &&
                loadMoreButton &&
                !loadMoreButton.hidden &&
                loadMoreButton.getBoundingClientRect().top <
                    window.innerHeight + 240
            ) {
                await loadMoreVideos();
            }
        } catch (error) {
            const stateElement = document.querySelector(
                `[data-sync-state="${control.dataset.platform}"]`
            );
            if (stateElement) stateElement.textContent = "Status unavailable";
        }
    }

    syncControls.forEach((control) => {
        control.addEventListener("click", async () => {
            control.disabled = true;
            try {
                const response = await fetch(control.dataset.actionUrl, {
                    method: "POST",
                    headers: { "X-Requested-With": "XMLHttpRequest" }
                });
                if (!response.ok) throw new Error("Could not update sync.");
                updateSyncControl(control, await response.json());
                pollSync(control);
            } catch (error) {
                control.disabled = false;
                const stateElement = document.querySelector(
                    `[data-sync-state="${control.dataset.platform}"]`
                );
                if (stateElement) stateElement.textContent = "Could not update sync";
            }
        });
        pollSync(control);
    });

    // Syncs can be started or accounts removed from other pages or tabs, so
    // refresh whenever the page is shown again and periodically while visible.
    function refreshAll() {
        refreshLibraryTotal();
        syncControls.forEach((control) => {
            if (!syncTimers.has(control.dataset.platform)) pollSync(control);
        });
    }
    window.addEventListener("pageshow", (event) => {
        if (event.persisted) refreshAll();
    });
    document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "visible") refreshAll();
    });
    window.setInterval(() => {
        if (document.visibilityState === "visible") refreshAll();
    }, 10000);

    if (loadMoreButton) {
        loadMoreButton.addEventListener("click", loadMoreVideos);
        const observer = new IntersectionObserver((entries) => {
            if (entries[0].isIntersecting) loadMoreVideos();
        }, { rootMargin: "240px" });
        observer.observe(loadMoreButton);
    }
})();
