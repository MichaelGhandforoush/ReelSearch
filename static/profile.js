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

    const MIN_VIDEOS_PER_PAGE = 3;

    function gridColumns(grid) {
        const columns = window.getComputedStyle(grid).gridTemplateColumns
            .split(" ").filter(Boolean).length;
        return Math.max(1, columns);
    }

    // The fewest whole rows that show at least MIN_VIDEOS_PER_PAGE videos:
    // 3 columns -> 3, 2 columns -> 4 (two rows), 1 column -> 3 (three rows).
    function searchPageSize() {
        const grid = document.createElement("div");
        grid.className = "video-grid";
        grid.style.cssText = "position:absolute;visibility:hidden;width:100%";
        (searchResults || document.body).appendChild(grid);
        const columns = gridColumns(grid);
        grid.remove();
        return columns * Math.ceil(MIN_VIDEOS_PER_PAGE / columns);
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
        if (!container) return [];
        return [...container.querySelectorAll(
            `.video-card[data-video-source="${source}"]`
        )]
            .map((card, index) => ({
                card,
                index,
                order: Number(card.style.order) || 0
            }))
            .sort((a, b) => a.order - b.order || a.index - b.index)
            .map((item) => item.card);
    }

    function createViewerCard(sourceCard, index, autoplay) {
        const card = sourceCard.cloneNode(true);
        const trigger = card.querySelector(".video-open-trigger");
        if (trigger) trigger.remove();
        // The copy loads its own frame, so it waits for that one.
        const frame = card.querySelector(".video-frame");
        if (frame) frame.classList.remove("is-ready");

        const iframe = card.querySelector("iframe");
        if (iframe) {
            iframe.setAttribute("loading", "eager");
            iframe.setAttribute("muted", "");
            if (autoplay) {
                iframe.src = iframe.src.replace("autoplay=0", "autoplay=1");
                iframe.src = iframe.src.replace("muted=0", "muted=1");
                if (!iframe.src.includes("muted=")) {
                    iframe.src += (iframe.src.includes("?") ? "&" : "?") + "muted=1";
                }
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
            index === viewerState.index && autoplayEnabled()
        );
        const iframe = viewerCard.querySelector("iframe");
        // A video that is still loading when it becomes current gets its
        // play command once it can receive it.
        if (iframe) {
            iframe.addEventListener("load", () => applyPlayback(viewerCard));
        }
        viewerScroll.appendChild(viewerCard);
        // Load details for upcoming videos so switching to them is instant.
        fetchVideoInfo(viewerCard.dataset.videoUrl);
        viewerState.rendered.add(index);
        processInstagramEmbeds(viewerCard);
    }

    // ---- Playback: the video on screen plays, the rest are paused ----

    function autoplayEnabled() {
        return window.ReelSearchSettings
            ? window.ReelSearchSettings.get("viewerAutoplay")
            : true;
    }

    function sendPlayback(card, command) {
        const iframe = card.querySelector("iframe");
        if (!iframe || !iframe.contentWindow) return;
        if (iframe.classList.contains("instagram-embed")) {
            // Our own /embed/instagram page, served from this origin.
            iframe.contentWindow.postMessage(
                { "x-reelsearch": true, type: command },
                window.location.origin
            );
        } else if (iframe.src.startsWith("https://www.tiktok.com/player/")) {
            iframe.contentWindow.postMessage(
                { "x-tiktok-player": true, type: command, value: null },
                "https://www.tiktok.com"
            );
        }
    }

    function applyPlayback(card) {
        const current = Number(card.dataset.viewerIndex) === viewerState.index;
        if (!current) {
            sendPlayback(card, "pause");
        } else if (autoplayEnabled()) {
            sendPlayback(card, "play");
        }
    }

    function activateViewerCard() {
        viewerScroll.querySelectorAll(".video-viewer-card").forEach(applyPlayback);
    }

    // ---- Hover preview: a muted clip plays while the cursor is over a card ----

    const HOVER_PREVIEW_DELAY_MS = 150;
    // Touch screens have no hover; a tap there opens the viewer instead.
    const canHover = window.matchMedia("(hover: hover)");
    const tiktokReady = new WeakSet();
    let hoveredCard = null;
    let previewingCard = null;
    let previewTimer = null;

    // Sends the card whatever it should be doing now: playing muted while it
    // is the one being previewed, otherwise stopped and back at the start.
    function applyPreview(card) {
        const iframe = card.querySelector("iframe");
        const target = iframe && iframe.contentWindow;
        if (!target) return;
        const playing = previewingCard === card;
        if (iframe.classList.contains("instagram-embed")) {
            const loaded = iframe.contentDocument &&
                iframe.contentDocument.readyState === "complete";
            if (!loaded) {
                // Its page is not listening yet; settle once it has loaded.
                iframe.addEventListener("load", () => applyPreview(card), { once: true });
                return;
            }
            target.postMessage(
                { "x-reelsearch": true, type: playing ? "preview" : "stop" },
                window.location.origin
            );
        } else if (iframe.src.startsWith("https://www.tiktok.com/player/")) {
            // TikTok's player ignores commands until it says it is ready; the
            // ready message below brings a waiting card back here.
            if (!tiktokReady.has(target)) return;
            const send = (type, value) => target.postMessage(
                { "x-tiktok-player": true, type, value: value ?? null },
                "https://www.tiktok.com"
            );
            if (playing) {
                send("mute");
                send("play");
            } else {
                send("pause");
                send("seekTo", 0);
            }
        }
    }

    function startPreview(card) {
        previewingCard = card;
        applyPreview(card);
    }

    function stopPreview() {
        const card = previewingCard;
        previewingCard = null;
        if (card) applyPreview(card);
    }

    function setHoveredCard(card) {
        if (card === hoveredCard) return;
        clearTimeout(previewTimer);
        hoveredCard = card;
        stopPreview();
        // A short delay, so sweeping across the grid doesn't start every clip.
        if (card) {
            previewTimer = window.setTimeout(
                () => startPreview(card),
                HOVER_PREVIEW_DELAY_MS
            );
        }
    }

    document.addEventListener("mouseover", (event) => {
        if (!canHover.matches || (viewer && !viewer.hidden)) return;
        const card = event.target.closest && event.target.closest(".video-card");
        setHoveredCard(card && !(viewer && viewer.contains(card)) ? card : null);
    });
    // Leaving the page (or its tab) counts as leaving the video.
    document.documentElement.addEventListener("mouseleave", () => setHoveredCard(null));
    window.addEventListener("blur", () => setHoveredCard(null));
    document.addEventListener("visibilitychange", () => {
        if (document.hidden) setHoveredCard(null);
    });

    // TikTok's player ignores commands until it says it is ready.
    window.addEventListener("message", (event) => {
        if (event.origin !== "https://www.tiktok.com") return;
        let data = event.data;
        if (typeof data === "string") {
            try {
                data = JSON.parse(data);
            } catch (error) {
                return;
            }
        }
        if (!data || data["x-tiktok-player"] !== true || data.type !== "onPlayerReady") {
            return;
        }
        tiktokReady.add(event.source);
        if (previewingCard && cardWindowIs(previewingCard, event.source)) {
            applyPreview(previewingCard);
        }
        if (!viewerScroll) return;
        const card = [...viewerScroll.querySelectorAll(".video-viewer-card")].find(
            (item) => item.querySelector("iframe")?.contentWindow === event.source
        );
        if (card) applyPlayback(card);
    });

    // ---- Placeholders: a card shows one until its video is ready ----

    function markFrameReady(iframe) {
        const frame = iframe && iframe.closest(".video-frame");
        if (frame) frame.classList.add("is-ready");
    }

    // Our Instagram player says when its thumbnail is in; every other frame
    // (TikTok, Instagram's own page) counts as ready once it has loaded.
    function checkFrame(iframe) {
        let doc = null;
        try {
            doc = iframe.contentDocument;
        } catch (error) {
            doc = null;
        }
        if (!doc || doc.readyState !== "complete" || doc.URL === "about:blank") return;
        const root = doc.documentElement;
        if (root.dataset.player !== "1" || root.classList.contains("ready")) {
            markFrameReady(iframe);
        }
    }

    window.addEventListener("message", (event) => {
        const data = event.data;
        if (
            event.origin !== window.location.origin || !data ||
            data["x-reelsearch"] !== true || data.type !== "ready"
        ) return;
        markFrameReady([...document.querySelectorAll("iframe.instagram-embed")].find(
            (iframe) => iframe.contentWindow === event.source
        ));
    });
    // Load events don't bubble, so they are caught on the way down.
    document.addEventListener("load", (event) => {
        if (event.target instanceof HTMLIFrameElement) checkFrame(event.target);
    }, true);
    document.querySelectorAll(".video-frame iframe").forEach(checkFrame);

    function cardWindowIs(card, source) {
        const iframe = card.querySelector("iframe");
        return Boolean(iframe) && iframe.contentWindow === source;
    }

    window.addEventListener("reelsearch:settingschange", (event) => {
        if (event.detail.name !== "viewerAutoplay" || !viewer || viewer.hidden) return;
        const card = viewerScroll.querySelector(
            `[data-viewer-index="${viewerState.index}"]`
        );
        if (card) sendPlayback(card, event.detail.value ? "play" : "pause");
    });

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
                showViewerInfo(index);
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
                // Weaker matches stay hidden until asked for.
                const moreButton = searchResults.querySelector(
                    ".load-more-search:not([data-weak])"
                );
                if (moreButton) await loadMoreSearchResults(moreButton);
            }
            ensureViewerWindow();
        } finally {
            viewerState.loading = false;
        }
    }

    // ---- Details sidebar for the video on screen ----

    const viewerInfo = document.querySelector("#viewer-info");
    const viewerInfoToggle = document.querySelector("#viewer-info-toggle");
    const VIEWER_INFO_HIDDEN_KEY = "reelsearch.viewerInfoHidden";
    const videoInfoCache = new Map();
    let viewerInfoUrl = null;

    function setViewerInfoVisible(visible, remember) {
        if (!viewerInfo || !viewerInfoToggle) return;
        viewer.classList.toggle("info-collapsed", !visible);
        viewerInfoToggle.setAttribute("aria-expanded", String(visible));
        viewerInfoToggle.querySelector(".viewer-info-toggle-label").textContent =
            visible ? "Hide details" : "Show details";
        if (!remember) return;
        try {
            window.localStorage.setItem(VIEWER_INFO_HIDDEN_KEY, visible ? "0" : "1");
        } catch (error) {
            // The choice just won't be remembered next time.
        }
    }

    if (viewerInfoToggle) {
        let startHidden = false;
        try {
            startHidden =
                window.localStorage.getItem(VIEWER_INFO_HIDDEN_KEY) === "1";
        } catch (error) {
            startHidden = false;
        }
        setViewerInfoVisible(!startHidden, false);
        viewerInfoToggle.addEventListener("click", () => {
            setViewerInfoVisible(viewer.classList.contains("info-collapsed"), true);
        });
    }

    function platformFromUrl(url) {
        if (url.includes("tiktok.com")) return "tiktok";
        if (url.includes("instagram.com")) return "instagram";
        return "";
    }

    function formatPosted(seconds) {
        const date = new Date(seconds * 1000);
        const days = Math.round((date - Date.now()) / 86400000);
        const relative = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
        const ago = Math.abs(days) < 30
            ? relative.format(days, "day")
            : Math.abs(days) < 365
                ? relative.format(Math.round(days / 30), "month")
                : relative.format(Math.round(days / 365), "year");
        const formatted = date.toLocaleDateString(undefined, {
            day: "numeric",
            month: "short",
            year: "numeric"
        });
        return `${formatted} · ${ago}`;
    }

    function setInfoField(name, text) {
        const row = viewerInfo.querySelector(`[data-info="${name}"]`);
        if (!row) return;
        row.hidden = !text;
        row.querySelector("dd, p").textContent = text || "";
    }

    function renderViewerInfo(info) {
        const platform = info.platform || platformFromUrl(info.url);
        const labels = { instagram: "Instagram", tiktok: "TikTok" };
        const link = viewerInfo.querySelector("#viewer-info-link");
        let safeUrl = null;
        try {
            const parsed = new URL(info.url);
            if (["http:", "https:"].includes(parsed.protocol)) safeUrl = parsed.href;
        } catch (error) {
            safeUrl = null;
        }
        link.hidden = !safeUrl;
        if (safeUrl) link.href = safeUrl;
        const label = info.platform_label || labels[platform] || "the platform";
        viewerInfo.querySelector("#viewer-info-platform").textContent = label;
        link.setAttribute("aria-label", `Open this video on ${label} in a new tab`);
        const icon = viewerInfo.querySelector("#viewer-info-icon");
        icon.className = `platform-icon platform-${platform}`;
        icon.textContent = info.abbreviation ||
            { instagram: "IG", tiktok: "TT" }[platform] || "";

        const likes = Number(info.likes);
        setInfoField("uploader", info.uploader);
        setInfoField(
            "posted",
            Number.isFinite(Number(info.uploaded_at)) && info.uploaded_at !== null
                ? formatPosted(Number(info.uploaded_at))
                : ""
        );
        setInfoField(
            "likes",
            info.likes !== null && info.likes !== undefined && Number.isFinite(likes)
                ? likes.toLocaleString()
                : ""
        );
        setInfoField("saved_by", info.saved_by ? `@${info.saved_by}` : "");
        setInfoField("caption", info.caption);
    }

    // Requests are shared, so prefetching and showing never ask twice.
    function fetchVideoInfo(url) {
        if (!videoInfoCache.has(url)) {
            const request = fetch(`/videos/info?url=${encodeURIComponent(url)}`)
                .then((response) => (response.ok ? response.json() : null))
                .catch(() => null)
                .then((info) => {
                    if (!info) videoInfoCache.delete(url);
                    return info;
                });
            videoInfoCache.set(url, request);
        }
        return videoInfoCache.get(url);
    }

    let viewerInfoMinHeight = 0;
    let viewerInfoBusyTimer = null;

    // The panel may grow to fit a longer caption but never shrinks while
    // the viewer is open, so scrolling between videos doesn't make it jump.
    function holdViewerInfoHeight() {
        viewerInfo.style.minHeight = "";
        viewerInfoMinHeight = Math.max(viewerInfoMinHeight, viewerInfo.offsetHeight);
        viewerInfo.style.minHeight = `${viewerInfoMinHeight}px`;
    }

    function resetViewerInfoHeight() {
        viewerInfoMinHeight = 0;
        if (viewerInfo) viewerInfo.style.minHeight = "";
    }

    async function showViewerInfo(index) {
        if (!viewerInfo) return;
        const card = viewerScroll.querySelector(`[data-viewer-index="${index}"]`);
        if (!card) return;
        const url = card.dataset.videoUrl;
        viewerInfoUrl = url;
        // Keep the previous video's details until these are complete, and
        // only dim them if loading is slow enough to notice.
        clearTimeout(viewerInfoBusyTimer);
        viewerInfoBusyTimer = window.setTimeout(() => {
            if (viewerInfoUrl === url) viewerInfo.classList.add("is-loading");
        }, 150);
        const info = await fetchVideoInfo(url);
        if (viewerInfoUrl !== url) return;
        clearTimeout(viewerInfoBusyTimer);
        viewerInfo.classList.remove("is-loading");
        renderViewerInfo(info || {
            url,
            likes: card.dataset.likes ?? null,
            uploaded_at: card.dataset.uploadedAt ?? null
        });
        holdViewerInfoHeight();
    }

    // "More like this": results for the video on screen replace the search,
    // shown back on the page rather than in the viewer.
    const viewerSimilarButton = document.querySelector("#viewer-info-similar");
    let startSimilarSearch = null;

    if (viewerSimilarButton) {
        viewerSimilarButton.addEventListener("click", async () => {
            const url = viewerInfoUrl;
            if (!url || !startSimilarSearch) return;
            const info = await fetchVideoInfo(url);
            closeViewer();
            startSimilarSearch(url, (info && info.similar_label) || "this video");
        });
    }

    window.addEventListener("resize", () => {
        if (!viewer || viewer.hidden) return;
        resetViewerInfoHeight();
        if (viewerInfo) holdViewerInfoHeight();
    });

    function openViewer(card) {
        if (!viewer || !viewerScroll) return;
        setHoveredCard(null);
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
        showViewerInfo(viewerState.index);
        preloadViewerAhead();
        // Opening a result means the search found something worth keeping.
        if (viewerState.source === "search") recordRecentSearch();
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
        viewerInfoUrl = null;
        resetViewerInfoHeight();
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
    // Clicking the dark backdrop, anywhere but the video, the details card
    // or the viewer's buttons, leaves scroll mode.
    if (viewer) {
        viewer.addEventListener("click", (event) => {
            if (event.target.closest(
                ".video-frame, #viewer-info, #viewer-info-toggle, #video-viewer-close"
            )) return;
            closeViewer();
            // The clicked card is gone now, so the page's "click a card to
            // open it" handler would otherwise reopen the viewer.
            event.stopPropagation();
        });
    }
    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape" && viewer && !viewer.hidden) {
            closeViewer();
        }
        if (
            (event.key === "i" || event.key === "I") &&
            !event.ctrlKey && !event.metaKey && !event.altKey &&
            viewer && !viewer.hidden && viewerInfo &&
            !event.target.closest("input, textarea, [contenteditable]")
        ) {
            setViewerInfoVisible(viewer.classList.contains("info-collapsed"), true);
        }
    });

    // ---- Saved and recent searches (kept in this browser only) ----

    const SAVED_KEY = "reelsearch.savedSearches";
    const RECENT_KEY = "reelsearch.recentSearches";
    const MAX_RECENT = 6;
    const filterGroups = [
        ...document.querySelectorAll(".search-filters [data-filter]")
    ];

    function filterOptions(group) {
        return [...group.querySelectorAll('input[type="radio"]')];
    }

    function filterValue(group) {
        const checked = group.querySelector("input:checked");
        return checked && !checked.matches(":disabled") ? checked.value : "";
    }

    function setFilterValue(group, value) {
        const options = filterOptions(group);
        const match = options.find(
            (input) => input.value === value && !input.matches(":disabled")
        ) || options.find((input) => input.value === "");
        if (match) match.checked = true;
    }

    function filterLabel(group, value) {
        const input = filterOptions(group).find((item) => item.value === value);
        return input ? input.closest("label").textContent.trim() : "";
    }

    const SORT_DATA_KEYS = { newest: "uploadedAt", likes: "likes" };
    // Keep in sync with order_labels in profile.html.
    const ORDER_LABELS = {
        "": ["Default order", "Reversed order"],
        newest: ["Newest first", "Oldest first"],
        likes: ["Most liked first", "Least liked first"]
    };
    const reverseButton = document.querySelector("#reverse-sort");

    function isReversed() {
        return Boolean(reverseButton) &&
            reverseButton.getAttribute("aria-pressed") === "true";
    }

    function setReversed(reversed) {
        if (!reverseButton) return;
        reverseButton.setAttribute("aria-pressed", String(reversed));
        const labels = ORDER_LABELS[currentSort()] || ORDER_LABELS[""];
        reverseButton.querySelector(".reverse-label").textContent =
            labels[reversed ? 1 : 0];
    }

    function currentSort() {
        const group = filterGroups.find((item) => item.dataset.filter === "sort");
        return group ? filterValue(group) : "";
    }

    // Sorting a search only rearranges the results already shown; the
    // search itself always returns best matches first.
    function sortShownResults() {
        if (!searchResults) return;
        const key = SORT_DATA_KEYS[currentSort()];
        const reversed = isReversed();
        const valueOf = (card) => {
            const raw = key ? card.dataset[key] : undefined;
            return raw === undefined || raw === "" ? null : Number(raw);
        };
        [...searchResults.querySelectorAll(
            '.video-card[data-video-source="search"]'
        )]
            .map((card) => ({
                card,
                rank: Number(card.dataset.rank) || 0,
                value: valueOf(card),
                weak: card.classList.contains("weak-match")
            }))
            .sort((a, b) => {
                // Weaker matches stay after the strong ones.
                if (a.weak !== b.weak) return a.weak ? 1 : -1;
                // Videos without the value stay last in either direction.
                if (key && (a.value === null) !== (b.value === null)) {
                    return a.value === null ? 1 : -1;
                }
                if (key && a.value !== b.value) {
                    return reversed ? a.value - b.value : b.value - a.value;
                }
                return !key && reversed ? b.rank - a.rank : a.rank - b.rank;
            })
            .forEach((item, index) => {
                item.card.style.order = index;
            });
    }
    const clearFiltersButton = document.querySelector("#clear-filters");
    const saveSearchButton = document.querySelector("#save-search");
    const suggestionList = document.querySelector("#search-suggestions");
    const shortcuts = document.querySelector("#search-shortcuts");
    const savedGroup = document.querySelector("#saved-searches");
    const recentGroup = document.querySelector("#recent-searches");
    const clearRecentButton = document.querySelector("#clear-recent");
    const shortcutsEmpty = document.querySelector("#shortcuts-empty");
    const advancedToggle = document.querySelector("#advanced-toggle");
    const advancedPanel = document.querySelector("#advanced-search");
    const advancedCount = document.querySelector("#advanced-count");
    const advancedTabs = [
        ...document.querySelectorAll('#advanced-search [role="tab"]')
    ];
    const platformBoxes = [
        ...document.querySelectorAll("[data-source-platform]")
    ];
    const accountBoxes = [
        ...document.querySelectorAll("[data-source-account]")
    ];
    const sourceWarning = document.querySelector("#source-warning");
    const similarChip = document.querySelector("#similar-chip");
    const similarChipLabel = document.querySelector("#similar-chip-label");
    const similarClear = document.querySelector("#similar-clear");
    // The video a "More like this" search started from, if one is shown.
    let similarVideo = similarChip && similarChip.dataset.url
        ? { url: similarChip.dataset.url, label: similarChipLabel.textContent }
        : null;

    function similarText(label) {
        return `More like ${label || "a video"}`;
    }

    function setSimilarVideo(video) {
        similarVideo = video;
        if (!similarChip) return;
        similarChip.hidden = !video;
        similarChip.dataset.url = video ? video.url : "";
        similarChipLabel.textContent = video ? video.label : "";
        const wrap = similarChip.closest(".search-input-wrap");
        wrap.classList.toggle("has-similar", Boolean(video));
        searchInput.placeholder = video
            ? "Or type to search instead"
            : searchInput.dataset.placeholder;
        fitSimilarChip();
    }

    // The words typed start after the chip.
    function fitSimilarChip() {
        if (!similarChip || !searchInput) return;
        searchInput.style.paddingLeft = similarVideo
            ? `${similarChip.offsetWidth + 16}px`
            : "";
    }

    fitSimilarChip();
    window.addEventListener("resize", fitSimilarChip);

    // ---- Accounts & platforms ----

    function platformAccounts(platform) {
        return accountBoxes.filter((box) => box.dataset.platform === platform);
    }

    function syncPlatformBox(platformBox) {
        const accounts = platformAccounts(platformBox.dataset.sourcePlatform);
        if (!accounts.length) {
            platformBox.indeterminate = false;
            return;
        }
        const checked = accounts.filter((box) => box.checked).length;
        platformBox.checked = checked === accounts.length;
        platformBox.indeterminate = checked > 0 && checked < accounts.length;
    }

    function anySourceSelected() {
        return platformBoxes.some(
            (box) => box.checked || box.indeterminate
        );
    }

    function allSourcesSelected() {
        return platformBoxes.every((box) => box.checked);
    }

    function setAllSources(checked) {
        [...platformBoxes, ...accountBoxes].forEach((box) => {
            box.checked = checked;
            box.indeterminate = false;
        });
    }

    function appendSourceParams(params) {
        if (allSourcesSelected()) return;
        // A fully selected platform also covers its videos that predate
        // per-account tracking.
        platformBoxes.forEach((platformBox) => {
            if (platformBox.checked) {
                params.append("platform", platformBox.dataset.sourcePlatform);
                return;
            }
            platformAccounts(platformBox.dataset.sourcePlatform).forEach(
                (box) => {
                    if (box.checked) {
                        params.append("account", box.dataset.sourceAccount);
                    }
                }
            );
        });
    }

    function applySourceParams(params) {
        const platforms = params.getAll("platform");
        const accounts = params.getAll("account");
        if (!platforms.length && !accounts.length) {
            setAllSources(true);
            return;
        }
        platformBoxes.forEach((platformBox) => {
            const platform = platformBox.dataset.sourcePlatform;
            const wholePlatform = platforms.includes(platform);
            platformBox.checked = wholePlatform;
            platformAccounts(platform).forEach((box) => {
                box.checked = wholePlatform ||
                    accounts.includes(box.dataset.sourceAccount);
            });
            syncPlatformBox(platformBox);
        });
        if (!anySourceSelected()) setAllSources(true);
    }

    function sourceLabels(params) {
        const platforms = params.getAll("platform");
        const accounts = params.getAll("account");
        return [
            ...platformBoxes
                .filter((box) => platforms.includes(box.dataset.sourcePlatform))
                .map((box) => box.dataset.label),
            ...accountBoxes
                .filter((box) => accounts.includes(box.dataset.sourceAccount))
                .map((box) => box.dataset.label)
        ];
    }

    platformBoxes.forEach(syncPlatformBox);

    // ---- Advanced panel and tabs ----

    function setAdvancedOpen(open) {
        if (!advancedPanel) return;
        advancedPanel.hidden = !open;
        advancedToggle.setAttribute("aria-expanded", String(open));
    }

    function selectTab(tab, focus) {
        advancedTabs.forEach((item) => {
            const selected = item === tab;
            item.setAttribute("aria-selected", String(selected));
            item.tabIndex = selected ? 0 : -1;
            document.getElementById(
                item.getAttribute("aria-controls")
            ).hidden = !selected;
        });
        if (focus) tab.focus();
    }

    if (advancedToggle) {
        advancedToggle.addEventListener("click", () => {
            setAdvancedOpen(advancedPanel.hidden);
        });
        advancedTabs.forEach((tab, index) => {
            tab.addEventListener("click", () => selectTab(tab, false));
            tab.addEventListener("keydown", (event) => {
                const step = { ArrowRight: 1, ArrowLeft: -1 }[event.key];
                if (!step) return;
                event.preventDefault();
                const next = advancedTabs[
                    (index + step + advancedTabs.length) % advancedTabs.length
                ];
                selectTab(next, true);
            });
        });
        advancedPanel.addEventListener("keydown", (event) => {
            if (event.key === "Escape") {
                event.stopPropagation();
                setAdvancedOpen(false);
                advancedToggle.focus();
            }
        });
    }

    function readSearchList(key) {
        try {
            const value = JSON.parse(window.localStorage.getItem(key));
            return Array.isArray(value)
                ? value.filter((item) => item && typeof item.params === "string")
                : [];
        } catch (error) {
            return [];
        }
    }

    function writeSearchList(key, list) {
        try {
            window.localStorage.setItem(key, JSON.stringify(list));
        } catch (error) {
            // Storage can be unavailable (private mode); shortcuts just
            // won't persist.
        }
    }

    function currentSearchParams() {
        const params = new URLSearchParams();
        const query = searchInput ? searchInput.value.trim() : "";
        if (similarVideo) {
            params.set("similar", similarVideo.url);
        } else if (query) {
            params.set("q", query);
        }
        appendSourceParams(params);
        filterGroups.forEach((group) => {
            const value = filterValue(group);
            if (value) params.set(group.dataset.filter, value);
        });
        if (isReversed()) params.set("reverse", "1");
        return params;
    }

    function activeAdvancedCount() {
        return (allSourcesSelected() ? 0 : 1) + (isReversed() ? 1 : 0) +
            filterGroups.filter((group) => filterValue(group)).length;
    }

    function describeSearch(params, similarLabel) {
        const parts = [];
        if (params.get("similar")) parts.push(similarText(similarLabel));
        if (params.get("q")) parts.push(params.get("q"));
        parts.push(...sourceLabels(params));
        filterGroups.forEach((group) => {
            const value = params.get(group.dataset.filter);
            const label = value && filterLabel(group, value);
            if (label) parts.push(label);
        });
        if (params.get("reverse") === "1") {
            parts.push((ORDER_LABELS[params.get("sort") || ""] || ORDER_LABELS[""])[1]);
        }
        return parts.join(" · ");
    }

    function toSearchForm(params, offset, weak) {
        const body = new URLSearchParams(params);
        body.set("query", body.get("q") || "");
        body.delete("q");
        if (offset !== undefined) body.set("offset", offset);
        if (weak) body.set("weak", "1");
        body.set("limit", searchPageSize());
        return body;
    }

    function renderChips(group, list, removable) {
        if (!group) return;
        const container = group.querySelector(".chip-list");
        container.replaceChildren();
        list.forEach((item) => {
            const label = describeSearch(
                new URLSearchParams(item.params), item.label
            );
            if (!label) return;
            const chip = document.createElement("span");
            chip.className = "chip";
            const apply = document.createElement("button");
            apply.type = "button";
            apply.className = "chip-apply";
            apply.dataset.params = item.params;
            if (item.label) apply.dataset.label = item.label;
            apply.textContent = label;
            chip.append(apply);
            if (removable) {
                const remove = document.createElement("button");
                remove.type = "button";
                remove.className = "chip-remove";
                remove.dataset.params = item.params;
                remove.setAttribute("aria-label", `Remove saved search ${label}`);
                remove.textContent = "×";
                chip.append(remove);
            }
            container.append(chip);
        });
        group.hidden = container.children.length === 0;
    }

    function renderSearchShortcuts() {
        if (!searchForm) return;
        const saved = readSearchList(SAVED_KEY);
        const savedParams = new Set(saved.map((item) => item.params));
        const recent = readSearchList(RECENT_KEY).filter(
            (item) => !savedParams.has(item.params)
        );
        renderChips(savedGroup, saved, true);
        renderChips(recentGroup, recent, false);
        if (shortcutsEmpty) {
            shortcutsEmpty.hidden = !(savedGroup.hidden && recentGroup.hidden);
        }

        if (suggestionList) {
            const queries = new Set();
            [...saved, ...recent].forEach((item) => {
                const query = new URLSearchParams(item.params).get("q");
                if (query) queries.add(query);
            });
            suggestionList.replaceChildren(...[...queries].map((query) => {
                const option = document.createElement("option");
                option.value = query;
                return option;
            }));
        }

        const params = currentSearchParams().toString();
        const isSaved = savedParams.has(params);
        if (saveSearchButton) {
            saveSearchButton.disabled = params === "";
            saveSearchButton.setAttribute("aria-pressed", String(isSaved));
            const label = isSaved ? "Remove saved search" : "Save this search";
            saveSearchButton.setAttribute("aria-label", label);
            saveSearchButton.title = label;
            saveSearchButton.firstElementChild.textContent = isSaved ? "★" : "☆";
        }
        const active = activeAdvancedCount();
        if (clearFiltersButton) clearFiltersButton.hidden = active === 0;
        if (advancedCount) {
            advancedCount.hidden = active === 0;
            advancedCount.textContent = active;
            advancedToggle.setAttribute(
                "aria-label",
                active ? `Advanced search, ${active} active` : "Advanced search"
            );
        }
    }

    // Stored searches keep the name of a "More like this" video, which the
    // params alone don't say.
    function searchItem(params) {
        return similarVideo
            ? { params, label: similarVideo.label }
            : { params };
    }

    function recordRecentSearch() {
        const params = currentSearchParams();
        const key = params.toString();
        if (!key) return;
        const query = params.get("q") || "";
        const rest = new URLSearchParams(params);
        rest.delete("q");
        // Drop the same search and shorter versions of the query that were
        // recorded while it was still being typed.
        const recent = readSearchList(RECENT_KEY).filter((item) => {
            const older = new URLSearchParams(item.params);
            const olderQuery = older.get("q") || "";
            older.delete("q");
            return item.params !== key && !(
                olderQuery &&
                query.startsWith(olderQuery) &&
                older.toString() === rest.toString()
            );
        });
        recent.unshift(searchItem(key));
        writeSearchList(RECENT_KEY, recent.slice(0, MAX_RECENT));
        renderSearchShortcuts();
    }

    if (searchForm) {
        let searchTimer = null;
        let searchRequest = null;

        function updateUrl() {
            const params = currentSearchParams();
            history.replaceState(
                null,
                "",
                params.toString()
                    ? `${window.location.pathname}?${params}`
                    : window.location.pathname
            );
            renderSearchShortcuts();
        }

        // Only text in the search bar starts a search; filters and sort on
        // their own rearrange All videos instead.
        async function submitSearch() {
            const params = currentSearchParams();
            updateUrl();
            if (searchRequest) searchRequest.abort();
            if (!params.get("q") && !params.get("similar")) {
                searchRequest = null;
                searchResults.innerHTML = "";
                return;
            }
            searchRequest = new AbortController();
            try {
                const response = await fetch(searchForm.action, {
                    method: "POST",
                    body: toSearchForm(params),
                    headers: { "X-Requested-With": "XMLHttpRequest" },
                    signal: searchRequest.signal
                });
                if (!response.ok) throw new Error("Search request failed.");
                closeViewer();
                searchResults.innerHTML = await response.text();
                sortShownResults();
                processInstagramEmbeds(searchResults);
            } catch (error) {
                if (error.name !== "AbortError") {
                    searchResults.innerHTML =
                        '<div class="inline-error">Search failed. Please try again.</div>';
                }
            }
        }

        function applySearch(paramsText, similarLabel) {
            const params = new URLSearchParams(paramsText);
            searchInput.value = params.get("q") || "";
            setSimilarVideo(params.get("similar")
                ? { url: params.get("similar"), label: similarLabel || "a video" }
                : null);
            filterGroups.forEach((group) => {
                setFilterValue(group, params.get(group.dataset.filter) || "");
            });
            setReversed(params.get("reverse") === "1");
            applySourceParams(params);
            settingsChanged(true);
        }

        function settingsChanged(refetchSearch) {
            clearTimeout(searchTimer);
            if (refetchSearch) {
                submitSearch();
            } else {
                updateUrl();
                sortShownResults();
            }
            reloadLibrary();
            recordRecentSearch();
        }

        searchForm.addEventListener("submit", (event) => {
            event.preventDefault();
            clearTimeout(searchTimer);
            submitSearch();
            recordRecentSearch();
        });

        startSimilarSearch = async (url, label) => {
            clearTimeout(searchTimer);
            searchInput.value = "";
            setSimilarVideo({ url, label });
            await submitSearch();
            recordRecentSearch();
            searchForm.scrollIntoView({ block: "start", behavior: "smooth" });
        };

        if (similarClear) {
            similarClear.addEventListener("click", () => {
                setSimilarVideo(null);
                submitSearch();
                searchInput.focus();
            });
        }

        searchInput.addEventListener("input", () => {
            clearTimeout(searchTimer);
            // Typing switches back to a search for the words.
            if (similarVideo && searchInput.value.trim()) setSimilarVideo(null);
            renderSearchShortcuts();
            searchTimer = window.setTimeout(submitSearch, 350);
        });

        filterGroups.forEach((group) => {
            group.addEventListener("change", (event) => {
                if (!event.target.matches('input[type="radio"]')) return;
                const isSort = group.dataset.filter === "sort";
                // A new sort starts in its natural direction.
                if (isSort) setReversed(false);
                settingsChanged(!isSort);
            });
        });

        if (reverseButton) {
            reverseButton.addEventListener("click", () => {
                setReversed(!isReversed());
                settingsChanged(false);
            });
        }

        function propagateSourceChange(changed) {
            if (changed.dataset.sourcePlatform) {
                platformAccounts(changed.dataset.sourcePlatform).forEach(
                    (box) => { box.checked = changed.checked; }
                );
            } else {
                syncPlatformBox(platformBoxes.find(
                    (box) => box.dataset.sourcePlatform === changed.dataset.platform
                ));
            }
        }

        function sourceChanged(changed) {
            propagateSourceChange(changed);
            if (!anySourceSelected()) {
                // Searching nothing is never useful, so undo the last uncheck.
                changed.checked = true;
                propagateSourceChange(changed);
                if (sourceWarning) sourceWarning.hidden = false;
                return;
            }
            if (sourceWarning) sourceWarning.hidden = true;
            settingsChanged(true);
        }

        [...platformBoxes, ...accountBoxes].forEach((box) => {
            box.addEventListener("change", () => sourceChanged(box));
        });

        if (clearFiltersButton) {
            clearFiltersButton.addEventListener("click", () => {
                filterGroups.forEach((group) => setFilterValue(group, ""));
                setReversed(false);
                setAllSources(true);
                if (sourceWarning) sourceWarning.hidden = true;
                settingsChanged(true);
            });
        }

        if (saveSearchButton) {
            saveSearchButton.addEventListener("click", () => {
                const key = currentSearchParams().toString();
                if (!key) return;
                const saved = readSearchList(SAVED_KEY);
                const updated = saved.some((item) => item.params === key)
                    ? saved.filter((item) => item.params !== key)
                    : [searchItem(key), ...saved];
                writeSearchList(SAVED_KEY, updated);
                renderSearchShortcuts();
            });
        }

        if (shortcuts) {
            shortcuts.addEventListener("click", (event) => {
                const remove = event.target.closest(".chip-remove");
                if (remove) {
                    writeSearchList(
                        SAVED_KEY,
                        readSearchList(SAVED_KEY).filter(
                            (item) => item.params !== remove.dataset.params
                        )
                    );
                    renderSearchShortcuts();
                    return;
                }
                const apply = event.target.closest(".chip-apply");
                if (apply) applySearch(apply.dataset.params, apply.dataset.label);
            });
        }

        if (clearRecentButton) {
            clearRecentButton.addEventListener("click", () => {
                writeSearchList(RECENT_KEY, []);
                renderSearchShortcuts();
            });
        }

        renderSearchShortcuts();
        sortShownResults();
    }

    // ---- Fetching upload dates and likes for older videos ----

    const fetchDetailsButton = document.querySelector("#fetch-details");
    const detailsStatus = document.querySelector("#details-status");

    function showDetailsStatus(data) {
        if (!detailsStatus) return;
        if (data.running) {
            detailsStatus.textContent =
                `Fetching details · ${data.done} of ${data.total}`;
            fetchDetailsButton.hidden = true;
            window.setTimeout(pollDetails, 3000);
            return;
        }
        fetchDetailsButton.hidden = false;
        if (data.error) {
            detailsStatus.textContent = data.error;
            fetchDetailsButton.textContent = "Try again";
        } else if (data.missing > 0 && data.total > 0) {
            detailsStatus.textContent =
                `${data.missing} video${data.missing === 1 ? "" : "s"} could not be checked.`;
            fetchDetailsButton.textContent = "Try again";
        } else {
            detailsStatus.textContent = "Details fetched.";
            fetchDetailsButton.textContent = "Reload to use the new filters";
            fetchDetailsButton.dataset.reload = "true";
        }
    }

    async function pollDetails() {
        try {
            const response = await fetch(fetchDetailsButton.dataset.statusUrl);
            if (!response.ok) throw new Error("Could not read status.");
            showDetailsStatus(await response.json());
        } catch (error) {
            detailsStatus.textContent = "Status unavailable";
            fetchDetailsButton.hidden = false;
        }
    }

    if (fetchDetailsButton) {
        fetchDetailsButton.addEventListener("click", async () => {
            if (fetchDetailsButton.dataset.reload === "true") {
                window.location.reload();
                return;
            }
            fetchDetailsButton.disabled = true;
            try {
                const response = await fetch(
                    fetchDetailsButton.dataset.actionUrl,
                    { method: "POST" }
                );
                if (!response.ok) throw new Error("Could not start.");
                showDetailsStatus(await response.json());
            } catch (error) {
                detailsStatus.textContent = "Could not start fetching details.";
            } finally {
                fetchDetailsButton.disabled = false;
            }
        });
        if (fetchDetailsButton.dataset.running === "true") pollDetails();
    }

    async function loadMoreSearchResults(button) {
        const resultsGrid = searchResults.querySelector(".video-grid");
        const loading = button.parentElement.querySelector(".loading-indicator");
        if (!resultsGrid || button.disabled) return;

        button.disabled = true;
        loading.hidden = false;
        const formData = toSearchForm(
            new URLSearchParams(button.dataset.params),
            button.dataset.offset,
            button.dataset.weak === "1"
        );

        try {
            const response = await fetch("/search", {
                method: "POST",
                body: formData,
                headers: { "X-Requested-With": "XMLHttpRequest" }
            });
            if (!response.ok) throw new Error("Could not load more results.");
            resultsGrid.insertAdjacentHTML("beforeend", await response.text());
            sortShownResults();

            const loadedCount = resultsGrid.querySelectorAll(".video-card").length;
            button.dataset.offset = response.headers.get(
                "X-Search-Next-Offset"
            ) || loadedCount;
            if (response.headers.get("X-Search-Has-More") === "true") {
                button.disabled = false;
            } else if (response.headers.get("X-Search-Has-Weak") === "true") {
                // The strong matches ran out; offer the weaker ones next.
                button.dataset.weak = "1";
                button.firstChild.textContent = "Show weaker matches ";
                button.disabled = false;
            } else {
                button.parentElement.remove();
            }
            const weakEmpty = searchResults.querySelector(".weak-empty-state");
            if (weakEmpty && loadedCount) weakEmpty.remove();
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

    const libraryEmpty = document.querySelector("#library-empty");
    let libraryRequest = null;
    let libraryGeneration = 0;

    function libraryParams() {
        const params = currentSearchParams();
        params.delete("q");
        params.delete("similar");
        return params;
    }

    function libraryFiltered() {
        const params = libraryParams();
        params.delete("sort");
        return params.toString() !== "";
    }

    function setLibraryTotal(total, matching) {
        if (!Number.isFinite(total)) return;
        if (libraryTotal) libraryTotal.textContent = total;
        if (matching === undefined) {
            // Without filters every video is listed; with filters the count
            // comes from the next /videos/count refresh.
            if (libraryFiltered()) return;
            matching = total;
        }
        if (libraryEmpty) libraryEmpty.hidden = matching > 0;
        if (!loadMoreButton) return;
        loadMoreButton.dataset.total = matching;
        const offset = Number(loadMoreButton.dataset.offset);
        if (progress) {
            progress.textContent = `${Math.min(offset, matching)} of ${matching}`;
        }
        if (offset < matching && !loadMoreButton.disabled) {
            loadMoreButton.hidden = false;
        }
    }

    async function refreshLibraryTotal() {
        try {
            const response = await fetch(`/videos/count?${libraryParams()}`, {
                headers: { "X-Requested-With": "XMLHttpRequest" }
            });
            if (!response.ok) return;
            const data = await response.json();
            setLibraryTotal(data.total_videos, data.matching);
        } catch (error) {
            // Keep the last known total; the next refresh will retry.
        }
    }

    async function reloadLibrary() {
        if (!allVideos || !loadMoreButton) return;
        if (libraryRequest) libraryRequest.abort();
        const request = new AbortController();
        libraryRequest = request;
        libraryGeneration += 1;
        const params = libraryParams();
        params.set("offset", 0);
        params.set("limit", searchPageSize());
        loadMoreButton.disabled = true;
        loadingIndicator.hidden = false;
        try {
            const response = await fetch(`/videos?${params}`, {
                headers: { "X-Requested-With": "XMLHttpRequest" },
                signal: request.signal
            });
            if (!response.ok) throw new Error("Could not load videos.");
            const html = await response.text();
            const total = Number(response.headers.get("X-Library-Total"));
            if (viewerState.source === "all") closeViewer();
            allVideos.innerHTML = html;
            const shown = allVideos.querySelectorAll(".video-card").length;
            loadMoreButton.dataset.offset = shown;
            loadMoreButton.dataset.total = total;
            if (progress) progress.textContent = `${shown} of ${total}`;
            loadMoreButton.hidden = shown >= total;
            if (libraryEmpty) libraryEmpty.hidden = total > 0;
            processInstagramEmbeds(allVideos);
        } catch (error) {
            if (error.name !== "AbortError") {
                loadingIndicator.textContent = "Could not load videos. Try again.";
            }
        } finally {
            if (libraryRequest === request) {
                libraryRequest = null;
                loadMoreButton.disabled = false;
                loadingIndicator.hidden = true;
            }
        }
    }

    // Rows added per load. The next load starts well before the end of the
    // grid is on screen (see LOAD_AHEAD_MARGIN), and the page after that is
    // fetched in advance, so scrolling normally never reaches an empty end.
    const LIBRARY_ROWS_PER_LOAD = 2;
    const LOAD_AHEAD_MARGIN = "0px 0px 150% 0px";
    const PREFETCH_MAX_AGE_MS = 15000;
    let prefetchedPage = null;

    function libraryPageParams(offset) {
        const params = libraryParams();
        params.set("offset", offset);
        params.set("limit", videosPerRow() * LIBRARY_ROWS_PER_LOAD);
        return params;
    }

    function requestLibraryPage(params) {
        return fetch(`/videos?${params}`, {
            headers: { "X-Requested-With": "XMLHttpRequest" }
        }).then(async (response) => {
            if (!response.ok) throw new Error("Could not load videos.");
            return response.text();
        });
    }

    // Uses the page fetched in advance when it matches and is recent.
    function fetchLibraryPage(params) {
        const key = `${libraryGeneration}|${params}`;
        const ahead = prefetchedPage;
        prefetchedPage = null;
        if (
            ahead && ahead.key === key &&
            performance.now() - ahead.at < PREFETCH_MAX_AGE_MS
        ) {
            return ahead.page.catch(() => requestLibraryPage(params));
        }
        return requestLibraryPage(params);
    }

    function prefetchLibraryPage(offset) {
        if (offset >= Number(loadMoreButton.dataset.total)) return;
        const params = libraryPageParams(offset);
        const page = requestLibraryPage(params);
        // A failed prefetch is simply fetched again when it is needed.
        page.catch(() => {});
        prefetchedPage = {
            key: `${libraryGeneration}|${params}`,
            at: performance.now(),
            page
        };
    }

    async function loadMoreVideos() {
        if (!loadMoreButton || loadMoreButton.disabled || libraryRequest) return;
        const offset = Number(loadMoreButton.dataset.offset);
        const total = Number(loadMoreButton.dataset.total);
        if (offset >= total) return;

        const generation = libraryGeneration;
        loadMoreButton.disabled = true;
        loadMoreButton.hidden = true;
        loadingIndicator.hidden = false;
        try {
            const html = await fetchLibraryPage(libraryPageParams(offset));
            // The filters or sort changed while this page was loading.
            if (generation !== libraryGeneration) return;
            allVideos.insertAdjacentHTML("beforeend", html);
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
            // Showing the button again makes the observer below load the
            // next rows right away if they are still within reach; if not,
            // they are waiting by the time the reader gets there.
            prefetchLibraryPage(nextOffset);
            processInstagramEmbeds(allVideos);
        } catch (error) {
            loadMoreButton.hidden = false;
            loadMoreButton.disabled = false;
            loadingIndicator.textContent = "Could not load more. Try again.";
        } finally {
            if (generation === libraryGeneration) {
                loadingIndicator.hidden = true;
            }
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
        }, { rootMargin: LOAD_AHEAD_MARGIN });
        observer.observe(loadMoreButton);
    }
})();
