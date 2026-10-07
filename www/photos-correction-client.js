// Kittyhack – Detection validate/correct for the ungrouped Photos grid
// (09.09, Sid). Same validate/confirm/false-positive popover as the event
// modal (event-modal.js), adapted for many cards on one page instead of one
// player: delegated click on document.body (MutationObserver-safe against
// Shiny's SPA DOM churn, same pattern as watchdog-cams-client.js), reading
// shared config (correction labels, i18n) from a JSON script tag the server
// re-renders alongside the grid.

(function () {
    "use strict";

    var openPopover = null; // currently open .kh-correction-popover element, or null

    function escHtml(s) {
        if (!s) return "";
        return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
    }

    function getConfig() {
        var el = document.getElementById("kh_photos_correction_data");
        if (!el) return { correctionLabels: [], i18n: {} };
        try {
            return JSON.parse(el.textContent || "{}");
        } catch (e) {
            return { correctionLabels: [], i18n: {} };
        }
    }

    function closePopover() {
        if (openPopover) {
            openPopover.remove();
            openPopover = null;
        }
    }

    function openPopoverFor(labelBtn) {
        closePopover();
        var config = getConfig();
        var currentName = labelBtn.getAttribute("data-obj-name") || "";
        var i18n = config.i18n || {};

        var html = '<div class="kh-correction-popover">';
        html += '<button type="button" class="kh-correction-option kh-correction-confirm" data-correction-action="confirm">'
            + "✓ " + escHtml(i18n.confirm || "Correct") + "</button>";
        (config.correctionLabels || []).forEach(function (lbl) {
            if (lbl === currentName) return;
            html += '<button type="button" class="kh-correction-option" data-correction-action="correct" data-correction-name="'
                + escHtml(lbl) + '">' + escHtml(lbl) + "</button>";
        });
        html += '<button type="button" class="kh-correction-option kh-correction-false-positive" data-correction-action="false_positive">'
            + "✗ " + escHtml(i18n.falsePositive || "Not a real detection") + "</button>";
        html += "</div>";

        var wrap = document.createElement("div");
        wrap.innerHTML = html;
        var popover = wrap.firstChild;

        // 05.10, Sid ("je ne peux pas corriger"): the photos grid auto-
        // refreshes every ~3s whenever a new photo arrives (very often -
        // see session_setup.py's ext_trigger_reload_photos), which replaces
        // the whole card and used to destroy an anchored-to-the-card
        // popover before it could be clicked. Anchor to <body> instead
        // (survives the card being replaced) and capture pid/objIdx now,
        // not at submit time, since the original labelBtn node may be gone
        // by then even if a fresh one for the same photo/object exists.
        popover.setAttribute("data-pid", labelBtn.getAttribute("data-pid") || "");
        popover.setAttribute("data-obj-idx", labelBtn.getAttribute("data-obj-idx") || "");
        document.body.appendChild(popover);
        var labelRect = labelBtn.getBoundingClientRect();
        popover.style.position = "fixed";
        popover.style.zIndex = "2000";
        popover.style.top = (labelRect.bottom + 4) + "px";
        popover.style.left = labelRect.left + "px";

        // Keep it from overflowing the viewport horizontally - flip to the
        // right edge if it would spill past the window's right side.
        var rect = popover.getBoundingClientRect();
        if (rect.right > window.innerWidth) {
            popover.style.left = "auto";
            popover.style.right = (window.innerWidth - labelRect.right) + "px";
        }
        // Same idea vertically, in case the label is near the bottom of the viewport.
        if (rect.bottom > window.innerHeight) {
            popover.style.top = "auto";
            popover.style.bottom = (window.innerHeight - labelRect.top + 4) + "px";
        }

        openPopover = popover;
    }

    function submitCorrection(pid, objIdx, action, correctedName, labelBtn) {
        try {
            if (typeof Shiny === "undefined" || !Shiny.setInputValue) return;
            Shiny.setInputValue(
                "photos_client_correction",
                JSON.stringify({ pid: pid, idx: objIdx, action: action, correctedName: correctedName }),
                { priority: "event" }
            );
            // Optimistic local update - best-effort only, the labelBtn node
            // may be gone if the grid auto-refreshed since the popover
            // opened; a later full re-render reflects the result either way.
            if (!labelBtn) return;
            var baseText = labelBtn.getAttribute("data-obj-name") || "";
            var probMatch = labelBtn.textContent.match(/\(([0-9]+)%\)/);
            var prob = probMatch ? probMatch[0] : "";
            if (action === "confirm") {
                labelBtn.textContent = "✓ " + baseText + " " + prob;
            } else if (action === "false_positive") {
                labelBtn.textContent = "✗ " + baseText + " " + prob;
            } else if (action === "correct") {
                labelBtn.textContent = "✓ " + baseText + " " + prob + " → " + correctedName;
            }
        } catch (e) {}
    }

    // ---- 05.10, Sid ("je puisse tagger directement depuis ces photos"):
    // draw a box for a detection the model missed entirely, without having
    // to send the picture to Label Studio first. Click the "+" button on a
    // card to arm it, drag a rectangle on the thumbnail, then pick which
    // cat (or Prey) it is from a popover - same submit path/table as a
    // correction, just with no "original" detection behind it.

    var addModePid = null; // pid currently armed for drawing, or null
    var drawState = null; // {thumb, rect, startX, startY, el} while dragging

    function exitAddMode() {
        if (addModePid !== null) {
            var thumb = document.querySelector('.kh-photo-thumb[data-photo-id="' + addModePid + '"]');
            if (thumb) {
                thumb.classList.remove("kh-add-detection-mode");
                delete thumb.dataset.khDrawing;
            }
        }
        addModePid = null;
        if (drawState && drawState.el) drawState.el.remove();
        drawState = null;
    }

    function updateDrawRect(clientX, clientY) {
        var rect = drawState.rect;
        var x1 = Math.min(drawState.startX, clientX), x2 = Math.max(drawState.startX, clientX);
        var y1 = Math.min(drawState.startY, clientY), y2 = Math.max(drawState.startY, clientY);
        drawState.el.style.left = (x1 - rect.left) + "px";
        drawState.el.style.top = (y1 - rect.top) + "px";
        drawState.el.style.width = (x2 - x1) + "px";
        drawState.el.style.height = (y2 - y1) + "px";
    }

    function openAddPopoverFor(pid, x, y, width, height, anchorClientX, anchorClientY) {
        closePopover();
        var config = getConfig();
        var html = '<div class="kh-correction-popover">';
        (config.correctionLabels || []).forEach(function (lbl) {
            html += '<button type="button" class="kh-correction-option kh-add-detection-option" data-add-name="'
                + escHtml(lbl) + '">' + escHtml(lbl) + "</button>";
        });
        html += '<button type="button" class="kh-correction-option" data-add-cancel="1">'
            + escHtml("Annuler") + "</button>";
        html += "</div>";

        var wrap = document.createElement("div");
        wrap.innerHTML = html;
        var popover = wrap.firstChild;
        popover.setAttribute("data-add-pid", pid);
        popover.setAttribute("data-add-x", x);
        popover.setAttribute("data-add-y", y);
        popover.setAttribute("data-add-width", width);
        popover.setAttribute("data-add-height", height);

        document.body.appendChild(popover);
        popover.style.position = "fixed";
        popover.style.zIndex = "2000";
        popover.style.top = (anchorClientY + 4) + "px";
        popover.style.left = anchorClientX + "px";
        var rect = popover.getBoundingClientRect();
        if (rect.right > window.innerWidth) {
            popover.style.left = "auto";
            popover.style.right = "4px";
        }
        if (rect.bottom > window.innerHeight) {
            popover.style.top = "auto";
            popover.style.bottom = "4px";
        }
        openPopover = popover;
    }

    document.addEventListener("pointerdown", function (e) {
        if (addModePid === null) return;
        var thumb = e.target.closest('.kh-photo-thumb[data-photo-id="' + addModePid + '"]');
        if (!thumb) return;
        e.preventDefault();
        var el = document.createElement("div");
        el.className = "kh-draw-rect";
        thumb.appendChild(el);
        drawState = {
            thumb: thumb,
            rect: thumb.getBoundingClientRect(),
            startX: e.clientX, startY: e.clientY,
            el: el,
        };
        updateDrawRect(e.clientX, e.clientY);
    });

    document.addEventListener("pointermove", function (e) {
        if (!drawState) return;
        updateDrawRect(e.clientX, e.clientY);
    });

    document.addEventListener("pointerup", function (e) {
        if (!drawState) return;
        var rect = drawState.rect;
        var x1 = Math.min(drawState.startX, e.clientX), x2 = Math.max(drawState.startX, e.clientX);
        var y1 = Math.min(drawState.startY, e.clientY), y2 = Math.max(drawState.startY, e.clientY);
        var pctX = (x1 - rect.left) / rect.width * 100;
        var pctY = (y1 - rect.top) / rect.height * 100;
        var pctW = (x2 - x1) / rect.width * 100;
        var pctH = (y2 - y1) / rect.height * 100;
        var pid = addModePid;
        var thumb = drawState.thumb;
        drawState.el.remove();
        drawState = null;

        // Too small to be a deliberate drag - treat as a mis-tap, don't open
        // the picker, but keep add-mode armed (and khDrawing set) so the
        // inevitable click right after this pointerup still doesn't open
        // the lightbox, and she can just try the drag again.
        if (pctW < 3 || pctH < 3) return;

        openAddPopoverFor(
            pid,
            Math.max(0, pctX), Math.max(0, pctY),
            Math.min(100, pctW), Math.min(100, pctH),
            e.clientX, e.clientY
        );
        // Not exitAddMode() here - that clears khDrawing synchronously, but
        // the click event app.js listens for fires right after this same
        // pointerup, so it would still see khDrawing unset and open the
        // lightbox. Keep the flag one tick longer than the class/mode state.
        addModePid = null;
        thumb.classList.remove("kh-add-detection-mode");
        setTimeout(function () { delete thumb.dataset.khDrawing; }, 0);
    });

    document.addEventListener("click", function (e) {
        var addBtn = e.target.closest(".kh-photo-add-detection-btn");
        if (addBtn) {
            e.preventDefault();
            e.stopPropagation();
            var btnPid = parseInt(addBtn.getAttribute("data-pid"), 10);
            if (addModePid === btnPid) {
                exitAddMode();
            } else {
                exitAddMode();
                var thumb = document.querySelector('.kh-photo-thumb[data-photo-id="' + btnPid + '"]');
                if (thumb) {
                    addModePid = btnPid;
                    thumb.classList.add("kh-add-detection-mode");
                    thumb.dataset.khDrawing = "1";
                }
            }
            return;
        }

        var addOption = e.target.closest(".kh-add-detection-option");
        if (addOption) {
            e.preventDefault();
            e.stopPropagation();
            var addPopover = addOption.closest(".kh-correction-popover");
            if (addPopover && typeof Shiny !== "undefined" && Shiny.setInputValue) {
                Shiny.setInputValue(
                    "photos_client_add_detection",
                    JSON.stringify({
                        pid: parseInt(addPopover.getAttribute("data-add-pid"), 10),
                        x: parseFloat(addPopover.getAttribute("data-add-x")),
                        y: parseFloat(addPopover.getAttribute("data-add-y")),
                        width: parseFloat(addPopover.getAttribute("data-add-width")),
                        height: parseFloat(addPopover.getAttribute("data-add-height")),
                        label: addOption.getAttribute("data-add-name"),
                    }),
                    { priority: "event" }
                );
            }
            closePopover();
            return;
        }

        if (e.target.closest("[data-add-cancel]")) {
            e.preventDefault();
            e.stopPropagation();
            closePopover();
            return;
        }

        var option = e.target.closest(".kh-correction-option");
        if (option) {
            e.preventDefault();
            e.stopPropagation();
            var popover = option.closest(".kh-correction-popover");
            var pid = popover ? parseInt(popover.getAttribute("data-pid"), 10) : NaN;
            var objIdx = popover ? parseInt(popover.getAttribute("data-obj-idx"), 10) : NaN;
            // Fresh lookup - the node captured when the popover opened may
            // have been replaced by an auto-refresh since (see openPopoverFor).
            var labelBtn = !isNaN(pid)
                ? document.querySelector(
                      '.kh-detect-label-clickable[data-pid="' + pid + '"][data-obj-idx="' + objIdx + '"]'
                  )
                : null;
            if (!isNaN(pid) && !isNaN(objIdx)) {
                var action = option.getAttribute("data-correction-action");
                var correctedName = option.getAttribute("data-correction-name") || null;
                submitCorrection(pid, objIdx, action, correctedName, labelBtn);
            }
            closePopover();
            return;
        }

        var label = e.target.closest(".kh-detect-label-clickable");
        if (label) {
            e.preventDefault();
            e.stopPropagation();
            if (openPopover
                && openPopover.getAttribute("data-pid") === label.getAttribute("data-pid")
                && openPopover.getAttribute("data-obj-idx") === label.getAttribute("data-obj-idx")) {
                closePopover();
            } else {
                openPopoverFor(label);
            }
            return;
        }

        // Click elsewhere - close any open popover.
        closePopover();
    });
})();
