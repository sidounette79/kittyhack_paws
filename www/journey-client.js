// Kittyhack – Journey tab: delete / send-to-Label-Studio actions on
// watchdog-capture cards (09.09, Sid). Delegated click on document.body
// (MutationObserver-safe against Shiny's SPA DOM churn, same pattern as
// photos-correction-client.js / watchdog-cams-client.js).

(function () {
    "use strict";

    document.addEventListener("click", function (e) {
        var btn = e.target.closest(".kh-journey-action-btn");
        if (!btn) return;
        e.preventDefault();
        e.stopPropagation();

        var filename = btn.getAttribute("data-filename");
        var action = btn.getAttribute("data-journey-action");
        if (!filename || !action) return;

        if (typeof Shiny === "undefined" || !Shiny.setInputValue) return;

        if (action === "delete") {
            var card = btn.closest(".kh-journey-capture");
            if (card) {
                card.style.transition = "opacity 0.2s ease";
                card.style.opacity = "0.3";
                card.style.pointerEvents = "none";
            }
        }

        Shiny.setInputValue(
            "journey_capture_action",
            JSON.stringify({ filename: filename, action: action }),
            { priority: "event" }
        );
    });
})();
